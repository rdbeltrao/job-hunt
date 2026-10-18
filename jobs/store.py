"""Read and write out/jobs.jsonl, the hand-off file between fetch, score and export."""

from __future__ import annotations

import json
from pathlib import Path

from jobs.models import Job

DEFAULT_JOBS_PATH = Path("out/jobs.jsonl")


def load_jobs(path: Path = DEFAULT_JOBS_PATH) -> list[Job]:
    if not path.exists():
        return []
    jobs: list[Job] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                jobs.append(Job.from_dict(json.loads(line)))
    return jobs


def save_jobs(jobs: list[Job], path: Path = DEFAULT_JOBS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for job in jobs:
            fh.write(json.dumps(job.to_dict(), ensure_ascii=False) + "\n")
