"""Environment-backed settings.

Lookup order for every setting: the process environment (a `.env` file at the project
root is loaded into it once), then the macOS Keychain, where the service name is the
variable name and the account is the current user. Store a secret there with

    security add-generic-password -a "$USER" -s OPENROUTER_API_KEY_JOB_SEARCH -w

which prompts for the value so it never lands in shell history.
"""

from __future__ import annotations

import getpass
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(".env"))

# The job-search specific name wins so a key on a separate OpenRouter account can sit next
# to a general-purpose OPENROUTER_API_KEY in the same Keychain or shell.
OPENROUTER_API_KEY_NAMES = ("OPENROUTER_API_KEY_JOB_SEARCH", "OPENROUTER_API_KEY")
OPENROUTER_API_KEY = OPENROUTER_API_KEY_NAMES[0]
EXPLAIN_MODEL = "JOB_HUNT_EXPLAIN_MODEL"
# "llm" (chat model, default) or "jev" (OpenRouter decisions endpoint, needs the typesafe provider).
SCORER = "JOB_HUNT_SCORER"
SCORE_MODEL = "JOB_HUNT_SCORE_MODEL"


def env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or keychain(name)


def keychain(name: str) -> str | None:
    """Read a generic password from the macOS Keychain; None when absent or not on macOS."""
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", getpass.getuser(), "-s", name, "-w"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def openrouter_key() -> str | None:
    """First OpenRouter key found, trying the names in OPENROUTER_API_KEY_NAMES in order."""
    for name in OPENROUTER_API_KEY_NAMES:
        value = env(name)
        if value is not None:
            return value
    return None


def missing(*names: str) -> list[str]:
    return [n for n in names if env(n) is None]
