"""Single-instance handoff for the tray app.

The running instance records {pid, port, token} in $REDLINE_HOME/instance.json.
A second launch first asks that instance to show its window (so double-clicking
redline.exe always "opens redline"). If the recorded instance doesn't answer -
e.g. a copy left hanging by an older build's broken self-update - the new launch
can find and end the stuck redline.exe processes and take over.
"""

from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from .config import redline_home

TOKEN_HEADER = "X-Redline-Token"


def state_path() -> Path:
    return redline_home() / "instance.json"


def write_state(port: int, token: str) -> None:
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "port": port, "token": token}, f)


def clear_state() -> None:
    try:
        doc = json.loads(state_path().read_text(encoding="utf-8"))
        if doc.get("pid") == os.getpid():
            state_path().unlink()
    except (OSError, ValueError):
        pass


def read_state() -> dict | None:
    try:
        doc = json.loads(state_path().read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) and doc.get("port") and doc.get("token") else None
    except (OSError, ValueError):
        return None


def ask_running_to_show(timeout: float = 3.0) -> bool:
    """True if a live instance answered and is now showing its window."""
    st = read_state()
    if not st:
        return False
    req = urllib.request.Request(
        f"http://127.0.0.1:{int(st['port'])}/api/action",
        data=json.dumps({"action": "show"}).encode(),
        headers={TOKEN_HEADER: str(st["token"]), "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return bool(json.loads(r.read()).get("ok"))
    except (OSError, ValueError, urllib.error.URLError):
        return False


def _no_window() -> dict:
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def stuck_pids() -> list[int]:
    """Other redline processes (not us, not our own one-file bootloader parent)."""
    mine = {os.getpid(), os.getppid()}
    if sys.platform != "win32":
        st = read_state()
        return [st["pid"]] if st and st.get("pid") not in mine else []
    image = Path(sys.executable).name
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH", "/FI", f"IMAGENAME eq {image}"],
                             capture_output=True, text=True, timeout=15, **_no_window()).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    pids = []
    for row in csv.reader(io.StringIO(out)):
        if len(row) >= 2 and row[0].lower() == image.lower() and row[1].isdigit():
            pid = int(row[1])
            if pid not in mine:
                pids.append(pid)
    return pids


def kill(pids: list[int]) -> None:
    for pid in pids:
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=15, **_no_window())
            else:
                os.kill(pid, 15)
        except (OSError, subprocess.TimeoutExpired):
            pass


def ask_yes_no(text: str) -> bool:
    if sys.platform != "win32":
        return True
    import ctypes

    MB_YESNO, MB_ICONQUESTION, IDYES = 0x4, 0x20, 6
    return ctypes.windll.user32.MessageBoxW(None, text, "redline", MB_YESNO | MB_ICONQUESTION) == IDYES
