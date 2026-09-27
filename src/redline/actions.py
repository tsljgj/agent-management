"""Account operations shared by the CLI and the console (add / import / login / launch...)."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .config import (
    DEFAULT_HOMES,
    PROVIDERS,
    Account,
    redline_home,
    find_account,
    load_accounts,
    load_removed,
    name_taken,
    save_accounts,
    split_ref,
    save_removed,
    validate_name,
)
from .procenv import child_env
from .providers import has_credentials

LOGIN_COMMANDS = {
    "claude": ["claude", "/login"],
    "codex": ["codex", "login"],
}


class ActionError(Exception):
    pass


def add_account(provider: str, name: str, home: str | None = None, note: str = "",
                auto_name: bool = False) -> Account:
    if provider not in PROVIDERS:
        raise ActionError(f"unknown provider {provider!r} (choose from {', '.join(PROVIDERS)})")
    try:
        validate_name(name)
    except ValueError as e:
        raise ActionError(str(e)) from None
    accounts = load_accounts()
    if name_taken(accounts, provider, name):
        raise ActionError(f"{provider} account {name!r} already exists")
    home = home or str(redline_home() / "accounts" / f"{provider}-{name}")
    acct = Account(name=name, provider=provider, home=home, note=note, auto_name=auto_name)
    acct.home_path.mkdir(parents=True, exist_ok=True)
    accounts.append(acct)
    save_accounts(accounts)
    _forget_removed(acct)
    return acct


def _forget_removed(acct: Account) -> None:
    """Re-adding an account (same name or same dir) clears its 'removed' record."""
    removed = load_removed()
    keep = [r for r in removed if r.provider != acct.provider
            or (r.name != acct.name and _resolved(r.home_path) != _resolved(acct.home_path))]
    if len(keep) != len(removed):
        save_removed(keep)


def unique_name(base: str, taken: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9._@+-]+", "-", base).strip("-._") or "account"
    name, i = base, 2
    while name in taken:
        name, i = f"{base}-{i}", i + 1
    return name


def account_details() -> dict[str, dict]:
    """Static per-account facts for the console's details view (no network)."""
    from . import browsers
    from .login import resolve_profile
    from .providers import identity

    profs = browsers.all_profiles()
    out = {}
    for a in load_accounts():
        p = resolve_profile(a, profs)
        out[a.key] = {
            "home": str(a.home_path),
            "email": identity(a) or (a.note if "@" in a.note else ""),
            "profile": {"spec": p.spec, "name": p.name, "email": p.email, "bound": bool(a.browser_profile)} if p else None,
        }
    return out


def candidates(provider: str) -> dict:
    """What the console's `+` panel offers for this provider.

    removed:  accounts the user removed earlier (one click restores them)
    profiles: browser profiles whose Google account isn't used by any account of this provider yet
    """
    from . import browsers
    from .login import resolve_profile
    from .providers import identity

    if provider not in PROVIDERS:
        raise ActionError(f"unknown provider {provider!r}")
    accounts = [a for a in load_accounts() if a.provider == provider]
    used_emails, used_specs = set(), set()
    profs = browsers.all_profiles()
    for a in accounts:
        for e in (identity(a), a.note if "@" in a.note else None):
            if e:
                used_emails.add(e.lower())
        p = resolve_profile(a, profs)
        if p:
            used_specs.add(p.spec)
    removed = [
        {"name": r.name, "email": identity(r) or (r.note if "@" in r.note else ""), "logged_in": has_credentials(r)}
        for r in load_removed() if r.provider == provider
    ]
    removed_emails = {r["email"].lower() for r in removed if r["email"]}
    profiles = [
        p.to_dict() for p in profs
        if p.email and p.email.lower() not in used_emails | removed_emails and p.spec not in used_specs
    ]
    return {"provider": provider, "removed": removed, "profiles": profiles}


