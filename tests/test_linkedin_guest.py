import pytest

from jobs.adapters import linkedin_guest as li
from jobs.http import PoliteClient, RateLimited
from tests.conftest import FakeResponse, FakeSession

SEARCH_URL = "https://www.linkedin.com/jobs/search/?keywords=Engineering%20Manager&geoId=101282230&f_TPR=r86400&f_WT=2&currentJobId=123&origin=JOB_SEARCH_PAGE"


def test_search_params_keeps_only_forwardable_params():
    params = li.search_params(SEARCH_URL)
    assert params == {"keywords": "Engineering Manager", "geoId": "101282230", "f_TPR": "r86400", "f_WT": "2"}


def test_search_params_rejects_url_without_query():
    with pytest.raises(ValueError):
        li.search_params("https://www.linkedin.com/jobs/search/")


def test_extract_job_ids_dedupes_and_keeps_order(fixture_text):
    assert li.extract_job_ids(fixture_text("linkedin_search.html")) == ["4477388263", "4477001122"]


def test_parse_search_page_cards(fixture_text):
    cards = li.parse_search_page(fixture_text("linkedin_search.html"))

    assert [c["id"] for c in cards] == ["4477388263", "4477001122", "4477388263"]
    first, second = cards[0], cards[1]
    assert first["title"] == "Teamleitung OSS Software Engineering (w/m/d)"
    assert first["company"] == "Deutsche Glasfaser Unternehmensgruppe"
    assert first["location"] == "Düsseldorf, North Rhine-Westphalia, Germany"
    assert first["posted_date"] == "2026-10-16"
    assert first["salary"] is None
    assert second["salary"] == "€90,000 - €110,000"
    assert second["posted_date"] == "2026-10-15"


def test_parse_posting_detail(fixture_text):
    posting = li.parse_posting(fixture_text("linkedin_posting.html"), "4477388263")

    assert posting["title"] == "Teamleitung OSS Software Engineering (w/m/d)"
    assert posting["company"] == "Deutsche Glasfaser Unternehmensgruppe"
    assert posting["location"] == "Düsseldorf, North Rhine-Westphalia, Germany"
    assert posting["seniority"] == "Associate"
    assert posting["employment_type"] == "Full-time"
    assert posting["industries"] == "Telecommunications"
    assert "Unternehmensbeschreibung" in posting["description"]
    assert "<" not in posting["description"]
    assert len(posting["description"]) > 500


def test_fetch_pages_dedupes_and_merges_details(fixture_text):
    search_html = fixture_text("linkedin_search.html")
    session = FakeSession(
        {
            "seeMoreJobPostings/search?keywords=Engineering Manager&geoId=101282230&f_TPR=r86400&start=0": FakeResponse(200, search_html),
            "seeMoreJobPostings/search?keywords=Engineering Manager&geoId=101282230&f_TPR=r86400&start=25": FakeResponse(200, "<ul></ul>"),
            "jobPosting/4477388263": FakeResponse(200, fixture_text("linkedin_posting.html")),
            "jobPosting/4477001122": FakeResponse(200, "<html></html>"),
        }
    )
    client = PoliteClient(min_sleep=0, max_sleep=0, session=session)

    jobs = li.fetch("https://www.linkedin.com/jobs/search/?keywords=Engineering%20Manager&geoId=101282230&f_TPR=r86400", max_pages=3, client=client)

    assert [j.source_id for j in jobs] == ["4477388263", "4477001122"]
    assert jobs[0].seniority == "Associate"
    assert jobs[0].url == "https://www.linkedin.com/jobs/view/4477388263"
    assert jobs[0].description.startswith("Unternehmensbeschreibung")
    assert jobs[0].workplace == "unknown"
    # second posting had no parsable detail: card data survives
    assert jobs[1].title == "Engineering Manager Platform (m/f/d)"
    assert jobs[1].workplace == "remote"
    assert jobs[1].salary == "€90,000 - €110,000"
    # 2 search pages (second empty stops paging) + 2 detail fetches
    assert len(session.calls) == 4


def test_fetch_respects_max_pages(fixture_text):
    search_html = fixture_text("linkedin_search.html")
    session = FakeSession({"seeMoreJobPostings/search": FakeResponse(200, search_html)})
    client = PoliteClient(min_sleep=0, max_sleep=0, session=session)

    li.fetch("https://www.linkedin.com/jobs/search/?keywords=x&geoId=1", max_pages=1, client=client, fetch_details=False)

    assert len(session.calls) == 1


def test_f_wt_hint_sets_workplace(fixture_text):
    session = FakeSession({"seeMoreJobPostings/search": FakeResponse(200, fixture_text("linkedin_search.html"))})
    client = PoliteClient(min_sleep=0, max_sleep=0, session=session)

    jobs = li.fetch("https://www.linkedin.com/jobs/search/?keywords=x&geoId=1&f_WT=3", max_pages=1, client=client, fetch_details=False)

    assert {j.workplace for j in jobs} == {"hybrid"}


def test_429_during_search_stops_and_returns_nothing(monkeypatch):
    monkeypatch.setattr("jobs.http.time.sleep", lambda s: None)
    session = FakeSession({"seeMoreJobPostings/search": FakeResponse(429, "", {"Retry-After": "5"})})
    client = PoliteClient(min_sleep=0, max_sleep=0, session=session)

    jobs = li.fetch("https://www.linkedin.com/jobs/search/?keywords=x&geoId=1", max_pages=5, client=client)

    assert jobs == []
    assert len(session.calls) == 1  # no retry loop


def test_429_during_details_keeps_card_data(fixture_text, monkeypatch):
    monkeypatch.setattr("jobs.http.time.sleep", lambda s: None)
    session = FakeSession(
        {
            "seeMoreJobPostings/search": FakeResponse(200, fixture_text("linkedin_search.html")),
            "jobPosting/": FakeResponse(429, ""),
        }
    )
    client = PoliteClient(min_sleep=0, max_sleep=0, session=session)

    jobs = li.fetch("https://www.linkedin.com/jobs/search/?keywords=x&geoId=1", max_pages=1, client=client)

    assert len(jobs) == 2
    assert all(j.description == "" for j in jobs)
    assert jobs[0].title == "Teamleitung OSS Software Engineering (w/m/d)"
    # 1 search + 1 detail attempt that hit 429, then no more detail requests
    assert len(session.calls) == 2


def test_polite_client_raises_rate_limited(monkeypatch):
    monkeypatch.setattr("jobs.http.time.sleep", lambda s: None)
    session = FakeSession({"x": FakeResponse(429, "")})
    client = PoliteClient(min_sleep=0, max_sleep=0, session=session)
    with pytest.raises(RateLimited):
        client.get("https://x")
