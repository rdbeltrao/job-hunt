import argparse
import json
from pathlib import Path

from jobs import cli
from jobs.models import Job
from jobs.store import load_jobs, save_jobs


def _job(sid="1", **kw) -> Job:
    base = dict(source="linkedin", title="EM", company="Acme", location="Berlin", workplace="remote", seniority="", posted_date="2026-10-16", url="u", description="d")
    base.update(kw)
    return Job(source_id=sid, **base)


def test_store_roundtrip_keeps_scoring_fields(tmp_path):
    path = tmp_path / "jobs.jsonl"
    save_jobs([_job(score=70, verdict="apply", pitch="hi", scoring={"a": 1})], path)
    loaded = load_jobs(path)
    assert loaded[0].score == 70 and loaded[0].verdict == "apply" and loaded[0].scoring == {"a": 1}


def test_from_dict_ignores_unknown_keys():
    job = Job.from_dict({**_job().to_dict(), "future_field": 1})
    assert job.source_id == "1"


def test_carry_over_scores_on_refetch():
    previous = [_job("1", score=80, verdict="apply", pitch="p"), _job("2")]
    fresh = [_job("1", title="new title"), _job("2"), _job("3")]
    assert cli.carry_over_scores(fresh, previous) == 1
    assert fresh[0].score == 80 and fresh[0].pitch == "p" and fresh[0].title == "new title"
    assert fresh[1].score is None


def test_score_skips_clearly_without_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY_JOB_SEARCH", raising=False)
    jobs_path = tmp_path / "jobs.jsonl"
    save_jobs([_job()], jobs_path)
    profile = tmp_path / "profile.md"
    profile.write_text("EM, Berlin")
    args = argparse.Namespace(jobs=str(jobs_path), profile=str(profile), explain=False)

    assert cli.run_score(args) == 1
    assert "OPENROUTER_API_KEY is not set" in capsys.readouterr().out


