"""Personio career-page adapter.

Every Personio-hosted career page exposes an XML feed at
https://{slug}.jobs.personio.de/xml with one <position> element per opening.

The feed without a `language` parameter is rendered in the company's default career-page
language. A position written only in another language then comes back with an empty
<jobDescriptions/> element (Wandelbots: default feed empty, `?language=en` complete;
Everphone: the reverse). `fetch` therefore re-requests the feed per language in
DESCRIPTION_LANGUAGES for the positions that are still missing a description.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

from jobs.html_text import html_to_text, squash
from jobs.http import PoliteClient
from jobs.models import Job, guess_workplace

log = logging.getLogger(__name__)

SOURCE = "personio"

# Languages tried, in order, for positions whose description is empty in the default feed.
DESCRIPTION_LANGUAGES = ("en", "de")


def feed_url(slug: str, language: str | None = None) -> str:
    url = f"https://{slug}.jobs.personio.de/xml"
    return f"{url}?language={language}" if language else url


def job_url(slug: str, position_id: str) -> str:
    return f"https://{slug}.jobs.personio.de/job/{position_id}"


def _text(el: ET.Element | None, path: str) -> str:
    if el is None:
        return ""
    found = el.find(path)
    return squash(found.text or "") if found is not None and found.text else ""


def parse_feed(xml_text: str | bytes, slug: str, company_name: str | None = None) -> list[Job]:
    # Bytes are preferred: ElementTree then honours the feed's own encoding declaration.
    root = ET.fromstring(xml_text)
    jobs: list[Job] = []
    for pos in root.iter("position"):
        position_id = _text(pos, "id")
        if not position_id:
            continue
        title = _text(pos, "name")
        office = _text(pos, "office")
        extra_offices = [squash(o.text or "") for o in pos.findall("additionalOffices/office") if o.text]
        location = ", ".join([o for o in [office, *extra_offices] if o])

        description = _description(pos)

        created = _text(pos, "createdAt")
        posted_date = created[:10] if len(created) >= 10 else ""
        subcompany = _text(pos, "subcompany")
        company = company_name or subcompany or slug

        jobs.append(
            Job(
                source=SOURCE,
                source_id=position_id,
                title=title,
                company=company,
                location=location,
                workplace=guess_workplace(title, location, description),
                seniority=_text(pos, "seniority"),
                posted_date=posted_date,
                url=job_url(slug, position_id),
                description=description,
                salary=None,
                extra={
                    "slug": slug,
                    "subcompany": subcompany,
                    "department": _text(pos, "department"),
                    "employment_type": _text(pos, "employmentType"),
                    "schedule": _text(pos, "schedule"),
                    "years_of_experience": _text(pos, "yearsOfExperience"),
                    "recruiting_category": _text(pos, "recruitingCategory"),
                },
            )
        )
    return jobs


def parse_descriptions(xml_text: str | bytes) -> dict[str, str]:
    """Map position id to its plain-text description; positions without one are left out."""
    root = ET.fromstring(xml_text)
    out: dict[str, str] = {}
    for pos in root.iter("position"):
        position_id = _text(pos, "id")
        description = _description(pos)
        if position_id and description:
            out[position_id] = description
    return out


def fill_missing_descriptions(jobs: list[Job], xml_text: str | bytes) -> int:
    """Fill empty descriptions from another language's feed. Returns how many were filled."""
    missing = {j.source_id: j for j in jobs if not j.description}
    if not missing:
        return 0
    filled = 0
    for position_id, description in parse_descriptions(xml_text).items():
        job = missing.get(position_id)
        if job is None:
            continue
        job.description = description
        job.workplace = guess_workplace(job.title, job.location, description)
        filled += 1
    return filled


def _description(pos: ET.Element) -> str:
    """Join the <jobDescription> sections of a position into headed plain text.

    Handles the usual CDATA-wrapped HTML in <value>, nested markup inside <value> that
    was not escaped, and the empty <jobDescriptions/> Personio emits when a position has
    no text in the requested language.
    """
    sections: list[str] = []
    for desc in pos.findall("jobDescriptions/jobDescription"):
        heading = _text(desc, "name")
        value_el = desc.find("value")
        if value_el is None:
            continue
        raw = value_el.text or ""
        if len(value_el):
            # Unescaped markup was parsed into child elements; serialise it back to HTML.
            raw = "".join(ET.tostring(child, encoding="unicode", method="html") for child in value_el)
            raw = (value_el.text or "") + raw
        body = html_to_text(raw)
        if heading and body:
            sections.append(f"{heading}\n{body}")
        elif body:
            sections.append(body)
    return "\n\n".join(sections)


def fetch(slug: str, company_name: str | None = None, client: PoliteClient | None = None) -> list[Job]:
    client = client or PoliteClient()
    jobs = parse_feed(_get_feed(client, feed_url(slug)), slug, company_name)
    log.info("personio: %s -> %d positions", slug, len(jobs))
    for language in DESCRIPTION_LANGUAGES:
        missing = sum(1 for j in jobs if not j.description)
        if not missing:
            break
        log.info("personio: %s -> %d positions without a description, trying language=%s", slug, missing, language)
        try:
            filled = fill_missing_descriptions(jobs, _get_feed(client, feed_url(slug, language)))
        except Exception as exc:  # noqa: BLE001 - a missing translation must not fail the slug
            log.warning("personio: %s language=%s feed failed: %s", slug, language, exc)
            continue
        log.info("personio: %s language=%s filled %d descriptions", slug, language, filled)
    return jobs


def _get_feed(client: PoliteClient, url: str) -> bytes:
    log.info("personio: fetching %s", url)
    resp = client.get(url)
    content_type = resp.headers.get("Content-Type", "")
    # Personio serves the feed without a charset in Content-Type, so requests would guess
    # latin-1 for resp.text. Parse the raw bytes and let the XML declaration decide.
    body = resp.content
    if "xml" not in content_type and not body.lstrip().startswith(b"<?xml") and b"<workzag-jobs" not in body:
        raise ValueError(f"{url} did not return a Personio XML feed (Content-Type: {content_type!r})")
    return body
