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

A switch reads, checks and moves everything while holding the CLIs' own locks on
both dirs (a refresh in flight must finish first), and never moves a login on a
guess: an unreadable slots.json or .claude.json stops it instead.

State ($REDLINE_HOME/slots.json, last good copy in slots.bak):
  {"claude": {"home": "<owner's own dir>", "since": ts, "email": "<who was put in the slot>",
              "history": [[home, from, to], ...]}}
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

from .config import DEFAULT_HOMES, EDIT_LOCK, Account, load_accounts, redline_home, save_accounts
from .models import ProviderError
from .providers._util import cli_lock, read_json, write_json_atomic

LOGIN_FILES = {"claude": ".credentials.json", "codex": "auth.json"}
HISTORY = 60
_lock = EDIT_LOCK  # switches, reconciles and config edits never interleave in this process


def slot_dir(provider: str) -> Path:
    return Path(DEFAULT_HOMES[provider]).expanduser()


def _resolved(p: Path | str) -> str:
    try:
        return str(Path(p).expanduser().resolve())
    except OSError:
        return str(p)


def _state_path() -> Path:
    return redline_home() / "slots.json"


def _read_state(p: Path) -> dict | None:
    """None when the file is missing; raises OSError / ValueError when it can't be used."""
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    doc = json.loads(text)
    if not isinstance(doc, dict):
        raise ValueError(f"{p} is not a JSON object")
    return doc


def load_state(strict: bool = False) -> dict:
    """slots.json; a damaged file falls back to its last good copy.

    Without either, `strict` raises: treating the provider as "never switched" would
    make the next switch file the default account's login as someone else's.
    """
    p = _state_path()
    try:
        doc = _read_state(p)
        return doc if doc is not None else {}
    except (OSError, ValueError):
        pass
    try:
        doc = _read_state(p.with_suffix(".bak"))
        if doc is not None:
            return doc
    except (OSError, ValueError):
        pass
    if strict:
        raise ProviderError(f"{p} is damaged; redline won't move logins until it is fixed or deleted")
    return {}


