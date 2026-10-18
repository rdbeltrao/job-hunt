from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def no_keychain(monkeypatch):
    """Tests never read the developer's real Keychain; test_settings.py overrides this where needed."""
    from jobs import settings

    monkeypatch.setattr(settings, "keychain", lambda name: None)


@pytest.fixture
def fixture_text():
    def _load(name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    return _load


class FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "", headers: dict | None = None) -> None:
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Stands in for requests.Session: answers from a {url_substring: FakeResponse} routing table."""

    def __init__(self, routes: dict[str, FakeResponse]) -> None:
        self.routes = routes
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, dict | None]] = []

    def get(self, url: str, params=None, timeout=None):
        self.calls.append((url, params))
        full = url + ("?" + "&".join(f"{k}={v}" for k, v in (params or {}).items()) if params else "")
        for key, resp in self.routes.items():
            if key in full:
                return resp
        raise AssertionError(f"unexpected request in test: {full}")
