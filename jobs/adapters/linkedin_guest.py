"""LinkedIn guest (logged-out) job search adapter. DISABLED BY DEFAULT.

LinkedIn answers HTTP 429 after a handful of detail requests (4 of 10 in the first real
run, and even 3 search requests within a few minutes), so most jobs come back without a
description. Kept for reference; the logged-in browser adapter in linkedin_browser.py
is the one wired to `source: linkedin` in searches.yaml.

Uses the unofficial endpoints that back LinkedIn's public job search page:

  search:  https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?...&start=N
  detail:  https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/<id>

Both return HTML fragments. They need no login, but they are not a supported API and
can change or start rate limiting at any time. The adapter stays polite: random
sleeps between requests, a browser User-Agent, a page cap, and a hard stop on HTTP 429.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, urlparse

from jobs.html_text import html_to_text, squash
from jobs.http import PoliteClient, RateLimited
from jobs.models import Job, Workplace, guess_workplace

log = logging.getLogger(__name__)

SOURCE = "linkedin"
SEARCH_ENDPOINT = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
POSTING_ENDPOINT = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
PAGE_SIZE = 25

# Query params from a saved LinkedIn search URL that the guest endpoint understands.
FORWARDED_PARAMS = ("keywords", "location", "geoId", "f_WT", "f_E", "f_TPR", "f_JT", "f_SB2", "sortBy")

# LinkedIn's f_WT (work type) filter values.
F_WT_TO_WORKPLACE: dict[str, Workplace] = {"1": "onsite", "2": "remote", "3": "hybrid"}

_JOB_ID_RE = re.compile(r"/jobs/view/(?:[^\"'?]*?-)?(\d+)")
_CARD_RE = re.compile(r'<div class="base-card[^"]*"[^>]*data-entity-urn="urn:li:jobPosting:(\d+)"(.*?)</li>', re.S)
_TITLE_RE = re.compile(r'<h3 class="base-search-card__title">(.*?)</h3>', re.S)
_COMPANY_RE = re.compile(r'<h4 class="base-search-card__subtitle">(.*?)</h4>', re.S)
_LOCATION_RE = re.compile(r'<span class="job-search-card__location">(.*?)</span>', re.S)
_DATE_RE = re.compile(r'<time[^>]*datetime="(\d{4}-\d{2}-\d{2})"')
_SALARY_RE = re.compile(r'<span class="job-search-card__salary-info">(.*?)</span>', re.S)

_P_TITLE_RE = re.compile(r'<h2 class="top-card-layout__title[^"]*"[^>]*>(.*?)</h2>', re.S)
_P_ORG_RE = re.compile(r'<a class="topcard__org-name-link[^"]*"[^>]*>(.*?)</a>', re.S)
_P_LOCATION_RE = re.compile(r'<span class="topcard__flavor topcard__flavor--bullet">(.*?)</span>', re.S)
_P_DESC_RE = re.compile(r'<div class="description__text description__text--rich">(.*?)</div>\s*</section>', re.S)
_P_DESC_FALLBACK_RE = re.compile(r'<div class="show-more-less-html__markup[^"]*"[^>]*>(.*?)</div>', re.S)
_P_CRITERIA_RE = re.compile(
    r'<h3 class="description__job-criteria-subheader">(.*?)</h3>\s*'
    r'<span class="description__job-criteria-text[^"]*">(.*?)</span>',
    re.S,
)
_P_SALARY_RE = re.compile(r'<div class="salary[^"]*"[^>]*>(.*?)</div>', re.S)


def search_params(search_url: str) -> dict[str, str]:
    """Extract the forwardable query params from a saved LinkedIn jobs search URL."""
    qs = parse_qs(urlparse(search_url).query)
    params = {k: v[0] for k, v in qs.items() if k in FORWARDED_PARAMS and v and v[0]}
    if "keywords" not in params and "geoId" not in params and "location" not in params:
        raise ValueError(f"search URL has no keywords/geoId/location params: {search_url}")
    return params


def extract_job_ids(search_html: str) -> list[str]:
    """Job IDs in page order, deduplicated."""
    seen: set[str] = set()
    ids: list[str] = []
    for job_id in _JOB_ID_RE.findall(search_html):
        if job_id not in seen:
            seen.add(job_id)
            ids.append(job_id)
    return ids


def parse_search_page(search_html: str) -> list[dict]:
    """Lightweight card data from a search result page, keyed by job id."""
    cards: list[dict] = []
    for job_id, body in _CARD_RE.findall(search_html):
        title = _first(_TITLE_RE, body)
        company = _first(_COMPANY_RE, body)
        cards.append(
            {
                "id": job_id,
                "title": title,
                "company": company,
                "location": _first(_LOCATION_RE, body),
                "posted_date": _first(_DATE_RE, body, raw=True),
                "salary": _first(_SALARY_RE, body) or None,
            }
        )
    return cards


def parse_posting(posting_html: str, job_id: str) -> dict:
    criteria = {squash(k).lower(): squash(v) for k, v in _P_CRITERIA_RE.findall(posting_html)}
    desc_html = _first(_P_DESC_RE, posting_html, raw=True) or _first(_P_DESC_FALLBACK_RE, posting_html, raw=True)
    return {
        "id": job_id,
        "title": _first(_P_TITLE_RE, posting_html),
        "company": _first(_P_ORG_RE, posting_html),
        "location": _first(_P_LOCATION_RE, posting_html),
        "description": html_to_text(desc_html),
        "seniority": criteria.get("seniority level", ""),
        "employment_type": criteria.get("employment type", ""),
        "job_function": criteria.get("job function", ""),
        "industries": criteria.get("industries", ""),
        "salary": _first(_P_SALARY_RE, posting_html) or None,
    }


def build_job(card: dict, posting: dict | None, workplace_hint: Workplace | None) -> Job:
    posting = posting or {}
    title = posting.get("title") or card.get("title") or ""
    company = posting.get("company") or card.get("company") or ""
    location = posting.get("location") or card.get("location") or ""
    description = posting.get("description") or ""
    workplace = workplace_hint or guess_workplace(title, location, description)
    return Job(
        source=SOURCE,
        source_id=card["id"],
        title=title,
        company=company,
        location=location,
        workplace=workplace,
        seniority=posting.get("seniority", ""),
        posted_date=card.get("posted_date") or "",
        url=f"https://www.linkedin.com/jobs/view/{card['id']}",
        description=description,
        salary=posting.get("salary") or card.get("salary"),
        extra={
            k: v
            for k, v in {
                "employment_type": posting.get("employment_type", ""),
                "job_function": posting.get("job_function", ""),
                "industries": posting.get("industries", ""),
            }.items()
            if v
        },
    )


def fetch(
    search_url: str,
    max_pages: int = 2,
    client: PoliteClient | None = None,
    fetch_details: bool = True,
) -> list[Job]:
    """Run one saved search through the guest endpoints and return normalized jobs.

    Stops early on an empty page, on max_pages, or on HTTP 429 (returning what was
    collected so far, with a warning).
    """
    client = client or PoliteClient()
    params = search_params(search_url)
    f_wt = params.get("f_WT", "")
    workplace_hint = F_WT_TO_WORKPLACE.get(f_wt) if f_wt and "," not in f_wt else None

    cards: dict[str, dict] = {}
    for page in range(max_pages):
        page_params = {**params, "start": str(page * PAGE_SIZE)}
        log.info("linkedin: search page %d (start=%s) %s", page + 1, page_params["start"], params.get("keywords", ""))
        try:
            resp = client.get(SEARCH_ENDPOINT, params=page_params)
        except RateLimited:
            log.warning("linkedin: rate limited during search, stopping with %d cards", len(cards))
            break
        page_cards = parse_search_page(resp.text)
        if not page_cards:
            # Some responses carry links without card markup; fall back to bare IDs.
            page_cards = [{"id": i} for i in extract_job_ids(resp.text)]
        if not page_cards:
            log.info("linkedin: empty page, stopping")
            break
        new = 0
        for card in page_cards:
            if card["id"] not in cards:
                cards[card["id"]] = card
                new += 1
        if new == 0:
            log.info("linkedin: no new jobs on page, stopping")
            break

    jobs: list[Job] = []
    for job_id, card in cards.items():
        posting = None
        if fetch_details:
            try:
                resp = client.get(POSTING_ENDPOINT.format(job_id=job_id))
                posting = parse_posting(resp.text, job_id)
            except RateLimited:
                log.warning("linkedin: rate limited fetching details, %d of %d done", len(jobs), len(cards))
                fetch_details = False
            except Exception as exc:  # noqa: BLE001 - one bad posting must not kill the run
                log.warning("linkedin: failed to fetch posting %s: %s", job_id, exc)
        jobs.append(build_job(card, posting, workplace_hint))
    log.info("linkedin: %d jobs", len(jobs))
    return jobs


def _first(pattern: re.Pattern, text: str, raw: bool = False) -> str:
    m = pattern.search(text)
    if not m:
        return ""
    return m.group(1) if raw else squash(re.sub(r"<[^>]+>", " ", m.group(1)))
