from __future__ import annotations

import json
from pathlib import Path

DEFAULT_SEEN_PATH = Path("out/seen_ids.json")


def load_seen(path: Path = DEFAULT_SEEN_PATH) -> set[str]:
    """Job ids (source-qualified, e.g. 'linkedin:123') opened in earlier runs."""
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    return set(str(x) for x in data)


def save_seen(ids: set[str], path: Path = DEFAULT_SEEN_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(ids), indent=0) + "\n", encoding="utf-8")


def qualify(source: str, source_id: str) -> str:
    return f"{source}:{source_id}"
