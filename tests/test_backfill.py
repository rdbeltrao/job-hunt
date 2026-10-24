import argparse
import json

from jobs import backfill, cli, export
from jobs.adapters import linkedin_browser as lb
from jobs.models import Job
from jobs.store import load_jobs, save_jobs


def _row(job_id="linkedin:1", verdict="apply", status="new", score="70", source="linkedin", **kw):
    row = {col: "" for col in export.HEADER}
    row.update(job_id=job_id, source=source, verdict=verdict, status=status, score=score, company="Acme", title="EM", url=f"https://x/{job_id}")
    row.update(kw)
    return row


def test_select_targets_orders_active_then_apply_then_maybe_by_score_and_skips_closed_done_and_filled():
    rows = [
        _row("linkedin:1", verdict="maybe", score="50"),
        _row("linkedin:2", verdict="apply", score="60"),
        _row("linkedin:3", verdict="apply", score="90"),
        _row("linkedin:4", verdict="maybe", status="applied", score="40"),
        _row("linkedin:5", verdict="apply", status="rejected", score="99"),
        _row("linkedin:6", verdict="skip", score="10"),
        _row("linkedin:7", verdict="apply", score="80", company_url="https://www.linkedin.com/company/acme/"),
        _row("greenhouse:8", verdict="apply", score="95"),
        _row("linkedin:9", verdict="apply", score="85"),
    ]
    targets = backfill.select_targets(rows, done={"linkedin:9"})
    assert [t.job_id for t in targets] == ["linkedin:4", "linkedin:3", "linkedin:2", "linkedin:1"]
    assert targets[1].source_id == "3"

    assert [t.job_id for t in backfill.select_targets(rows, done={"linkedin:9"}, limit=2)] == ["linkedin:4", "linkedin:3"]
    everything = backfill.select_targets(rows, done={"linkedin:9"}, everything=True)
    assert "linkedin:6" in [t.job_id for t in everything] and "linkedin:5" not in [t.job_id for t in everything]
    assert [t.job_id for t in backfill.select_targets(rows, verdicts=("apply",))] == ["linkedin:4", "linkedin:3", "linkedin:9", "linkedin:2"]


def test_apply_detail_fills_row_and_job_only_when_something_was_found():
    row = _row()
    job = Job(source="linkedin", source_id="1", title="EM", company="Acme", location="", workplace="unknown", seniority="", posted_date="", url="u", description="")
    assert backfill.apply_detail(row, lb.Detail(), job) is False
    assert row["company_url"] == "" and job.company_url == ""

    detail = lb.Detail(company_url="https://www.linkedin.com/company/acme/", contact_name="Jane Doe", contact_title="Recruiter", contact_url="https://www.linkedin.com/in/jane-doe/")
    assert backfill.apply_detail(row, detail, job) is True
    assert row["contact_name"] == "Jane Doe" and row["company_url"] == "https://www.linkedin.com/company/acme/"
    assert job.contact_url == "https://www.linkedin.com/in/jane-doe/" and job.contact_title == "Recruiter"


def test_done_roundtrip_and_corrupt_file(tmp_path):
    path = tmp_path / "backfill.json"
    assert backfill.load_done(path) == set()
    backfill.save_done({"linkedin:2", "linkedin:1"}, path)
    assert backfill.load_done(path) == {"linkedin:1", "linkedin:2"}
    path.write_text("{nope")
    assert backfill.load_done(path) == set()


def test_export_fills_linkedin_columns_on_existing_rows_without_touching_the_rest(tmp_path):
    path = tmp_path / "jobs.csv"
    export.write_rows(path, [_row("linkedin:1", status="applied", notes="called"), _row("linkedin:2", contact_url="https://www.linkedin.com/in/kept/"), _row("linkedin:3")])
    jobs = [
        Job(source="linkedin", source_id="1", title="EM", company="Acme", location="", workplace="unknown", seniority="", posted_date="", url="u", description="", company_url="https://www.linkedin.com/company/acme/", contact_name="Jane Doe", contact_title="", contact_url="https://www.linkedin.com/in/jane-doe/"),
        Job(source="linkedin", source_id="2", title="EM", company="Acme", location="", workplace="unknown", seniority="", posted_date="", url="u", description="", company_url="https://www.linkedin.com/company/other/"),
        Job(source="linkedin", source_id="3", title="EM", company="Acme", location="", workplace="unknown", seniority="", posted_date="", url="u", description=""),
    ]
    assert export.fill_linkedin_columns(path, jobs) == 1
    rows = export.read_rows(path)
    assert rows[0]["company_url"] == "https://www.linkedin.com/company/acme/" and rows[0]["contact_name"] == "Jane Doe"
    assert rows[0]["status"] == "applied" and rows[0]["notes"] == "called"
    assert rows[1]["contact_url"] == "https://www.linkedin.com/in/kept/" and rows[1]["company_url"] == ""  # a row with any value is left alone
    assert rows[2]["company_url"] == ""
    assert export.fill_linkedin_columns(path, jobs) == 0


