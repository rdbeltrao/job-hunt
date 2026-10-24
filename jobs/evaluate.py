"""Hand the pending career-ops inbox to its headless batch evaluation (the `jobs evaluate` command).

`jobs pipeline` appends every new opening to `data/pipeline.md` in the career-ops data root.
From there career-ops needs two more steps before a posting has a full A-G report and a
tracker row: `sync-batch-input.mjs` copies the pending rows into `batch/batch-input.tsv`,
and `batch/batch-runner.sh` runs one headless agent per row. Both used to be started by
hand, or from the dashboard, whose evaluate worker dies with the browser tab. This module
runs them from the command line so the daily run can finish the job on its own:

1. `node sync-batch-input.mjs --pipeline <data root>/data/pipeline.md` in the career-ops
   checkout. Idempotent: rows already queued or processed are not added again.
2. `bash batch/batch-runner.sh --skip-jev-verdict skip --parallel N`, detached from the
   calling process (its own session, output appended to `<data root>/data/batch-runner.log`),
   so a scheduled agent can start it and end its turn. Only postings Jev did not mark `skip`
   get an agent; apply and maybe both do. The runner keeps its own lock
   (`batch/batch-runner.pid`); when it is held by a live process the new rows are only
   queued and the running batch is left alone.

The career-ops checkout is `--career-ops`, else JOB_HUNT_CAREER_OPS, else ~/Projects/career-ops.
Only the standard library is used here.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from jobs import settings

SYNC_SCRIPT = Path("sync-batch-input.mjs")
RUNNER_SCRIPT = Path("batch") / "batch-runner.sh"
LOCK_FILE = Path("batch") / "batch-runner.pid"
LOG_RELATIVE = Path("data") / "batch-runner.log"
DEFAULT_PARALLEL = 3
# Rows whose Jev verdict is in this list get no agent; apply and maybe are both evaluated.
SKIP_VERDICTS = "skip"


@dataclass
class EvaluateResult:
    synced_output: str = ""
    started_pid: int | None = None
    already_running_pid: int | None = None
    log_path: Path | None = None
    runner_exit: int | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def career_ops_checkout(explicit: str | None = None) -> Path:
    """The career-ops checkout: the flag, else JOB_HUNT_CAREER_OPS, else ~/Projects/career-ops."""
    value = explicit or os.environ.get(settings.CAREER_OPS, "").strip() or "~/Projects/career-ops"
    return Path(value).expanduser()


def missing_scripts(checkout: Path) -> list[str]:
    return [str(rel) for rel in (SYNC_SCRIPT, RUNNER_SCRIPT) if not (checkout / rel).is_file()]


def running_runner_pid(checkout: Path) -> int | None:
    """The PID in batch/batch-runner.pid when that process is alive, else None."""
    try:
        pid = int((checkout / LOCK_FILE).read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid
    return pid


def sync_batch_input(checkout: Path, data_root: Path, *, dry_run: bool = False) -> subprocess.CompletedProcess[str]:
    cmd = ["node", str(SYNC_SCRIPT), "--pipeline", str(data_root / "data" / "pipeline.md")]
    if dry_run:
        cmd.append("--dry-run")
    return subprocess.run(cmd, cwd=checkout, capture_output=True, text=True, check=False)


def runner_command(parallel: int) -> list[str]:
    return ["bash", str(RUNNER_SCRIPT), "--skip-jev-verdict", SKIP_VERDICTS, "--parallel", str(parallel)]


def start_runner(checkout: Path, log_path: Path, parallel: int) -> int:
    """Start the batch runner detached (own session, output appended to log_path) and return its PID."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as log, open(os.devnull, "rb") as devnull:
        proc = subprocess.Popen(  # noqa: S603 - fixed command, arguments are ours
            runner_command(parallel),
            cwd=checkout,
            stdin=devnull,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return proc.pid


def run_runner_foreground(checkout: Path, parallel: int) -> int:
    proc = subprocess.run(runner_command(parallel), cwd=checkout, check=False)  # noqa: S603
    return proc.returncode


def evaluate(
    data_root: Path,
    checkout: Path,
    *,
    parallel: int = DEFAULT_PARALLEL,
    wait: bool = False,
    dry_run: bool = False,
) -> EvaluateResult:
    """Sync the pending inbox into the batch input, then start the runner unless one is alive."""
    result = EvaluateResult(log_path=data_root / LOG_RELATIVE)
    missing = missing_scripts(checkout)
    if missing:
        result.errors.append(f"career-ops checkout {checkout} has no {', '.join(missing)}")
        return result
    if not (data_root / "data" / "pipeline.md").is_file():
        result.errors.append(f"no pipeline at {data_root / 'data' / 'pipeline.md'}; run `jobs pipeline` first")
        return result

    synced = sync_batch_input(checkout, data_root, dry_run=dry_run)
    result.synced_output = (synced.stdout + synced.stderr).strip()
    if synced.returncode != 0:
        result.errors.append(f"sync-batch-input.mjs exited {synced.returncode}: {result.synced_output[-400:]}")
        return result
    if dry_run:
        return result

    live = running_runner_pid(checkout)
    if live is not None:
        result.already_running_pid = live
        return result
    if wait:
        result.runner_exit = run_runner_foreground(checkout, parallel)
        if result.runner_exit != 0:
            result.errors.append(f"batch-runner.sh exited {result.runner_exit}")
        return result
    try:
        result.started_pid = start_runner(checkout, result.log_path, parallel)
    except OSError as exc:
        result.errors.append(f"could not start batch-runner.sh: {type(exc).__name__}: {exc}")
    return result


def describe(result: EvaluateResult, checkout: Path) -> str:
    lines = []
    if result.synced_output:
        tail = [ln for ln in result.synced_output.splitlines() if ln.strip()][-3:]
        lines.extend(f"evaluate: {ln.strip()}" for ln in tail)
    if result.already_running_pid is not None:
        lines.append(
            f"evaluate: batch-runner.sh already running (PID {result.already_running_pid}); new rows are queued in "
            f"{checkout / 'batch' / 'batch-input.tsv'} for its next start"
        )
    if result.started_pid is not None:
        lines.append(f"evaluate: started batch-runner.sh (PID {result.started_pid}), log {result.log_path}")
    if result.runner_exit is not None:
        lines.append(f"evaluate: batch-runner.sh finished with exit {result.runner_exit}")
    lines.extend(f"evaluate: failed: {err}" for err in result.errors)
    return "\n".join(lines) or "evaluate: nothing to do"


def main_from_args(data_root: str | None, career_ops: str | None, *, parallel: int, wait: bool, dry_run: bool) -> int:
    if not data_root:
        print(f"evaluate: skipped, no data root: pass --data-root or set {settings.DATA_ROOT} to the career-ops data directory.")
        return 1
    checkout = career_ops_checkout(career_ops)
    result = evaluate(Path(data_root).expanduser(), checkout, parallel=parallel, wait=wait, dry_run=dry_run)
    print(describe(result, checkout), file=sys.stdout)
    return 0 if result.ok else 1
