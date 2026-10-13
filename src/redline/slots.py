"""The default login ("slot") and switching which account sits in it.

Everything that doesn't point at a specific account dir uses the CLI's default
home: plain `claude` / `codex` in any terminal, and the VS Code extensions (the
Claude Code extension ignores CLAUDE_CONFIG_DIR, see anthropics/claude-code#30538).
So "use account X in VS Code" means: put X's login into ~/.claude (or ~/.codex).

Refresh tokens rotate, so two copies of one login invalidate each other. We
therefore *move* logins: the slot's current login goes back to its owner's own
dir, X's login moves into the slot, and X's own dir is left without one while X
is the default. `effective(account)` says where an account's login lives now;
the providers, the wake job and the terminals all go through it.

VS Code's Codex / Claude panels and plain `codex login` / `claude /login` write
straight into the slot, so it can end up holding someone else's login. `reconcile`
notices that (by email) and makes whoever is really there the default, so the
next switch never files that login under the previous owner's name.

State ($REDLINE_HOME/slots.json):
  {"claude": {"home": "<owner's own dir>", "since": ts, "email": "<who was put in the slot>",
              "history": [[home, from, to], ...]}}
"""

from __future__ import annotations

import dataclasses
import json
import re
import sys
import threading
import time
from pathlib import Path

from .config import DEFAULT_HOMES, Account, load_accounts, redline_home, save_accounts
from .models import ProviderError
from .providers._util import cli_lock, read_json, write_json_atomic

LOGIN_FILES = {"claude": ".credentials.json", "codex": "auth.json"}
HISTORY = 60
_lock = threading.Lock()


def slot_dir(provider: str) -> Path:
    return Path(DEFAULT_HOMES[provider]).expanduser()


def _resolved(p: Path | str) -> str:
    try:
        return str(Path(p).expanduser().resolve())
    except OSError:
        return str(p)


def _state_path() -> Path:
    return redline_home() / "slots.json"


def load_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def managed(provider: str) -> bool:
    """True once redline has switched this provider's default login at least once."""
    return bool(load_state().get(provider, {}).get("home"))


def is_active(account: Account, state: dict | None = None) -> bool:
    cur = (state if state is not None else load_state()).get(account.provider, {}).get("home")
    return bool(cur) and cur == _resolved(account.home_path)


def effective(account: Account) -> Account:
    """The account as the CLIs see it right now (home = the slot while it is the default)."""
    if is_active(account):
        return dataclasses.replace(account, home=str(slot_dir(account.provider)))
    return account


def is_slot(provider: str, home: Path | str) -> bool:
    return _resolved(home) == _resolved(slot_dir(provider))


def active_periods(account: Account) -> list[tuple[float, float]]:
    """When this account was the default ([from, to]; the current period ends now)."""
    st = load_state().get(account.provider, {})
    me = _resolved(account.home_path)
    out = [(f, t) for h, f, t in st.get("history", []) if h == me]
    if st.get("home") == me:
        out.append((st.get("since", 0), time.time() + 60))
    return out


# ------------------------------------------------------------ identity file (Claude)


def _claude_config(home: Path) -> Path:
    """Where Claude Code keeps oauthAccount (the email it shows) for this home."""
    return Path("~/.claude.json").expanduser() if is_slot("claude", home) else home / ".claude.json"


def _copy_oauth_account(src_home: Path, dst_home: Path) -> None:
    src = read_json_safe(_claude_config(src_home)) or {}
    acct = src.get("oauthAccount")
    if not acct:
        return
    dst_path = _claude_config(dst_home)
    dst = read_json_safe(dst_path) or {}
    dst["oauthAccount"] = acct
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(dst_path, dst)


def read_json_safe(p: Path) -> dict | None:
    try:
        return read_json(p)
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------ whose login is where


def email_in(provider: str, home: Path | str) -> str | None:
    """Email of the login stored in `home` itself (local files; never redirected to the slot)."""
    from .providers import _MODULES

    try:
        return _MODULES[provider].identity(Account("probe", provider, str(home)))
    except Exception:
        return None


def _same(a: str | None, b: str | None) -> bool:
    return bool(a and b) and a.strip().lower() == b.strip().lower()


def _labelled(accounts: list[Account], provider: str, email: str | None) -> Account | None:
    """The account meant to hold `email` (by its configured email, not its current login)."""
    from .login import expected_email

    return next((a for a in accounts if a.provider == provider and _same(expected_email(a), email)), None)


def _stash(provider: str, home: Path) -> Path | None:
    """Keep a copy of the login in `home` before it is overwritten: it may be the only one."""
    login = read_json_safe(home / LOGIN_FILES[provider])
    if not login:
        return None
    email = re.sub(r"[^A-Za-z0-9._@+-]+", "-", email_in(provider, home) or "unknown")
    d = redline_home() / "stash"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{provider}-{email}-{time.strftime('%Y%m%dT%H%M%S')}.json"
    write_json_atomic(p, login)
    return p


def reconcile() -> list[str]:
    """Follow logins that replaced the default one outside redline. Returns what changed."""
    with _lock:
        return [m for m in (_reconcile(p) for p in LOGIN_FILES) if m]


