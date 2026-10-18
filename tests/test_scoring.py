import json

import pytest

from jobs.models import Job
from jobs.scoring import criteria, explain, jev


class FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.headers: dict = {}
        self.calls: list[dict] = []

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        return self.response


def _job(**kw) -> Job:
    base = dict(source="linkedin", source_id="1", title="Engineering Manager", company="Acme", location="Berlin, Germany", workplace="hybrid", seniority="Mid-Senior level", posted_date="2026-10-16", url="u", description="Lead a team of 6.")
    base.update(kw)
    return Job(**base)


JEV_OK = {
    "model": "typesafe/jev-1.13",
    "answers": {
        "fit": {"type": "noul", "noul": 0.82},
        "verdict": {"type": "choice", "choice": "apply", "probabilities": {"apply": 0.9, "maybe": 0.1, "skip": 0}, "confidence": 0.9},
        "seniority_match": {"type": "choice", "choice": "match", "confidence": 0.8},
        "location_ok": {"type": "choice", "choice": "yes"},
        "language_blocker": {"type": "choice", "choice": "none", "confidence": 0.95},
        "team_type": {"type": "choice", "choice": "product", "confidence": 0.9},
        "stack_fit": {"type": "choice", "choice": "required_match", "confidence": 0.8},
    },
    "usage": {"input_tokens": 10, "output_tokens": 5, "cost": 0.0001},
}


def test_jev_request_shape_and_parsing():
    session = FakeSession(FakeResponse(200, JEV_OK))
    client = jev.JevClient("sk-test", session=session)

    result = client.score_job("I am an EM with 8 years.", _job())

    call = session.calls[0]
    assert call["url"] == jev.ENDPOINT
    assert session.headers["Authorization"] == "Bearer sk-test"
    body = call["json"]
    assert body["model"] == "typesafe/jev-1.13"
    assert body["state"]["profile"] == "I am an EM with 8 years."
    assert body["state"]["job"]["title"] == "Engineering Manager"
    assert set(body["questions"]) == {"fit", "verdict", "seniority_match", "location_ok", "language_blocker", "team_type", "stack_fit"}
    assert result.team_type == "product" and result.stack_fit == "required_match"
    assert body["questions"]["verdict"]["type"] == "choice"
    assert set(body["questions"]["verdict"]["criteria"]) == {"apply", "maybe", "skip"}
    assert body["questions"]["language_blocker"]["criteria"].keys() >= {"none", "missing", "partial"}

    assert result.fit == 82
    assert result.verdict == "apply"
    assert result.seniority_match == "match"
    assert result.location_ok == "yes"
    assert result.language_blocker == "none"
    assert result.confidences["verdict"] == 0.9


def test_parse_answers_accepts_numeric_score_variants():
    assert jev.parse_answers({"answers": {"fit": {"type": "score", "score": 73}}}).fit == 73
    assert jev.parse_answers({"answers": {"fit": {"type": "score", "value": 0.5}}}).fit == 50
    assert jev.parse_answers({"answers": {"fit": {"type": "score", "score": 140}}}).fit == 100
    assert jev.parse_answers({"answers": {}}).fit is None


def test_jev_http_error_raises_without_retry():
    session = FakeSession(FakeResponse(402, text="insufficient credits"))
    client = jev.JevClient("k", session=session)
    with pytest.raises(jev.JevError, match="HTTP 402"):
        client.score_job("p", _job())
    assert len(session.calls) == 1


def test_jev_missing_answers_raises():
    session = FakeSession(FakeResponse(200, {"error": "bad"}))
    with pytest.raises(jev.JevError, match="no 'answers'"):
        jev.JevClient("k", session=session).score_job("p", _job())


def test_apply_result_and_no_description_rules():
    job = _job()
    jev.apply_result(job, jev.parse_answers(JEV_OK))
    assert job.score == 82 and job.verdict == "apply"
    assert job.scoring["seniority_match"] == "match"
    assert not jev.needs_scoring(job)

    blank = _job(description="")
    assert jev.needs_scoring(blank)
    jev.mark_no_description(blank)
    assert blank.score is None and blank.verdict == "maybe" and blank.notes == "no description"
    assert not jev.needs_scoring(blank)


def test_long_description_is_truncated_in_state():
    state = jev.build_state("p", _job(description="x" * 10_000))
    assert len(state["job"]["description"]) < 10_000
    assert state["job"]["description"].endswith("[truncated]")


# ---- Claude explain pass


def _completion(content: str, finish_reason: str = "stop") -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish_reason}]}


def test_explain_job_parses_structured_output():
    payload = {"strong_matches": "8y EM; platform", "gaps": "no fintech", "red_flags": "", "pitch": "I led..."}
    session = FakeSession(FakeResponse(200, _completion(json.dumps(payload))))
    client = explain.ExplainClient("key", session=session)

    fields = explain.explain_job(client, "profile text", _job())

    call = session.calls[0]
    assert call["url"] == explain.ENDPOINT
    assert call["json"]["model"] == "anthropic/claude-sonnet-5.5"
    assert call["json"]["response_format"]["type"] == "json_schema"
    assert call["json"]["messages"][0]["role"] == "system"
    assert "profile text" in call["json"]["messages"][1]["content"]
    assert session.headers["Authorization"] == "Bearer key"
    assert fields == payload


def test_explain_model_override():
    session = FakeSession(FakeResponse(200, _completion("{}")))
    client = explain.ExplainClient("key", session=session, model="openai/gpt-5")
    explain.explain_job(client, "p", _job())
    assert session.calls[0]["json"]["model"] == "openai/gpt-5"


def test_explain_http_error_raises():
    client = explain.ExplainClient("key", session=FakeSession(FakeResponse(402, {"error": "no credits"})))
    with pytest.raises(explain.ExplainError):
        explain.explain_job(client, "p", _job())


def test_explain_content_filter_raises():
    client = explain.ExplainClient("key", session=FakeSession(FakeResponse(200, _completion("", "content_filter"))))
    with pytest.raises(explain.ExplainError):
        explain.explain_job(client, "p", _job())


def test_explain_non_json_raises():
    client = explain.ExplainClient("key", session=FakeSession(FakeResponse(200, _completion("not json"))))
    with pytest.raises(explain.ExplainError):
        explain.explain_job(client, "p", _job())


def test_needs_explanation_threshold():
    assert explain.needs_explanation(_job(score=criteria.EXPLAIN_THRESHOLD))
    assert not explain.needs_explanation(_job(score=criteria.EXPLAIN_THRESHOLD - 1))
    assert not explain.needs_explanation(_job(score=None))
    assert not explain.needs_explanation(_job(score=90, pitch="done"))


def test_jev_probabilities_are_kept():
    result = jev.parse_answers(JEV_OK)
    assert result.probabilities["verdict"] == {"apply": 0.9, "maybe": 0.1, "skip": 0.0}
    assert "seniority_match" not in result.probabilities
    assert result.to_scoring_dict()["probabilities"]["verdict"]["apply"] == 0.9
