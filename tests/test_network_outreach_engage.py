import argparse
import json
from datetime import date, datetime
from pathlib import Path

from jobs import cli, engage, export, network, outreach
from jobs.adapters import linkedin_activity

FIXTURES = Path(__file__).parent / "fixtures"


def _row(job_id="linkedin:1", company="Acme", verdict="apply", status="new", title="Engineering Manager", **kw):
    row = {col: "" for col in export.HEADER}
    row.update(job_id=job_id, company=company, verdict=verdict, status=status, title=title, url=f"https://x/{job_id}")
    row.update(kw)
    return row


# --- network -------------------------------------------------------------------------


def test_rank_companies_prefers_applied_then_apply_then_maybe_and_skips_closed_and_done():
    rows = [
        _row("linkedin:1", "Maybe Co", verdict="maybe"),
        _row("linkedin:2", "Apply Co", verdict="apply"),
        _row("linkedin:3", "Applied Co", verdict="maybe", status="applied"),
        _row("linkedin:4", "Skip Co", verdict="skip"),
        _row("linkedin:5", "Rejected Co", verdict="apply", status="rejected"),
        _row("linkedin:6", "Done Co", verdict="apply"),
        _row("linkedin:7", "Apply Co", verdict="maybe"),
    ]
    plans = network.rank_companies(rows, done={"done co": "2026-10-12"})
    assert [p.company for p in plans] == ["Applied Co", "Apply Co", "Maybe Co"]
    assert plans[0].applied and not plans[1].applied
    assert len(plans[1].openings) == 2


def test_render_plan_lists_links_per_role_and_company_page():
    plans = network.rank_companies([_row("linkedin:1", "Acme GmbH", verdict="apply")])
    text = network.render_plan(plans, roles=["CTO"], today="2026-10-19")
    assert "# LinkedIn connection plan, week of 2026-10-19" in text
    assert "## 1. Acme GmbH (1 opening)" in text
    assert "search/results/companies/?keywords=Acme+GmbH" in text
    assert "CTO: https://www.linkedin.com/search/results/people/?keywords=Acme+GmbH+CTO" in text
    assert "Engineering Manager [apply](https://x/linkedin:1)" in text


def test_render_plan_uses_company_page_and_lists_job_poster_when_the_posting_had_them():
    rows = [
        _row(
            "linkedin:1",
            "Acme GmbH",
            verdict="apply",
            company_url="https://www.linkedin.com/company/acme-gmbh/",
            contact_name="Jane Doe",
            contact_title="Talent Acquisition Manager",
            contact_url="https://www.linkedin.com/in/jane-doe/",
        ),
        _row("linkedin:2", "Acme GmbH", verdict="maybe", title="Head of Engineering"),
    ]
    text = network.render_plan(network.rank_companies(rows), roles=["CTO"], today="2026-10-19")
    assert "- [ ] Follow the company page: https://www.linkedin.com/company/acme-gmbh/" in text
    assert "search/results/companies/" not in text
    assert "- [ ] Message the job poster of Engineering Manager: Jane Doe (Talent Acquisition Manager) https://www.linkedin.com/in/jane-doe/" in text
    assert text.count("Message the job poster") == 1


def test_render_plan_without_companies_says_so():
    assert "Nothing to do" in network.render_plan([], today="2026-10-19")


def test_network_done_roundtrip(tmp_path):
    path = tmp_path / "network.json"
    done = network.mark_done(["Acme", " Beta Co "], {}, today="2026-10-19")
    network.save_done(done, path)
    assert network.load_done(path) == {"acme": "2026-10-19", "beta co": "2026-10-19"}


