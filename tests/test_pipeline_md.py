"""jobs/pipeline_md.py: the career-ops feeder (contract C6 and C7 of the consolidation plan)."""

from __future__ import annotations

import json

from jobs import pipeline_md
from jobs.models import Job


def _job(sid="4477094289", **kw) -> Job:
    base = dict(
        source="linkedin",
        title="Engineering Manager",
        company="Acme GmbH",
        location="Berlin, Germany",
        workplace="hybrid",
        seniority="Mid-Senior level",
        posted_date="2026-10-16",
        url=f"https://www.linkedin.com/jobs/view/{sid}/",
        description="Lead a team of eight.",
        extra={"search": "gerente-eng-germany-7d"},
    )
    base.update(kw)
    return Job(source_id=sid, **base)


def _greenhouse(**kw) -> Job:
    base = dict(
        source="greenhouse",
        source_id="792",
        title="Head of Engineering",
        company="Acme Corp",
        location="",
        workplace="remote",
        seniority="",
        posted_date="2026-10-20",
        url="https://boards.greenhouse.io/acme/jobs/792",
        description="Remote, EU.",
        salary="120000-150000 EUR",
    )
    base.update(kw)
    return Job(**base)


# ── sanitising ─────────────────────────────────────────────────────────


def test_sanitize_field_collapses_whitespace_and_escapes_markdown():
    assert pipeline_md.sanitize_field("  Senior \n\n EM\t(m/w/d)  ") == "Senior EM (m/w/d)"
    assert pipeline_md.sanitize_field("Head of | Platform") == "Head of / Platform"
    assert pipeline_md.sanitize_field("[Remote] Berlin") == "\\[Remote\\] Berlin"
    assert pipeline_md.sanitize_field(None) == ""


def test_sanitize_url_keeps_first_token_and_encodes_pipes():
    assert pipeline_md.sanitize_url(" https://x.example/a|b extra ") == "https://x.example/a%7Cb"
    assert pipeline_md.sanitize_url("") == ""


# ── row formatting (C7) ────────────────────────────────────────────────


def test_linkedin_job_renders_four_columns_with_posted_and_note():
    line = pipeline_md.format_pipeline_line(_job())
    assert line == (
        "- [ ] https://www.linkedin.com/jobs/view/4477094289/ | Acme GmbH | Engineering Manager"
        " | Berlin, Germany | posted: 2026-10-16 | note: linkedin gerente-eng-germany-7d"
    )


def test_search_name_argument_overrides_the_job_extra():
    line = pipeline_md.format_pipeline_line(_job(), search_name="em-berlin")
    assert line.endswith("| note: linkedin em-berlin")


def test_salary_without_location_keeps_an_empty_location_cell():
    line = pipeline_md.format_pipeline_line(_greenhouse())
    assert line == (
        "- [ ] https://boards.greenhouse.io/acme/jobs/792 | Acme Corp | Head of Engineering"
        " |  | 120000-150000 EUR | posted: 2026-10-20 | note: greenhouse"
    )


def test_no_salary_and_no_location_stops_after_title_and_posted_is_omitted_when_empty():
    line = pipeline_md.format_pipeline_line(_greenhouse(salary=None, posted_date=""))
    assert line == "- [ ] https://boards.greenhouse.io/acme/jobs/792 | Acme Corp | Head of Engineering | note: greenhouse"


def test_pipe_in_title_becomes_slash_and_url_pipe_is_percent_encoded():
    job = _job(title="EM | Platform", url="https://x.example/jobs?a=1|2")
    line = pipeline_md.format_pipeline_line(job)
    assert "| Acme GmbH | EM / Platform |" in line
    assert line.startswith("- [ ] https://x.example/jobs?a=1%7C2 |")


def test_newlines_in_fields_collapse_to_one_space():
    job = _job(company="Acme\nGmbH", location="Berlin,\r\n  Germany")
    line = pipeline_md.format_pipeline_line(job)
    assert "| Acme GmbH | Engineering Manager | Berlin, Germany |" in line


def test_jd_relative_path_follows_the_jd_store_layout():
    assert pipeline_md.jd_relative_path(_job()) == "jds/feeder/linkedin-4477094289.json"


