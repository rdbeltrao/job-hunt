import subprocess

from jobs import settings
from jobs.settings import keychain as real_keychain  # bound before the autouse no_keychain fixture runs


def test_env_prefers_process_environment(monkeypatch):
    monkeypatch.setenv("JOB_HUNT_TEST_SECRET", " from-env ")
    monkeypatch.setattr(settings, "keychain", lambda name: "from-keychain")
    assert settings.env("JOB_HUNT_TEST_SECRET") == "from-env"


def test_env_falls_back_to_keychain(monkeypatch):
    monkeypatch.delenv("JOB_HUNT_TEST_SECRET", raising=False)
    monkeypatch.setattr(settings, "keychain", lambda name: "from-keychain" if name == "JOB_HUNT_TEST_SECRET" else None)
    assert settings.env("JOB_HUNT_TEST_SECRET") == "from-keychain"


def test_env_none_when_nowhere(monkeypatch):
    monkeypatch.delenv("JOB_HUNT_TEST_SECRET", raising=False)
    monkeypatch.setattr(settings, "keychain", lambda name: None)
    assert settings.env("JOB_HUNT_TEST_SECRET") is None
    assert settings.missing("JOB_HUNT_TEST_SECRET") == ["JOB_HUNT_TEST_SECRET"]


def test_keychain_reads_security_output(monkeypatch):
    monkeypatch.setattr(settings.sys, "platform", "darwin")
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="sk-or-abc\n", stderr="")

    monkeypatch.setattr(settings.subprocess, "run", fake_run)
    assert real_keychain("OPENROUTER_API_KEY") == "sk-or-abc"
    assert calls[0][:3] == ["security", "find-generic-password", "-a"]
    assert "OPENROUTER_API_KEY" in calls[0]


def test_keychain_none_when_item_missing(monkeypatch):
    monkeypatch.setattr(settings.sys, "platform", "darwin")
    monkeypatch.setattr(
        settings.subprocess,
        "run",
        lambda args, **kw: subprocess.CompletedProcess(args, 44, stdout="", stderr="could not be found"),
    )
    assert real_keychain("OPENROUTER_API_KEY") is None


def test_keychain_skipped_off_macos(monkeypatch):
    monkeypatch.setattr(settings.sys, "platform", "linux")
    monkeypatch.setattr(settings.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    assert real_keychain("OPENROUTER_API_KEY") is None


def test_openrouter_key_prefers_job_search_name(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "general")
    monkeypatch.setenv("OPENROUTER_API_KEY_JOB_SEARCH", "job")
    assert settings.openrouter_key() == "job"
    monkeypatch.delenv("OPENROUTER_API_KEY_JOB_SEARCH")
    assert settings.openrouter_key() == "general"
    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert settings.openrouter_key() is None