def test_run_network_writes_plan_and_done_hides_company(tmp_path, capsys):
    csv_path = tmp_path / "jobs.csv"
    export.write_rows(csv_path, [_row("linkedin:1", "Acme", verdict="apply"), _row("linkedin:2", "Beta", verdict="maybe")])
    done_file = tmp_path / "network.json"
    out = tmp_path / "plan.md"
    args = argparse.Namespace(csv=str(csv_path), done_file=str(done_file), limit=10, quota=100, roles=None, out=str(out), date="2026-10-19", done=None)
    assert cli.run_network(args) == 0
    assert "2 companies" in capsys.readouterr().out
    assert "## 1. Acme" in out.read_text() and "## 2. Beta" in out.read_text()

    assert cli.run_network(argparse.Namespace(csv=str(csv_path), done_file=str(done_file), limit=10, quota=100, roles=None, out=None, date="2026-10-19", done=["Acme"])) == 0
    capsys.readouterr()
    assert cli.run_network(argparse.Namespace(csv=str(csv_path), done_file=str(done_file), limit=10, quota=100, roles=None, out=None, date=None, done=None)) == 0
    text = capsys.readouterr().out
    assert "Acme" not in text and "## 1. Beta" in text


# --- outreach ------------------------------------------------------------------------


def test_build_pack_has_job_profile_people_searches_and_message_shapes():
    pack = outreach.build_pack(_row("linkedin:9", "Soar", title="Engineering Manager", description="x" * 10000), "I am Renato")
    assert pack["profile"] == "I am Renato"
    assert pack["job"]["job_id"] == "linkedin:9" and len(pack["job"]["description"]) == outreach.MAX_DESCRIPTION_CHARS
    assert set(pack["find_people"]) == {"hiring_manager", "recruiter", "referrer"}
    assert pack["find_people"]["recruiter"][0]["url"].startswith("https://www.linkedin.com/search/results/people/?keywords=Soar+Technical+Recruiter")
    assert "Engineering+Manager" in pack["job_posts_search"]
    assert set(pack["message_types"]) == {"recruiter-or-hiring-manager", "referral", "networking", "connection-note"}
    assert pack["contact"] is None
    assert pack["job"]["company_url"] == ""
    assert pack["company_page_search"].startswith("https://www.linkedin.com/search/results/companies/?keywords=Soar")


def test_build_pack_names_the_job_poster_and_company_page_when_known():
    row = _row(
        "linkedin:9",
        "Soar",
        company_url="https://www.linkedin.com/company/soar/",
        contact_name="Jane Doe",
        contact_title="Talent Acquisition Manager at Soar",
        contact_url="https://www.linkedin.com/in/jane-doe/",
    )
    pack = outreach.build_pack(row, "I am Renato")
    assert pack["contact"] == {"name": "Jane Doe", "title": "Talent Acquisition Manager at Soar", "url": "https://www.linkedin.com/in/jane-doe/"}
    assert pack["job"]["company_url"] == "https://www.linkedin.com/company/soar/"
    assert pack["company_page_search"] == "https://www.linkedin.com/company/soar/"


def test_run_outreach_writes_pack_and_rejects_unknown_id(tmp_path, capsys):
    csv_path = tmp_path / "jobs.csv"
    export.write_rows(csv_path, [_row("linkedin:9", "Soar")])
    profile = tmp_path / "profile.md"
    profile.write_text("profile text")
    out = tmp_path / "pack.json"
    assert cli.run_outreach(argparse.Namespace(job_id="linkedin:9", csv=str(csv_path), profile=str(profile), out=str(out))) == 0
    assert json.loads(out.read_text())["job"]["company"] == "Soar"
    assert cli.run_outreach(argparse.Namespace(job_id="linkedin:404", csv=str(csv_path), profile=str(profile), out=None)) == 1
    assert "job_id not in" in capsys.readouterr().out


def test_status_note_without_set_keeps_status(tmp_path, capsys):
    csv_path = tmp_path / "jobs.csv"
    export.write_rows(csv_path, [_row("linkedin:9", "Soar", status="applied", notes="2026-10-19 applied: form")])
    args = argparse.Namespace(csv=str(csv_path), set=None, note="outreach: Anna, recruiter, LinkedIn", date="2026-10-20", job_ids=["linkedin:9"])
    assert cli.run_status(args) == 0
    row = export.read_rows(csv_path)[0]
    assert row["status"] == "applied"
    assert row["notes"].splitlines() == ["2026-10-19 applied: form", "2026-10-20 applied: outreach: Anna, recruiter, LinkedIn"]


# --- engage --------------------------------------------------------------------------


