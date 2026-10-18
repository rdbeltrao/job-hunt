"""Playwright helpers: a persistent Chromium profile so a LinkedIn login survives between runs."""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from playwright.sync_api import BrowserContext, Page, sync_playwright

log = logging.getLogger(__name__)

DEFAULT_PROFILE_DIR = Path.home() / ".job-hunt" / "browser-profile"

LINKEDIN_LOGIN_URL = "https://www.linkedin.com/login"
LINKEDIN_FEED_URL = "https://www.linkedin.com/feed/"

# URL fragments that mean LinkedIn wants the human back: login page, 2FA / captcha
# checkpoint, or the logged-out "authwall" interstitial.
LOGIN_WALL_MARKERS = ("/login", "/uas/login", "/checkpoint/", "/authwall", "/signup", "/sign-in")


# Present on every logged-in LinkedIn page (the top navigation with the "Me" menu).
LOGGED_IN_SELECTOR = "#global-nav, .global-nav__me, #global-nav-typeahead"
# Present on the public, logged-out variants of the jobs pages: the "Sign in" button and
# the guest result cards. LinkedIn serves those instead of redirecting to /login.
LOGGED_OUT_SELECTOR = "a.nav__button-secondary[href*='login'], .base-search-card, .authwall-join-form, form.join-form"


class LoginRequired(Exception):
    """The browser profile is not (or no longer) signed in to LinkedIn."""


def is_login_wall(url: str) -> bool:
    return any(marker in url for marker in LOGIN_WALL_MARKERS)


def assert_logged_in(page: Page) -> None:
    """Raise LoginRequired when the current page is a login/checkpoint URL or a logged-out page."""
    if is_login_wall(page.url):
        raise LoginRequired(page.url)
    try:
        if page.locator(LOGGED_IN_SELECTOR).first.count():
            return
        if page.locator(LOGGED_OUT_SELECTOR).first.count():
            raise LoginRequired(page.url)
    except LoginRequired:
        raise
    except Exception:  # noqa: BLE001 - page navigating mid-check; next check decides
        return


@contextmanager
def persistent_context(
    headless: bool = True,
    profile_dir: Path = DEFAULT_PROFILE_DIR,
) -> Iterator[BrowserContext]:
    """Open the shared Chromium profile. Cookies and local storage persist across runs."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(profile_dir),
            headless=headless,
            viewport={"width": 1400, "height": 1000},
            locale="en-US",
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            yield context
        finally:
            context.close()


def first_page(context: BrowserContext) -> Page:
    return context.pages[0] if context.pages else context.new_page()


def wait_for_login(page: Page, timeout_s: float = 600.0, poll_s: float = 2.0) -> bool:
    """Open the login page and wait until the user has signed in. Never types credentials."""
    page.goto(LINKEDIN_LOGIN_URL, wait_until="domcontentloaded")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if is_logged_in(page):
            return True
        time.sleep(poll_s)
    return False


def is_logged_in(page: Page) -> bool:
    url = page.url
    if url.startswith(LINKEDIN_FEED_URL) or "/feed" in url:
        return True
    if is_login_wall(url):
        return False
    try:
        return page.locator(LOGGED_IN_SELECTOR).first.is_visible(timeout=500)
    except Exception:  # noqa: BLE001 - locator timeouts and detached frames both mean "not visible"
        return False


def check_session(page: Page) -> None:
    """Open the feed once and raise LoginRequired if LinkedIn bounces to a login or checkpoint page."""
    page.goto(LINKEDIN_FEED_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    assert_logged_in(page)
    if not is_logged_in(page):
        raise LoginRequired(page.url)
