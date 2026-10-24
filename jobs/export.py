"""CSV export: the user's working list of openings.

`status` and `notes` are edited by hand in Numbers, Excel or Google Sheets (File > Import),
so rows that already exist are never rewritten. Only unseen job_ids are appended. The file
is UTF-8 with a BOM so Excel detects the encoding; descriptions keep their newlines inside
quoted cells.
"""

from __future__ import annotations

import csv
import logging
from datetime import date
from pathlib import Path

from jobs.models import Job

log = logging.getLogger(__name__)

DEFAULT_PATH = Path("out/jobs.csv")
ENCODING = "utf-8-sig"
HEADER = [
    "job_id",
    "first_seen",
    "posted_date",
    "search_name",
    "source",
    "title",
    "company",
    "location",
    "workplace",
    "seniority",
    "salary",
    "url",
    "company_url",
    "contact_name",
    "contact_title",
    "contact_url",
    "description",
    "score",
    "verdict",
    "seniority_match",
    "location_ok",
    "language_blocker",
    "team_type",
    "stack_fit",
    "strong_matches",
    "gaps",
    "red_flags",
    "pitch",
    "status",
    "notes",
]
DEFAULT_STATUS = "new"
# The application pipeline, in the order it usually moves. `jobs status --set` accepts these.
STATUSES = ("new", "skipped", "draft", "applied", "replied", "interview", "offer", "rejected", "withdrawn")
REASON_COLUMNS = ("seniority_match", "location_ok", "language_blocker", "team_type", "stack_fit")
# LinkedIn company page and the hiring-team contact who can be messaged; empty for other sources.
LINKEDIN_COLUMNS = ("company_url", "contact_name", "contact_title", "contact_url")
# Headers this file used before; a file with one of them is rewritten to HEADER on first read.
LEGACY_HEADERS = [
    [c for c in HEADER if c not in REASON_COLUMNS and c not in LINKEDIN_COLUMNS],
    [c for c in HEADER if c not in LINKEDIN_COLUMNS],
]


class HeaderMismatch(Exception):
    """The CSV on disk has a header that differs from HEADER; refusing to append to it."""


def migrate(path: Path) -> bool:
    """Rewrite a file with a legacy header to HEADER, new columns empty. Returns True when it did."""
    if not path.exists():
        return False
    with path.open(newline="", encoding=ENCODING) as fh:
        reader = csv.reader(fh)
        header = next(reader, [])
        if header not in LEGACY_HEADERS:
            return False
        old_rows = [dict(zip(header, row + [""] * (len(header) - len(row)))) for row in reader if row]
    write_rows(path, [{col: row.get(col, "") for col in HEADER} for row in old_rows])
    log.info("csv: migrated %s to the current %d-column header", path, len(HEADER))
    print(f"csv: {path} migrated to the current columns ({', '.join(c for c in HEADER if c not in header)} added)")
    return True


def read_existing(path: Path) -> tuple[bool, set[str]]:
    """(has_header, job_ids) for the file on disk. Raises HeaderMismatch when the header is not ours."""
    if not path.exists():
        return False, set()
    migrate(path)
    with path.open(newline="", encoding=ENCODING) as fh:
        reader = csv.reader(fh)
        header = next(reader, [])
        if not header:
            return False, set()
        if header != HEADER:
            raise HeaderMismatch(f"{path} has columns {header[:5]}..., expected {HEADER[:5]}...")
        return True, {row[0] for row in reader if row}


def job_to_row(job: Job, today: str | None = None) -> list:
    today = today or date.today().isoformat()
    first_seen = job.first_seen or today
    return [
        job.job_id,
        first_seen,
        job.posted_date,
        job.extra.get("search", ""),
        job.source,
        job.title,
        job.company,
        job.location,
        job.workplace,
        job.seniority,
        job.salary or "",
        job.url,
        job.company_url or "",
        job.contact_name or "",
        job.contact_title or "",
        job.contact_url or "",
        job.description or "",
        "" if job.score is None else job.score,
        job.verdict,
        *[str(job.scoring.get(col, "") or "") for col in REASON_COLUMNS],
        job.strong_matches or "",
        job.gaps or "",
        job.red_flags or "",
        job.pitch or "",
        DEFAULT_STATUS,
        job.notes or "",
    ]


