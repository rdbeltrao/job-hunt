"""Daily LinkedIn engagement: which posts from your chosen creators are worth a comment today.

The guide this follows (Ksu Dranitsina's LinkedIn guide) says that during a job search,
one to three thoughtful comments a day on posts by a handful of creators in your field
raise search appearances far more than writing posts. This module keeps the list of
creators (creators.yaml), remembers which posts you commented on (out/engaged.json) and
turns the posts the browser adapter read into a JSON hand-off for whoever drafts the
comments. It never comments, likes or posts.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

DEFAULT_CREATORS_PATH = Path("creators.yaml")
DEFAULT_DONE_PATH = Path("out/engaged.json")

INSTRUCTIONS = (
    "Pick at most three posts for today, preferring posts about engineering management, team "
    "practices, hiring, remote work or AI in engineering, then higher-priority creators, then fresher "
    "posts. For each, write one comment of 40 to 90 words in the language of the post that adds one "
    "thing: an experience with its outcome, a respectful counterpoint, a concrete example, or a "
    "question after one sentence of your own view. First sentence states the view, last sentence "
    "lands the point. No 'great post', no restating the post, no hashtags, no links, no numbers that "
    "are not confirmed, nothing unkind about a person, company or recruiter. Never post anything; "
    "return the comments with the post URLs."
)


@dataclass
class Creator:
    name: str
    url: str
    priority: int = 1
    enabled: bool = True


@dataclass
class Post:
    url: str
    author: str
    text: str
    posted_date: str = ""  # ISO date or empty
    age_text: str = ""  # LinkedIn's relative wording, e.g. "2d"
    reactions: str = ""
    comments: str = ""
    creator: str = ""
    priority: int = 1

    def to_dict(self) -> dict:
        return asdict(self)


def load_creators(path: Path = DEFAULT_CREATORS_PATH) -> list[Creator]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    creators = [Creator(**item) for item in data.get("creators", [])]
    return [c for c in creators if c.enabled]


def load_done(path: Path = DEFAULT_DONE_PATH) -> dict[str, str]:
    """Post URLs already commented on, normalised -> ISO date."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {normalise_url(str(k)): str(v) for k, v in data.items()}


def save_done(done: dict[str, str], path: Path = DEFAULT_DONE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(sorted(done.items())), indent=1) + "\n", encoding="utf-8")


def mark_done(urls: list[str], done: dict[str, str], today: str | None = None) -> dict[str, str]:
    today = today or date.today().isoformat()
    for u in urls:
        done[normalise_url(u)] = today
    return done


def normalise_url(url: str) -> str:
    """Strip tracking query strings and trailing slashes so the same post matches itself."""
    base = url.split("?", 1)[0].split("#", 1)[0].strip()
    return base.rstrip("/")


def build_pending(posts: list[Post], done: dict[str, str], max_age_days: int = 3, today: date | None = None) -> dict:
    """The hand-off: posts not yet commented on and not older than max_age_days, best first."""
    today = today or date.today()
    cutoff = (today - timedelta(days=max_age_days)).isoformat()
    fresh: list[Post] = []
    skipped_done: list[str] = []
    skipped_old: list[str] = []
    seen: set[str] = set()
    for post in posts:
        key = normalise_url(post.url)
        if not key or key in seen:
            continue
        seen.add(key)
        if key in done:
            skipped_done.append(post.url)
            continue
        if post.posted_date and post.posted_date < cutoff:
            skipped_old.append(post.url)
            continue
        fresh.append(post)
    # Highest priority first; within a priority, newest first.
    fresh = sorted(fresh, key=lambda p: (p.priority, p.posted_date or ""), reverse=True)
    return {
        "instructions": INSTRUCTIONS,
        "generated": today.isoformat(),
        "posts": [p.to_dict() for p in fresh],
        "skipped_done": skipped_done,
        "skipped_old": skipped_old,
    }


def posted_date_from_age(age_text: str, now: datetime | None = None) -> str:
    """LinkedIn's relative age ('3h', '2d', '1w', '5mo') to an ISO date; empty when unreadable."""
    now = now or datetime.now()
    t = (age_text or "").strip().lower().replace("•", " ").split()
    if not t:
        return ""
    token = t[0]
    digits = "".join(ch for ch in token if ch.isdigit())
    unit = token[len(digits):]
    if not digits:
        return ""
    n = int(digits)
    if unit.startswith("h") or unit.startswith("m") and unit != "mo":
        return now.date().isoformat()
    if unit.startswith("d"):
        return (now - timedelta(days=n)).date().isoformat()
    if unit.startswith("w"):
        return (now - timedelta(weeks=n)).date().isoformat()
    if unit.startswith("mo"):
        return (now - timedelta(days=30 * n)).date().isoformat()
    if unit.startswith("y"):
        return (now - timedelta(days=365 * n)).date().isoformat()
    return ""
