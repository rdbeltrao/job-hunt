"""Read the latest posts on a LinkedIn member's activity page, as the logged-in user.

Same approach and pacing as the jobs adapter: one page load per creator, a few seconds
between creators, parsing split into a pure function over the HTML (parse_posts) so it
can be tested against saved fixtures. `--dump-html` on `jobs engage` saves the raw page
to out/debug/ when a selector stops matching after a LinkedIn redesign.
"""

from __future__ import annotations

import logging
import random
import re
import time
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup, Tag
from playwright.sync_api import BrowserContext

from jobs.browser import assert_logged_in
from jobs.engage import Post, posted_date_from_age
from jobs.html_text import squash

log = logging.getLogger(__name__)

CREATOR_WAIT_S = (4.0, 9.0)

_URN_RE = re.compile(r"urn:li:(?:activity|share|ugcPost):(\d+)")
_AGE_RE = re.compile(r"\b(\d+)\s?(h|d|w|mo|m|y)\b")
_AGE_EXACT_RE = re.compile(r"^(\d+)\s?(h|d|w|mo|m|y)$")
# The 2025 profile activity page (obfuscated class names) carries the post id only inside
# component ids such as "...UserGeneratedContentPostUrn(userGeneratedContentId=7513367456111906816)...".
_UGC_ID_RE = re.compile(r"userGeneratedContentId=(\d{10,})")
_SHARE_ID_RE = re.compile(r"shareId=(\d{10,})")
_ACTIVITY_ID_RE = re.compile(r"(?:activity[-:=]|activityUrn[^0-9]{0,40})(\d{10,})")

# 2025 markup: a list of update cards, each with its commentary in expandable-text-box spans.
_LIST_SELECTOR = "div[role='list'][data-testid^='ProfileRecentActivity']"
_ITEM_SELECTOR = "div[role='listitem']"
_TEXT_BOX_SELECTOR = "[data-testid='expandable-text-box']"

# Post containers, newest markup first; each has fallbacks because LinkedIn renames classes often.
_POST_SELECTORS = (
    "div.feed-shared-update-v2[data-urn]",
    "div[data-urn^='urn:li:activity']",
    "div[data-id^='urn:li:activity']",
    "div.feed-shared-update-v2",
    "article",
)
_TEXT_SELECTORS = (
    ".update-components-text",
    ".feed-shared-update-v2__description",
    ".feed-shared-inline-show-more-text",
    "[data-test-id='main-feed-activity-card__commentary']",
    ".attributed-text-segment-list__content",
)
_AUTHOR_SELECTORS = (
    ".update-components-actor__name span[aria-hidden='true']",
    ".update-components-actor__name",
    ".update-components-actor__title span[aria-hidden='true']",
    ".update-components-actor__title",
    "[data-test-id='main-feed-activity-card__entity-lockup'] a",
)
_AGE_SELECTORS = (
    ".update-components-actor__sub-description span[aria-hidden='true']",
    ".update-components-actor__sub-description",
    ".update-components-actor__meta time",
    "time",
)
_REACTIONS_SELECTORS = (
    ".social-details-social-counts__reactions-count",
    "[data-test-id='social-actions__reactions'] span",
)
_COMMENTS_SELECTORS = (
    ".social-details-social-counts__comments button span",
    ".social-details-social-counts__comments",
    "[data-test-id='social-actions__comments']",
)


def activity_url(profile_url: str) -> str:
    """https://www.linkedin.com/in/<slug>/ -> the member's recent posts page."""
    base = profile_url.split("?", 1)[0].rstrip("/")
    if "/recent-activity" in base:
        return base + "/"
    return base + "/recent-activity/all/"


def parse_posts(html: str, now: datetime | None = None) -> list[Post]:
    soup = BeautifulSoup(html, "html.parser")
    posts = _parse_update_cards(soup, now)
    if posts:
        return posts
    return _parse_classic(soup, now)


def _parse_update_cards(soup: BeautifulSoup, now: datetime | None) -> list[Post]:
    """The profile activity page as LinkedIn renders it since 2025: role=list of role=listitem cards."""
    container = soup.select_one(_LIST_SELECTOR) or soup
    posts: list[Post] = []
    seen: set[str] = set()
    for item in container.select(_ITEM_SELECTOR):
        boxes = item.select(_TEXT_BOX_SELECTOR)
        if not boxes:
            continue
        text = "\n".join(t for t in (squash(b.get_text(" ", strip=True)) for b in boxes) if t)
        url = _card_url(item)
        if not text or not url or url in seen:
            continue
        seen.add(url)
        spans = [squash(sp.get_text(" ", strip=True)) for sp in item.find_all("span")]
        age = next((t for t in spans if _AGE_EXACT_RE.match(t)), "")
        numbers = [squash(b.get_text(" ", strip=True)) for b in item.find_all("button")]
        numbers = [n for n in numbers if n.replace(",", "").replace(".", "").isdigit()]
        posts.append(
            Post(
                url=url,
                author=_card_author(item),
                text=text,
                posted_date=posted_date_from_age(age, now),
                age_text=age,
                reactions=numbers[0] if numbers else "",
                comments=numbers[1] if len(numbers) > 1 else "",
            )
        )
    return posts


