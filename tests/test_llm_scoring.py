import json

import pytest

from jobs.models import Job
from jobs.scoring import criteria, llm
from tests.test_scoring import FakeResponse, FakeSession


def _job(**kw) -> Job:
    base = dict(source="linkedin", source_id="1", title="Engineering Manager", company="Acme", location="Berlin, Germany", workplace="hybrid", seniority="Mid-Senior level", posted_date="2026-10-16", url="u", description="Lead a team of 6.")
    base.update(kw)
    return Job(**base)


def _completion(answers: dict, model: str = "anthropic/claude-haiku-5.5") -> dict:
    return {
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": json.dumps(answers)}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


GOOD = {
    "fit": 78,
    "fit_confidence": 0.8,
    "verdict": "apply",
    "verdict_confidence": 0.85,
    "seniority_match": "match",
    "seniority_match_confidence": 0.9,
    "location_ok": "yes",
    "location_ok_confidence": 0.95,
    "language_blocker": "none",
    "language_blocker_confidence": 0.9,
    "team_type": "product",
    "team_type_confidence": 0.8,
    "stack_fit": "stack_agnostic",
    "stack_fit_confidence": 0.7,
    "reasoning": "EM role in Berlin, English, team of 6.",
}


def test_request_shape_and_parsing():
    session = FakeSession(FakeResponse(200, _completion(GOOD)))
    scorer = llm.LlmScorer("sk-test", session=session)

    result = scorer.score_job("I am an EM.", _job())

    body = session.calls[0]["json"]
    assert session.calls[0]["url"] == llm.ENDPOINT
    assert body["model"] == llm.MODEL
    assert body["temperature"] == 0
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["messages"][0]["role"] == "system"
    assert "verdict (one of: apply, maybe, skip)" in body["messages"][0]["content"]
    assert "I am an EM." in body["messages"][1]["content"]
    assert (result.fit, result.verdict, result.seniority_match, result.location_ok, result.language_blocker) == (78, "apply", "match", "yes", "none")
    assert result.confidences["verdict"] == 0.85
    assert result.raw["model"] == "anthropic/claude-haiku-5.5"


def test_schema_enums_follow_criteria():
    schema = llm.answer_schema()
    assert schema["properties"]["verdict"]["enum"] == list(criteria.VERDICT_QUESTION["criteria"])
    assert schema["properties"]["language_blocker"]["enum"] == list(criteria.LANGUAGE_QUESTION["criteria"])
    assert set(schema["required"]) == set(schema["properties"])


def test_fit_is_clamped():
    answers = dict(GOOD, fit=140)
    result = llm.parse_completion(_completion(answers))
    assert result.fit == 100


def test_unknown_choice_label_raises():
    answers = dict(GOOD, verdict="definitely")
    with pytest.raises(llm.LlmScoreError, match="verdict"):
        llm.parse_completion(_completion(answers))


def test_http_error_raises():
    scorer = llm.LlmScorer("k", session=FakeSession(FakeResponse(402, text="insufficient credits")))
    with pytest.raises(llm.LlmScoreError, match="HTTP 402"):
        scorer.score_job("p", _job())


def test_non_json_content_raises():
    data = {"choices": [{"message": {"content": "nope"}}]}
    with pytest.raises(llm.LlmScoreError, match="non-JSON"):
        llm.parse_completion(data)


def test_model_override():
    session = FakeSession(FakeResponse(200, _completion(GOOD)))
    llm.LlmScorer("k", session=session, model="openai/gpt-5-mini").score_job("p", _job())
    assert session.calls[0]["json"]["model"] == "openai/gpt-5-mini"
