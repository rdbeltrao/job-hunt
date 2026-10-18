from jobs.adapters import personio
from jobs.http import PoliteClient
from tests.conftest import FakeResponse, FakeSession


def test_parse_feed_extracts_positions(fixture_text):
    jobs = personio.parse_feed(fixture_text("personio_feed.xml"), slug="everphone", company_name="Everphone")

    assert [j.source_id for j in jobs] == ["2019718", "2825215"]
    first = jobs[0]
    assert first.source == "personio"
    assert first.title == "Account Executive (w/m/d)"
    assert first.company == "Everphone"
    assert first.location.startswith("Berlin")
    assert first.seniority == "experienced"
    assert first.posted_date and len(first.posted_date) == 10
    assert first.url == "https://everphone.jobs.personio.de/job/2019718"
    assert first.salary is None
    assert first.extra["department"] == "Revenue"
    assert first.extra["slug"] == "everphone"


def test_parse_feed_description_is_plain_text_with_section_headings(fixture_text):
    jobs = personio.parse_feed(fixture_text("personio_feed.xml"), slug="everphone")

    desc = jobs[0].description
    assert "Über die Rolle" in desc
    assert "Deine Aufgaben" in desc
    assert "<p" not in desc and "<strong>" not in desc
    assert "- " in desc  # list items survive as bullets


def test_parse_feed_falls_back_to_subcompany_for_company_name(fixture_text):
    jobs = personio.parse_feed(fixture_text("personio_feed.xml"), slug="everphone")
    assert jobs[0].company == "Everphone GmbH"


def test_fetch_uses_feed_url_and_parses(fixture_text):
    session = FakeSession({"everphone.jobs.personio.de/xml": FakeResponse(200, fixture_text("personio_feed.xml"), {"Content-Type": "text/xml"})})
    client = PoliteClient(min_sleep=0, max_sleep=0, session=session)

    jobs = personio.fetch("everphone", "Everphone", client=client)

    assert len(jobs) == 2
    assert session.calls[0][0] == "https://everphone.jobs.personio.de/xml"


def test_fetch_rejects_html_not_found_page():
    session = FakeSession({"nobody.jobs.personio.de/xml": FakeResponse(200, "<!DOCTYPE html><html>Page Not Found</html>", {"Content-Type": "text/html"})})
    client = PoliteClient(min_sleep=0, max_sleep=0, session=session)

    try:
        personio.fetch("nobody", client=client)
    except ValueError as exc:
        assert "did not return a Personio XML feed" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_fetch_decodes_utf8_from_bytes_without_charset_header(fixture_text):
    """Personio sends no charset; umlauts must survive (requests.text would guess latin-1)."""
    session = FakeSession({"everphone.jobs.personio.de/xml": FakeResponse(200, fixture_text("personio_feed.xml"), {"Content-Type": "application/xml"})})
    client = PoliteClient(min_sleep=0, max_sleep=0, session=session)

    jobs = personio.fetch("everphone", "Everphone", client=client)

    assert "Köpenicker Straße" in jobs[0].location
    assert "Ã" not in jobs[0].location


def test_parse_feed_tolerates_empty_job_descriptions(fixture_text):
    """Wandelbots' default-language feed ships <jobDescriptions></jobDescriptions> for every position."""
    jobs = personio.parse_feed(fixture_text("personio_feed_wandelbots.xml"), slug="wandelbots", company_name="Wandelbots")

    assert [j.source_id for j in jobs] == ["2807515", "2743165"]
    assert all(j.description == "" for j in jobs)
    assert jobs[0].title == "Automation Engineer (gn) Industrial Robotics & Software Development"
    assert jobs[0].seniority == "experienced"
    assert jobs[0].posted_date == "2026-09-22"


def test_fill_missing_descriptions_from_english_feed(fixture_text):
    jobs = personio.parse_feed(fixture_text("personio_feed_wandelbots.xml"), slug="wandelbots")

    filled = personio.fill_missing_descriptions(jobs, fixture_text("personio_feed_wandelbots_en.xml"))

    assert filled == 2
    desc = jobs[0].description
    assert desc.startswith("Your mission\n")
    assert "Your profile" in desc and "Why us?" in desc
    assert "- Configuring, parameterizing, and commissioning industrial robots" in desc
    assert "<strong>" not in desc and "<li>" not in desc
    assert "Kubernetes" in jobs[1].description


def test_fill_missing_descriptions_keeps_existing_text(fixture_text):
    jobs = personio.parse_feed(fixture_text("personio_feed.xml"), slug="everphone")
    before = [j.description for j in jobs]

    assert personio.fill_missing_descriptions(jobs, fixture_text("personio_feed_wandelbots_en.xml")) == 0
    assert [j.description for j in jobs] == before


def test_fetch_retries_other_languages_when_descriptions_are_empty(fixture_text):
    session = FakeSession(
        {
            "wandelbots.jobs.personio.de/xml?language=en": FakeResponse(200, fixture_text("personio_feed_wandelbots_en.xml"), {"Content-Type": "application/xml"}),
            "wandelbots.jobs.personio.de/xml": FakeResponse(200, fixture_text("personio_feed_wandelbots.xml"), {"Content-Type": "application/xml"}),
        }
    )
    client = PoliteClient(min_sleep=0, max_sleep=0, session=session)

    jobs = personio.fetch("wandelbots", "Wandelbots", client=client)

    assert [c[0] for c in session.calls] == [
        "https://wandelbots.jobs.personio.de/xml",
        "https://wandelbots.jobs.personio.de/xml?language=en",
    ]
    assert len(jobs) == 2
    assert all(j.description for j in jobs)
    assert jobs[0].company == "Wandelbots"


def test_fetch_does_not_request_other_languages_when_descriptions_present(fixture_text):
    session = FakeSession({"everphone.jobs.personio.de/xml": FakeResponse(200, fixture_text("personio_feed.xml"), {"Content-Type": "application/xml"})})
    client = PoliteClient(min_sleep=0, max_sleep=0, session=session)

    personio.fetch("everphone", "Everphone", client=client)

    assert [c[0] for c in session.calls] == ["https://everphone.jobs.personio.de/xml"]


def test_fetch_tries_next_language_when_one_feed_fails(fixture_text):
    session = FakeSession(
        {
            "wandelbots.jobs.personio.de/xml?language=en": FakeResponse(500, "boom", {"Content-Type": "text/html"}),
            "wandelbots.jobs.personio.de/xml?language=de": FakeResponse(200, fixture_text("personio_feed_wandelbots_en.xml"), {"Content-Type": "application/xml"}),
            "wandelbots.jobs.personio.de/xml": FakeResponse(200, fixture_text("personio_feed_wandelbots.xml"), {"Content-Type": "application/xml"}),
        }
    )
    client = PoliteClient(min_sleep=0, max_sleep=0, session=session)

    jobs = personio.fetch("wandelbots", client=client)

    assert len(session.calls) == 3
    assert all(j.description for j in jobs)
