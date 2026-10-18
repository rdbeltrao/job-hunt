from __future__ import annotations

import logging
import random
import time

import requests

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)


class RateLimited(Exception):
    """Raised when the remote side answers HTTP 429. Callers must stop, not retry."""


class PoliteClient:
    """Small requests wrapper: browser User-Agent, random sleep between calls, 429 handling."""

    def __init__(
        self,
        min_sleep: float = 2.0,
        max_sleep: float = 4.0,
        timeout: float = 30.0,
        session: requests.Session | None = None,
    ) -> None:
        self.min_sleep = min_sleep
        self.max_sleep = max_sleep
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
            }
        )
        self._calls = 0

    def get(self, url: str, params: dict | None = None) -> requests.Response:
        if self._calls > 0 and self.max_sleep > 0:
            delay = random.uniform(self.min_sleep, self.max_sleep)
            log.debug("sleeping %.1fs before %s", delay, url)
            time.sleep(delay)
        self._calls += 1
        resp = self.session.get(url, params=params, timeout=self.timeout)
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            # Back off once so the next adapter is not immediately blocked too, then give up.
            backoff = min(float(retry_after), 60.0) if retry_after and retry_after.isdigit() else 30.0
            log.warning("HTTP 429 from %s, backing off %.0fs and stopping", url, backoff)
            time.sleep(backoff)
            raise RateLimited(url)
        resp.raise_for_status()
        return resp
