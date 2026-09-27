"""Find Claude Code / Codex account directories that already exist on this machine.

Sources, cheapest first:
  1. the CLIs' default homes (~/.claude, ~/.codex)
  2. CLAUDE_CONFIG_DIR / CODEX_HOME in the current environment
  3. the same variables set in shell profiles / aliases (bash, zsh, fish, PowerShell)
  4. ~/.claude* / ~/.codex* style sibling dirs
  5. a shallow, time-boxed walk of the home directory for credential files
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .config import DEFAULT_HOMES, HOME_ENV_VARS, Account
from .providers import has_credentials, identity
from .providers._util import read_json

WALK_DEPTH = 4
WALK_BUDGET = 4.0  # seconds
SKIP_DIRS = {
    "node_modules", ".git", ".hg", ".svn", "AppData", "Library", ".cache", ".npm", ".yarn", ".pnpm-store",
    ".cargo", ".rustup", ".venv", "venv", "env", "__pycache__", ".gradle", ".m2", ".nuget", ".vscode",
    ".vscode-server", ".local", "go", "Pictures", "Music", "Videos", "Movies", ".Trash", "snap",
    "OneDrive", "Dropbox", "iCloud Drive", ".docker", ".android", "site-packages", "dist", "build",
}
_ENV_RE = re.compile(
    r"""(?:\$env:)?(CLAUDE_CONFIG_DIR|CODEX_HOME)\s*=\s*(?:"([^"]+)"|'([^']+)'|([^\s;&|`"']+))"""
)
PROVIDER_OF_VAR = {v: k for k, v in HOME_ENV_VARS.items()}


@dataclass
class Found:
    provider: str
    home: Path
    email: str | None
    logged_in: bool
    source: str

    def to_dict(self) -> dict:
        return {"provider": self.provider, "home": str(self.home), "email": self.email,
                "logged_in": self.logged_in, "source": self.source}


def _expand(raw: str) -> Path:
    home = str(Path.home())
    s = raw.strip()
    for token in ("$HOME", "${HOME}", "$env:USERPROFILE", "%USERPROFILE%", "$env:HOME"):
        s = s.replace(token, home)
    return Path(os.path.expandvars(s)).expanduser()


def _profile_files() -> list[Path]:
    h = Path.home()
    files = [h / n for n in (".bashrc", ".bash_profile", ".bash_aliases", ".profile", ".zshrc", ".zprofile",
                             ".zshenv", ".config/fish/config.fish")]
    files += list((h / ".config/fish/conf.d").glob("*.fish"))
    for docs in (h / "Documents", h / "OneDrive/Documents", h / "OneDrive/文档"):
        files += [docs / "PowerShell/Microsoft.PowerShell_profile.ps1",
                  docs / "WindowsPowerShell/Microsoft.PowerShell_profile.ps1"]
    return [f for f in files if f.is_file()]


def _from_profiles() -> list[tuple[str, Path, str]]:
    out = []
    for f in _profile_files():
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _ENV_RE.finditer(text):
            raw = m.group(2) or m.group(3) or m.group(4)
            if raw and "$" not in raw.replace("$HOME", "").replace("${HOME}", "").replace("$env:", ""):
                out.append((PROVIDER_OF_VAR[m.group(1)], _expand(raw), f"profile:{f.name}"))
    return out


def looks_like(provider: str, d: Path) -> bool:
    """Is `d` a logged-in (or previously logged-in) account home for `provider`?"""
    if provider == "claude":
        if (d / ".credentials.json").is_file():
            doc = read_json_safe(d / ".credentials.json")
            if doc and doc.get("claudeAiOauth"):
                return True
        doc = read_json_safe(d / ".claude.json")
        return bool(doc and doc.get("oauthAccount"))
    doc = read_json_safe(d / "auth.json")
    tokens = (doc or {}).get("tokens") or {}
    # Codex's auth.json specifically: ChatGPT tokens + its own bookkeeping keys.
    return bool(tokens.get("access_token") and tokens.get("id_token")
                and ("OPENAI_API_KEY" in doc or "last_refresh" in doc or "auth_mode" in doc))


def read_json_safe(p: Path) -> dict | None:
    try:
        doc = read_json(p)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _walk(root: Path, deadline: float):
    """Yield (provider, dir) for credential files under root, depth/time bounded."""
    stack = [(root, 0)]
    while stack and time.monotonic() < deadline:
        d, depth = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        names = {e.name for e in entries}
        if d != root:
            if ".credentials.json" in names or ".claude.json" in names:
                yield "claude", Path(d)
            if "auth.json" in names:
                yield "codex", Path(d)
        if depth >= WALK_DEPTH:
            continue
        for e in entries:
            if e.name in SKIP_DIRS or e.name.endswith(".lock"):
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append((Path(e.path), depth + 1))
            except OSError:
                continue


def discover(walk: bool = True) -> list[Found]:
    home = Path.home()
    cands: list[tuple[str, Path, str]] = []
    for provider, d in DEFAULT_HOMES.items():
        cands.append((provider, Path(d).expanduser(), "default"))
    for provider, var in HOME_ENV_VARS.items():
        if os.environ.get(var):
            cands.append((provider, _expand(os.environ[var]), f"env:{var}"))
    cands += _from_profiles()
    for pattern, provider in ((".claude*", "claude"), (".codex*", "codex"),
                              (".config/claude*", "claude"), (".config/codex*", "codex")):
        for d in home.glob(pattern):
            if d.is_dir() and not d.name.endswith(".lock"):
                cands.append((provider, d, "home"))
    if walk:
        deadline = time.monotonic() + WALK_BUDGET
        cands += [(p, d, "scan") for p, d in _walk(home, deadline)]

    found: list[Found] = []
    seen: set[tuple[str, str]] = set()
    for provider, d, source in cands:
        try:
            key = (provider, str(d.resolve()))
        except OSError:
            continue
        if key in seen or not d.is_dir():
            continue
        seen.add(key)
        default = d.resolve() == Path(DEFAULT_HOMES[provider]).expanduser().resolve()
        probe = Account(name="probe", provider=provider, home=str(d))
        logged_in = has_credentials(probe) and (provider != "codex" or looks_like("codex", d))
        if not (logged_in or looks_like(provider, d) or (default and provider == "claude" and _default_claude_known())):
            continue
        found.append(Found(provider, d, identity(probe), logged_in, source))
    return found


def _default_claude_known() -> bool:
    doc = read_json_safe(Path("~/.claude.json").expanduser())
    return bool(doc and doc.get("oauthAccount"))


def suggest_name(f: Found, taken: set[str]) -> str:
    if f.home == Path(DEFAULT_HOMES[f.provider]).expanduser():
        base = f.provider
    else:
        base = f.home.name.lstrip(".") or f.provider
        if base.lower() in ("claude", "codex", "config", ".claude", ".codex"):
            base = f"{f.provider}-{f.home.parent.name.lstrip('.')}"
        if f.provider not in base.lower():
            base = f"{f.provider}-{base}"
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-._") or f.provider
    name, i = base, 2
    while name in taken:
        name, i = f"{base}-{i}", i + 1
    return name
