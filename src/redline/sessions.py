"""Which projects an account has been used in lately (read from the CLIs' own transcripts).

Claude Code: <home>/projects/<encoded cwd>/<session>.jsonl, every line carries "cwd".
Codex:       <home>/sessions/YYYY/MM/DD/rollout-*.jsonl, first line is session_meta with "cwd".
The VS Code extensions write the same files, so extension chats show up too.

Transcripts in the default dir (~/.claude, ~/.codex) belong to whichever account was
the default at the time, so they are attributed by slots.active_periods.
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, timedelta
from pathlib import Path

from .config import Account

ACTIVE_SECS = 180  # a transcript written this recently = a chat running right now
DAYS = 7


def _first_cwd(path: Path, max_lines: int = 40) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                if '"cwd"' not in line:
                    continue
                try:
                    doc = json.loads(line)
                except ValueError:
                    continue
                cwd = doc.get("cwd") or (doc.get("payload") or {}).get("cwd")
                if cwd:
                    return cwd
    except OSError:
        pass
    return None


def _claude(home: Path, since: float) -> dict[str, float]:
    out: dict[str, float] = {}
    root = home / "projects"
    try:
        dirs = list(os.scandir(root))
    except OSError:
        return out
    for d in dirs:
        if not d.is_dir():
            continue
        newest, newest_path = 0.0, None
        try:
            for f in os.scandir(d.path):
                if f.name.endswith(".jsonl"):
                    m = f.stat().st_mtime
                    if m > newest:
                        newest, newest_path = m, f.path
        except OSError:
            continue
        if newest_path and newest >= since:
            cwd = _first_cwd(Path(newest_path)) or d.name
            out[cwd] = max(out.get(cwd, 0), newest)
    return out


def _codex(home: Path, since: float) -> dict[str, float]:
    out: dict[str, float] = {}
    today = date.today()
    for n in range(DAYS + 1):
        day = today - timedelta(days=n)
        d = home / "sessions" / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
        try:
            files = list(os.scandir(d))
        except OSError:
            continue
        for f in files:
            if not f.name.endswith(".jsonl"):
                continue
            try:
                m = f.stat().st_mtime
            except OSError:
                continue
            if m < since:
                continue
            cwd = _first_cwd(Path(f.path), max_lines=3)
            if cwd:
                out[cwd] = max(out.get(cwd, 0), m)
    return out


def recent(account: Account, limit: int = 5) -> list[dict]:
    """[{"path", "at", "active"}], newest first, for the last DAYS days."""
    from .slots import active_periods, is_slot, slot_dir

    now = time.time()
    since = now - DAYS * 86400
    scan = _claude if account.provider == "claude" else _codex
    seen: dict[str, float] = {}

    def add(found: dict[str, float], periods: list[tuple[float, float]] | None = None) -> None:
        for cwd, at in found.items():
            if periods is not None and not any(f <= at < t for f, t in periods):  # half-open: a switch instant has one owner
                continue
            seen[cwd] = max(seen.get(cwd, 0), at)

    if not is_slot(account.provider, account.home_path):
        add(scan(account.home_path, since))
    periods = active_periods(account)
    if is_slot(account.provider, account.home_path) and not periods:
        add(scan(account.home_path, since))  # never switched: the default dir is simply this account's
    elif periods:
        add(scan(slot_dir(account.provider), since), periods)
    items = sorted(seen.items(), key=lambda kv: -kv[1])[:limit]
    return [{"path": p, "at": at, "active": now - at < ACTIVE_SECS} for p, at in items]
