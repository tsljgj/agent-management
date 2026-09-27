"""Locate the `claude` / `codex` executables even when they aren't on our PATH.

The tray app can be started by autostart or a self-update with a stale PATH, and
Codex is often only installed inside an IDE extension. So besides `which` we
re-read the user's current PATH from the registry and look in the usual install
locations (npm/pnpm/bun/volta/scoop/nvm, native installers, VS Code/Cursor extensions).
"""

from __future__ import annotations

import glob
import os
import shutil
import sys
from pathlib import Path


def fresh_path() -> str:
    """The PATH a newly started program would get (Windows: re-read from the registry)."""
    if sys.platform != "win32":
        return os.environ.get("PATH", "")
    import winreg

    parts = []
    for root, key in ((winreg.HKEY_CURRENT_USER, r"Environment"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(root, key) as k:
                value, _ = winreg.QueryValueEx(k, "Path")
                parts.append(os.path.expandvars(value))
        except OSError:
            continue
    parts.append(os.environ.get("PATH", ""))
    seen, out = set(), []
    for p in ";".join(parts).split(";"):
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return ";".join(out)


def _candidates(name: str) -> list[str]:
    home = Path.home()
    if sys.platform == "win32":
        appdata = Path(os.environ.get("APPDATA", home / "AppData/Roaming"))
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local"))
        dirs = [appdata / "npm", local / "pnpm", home / ".bun/bin", home / ".local/bin", local / "Volta/bin",
                home / "scoop/shims", Path(os.environ.get("NVM_SYMLINK", r"C:\Program Files\nodejs")),
                local / "Programs" / name, local / "Microsoft/WinGet/Links"]
        exts = (".exe", ".cmd", ".bat")
        files = [str(d / (name + e)) for d in dirs for e in exts]
    else:
        dirs = [home / ".local/bin", home / ".npm-global/bin", home / ".bun/bin", home / ".volta/bin",
                Path("/opt/homebrew/bin"), Path("/usr/local/bin"), home / ".claude/local"]
        files = [str(d / name) for d in dirs]
    if name == "code" and sys.platform == "win32":
        for base in (local / "Programs", Path(os.environ.get("ProgramFiles", r"C:\Program Files"))):
            files += [str(base / "Microsoft VS Code" / "bin" / "code.cmd"),
                      str(base / "Microsoft VS Code Insiders" / "bin" / "code-insiders.cmd"),
                      str(base / "cursor" / "resources" / "app" / "bin" / "cursor.cmd")]
    if name == "codex":  # the CLI bundled with the ChatGPT/Codex IDE extension
        exe = "codex.exe" if sys.platform == "win32" else "codex"
        for ide in (".vscode", ".vscode-insiders", ".cursor", ".windsurf"):
            hits = glob.glob(str(home / ide / "extensions" / "openai.chatgpt-*" / "bin" / "*" / exe))
            files += sorted(hits, key=os.path.getmtime, reverse=True)
    return files


def find_cli(name: str) -> str | None:
    found = shutil.which(name) or shutil.which(name, path=fresh_path())
    if found:
        return found
    for f in _candidates(name):
        if os.path.isfile(f):
            return f
    return None


def cli_env(exe: str | None, env: dict[str, str]) -> dict[str, str]:
    """Child env whose PATH is current and contains the CLI's own dir (npm shims need node next to them)."""
    path = fresh_path() if sys.platform == "win32" else env.get("PATH", os.environ.get("PATH", ""))
    if exe:
        path = str(Path(exe).parent) + os.pathsep + path
    return {**env, "PATH": path}