def add_from_profile(provider: str, spec: str) -> Account:
    """New account for the Google account signed in to browser profile `spec`, bound to it."""
    from . import browsers

    prof = browsers.find_profile(spec)
    if prof is None:
        raise ActionError(f"browser profile {spec!r} not found")
    taken = {a.name for a in load_accounts() if a.provider == provider}
    base = prof.email or prof.name
    acct = add_account(provider, unique_name(base, taken), note=prof.email, auto_name=bool(prof.email))
    accounts = load_accounts()
    for a in accounts:
        if a.key == acct.key:
            a.browser_profile = prof.spec
    save_accounts(accounts)
    acct.browser_profile = prof.spec
    return acct


def remove_account(name: str, provider: str | None = None) -> Account:
    """Unregister an account (its login files stay on disk) and keep `scan` from re-adding it."""
    accounts = load_accounts()
    try:
        acct = find_account(accounts, name, provider)
    except KeyError as e:
        raise ActionError(e.args[0]) from None
    accounts.remove(acct)
    save_accounts(accounts)
    removed = [r for r in load_removed() if r.provider != acct.provider
               or (r.name != acct.name and _resolved(r.home_path) != _resolved(acct.home_path))]
    removed.append(acct)
    save_removed(removed)
    return acct


def rename_account(old: str, new: str, provider: str | None = None, auto: bool = False) -> Account:
    """Change an account's display name. Its login dir and browser binding stay the same."""
    new = new.strip()
    try:
        validate_name(new)
    except ValueError as e:
        raise ActionError(str(e)) from None
    accounts = load_accounts()
    try:
        acct = find_account(accounts, old, provider)
    except KeyError as e:
        raise ActionError(e.args[0]) from None
    if new == acct.name and acct.auto_name == auto:
        return acct
    if new != acct.name and name_taken(accounts, acct.provider, new):
        raise ActionError(f"{acct.provider} account {new!r} already exists")
    acct.name = new
    acct.auto_name = auto  # a hand-picked name sticks; auto names keep following the email
    save_accounts(accounts)
    return acct


def _looks_auto(a: Account) -> bool:
    """For configs written before `auto_name` existed: was this name generated by us?"""
    p = a.provider
    if re.fullmatch(rf"{p}(-default)?(-\d+)?", a.name) or a.name.startswith(p + "-"):
        return True
    email = a.note if "@" in a.note else ""
    return bool(email) and a.name in (email, email.split("@")[0])


def adopt_email_names() -> list[tuple[str, str, str]]:
    """Rename accounts whose name is still automatic to their login email.

    Returns [(provider, old, new)] so callers (the monitor) can carry state over.
    """
    from .providers import identity

    accounts = load_accounts()
    changed, dirty = [], False
    for a in accounts:
        auto = a.auto_name if a.auto_name is not None else _looks_auto(a)
        if a.auto_name is None:
            a.auto_name, dirty = auto, True
        if not auto:
            continue
        email = identity(a) or (a.note if "@" in a.note else None)
        if not email or a.name == email:
            continue
        try:
            validate_name(email)
        except ValueError:
            continue
        if name_taken(accounts, a.provider, email):
            continue  # another account of this provider already uses that email as its name
        changed.append((a.provider, a.name, email))
        a.name, dirty = email, True
    if dirty:
        save_accounts(accounts)
    return changed


def restore_account(name: str, provider: str | None = None) -> Account:
    removed = load_removed()
    provider, name = split_ref(name, provider)
    match = [r for r in removed if r.name == name and (provider is None or r.provider == provider)]
    if not match:
        raise ActionError(f"no removed account named {name!r} (see `removed`)")
    acct = match[0]
    accounts = load_accounts()
    if any(_resolved(a.home_path) == _resolved(acct.home_path) and a.provider == acct.provider for a in accounts):
        save_removed([r for r in removed if r is not acct])
        raise ActionError(f"{acct.name} is already registered (as another name)")
    if name_taken(accounts, acct.provider, acct.name):
        raise ActionError(f"{acct.provider} account {acct.name!r} already exists")
    accounts.append(acct)
    save_accounts(accounts)
    save_removed([r for r in removed if r is not acct])
    return acct


