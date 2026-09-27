"""Command-line interface: `agentman <command>`."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import __version__
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
from .models import Usage
from .providers import fetch_usage, has_credentials
from .render import render_table

LOGIN_COMMANDS = {
    "claude": ["claude", "/login"],
    "codex": ["codex", "login"],
}


def collect(accounts: list[Account], refresh_tokens: bool = False) -> list[Usage]:
    if not accounts:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(accounts))) as pool:
        return list(pool.map(lambda a: fetch_usage(a, refresh_tokens=refresh_tokens), accounts))


def _select(names: list[str] | None) -> list[Account]:
    accounts = load_accounts()
    if not names:
        return accounts
    return [find_account(accounts, n) for n in names]


# ---------------------------------------------------------------- commands


def cmd_add(args) -> int:
    validate_name(args.name)
    accounts = load_accounts()
    if any(a.name == args.name for a in accounts):
        print(f"account {args.name!r} already exists", file=sys.stderr)
        return 1
    home = args.home or str(agentman_home() / "accounts" / f"{args.provider}-{args.name}")
    acct = Account(name=args.name, provider=args.provider, home=home, note=args.note or "")
    acct.home_path.mkdir(parents=True, exist_ok=True)
    accounts.append(acct)
    save_accounts(accounts)
    print(f"added {acct.provider} account {acct.name!r} -> {acct.home_path}")
    if not has_credentials(acct):
        print(f"not logged in yet; run:  agentman login {acct.name}")
    return 0


def cmd_import(args) -> int:
    """Register the CLIs' default home dirs (~/.claude, ~/.codex) if they hold a login."""
    accounts = load_accounts()
    added = 0
    for provider in PROVIDERS:
        home = DEFAULT_HOMES[provider]
        probe = Account(name=f"{provider}-default", provider=provider, home=home)
        if any(a.provider == provider and a.home_path == probe.home_path for a in accounts):
            continue
        if not has_credentials(probe):
            continue
        if any(a.name == probe.name for a in accounts):
            continue
        accounts.append(probe)
        added += 1
        print(f"imported {probe.name} ({home})")
    save_accounts(accounts)
    if not added:
        print("nothing new to import")
    return 0


def cmd_list(args) -> int:
    accounts = load_accounts()
    if not accounts:
        print("no accounts; add one with `agentman add claude <name>` or `agentman import`")
        return 0
    w = max(len(a.name) for a in accounts)
    for a in accounts:
        status = "logged in" if has_credentials(a) else "NOT logged in"
        note = f"  # {a.note}" if a.note else ""
        print(f"{a.name:<{w}}  {a.provider:<6}  {status:<13}  {a.home_path}{note}")
    return 0


def cmd_remove(args) -> int:
    accounts = load_accounts()
    acct = find_account(accounts, args.name)
    accounts.remove(acct)
    save_accounts(accounts)
    print(f"removed {acct.name!r} from the registry (its directory {acct.home_path} was kept)")
    return 0


def cmd_usage(args) -> int:
    accounts = _select(args.names)
    if not accounts:
        print("no accounts; add one with `agentman add claude <name>` or `agentman import`")
        return 1
    usages = collect(accounts, refresh_tokens=args.refresh_tokens)
    if args.json:
        print(json.dumps([u.to_dict() for u in usages], indent=2))
    else:
        print(render_table(usages), end="")
    return 0 if all(u.ok for u in usages) else 2


