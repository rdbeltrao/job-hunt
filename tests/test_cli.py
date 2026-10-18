import json

from jobs.cli import dedupe, format_table, write_jsonl
from jobs.models import Job


def _job(source="linkedin", source_id="1", **kw) -> Job:
    base = dict(title="EM", company="Acme", location="Berlin", workplace="remote", seniority="", posted_date="2026-10-16", url="u", description="d")
    base.update(kw)
    return Job(source=source, source_id=source_id, **base)


def test_dedupe_on_source_and_id():
    jobs = [_job("linkedin", "1"), _job("linkedin", "1", title="dup"), _job("personio", "1"), _job("linkedin", "2")]
    out = dedupe(jobs)
    assert [(j.source, j.source_id) for j in out] == [("linkedin", "1"), ("personio", "1"), ("linkedin", "2")]
    assert out[0].title == "EM"


def test_write_jsonl_roundtrip(tmp_path):
    path = tmp_path / "out" / "jobs.jsonl"
    write_jsonl([_job(salary="€100k", description="Über uns")], path)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["salary"] == "€100k"
    assert rows[0]["description"] == "Über uns"
    assert rows[0]["source_id"] == "1"


def test_format_table_truncates_long_values():
    table = format_table([_job(title="X" * 80)])
    lines = table.splitlines()
    assert lines[0].startswith("source")
    assert "…" in lines[2]