def scan(register: bool = True, include_removed: bool = False) -> tuple[list[tuple[Account, "Found"]], list["Found"]]:
    """Discover account dirs on this machine; register the new ones.

    Accounts the user removed are skipped unless include_removed.
    Returns (newly added, already registered or removed).
    """
    from .discover import discover, suggest_name

    accounts = load_accounts()
    removed = load_removed()
    known = {(a.provider, _resolved(a.home_path)) for a in accounts}
    if not include_removed:
        known |= {(a.provider, _resolved(a.home_path)) for a in removed}
    taken = {(a.provider, a.name) for a in accounts}
    added, existing = [], []
    for f in discover():
        if (f.provider, _resolved(f.home)) in known:
            existing.append(f)
            continue
        mine = {n for p, n in taken if p == f.provider}
        name = unique_name(f.email, mine) if f.email else suggest_name(f, mine)
        taken.add((f.provider, name))
        acct = Account(name=name, provider=f.provider, home=str(f.home), note=f.email or "", auto_name=True)
        accounts.append(acct)
        added.append((acct, f))
    if register and added:
        save_accounts(accounts)
        if include_removed:
            back = {(a.provider, _resolved(a.home_path)) for a, _ in added}
            save_removed([r for r in removed if (r.provider, _resolved(r.home_path)) not in back])
    return added, existing


def _resolved(p: Path) -> str:
    try:
        return str(p.resolve())
    except OSError:
        return str(p)


def bind(name: str, spec: str, provider: str | None = None) -> str:
    from . import browsers

    accounts = load_accounts()
    try:
        acct = find_account(accounts, name, provider)
    except KeyError as e:
        raise ActionError(e.args[0]) from None
    if spec in ("", "none", "-"):
        acct.browser_profile = ""
        save_accounts(accounts)
        return f"{name}: browser binding cleared (auto-match by email)"
    prof = browsers.find_profile(spec)
    if prof is None:
        raise ActionError(f"no browser profile matches {spec!r}; run `profiles` to list them")
    acct.browser_profile = prof.spec
    save_accounts(accounts)
    return f"{name} -> {prof.spec} “{prof.name}” {prof.email}"


def profiles_text() -> str:
    from . import browsers
    from .login import resolve_profile

    profs = browsers.all_profiles()
    if not profs:
        return "no Chrome / Edge / Brave profiles found"
    users: dict[str, list[str]] = {}
    for a in load_accounts():
        p = resolve_profile(a, profs)
        if p:
            users.setdefault(p.spec, []).append(a.name)
    return "\n".join(
        f"{p.spec:<22} {p.name[:18]:<18} {p.email or '(not signed in)':<30} {', '.join(users.get(p.spec, []))}"
        for p in profs
    )


def import_defaults() -> list[Account]:
    """Register the CLIs' default homes (~/.claude, ~/.codex) if they hold a login."""
    accounts = load_accounts()
    added = []
    for provider in PROVIDERS:
        probe = Account(name=f"{provider}-default", provider=provider, home=DEFAULT_HOMES[provider])
        if any(a.provider == provider and a.home_path == probe.home_path for a in accounts):
            continue
        if any(a.name == probe.name for a in accounts) or not has_credentials(probe):
            continue
        accounts.append(probe)
        added.append(probe)
    save_accounts(accounts)
    return added


def get_account(name: str, provider: str | None = None) -> Account:
    try:
        return find_account(load_accounts(), name, provider)
    except KeyError as e:
        raise ActionError(e.args[0]) from None


# ------------------------------------------------------------ terminals


