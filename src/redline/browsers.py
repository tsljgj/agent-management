"""Chromium-family browser profiles (Chrome / Edge / Brave / Chromium).

Every Chrome profile is usually signed in to one Google account; its email lives in
`<User Data>/Local State` -> profile.info_cache[<dir>].user_name. We match an agent
account's email against that to open claude.ai / chatgpt.com in the right profile.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

URLS = {
    "claude": "https://claude.ai/new",
    "codex": "https://chatgpt.com/codex",
}
USAGE_URLS = {
    "claude": "https://claude.ai/settings/usage",
    "codex": "https://chatgpt.com/codex/settings/usage",
}


@dataclass
class Browser:
    key: str  # "chrome", "edge", ...
    label: str
    user_data: Path
    exe: str | None


@dataclass
class Profile:
    browser: str
    directory: str  # "Default", "Profile 2"
    name: str  # display name shown in Chrome
    email: str  # Google account signed in to the profile ("" if none)

    @property
    def spec(self) -> str:
        return f"{self.browser}:{self.directory}"

    def to_dict(self) -> dict:
        return {"spec": self.spec, "browser": self.browser, "directory": self.directory,
                "name": self.name, "email": self.email}


def _win_app_path(exe_name: str) -> str | None:
    try:
        import winreg
    except ImportError:
        return None
    key = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, key) as k:
                path = winreg.QueryValue(k, None)
                if path and Path(path).exists():
                    return path
        except OSError:
            continue
    return None


def _first_existing(paths) -> str | None:
    for p in paths:
        if p and Path(p).exists():
            return str(p)
    return None


def _candidates() -> list[tuple[str, str, Path, list]]:
    """(key, label, user data dir, exe candidates) for this OS."""
    home = Path.home()
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        pf = [os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), str(local)]

        def under(*parts):
            return [Path(base, *parts) for base in pf if base]

        return [
            ("chrome", "Google Chrome", local / "Google/Chrome/User Data",
             [_win_app_path("chrome.exe"), *under("Google/Chrome/Application/chrome.exe")]),
            ("edge", "Microsoft Edge", local / "Microsoft/Edge/User Data",
             [_win_app_path("msedge.exe"), *under("Microsoft/Edge/Application/msedge.exe")]),
            ("brave", "Brave", local / "BraveSoftware/Brave-Browser/User Data",
             [_win_app_path("brave.exe"), *under("BraveSoftware/Brave-Browser/Application/brave.exe")]),
        ]
    if sys.platform == "darwin":
        sup = home / "Library/Application Support"
        return [
            ("chrome", "Google Chrome", sup / "Google/Chrome",
             ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]),
            ("edge", "Microsoft Edge", sup / "Microsoft Edge",
             ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]),
            ("brave", "Brave", sup / "BraveSoftware/Brave-Browser",
             ["/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"]),
            ("chromium", "Chromium", sup / "Chromium", ["/Applications/Chromium.app/Contents/MacOS/Chromium"]),
        ]
    cfg = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    w = shutil.which
    return [
        ("chrome", "Google Chrome", cfg / "google-chrome", [w("google-chrome"), w("google-chrome-stable")]),
        ("chromium", "Chromium", cfg / "chromium", [w("chromium"), w("chromium-browser")]),
        ("edge", "Microsoft Edge", cfg / "microsoft-edge", [w("microsoft-edge"), w("microsoft-edge-stable")]),
        ("brave", "Brave", cfg / "BraveSoftware/Brave-Browser", [w("brave-browser"), w("brave")]),
    ]


def browsers() -> list[Browser]:
    out = []
    for key, label, user_data, exes in _candidates():
        if (user_data / "Local State").exists():
            out.append(Browser(key, label, user_data, _first_existing(exes)))
    return out


def read_profiles(browser: Browser) -> list[Profile]:
    try:
        state = json.loads((browser.user_data / "Local State").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    cache = (state.get("profile") or {}).get("info_cache") or {}
    profiles = []
    for directory, info in cache.items():
        if not isinstance(info, dict):
            continue
        profiles.append(Profile(
            browser=browser.key,
            directory=directory,
            name=info.get("name") or info.get("gaia_name") or directory,
            email=(info.get("user_name") or "").strip(),
        ))
    profiles.sort(key=lambda p: (p.directory != "Default", p.directory))
    return profiles


def all_profiles() -> list[Profile]:
    return [p for b in browsers() for p in read_profiles(b)]


def find_profile(spec_or_email: str, profiles: list[Profile] | None = None) -> Profile | None:
    """Resolve "chrome:Profile 2", "Profile 2", a profile display name, or a Google email."""
    profiles = all_profiles() if profiles is None else profiles
    want = spec_or_email.strip()
    low = want.lower()
    for p in profiles:
        if p.spec.lower() == low:
            return p
    if "@" in want:
        for p in profiles:
            if p.email.lower() == low:
                return p
        return None
    for p in profiles:
        if p.directory.lower() == low or p.name.lower() == low:
            return p
    return None


def open_in_profile(profile: Profile, url: str) -> None:
    b = next((b for b in browsers() if b.key == profile.browser), None)
    if b is None or not b.exe:
        raise FileNotFoundError(f"{profile.browser} executable not found")
    kwargs = {"start_new_session": True} if sys.platform != "win32" else {
        "creationflags": getattr(subprocess, "DETACHED_PROCESS", 0)}
    from .procenv import child_env

    subprocess.Popen(
        [b.exe, f"--profile-directory={profile.directory}", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=child_env(), **kwargs,
    )