def test_score_fails_clearly_without_profile(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    jobs_path = tmp_path / "jobs.jsonl"
    save_jobs([_job()], jobs_path)
    args = argparse.Namespace(jobs=str(jobs_path), profile=str(tmp_path / "profile.md"), explain=False)

    assert cli.run_score(args) == 1
    assert "profile.md not found" in capsys.readouterr().out


def test_score_marks_no_description_without_calling_jev(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    jobs_path = tmp_path / "jobs.jsonl"
    save_jobs([_job(description="")], jobs_path)
    profile = tmp_path / "profile.md"
    profile.write_text("EM")
    args = argparse.Namespace(jobs=str(jobs_path), profile=str(profile), explain=False)

    assert cli.run_score(args) == 0
    job = load_jobs(jobs_path)[0]
    assert job.verdict == "maybe" and job.score is None and job.notes == "no description"
    assert "1 without description" in capsys.readouterr().out


def test_export_writes_csv_and_skips_known_rows(tmp_path, capsys):
    jobs_path = tmp_path / "jobs.jsonl"
    save_jobs([_job()], jobs_path)
    csv_path = tmp_path / "jobs.csv"
    args = argparse.Namespace(jobs=str(jobs_path), csv=str(csv_path))

    assert cli.run_export(args) == 0
    assert "1 rows appended, 0 skipped" in capsys.readouterr().out
    assert cli.run_export(args) == 0
    assert "0 rows appended, 1 skipped" in capsys.readouterr().out
    assert csv_path.read_text(encoding="utf-8-sig").startswith("job_id,first_seen,")


def test_export_refuses_foreign_csv(tmp_path, capsys):
    jobs_path = tmp_path / "jobs.jsonl"
    save_jobs([_job()], jobs_path)
    csv_path = tmp_path / "jobs.csv"
    csv_path.write_text("a,b\n1,2\n")

    assert cli.run_export(argparse.Namespace(jobs=str(jobs_path), csv=str(csv_path))) == 1
    assert "export: refused" in capsys.readouterr().out
    assert csv_path.read_text() == "a,b\n1,2\n"


def test_run_all_continues_after_failures(monkeypatch, capsys):
    calls: list[str] = []

    def fake_fetch(args):
        calls.append("fetch")
        raise RuntimeError("browser exploded")

    def fake_score(args):
        calls.append("score")
        return 1

    def fake_export(args):
        calls.append("export")
        return 0

    monkeypatch.setattr(cli, "run_fetch", fake_fetch)
    monkeypatch.setattr(cli, "run_score", fake_score)
    monkeypatch.setattr(cli, "run_export", fake_export)

    assert cli.run_all(argparse.Namespace()) == 1
    assert calls == ["fetch", "score", "export"]
    out = capsys.readouterr().out
    assert "fetch: crashed: RuntimeError: browser exploded" in out
    assert "export: ok" in out


def test_score_falls_back_to_llm_when_jev_provider_blocked(tmp_path, monkeypatch, capsys):
    from jobs.scoring import jev, llm

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("JOB_HUNT_SCORER", raising=False)
    jobs_path = tmp_path / "jobs.jsonl"
    save_jobs([_job("1"), _job("2")], jobs_path)
    profile = tmp_path / "profile.md"
    profile.write_text("EM")

    blocked = jev.JevError('HTTP 404 from Jev: {"error":{"message":"No allowed providers are available for the selected model."}}')
    jev_calls: list[str] = []
    llm_calls: list[str] = []

    def jev_score(self, profile_text, job):
        jev_calls.append(job.job_id)
        raise blocked

    def llm_score(self, profile_text, job):
        llm_calls.append(job.job_id)
        return jev.JevResult(fit=70, verdict="apply", seniority_match="match", location_ok="yes", language_blocker="none")

    monkeypatch.setattr(jev.JevClient, "score_job", jev_score)
    monkeypatch.setattr(llm.LlmScorer, "score_job", llm_score)

    args = argparse.Namespace(jobs=str(jobs_path), profile=str(profile), explain=False)
    assert cli.run_score(args) == 0
    out = capsys.readouterr().out
    assert jev_calls == ["linkedin:1"]
    assert llm_calls == ["linkedin:1", "linkedin:2"]
    assert "falling back to" in out
    assert "score (llm): 2 scored" in out
    assert all(j.score == 70 for j in load_jobs(jobs_path))


def test_rescore_updates_score_columns_and_keeps_status(tmp_path, monkeypatch, capsys):
    from jobs import export
    from jobs.scoring import jev

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("JOB_HUNT_SCORER", raising=False)
    csv_path = tmp_path / "jobs.csv"
    old = _job("1", score=30, verdict="skip")
    export.export_jobs(csv_path, [old, _job("2", score=10, verdict="skip")])
    rows = export.read_rows(csv_path)
    rows[0]["status"] = "applied"
    export.write_rows(csv_path, rows)
    profile = tmp_path / "profile.md"
    profile.write_text("EM")

    monkeypatch.setattr(
        jev.JevClient,
        "score_job",
        lambda self, p, job: jev.JevResult(fit=90, verdict="apply", seniority_match="match", location_ok="yes", language_blocker="none"),
    )
    args = argparse.Namespace(csv=str(csv_path), profile=str(profile), explain=False, status=None, job_ids=["linkedin:1"])
    assert cli.run_rescore(args) == 0
    assert "1 of 2 rows selected" in capsys.readouterr().out

    rows = export.read_rows(csv_path)
    assert rows[0]["score"] == "90" and rows[0]["verdict"] == "apply" and rows[0]["status"] == "applied"
    assert rows[1]["score"] == "10" and rows[1]["verdict"] == "skip"


def test_explain_list_and_apply_round_trip(tmp_path, capsys):
    import json

    from jobs import export

    csv_path = tmp_path / "jobs.csv"
    export.export_jobs(csv_path, [_job("1", score=80, verdict="apply"), _job("2", score=40, verdict="maybe"), _job("3", score=70, verdict="apply", pitch="done")])
    profile = tmp_path / "profile.md"
    profile.write_text("EM in Berlin")
    out = tmp_path / "pending.json"

    args = argparse.Namespace(csv=str(csv_path), profile=str(profile), min_fit=None, out=str(out), list=True, apply=None)
    assert cli.run_explain(args) == 0
    payload = json.loads(out.read_text())
    assert [j["job_id"] for j in payload["jobs"]] == ["linkedin:1"]
    assert payload["profile"] == "EM in Berlin" and "pitch" in payload["fields"]
    assert "1 jobs with fit >= 60" in capsys.readouterr().out

    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"jobs": [{"job_id": "linkedin:1", "strong_matches": "a; b", "gaps": "c", "red_flags": "", "pitch": "Hi"}, {"job_id": "nope:9", "pitch": "x"}]}))
    args = argparse.Namespace(csv=str(csv_path), profile=str(profile), min_fit=None, out=None, list=False, apply=str(answers))
    assert cli.run_explain(args) == 0
    assert "1 rows updated" in capsys.readouterr().out and "1 job_ids not found"
    rows = {r["job_id"]: r for r in export.read_rows(csv_path)}
    assert rows["linkedin:1"]["strong_matches"] == "a; b" and rows["linkedin:1"]["pitch"] == "Hi"
    assert rows["linkedin:3"]["pitch"] == "done"


