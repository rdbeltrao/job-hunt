"""Weekly LinkedIn connection plan built from the openings CSV.

The idea (from Ksu Dranitsina's LinkedIn guide): recruiters filter on "has connections at
our company" and "follows our company", so the network should grow at the companies whose
openings you like or applied to. This module ranks those companies and writes a checklist
with the LinkedIn search links to work through by hand. Nothing here touches LinkedIn.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import quote_plus

DEFAULT_DONE_PATH = Path("out/network.json")

# Role families to connect with at each company, in priority order: peers with the target
# title, the level above, then the people who run the hiring.
DEFAULT_ROLES = [
    "Engineering Manager",
    "Head of Engineering",
    "VP Engineering",
    "CTO",
    "Technical Recruiter",
]
DEFAULT_WEEKLY_QUOTA = 100
DEFAULT_PER_COMPANY = 10

# Verdicts and statuses that make a company worth connecting at.
INTERESTING_VERDICTS = ("apply", "maybe")
ACTIVE_STATUSES = ("draft", "applied", "replied", "interview", "offer")
CLOSED_STATUSES = ("rejected", "withdrawn", "skipped")


@dataclass
class Opening:
    job_id: str
    title: str
    url: str
    verdict: str
    status: str
    contact_name: str = ""  # the hiring-team person the posting showed, when any
    contact_title: str = ""
    contact_url: str = ""


@dataclass
class CompanyPlan:
    company: str
    openings: list[Opening] = field(default_factory=list)
    company_url: str = ""  # the LinkedIn company page when a posting linked it, else ""

    @property
    def score(self) -> int:
        """An application in progress counts most, then apply verdicts, then maybes.

        Counts are capped so a company that posts the same role in twenty cities does not
        outrank one you actually applied to.
        """
        active = sum(1 for o in self.openings if o.status in ACTIVE_STATUSES)
        applies = sum(1 for o in self.openings if o.verdict == "apply")
        maybes = sum(1 for o in self.openings if o.verdict == "maybe")
        return 10 * min(active, 2) + 3 * min(applies, 3) + min(maybes, 4)

    @property
    def applied(self) -> bool:
        return any(o.status in ACTIVE_STATUSES for o in self.openings)


def load_done(path: Path = DEFAULT_DONE_PATH) -> dict[str, str]:
    """Companies already covered, lower-cased name -> ISO date."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {str(k).lower(): str(v) for k, v in data.items()}


def save_done(done: dict[str, str], path: Path = DEFAULT_DONE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(sorted(done.items())), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def mark_done(companies: list[str], done: dict[str, str], today: str | None = None) -> dict[str, str]:
    today = today or date.today().isoformat()
    for name in companies:
        done[name.strip().lower()] = today
    return done


def rank_companies(rows: list[dict[str, str]], done: dict[str, str] | None = None) -> list[CompanyPlan]:
    """Companies from the CSV worth connecting at, best first; companies in `done` are left out.

    A row counts when its verdict is apply or maybe, or its status shows an application in
    progress. Rejected, withdrawn and skipped rows count for nothing, so a company whose
    only opening was rejected does not appear.
    """
    done = done or {}
    plans: dict[str, CompanyPlan] = {}
    for r in rows:
        company = (r.get("company") or "").strip()
        if not company or company.lower() in done:
            continue
        status = (r.get("status") or "").strip()
        verdict = (r.get("verdict") or "").strip()
        if status in CLOSED_STATUSES:
            continue
        if verdict not in INTERESTING_VERDICTS and status not in ACTIVE_STATUSES:
            continue
        plan = plans.setdefault(company.lower(), CompanyPlan(company=company))
        plan.company_url = plan.company_url or (r.get("company_url") or "").strip()
        plan.openings.append(
            Opening(
                r.get("job_id", ""),
                r.get("title", ""),
                r.get("url", ""),
                verdict,
                status,
                contact_name=(r.get("contact_name") or "").strip(),
                contact_title=(r.get("contact_title") or "").strip(),
                contact_url=(r.get("contact_url") or "").strip(),
            )
        )
    return sorted(plans.values(), key=lambda p: (-p.score, p.company.lower()))


def people_search_url(company: str, role: str) -> str:
    return f"https://www.linkedin.com/search/results/people/?keywords={quote_plus(f'{company} {role}')}&origin=GLOBAL_SEARCH_HEADER"


def company_search_url(company: str) -> str:
    return f"https://www.linkedin.com/search/results/companies/?keywords={quote_plus(company)}"


def job_posts_search_url(title: str) -> str:
    """Posts that are job ads (free postings from small companies, invisible in Jobs search)."""
    return f"https://www.linkedin.com/search/results/content/?keywords={quote_plus(chr(34) + title + chr(34))}&contentType=%22jobs%22"


def render_plan(
    plans: list[CompanyPlan],
    roles: list[str] | None = None,
    quota: int = DEFAULT_WEEKLY_QUOTA,
    per_company: int = DEFAULT_PER_COMPANY,
    today: str | None = None,
) -> str:
    """Markdown checklist: one block per company with the links to open in LinkedIn."""
    roles = roles or DEFAULT_ROLES
    today = today or date.today().isoformat()
    lines = [
        f"# LinkedIn connection plan, week of {today}",
        "",
        f"Budget: {quota} invitations this week, about {per_company} per company, Connect (not Follow), "
        "a note only when there is something real to say. Follow each company page and press "
        '"I am interested" where it is offered. Mark a company covered with '
        "`uv run python -m jobs network --done \"<company>\"`.",
        "",
    ]
    if not plans:
        lines.append("Nothing to do: every company with an interesting opening is already marked done.")
        return "\n".join(lines) + "\n"
    for i, plan in enumerate(plans, 1):
        tag = "applied" if plan.applied else f"{len(plan.openings)} opening{'s' if len(plan.openings) != 1 else ''}"
        lines.append(f"## {i}. {plan.company} ({tag})")
        lines.append("")
        for o in plan.openings[:3]:
            state = o.status if o.status and o.status != "new" else o.verdict
            lines.append(f"- {o.title} [{state}]({o.url})" if o.url else f"- {o.title} [{state}]")
        for o in plan.openings:
            if o.contact_url:
                who = f"{o.contact_name} ({o.contact_title})" if o.contact_title else o.contact_name
                lines.append(f"- [ ] Message the job poster of {o.title}: {who} {o.contact_url}")
        lines.append(f"- [ ] Follow the company page: {plan.company_url or company_search_url(plan.company)}")
        for role in roles:
            lines.append(f"- [ ] {role}: {people_search_url(plan.company, role)}")
        lines.append("")
    return "\n".join(lines) + "\n"
