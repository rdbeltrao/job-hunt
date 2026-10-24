"""LinkedIn adapter that drives a logged-in browser session with Playwright.

Replaces the guest-endpoint adapter, which hits HTTP 429 after a handful of detail
requests. Volume is small (a few searches, 1 to 3 result pages each), so navigating
the normal job search page as the signed-in user, at human pace, is the chosen trade-off.

Parsing is split out into pure functions over HTML strings (parse_cards, parse_detail)
so it can be unit tested against saved fixtures without a browser. LinkedIn changes
its DOM regularly; every selector below has fallbacks, and `--dump-html` writes the raw
list and detail HTML to out/debug/ so a broken selector can be fixed from real markup.
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup, Tag
from playwright.sync_api import BrowserContext, Page

from jobs.browser import LoginRequired, assert_logged_in
from jobs.html_text import html_to_text, squash
from jobs.models import Job, Workplace, guess_workplace

log = logging.getLogger(__name__)

SOURCE = "linkedin"
PAGE_SIZE = 25

CARD_WAIT_S = (1.5, 4.0)
PAGE_WAIT_S = (5.0, 10.0)

_JOB_ID_RE = re.compile(r"/jobs/view/(\d+)")
_SALARY_RE = re.compile(r"(€|\$|£|EUR|USD|GBP)\s?\d|\d\s?(€|EUR|USD|GBP|k/yr|/yr|/hr|/month|/mo|per year|per month)", re.I)
_WORKPLACE_WORDS: dict[str, Workplace] = {"remote": "remote", "hybrid": "hybrid", "on-site": "onsite", "onsite": "onsite", "on site": "onsite"}
_SENIORITY_WORDS = ("internship", "entry level", "associate", "mid-senior", "director", "executive", "level")
# "23 hours ago", "Reposted 2 days ago", "1 week ago", "just now", "yesterday".
_RELATIVE_RE = re.compile(r"\b(\d+|an?|one)\s+(minute|min|hour|hr|day|week|month|year)s?\s+ago\b", re.I)
_RELATIVE_UNIT_DAYS = {"minute": 0, "min": 0, "hour": 0, "hr": 0, "day": 1, "week": 7, "month": 30, "year": 365}
_ABSOLUTE_DATE_FORMATS = ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y", "%d.%m.%Y")
# Premium "hiring insights" describe the applicants, not the job; never read a seniority from them.
_APPLICANT_INSIGHT_RE = re.compile(r"applicant", re.I)
_EMPLOYMENT_WORDS = ("full-time", "part-time", "contract", "internship", "temporary", "volunteer", "other")
_COMPANY_URL_RE = re.compile(r"^(https?://[^/]+)?/company/([^/?#]+)", re.I)
_PROFILE_URL_RE = re.compile(r"^(https?://[^/]+)?/in/([^/?#]+)", re.I)
_LINKEDIN_ORIGIN = "https://www.linkedin.com"
# A people block counts as the hiring team only under one of these headings; the same
# section class also wraps "People you can reach out to" (your own connections there).
_HIRING_TEAM_RE = re.compile(r"hiring team|job poster|posted by", re.I)
_HIRING_TEAM_LABEL_RE = re.compile(r"^\s*(Meet the hiring team|Job poster)\s*$", re.I)
_CONNECTION_DEGREE_RE = re.compile(r"^[\s·•]*(1st|2nd\+?|3rd\+?)\s*$", re.I)
_CONTACT_NOISE = {"job poster", "message", "connect", "follow", "view profile"}

# Selectors for the results list cards; all are tried, matches are merged by job id.
CARD_SELECTORS = (
    "li[data-occludable-job-id]",
    "div.job-card-container[data-job-id]",
    "li.jobs-search-results__list-item",
    "li.scaffold-layout__list-item",
)
DETAIL_PANE_SELECTORS = (
    ".scaffold-layout__detail",
    ".jobs-search__job-details--wrapper",
    ".jobs-search__job-details--container",
    ".jobs-details",
)
# Containers of the top card, where the company name links to the company page.
COMPANY_LINK_SCOPES = (
    ".job-details-jobs-unified-top-card__company-name",
    ".jobs-unified-top-card__company-name",
    ".job-details-jobs-unified-top-card__container--two-pane",
    ".job-details-jobs-unified-top-card__primary-description-container",
    ".jobs-unified-top-card",
)
# The "Meet the hiring team" / "Job poster" block: one person with a profile link and a
# Message (InMail) button. Newer markup first, the 2023 jobs-poster markup after it.
HIRER_SELECTORS = (
    ".job-details-people-who-can-help__section .hirer-card__hirer-information",
    ".hirer-card__hirer-information",
    ".job-details-people-who-can-help__section",
    ".jobs-poster__wrapper",
    ".jobs-poster",
)


@dataclass
class Card:
    id: str
    title: str = ""
    company: str = ""
    location: str = ""
    posted_date: str = ""
    salary: str | None = None


@dataclass
class Detail:
    title: str = ""
    company: str = ""
    location: str = ""
    description: str = ""
    posted_date: str = ""  # ISO date derived from "x days ago" / "Reposted x hours ago"
    workplace: Workplace | None = None
    seniority: str = ""
    employment_type: str = ""
    salary: str | None = None
    company_url: str = ""  # https://www.linkedin.com/company/<slug>/ when the top card links it
    contact_name: str = ""  # the hiring-team person who can be messaged, when the pane shows one
    contact_title: str = ""
    contact_url: str = ""  # https://www.linkedin.com/in/<slug>/
    pills: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- pure parsing


def parse_cards(list_html: str) -> list[Card]:
    soup = BeautifulSoup(list_html, "html.parser")
    # Union of all card selectors in document order; nested matches (li and its inner
    # div) and re-rendered duplicates collapse on the job id below.
    nodes: list[Tag] = soup.select(", ".join(CARD_SELECTORS))
    cards: list[Card] = []
    seen: set[str] = set()
    for node in nodes:
        job_id = _card_id(node)
        if not job_id or job_id in seen:
            continue
        seen.add(job_id)
        cards.append(
            Card(
                id=job_id,
                title=_card_title(node),
                company=_text_first(node, ".artdeco-entity-lockup__subtitle", ".job-card-container__primary-description", ".job-card-container__company-name"),
                location=_text_first(node, ".job-card-container__metadata-wrapper li", ".job-card-container__metadata-item", ".artdeco-entity-lockup__caption"),
                posted_date=_datetime_first(node) or posted_date_from_text(_text_first(node, ".job-card-container__footer-item--highlighted", ".job-card-container__footer-item", ".job-card-list__footer-wrapper")),
                salary=_text_first(node, ".job-card-container__metadata-item--salary", ".job-card-list__salary") or _card_salary(node),
            )
        )
    return cards


def parse_detail(detail_html: str, now: datetime | None = None) -> Detail:
    soup = BeautifulSoup(detail_html, "html.parser")
    title = _text_first(soup, ".job-details-jobs-unified-top-card__job-title", "h1.t-24", "h1", "h2.jobs-details-top-card__job-title")
    company = _text_first(soup, ".job-details-jobs-unified-top-card__company-name", ".jobs-unified-top-card__company-name", ".jobs-details-top-card__company-url") or _company_name_from_link(soup)
    meta = _text_first(
        soup,
        ".job-details-jobs-unified-top-card__tertiary-description-container",
        ".job-details-jobs-unified-top-card__primary-description-container",
        ".jobs-unified-top-card__primary-description",
        ".jobs-unified-top-card__subtitle-primary-grouping",
    )
    location = _location_from_meta(meta)
    posted_date = _datetime_first(soup) or posted_date_from_text(meta, now)

    desc_node = _first_node(soup, "#job-details", ".jobs-description__content", ".jobs-description-content__text", ".jobs-box__html-content", "article")
    description = html_to_text(desc_node.decode_contents()) if desc_node is not None else ""
    description = re.sub(r"^About the job\s*", "", description).strip()

    pills = _pill_texts(soup)
    contact_name, contact_title, contact_url = _hiring_contact(soup)
    detail = Detail(
        title=title,
        company=company,
        location=location,
        description=description,
        posted_date=posted_date,
        company_url=_company_url(soup),
        contact_name=contact_name,
        contact_title=contact_title,
        contact_url=contact_url,
        pills=pills,
    )
    for pill in pills:
        low = pill.lower()
        if detail.workplace is None:
            for word, value in _WORKPLACE_WORDS.items():
                if word in low:
                    detail.workplace = value
                    break
        if not detail.employment_type and any(low == w or low.startswith(w) for w in _EMPLOYMENT_WORDS):
            detail.employment_type = pill
        elif not detail.seniority and any(w in low for w in _SENIORITY_WORDS):
            detail.seniority = pill
        elif detail.salary is None and _SALARY_RE.search(pill):
            detail.salary = pill
    if not detail.seniority:
        detail.seniority = _seniority_from_criteria(soup)
    return detail


def posted_date_from_text(text: str, now: datetime | None = None) -> str:
    """ISO date for LinkedIn's posting age text, or "" when the text carries none.

    Handles the relative forms ("15 minutes ago", "Reposted 23 hours ago", "2 days ago",
    "3 weeks ago", "1 month ago", "just now", "yesterday") and a few absolute spellings
    ("Oct 14, 2026", "14 Oct 2026", "2026-10-14"). Weeks and months are approximations,
    like LinkedIn's own rounding.
    """
    if not text:
        return ""
    now = now or datetime.now()
    m = _RELATIVE_RE.search(text)
    if m:
        amount = m.group(1).lower()
        n = 1 if amount in ("a", "an", "one") else int(amount)
        unit = m.group(2).lower()
        if unit in ("minute", "min", "hour", "hr"):
            delta = timedelta(minutes=n) if unit in ("minute", "min") else timedelta(hours=n)
        else:
            delta = timedelta(days=n * _RELATIVE_UNIT_DAYS[unit])
        return (now - delta).date().isoformat()
    low = text.lower()
    if re.search(r"\bjust now\b|\btoday\b", low):
        return now.date().isoformat()
    if re.search(r"\byesterday\b", low):
        return (now - timedelta(days=1)).date().isoformat()
    for part in re.split(r"\s[·•]\s|\n", text):
        part = squash(re.sub(r"^(Re)?posted(\s+on)?\s*", "", part, flags=re.I))
        for fmt in _ABSOLUTE_DATE_FORMATS:
            try:
                return datetime.strptime(part, fmt).date().isoformat()
            except ValueError:
                continue
    return ""


def with_start(search_url: str, start: int) -> str:
    """Return the search URL with `start` set (0 removes it), other params untouched."""
    parts = urlparse(search_url)
    qs = {k: v[0] for k, v in parse_qs(parts.query).items()}
    qs.pop("currentJobId", None)
    if start > 0:
        qs["start"] = str(start)
    else:
        qs.pop("start", None)
    return urlunparse(parts._replace(query=urlencode(qs)))


def build_job(card: Card, detail: Detail | None, search_name: str) -> Job:
    d = detail or Detail()
    title = d.title or card.title
    company = d.company or card.company
    location = d.location or card.location
    workplace = d.workplace or guess_workplace(title, location, d.description, " ".join(d.pills))
    extra = {"search": search_name}
    if d.employment_type:
        extra["employment_type"] = d.employment_type
    if d.pills:
        extra["pills"] = d.pills
    return Job(
        source=SOURCE,
        source_id=card.id,
        title=title,
        company=company,
        location=location,
        workplace=workplace,
        seniority=d.seniority,
        posted_date=card.posted_date or d.posted_date,
        url=f"https://www.linkedin.com/jobs/view/{card.id}/",
        description=d.description,
        salary=d.salary or card.salary,
        company_url=d.company_url,
        contact_name=d.contact_name,
        contact_title=d.contact_title,
        contact_url=d.contact_url,
        extra=extra,
    )


# ---------------------------------------------------------------- browser driving


def fetch(
    context: BrowserContext,
    search_url: str,
    search_name: str,
    max_pages: int = 3,
    skip_ids: set[str] | None = None,
    fetch_details: bool = True,
    dump_dir: Path | None = None,
) -> list[Job]:
    """Walk one saved search in the logged-in browser and return normalized jobs.

    Cards whose id is in skip_ids are not opened (and not returned); they were seen in
    an earlier run. Raises LoginRequired if LinkedIn redirects to a login or checkpoint page.
    """
    skip_ids = skip_ids or set()
    page = context.pages[0] if context.pages else context.new_page()
    jobs: list[Job] = []
    seen_this_run: set[str] = set()

    for page_no in range(max_pages):
        url = with_start(search_url, page_no * PAGE_SIZE)
        if page_no > 0:
            _sleep(*PAGE_WAIT_S)
        log.info("linkedin: %s page %d %s", search_name, page_no + 1, url)
        try:
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(1000)
            _assert_logged_in(page)
            _wait_for_cards(page)
            _scroll_results(page)
            list_html = _results_html(page)
        except LoginRequired:
            raise
        except Exception as exc:  # noqa: BLE001
            if page_no == 0 or not jobs:
                raise
            # A later page that fails to load must not discard the jobs already collected.
            log.warning("linkedin: %s page %d failed, keeping %d jobs: %s", search_name, page_no + 1, len(jobs), exc)
            break
        if dump_dir:
            _dump(dump_dir, f"{search_name}-page{page_no + 1}-list.html", list_html)
        cards = parse_cards(list_html)
        new_cards = [c for c in cards if c.id not in seen_this_run and c.id not in skip_ids]
        seen_this_run.update(c.id for c in cards)
        log.info("linkedin: %d cards, %d new", len(cards), len(new_cards))
        if not cards:
            break

        for card in new_cards:
            detail = None
            if fetch_details:
                _sleep(*CARD_WAIT_S)
                try:
                    detail_html = _open_detail(page, card.id)
                    if dump_dir:
                        _dump(dump_dir, f"{search_name}-{card.id}-detail.html", detail_html)
                    detail = parse_detail(detail_html)
                except LoginRequired:
                    raise
                except Exception as exc:  # noqa: BLE001 - keep the card, log the failure
                    log.warning("linkedin: detail for %s failed: %s", card.id, exc)
            jobs.append(build_job(card, detail, search_name))

        if len(cards) < PAGE_SIZE:
            break
    return jobs


def _assert_logged_in(page: Page) -> None:
    assert_logged_in(page)


def _wait_for_cards(page: Page, timeout_ms: int = 15000) -> None:
    selector = ", ".join(CARD_SELECTORS)
    try:
        page.wait_for_selector(selector, timeout=timeout_ms)
    except Exception:  # noqa: BLE001
        _assert_logged_in(page)
        log.warning("linkedin: no job cards appeared within %ds at %s", timeout_ms // 1000, page.url)


def _scroll_results(page: Page, max_rounds: int = 12) -> None:
    """Scroll the results list container so LinkedIn renders every (lazy) card."""
    selector = ", ".join(CARD_SELECTORS)
    last = -1
    for _ in range(max_rounds):
        count = page.evaluate(
            """(sel) => {
                const cards = document.querySelectorAll(sel);
                if (!cards.length) return 0;
                let el = cards[cards.length - 1];
                let scroller = el;
                while (scroller && scroller !== document.body) {
                    const s = getComputedStyle(scroller);
                    if (scroller.scrollHeight > scroller.clientHeight + 10 && /(auto|scroll)/.test(s.overflowY)) break;
                    scroller = scroller.parentElement;
                }
                if (scroller && scroller !== document.body) scroller.scrollTop += 600; else window.scrollBy(0, 600);
                el.scrollIntoView({block: 'end'});
                return cards.length;
            }""",
            selector,
        )
        page.wait_for_timeout(random.randint(500, 1100))
        if count == last:
            break
        last = count


def _results_html(page: Page) -> str:
    for sel in (".scaffold-layout__list", ".jobs-search-results-list", "ul.jobs-search__results-list", "main"):
        loc = page.locator(sel).first
        try:
            if loc.count():
                return loc.inner_html(timeout=3000)
        except Exception:  # noqa: BLE001
            continue
    return page.content()


def _open_detail(page: Page, job_id: str) -> str:
    card = page.locator(f'li[data-occludable-job-id="{job_id}"], div.job-card-container[data-job-id="{job_id}"], a[href*="/jobs/view/{job_id}"]').first
    try:
        card.scroll_into_view_if_needed(timeout=5000)
        card.click(timeout=5000)
    except Exception:  # noqa: BLE001 - occluded (virtualised) card; scroll it to the centre and retry once
        card.evaluate("el => el.scrollIntoView({block: 'center'})")
        page.wait_for_timeout(random.randint(600, 1200))
        card.click(timeout=5000)
    # The detail pane re-renders asynchronously; wait until the URL carries this job id
    # or the description block shows up, whichever comes first.
    try:
        page.wait_for_url(re.compile(rf"currentJobId={job_id}"), timeout=8000)
    except Exception:  # noqa: BLE001
        pass
    _assert_logged_in(page)
    try:
        page.wait_for_selector("#job-details, .jobs-description__content, .jobs-description-content__text", timeout=8000)
    except Exception:  # noqa: BLE001
        log.debug("linkedin: description selector did not appear for %s", job_id)
    page.wait_for_timeout(random.randint(400, 900))
    for sel in DETAIL_PANE_SELECTORS:
        loc = page.locator(sel).first
        try:
            if loc.count():
                return loc.inner_html(timeout=3000)
        except Exception:  # noqa: BLE001
            continue
    return page.content()


def _sleep(lo: float, hi: float) -> None:
    time.sleep(random.uniform(lo, hi))


def _dump(dump_dir: Path, name: str, html: str) -> None:
    dump_dir.mkdir(parents=True, exist_ok=True)
    (dump_dir / re.sub(r"[^\w.-]+", "_", name)).write_text(html, encoding="utf-8")


# ---------------------------------------------------------------- parsing helpers


def _card_id(node: Tag) -> str:
    for attr in ("data-occludable-job-id", "data-job-id"):
        if node.get(attr):
            return str(node[attr]).strip()
    inner = node.select_one("[data-job-id]")
    if inner is not None and inner.get("data-job-id"):
        return str(inner["data-job-id"]).strip()
    link = node.select_one('a[href*="/jobs/view/"]')
    if link is not None:
        m = _JOB_ID_RE.search(str(link.get("href", "")))
        if m:
            return m.group(1)
    return ""


def _card_title(node: Tag) -> str:
    link = node.select_one("a.job-card-list__title--link, a.job-card-container__link, a.job-card-list__title, a[href*='/jobs/view/']")
    if link is None:
        return _text_first(node, ".artdeco-entity-lockup__title")
    strong = link.select_one("strong")
    if strong is not None and squash(strong.get_text()):
        return squash(strong.get_text())
    aria = link.get("aria-label")
    if aria:
        return squash(str(aria))
    # Visually-hidden duplicates of the title live inside the link; take the first text run.
    hidden = link.select_one(".visually-hidden")
    if hidden is not None:
        return squash(hidden.get_text())
    return squash(link.get_text())


def _card_salary(node: Tag) -> str | None:
    """Salary row of the card metadata; LinkedIn renders it in an obfuscated-class <li>."""
    for li in node.select(".job-card-container__metadata-wrapper li, .artdeco-entity-lockup__metadata li"):
        txt = squash(li.get_text(" "))
        if txt and _SALARY_RE.search(txt):
            return txt
    return None


def _text_first(root: Tag | BeautifulSoup, *selectors: str) -> str:
    node = _first_node(root, *selectors)
    return squash(node.get_text(" ")) if node is not None else ""


def _first_node(root: Tag | BeautifulSoup, *selectors: str) -> Tag | None:
    for sel in selectors:
        node = root.select_one(sel)
        if node is not None and squash(node.get_text(" ")):
            return node
    return None


def _datetime_first(node: Tag | BeautifulSoup) -> str:
    t = node.select_one("time[datetime]")
    if t is None:
        return ""
    value = str(t["datetime"])
    return value[:10] if re.match(r"\d{4}-\d{2}-\d{2}", value) else ""


def _location_from_meta(meta: str) -> str:
    """'Berlin, Germany · 2 days ago · 45 applicants' -> 'Berlin, Germany'."""
    if not meta:
        return ""
    first = re.split(r"\s[·•]\s", meta)[0]
    return squash(first)


def _seniority_from_criteria(soup: BeautifulSoup) -> str:
    """Read the "Seniority level" criteria row when the pane renders the job criteria list.

    LinkedIn's public posting page and some logged-in layouts list
    "Seniority level / Employment type / Job function / Industries" as heading + value
    pairs. The premium "Applicant seniority level" insight is skipped on purpose.
    """
    for item in soup.select(".description__job-criteria-item, .jobs-description-details__list-item, .job-criteria__item, li"):
        heading = item.select_one(".description__job-criteria-subheader, .job-criteria__subheader, h3, h4, dt, strong")
        if heading is None:
            continue
        label = squash(heading.get_text(" ")).rstrip(":").strip().lower()
        # Only the literal criteria heading counts; description bullets such as
        # "Seniority & Leadership:" and the premium "Applicant seniority level" do not.
        if label != "seniority level":
            continue
        value = item.select_one(".description__job-criteria-text, .job-criteria__text, dd, span, p")
        text = squash(value.get_text(" ")) if value is not None else ""
        if not text or text.rstrip(":").strip().lower() == label:
            text = squash(item.get_text(" ").replace(squash(heading.get_text(" ")), "", 1)).lstrip(":").strip()
        if text and len(text) < 40 and not _APPLICANT_INSIGHT_RE.search(text):
            return text
    for node in soup.find_all(string=re.compile(r"^\s*Seniority level\s*:?\s*$", re.I)):
        parent = node.parent
        if parent is None or _APPLICANT_INSIGHT_RE.search(parent.get_text(" ")):
            continue
        sibling = parent.find_next_sibling()
        text = squash(sibling.get_text(" ")) if sibling is not None else ""
        if text and len(text) < 40:
            return text
    return ""


def _company_url(soup: BeautifulSoup) -> str:
    """Canonical LinkedIn company page linked from the top card, or "".

    The top card links the company name and logo to `/company/<slug>/life`; the slug is
    kept and the section, query and fragment dropped, so the URL is the same whatever
    tab LinkedIn linked. Links elsewhere in the pane (similar jobs, description) are ignored.
    """
    for scope in COMPANY_LINK_SCOPES:
        for node in soup.select(scope):
            for link in node.select('a[href*="/company/"]'):
                url = canonical_company_url(str(link.get("href", "")))
                if url:
                    return url
    # The 2026 job page carries only obfuscated class names; the top card is still the
    # first thing in the pane, so its company link is the first one in document order.
    for link in soup.select('a[href*="/company/"]'):
        url = canonical_company_url(str(link.get("href", "")))
        if url:
            return url
    return ""


def _company_name_from_link(soup: BeautifulSoup) -> str:
    """Text of the top card's company link on the 2026 markup ("Intact"), skipping follower counts and logos."""
    for link in soup.select('a[href*="/company/"]'):
        if not canonical_company_url(str(link.get("href", ""))):
            continue
        text = squash(link.get_text(" "))
        if text and len(text) < 80 and not re.search(r"\bfollowers?\b|show more", text, re.I):
            return text
    return ""


