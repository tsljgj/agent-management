"""Environment for processes we start (terminals, CLIs, browsers, the updated exe).

A PyInstaller one-file exe unpacks itself into a temp dir and advertises that dir
to its children through `_PYI_*` / `_MEIPASS2` variables. If we start another
copy of redline.exe (after a self-update, or as the $BROWSER helper) with those
inherited, it reuses *our* temp dir instead of unpacking its own - and dies as
soon as we exit and that dir is deleted. PYINSTALLER_RESET_ENVIRONMENT=1 tells
the child's bootloader to start as an independent instance.
"""

from __future__ import annotations

import os
import sys


def child_env(extra: dict | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not (k.startswith("_PYI_") or k.startswith("_MEIPASS"))}
    if getattr(sys, "frozen", False):
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    for k, v in (extra or {}).items():
        if v is None:  # None = make sure the child does *not* see this variable
            env.pop(k, None)
        else:
            env[k] = v
    return env
