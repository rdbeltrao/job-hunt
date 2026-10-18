"""Greenhouse job-board adapter.

Public JSON API, no key: https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
returns every open posting with its HTML description (HTML-escaped inside the JSON).
"""

from __future__ import annotations

import html
import logging

import requests

from jobs.html_text import html_to_text, squash
from jobs.http import PoliteClient
from jobs.models import Job, guess_workplace

log = logging.getLogger(__name__)

SOURCE = "greenhouse"


def board_url(slug: str) -> str:
    return f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"


def parse_board(data: dict, slug: str, company_name: str | None = None) -> list[Job]:
    jobs: list[Job] = []
    for item in data.get("jobs", []):
        job_id = str(item.get("id") or "")
        if not job_id:
            continue
        title = squash(item.get("title") or "")
        location = squash((item.get("location") or {}).get("name") or "")
        offices = [squash(o.get("name") or "") for o in item.get("offices") or [] if o.get("name")]
        if not location and offices:
            location = ", ".join(offices)
        # The content field is HTML that was HTML-escaped once more for JSON transport.
        description = html_to_text(html.unescape(item.get("content") or ""))
        published = item.get("first_published") or item.get("updated_at") or ""
        departments = [d.get("name") for d in item.get("departments") or [] if d.get("name")]
        metadata = {m.get("name"): m.get("value") for m in item.get("metadata") or [] if m.get("name") and m.get("value") not in (None, "", [])}
        seniority = str(metadata.get("Management Level") or metadata.get("Level") or metadata.get("Seniority") or "")
        jobs.append(
            Job(
                source=SOURCE,
                source_id=job_id,
                title=title,
                company=company_name or item.get("company_name") or slug,
                location=location,
                workplace=guess_workplace(title, location, description),
                seniority=seniority,
                posted_date=published[:10] if len(published) >= 10 else "",
                url=item.get("absolute_url") or f"https://job-boards.greenhouse.io/{slug}/jobs/{job_id}",
                description=description,
                salary=None,
                extra={"slug": slug, "departments": departments, "offices": offices, "language": item.get("language") or ""},
            )
        )
    return jobs


def fetch(slug: str, company_name: str | None = None, client: PoliteClient | None = None) -> list[Job]:
    client = client or PoliteClient()
    url = board_url(slug)
    log.info("greenhouse: fetching %s", url)
    try:
        resp = client.get(url)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            raise ValueError(f"no Greenhouse board named {slug!r}") from exc
        raise
    jobs = parse_board(resp.json(), slug, company_name)
    log.info("greenhouse: %s -> %d jobs", slug, len(jobs))
    return jobs
