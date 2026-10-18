"""Fit scoring with an ordinary chat model through OpenRouter's chat completions endpoint.

Same five questions as the Jev scorer (see criteria.py), answered in one strict JSON
object. Used when the OpenRouter account cannot reach the typesafe provider that serves
Jev; any model the account allows works, Claude Haiku by default because it is cheap and
follows a schema reliably.
"""

from __future__ import annotations

import json
import logging

import requests

from jobs.models import Job
from jobs.scoring import criteria
from jobs.scoring.jev import JevResult, build_state

log = logging.getLogger(__name__)

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "anthropic/claude-haiku-5.5"
MAX_TOKENS = 1024
TIMEOUT_S = 120.0


class LlmScoreError(Exception):
    pass


def _choice_labels(question: dict) -> list[str]:
    return list(question["criteria"].keys())


def answer_schema() -> dict:
    """JSON schema for the answers: fit 0..100 plus one enum per choice question."""
    props: dict = {
        "fit": {"type": "integer", "minimum": 0, "maximum": 100},
        "fit_confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reasoning": {"type": "string"},
    }
    for key, q in criteria.QUESTIONS.items():
        if q["type"] == "choice":
            props[key] = {"type": "string", "enum": _choice_labels(q)}
            props[f"{key}_confidence"] = {"type": "number", "minimum": 0, "maximum": 1}
    return {
        "type": "object",
        "properties": props,
        "required": list(props.keys()),
        "additionalProperties": False,
    }


def build_instructions() -> str:
    """Render the questions from criteria.py as text for a chat model."""
    lines = [criteria.PROFILE_PREAMBLE, "", "Answer every question below with one JSON object.", ""]
    for key, q in criteria.QUESTIONS.items():
        if q["type"] == "choice":
            lines.append(f"{key} (one of: {', '.join(_choice_labels(q))}): {q['instructions']}")
            for label, desc in q["criteria"].items():
                lines.append(f"  - {label}: {desc}")
            lines.append(f"{key}_confidence: your confidence in that choice, 0 to 1.")
        else:
            lines.append(f"{key} (integer 0 to 100, 100 = perfect match): {q['instructions']}")
            lines.append(f"{key}_confidence: your confidence in that number, 0 to 1.")
        lines.append("")
    lines.append("reasoning: at most three short sentences on the decisive factors.")
    return "\n".join(lines)


def build_messages(profile: str, job: Job) -> list[dict]:
    state = build_state(profile, job)
    user = (
        f"<profile>\n{state['profile']}\n</profile>\n\n"
        f"<job>\n{json.dumps(state['job'], ensure_ascii=False, indent=1)}\n</job>"
    )
    return [
        {"role": "system", "content": build_instructions()},
        {"role": "user", "content": user},
    ]


class LlmScorer:
    def __init__(self, api_key: str, session: requests.Session | None = None, model: str = MODEL) -> None:
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        self.model = model

    def complete(self, messages: list[dict]) -> dict:
        body = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "temperature": 0,
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "fit_answers", "strict": True, "schema": answer_schema()},
            },
        }
        resp = self.session.post(ENDPOINT, json=body, timeout=TIMEOUT_S)
        if resp.status_code >= 400:
            raise LlmScoreError(f"HTTP {resp.status_code} from OpenRouter: {resp.text[:300]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise LlmScoreError(f"OpenRouter returned non-JSON: {resp.text[:300]}") from exc

    def score_job(self, profile: str, job: Job) -> JevResult:
        data = self.complete(build_messages(profile, job))
        return parse_completion(data, model=self.model)


def parse_completion(data: dict, model: str = MODEL) -> JevResult:
    try:
        text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise LlmScoreError(f"unexpected response shape: {json.dumps(data)[:300]}") from exc
    try:
        answers = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LlmScoreError(f"model returned non-JSON: {text[:200]}") from exc

    confidences: dict[str, float] = {}
    for key in ("fit", *[k for k, q in criteria.QUESTIONS.items() if q["type"] == "choice"]):
        c = answers.get(f"{key}_confidence")
        if isinstance(c, (int, float)) and not isinstance(c, bool):
            confidences[key] = float(c)

    fit = answers.get("fit")
    fit_int = max(0, min(100, int(round(fit)))) if isinstance(fit, (int, float)) and not isinstance(fit, bool) else None

    def choice(key: str) -> str:
        value = str(answers.get(key) or "")
        allowed = _choice_labels(criteria.QUESTIONS[key])
        if value not in allowed:
            raise LlmScoreError(f"{key}={value!r} is not one of {allowed}")
        return value

    raw = {"model": data.get("model", model), "answers": answers, "usage": data.get("usage", {})}
    return JevResult(
        fit=fit_int,
        verdict=choice("verdict"),
        **{key: choice(key) for key in criteria.REASON_QUESTIONS},
        confidences=confidences,
        raw=raw,
    )