def canonical_company_url(href: str) -> str:
    """'https://www.linkedin.com/company/momox/life?x=1' -> 'https://www.linkedin.com/company/momox/'; "" when not a company link."""
    m = _COMPANY_URL_RE.match(href.strip())
    if not m:
        return ""
    origin = m.group(1) or _LINKEDIN_ORIGIN
    return f"{origin}/company/{m.group(2)}/"


def canonical_profile_url(href: str) -> str:
    """'https://www.linkedin.com/in/jane-doe?miniProfileUrn=...' -> 'https://www.linkedin.com/in/jane-doe/'; "" when not a profile link."""
    m = _PROFILE_URL_RE.match(href.strip())
    if not m:
        return ""
    origin = m.group(1) or _LINKEDIN_ORIGIN
    return f"{origin}/in/{m.group(2)}/"


def _hiring_contact(soup: BeautifulSoup) -> tuple[str, str, str]:
    """(name, title, profile URL) of the "Meet the hiring team" person, all "" when the pane shows none.

    LinkedIn renders at most one hirer card per posting, with the name inside a profile
    link (`/in/<slug>`), the headline below it and a Message button for InMail. The
    person is only recorded when a profile link is there: a card without one cannot be
    messaged. The title drops the connection degree ("3rd", "2nd+") LinkedIn appends.
    """
    for sel in HIRER_SELECTORS:
        for card in soup.select(sel):
            if "hirer-card" not in sel and "jobs-poster" not in sel and not _HIRING_TEAM_RE.search(card.get_text(" ")):
                continue
            link = card.select_one('a[href*="/in/"]')
            url = canonical_profile_url(str(link.get("href", ""))) if link is not None else ""
            if not url:
                continue
            name = _text_first(link, "strong", ".jobs-poster__name", "span[aria-hidden='true']") or squash(link.get_text(" "))
            name = re.sub(r"\s*(·\s*)?\b(1st|2nd\+?|3rd\+?)\b.*$", "", name).strip()
            title = _text_first(
                card,
                ".hirer-card__hirer-job-title",
                ".hirer-card__job-poster-headline",
                ".jobs-poster__headline",
                ".jobs-poster__job-title",
                ".t-black--light",
                ".t-14.t-normal",
            )
            title = re.sub(r"^\s*(1st|2nd\+?|3rd\+?)\s*[·•]?\s*", "", title).strip()
            if title.lower() == name.lower():
                title = ""
            if name:
                return name, title, url
    return _hiring_contact_by_label(soup)


