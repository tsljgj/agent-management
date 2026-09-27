"""Account registry, stored as JSON in $REDLINE_HOME/config.json (default ~/.redline)."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

PROVIDERS = ("claude", "codex")

# Where each CLI keeps its state when no override env var is set.
DEFAULT_HOMES = {
    "claude": "~/.claude",
    "codex": "~/.codex",
}

# Env var each CLI reads to relocate its state directory (=> one dir per account).
HOME_ENV_VARS = {
    "claude": "CLAUDE_CONFIG_DIR",
    "codex": "CODEX_HOME",
}

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def redline_home() -> Path:
    if os.environ.get("REDLINE_HOME"):
        return Path(os.environ["REDLINE_HOME"]).expanduser()
    home = Path("~/.redline").expanduser()
    legacy = Path("~/.agentman").expanduser()  # location before the rename
    if not home.exists() and (legacy / "config.json").exists():
        return legacy
    return home


def config_path() -> Path:
    return redline_home() / "config.json"


@dataclass
class Account:
    name: str
    provider: str
    home: str  # the CLAUDE_CONFIG_DIR / CODEX_HOME of this account
    note: str = ""
    browser_profile: str = ""  # e.g. "chrome:Profile 2"; empty = match by email

    @property
    def home_path(self) -> Path:
        return Path(self.home).expanduser()

    @property
    def is_default_home(self) -> bool:
        return self.home_path == Path(DEFAULT_HOMES[self.provider]).expanduser()

    def env(self) -> dict[str, str]:
        """Env vars that point the provider's CLI at this account."""
        return {HOME_ENV_VARS[self.provider]: str(self.home_path)}


def validate_name(name: str) -> None:
    if not _NAME_RE.match(name):
        raise ValueError(f"invalid account name {name!r} (use letters, digits, . _ -)")


DEFAULT_SETTINGS = {
    "auto_refresh": True,  # tray/serve: refresh expired tokens (with the CLI's lock) and write them back
    "interval": 120,
    "auto_update": True,  # packaged exe: install new GitHub releases automatically
}


def _load_raw() -> dict:
    path = config_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_raw(data: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_accounts() -> list[Account]:
    fields = set(Account.__dataclass_fields__)
    return [Account(**{k: v for k, v in a.items() if k in fields}) for a in _load_raw().get("accounts", [])]


def save_accounts(accounts: list[Account]) -> None:
    data = _load_raw()
    data["accounts"] = [asdict(a) for a in accounts]
    _save_raw(data)


def load_removed() -> list[Account]:
    """Accounts the user removed; `scan` must not bring them back."""
    fields = set(Account.__dataclass_fields__)
    return [Account(**{k: v for k, v in a.items() if k in fields}) for a in _load_raw().get("removed", [])]


def save_removed(removed: list[Account]) -> None:
    data = _load_raw()
    data["removed"] = [asdict(a) for a in removed]
    _save_raw(data)


def load_settings() -> dict:
    return {**DEFAULT_SETTINGS, **_load_raw().get("settings", {})}


def save_setting(key: str, value) -> None:
    data = _load_raw()
    data.setdefault("settings", {})[key] = value
    _save_raw(data)


def find_account(accounts: list[Account], name: str) -> Account:
    for a in accounts:
        if a.name == name:
            return a
    raise KeyError(f"no account named {name!r}")