# ── export (C6) ────────────────────────────────────────────────────────


def test_export_creates_pipeline_index_and_jd_files(tmp_path):
    root = tmp_path / "root"
    result = pipeline_md.export_to_data_root([_job(), _greenhouse()], root, today="2026-10-24")

    assert result == pipeline_md.ExportResult(added=2, skipped_existing=0, written_jds=2)
    pipeline = (root / "data" / "pipeline.md").read_text(encoding="utf-8")
    assert pipeline == (
        "# Pipeline\n\n## Pending\n\n"
        "- [ ] https://www.linkedin.com/jobs/view/4477094289/ | Acme GmbH | Engineering Manager"
        " | Berlin, Germany | posted: 2026-10-16 | note: linkedin gerente-eng-germany-7d\n"
        "- [ ] https://boards.greenhouse.io/acme/jobs/792 | Acme Corp | Head of Engineering"
        " |  | 120000-150000 EUR | posted: 2026-10-20 | note: greenhouse\n"
        "\n## Processed\n"
    )

    index = (root / "data" / "jd-index.tsv").read_text(encoding="utf-8").splitlines()
    assert index[0] == "url\tpath\tsource\tsource_id\tfirst_seen"
    assert index[1] == "https://www.linkedin.com/jobs/view/4477094289/\tjds/feeder/linkedin-4477094289.json\tlinkedin\t4477094289\t2026-10-24"
    assert index[2] == "https://boards.greenhouse.io/acme/jobs/792\tjds/feeder/greenhouse-792.json\tgreenhouse\t792\t2026-10-24"

    jd = json.loads((root / "jds" / "feeder" / "linkedin-4477094289.json").read_text(encoding="utf-8"))
    assert jd["url"] == "https://www.linkedin.com/jobs/view/4477094289/"
    assert jd["title"] == "Engineering Manager" and jd["description"] == "Lead a team of eight."
    assert jd["source"] == "linkedin" and jd["source_id"] == "4477094289"
    assert jd["first_seen"] == "2026-10-24" and jd["search_name"] == "gerente-eng-germany-7d"
    assert (root / "jds" / "feeder" / "greenhouse-792.json").exists()


def test_second_export_adds_nothing(tmp_path):
    root = tmp_path / "root"
    jobs = [_job(), _greenhouse()]
    pipeline_md.export_to_data_root(jobs, root, today="2026-10-24")
    before = {p: p.read_text(encoding="utf-8") for p in (root / "data").iterdir()}

    result = pipeline_md.export_to_data_root(jobs, root, today="2026-10-25")

    assert result == pipeline_md.ExportResult(added=0, skipped_existing=2, written_jds=0)
    assert {p: p.read_text(encoding="utf-8") for p in (root / "data").iterdir()} == before
    pipeline = (root / "data" / "pipeline.md").read_text(encoding="utf-8")
    assert pipeline.count("4477094289") == 1
    index = (root / "data" / "jd-index.tsv").read_text(encoding="utf-8").splitlines()
    assert sum(1 for line in index if "4477094289" in line) == 1


def test_duplicate_urls_inside_one_batch_are_written_once(tmp_path):
    root = tmp_path / "root"
    result = pipeline_md.export_to_data_root([_job(), _job()], root, today="2026-10-24")
    assert result == pipeline_md.ExportResult(added=1, skipped_existing=1, written_jds=1)


