from __future__ import annotations

import hashlib
import json
import os
import time
from threading import Event, Thread
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


REPLACE_WAIT = 3.0  # seconds write_json_atomic keeps retrying a busy target


def write_json_atomic(path: Path, data: dict) -> None:
    """Write JSON atomically, keeping the file private (tokens live in it).

    On Windows os.replace fails with PermissionError while anyone else has the target open (a read
    in another thread or process, antivirus, the indexer). The data is often a just-rotated refresh
    token whose predecessor is already spent, so a failed write is a lost login: retry for a while,
    then overwrite the file in place (those readers share writes, just not deletes). The tmp file
    never outlives this call, so it can't end up holding the only copy of live tokens.
    """
    tmp = path.with_name(path.name + ".redline.tmp")
    text = json.dumps(data, indent=2)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    deadline = time.monotonic() + REPLACE_WAIT
    delay = 0.02
    while True:
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                break
            time.sleep(delay)
            delay = min(delay * 2, 0.2)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        from ..applog import log

        log.warning("%s stayed busy for %.0f s; overwrote it in place", path, REPLACE_WAIT)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def token_id(token: str) -> str:
    """Short fingerprint of a secret, for remembering it without keeping it."""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def same_login(a: tuple, b: tuple) -> bool:
    """Do two identity tuples (e.g. (account id, email)) name the same login? Unknown parts match."""
    return all(x is None or y is None or x == y for x, y in zip(a, b))


class LockBusy(ProviderError):
    pass


class cli_lock:
    """Take the same lock Claude Code uses around token refreshes.

    Claude Code (via `proper-lockfile`) mkdirs `<config dir>.lock`, touches it every few seconds
    while it holds it, and treats it as stale once it is older than 10 s (60 s in newer versions).
    Holding it while we refresh means a concurrently running CLI waits and then re-reads the
    tokens we wrote instead of racing us with a rotated refresh token. We heartbeat it the same
    way: a token POST can take up to http.TIMEOUT (20 s), and a waiter that broke a live lock
    would spend the same single-use refresh token a second time.
    """

    STALE = 10.0
    UPDATE = 2.5  # heartbeat period, well inside STALE

    def __init__(self, target: Path, retries: int = 6):
        self.lock = Path(str(target).rstrip("/\\") + ".lock")
        self.retries = retries
        self._mtime: int | None = None  # the lock dir's mtime as we last set it; None = not ours
        self._stop = Event()
        self._beat: Thread | None = None

    def __enter__(self):
        for _ in range(self.retries):
            try:
                self.lock.mkdir()
            except FileExistsError:
                try:
                    seen = self.lock.stat().st_mtime_ns
                except FileNotFoundError:
                    continue
                if time.time() - seen / 1e9 > self.STALE:
                    try:
                        if self.lock.stat().st_mtime_ns == seen:  # not just re-taken by another waiter
                            self.lock.rmdir()
                    except OSError:
                        pass
                    continue
                time.sleep(1.0)
            except FileNotFoundError:  # parent dir missing
                return self
            else:
                self._hold()
                return self
        raise LockBusy(f"{self.lock} is held by another process")

    def _hold(self) -> None:
        try:
            self._mtime = self.lock.stat().st_mtime_ns
        except OSError:
            self._mtime = None
            return
        self._stop = Event()
        self._beat = Thread(target=self._heartbeat, name="redline-lock", daemon=True)
        self._beat.start()

    def _owned(self) -> bool:
        """Still our lock? A holder that stalled past STALE may have lost it to someone else."""
        try:
            return self._mtime is not None and self.lock.stat().st_mtime_ns == self._mtime
        except OSError:
            return False

    def _heartbeat(self) -> None:
        while not self._stop.wait(self.UPDATE):
            if not self._owned():
                return  # taken over: leave the new holder's lock alone
            try:
                os.utime(self.lock)
                self._mtime = self.lock.stat().st_mtime_ns
            except OSError:
                pass  # try again next beat

    def __exit__(self, *exc):
        self._stop.set()
        if self._beat is not None:
            self._beat.join()
        if self._owned():
            try:
                self.lock.rmdir()
            except OSError:
                pass
        self._mtime = None
        return False
