"""Feed new openings into a career-ops data root (the `jobs pipeline` command).

career-ops (https://github.com/rdbeltrao/career-ops) keeps its inbox in
`data/pipeline.md` and reads job descriptions through `data/jd-index.tsv`, which
maps a URL to a JSON file under `jds/`. This module writes both, plus one JD file
per job, following two contracts from the consolidation plan:

- C6, the JD store: `jds/feeder/{source}-{source_id}.json` holding the job object
  plus `source`, `source_id`, `first_seen` and `search_name`; `data/jd-index.tsv`
  with the header `url\tpath\tsource\tsource_id\tfirst_seen`, append-only, one
  row per URL, `path` relative to the data root.
- C7, the pipeline row written under `## Pending`:
  `- [ ] {url} | {company} | {title} | {location} | {salary} | posted: {date} | note: {source} {search_name}`
  The positional cells stop after `{location}` when there is no salary; a salary
  with no location keeps an empty location cell. `posted:` is omitted when the
  posting date is unknown; `note:` is always there.
- C4, the Jev segment: a job `jobs score` already scored also carries
  `| jev: {score} {verdict} — {reasons}` after `note:`, so `jev-rank.mjs` keeps the
  row instead of scoring it again. `reasons` is `seniority {v}, location {v},
  language {v}, {team_type}, stack {v}`, underscores as spaces, at most 140
  characters. An unscored job gets no segment.

Field sanitising mirrors `sanitizeMarkdownField` and `sanitizePipelineUrl` in
career-ops `scan.mjs`: whitespace runs and line breaks collapse to one space, `|`
becomes `/` in a cell and `%7C` in the URL, and `\\`, `[` and `]` are escaped with a
backslash so a cell can never open a Markdown link or a new column.

Every export is idempotent: a URL already present in `data/pipeline.md` (any
checkbox row, in whatever section) or in `data/jd-index.tsv` is skipped, so a
second run over the same `out/jobs.jsonl` adds no row, no index line and no file.
Only the standard library is used here.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from jobs.models import Job

PIPELINE_RELATIVE = Path("data") / "pipeline.md"
INDEX_RELATIVE = Path("data") / "jd-index.tsv"
JD_DIR_RELATIVE = Path("jds") / "feeder"
INDEX_HEADER = "url\tpath\tsource\tsource_id\tfirst_seen"
PIPELINE_SKELETON = "# Pipeline\n\n## Pending\n\n## Processed\n"

# The Pending/Processed headings every `modes/<lang>/pipeline.md` of career-ops
# writes (lib/pipeline-sections.mjs). Read flexibly, write in the file's own language.
SECTION_PAIRS = [
    ("Pending", "Processed"),
    ("Pendientes", "Procesadas"),
    ("Pendentes", "Processadas"),
    ("Offen", "Verarbeitet"),
    ("En attente", "Traitees"),
    ("In attesa", "Elaborati"),
    ("In afwachting", "Verwerkt"),
    ("Afventer", "Behandlede"),
    ("Oczekujące", "Przetworzone"),
    ("Bekleyenler", "İşlenenler"),
    ("Menunggu", "Diproses"),
    ("Ожидающие", "Обработанные"),
    ("Очікуючі", "Оброблені"),
    ("लंबित", "संसाधित"),
    ("대기", "처리 완료"),
]

_HEADING_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*$")
_CHECKBOX_RE = re.compile(r"^\s*- \[[ x!]\]\s+")
_STRIKETHROUGH_RE = re.compile(r"^~~([\s\S]*?)~~")
_URL_RE = re.compile(r"https?://[^\s|]+")
_POSTED_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
_MARKDOWN_ESCAPES = {"\\": "\\\\", "[": "\\[", "]": "\\]"}
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")
# The five reason answers jobs/scoring/criteria.py asks for, in the order the segment lists them.
JEV_REASON_KEYS = ("seniority_match", "location_ok", "language_blocker", "team_type", "stack_fit")
JEV_REASON_LABELS = {"seniority_match": "seniority", "location_ok": "location", "language_blocker": "language", "team_type": "", "stack_fit": "stack"}
JEV_REASONS_MAX_CHARS = 140


def _fold(text: str) -> str:
    """Accent- and case-insensitive key, as lib/pipeline-sections.mjs folds headings."""
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower().strip()


_SECTION_KIND = {}
for _pending, _processed in SECTION_PAIRS:
    _SECTION_KIND[_fold(_pending)] = ("pending", _processed)
    _SECTION_KIND[_fold(_processed)] = ("processed", None)


def section_kind(line: str) -> str | None:
    """'pending' or 'processed' when the line is one of the known `## ` headings, else None."""
    m = _HEADING_RE.match(line.rstrip("\r"))
    if not m:
        return None
    entry = _SECTION_KIND.get(_fold(m.group(1)))
    return entry[0] if entry else None


def _processed_heading_for(pending_line: str) -> str:
    m = _HEADING_RE.match(pending_line.rstrip("\r"))
    entry = _SECTION_KIND.get(_fold(m.group(1))) if m else None
    return f"## {entry[1] if entry and entry[1] else 'Processed'}"


# ── sanitising ─────────────────────────────────────────────────────────


def _normalize_scalar(value: object) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


def _escape_markdown(text: str) -> str:
    return "".join(_MARKDOWN_ESCAPES.get(ch, ch) for ch in text)


def sanitize_field(value: object) -> str:
    """One pipeline cell: whitespace collapsed, `\\` `[` `]` escaped, `|` turned into `/`."""
    return _escape_markdown(_normalize_scalar(value)).replace("|", "/")


def sanitize_url(value: object) -> str:
    """The URL cell: first whitespace-free token, `\\` `[` `]` escaped, `|` as `%7C`."""
    text = "" if value is None else str(value)
    first = text.strip().split()
    return _escape_markdown(first[0]).replace("|", "%7C") if first else ""


# ── rows and paths ─────────────────────────────────────────────────────


def _search_name(job: Job, search_name: str = "") -> str:
    return search_name or str(job.extra.get("search", "") or "")


def format_pipeline_line(job: Job, search_name: str = "") -> str:
    """The `- [ ]` row for one job (contract C7). `search_name` defaults to `job.extra["search"]`."""
    url = sanitize_url(job.url)
    company = sanitize_field(job.company)
    title = sanitize_field(job.title)
    location = sanitize_field(job.location) if isinstance(job.location, str) else ""
    salary = sanitize_field(job.salary) if isinstance(job.salary, str) else ""
    line = f"- [ ] {url} | {company} | {title}"
    if salary:
        line = f"{line} | {location} | {salary}"
    elif location:
        line = f"{line} | {location}"
    posted = _POSTED_RE.match(_normalize_scalar(job.posted_date))
    if posted:
        line = f"{line} | posted: {posted.group(1)}"
    note = sanitize_field(f"{job.source} {_search_name(job, search_name)}".strip())
    line = f"{line} | note: {note}"
    jev = format_jev_segment(job)
    return f"{line} | {jev}" if jev else line


def format_jev_segment(job: Job) -> str:
    """`jev: {score} {verdict} — {reasons}` for a job jobs score already scored, else "" (contract C4)."""
    scoring = job.scoring if isinstance(job.scoring, dict) else {}
    if job.score is None or not job.verdict:
        return ""
    answers = {key: _normalize_scalar(scoring.get(key)) for key in JEV_REASON_KEYS}
    if not all(answers.values()):
        return ""
    parts = []
    for key in JEV_REASON_KEYS:
        value = answers[key].replace("_", " ")
        label = JEV_REASON_LABELS[key]
        parts.append(f"{label} {value}" if label else value)
    reasons = sanitize_field(", ".join(parts)[:JEV_REASONS_MAX_CHARS])
    return f"jev: {sanitize_field(job.score)} {sanitize_field(job.verdict)} — {reasons}"


def jd_relative_path(job: Job) -> str:
    """`jds/feeder/{source}-{source_id}.json`, relative to the data root, forward slashes."""
    name = _UNSAFE_FILENAME_RE.sub("_", f"{job.source}-{job.source_id}")
    return f"{JD_DIR_RELATIVE.as_posix()}/{name}.json"


def jd_document(job: Job, today: str, search_name: str = "") -> dict:
    """The JD JSON body: the job as stored in jobs.jsonl plus the C6 bookkeeping fields."""
    doc = job.to_dict()
    doc["url"] = sanitize_url(job.url)
    doc["first_seen"] = job.first_seen or today
    doc["search_name"] = _search_name(job, search_name)
    return doc


# ── reading what is already there ──────────────────────────────────────


def _url_key(url: str) -> str:
    """Dedup key: exact string, with one trailing slash ignored (C6 `readJdForUrl` matches both)."""
    return url[:-1] if url.endswith("/") else url


def _pipeline_url(line: str) -> str | None:
    """The URL on a checkbox row, wherever it sits (processed rows lead with `#NNN`)."""
    checkbox = _CHECKBOX_RE.match(line)
    if not checkbox:
        return None
    body = line[checkbox.end():]
    if body.startswith("~~"):
        closed = _STRIKETHROUGH_RE.match(body)
        body = closed.group(1) if closed else body[2:]
    found = _URL_RE.search(body)
    return found.group(0) if found else None


def pipeline_urls(text: str) -> set[str]:
    """Dedup keys of every URL on a `- [ ]`, `- [x]` or `- [!]` row of a pipeline file."""
    keys: set[str] = set()
    for line in text.splitlines():
        url = _pipeline_url(line)
        if url:
            keys.add(_url_key(url))
    return keys


def index_urls(text: str) -> set[str]:
    """Dedup keys of the `url` column of a jd-index.tsv text (header skipped when present)."""
    keys: set[str] = set()
    lines = text.splitlines()
    if lines and lines[0].startswith("url\t"):
        lines = lines[1:]
    for line in lines:
        url = line.split("\t", 1)[0].strip()
        if url:
            keys.add(_url_key(url))
    return keys


def _read_if_exists(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


# ── writing ────────────────────────────────────────────────────────────


def insert_pending_rows(text: str, rows: list[str]) -> str:
    """Return `text` with `rows` appended at the end of the Pending section.

    Mirrors `appendToPipeline` in career-ops scan.mjs: a missing file starts from
    the skeleton, a missing Pending section is created before Processed (or at the
    end), and an existing section is extended after its last row, keeping one
    blank line before the next heading.
    """
    if not rows:
        return text
    if not text:
        text = PIPELINE_SKELETON
    lines = text.split("\n")
    trailing_newline = text.endswith("\n")
    if trailing_newline:
        lines = lines[:-1]

    headings = [i for i, line in enumerate(lines) if _HEADING_RE.match(line.rstrip("\r"))]
    pending = next((i for i in headings if section_kind(lines[i]) == "pending"), None)

    if pending is None:
        processed = next((i for i in headings if section_kind(lines[i]) == "processed"), None)
        block = ["## Pending", "", *rows, ""]
        if processed is None:
            if lines and lines[-1].strip():
                block = ["", *block]
            lines = [*lines, *block[:-1]]
        else:
            lines = [*lines[:processed], *block, *lines[processed:]]
        return "\n".join(lines) + "\n"

    end = next((i for i in headings if i > pending), len(lines))
    last = pending
    for i in range(pending + 1, end):
        if lines[i].strip():
            last = i
    insert_at = last + 1
    block = list(rows)
    if last == pending:
        block = ["", *block]
    if insert_at == end and end < len(lines):
        block = [*block, ""]
    lines = [*lines[:insert_at], *block, *lines[insert_at:]]
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class ExportResult:
    added: int
    skipped_existing: int
    written_jds: int
    skipped_verdict: int = 0


def export_to_data_root(jobs: list[Job], data_root: Path, today: str, include_skips: bool = False) -> ExportResult:
    """Write the JD file, the index row and the Pending row for every job not yet in the data root.

    `today` (YYYY-MM-DD) is the `first_seen` of jobs that do not carry one. A URL
    found in `data/pipeline.md` or `data/jd-index.tsv` is skipped whole: no file,
    no index row, no pipeline row. Jobs without a description are exported too;
    career-ops can fetch the posting itself. A job `jobs score` marked `skip` stays
    out unless `include_skips` is true: the verdict already answered the question
    the pipeline exists to ask, and 250 skip rows bury the inbox.
    """
    data_root = Path(data_root)
    pipeline_path = data_root / PIPELINE_RELATIVE
    index_path = data_root / INDEX_RELATIVE

    pipeline_text = _read_if_exists(pipeline_path)
    index_text = _read_if_exists(index_path)
    seen = pipeline_urls(pipeline_text) | index_urls(index_text)

    rows: list[str] = []
    index_rows: list[str] = []
    written = skipped = skipped_verdict = 0
    for job in jobs:
        if not include_skips and job.verdict == "skip":
            skipped_verdict += 1
            continue
        url = sanitize_url(job.url)
        key = _url_key(url)
        if not url or key in seen:
            skipped += 1
            continue
        seen.add(key)
        search_name = _search_name(job)
        relative = jd_relative_path(job)
        doc = jd_document(job, today, search_name)
        jd_path = data_root / relative
        jd_path.parent.mkdir(parents=True, exist_ok=True)
        jd_path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        written += 1
        index_rows.append("\t".join([url, relative, _normalize_scalar(job.source), _normalize_scalar(job.source_id), doc["first_seen"]]))
        rows.append(format_pipeline_line(job, search_name))

    if index_rows:
        index_path.parent.mkdir(parents=True, exist_ok=True)
        with index_path.open("a", encoding="utf-8") as fh:
            if not index_text:
                fh.write(INDEX_HEADER + "\n")
            elif not index_text.endswith("\n"):
                fh.write("\n")
            fh.write("\n".join(index_rows) + "\n")

    if rows:
        pipeline_path.parent.mkdir(parents=True, exist_ok=True)
        pipeline_path.write_text(insert_pending_rows(pipeline_text, rows), encoding="utf-8")

    return ExportResult(added=len(rows), skipped_existing=skipped, written_jds=written, skipped_verdict=skipped_verdict)