def test_existing_processed_row_with_the_same_url_blocks_re_adding(tmp_path):
    root = tmp_path / "root"
    data = root / "data"
    data.mkdir(parents=True)
    (data / "pipeline.md").write_text(
        "# Pipeline — Pending URLs\n\nPaste job URLs below.\n\n## Pending\n\n"
        "- [ ] https://jobs.example.com/posting/123\n\n"
        "## Processed\n"
        "- [x] #143 | https://www.linkedin.com/jobs/view/4477094289/ | Acme GmbH | EM | 4.2/5 | PDF ✅\n",
        encoding="utf-8",
    )

    result = pipeline_md.export_to_data_root([_job(), _greenhouse()], root, today="2026-10-24")

    assert result == pipeline_md.ExportResult(added=1, skipped_existing=1, written_jds=1)
    pipeline = (data / "pipeline.md").read_text(encoding="utf-8")
    assert pipeline == (
        "# Pipeline — Pending URLs\n\nPaste job URLs below.\n\n## Pending\n\n"
        "- [ ] https://jobs.example.com/posting/123\n"
        "- [ ] https://boards.greenhouse.io/acme/jobs/792 | Acme Corp | Head of Engineering"
        " |  | 120000-150000 EUR | posted: 2026-10-20 | note: greenhouse\n"
        "\n## Processed\n"
        "- [x] #143 | https://www.linkedin.com/jobs/view/4477094289/ | Acme GmbH | EM | 4.2/5 | PDF ✅\n"
    )
    assert not (root / "jds" / "feeder" / "linkedin-4477094289.json").exists()


def test_url_in_the_index_but_not_in_the_pipeline_is_not_re_added(tmp_path):
    root = tmp_path / "root"
    data = root / "data"
    data.mkdir(parents=True)
    (data / "jd-index.tsv").write_text(
        "url\tpath\tsource\tsource_id\tfirst_seen\n"
        "https://www.linkedin.com/jobs/view/4477094289/\tjds/feeder/linkedin-4477094289.json\tlinkedin\t4477094289\t2026-10-01\n",
        encoding="utf-8",
    )

    result = pipeline_md.export_to_data_root([_job()], root, today="2026-10-24")

    assert result == pipeline_md.ExportResult(added=0, skipped_existing=1, written_jds=0)
    assert not (data / "pipeline.md").exists()


def test_trailing_slash_difference_still_counts_as_the_same_url(tmp_path):
    root = tmp_path / "root"
    data = root / "data"
    data.mkdir(parents=True)
    (data / "pipeline.md").write_text("# Pipeline\n\n## Pending\n\n- [ ] https://www.linkedin.com/jobs/view/4477094289\n\n## Processed\n", encoding="utf-8")

    result = pipeline_md.export_to_data_root([_job()], root, today="2026-10-24")

    assert result.added == 0 and result.skipped_existing == 1


def test_pipeline_without_a_pending_section_gets_one_before_processed(tmp_path):
    root = tmp_path / "root"
    data = root / "data"
    data.mkdir(parents=True)
    (data / "pipeline.md").write_text("# Pipeline\n\n## Processed\n- [x] #1 | https://old.example/1 | Old | Role | 3/5 | PDF ❌\n", encoding="utf-8")

    pipeline_md.export_to_data_root([_greenhouse()], root, today="2026-10-24")

    pipeline = (data / "pipeline.md").read_text(encoding="utf-8")
    assert pipeline == (
        "# Pipeline\n\n## Pending\n\n"
        "- [ ] https://boards.greenhouse.io/acme/jobs/792 | Acme Corp | Head of Engineering"
        " |  | 120000-150000 EUR | posted: 2026-10-20 | note: greenhouse\n"
        "\n## Processed\n- [x] #1 | https://old.example/1 | Old | Role | 3/5 | PDF ❌\n"
    )


def test_localised_pending_heading_is_recognised(tmp_path):
    root = tmp_path / "root"
    data = root / "data"
    data.mkdir(parents=True)
    (data / "pipeline.md").write_text("# Pipeline\n\n## Pendentes\n\n## Processadas\n", encoding="utf-8")

    pipeline_md.export_to_data_root([_greenhouse()], root, today="2026-10-24")

    pipeline = (data / "pipeline.md").read_text(encoding="utf-8")
    assert pipeline.startswith("# Pipeline\n\n## Pendentes\n\n- [ ] https://boards.greenhouse.io/acme/jobs/792 |")
    assert pipeline.endswith("| note: greenhouse\n\n## Processadas\n")
    assert "## Pending" not in pipeline


def test_jobs_without_description_are_still_exported(tmp_path):
    root = tmp_path / "root"
    result = pipeline_md.export_to_data_root([_job(description="")], root, today="2026-10-24")
    assert result.added == 1 and result.written_jds == 1
    jd = json.loads((root / "jds" / "feeder" / "linkedin-4477094289.json").read_text(encoding="utf-8"))
    assert jd["description"] == ""