def open_terminal(acct: Account, argv: list[str] | None = None, extra_env: dict | None = None) -> str:
    """Open a new terminal window running `argv` (default: the provider CLI) as this account."""
    argv = argv or [acct.provider]
    acct.home_path.mkdir(parents=True, exist_ok=True)
    if shutil.which(argv[0]) is None:
        raise ActionError(f"{argv[0]!r} not found on PATH")
    env = child_env({**acct.env(), **(extra_env or {})})
    cwd = str(Path.home())
    title = f"redline: {acct.name}"

    if sys.platform == "win32":
        cmdline = subprocess.list2cmdline(argv)
        subprocess.Popen(
            ["cmd.exe", "/k", f"title {title} && {cmdline}"],
            env=env, cwd=cwd, creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
    elif sys.platform == "darwin":
        exports = " ".join(f"export {k}={shlex.quote(v)};" for k, v in acct.env().items())
        script = f"cd {shlex.quote(cwd)}; {exports} {shlex.join(argv)}"
        apple = f'tell application "Terminal" to do script "{_applescript_escape(script)}"'
        subprocess.Popen(["osascript", "-e", apple, "-e", 'tell application "Terminal" to activate'])
    else:
        for term in (["x-terminal-emulator", "-e"], ["gnome-terminal", "--"], ["konsole", "-e"], ["xterm", "-e"]):
            if shutil.which(term[0]):
                subprocess.Popen([*term, *argv], env=env, cwd=cwd, start_new_session=True)
                break
        else:
            raise ActionError("no terminal emulator found")
    return f"opened terminal: {' '.join(argv)} as {acct.name}"


def _applescript_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def launch(name: str, provider: str | None = None) -> str:
    return open_terminal(get_account(name, provider))


# ------------------------------------------------------------ console dispatch


def dispatch(action: str, args: dict) -> str:
    """Entry point for the console's POST /api/action. Returns a message or raises ActionError."""
    if action == "add":
        acct = add_account(args.get("provider", ""), args.get("name", ""), note=args.get("note", ""))
        hint = "" if has_credentials(acct) else f"; run `login {acct.name}` to sign in"
        return f"added {acct.provider}:{acct.name} -> {acct.home_path}{hint}"
    prov = args.get("provider") or None
    if action == "remove":
        acct = remove_account(args.get("name", ""), prov)
        return f"removed {acct.name} (login files kept; `scan` won't re-add it) -- undo: restore {acct.name}"
    if action == "rename":
        acct = rename_account(args.get("name", ""), args.get("new", ""), prov)
        return f"renamed {args.get('name')} -> {acct.name}"
    if action == "restore":
        acct = restore_account(args.get("name", ""), prov)
        return f"restored {acct.name}"
    if action == "removed":
        rs = load_removed()
        return "\n".join(f"{r.name:<18} {r.provider:<6} {r.note or '':<30} {r.home_path}" for r in rs) or "nothing removed"
    if action in ("import", "scan"):
        added, existing = scan(include_removed=bool(args.get("all")))
        lines = [f"+ {a.name:<18} {a.provider:<6} {f.email or '?':<30} {f.home}" for a, f in added]
        lines.append(f"scan: {len(added)} new, {len(existing)} already registered or removed"
                     + ("" if args.get("all") else "  (`scan all` also brings back removed ones)"))
        return "\n".join(lines)
    if action == "add-profile":
        acct = add_from_profile(args.get("provider", ""), args.get("profile", ""))
        if args.get("log") is None:
            return f"added {acct.name} ({acct.note}); run `login {acct.name}`"
        from .login import start_wake

        start_wake([acct.key], args["log"], force=True, on_done=args.get("on_done"))
        return f"added {acct.name} · logging in with {acct.note}…"
    if action in ("wake", "login"):
        from .login import start_wake

        if args.get("log") is None:
            raise ActionError("wake needs a log sink")
        names = [args["name"]] if action == "login" else (args.get("names") or None)
        try:
            return start_wake(names, args["log"], force=action == "login", on_done=args.get("on_done"), provider=prov)
        except KeyError as e:
            raise ActionError(e.args[0]) from None
    if action == "wake-cancel":
        from .login import cancel_wake

        return cancel_wake()
    if action == "web":
        from .login import open_web

        acct = get_account(args.get("name", ""), prov)
        return open_web(acct, args.get("url") or None)
    if action == "bind":
        return bind(args.get("name", ""), args.get("profile", ""), prov)
    if action == "profiles":
        return profiles_text()
    if action == "update":  # the tray overrides this with a check-and-install version
        from . import update

        try:
            return update.check()[1]
        except Exception as e:
            raise ActionError(f"update check failed: {e}") from None
    if action == "launch":
        return launch(args.get("name", ""), prov)
    if action == "list":
        accts = load_accounts()
        return "\n".join(
            f"{a.name:<16} {a.provider:<6} {'online' if has_credentials(a) else 'no-login':<8} {a.home_path}"
            for a in accts
        ) or "no accounts"
    raise ActionError(f"unknown action {action!r}")
