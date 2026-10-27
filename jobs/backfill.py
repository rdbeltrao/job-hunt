"""Backfill the LinkedIn company page and hiring contact on openings fetched before they were read.

`fetch` only opens a posting once (its id goes to out/seen_ids.json), so openings already
in out/jobs.csv never get `company_url`, `contact_name`, `contact_title` and `contact_url`
on their own. `jobs backfill` opens each such posting's page in the logged-in browser,
at the same human pace as fetch, parses the detail pane with the fetch parser and writes
the four columns into the CSV (and into out/jobs.jsonl for jobs still there). Every
other column stays as it is.

Visited ids are kept in out/backfill.json so a posting that shows no hiring team is not
opened again on the next run; `--retry` opens them once more.
"""

from __future__ import annotations

import json
import logging
import random
import re
from dataclasses import dataclass
from pathlib import Path

from jobs.export import LINKEDIN_COLUMNS
from jobs.models import Job

log = logging.getLogger(__name__)

DEFAULT_DONE_PATH = Path("out/backfill.json")
DEFAULT_VERDICTS = ("apply", "maybe")
# Rows with an application in progress are opened first, then apply, then maybe, each by score.
ACTIVE_STATUSES = ("draft", "applied", "replied", "interview", "offer")
CLOSED_STATUSES = ("skipped", "rejected", "withdrawn")
JOB_WAIT_S = (3.0, 7.0)
JOB_VIEW_URL = "https://www.linkedin.com/jobs/view/{job_id}/"
DETAIL_WAIT_SELECTOR = '#job-details, .jobs-description__content, .jobs-description-content__text, .jobs-box__html-content, section[aria-label="Primary content"] [data-testid="expandable-text-box"]'
_SOURCE_ID_RE = re.compile(r"^linkedin:(\d+)$")
_NOT_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_COMPANY_SUFFIX_RE = re.compile(r"\b(gmbh|ag|se|inc|llc|ltd|limited|co|kg|sa|sas|srl|bv|ab|oy|plc|group|labs?|technologies|technology|gmbh & co kg)\b")


class WrongPosting(Exception):
    """LinkedIn answered with another posting (the one asked for is gone), so nothing on the page is this job's."""


def _company_key(name: str) -> str:
    low = _NOT_ALNUM_RE.sub(" ", (name or "").lower())
    low = _COMPANY_SUFFIX_RE.sub(" ", low)
    return _NOT_ALNUM_RE.sub("", low)


def same_company(row_company: str, page_company: str) -> bool:
    """True when the page's company name is plausibly the row's (legal suffixes, punctuation and case ignored).

    An empty page name is accepted: the 2023 markup sometimes hides it, and the URL
    check in `open_job_page` already caught a redirect. A different name is not.
    """
    a, b = _company_key(row_company), _company_key(page_company)
    if not a or not b:
        return True
    return a == b or a.startswith(b) or b.startswith(a) or a in b or b in a


@dataclass(frozen=True)
class Target:
    job_id: str
    source_id: str
    score: int
    verdict: str
    status: str


def load_done(path: Path = DEFAULT_DONE_PATH) -> set[str]:
    """job_ids already opened by an earlier backfill run."""
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {str(x) for x in data} if isinstance(data, list) else set()


def save_done(done: set[str], path: Path = DEFAULT_DONE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(done), indent=0) + "\n", encoding="utf-8")


def needs_backfill(row: dict[str, str]) -> bool:
    """A LinkedIn row with none of the four columns filled."""
    return row.get("source") == "linkedin" and not any((row.get(col) or "").strip() for col in LINKEDIN_COLUMNS)


def select_targets(
    rows: list[dict[str, str]],
    done: set[str] | None = None,
    verdicts: tuple[str, ...] = DEFAULT_VERDICTS,
    everything: bool = False,
    limit: int | None = None,
) -> list[Target]:
    """Rows to open, best first: active applications, then the wanted verdicts by score.

    Closed rows (skipped, rejected, withdrawn) are never opened. With `everything`, the
    verdict filter is off and unscored rows count too. `done` rows are left out.
    """
    done = done or set()
    targets: list[Target] = []
    for row in rows:
        job_id = row.get("job_id", "")
        m = _SOURCE_ID_RE.match(job_id)
        if not m or not needs_backfill(row) or job_id in done:
            continue
        status = (row.get("status") or "").strip()
        verdict = (row.get("verdict") or "").strip()
        if status in CLOSED_STATUSES:
            continue
        if not everything and verdict not in verdicts and status not in ACTIVE_STATUSES:
            continue
        score = int(row["score"]) if (row.get("score") or "").strip().isdigit() else -1
        targets.append(Target(job_id=job_id, source_id=m.group(1), score=score, verdict=verdict, status=status))

    def rank(t: Target) -> tuple[int, int, int]:
        active = 0 if t.status in ACTIVE_STATUSES else 1
        verdict_rank = {"apply": 0, "maybe": 1}.get(t.verdict, 2)
        return (active, verdict_rank, -t.score)

    targets.sort(key=rank)
    return targets[:limit] if limit is not None else targets


def apply_detail(row: dict[str, str], detail, job: Job | None = None) -> bool:
    """Copy the four fields from a parsed Detail into the row (and the Job). True when anything was found.

    Raises WrongPosting when the page names another company: LinkedIn replaces a
    removed posting with a recommended one, and that job's contact is not this row's.
    """
    if not same_company(row.get("company", ""), detail.company):
        raise WrongPosting(f"page is for {detail.company!r}, row is {row.get('company')!r}")
    values = {
        "company_url": detail.company_url,
        "contact_name": detail.contact_name,
        "contact_title": detail.contact_title,
        "contact_url": detail.contact_url,
    }
    if not any(values.values()):
        return False
    for col, value in values.items():
        row[col] = value or ""
        if job is not None:
            setattr(job, col, value or "")
    return True


# ---------------------------------------------------------------- browser driving


def open_job_page(context, source_id: str, dump_dir: Path | None = None) -> str:
    """Load the posting's own page and return the HTML of its detail pane (or the whole page)."""
    from jobs.adapters import linkedin_browser as lb
    from jobs.browser import assert_logged_in, first_page

    page = first_page(context)
    page.goto(JOB_VIEW_URL.format(job_id=source_id), wait_until="domcontentloaded")
    page.wait_for_timeout(random.randint(800, 1500))
    assert_logged_in(page)
    if f"/jobs/view/{source_id}" not in page.url and f"currentJobId={source_id}" not in page.url:
        raise WrongPosting(f"landed on {page.url}")
    try:
        page.wait_for_selector(DETAIL_WAIT_SELECTOR, timeout=8000)
    except Exception:  # noqa: BLE001 - an expired posting shows no description; the top card still carries the company
        log.debug("backfill: description selector did not appear for %s", source_id)
    page.wait_for_timeout(random.randint(400, 900))
    # Keep the document title as well as the scoped primary content: the current
    # LinkedIn layout carries its title/company there and its job ID in a banner.
    html = page.content()
    if lb.parse_detail(html).job_id != source_id:
        raise WrongPosting(f"primary job identity does not match {source_id}")
    if dump_dir:
        lb._dump(dump_dir, f"backfill-{source_id}-detail.json", json.dumps(vars(lb.parse_detail(html)), ensure_ascii=False))
    return html


def sleep_between_jobs() -> None:
    import time

    time.sleep(random.uniform(*JOB_WAIT_S))