def _hiring_contact_by_label(soup: BeautifulSoup) -> tuple[str, str, str]:
    """The same card on the 2026 markup, which has no stable class names.

    Anchors on the literal "Meet the hiring team" / "Job poster" label, climbs to the
    nearest block holding a profile link and reads the link's text runs: the first is
    the name, the first later one that is not a connection degree or a button label is
    the headline.
    """
    for label in soup.find_all(string=_HIRING_TEAM_LABEL_RE):
        block = label.parent
        link = None
        for _ in range(8):
            if block is None or not isinstance(block, Tag):
                break
            link = block.select_one('a[href*="/in/"]')
            if link is not None:
                break
            block = block.parent
        if link is None:
            continue
        url = canonical_profile_url(str(link.get("href", "")))
        if not url:
            continue
        runs = [squash(t) for t in link.stripped_strings]
        runs = [t for t in runs if t and not _HIRING_TEAM_LABEL_RE.match(t)]
        if not runs:
            continue
        name = re.sub(r"\s*[·•]\s*(1st|2nd\+?|3rd\+?)\s*$", "", runs[0]).strip()
        title = ""
        for t in runs[1:]:
            if _CONNECTION_DEGREE_RE.match(t) or t.lower() in _CONTACT_NOISE or t.lower() == name.lower():
                continue
            title = t
            break
        if name:
            return name, title, url
    return "", "", ""


def _pill_texts(soup: BeautifulSoup) -> list[str]:
    """Texts of the job-insight pills (workplace type, employment type, seniority, salary)."""
    texts: list[str] = []
    for sel in (
        ".job-details-fit-level-preferences button",
        ".job-details-fit-level-preferences span[aria-hidden='true']",
        ".job-details-jobs-unified-top-card__job-insight span",
        ".job-details-jobs-unified-top-card__job-insight-view-model-secondary",
        ".jobs-unified-top-card__job-insight span",
        ".job-details-preferences-and-skills__pill span",
    ):
        for node in soup.select(sel):
            txt = squash(node.get_text(" "))
            if txt and txt not in texts and len(txt) < 80:
                texts.append(txt)
        if texts:
            break
    # Pills often nest "Hybrid Matches your job preferences"; keep the leading label only.
    cleaned: list[str] = []
    for t in texts:
        t = re.sub(r"\s*Matches your job preferences.*$", "", t, flags=re.I).strip()
        if t and t not in cleaned:
            cleaned.append(t)
    return cleaned