def _card_url(item: Tag) -> str:
    raw = str(item)
    m = _URN_RE.search(raw)
    if m:
        return f"https://www.linkedin.com/feed/update/urn:li:activity:{m.group(1)}/"
    for a in item.select("a[href*='activity-']"):
        m = re.search(r"activity-(\d{10,})", str(a.get("href") or ""))
        if m:
            return f"https://www.linkedin.com/feed/update/urn:li:activity:{m.group(1)}/"
    m = _UGC_ID_RE.search(raw)
    if m:
        return f"https://www.linkedin.com/feed/update/urn:li:ugcPost:{m.group(1)}/"
    m = _SHARE_ID_RE.search(raw)
    if m:
        return f"https://www.linkedin.com/feed/update/urn:li:share:{m.group(1)}/"
    return ""


def _card_author(item: Tag) -> str:
    for a in item.select("a[href*='/in/'], a[href*='/company/']"):
        text = squash(a.get_text(" ", strip=True))
        if text and text.lower() not in ("follow", "following"):
            # The lockup repeats the name for screen readers; the first words are the name.
            return text.split(" • ")[0].strip()
    return ""


def _parse_classic(soup: BeautifulSoup, now: datetime | None) -> list[Post]:
    """The older feed markup (feed-shared-update-v2 with data-urn), kept as a fallback."""
    posts: list[Post] = []
    seen: set[str] = set()
    for selector in _POST_SELECTORS:
        for node in soup.select(selector):
            urn_id = _urn_id(node)
            text = _text_first(node, *_TEXT_SELECTORS)
            if not urn_id or not text or urn_id in seen:
                continue
            seen.add(urn_id)
            age = _age_text(node)
            posts.append(
                Post(
                    url=f"https://www.linkedin.com/feed/update/urn:li:activity:{urn_id}/",
                    author=_text_first(node, *_AUTHOR_SELECTORS),
                    text=text,
                    posted_date=posted_date_from_age(age, now),
                    age_text=age,
                    reactions=_text_first(node, *_REACTIONS_SELECTORS),
                    comments=_text_first(node, *_COMMENTS_SELECTORS),
                )
            )
        if posts:
            break
    return posts


def fetch_recent_posts(
    context: BrowserContext,
    profile_url: str,
    max_posts: int = 3,
    dump_dir: Path | None = None,
) -> list[Post]:
    page = context.pages[0] if context.pages else context.new_page()
    url = activity_url(profile_url)
    log.info("activity: %s", url)
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(random.randint(2500, 4500))
    assert_logged_in(page)
    try:
        page.wait_for_selector(", ".join(_POST_SELECTORS), timeout=15000)
    except Exception:  # noqa: BLE001 - parse whatever rendered; an empty result is reported upstream
        log.warning("activity: no post container appeared on %s", url)
    # One gentle scroll so lazy-rendered posts below the fold exist in the DOM.
    page.mouse.wheel(0, 1200)
    page.wait_for_timeout(random.randint(1200, 2200))
    html = page.content()
    if dump_dir:
        slug = re.sub(r"[^\w.-]+", "_", profile_url.rstrip("/").rsplit("/", 1)[-1]) or "activity"
        dump_dir.mkdir(parents=True, exist_ok=True)
        (dump_dir / f"activity-{slug}.html").write_text(html, encoding="utf-8")
    posts = parse_posts(html)[:max_posts]
    time.sleep(random.uniform(*CREATOR_WAIT_S))
    return posts


def _urn_id(node: Tag) -> str:
    for attr in ("data-urn", "data-id"):
        value = node.get(attr) or ""
        m = _URN_RE.search(str(value))
        if m:
            return m.group(1)
    for a in node.select("a[href*='urn:li:activity'], a[href*='activity-']"):
        href = str(a.get("href") or "")
        m = _URN_RE.search(href) or re.search(r"activity-(\d+)", href)
        if m:
            return m.group(1)
    return ""


def _age_text(node: Tag) -> str:
    raw = _text_first(node, *_AGE_SELECTORS)
    m = _AGE_RE.search(raw)
    return m.group(0).replace(" ", "") if m else ""


def _text_first(root: Tag, *selectors: str) -> str:
    for selector in selectors:
        found = root.select_one(selector)
        if found is not None:
            text = squash(found.get_text(" ", strip=True))
            if text:
                return text
    return ""