def test_status_sets_rows_and_keeps_notes_history(tmp_path, capsys):
    from jobs import export

    csv_path = tmp_path / "jobs.csv"
    export.export_jobs(csv_path, [_job("1", score=80, verdict="apply"), _job("2")])
    rows = export.read_rows(csv_path)
    rows[0]["notes"] = "cover letter sent"
    export.write_rows(csv_path, rows)

    args = argparse.Namespace(csv=str(csv_path), set="applied", note="Easy Apply", date="2026-10-19", job_ids=["linkedin:1"])
    assert cli.run_status(args) == 0
    rows = {r["job_id"]: r for r in export.read_rows(csv_path)}
    assert rows["linkedin:1"]["status"] == "applied"
    assert rows["linkedin:1"]["notes"] == "cover letter sent\n2026-10-19 applied: Easy Apply"
    assert rows["linkedin:2"]["status"] == "new"

    args = argparse.Namespace(csv=str(csv_path), set=None, note=None, date=None, job_ids=[])
    assert cli.run_status(args) == 0
    out = capsys.readouterr().out
    assert "applied 1" in out and "new 1" in out and "linkedin:1" in out

    args = argparse.Namespace(csv=str(csv_path), set="bogus", note=None, date=None, job_ids=["linkedin:1"])
    assert cli.run_status(args) == 1
    args = argparse.Namespace(csv=str(csv_path), set="applied", note=None, date=None, job_ids=["linkedin:9"])
    assert cli.run_status(args) == 1
    assert "not in" in capsys.readouterr().out


def test_pipeline_command_writes_the_career_ops_files(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("JOB_HUNT_DATA_ROOT", raising=False)
    jobs_path = tmp_path / "jobs.jsonl"
    save_jobs([_job(url="https://www.linkedin.com/jobs/view/1/", posted_date="2026-10-16", extra={"search": "em-berlin"})], jobs_path)
    root = tmp_path / "root"

    assert cli.main(["pipeline", "--jobs", str(jobs_path), "--data-root", str(root), "--date", "2026-10-24"]) == 0
    out = capsys.readouterr().out
    assert "pipeline: 1 rows added" in out and "0 already there" in out and "1 JD files written" in out
    pipeline = (root / "data" / "pipeline.md").read_text(encoding="utf-8")
    assert "- [ ] https://www.linkedin.com/jobs/view/1/ | Acme | EM | Berlin | posted: 2026-10-16 | note: linkedin em-berlin\n" in pipeline
    assert (root / "data" / "jd-index.tsv").exists()
    assert (root / "jds" / "feeder" / "linkedin-1.json").exists()

    assert cli.main(["pipeline", "--jobs", str(jobs_path), "--data-root", str(root)]) == 0
    assert "pipeline: 0 rows added" in capsys.readouterr().out


def test_pipeline_command_reads_the_data_root_from_the_environment(tmp_path, monkeypatch, capsys):
    jobs_path = tmp_path / "jobs.jsonl"
    save_jobs([_job(url="https://www.linkedin.com/jobs/view/1/")], jobs_path)
    root = tmp_path / "root"
    monkeypatch.setenv("JOB_HUNT_DATA_ROOT", str(root))

    assert cli.main(["pipeline", "--jobs", str(jobs_path)]) == 0
    assert (root / "data" / "pipeline.md").exists()


def test_pipeline_command_fails_clearly_without_a_data_root(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("JOB_HUNT_DATA_ROOT", raising=False)
    jobs_path = tmp_path / "jobs.jsonl"
    save_jobs([_job()], jobs_path)

    assert cli.main(["pipeline", "--jobs", str(jobs_path)]) == 1
    assert "JOB_HUNT_DATA_ROOT" in capsys.readouterr().out


def test_run_all_feeds_career_ops_after_scoring_when_a_data_root_is_given(monkeypatch, capsys):
    calls: list[str] = []
    for name in ("fetch", "pipeline", "score", "export"):
        monkeypatch.setattr(cli, f"run_{name}", lambda args, name=name: calls.append(name) or 0)

    assert cli.run_all(argparse.Namespace(data_root="/tmp/root")) == 0
    # Scoring first, so every exported row carries its verdict and skips stay out.
    assert calls == ["fetch", "score", "pipeline", "export"]
    assert "pipeline: ok" in capsys.readouterr().out
