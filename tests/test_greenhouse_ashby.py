import json

import pytest
import requests

from jobs.adapters import ashby, greenhouse
from jobs.config import title_matches
from tests.conftest import FIXTURES, FakeResponse, FakeSession


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_greenhouse_parse_fixture():
    jobs = greenhouse.parse_board(_load("greenhouse_jobs.json"), "getyourguide", "GetYourGuide")
    assert len(jobs) == 2
    em = next(j for j in jobs if "Engineering Manager" in j.title)
    assert em.source == "greenhouse"
    assert em.company == "GetYourGuide"
    assert em.job_id == f"greenhouse:{em.source_id}"
    assert em.location == "Berlin"
    assert em.url.startswith("https://job-boards.greenhouse.io/getyourguide/jobs/")
    assert em.posted_date == "2026-10-15"
    assert em.seniority == "L7"
    assert "<" not in em.description and "&lt;" not in em.description
    assert "GetYourGuide" in em.description
    assert em.extra["departments"] == ["Engineering"]


class JsonResponse(FakeResponse):
    def json(self):
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


def test_greenhouse_fetch_uses_content_url_and_404_is_an_error():
    session = FakeSession({"boards/acme/jobs": JsonResponse(200, json.dumps({"jobs": []}))})
    client = greenhouse.PoliteClient(min_sleep=0, max_sleep=0, session=session)
    assert greenhouse.fetch("acme", client=client) == []
    assert session.calls[0][0] == greenhouse.board_url("acme")

    session = FakeSession({"boards/nope/jobs": JsonResponse(404, "")})
    client = greenhouse.PoliteClient(min_sleep=0, max_sleep=0, session=session)
    with pytest.raises(ValueError, match="no Greenhouse board"):
        greenhouse.fetch("nope", client=client)


def test_ashby_fetch_404_is_an_error():
    session = FakeSession({"job-board/nope": JsonResponse(404, "")})
    client = ashby.PoliteClient(min_sleep=0, max_sleep=0, session=session)
    with pytest.raises(ValueError, match="no Ashby job board"):
        ashby.fetch("nope", client=client)


def test_ashby_parse_fixture():
    jobs = ashby.parse_board(_load("ashby_jobs.json"), "pliant", "Pliant")
    assert len(jobs) == 2
    em = next(j for j in jobs if "Engineering Manager" in j.title)
    assert em.source == "ashby"
    assert em.company == "Pliant"
    assert em.workplace == "remote"
    assert em.location == "Remote (European Union), Germany"
    assert em.posted_date == "2026-08-07"
    assert em.url == "https://jobs.ashbyhq.com/pliant/" + em.source_id
    assert em.description.startswith("About Us")
    assert "\n\n\n" not in em.description
    assert em.extra["department"] == "Engineering"


def test_ashby_unlisted_jobs_are_skipped():
    data = {"jobs": [{"id": "x", "title": "Hidden", "isListed": False}, {"id": "y", "title": "Shown", "location": "Berlin", "workplaceType": "Hybrid"}]}
    jobs = ashby.parse_board(data, "acme")
    assert [j.title for j in jobs] == ["Shown"]
    assert jobs[0].workplace == "hybrid"


def test_title_filter():
    keywords = ["engineering manager", "head of engineering", "tribe lead"]
    assert title_matches("Senior Engineering Manager, Platform (m/f/d)", keywords)
    assert title_matches("HEAD OF ENGINEERING", keywords)
    assert not title_matches("Senior Backend Engineer", keywords)
    assert title_matches("anything", [])  # an empty list disables the filter
