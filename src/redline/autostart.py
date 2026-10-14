"""'Start with Windows' toggle via HKCU\\...\\Run (no admin rights needed)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE = "redline"


def supported() -> bool:
    return sys.platform == "win32"


def command() -> str:
    if getattr(sys, "frozen", False):  # PyInstaller exe
        return f'"{sys.executable}" --background'
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    return f'"{pythonw if pythonw.exists() else exe}" -m redline tray --background'


def is_enabled() -> bool:
    if not supported():
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, VALUE)
        return True
    except OSError:
        return False


def registered() -> str | None:
    """The command in the Run key now, or None."""
    if not supported():
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            value = winreg.QueryValueEx(k, VALUE)[0]
    except OSError:
        return None
    return value if isinstance(value, str) else None


def _program(cmd: str) -> str:
    """The program a Run command starts (its first, maybe quoted, word), comparable as a path."""
    cmd = cmd.strip()
    prog = cmd[1:].split('"', 1)[0] if cmd.startswith('"') else (cmd.split(None, 1) or [""])[0]
    return os.path.normcase(os.path.normpath(prog)) if prog else ""


def refresh() -> bool:
    """Bring an existing entry for *this* copy up to date (older builds wrote it without
    --background). An entry that starts another copy - the installed exe while a freshly
    downloaded one or a source checkout runs - is left alone. True if it was rewritten."""
    cur, want = registered(), command()
    if cur is None or cur == want or _program(cur) != _program(want):
        return False
    set_enabled(True)
    return True


def set_enabled(on: bool) -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, VALUE, 0, winreg.REG_SZ, command())
        else:
            try:
                winreg.DeleteValue(k, VALUE)
            except FileNotFoundError:
                pass
