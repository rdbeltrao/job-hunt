"""Second pass: an LLM writes strong_matches, gaps, red_flags and pitch for promising jobs.

Goes through OpenRouter's chat completions endpoint so the same OPENROUTER_API_KEY that
pays for Jev also pays for this. The model is any OpenRouter chat model id; the default
is Claude Sonnet, overridable with JOB_HUNT_EXPLAIN_MODEL.
"""

from __future__ import annotations

import json
import logging

import requests

from jobs.models import Job
from jobs.scoring import criteria

log = logging.getLogger(__name__)

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "anthropic/claude-sonnet-5.5"
MAX_TOKENS = 2048
TIMEOUT_S = 120.0

EXPLANATION_SCHEMA = {
    "type": "object",
    "properties": {
        "strong_matches": {"type": "string"},
        "gaps": {"type": "string"},
        "red_flags": {"type": "string"},
        "pitch": {"type": "string"},
    },
    "required": ["strong_matches", "gaps", "red_flags", "pitch"],
    "additionalProperties": False,
}

FIELDS = ("strong_matches", "gaps", "red_flags", "pitch")


class ExplainError(Exception):
    pass


class ExplainClient:
    def __init__(self, api_key: str, session: requests.Session | None = None, model: str = MODEL) -> None:
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        self.model = model

    def complete(self, messages: list[dict]) -> dict:
        body = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "explanation", "strict": True, "schema": EXPLANATION_SCHEMA},
            },
        }
        response = self.session.post(ENDPOINT, json=body, timeout=TIMEOUT_S)
        if response.status_code != 200:
            raise ExplainError(f"OpenRouter HTTP {response.status_code}: {response.text[:300]}")
        return response.json()


def make_client(api_key: str, model: str = MODEL) -> ExplainClient:
    return ExplainClient(api_key, model=model)


def build_messages(profile: str, job: Job) -> list[dict]:
    desc = job.description or ""
    if len(desc) > criteria.MAX_DESCRIPTION_CHARS:
        desc = desc[: criteria.MAX_DESCRIPTION_CHARS] + "\n[truncated]"
    user = (
        f"<profile>\n{profile}\n</profile>\n\n"
        f"<job>\nTitle: {job.title}\nCompany: {job.company}\nLocation: {job.location}\n"
        f"Workplace: {job.workplace}\nSeniority: {job.seniority}\n\n{desc}\n</job>\n\n"
        f"{criteria.EXPLAIN_INSTRUCTIONS}"
    )
    return [
        {"role": "system", "content": criteria.EXPLAIN_SYSTEM},
        {"role": "user", "content": user},
    ]


def explain_job(client: ExplainClient, profile: str, job: Job) -> dict[str, str]:
    """Ask the model for the four explanation fields. Returns a dict with exactly those keys."""
    data = client.complete(build_messages(profile, job))
    try:
        choice = data["choices"][0]
        text = choice["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise ExplainError(f"unexpected response shape: {json.dumps(data)[:300]}") from exc
    if choice.get("finish_reason") == "content_filter":
        raise ExplainError("model declined to answer (finish_reason=content_filter)")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExplainError(f"model returned non-JSON: {text[:200]}") from exc
    return {k: str(parsed.get(k, "")).strip() for k in FIELDS}


def apply_explanation(job: Job, fields: dict[str, str]) -> None:
    job.strong_matches = fields.get("strong_matches", "")
    job.gaps = fields.get("gaps", "")
    job.red_flags = fields.get("red_flags", "")
    job.pitch = fields.get("pitch", "")


def needs_explanation(job: Job) -> bool:
    return (
        job.score is not None
        and job.score >= criteria.EXPLAIN_THRESHOLD
        and not (job.strong_matches or job.gaps or job.pitch)
    )
