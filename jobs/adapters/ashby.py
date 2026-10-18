"""Ashby job-board adapter.

Public JSON API, no key: https://api.ashbyhq.com/posting-api/job-board/{slug}
returns every listed posting with plain-text and HTML descriptions, workplace type and,
with includeCompensation=true, a salary summary when the company publishes one.
"""

from __future__ import annotations

import logging

import requests

from jobs.html_text import html_to_text, squash
from jobs.http import PoliteClient
from jobs.models import Job, Workplace, guess_workplace

log = logging.getLogger(__name__)

SOURCE = "ashby"

WORKPLACE_TYPES: dict[str, Workplace] = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite", "on-site": "onsite"}


def board_url(slug: str) -> str:
    return f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"


def parse_board(data: dict, slug: str, company_name: str | None = None) -> list[Job]:
    jobs: list[Job] = []
    for item in data.get("jobs", []):
        job_id = str(item.get("id") or "")
        if not job_id or item.get("isListed") is False:
            continue
        title = squash(item.get("title") or "")
        locations = [squash(item.get("location") or "")] + [squash(s.get("location") or "") for s in item.get("secondaryLocations") or []]
        location = ", ".join(loc for loc in locations if loc)
        country = ((item.get("address") or {}).get("postalAddress") or {}).get("addressCountry") or ""
        if country and country.lower() not in location.lower():
            location = f"{location}, {country}" if location else country
        description = squash_paragraphs(item.get("descriptionPlain") or "") or html_to_text(item.get("descriptionHtml") or "")
        workplace = WORKPLACE_TYPES.get(str(item.get("workplaceType") or "").lower())
        if workplace is None:
            workplace = "remote" if item.get("isRemote") else guess_workplace(title, location, description)
        published = item.get("publishedAt") or ""
        comp = item.get("compensation") or {}
        salary = comp.get("scrapeableCompensationSalarySummary") or comp.get("compensationTierSummary") or None
        jobs.append(
            Job(
                source=SOURCE,
                source_id=job_id,
                title=title,
                company=company_name or slug,
                location=location,
                workplace=workplace,
                seniority="",
                posted_date=published[:10] if len(published) >= 10 else "",
                url=item.get("jobUrl") or f"https://jobs.ashbyhq.com/{slug}/{job_id}",
                description=description,
                salary=salary,
                extra={
                    "slug": slug,
                    "department": item.get("department") or "",
                    "team": item.get("team") or "",
                    "employment_type": item.get("employmentType") or "",
                },
            )
        )
    return jobs


def squash_paragraphs(text: str) -> str:
    """Trim each line and collapse runs of blank lines; keeps the paragraph structure of descriptionPlain."""
    lines = [line.strip() for line in text.splitlines()]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def fetch(slug: str, company_name: str | None = None, client: PoliteClient | None = None) -> list[Job]:
    client = client or PoliteClient()
    url = board_url(slug)
    log.info("ashby: fetching %s", url)
    try:
        resp = client.get(url)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            raise ValueError(f"no Ashby job board named {slug!r}") from exc
        raise
    jobs = parse_board(resp.json(), slug, company_name)
    log.info("ashby: %s -> %d jobs", slug, len(jobs))
    return jobs
