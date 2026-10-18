import csv

import pytest

from jobs import export
from jobs.models import Job


def _job(source_id: str, **kw) -> Job:
    base = dict(
        source="linkedin",
        title="EM",
        company="Acme",
        location="Berlin",
        workplace="remote",
        seniority="",
        posted_date="2026-10-16",
        url="https://x/" + source_id,
        description="line one\nline two, with comma",
    )
    base.update(kw)
    return Job(source_id=source_id, **base)


def _read(path):
    with path.open(newline="", encoding=export.ENCODING) as fh:
        return list(csv.reader(fh))


def test_creates_file_with_header_and_rows(tmp_path):
    path = tmp_path / "out" / "jobs.csv"
    appended, skipped = export.export_jobs(path, [_job("1"), _job("2")], today="2026-10-16")

    rows = _read(path)
    assert (appended, skipped) == (2, 0)
    assert rows[0] == export.HEADER
    assert rows[1][0] == "linkedin:1"
    assert rows[1][1] == "2026-10-16"
    assert rows[1][export.HEADER.index("status")] == "new"
    assert rows[1][export.HEADER.index("description")] == "line one\nline two, with comma"


def test_second_run_appends_only_new_rows_and_keeps_edits(tmp_path):
    path = tmp_path / "jobs.csv"
    export.export_jobs(path, [_job("1")], today="2026-10-16")

    # The user edits status and notes by hand between runs.
    rows = _read(path)
    rows[1][export.HEADER.index("status")] = "applied"
    rows[1][export.HEADER.index("notes")] = "spoke to recruiter"
    with path.open("w", newline="", encoding=export.ENCODING) as fh:
        csv.writer(fh).writerows(rows)

    appended, skipped = export.export_jobs(path, [_job("1"), _job("2")], today="2026-10-17")

    rows = _read(path)
    assert (appended, skipped) == (1, 1)
    assert len(rows) == 3
    assert rows[1][export.HEADER.index("status")] == "applied"
    assert rows[1][export.HEADER.index("notes")] == "spoke to recruiter"
    assert rows[2][0] == "linkedin:2"


def test_duplicate_ids_within_one_batch_written_once(tmp_path):
    path = tmp_path / "jobs.csv"
    appended, skipped = export.export_jobs(path, [_job("1"), _job("1")])
    assert (appended, skipped) == (1, 1)
    assert len(_read(path)) == 2


def test_score_and_explanations_land_in_their_columns(tmp_path):
    path = tmp_path / "jobs.csv"
    job = _job("1", score=82, verdict="apply", gaps="no fintech", pitch="I led...")
    export.export_jobs(path, [job])
    row = _read(path)[1]
    assert row[export.HEADER.index("score")] == "82"
    assert row[export.HEADER.index("verdict")] == "apply"
    assert row[export.HEADER.index("gaps")] == "no fintech"
    assert row[export.HEADER.index("pitch")] == "I led..."


def test_foreign_header_is_refused(tmp_path):
    path = tmp_path / "jobs.csv"
    path.write_text("a,b,c\n1,2,3\n", encoding=export.ENCODING)
    with pytest.raises(export.HeaderMismatch):
        export.export_jobs(path, [_job("1")])
    assert path.read_text(encoding=export.ENCODING) == "a,b,c\n1,2,3\n"


def test_empty_file_gets_header(tmp_path):
    path = tmp_path / "jobs.csv"
    path.write_text("", encoding=export.ENCODING)
    export.export_jobs(path, [])
    assert _read(path) == [export.HEADER]


def test_rows_round_trip_and_rescore_helpers(tmp_path):
    path = tmp_path / "jobs.csv"
    job = _job("1", score=40, verdict="maybe", gaps="old gap")
    export.export_jobs(path, [job], today="2026-10-16")
    rows = export.read_rows(path)
    rows[0]["status"] = "applied"
    rows[0]["notes"] = "called them"

    rebuilt = export.row_to_job(rows[0])
    assert rebuilt.job_id == "linkedin:1"
    assert rebuilt.description == job.description
    assert rebuilt.score is None and rebuilt.verdict == ""  # ready to be scored again
    assert rebuilt.gaps == "old gap"  # explanations survive a rescore
    assert rebuilt.first_seen == "2026-10-16"

    rebuilt.score, rebuilt.verdict, rebuilt.gaps, rebuilt.pitch = 85, "apply", "", "Hi"
    export.apply_scores(rows[0], rebuilt)
    export.write_rows(path, rows)

    again = export.read_rows(path)
    assert again[0]["score"] == "85" and again[0]["verdict"] == "apply"
    assert again[0]["gaps"] == "" and again[0]["pitch"] == "Hi"
    assert again[0]["status"] == "applied" and again[0]["notes"] == "called them"
    assert _read(path)[0] == export.HEADER


def test_legacy_csv_is_migrated_once(tmp_path, capsys):
    path = tmp_path / "jobs.csv"
    legacy = [c for c in export.HEADER if c not in export.REASON_COLUMNS]
    with path.open("w", newline="", encoding=export.ENCODING) as fh:
        w = csv.writer(fh)
        w.writerow(legacy)
        w.writerow(["linkedin:1", "2026-10-16", "", "s", "linkedin", "EM", "Acme", "Berlin", "remote", "", "", "u", "d", "70", "apply", "m", "g", "", "p", "applied", "n"])
    rows = export.read_rows(path)
    assert "migrated" in capsys.readouterr().out
    assert _read(path)[0] == export.HEADER
    assert rows[0]["job_id"] == "linkedin:1" and rows[0]["status"] == "applied" and rows[0]["pitch"] == "p"
    assert rows[0]["team_type"] == "" and rows[0]["stack_fit"] == ""
    export.read_rows(path)
    assert "migrated" not in capsys.readouterr().out


def test_reason_columns_come_from_scoring(tmp_path):
    path = tmp_path / "jobs.csv"
    job = _job("1", score=55, verdict="maybe")
    job.scoring = {"seniority_match": "match", "location_ok": "yes", "language_blocker": "none", "team_type": "platform_infra", "stack_fit": "required_mismatch"}
    export.export_jobs(path, [job])
    row = export.read_rows(path)[0]
    assert row["team_type"] == "platform_infra" and row["stack_fit"] == "required_mismatch" and row["location_ok"] == "yes"
