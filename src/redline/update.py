"""Self-update for the packaged Windows exe, from GitHub Releases.

CI publishes every green build as a release tagged `build-<N>` with `redline.exe`
and `redline.exe.sha256`. The running exe compares N with its own BUILD, downloads
the new exe next to itself, verifies the checksum, then swaps files: Windows lets
you *rename* a running exe, so the current one becomes `redline.exe.old`, the new
one takes its place, gets launched, and the old process exits. The next start
deletes the `.old` file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ._build import BUILD
from .applog import log
from .models import ProviderError
from .procenv import child_env

REPO = "tsljgj/agent-management"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
TAG_RE = re.compile(r"^build-(\d+)$")
ASSET = "redline.exe"
UA = {"User-Agent": f"redline-updater/{BUILD}", "Accept": "application/vnd.github+json"}


@dataclass
class Release:
    build: int
    tag: str
    name: str
    exe_url: str
    sha_url: str
    html_url: str


def is_packaged() -> bool:
    return bool(getattr(sys, "frozen", False)) and BUILD > 0


def can_self_update() -> bool:
    return is_packaged() and sys.platform == "win32"


def _get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise ProviderError(f"HTTP {e.code} from {url}") from None
    except urllib.error.URLError as e:
        raise ProviderError(f"network error: {e.reason}") from None


def _feed() -> str:
    # REDLINE_UPDATE_FEED: a local release JSON (same shape as GitHub's) for end-to-end tests
    return os.environ.get("REDLINE_UPDATE_FEED") or LATEST_API


def _get_any(url: str, timeout: int = 20) -> bytes:
    if url.startswith(("http://", "https://")):
        return _get(url, timeout)
    return Path(url.removeprefix("file://")).read_bytes()


def latest() -> Release | None:
    doc = json.loads(_get_any(_feed()))
    m = TAG_RE.match(doc.get("tag_name") or "")
    if not m:
        return None
    assets = {a.get("name"): a.get("browser_download_url") for a in doc.get("assets") or []}
    if ASSET not in assets or ASSET + ".sha256" not in assets:
        return None
    return Release(int(m.group(1)), doc["tag_name"], doc.get("name") or doc["tag_name"],
                   assets[ASSET], assets[ASSET + ".sha256"], doc.get("html_url") or "")


def check() -> tuple[Release | None, str]:
    """(newer release or None, human-readable status)."""
    if not is_packaged():
        return None, "running from source (build 0): update with `git pull` + `pip install -e .`"
    rel = latest()
    if rel is None:
        return None, "no published build found"
    if rel.build <= BUILD:
        return None, f"up to date (build {BUILD})"
    return rel, f"update available: build {BUILD} -> {rel.build}"


def _download(url: str, dest: Path) -> str:
    h = hashlib.sha256()
    if not url.startswith(("http://", "https://")):
        data = Path(url.removeprefix("file://")).read_bytes()
        dest.write_bytes(data)
        return hashlib.sha256(data).hexdigest()
    req = urllib.request.Request(url, headers={"User-Agent": UA["User-Agent"]})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        while chunk := r.read(1 << 16):
            h.update(chunk)
            f.write(chunk)
    return h.hexdigest()


def install(rel: Release, extra_args: list[str] | None = None, window: dict | None = None) -> None:
    """Download, verify, swap and launch the new exe. The caller must exit afterwards."""
    if not can_self_update():
        raise ProviderError("self-update only works for the packaged Windows exe")
    exe = Path(sys.executable)
    new = exe.with_name(exe.name + ".download")
    old = exe.with_name(exe.name + ".old")
    log.info("installing build %s from %s", rel.build, rel.exe_url)
    expected = _get_any(rel.sha_url).decode().split()[0].strip().lower()
    got = _download(rel.exe_url, new)
    if got != expected:
        new.unlink(missing_ok=True)
        raise ProviderError(f"checksum mismatch for build {rel.build}; update aborted")
    try:
        old.unlink(missing_ok=True)
    except OSError:
        pass
    # Renaming a running exe is allowed on Windows, but a scanner (Defender) or our own
    # archive reader may hold a handle for a moment -> WinError 32. Retry briefly.
    _retry(lambda: os.replace(exe, old))
    try:
        _retry(lambda: os.replace(new, exe))
    except OSError:
        _retry(lambda: os.replace(old, exe))
        raise
    write_handoff(window)
    log.info("swapped exe, relaunching %s", exe)
    launch_detached(str(exe), ["--updated-from", str(BUILD), *(extra_args or [])])


def handoff_path() -> Path:
    from .config import redline_home

    return redline_home() / "update-handoff.json"


def write_handoff(window: dict | None = None) -> None:
    """Tells the relaunched exe it comes from an update even if it lost its arguments,
    and how the window was ({"show", "x", "y"}) so the new one picks up where we left off."""
    try:
        p = handoff_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        doc = {"from": BUILD, "at": time.time(), "pid": os.getpid(), "window": window or {}}
        p.write_text(json.dumps(doc), encoding="utf-8")
    except OSError:
        pass


def take_handoff(max_age: float = 180, full: bool = False):
    """If we were started by a self-update, return the old build (and consume the note).
    full=True returns the whole note ({"from", "window", ...}) instead."""
    p = handoff_path()
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        p.unlink()
    except (OSError, ValueError):
        return None
    if time.time() - float(doc.get("at", 0)) > max_age:
        return None
    return doc if full else int(doc.get("from", 0))


def launch_detached(exe: str, args: list[str], reset_env: bool = True) -> None:
    """Start `exe` as an independent process that outlives us (see procenv)."""
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    env = child_env() if reset_env else dict(os.environ)
    subprocess.Popen([exe, *args], env=env, creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _retry(fn, attempts: int = 30, delay: float = 0.2):
    for i in range(attempts):
        try:
            return fn()
        except PermissionError as e:  # WinError 5/32: file briefly in use
            if i == attempts - 1:
                raise
            log.info("file busy (%s), retrying", e)
            time.sleep(delay)


def cleanup_old() -> None:
    if not is_packaged():
        return
    old = Path(sys.executable).with_name(Path(sys.executable).name + ".old")
    for _ in range(20):  # the previous process may still be shutting down
        try:
            old.unlink(missing_ok=True)
            return
        except OSError:
            time.sleep(0.5)
