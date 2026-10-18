"""Jev fit scoring through OpenRouter's decisions endpoint.

Request shape (confirmed from the OpenRouter Jev classification cookbook and the
jevhunt.com OpenRouter post, both read 2026-10-16):

  POST https://openrouter.ai/api/alpha/decisions
  Authorization: Bearer $OPENROUTER_API_KEY
  {"model": "typesafe/jev-1.13", "state": {...}, "questions": {key: question, ...}}

Response: {"model": "...", "answers": {key: {"type": "choice", "choice": label,
"confidence": 0..1} | {"type": "noul", "noul": 0..1}}, "usage": {...}}.

Neither source documents a `score` question type; see criteria.py for how `fit` is asked.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import requests

from jobs.models import Job
from jobs.scoring import criteria

log = logging.getLogger(__name__)

ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
MODEL = "typesafe/jev-1.13"
TIMEOUT_S = 60.0


class JevError(Exception):
    pass


def is_provider_blocked(exc: Exception) -> bool:
    """True for OpenRouter's refusal when the account's allowed-providers setting excludes typesafe."""
    text = str(exc)
    return "HTTP 404" in text and "allowed providers" in text.lower()


@dataclass
class JevResult:
    fit: int | None
    verdict: str
    seniority_match: str
    location_ok: str
    language_blocker: str
    team_type: str = ""
    stack_fit: str = ""
    confidences: dict[str, float] = field(default_factory=dict)
    probabilities: dict[str, dict[str, float]] = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    def reasons(self) -> dict[str, str]:
        return {key: getattr(self, key) for key in criteria.REASON_QUESTIONS}

    def to_scoring_dict(self) -> dict:
        out = {
            "model": self.raw.get("model", MODEL),
            **self.reasons(),
            "confidences": self.confidences,
        }
        if self.probabilities:
            out["probabilities"] = self.probabilities
        return out


class JevClient:
    def __init__(self, api_key: str, session: requests.Session | None = None, model: str = MODEL) -> None:
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        self.model = model

    def decide(self, state: dict, questions: dict) -> dict:
        payload = {"model": self.model, "state": state, "questions": questions}
        resp = self.session.post(ENDPOINT, json=payload, timeout=TIMEOUT_S)
        if resp.status_code >= 400:
            raise JevError(f"HTTP {resp.status_code} from Jev: {resp.text[:300]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise JevError(f"Jev returned non-JSON: {resp.text[:300]}") from exc
        if "answers" not in data:
            raise JevError(f"Jev response has no 'answers': {str(data)[:300]}")
        return data

    def score_job(self, profile: str, job: Job) -> JevResult:
        state = build_state(profile, job)
        data = self.decide(state, criteria.QUESTIONS)
        return parse_answers(data)


def build_state(profile: str, job: Job) -> dict:
    desc = job.description or ""
    if len(desc) > criteria.MAX_DESCRIPTION_CHARS:
        desc = desc[: criteria.MAX_DESCRIPTION_CHARS] + "\n[truncated]"
    return {
        "instructions": criteria.PROFILE_PREAMBLE,
        "profile": profile,
        "job": {
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "workplace": job.workplace,
            "seniority": job.seniority,
            "description": desc,
        },
    }


def parse_answers(data: dict) -> JevResult:
    answers = data.get("answers", {})
    confidences: dict[str, float] = {}
    probabilities: dict[str, dict[str, float]] = {}

    def choice(key: str) -> str:
        a = answers.get(key) or {}
        if isinstance(a.get("confidence"), (int, float)):
            confidences[key] = float(a["confidence"])
        if isinstance(a.get("probabilities"), dict):
            probabilities[key] = {str(k): float(v) for k, v in a["probabilities"].items() if isinstance(v, (int, float))}
        return str(a.get("choice") or a.get("value") or "")

    fit_answer = answers.get("fit") or {}
    fit: int | None = None
    # Documented: noul probability 0..1. Tolerated: a numeric score/value if a `score`
    # question type is used in criteria.py (shape unconfirmed, see that file).
    for key in ("noul", "score", "value"):
        v = fit_answer.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            # noul is a probability; a score/value is taken as 0..100 unless it is clearly a 0..1 fraction.
            scaled = v * 100 if key == "noul" or (isinstance(v, float) and 0.0 <= v <= 1.0) else v
            fit = max(0, min(100, int(round(scaled))))
            break

    return JevResult(
        fit=fit,
        verdict=choice("verdict"),
        **{key: choice(key) for key in criteria.REASON_QUESTIONS},
        confidences=confidences,
        probabilities=probabilities,
        raw=data,
    )


def apply_result(job: Job, result: JevResult) -> None:
    job.score = result.fit
    job.verdict = result.verdict
    job.scoring = result.to_scoring_dict()


def mark_no_description(job: Job) -> None:
    job.score = None
    job.verdict = "maybe"
    job.notes = "no description" if not job.notes else job.notes


def needs_scoring(job: Job) -> bool:
    return job.score is None and job.verdict == ""