def _reconcile(provider: str) -> str | None:
    state = load_state()
    st = state.get(provider) or {}
    cur = st.get("home")
    if not cur:
        return None
    there = email_in(provider, slot_dir(provider))
    if not there:
        return None  # logged out: nothing to file under anyone
    from .login import expected_email

    accounts = load_accounts()
    owner = next((a for a in accounts if a.provider == provider and _resolved(a.home_path) == cur), None)
    meant = st.get("email") or (expected_email(owner) if owner else None)
    if not meant or _same(meant, there):
        if st.get("email") != there:
            st["email"] = there  # older state without it: remember who is there now
            _save_state(state)
        return None
    # Someone logged a different account in (VS Code, `codex login`, `claude /login`).
    who = _labelled(accounts, provider, there)
    if who is not None and owner is not None and who.key == owner.key:
        st["email"] = there  # the owner's own email showed up late (Claude caches it lazily)
        _save_state(state)
        return None
    if who is None:
        from .actions import add_account, unique_name

        taken = {a.name for a in accounts if a.provider == provider}
        who = add_account(provider, unique_name(there, taken), home=str(_new_home(provider, there)),
                          note=there, auto_name=True)
    now = time.time()
    hist = st.setdefault("history", [])
    hist.append([cur, st.get("since", 0), now])
    st["history"] = hist[-HISTORY:]
    st["home"], st["since"], st["email"] = _resolved(who.home_path), now, there
    state[provider] = st
    _save_state(state)
    msg = (f"the default {provider} login was replaced outside redline (VS Code or `{provider} login`): "
           f"it is {there} now, so {who.name} is the default")
    if owner is not None and not read_json_safe(owner.home_path / LOGIN_FILES[provider]):
        msg += f"; {owner.name} lost its login there — log it in again"
    return msg


# ------------------------------------------------------------ switching


def _new_home(provider: str, name: str) -> Path:
    import re

    base = redline_home() / "accounts" / f"{provider}-{re.sub(r'[^A-Za-z0-9._@+-]+', '-', name)}"
    home, i = base, 2
    while home.exists() and any(home.iterdir()):
        home, i = Path(f"{base}-{i}"), i + 1
    return home


def activate(key: str) -> str:
    """Make account `key` ("claude:name") the default login. Returns a status message."""
    with _lock:
        return _activate(key)


def _activate(key: str) -> str:
    provider, _, name = key.partition(":")
    if provider not in LOGIN_FILES:
        raise ProviderError(f"unknown provider {provider!r}")
    if provider == "claude" and sys.platform == "darwin":
        raise ProviderError("switching the default login isn't supported on macOS (logins live in the Keychain)")
    _reconcile(provider)  # whatever is in the slot now must go back to its real owner
    accounts = load_accounts()
    acct = next((a for a in accounts if a.key == key), None)
    if acct is None:
        raise ProviderError(f"no account {key!r}")
    state = load_state()
    slot, fname = slot_dir(provider), LOGIN_FILES[provider]
    if is_active(acct, state) or (not state.get(provider, {}).get("home") and is_slot(provider, acct.home_path)):
        return f"{acct.name} is already the default {provider} login"
    src = acct.home_path / fname
    login = read_json_safe(src)
    if not login:
        raise ProviderError(f"{acct.name} is not logged in; log it in first")
    from .login import expected_email

    incoming, meant = email_in(provider, acct.home_path), expected_email(acct)
    if incoming and meant and not _same(incoming, meant):
        raise ProviderError(f"{acct.name} holds the login of {incoming}, not {meant}; "
                            f"log {acct.name} in again with the right account first")

    with cli_lock(slot), cli_lock(acct.home_path):
        cur = state.get(provider, {}).get("home")
        owner_home = Path(cur) if cur else None
        if owner_home is None or is_slot(provider, owner_home):
            # First switch: the slot is some account's own dir. Give that account a dir of its own.
            owner = next((a for a in accounts if a.provider == provider and is_slot(provider, a.home_path)), None)
            current = read_json_safe(slot / fname)
            email = email_in(provider, slot)
            if owner is not None:
                owner_home = _new_home(provider, owner.name)
                owner.home = str(owner_home)
                save_accounts(accounts)
            elif current and _labelled(accounts, provider, email) is not None:
                # A second login of an account we already have: keep a copy, don't list it twice.
                _stash(provider, slot)
                owner_home = None
            elif current:
                # An unregistered login sits in the slot: keep it as a new account.
                from .actions import add_account, unique_name

                taken = {a.name for a in accounts if a.provider == provider}
                owner_home = _new_home(provider, email or f"{provider}-default")
                add_account(provider, unique_name(email or f"{provider}-default", taken),
                            home=str(owner_home), note=email or "", auto_name=True)
            else:
                owner_home = None
        current = read_json_safe(slot / fname)
        if owner_home is not None and current:
            owner_home.mkdir(parents=True, exist_ok=True)
            if read_json_safe(owner_home / fname) and not _same(email_in(provider, owner_home), email_in(provider, slot)):
                _stash(provider, owner_home)
            write_json_atomic(owner_home / fname, current)
            if provider == "claude":
                _copy_oauth_account(slot, owner_home)
        slot.mkdir(parents=True, exist_ok=True)
        write_json_atomic(slot / fname, login)
        if provider == "claude":
            _copy_oauth_account(acct.home_path, slot)
        src.unlink()

    now = time.time()
    st = state.setdefault(provider, {})
    hist = st.setdefault("history", [])
    if st.get("home"):
        hist.append([st["home"], st.get("since", 0), now])
    elif owner_home is not None:
        hist.append([_resolved(owner_home), 0, now])  # everything in the slot so far was theirs
    st["history"] = hist[-HISTORY:]
    st["home"], st["since"] = _resolved(acct.home_path), now
    there = email_in(provider, slot)  # what the slot reports now; `reconcile` watches for a change
    if there:
        st["email"] = there
    else:
        st.pop("email", None)
    _save_state(state)
    return f"{acct.name} is now the default {provider} login"