def _save_state(state: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(state, indent=2, ensure_ascii=False)
    for f in (p, p.with_suffix(".bak")):
        tmp = f.with_name(f"{f.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(data, encoding="utf-8")
        tmp.replace(f)


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
    return Path("~/.claude.json").expanduser() if is_slot("claude", home) else Path(home) / ".claude.json"


def _read_doc(p: Path) -> dict | None:
    """A JSON object file: None if it is missing; an error if it exists but can't be parsed.

    Rewriting an unparsable ~/.claude.json would wipe the user's MCP servers and project settings.
    """
    try:
        doc = read_json(p)
    except (OSError, ValueError):
        raise ProviderError(f"{p} can't be read right now (being written?); try again") from None
    if doc is not None and not isinstance(doc, dict):
        raise ProviderError(f"{p} is not a JSON object; fix it before switching")
    return doc


def _copy_oauth_account(src_home: Path, dst_home: Path) -> None:
    """Make dst show src's email. When src has none, drop dst's: a stale one names the wrong person."""
    acct = (_read_doc(_claude_config(src_home)) or {}).get("oauthAccount")
    dst_path = _claude_config(dst_home)
    dst = _read_doc(dst_path)
    if acct:
        dst = dst or {}
        dst["oauthAccount"] = acct
    elif dst and "oauthAccount" in dst:
        del dst["oauthAccount"]
    else:
        return
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(dst_path, dst)


def read_json_safe(p: Path) -> dict | None:
    try:
        return read_json(p)
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------ whose login is where


def email_in(provider: str, home: Path | str) -> str | None:
    """Email of the login stored in `home` itself (local files; never redirected to the slot).

    Claude: always the file Claude Code itself uses for that dir, whatever redline's own
    environment says (redline unsets CLAUDE_CONFIG_DIR for the default account).
    """
    home = Path(home).expanduser()
    if provider == "claude":
        for p in (_claude_config(home), home / ".config.json"):
            doc = read_json_safe(p)
            acct = doc.get("oauthAccount") if isinstance(doc, dict) else None
            if isinstance(acct, dict) and acct.get("emailAddress"):
                return acct["emailAddress"]
        return None
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


def _has_login(provider: str, home: Path) -> bool:
    return bool(read_json_safe(Path(home) / LOGIN_FILES[provider]))


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


def _home_for_slot_login(provider: str, accounts: list[Account], email: str | None,
                         skip: str | None = None) -> Path | None:
    """Where the login now in the slot belongs when it is not the recorded owner's.

    The account meant for that email gets it back if it has no login of its own; if it has
    one, this is a second copy and is stashed. An email nobody is meant for becomes an account.
    """
    slot = slot_dir(provider)
    twin = _labelled(accounts, provider, email)
    if twin is not None and twin.key != skip:
        if not _has_login(provider, twin.home_path):
            return twin.home_path
        _stash(provider, slot)
        return None
    from .actions import add_account, unique_name

    base = email or f"{provider}-default"
    home = _new_home(provider, base)
    add_account(provider, unique_name(base, {a.name for a in accounts if a.provider == provider}),
                home=str(home), note=email or "", auto_name=True)
    return home


def reconcile() -> list[str]:
    """Follow logins that replaced the default one outside redline. Returns what changed."""
    out = []
    with _lock:
        for provider in LOGIN_FILES:
            try:
                msg = _reconcile(provider)
            except Exception:  # damaged state, a busy lock...: try again next sync
                continue
            if msg:
                out.append(msg)
    return out


def _reconcile(provider: str) -> str | None:
    from .login import expected_email

    state = load_state(strict=True)
    st = state.get(provider) or {}
    cur = st.get("home")
    if not cur:
        return None
    there = email_in(provider, slot_dir(provider))
    if not there:
        return None  # logged out (or no email cached yet): nothing to file under anyone
    accounts = load_accounts()
    owner = next((a for a in accounts if a.provider == provider and _resolved(a.home_path) == cur), None)
    meant = st.get("email") or (expected_email(owner) if owner else None)
    if not meant or _same(meant, there):
        if st.get("email") != there:
            st["email"] = there  # first sight of the email (older state, or Claude cached it late)
            state[provider] = st
            _save_state(state)
        return None
    # Someone logged a different account in (VS Code, `codex login`, `claude /login`).
    who = _labelled(accounts, provider, there)
    if who is not None and owner is not None and who.key == owner.key:
        st["email"] = there
        state[provider] = st
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
    if owner is not None and not _has_login(provider, owner.home_path):
        msg += f"; {owner.name} lost its login there — log it in again"
    return msg


# ------------------------------------------------------------ switching


def _new_home(provider: str, name: str) -> Path:
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
    from .login import expected_email, running

    provider, _, name = key.partition(":")
    if provider not in LOGIN_FILES:
        raise ProviderError(f"unknown provider {provider!r}")
    if provider == "claude" and sys.platform == "darwin":
        raise ProviderError("switching the default login isn't supported on macOS (logins live in the Keychain)")
    if running(provider):
        # The job writes each login where it was when the job started: a switch now would put
        # the finished login over the new default's.
        raise ProviderError(f"a {provider} login is in progress; finish or cancel it (`wake cancel`) first")
    acct = next((a for a in load_accounts() if a.key == key), None)
    if acct is None:
        raise ProviderError(f"no account {key!r}")
    slot, fname = slot_dir(provider), LOGIN_FILES[provider]

    # Everything below reads and writes under both CLI locks: a refresh in flight finishes first,
    # and another redline process can't switch between our read of slots.json and our write.
    with contextlib.ExitStack() as locks:
        locks.enter_context(cli_lock(slot))
        if not is_slot(provider, acct.home_path):  # the same dir before the first switch
            locks.enter_context(cli_lock(acct.home_path))
        notes = [m for m in [_reconcile(provider)] if m]  # the slot's login must go to its real owner
        accounts = load_accounts()
        acct = next((a for a in accounts if a.key == key), None)
        if acct is None:
            raise ProviderError(f"no account {key!r}")
        state = load_state(strict=True)
        cur = state.get(provider, {}).get("home")
        if (cur and cur == _resolved(acct.home_path)) or (not cur and is_slot(provider, acct.home_path)):
            return "; ".join(notes + [f"{acct.name} is already the default {provider} login"])
        src = acct.home_path / fname
        login = read_json_safe(src)
        if not login:
            raise ProviderError(f"{acct.name} is not logged in; log it in first")
        incoming, meant = email_in(provider, acct.home_path), expected_email(acct)
        if incoming and meant and not _same(incoming, meant):
            raise ProviderError(f"{acct.name} holds the login of {incoming}, not {meant}; "
                                f"log {acct.name} in again with the right account first")
        if provider == "claude":  # refuse before moving anything, not halfway through
            _read_doc(_claude_config(slot))
            _read_doc(_claude_config(acct.home_path))

        current = read_json_safe(slot / fname)
        there = email_in(provider, slot)
        moved = None  # first switch: (account key, its new dir), saved once the files have moved
        if cur:
            owner_home = Path(cur)
        else:
            # First switch: the slot is the dir of whoever is registered there (if anyone).
            registered = next((a for a in accounts if a.provider == provider and is_slot(provider, a.home_path)),
                              None)
            if registered is not None:
                moved = (registered.key, _new_home(provider, registered.name))
            mine = registered is not None and (not there or not expected_email(registered)
                                               or _same(there, expected_email(registered)))
            if not current:
                owner_home = moved[1] if moved else None
            elif mine:
                owner_home = moved[1]
            else:  # nobody registered there, or VS Code holds someone else's login
                owner_home = _home_for_slot_login(provider, accounts, there,
                                                  skip=registered.key if registered else None)
                accounts = load_accounts()
        if owner_home is not None and current:
            if provider == "claude":
                _read_doc(_claude_config(owner_home))
            owner_home.mkdir(parents=True, exist_ok=True)
            if _has_login(provider, owner_home) and not _same(email_in(provider, owner_home), there):
                _stash(provider, owner_home)
            write_json_atomic(owner_home / fname, current)
            if provider == "claude":
                _copy_oauth_account(slot, owner_home)
        slot.mkdir(parents=True, exist_ok=True)
        write_json_atomic(slot / fname, login)
        if provider == "claude":
            _copy_oauth_account(acct.home_path, slot)
        src.unlink()
        if moved is not None:
            accounts = load_accounts()
            for a in accounts:
                if a.key == moved[0]:
                    a.home = str(moved[1])
            save_accounts(accounts)

        now = time.time()
        st = state.setdefault(provider, {})
        hist = st.setdefault("history", [])
        if cur:
            hist.append([cur, st.get("since", 0), now])
        elif owner_home is not None:
            hist.append([_resolved(owner_home), 0, now])  # everything in the slot so far was theirs
        st["history"] = hist[-HISTORY:]
        st["home"], st["since"] = _resolved(acct.home_path), now
        now_there = email_in(provider, slot)  # what the slot reports now; `reconcile` watches for a change
        if now_there:
            st["email"] = now_there
        else:
            st.pop("email", None)
        _save_state(state)
    return "; ".join(notes + [f"{acct.name} is now the default {provider} login"])
