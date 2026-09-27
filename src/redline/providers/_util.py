from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from ..models import ProviderError


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
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, NotADirectoryError):
        return None


def write_json_atomic(path: Path, data: dict) -> None:
    """Write JSON atomically, keeping the file private (tokens live in it)."""
    tmp = path.with_name(path.name + ".redline.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


class LockBusy(ProviderError):
    pass


class cli_lock:
    """Take the same lock Claude Code uses around token refreshes.

    Claude Code (via `proper-lockfile`) mkdirs `<config dir>.lock` and treats it as stale
    after 10s. Holding it while we refresh means a concurrently running CLI waits and then
    re-reads the tokens we wrote instead of racing us with a rotated refresh token.
    """

    STALE = 10.0

    def __init__(self, target: Path, retries: int = 6):
        self.lock = Path(str(target).rstrip("/\\") + ".lock")
        self.retries = retries

    def __enter__(self):
        import time

        for _ in range(self.retries):
            try:
                self.lock.mkdir()
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.lock.stat().st_mtime
                except FileNotFoundError:
                    continue
                if age > self.STALE:
                    try:
                        self.lock.rmdir()
                    except OSError:
                        pass
                    continue
                time.sleep(1.0)
            except FileNotFoundError:  # parent dir missing
                return self
        raise LockBusy(f"{self.lock} is held by another process")

    def __exit__(self, *exc):
        try:
            self.lock.rmdir()
        except OSError:
            pass
        return False
