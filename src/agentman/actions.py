"""Account operations shared by the CLI and the console (add / import / login / launch...)."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .config import (
    DEFAULT_HOMES,
    PROVIDERS,
    Account,
    agentman_home,
    find_account,
    load_accounts,
    save_accounts,
    validate_name,
)
from .providers import has_credentials

LOGIN_COMMANDS = {
    "claude": ["claude", "/login"],
    "codex": ["codex", "login"],
}


class ActionError(Exception):
    pass


def add_account(provider: str, name: str, home: str | None = None, note: str = "") -> Account:
    if provider not in PROVIDERS:
        raise ActionError(f"unknown provider {provider!r} (choose from {', '.join(PROVIDERS)})")
    try:
        validate_name(name)
    except ValueError as e:
        raise ActionError(str(e)) from None
    accounts = load_accounts()
    if any(a.name == name for a in accounts):
        raise ActionError(f"account {name!r} already exists")
    home = home or str(agentman_home() / "accounts" / f"{provider}-{name}")
    acct = Account(name=name, provider=provider, home=home, note=note)
    acct.home_path.mkdir(parents=True, exist_ok=True)
    accounts.append(acct)
    save_accounts(accounts)
    return acct


def remove_account(name: str) -> Account:
    accounts = load_accounts()
    try:
        acct = find_account(accounts, name)
    except KeyError as e:
        raise ActionError(e.args[0]) from None
    accounts.remove(acct)
    save_accounts(accounts)
    return acct


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


def get_account(name: str) -> Account:
    try:
        return find_account(load_accounts(), name)
    except KeyError as e:
        raise ActionError(e.args[0]) from None


# ------------------------------------------------------------ terminals


def open_terminal(acct: Account, argv: list[str] | None = None) -> str:
    """Open a new terminal window running `argv` (default: the provider CLI) as this account."""
    argv = argv or [acct.provider]
    acct.home_path.mkdir(parents=True, exist_ok=True)
    if shutil.which(argv[0]) is None:
        raise ActionError(f"{argv[0]!r} not found on PATH")
    env = {**os.environ, **acct.env()}
    cwd = str(Path.home())
    title = f"agentman: {acct.name}"

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


def launch(name: str) -> str:
    return open_terminal(get_account(name))


def login(name: str) -> str:
    acct = get_account(name)
    return open_terminal(acct, LOGIN_COMMANDS[acct.provider])


# ------------------------------------------------------------ console dispatch


def dispatch(action: str, args: dict) -> str:
    """Entry point for the console's POST /api/action. Returns a message or raises ActionError."""
    if action == "add":
        acct = add_account(args.get("provider", ""), args.get("name", ""), note=args.get("note", ""))
        hint = "" if has_credentials(acct) else f"; run `login {acct.name}` to sign in"
        return f"added {acct.provider}:{acct.name} -> {acct.home_path}{hint}"
    if action == "remove":
        acct = remove_account(args.get("name", ""))
        return f"removed {acct.name} (files kept in {acct.home_path})"
    if action == "import":
        added = import_defaults()
        return "imported " + ", ".join(a.name for a in added) if added else "nothing new to import"
    if action == "login":
        return login(args.get("name", ""))
    if action == "launch":
        return launch(args.get("name", ""))
    if action == "list":
        accts = load_accounts()
        return "\n".join(
            f"{a.name:<16} {a.provider:<6} {'online' if has_credentials(a) else 'no-login':<8} {a.home_path}"
            for a in accts
        ) or "no accounts"
    raise ActionError(f"unknown action {action!r}")