def test_first_seen_keeps_the_job_value_when_set(tmp_path):
    root = tmp_path / "root"
    pipeline_md.export_to_data_root([_job(first_seen="2026-10-10")], root, today="2026-10-24")
    index = (root / "data" / "jd-index.tsv").read_text(encoding="utf-8").splitlines()
    assert index[1].endswith("\t2026-10-10")
    jd = json.loads((root / "jds" / "feeder" / "linkedin-4477094289.json").read_text(encoding="utf-8"))
    assert jd["first_seen"] == "2026-10-10"


# ── Jev segment (C4) ───────────────────────────────────────────────────


def _scored(**kw) -> Job:
    scoring = {
        "model": "typesafe/jev-1.13",
        "seniority_match": "match",
        "location_ok": "yes",
        "language_blocker": "none",
        "team_type": "product",
        "stack_fit": "stack_agnostic",
        "confidences": {"seniority_match": 0.91},
    }
    scoring.update(kw.pop("scoring", {}))
    return _job(score=kw.pop("score", 79), verdict=kw.pop("verdict", "apply"), scoring=scoring, **kw)


def test_scored_job_carries_the_jev_segment_after_note():
    line = pipeline_md.format_pipeline_line(_scored())
    assert line == (
        "- [ ] https://www.linkedin.com/jobs/view/4477094289/ | Acme GmbH | Engineering Manager"
        " | Berlin, Germany | posted: 2026-10-16 | note: linkedin gerente-eng-germany-7d"
        " | jev: 79 apply — seniority match, location yes, language none, product, stack stack agnostic"
    )


def test_unscored_job_has_no_jev_segment():
    assert "jev:" not in pipeline_md.format_pipeline_line(_job())
    # `score` without a description: verdict maybe, no score, no segment.
    assert "jev:" not in pipeline_md.format_pipeline_line(_job(verdict="maybe", notes="no description"))
    # A verdict and score but reasons missing: no segment either.
    assert "jev:" not in pipeline_md.format_pipeline_line(_job(score=50, verdict="maybe", scoring={"model": "x"}))


def test_jev_segment_never_contains_a_pipe_and_is_capped():
    job = _scored(scoring={"team_type": "a|b [c]", "stack_fit": "x" * 300})
    segment = pipeline_md.format_jev_segment(job)
    assert segment.startswith("jev: 79 apply — seniority match, location yes, language none, a/b \\[c\\], stack ")
    assert "|" not in segment
    reasons = segment.split(" — ", 1)[1]
    assert len(reasons) <= 140 + reasons.count("\\")
    line = pipeline_md.format_pipeline_line(job)
    assert line.count("| jev: ") == 1 and line.endswith(segment)


def test_jev_segment_lands_in_the_exported_pipeline(tmp_path):
    root = tmp_path / "root"
    pipeline_md.export_to_data_root([_scored()], root, today="2026-10-24")
    pipeline = (root / "data" / "pipeline.md").read_text(encoding="utf-8")
    assert "| note: linkedin gerente-eng-germany-7d | jev: 79 apply — seniority match, location yes, language none, product, stack stack agnostic\n" in pipeline


def test_skip_verdict_rows_stay_out_unless_asked(tmp_path):
    skipped = _job(sid="900001", url="https://www.linkedin.com/jobs/view/900001/", score=12, verdict="skip")
    unscored = _job(sid="900002", url="https://www.linkedin.com/jobs/view/900002/")
    result = pipeline_md.export_to_data_root([skipped, unscored], tmp_path, "2026-10-24")
    assert result.added == 1
    assert result.skipped_verdict == 1
    text = (tmp_path / "data" / "pipeline.md").read_text(encoding="utf-8")
    assert "900001" not in text

    result = pipeline_md.export_to_data_root([skipped], tmp_path, "2026-10-24", include_skips=True)
    assert result.added == 1
    assert result.skipped_verdict == 0
    assert "900001" in (tmp_path / "data" / "pipeline.md").read_text(encoding="utf-8")
