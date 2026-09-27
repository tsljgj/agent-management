from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def from_epoch(ts: float | int | None) -> datetime | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), tz=timezone.utc)


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, NotADirectoryError):
        return None


def write_json_atomic(path: Path, data: dict) -> None:
    """Write JSON atomically, keeping the file private (tokens live in it)."""
    tmp = path.with_name(path.name + ".agentman.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
