"""Command-line interface: `redline <command>`."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from . import __version__, actions
from .config import PROVIDERS, Account, find_account, load_accounts, save_accounts
from .models import Usage
from .procenv import child_env
from .providers import fetch_usage, has_credentials
from .render import render_table

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
    try:
        acct = actions.add_account(args.provider, args.name, home=args.home, note=args.note or "")
    except actions.ActionError as e:
        print(e, file=sys.stderr)
        return 1
    print(f"added {acct.provider} account {acct.name!r} -> {acct.home_path}")
    if not has_credentials(acct):
        print(f"not logged in yet; run:  redline login {acct.name}")
    return 0


def cmd_scan(args) -> int:
    if args.dry_run:
        from .discover import discover

        for f in discover():
            state = "logged in" if f.logged_in else "no token"
            print(f"{f.provider:<6} {state:<9} {f.email or '?':<32} {f.home}  [{f.source}]")
        return 0
    added, existing = actions.scan(include_removed=args.all)
    for a, f in added:
        print(f"+ {a.name:<18} {a.provider:<6} {f.email or '?':<32} {f.home}")
    print(f"{len(added)} new, {len(existing)} already registered")
    return 0


def _print_log(level: str, text: str) -> None:
    print(f"[{level}] {text}", flush=True)


def cmd_wake(args) -> int:
    from .login import WakeJob

    accounts = _select(args.names)
    if not accounts:
        print("no accounts; run `redline scan` first")
        return 1
    results = WakeJob(accounts, _print_log, force=args.force).run()
    return 0 if all(v in ("ok", "refreshed", "logged-in") for v in results.values()) else 2


def cmd_web(args) -> int:
    from .login import open_web

    print(open_web(find_account(load_accounts(), args.name), args.url))
    return 0


def cmd_bind(args) -> int:
    try:
        print(actions.bind(args.name, args.profile))
    except actions.ActionError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


def cmd_profiles(args) -> int:
    print(actions.profiles_text())
    return 0


def cmd_list(args) -> int:
    accounts = load_accounts()
    if not accounts:
        print("no accounts; add one with `redline add claude <name>` or `redline import`")
        return 0
    w = max(len(a.name) for a in accounts)
    for a in accounts:
        status = "logged in" if has_credentials(a) else "NOT logged in"
        note = f"  # {a.note}" if a.note else ""
        print(f"{a.name:<{w}}  {a.provider:<6}  {status:<13}  {a.home_path}{note}")
    return 0


def cmd_remove(args) -> int:
    for name in args.names:
        try:
            acct = actions.remove_account(name)
        except actions.ActionError as e:
            print(e, file=sys.stderr)
            return 1
        print(f"removed {acct.name!r} (files in {acct.home_path} kept; `scan` won't re-add it; undo: redline restore {acct.name})")
    return 0


def cmd_rename(args) -> int:
    try:
        acct = actions.rename_account(args.old, args.new)
    except actions.ActionError as e:
        print(e, file=sys.stderr)
        return 1
    print(f"renamed {args.old} -> {acct.name}")
    return 0


def cmd_restore(args) -> int:
    if not args.name:
        print(actions.dispatch("removed", {}))
        return 0
    try:
        print(f"restored {actions.restore_account(args.name).name}")
    except actions.ActionError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


def cmd_usage(args) -> int:
    accounts = _select(args.names)
    if not accounts:
        print("no accounts; add one with `redline add claude <name>` or `redline import`")
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
    from .slots import effective

    env = child_env(effective(acct).env())
    os.execvpe(exe, argv, env)
    return 0  # unreachable


def cmd_login(args) -> int:
    from .login import WakeJob

    from .slots import effective

    acct = effective(find_account(load_accounts(), args.name))
    results = WakeJob([acct], _print_log, force=True).run()
    return 0 if results.get(acct.name) == "logged-in" else 2


def cmd_run(args) -> int:
    acct = find_account(load_accounts(), args.name)
    extra = args.args[1:] if args.args[:1] == ["--"] else args.args
    return _exec_with_account(acct, [acct.provider, *extra])


def cmd_exec(args) -> int:
    acct = find_account(load_accounts(), args.name)
    cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    if not cmd:
        print("usage: redline exec <name> -- <command> [args...]", file=sys.stderr)
        return 1
    return _exec_with_account(acct, cmd)


def cmd_env(args) -> int:
    acct = find_account(load_accounts(), args.name)
    from .slots import effective

    for k, v in effective(acct).env().items():
        print(f"export {k}={_shell_quote(v)}" if v is not None else f"unset {k}")
    return 0


def _shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def cmd_serve(args) -> int:
    from .web import serve

    from .config import load_settings

    serve(args.host, args.port, interval=args.interval,
          refresh_tokens=args.refresh_tokens or load_settings()["auto_refresh"])
    return 0


def cmd_tray(args) -> int:
    from .tray import restart_test, restart_test_child, run_tray

    if args.self_test_restart:
        return restart_test(args.self_test_restart, reset_env=not args.no_reset)
    if args.self_test_child:
        return restart_test_child(args.self_test_child)

    return run_tray(interval=args.interval, refresh_tokens=True if args.refresh_tokens else None,
                    self_test_mode=args.self_test, updated_from=args.updated_from,
                    show=args.show or not (args.background or args.updated_from is not None))


# ---------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="redline", description=__doc__)
    p.add_argument("--version", action="version", version=f"redline {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("add", help="register a new account (own config dir per account)")
    s.add_argument("provider", choices=PROVIDERS)
    s.add_argument("name")
    s.add_argument("--home", help="use an existing CLAUDE_CONFIG_DIR / CODEX_HOME instead of creating one")
    s.add_argument("--note", help="free-form note, e.g. the account email")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("scan", aliases=["import"], help="find Claude/Codex accounts on this machine and register them")
    s.add_argument("--dry-run", action="store_true", help="only list what would be registered")
    s.add_argument("--all", action="store_true", help="also re-add accounts you removed")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("wake", help="bring all (or given) accounts online: refresh expired tokens, log in the rest")
    s.add_argument("names", nargs="*")
    s.add_argument("--force", action="store_true", help="log in again even if the token is valid")
    s.set_defaults(func=cmd_wake)

    s = sub.add_parser("web", help="open claude.ai / chatgpt.com in this account's Chrome profile")
    s.add_argument("name")
    s.add_argument("url", nargs="?")
    s.set_defaults(func=cmd_web)

    s = sub.add_parser("bind", help="bind an account to a browser profile: redline bind work me@gmail.com")
    s.add_argument("name")
    s.add_argument("profile", help='Google email, "Profile 2", "chrome:Profile 2", or "none"')
    s.set_defaults(func=cmd_bind)

    s = sub.add_parser("profiles", help="list Chrome/Edge/Brave profiles and their Google accounts")
    s.set_defaults(func=cmd_profiles)

    s = sub.add_parser("list", aliases=["ls"], help="list registered accounts")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("remove", aliases=["rm"], help="unregister accounts (keeps their files; scan won't re-add them)")
    s.add_argument("names", nargs="+")
    s.set_defaults(func=cmd_remove)

    s = sub.add_parser("rename", aliases=["mv"], help="rename an account (its login stays the same)")
    s.add_argument("old")
    s.add_argument("new")
    s.set_defaults(func=cmd_rename)

    s = sub.add_parser("restore", help="bring back a removed account (no name: list removed ones)")
    s.add_argument("name", nargs="?")
    s.set_defaults(func=cmd_restore)

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

    s = sub.add_parser("login", help="log this account in (browser opens in its bound Chrome profile)")
    s.add_argument("name")
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("run", help="run claude/codex as this account: redline run work -- --resume")
    s.add_argument("name")
    s.add_argument("args", nargs=argparse.REMAINDER)
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("exec", help="run any command with this account's env: redline exec work -- cmd")
    s.add_argument("name")
    s.add_argument("cmd", nargs=argparse.REMAINDER)
    s.set_defaults(func=cmd_exec)

    s = sub.add_parser("env", help="print export lines, e.g. eval \"$(redline env work)\"")
    s.add_argument("name")
    s.set_defaults(func=cmd_env)

    s = sub.add_parser("serve", help="local web dashboard")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("-n", "--interval", type=int, default=120, help="seconds between upstream fetches")
    s.add_argument("--refresh-tokens", action="store_true", help=refresh_help)
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("tray", help="system tray app with the console window (needs `pip install redline[tray]`)")
    s.add_argument("-n", "--interval", type=int, default=None, help="seconds between upstream fetches")
    s.add_argument("--refresh-tokens", action="store_true", help=refresh_help)
    s.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--updated-from", type=int, default=None, help=argparse.SUPPRESS)
    s.add_argument("--self-test-restart", metavar="OUT", help=argparse.SUPPRESS)
    s.add_argument("--self-test-child", metavar="OUT", help=argparse.SUPPRESS)
    s.add_argument("--no-reset", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--show", action="store_true", help=argparse.SUPPRESS)  # kept for old updaters
    s.add_argument("--background", action="store_true", help="start hidden in the tray (used by autostart)")
    s.set_defaults(func=cmd_tray)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 1:
        from .login import handle_browser_callback, is_url

        if is_url(argv[0]):  # we were invoked as $BROWSER by claude/codex during a login
            return handle_browser_callback(argv[0])
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except (KeyError, ValueError) as e:
        print(f"error: {e.args[0] if e.args else e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
