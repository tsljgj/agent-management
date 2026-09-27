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
from .models import ProviderError

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


def latest() -> Release | None:
    doc = json.loads(_get(LATEST_API))
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
    req = urllib.request.Request(url, headers={"User-Agent": UA["User-Agent"]})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        while chunk := r.read(1 << 16):
            h.update(chunk)
            f.write(chunk)
    return h.hexdigest()


def install(rel: Release, extra_args: list[str] | None = None) -> None:
    """Download, verify, swap and launch the new exe. The caller must exit afterwards."""
    if not can_self_update():
        raise ProviderError("self-update only works for the packaged Windows exe")
    exe = Path(sys.executable)
    new = exe.with_name(exe.name + ".download")
    old = exe.with_name(exe.name + ".old")
    expected = _get(rel.sha_url).decode().split()[0].strip().lower()
    got = _download(rel.exe_url, new)
    if got != expected:
        new.unlink(missing_ok=True)
        raise ProviderError(f"checksum mismatch for build {rel.build}; update aborted")
    try:
        old.unlink(missing_ok=True)
    except OSError:
        pass
    os.replace(exe, old)  # renaming a running exe is allowed on Windows
    try:
        os.replace(new, exe)
    except OSError:
        os.replace(old, exe)
        raise
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([str(exe), "--updated-from", str(BUILD), *(extra_args or [])],
                     creationflags=flags, close_fds=True)


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