def export_jobs(path: Path, jobs: list[Job], today: str | None = None) -> tuple[int, int]:
    """Append rows for jobs whose job_id is not in the file yet. Returns (appended, skipped)."""
    has_header, existing = read_existing(path)
    new_rows: list[list] = []
    skipped = 0
    seen_in_batch: set[str] = set()
    for job in jobs:
        if job.job_id in existing or job.job_id in seen_in_batch:
            skipped += 1
            continue
        seen_in_batch.add(job.job_id)
        new_rows.append(job_to_row(job, today))
    write_header = not has_header
    if new_rows or write_header:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", newline="", encoding=ENCODING) as fh:
            writer = csv.writer(fh)
            if write_header:
                writer.writerow(HEADER)
            writer.writerows(new_rows)
    return len(new_rows), skipped


SCORE_COLUMNS = ("score", "verdict", *REASON_COLUMNS, "strong_matches", "gaps", "red_flags", "pitch")


def fill_linkedin_columns(path: Path, jobs: list[Job]) -> int:
    """Copy company_url and the contact into rows that have none yet, from jobs that do. Returns rows changed.

    Rows are matched on job_id; a row that already carries any of the four values is
    left alone, as is every other column. The file is rewritten only when a row changed.
    """
    by_id = {job.job_id: job for job in jobs if any(getattr(job, col) for col in LINKEDIN_COLUMNS)}
    if not by_id:
        return 0
    rows = read_rows(path)
    changed = 0
    for row in rows:
        job = by_id.get(row["job_id"])
        if job is None or any((row.get(col) or "").strip() for col in LINKEDIN_COLUMNS):
            continue
        for col in LINKEDIN_COLUMNS:
            row[col] = getattr(job, col) or ""
        changed += 1
    if changed:
        write_rows(path, rows)
    return changed


def read_rows(path: Path) -> list[dict[str, str]]:
    """All rows as dicts keyed by HEADER. Raises HeaderMismatch when the header is not ours."""
    if not path.exists():
        return []
    migrate(path)
    with path.open(newline="", encoding=ENCODING) as fh:
        reader = csv.reader(fh)
        header = next(reader, [])
        if not header:
            return []
        if header != HEADER:
            raise HeaderMismatch(f"{path} has columns {header[:5]}..., expected {HEADER[:5]}...")
        return [dict(zip(HEADER, row + [""] * (len(HEADER) - len(row)))) for row in reader if row]


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    """Rewrite the whole file, same header and row order. Used by rescore; export only appends."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding=ENCODING) as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        writer.writerows([[row.get(col, "") for col in HEADER] for row in rows])


def row_to_job(row: dict[str, str]) -> Job:
    """A Job built from a CSV row, with score and verdict cleared so it can be scored again; explanations stay."""
    source, _, source_id = row["job_id"].partition(":")
    return Job(
        source=source,
        source_id=source_id,
        title=row["title"],
        company=row["company"],
        location=row["location"],
        workplace=row["workplace"] or "unknown",  # type: ignore[arg-type]
        seniority=row["seniority"],
        posted_date=row["posted_date"],
        url=row["url"],
        description=row["description"],
        salary=row["salary"] or None,
        company_url=row.get("company_url", ""),
        contact_name=row.get("contact_name", ""),
        contact_title=row.get("contact_title", ""),
        contact_url=row.get("contact_url", ""),
        extra={"search": row["search_name"]},
        first_seen=row["first_seen"],
        # Explanations are kept: a rescore changes the score and reasons, not the write-ups.
        strong_matches=row["strong_matches"],
        gaps=row["gaps"],
        red_flags=row["red_flags"],
        pitch=row["pitch"],
        notes=row["notes"],
    )


def set_status(row: dict[str, str], status: str, note: str = "", today: str | None = None) -> None:
    """Change a row's status and append a dated line to its notes, keeping the earlier notes."""
    today = today or date.today().isoformat()
    row["status"] = status
    entry = f"{today} {status}" + (f": {note}" if note else "")
    row["notes"] = f"{row['notes']}\n{entry}" if row.get("notes") else entry


def apply_scores(row: dict[str, str], job: Job) -> None:
    """Copy the score columns from a scored Job into its CSV row; status and notes stay as the user left them."""
    row["score"] = "" if job.score is None else str(job.score)
    row["verdict"] = job.verdict
    for col in REASON_COLUMNS:
        row[col] = str(job.scoring.get(col, "") or "")
    row["strong_matches"] = job.strong_matches or ""
    row["gaps"] = job.gaps or ""
    row["red_flags"] = job.red_flags or ""
    row["pitch"] = job.pitch or ""