HIRER_HTML = """
<div class="jobs-details">
  <div class="job-details-jobs-unified-top-card__company-name"><a href="https://www.linkedin.com/company/acme/life">Acme</a></div>
  <div class="hirer-card__hirer-information">
    <a href="https://www.linkedin.com/in/jane-doe/"><strong>Jane Doe</strong></a>
    <div class="hirer-card__hirer-job-title">Recruiter at Acme</div>
  </div>
</div>
"""


def test_run_backfill_writes_csv_jsonl_and_done_file(tmp_path, monkeypatch, capsys):
    csv_path = tmp_path / "jobs.csv"
    export.write_rows(csv_path, [_row("linkedin:1", status="applied", notes="n"), _row("linkedin:2", verdict="maybe"), _row("linkedin:3", verdict="skip")])
    jobs_path = tmp_path / "jobs.jsonl"
    save_jobs([Job(source="linkedin", source_id="1", title="EM", company="Acme", location="", workplace="unknown", seniority="", posted_date="", url="u", description="d", score=70, verdict="apply")], jobs_path)
    done_path = tmp_path / "backfill.json"

    opened: list[str] = []

    class FakeContext:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(context, source_id, dump_dir=None):
        opened.append(source_id)
        return HIRER_HTML if source_id == "1" else "<div></div>"

    monkeypatch.setattr("jobs.browser.persistent_context", lambda headless=True: FakeContext())
    monkeypatch.setattr(backfill, "open_job_page", fake_open)
    monkeypatch.setattr(backfill, "sleep_between_jobs", lambda: None)
    args = argparse.Namespace(csv=str(csv_path), jobs=str(jobs_path), done=str(done_path), verdicts="apply,maybe", all=False, limit=None, retry=False, headed=False, dump_html=None)

    assert cli.run_backfill(args) == 0
    out = capsys.readouterr()
    assert opened == ["1", "2"]
    assert "2 postings opened, 1 given a company page or contact, 0 left" in out.out
    rows = export.read_rows(csv_path)
    assert rows[0]["company_url"] == "https://www.linkedin.com/company/acme/" and rows[0]["contact_name"] == "Jane Doe" and rows[0]["contact_title"] == "Recruiter at Acme"
    assert rows[0]["status"] == "applied" and rows[0]["notes"] == "n"
    assert rows[1]["company_url"] == "" and rows[2]["company_url"] == ""
    assert load_jobs(jobs_path)[0].contact_url == "https://www.linkedin.com/in/jane-doe/"
    assert load_jobs(jobs_path)[0].score == 70
    assert set(json.loads(done_path.read_text())) == {"linkedin:1", "linkedin:2"}

    # A second run opens nothing: row 1 is filled, row 2 was opened before.
    assert cli.run_backfill(args) == 0
    assert opened == ["1", "2"]
    assert "nothing to open" in capsys.readouterr().out

    # --retry opens row 2 again.
    args.retry = True
    assert cli.run_backfill(args) == 0
    assert opened == ["1", "2", "2"]
    assert set(json.loads(done_path.read_text())) == {"linkedin:1", "linkedin:2"}


def test_run_backfill_stops_on_login_wall(tmp_path, monkeypatch, capsys):
    from jobs.browser import LoginRequired

    csv_path = tmp_path / "jobs.csv"
    export.write_rows(csv_path, [_row("linkedin:1"), _row("linkedin:2")])

    class FakeContext:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(context, source_id, dump_dir=None):
        raise LoginRequired("https://www.linkedin.com/login")

    monkeypatch.setattr("jobs.browser.persistent_context", lambda headless=True: FakeContext())
    monkeypatch.setattr(backfill, "open_job_page", fake_open)
    monkeypatch.setattr(backfill, "sleep_between_jobs", lambda: None)
    args = argparse.Namespace(csv=str(csv_path), jobs=str(tmp_path / "none.jsonl"), done=str(tmp_path / "done.json"), verdicts="apply", all=False, limit=None, retry=False, headed=False, dump_html=None)

    assert cli.run_backfill(args) == 2
    out = capsys.readouterr().out
    assert "login required" in out and "jobs login" in out
    assert export.read_rows(csv_path)[0]["company_url"] == ""
