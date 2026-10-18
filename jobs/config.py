from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

# "linkedin" drives the logged-in browser (jobs/adapters/linkedin_browser.py).
# "linkedin_guest" uses the logged-out guest endpoints; it is kept in the tree but should
# stay disabled: LinkedIn answers HTTP 429 after a handful of detail requests.
SEARCH_SOURCES = ("linkedin", "linkedin_guest")


@dataclass
class Search:
    name: str
    url: str
    source: str = "linkedin"
    max_pages: int = 8
    enabled: bool = True


COMPANY_SOURCES = ("personio", "greenhouse", "ashby")

# Company boards list every opening, so titles are filtered before anything is stored.
DEFAULT_TITLE_KEYWORDS = [
    "engineering manager",
    "head of engineering",
    "tribe lead",
    "engineering lead",
    "head of technology",
    "head of software",
]


@dataclass
class Company:
    name: str
    ats: str
    slug: str
    enabled: bool = True


def load_searches(path: Path) -> list[Search]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    return [Search(**item) for item in data.get("searches", [])]


def load_companies(path: Path) -> list[Company]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    return [Company(**item) for item in data.get("companies", [])]


def load_title_keywords(path: Path) -> list[str]:
    """`title_keywords` from companies.yaml; the defaults when the key is absent, [] disables the filter."""
    if not path.exists():
        return list(DEFAULT_TITLE_KEYWORDS)
    data = yaml.safe_load(path.read_text()) or {}
    keywords = data.get("title_keywords")
    if keywords is None:
        return list(DEFAULT_TITLE_KEYWORDS)
    return [str(k).lower() for k in keywords]


def title_matches(title: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    lowered = title.lower()
    return any(k in lowered for k in keywords)
