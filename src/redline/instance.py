"""Single-instance handoff for the tray app.

The running instance records {pid, port, token} in $REDLINE_HOME/instance.json.
A second launch first asks that instance to show its window (so double-clicking
redline.exe always "opens redline"). If the recorded instance doesn't answer -
e.g. a copy left hanging by an older build's broken self-update - the new launch
can find and end the stuck redline tray processes and take over.

Only processes that are recognisably a redline tray (the packaged exe, or Python
running `redline tray`) are ever targeted: never an interpreter just because it is
python.exe, never ourselves, our own bootloader or anything we started.
"""

from __future__ import annotations

import json
import os
import re
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


def ping_running(timeout: float = 3.0) -> bool:
    """True if a live instance answered, without making it show anything (a --background
    launch must not pop the console open). Any build's server answers this read."""
    st = read_state()
    if not st:
        return False
    req = urllib.request.Request(
        f"http://127.0.0.1:{int(st['port'])}/api/usage?since={2 ** 62}",
        headers={TOKEN_HEADER: str(st["token"])},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return "usages" in json.loads(r.read())
    except (OSError, ValueError, urllib.error.URLError):
        return False


def _no_window() -> dict:
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


# One snapshot of the process table: pid, parent, session, image name and command line.
_PS_SNAPSHOT = (
    "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
    "Get-CimInstance Win32_Process | "
    "Select-Object ProcessId,ParentProcessId,SessionId,Name,CommandLine | ConvertTo-Json -Compress"
)


def _processes() -> list[dict] | None:
    """[{pid, ppid, session, name, cmd}] for every process, or None if the table can't be read."""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_SNAPSHOT],
                             capture_output=True, timeout=60, **_no_window()).stdout
        doc = json.loads(out.decode("utf-8-sig", "replace").strip() or "null")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if isinstance(doc, dict):
        doc = [doc]
    if not isinstance(doc, list):
        return None
    procs = []
    for p in doc:
        try:
            procs.append({"pid": int(p["ProcessId"]), "ppid": int(p.get("ParentProcessId") or 0),
                          "session": p.get("SessionId"), "name": str(p.get("Name") or ""),
                          "cmd": str(p.get("CommandLine") or "")})
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return procs


_PYTHON = re.compile(r"^pythonw?[\d.]*(\.exe)?$", re.I)


def _args(cmd: str) -> list[str]:
    return [a if a else b for a, b in re.findall(r'"([^"]*)"|(\S+)', cmd)]


def is_redline_tray(proc: dict, image: str = "") -> bool:
    """Is this process (from _processes) a redline tray app? The packaged exe runs the tray
    unless it got a URL (browser callback); Python counts only when it runs `redline tray`."""
    name = proc.get("name", "").lower()
    args = _args(proc.get("cmd", ""))[1:]
    if name and name in ("redline.exe", image.lower()):
        # the packaged exe, or pip's `redline` launcher: tray unless a URL / another subcommand
        return not args or args[0] == "tray" or args[0].startswith("-")
    if not _PYTHON.match(name):
        return False
    i = 0
    while i < len(args):  # skip the interpreter's own options
        a = args[i]
        if a == "-m":
            return args[i + 1:i + 3] == ["redline", "tray"]
        if a.startswith("-c"):
            return False
        i += 2 if a in ("-W", "-X") else 1
        if not a.startswith("-"):
            stem = re.split(r"[\\/]", a)[-1].lower()
            if stem == "redline_tray.py":  # the packaged entry point, run from a checkout
                return not args[i:] or not args[i].strip("\"'").startswith(("http://", "https://"))
            return stem in ("redline", "redline.exe", "redline-script.py") and args[i:i + 1] == ["tray"]
    return False


def pick_stuck(procs: list[dict], me: int, previous: int | None = None, image: str = "") -> list[int]:
    """The redline tray processes to end so that we can take over.

    Never us, never anything we started (descendants) and never our ancestors: our own
    one-file bootloader, the shell or launcher that started us. The one exception is
    `previous`: the old instance that started us during a self-update (and its bootloader).
    It is our ancestor by construction, and it is exactly what this is for; without a tree
    kill, ending it cannot take us down."""
    by_pid = {p["pid"]: p for p in procs}
    session = by_pid.get(me, {}).get("session")
    ancestors, cur, seen = [], by_pid.get(me), {me}
    while cur and cur["ppid"] not in seen:
        seen.add(cur["ppid"])
        ancestors.append(cur["ppid"])
        cur = by_pid.get(cur["ppid"])
    children: dict[int, list[int]] = {}
    for p in procs:
        children.setdefault(p["ppid"], []).append(p["pid"])
    descendants, todo = set(), [me]
    while todo:
        for c in children.get(todo.pop(), []):
            if c not in descendants and c != me:
                descendants.add(c)
                todo.append(c)
    allowed = set()
    if previous and previous in by_pid and is_redline_tray(by_pid[previous], image):
        allowed.add(previous)
        boot = by_pid.get(by_pid[previous]["ppid"])
        if boot and is_redline_tray(boot, image):
            allowed.add(boot["pid"])
    own = {me, ancestors[0]} if ancestors else {me}  # ourselves and our direct parent: never
    protected = (set(ancestors) - allowed) | descendants | own
    return [p["pid"] for p in procs
            if p["pid"] not in protected and is_redline_tray(p, image)
            and (session is None or p.get("session") is None or p.get("session") == session)]


def stuck_pids(previous: int | None = None) -> list[int]:
    """Other redline tray processes (see pick_stuck); `previous` = the instance that started
    us during a self-update."""
    mine = {os.getpid(), os.getppid()}
    if sys.platform != "win32":
        st = read_state()
        return [st["pid"]] if st and st.get("pid") not in mine else []
    procs = _processes()
    if procs is None:
        return []
    image = Path(sys.executable).name if getattr(sys, "frozen", False) else ""
    return [pid for pid in pick_stuck(procs, os.getpid(), previous, image) if pid not in mine]


def kill(pids: list[int]) -> None:
    """End exactly these processes. No tree kill (/T): the stuck instance's children are the
    user's terminals and editors, and after a self-update its descendant is the new us."""
    for pid in pids:
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=15, **_no_window())
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
