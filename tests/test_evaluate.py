import argparse
import os
import subprocess
from pathlib import Path

from jobs import cli
from jobs import evaluate as ev


def _checkout(tmp_path: Path) -> Path:
    root = tmp_path / "career-ops"
    (root / "batch").mkdir(parents=True)
    (root / ev.SYNC_SCRIPT).write_text("// sync")
    (root / ev.RUNNER_SCRIPT).write_text("#!/bin/bash\n")
    return root


def _data_root(tmp_path: Path) -> Path:
    root = tmp_path / "data-root"
    (root / "data").mkdir(parents=True)
    (root / "data" / "pipeline.md").write_text("# Pipeline\n\n## Pending\n\n- [ ] https://x/1 | A | EM\n\n## Processed\n")
    return root


def _ok_sync(*args, **kwargs):
    return subprocess.CompletedProcess(args[0], 0, stdout="Added 1 new entries\n", stderr="")


def test_missing_scripts_fail_before_anything_runs(tmp_path, monkeypatch):
    checkout = tmp_path / "empty"
    checkout.mkdir()
    called = []
    monkeypatch.setattr(ev.subprocess, "run", lambda *a, **k: called.append(a))
    result = ev.evaluate(_data_root(tmp_path), checkout)
    assert not result.ok
    assert "sync-batch-input.mjs" in result.errors[0] and "batch/batch-runner.sh" in result.errors[0]
    assert called == []


def test_missing_pipeline_fails_clearly(tmp_path):
    result = ev.evaluate(tmp_path / "no-root", _checkout(tmp_path))
    assert not result.ok and "run `jobs pipeline` first" in result.errors[0]


def test_sync_then_detached_start(tmp_path, monkeypatch):
    checkout = _checkout(tmp_path)
    data_root = _data_root(tmp_path)
    runs: list[list[str]] = []
    starts: list[tuple[Path, Path, int]] = []

    def fake_run(cmd, cwd=None, **kwargs):
        runs.append(cmd)
        assert Path(cwd) == checkout
        return _ok_sync(cmd)

    monkeypatch.setattr(ev.subprocess, "run", fake_run)
    monkeypatch.setattr(ev, "start_runner", lambda c, log, parallel: starts.append((c, log, parallel)) or 4242)

    result = ev.evaluate(data_root, checkout, parallel=2)
    assert result.ok and result.started_pid == 4242
    assert runs == [["node", "sync-batch-input.mjs", "--pipeline", str(data_root / "data" / "pipeline.md")]]
    assert starts == [(checkout, data_root / "data" / "batch-runner.log", 2)]
    text = ev.describe(result, checkout)
    assert "Added 1 new entries" in text and "started batch-runner.sh (PID 4242)" in text


def test_live_lock_only_queues(tmp_path, monkeypatch):
    checkout = _checkout(tmp_path)
    (checkout / ev.LOCK_FILE).write_text(str(os.getpid()))
    monkeypatch.setattr(ev.subprocess, "run", lambda cmd, **k: _ok_sync(cmd))
    monkeypatch.setattr(ev, "start_runner", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not start")))

    result = ev.evaluate(_data_root(tmp_path), checkout)
    assert result.ok and result.already_running_pid == os.getpid() and result.started_pid is None
    assert "already running" in ev.describe(result, checkout)


def test_stale_lock_is_ignored(tmp_path, monkeypatch):
    checkout = _checkout(tmp_path)
    (checkout / ev.LOCK_FILE).write_text("999999999")
    monkeypatch.setattr(ev.subprocess, "run", lambda cmd, **k: _ok_sync(cmd))
    monkeypatch.setattr(ev, "start_runner", lambda *a, **k: 7)
    assert ev.evaluate(_data_root(tmp_path), checkout).started_pid == 7


def test_dry_run_syncs_with_flag_and_starts_nothing(tmp_path, monkeypatch):
    checkout = _checkout(tmp_path)
    seen: list[list[str]] = []
    monkeypatch.setattr(ev.subprocess, "run", lambda cmd, **k: seen.append(cmd) or _ok_sync(cmd))
    monkeypatch.setattr(ev, "start_runner", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not start")))
    result = ev.evaluate(_data_root(tmp_path), checkout, dry_run=True)
    assert result.ok and seen[0][-1] == "--dry-run" and result.started_pid is None


def test_sync_failure_stops_before_runner(tmp_path, monkeypatch):
    checkout = _checkout(tmp_path)
    monkeypatch.setattr(ev.subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom"))
    monkeypatch.setattr(ev, "start_runner", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not start")))
    result = ev.evaluate(_data_root(tmp_path), checkout)
    assert not result.ok and "sync-batch-input.mjs exited 1: boom" in result.errors[0]


def test_wait_runs_runner_in_foreground(tmp_path, monkeypatch):
    checkout = _checkout(tmp_path)
    monkeypatch.setattr(ev.subprocess, "run", lambda cmd, **k: _ok_sync(cmd))
    monkeypatch.setattr(ev, "run_runner_foreground", lambda c, parallel: 3)
    result = ev.evaluate(_data_root(tmp_path), checkout, wait=True)
    assert result.runner_exit == 3 and not result.ok


def test_runner_command_evaluates_apply_and_maybe_only():
    assert ev.runner_command(3) == ["bash", "batch/batch-runner.sh", "--skip-jev-verdict", "skip", "--parallel", "3"]


def test_checkout_resolution(monkeypatch):
    monkeypatch.delenv("JOB_HUNT_CAREER_OPS", raising=False)
    assert ev.career_ops_checkout(None) == Path("~/Projects/career-ops").expanduser()
    monkeypatch.setenv("JOB_HUNT_CAREER_OPS", "/opt/co")
    assert ev.career_ops_checkout(None) == Path("/opt/co")
    assert ev.career_ops_checkout("/x") == Path("/x")


def test_cli_evaluate_without_data_root_skips(monkeypatch, capsys):
    monkeypatch.delenv("JOB_HUNT_DATA_ROOT", raising=False)
    assert cli.run_evaluate(argparse.Namespace(data_root=None)) == 1
    assert "no data root" in capsys.readouterr().out


def test_run_all_adds_evaluate_after_pipeline(monkeypatch, capsys):
    calls: list[str] = []
    for name in ("fetch", "score", "pipeline", "evaluate", "export"):
        monkeypatch.setattr(cli, f"run_{name}", (lambda n: lambda args: calls.append(n) or 0)(name))
    assert cli.run_all(argparse.Namespace(data_root="/tmp/root")) == 0
    assert calls == ["fetch", "score", "pipeline", "evaluate", "export"]
    calls.clear()
    assert cli.run_all(argparse.Namespace(data_root="/tmp/root", no_evaluate=True)) == 0
    assert calls == ["fetch", "score", "pipeline", "export"]


def test_run_parser_accepts_evaluate_flags():
    args = cli.build_parser().parse_args(["run", "--data-root", "/r", "--no-evaluate", "--parallel", "2"])
    assert args.no_evaluate is True and args.parallel == 2
    args = cli.build_parser().parse_args(["evaluate", "--data-root", "/r", "--dry-run"])
    assert args.func is cli.run_evaluate and args.dry_run is True