def cmd_watch(args) -> int:
    accounts = _select(args.names)
    try:
        while True:
            usages = collect(accounts, refresh_tokens=args.refresh_tokens)
            out = render_table(usages)
            if sys.stdout.isatty():
                sys.stdout.write("\033[H\033[2J")
            print(time.strftime("%Y-%m-%d %H:%M:%S") + f"  (every {args.interval}s, Ctrl-C to quit)\n")
            print(out, end="", flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def _exec_with_account(acct: Account, argv: list[str]) -> int:
    exe = shutil.which(argv[0])
    if exe is None:
        print(f"{argv[0]!r} not found on PATH", file=sys.stderr)
        return 127
    env = {**os.environ, **acct.env()}
    os.execvpe(exe, argv, env)
    return 0  # unreachable


def cmd_login(args) -> int:
    acct = find_account(load_accounts(), args.name)
    acct.home_path.mkdir(parents=True, exist_ok=True)
    argv = LOGIN_COMMANDS[acct.provider]
    print(f"launching `{' '.join(argv)}` with {acct.env()}", file=sys.stderr)
    return _exec_with_account(acct, argv)


def cmd_run(args) -> int:
    acct = find_account(load_accounts(), args.name)
    extra = args.args[1:] if args.args[:1] == ["--"] else args.args
    return _exec_with_account(acct, [acct.provider, *extra])


def cmd_exec(args) -> int:
    acct = find_account(load_accounts(), args.name)
    cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    if not cmd:
        print("usage: agentman exec <name> -- <command> [args...]", file=sys.stderr)
        return 1
    return _exec_with_account(acct, cmd)


def cmd_env(args) -> int:
    acct = find_account(load_accounts(), args.name)
    for k, v in acct.env().items():
        print(f"export {k}={_shell_quote(v)}")
    return 0


def _shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def cmd_serve(args) -> int:
    from .web import serve

    serve(args.host, args.port, min_interval=args.min_interval, refresh_tokens=args.refresh_tokens)
    return 0


# ---------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agentman", description=__doc__)
    p.add_argument("--version", action="version", version=f"agentman {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("add", help="register a new account (own config dir per account)")
    s.add_argument("provider", choices=PROVIDERS)
    s.add_argument("name")
    s.add_argument("--home", help="use an existing CLAUDE_CONFIG_DIR / CODEX_HOME instead of creating one")
    s.add_argument("--note", help="free-form note, e.g. the account email")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("import", help="register the default ~/.claude and ~/.codex logins")
    s.set_defaults(func=cmd_import)

    s = sub.add_parser("list", aliases=["ls"], help="list registered accounts")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("remove", aliases=["rm"], help="unregister an account (keeps its files)")
    s.add_argument("name")
    s.set_defaults(func=cmd_remove)

    refresh_help = (
        "if an access token has expired, refresh it and write the new tokens back to the "
        "account's credentials file (same as the CLI itself would do)"
    )

    s = sub.add_parser("usage", aliases=["u"], help="show current usage of all (or given) accounts")
    s.add_argument("names", nargs="*")
    s.add_argument("--json", action="store_true")
    s.add_argument("--refresh-tokens", action="store_true", help=refresh_help)
    s.set_defaults(func=cmd_usage)

    s = sub.add_parser("watch", help="re-render usage every N seconds")
    s.add_argument("names", nargs="*")
    s.add_argument("-n", "--interval", type=int, default=120)
    s.add_argument("--refresh-tokens", action="store_true", help=refresh_help)
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("login", help="run the provider's login flow inside this account's dir")
    s.add_argument("name")
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("run", help="run claude/codex as this account: agentman run work -- --resume")
    s.add_argument("name")
    s.add_argument("args", nargs=argparse.REMAINDER)
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("exec", help="run any command with this account's env: agentman exec work -- cmd")
    s.add_argument("name")
    s.add_argument("cmd", nargs=argparse.REMAINDER)
    s.set_defaults(func=cmd_exec)

    s = sub.add_parser("env", help="print export lines, e.g. eval \"$(agentman env work)\"")
    s.add_argument("name")
    s.set_defaults(func=cmd_env)

    s = sub.add_parser("serve", help="local web dashboard")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--min-interval", type=int, default=60, help="min seconds between upstream fetches")
    s.add_argument("--refresh-tokens", action="store_true", help=refresh_help)
    s.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except (KeyError, ValueError) as e:
        print(f"error: {e.args[0] if e.args else e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
