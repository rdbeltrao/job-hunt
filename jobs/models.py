from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Literal

Workplace = Literal["remote", "hybrid", "onsite", "unknown"]


@dataclass
class Job:
    source: str
    source_id: str
    title: str
    company: str
    location: str
    workplace: Workplace
    seniority: str
    posted_date: str  # ISO date (YYYY-MM-DD) or empty string when unknown
    url: str
    description: str
    salary: str | None = None
    # LinkedIn only: the company page, and the person from "Meet the hiring team" who can
    # be messaged (InMail). All empty when the posting shows none.
    company_url: str = ""
    contact_name: str = ""
    contact_title: str = ""
    contact_url: str = ""
    extra: dict = field(default_factory=dict)
    # Filled by `jobs score` / `jobs score --explain`; empty until then.
    first_seen: str = ""
    score: int | None = None
    verdict: str = ""
    strong_matches: str = ""
    gaps: str = ""
    red_flags: str = ""
    pitch: str = ""
    notes: str = ""
    scoring: dict = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        return (self.source, self.source_id)

    @property
    def job_id(self) -> str:
        return f"{self.source}:{self.source_id}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


def guess_workplace(*texts: str) -> Workplace:
    """Best-effort workplace classification from free text (title, location, description)."""
    blob = " ".join(t for t in texts if t).lower()
    if not blob:
        return "unknown"
    if "hybrid" in blob:
        return "hybrid"
    if "remote" in blob or "home office" in blob or "homeoffice" in blob or "work from home" in blob:
        return "remote"
    if "on-site" in blob or "onsite" in blob or "on site" in blob or "vor ort" in blob:
        return "onsite"
    return "unknown"