def test_posted_date_from_age():
    now = datetime(2026, 10, 19, 12, 0)
    assert engage.posted_date_from_age("3h", now) == "2026-10-19"
    assert engage.posted_date_from_age("2d •", now) == "2026-10-17"
    assert engage.posted_date_from_age("1w", now) == "2026-10-12"
    assert engage.posted_date_from_age("2mo", now) == "2026-08-20"
    assert engage.posted_date_from_age("", now) == ""


def test_build_pending_skips_done_and_old_and_sorts_by_priority_then_date():
    posts = [
        engage.Post(url="https://l/a?x=1", author="A", text="t", posted_date="2026-10-18", priority=1),
        engage.Post(url="https://l/b/", author="B", text="t", posted_date="2026-10-19", priority=1),
        engage.Post(url="https://l/c", author="C", text="t", posted_date="2026-10-10", priority=3),
        engage.Post(url="https://l/d", author="D", text="t", posted_date="2026-10-17", priority=2),
    ]
    payload = engage.build_pending(posts, done={"https://l/b": "2026-10-19"}, max_age_days=3, today=date(2026, 10, 19))
    assert [p["url"] for p in payload["posts"]] == ["https://l/d", "https://l/a?x=1"]
    assert payload["skipped_done"] == ["https://l/b/"] and payload["skipped_old"] == ["https://l/c"]


def test_engage_done_roundtrip_normalises_urls(tmp_path):
    path = tmp_path / "engaged.json"
    engage.save_done(engage.mark_done(["https://l/a/?utm=1"], {}, today="2026-10-19"), path)
    assert engage.load_done(path) == {"https://l/a": "2026-10-19"}


def test_load_creators_drops_disabled(tmp_path):
    path = tmp_path / "creators.yaml"
    path.write_text("creators:\n  - name: A\n    url: https://www.linkedin.com/in/a/\n    priority: 2\n  - name: B\n    url: https://www.linkedin.com/in/b/\n    enabled: false\n")
    creators = engage.load_creators(path)
    assert [c.name for c in creators] == ["A"] and creators[0].priority == 2


def test_activity_url():
    assert linkedin_activity.activity_url("https://www.linkedin.com/in/ksuhelpme/?x=1") == "https://www.linkedin.com/in/ksuhelpme/recent-activity/all/"
    assert linkedin_activity.activity_url("https://www.linkedin.com/in/ksuhelpme/recent-activity/all") == "https://www.linkedin.com/in/ksuhelpme/recent-activity/all/"


def test_parse_posts_reads_urn_author_text_and_age():
    html = (FIXTURES / "linkedin_activity.html").read_text(encoding="utf-8")
    posts = linkedin_activity.parse_posts(html, now=datetime(2026, 10, 19, 12, 0))
    assert len(posts) == 2
    first = posts[0]
    assert first.url == "https://www.linkedin.com/feed/update/urn:li:activity:7384000000000000001/"
    assert first.author == "Ksu Dranitsina"
    assert first.text.startswith("Three questions I ask every hiring manager")
    assert first.age_text == "2d" and first.posted_date == "2026-10-17"
    assert posts[1].age_text == "1w"


def test_parse_posts_on_empty_html_returns_nothing():
    assert linkedin_activity.parse_posts("<html></html>") == []


def test_parse_posts_real_2025_markup_reads_share_and_ugc_ids_age_and_counts():
    html = (FIXTURES / "linkedin_activity_real.html").read_text(encoding="utf-8")
    posts = linkedin_activity.parse_posts(html, now=datetime(2026, 10, 19, 12, 0))
    assert [p.url for p in posts] == [
        "https://www.linkedin.com/feed/update/urn:li:ugcPost:7513367456111906816/",
        "https://www.linkedin.com/feed/update/urn:li:share:7516767318291398657/",
    ]
    assert posts[0].author == "Ksu Dranitsina"
    assert posts[0].age_text == "1h" and posts[0].posted_date == "2026-10-19"
    assert posts[1].age_text == "2d" and posts[1].posted_date == "2026-10-17"
    assert posts[0].reactions == "14" and posts[0].comments == "3"
    assert posts[1].text.startswith("Я проснулась")
