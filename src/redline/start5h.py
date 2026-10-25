"""Start an idle Claude 5h window on purpose: one tiny subscription request through the installed CLI.

A 5h window only begins at the first request. Only an explicit user action (the ▶ on the console's
`idle` label, or `redline start <name>`) calls this. The request runs as the account's OAuth login,
never an API key: only subscription usage starts the window. Success is what the usage endpoint
reports afterwards (a 5h reset time), not the CLI's exit code.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .clis import cli_env, find_cli
from .config import Account, find_account, load_accounts
from .models import Usage, spent
from .procenv import child_env
from .providers import fetch_usage
from .providers import claude as claude_provider
from .providers._util import LockBusy, cli_lock
from .slots import effective

TIMEOUT = 90
# Claude Code refreshes a token a few minutes before it expires. Refresh one that close first, so the CLI
# has no reason to want the login's lock we hold during the run.
FRESH_FOR = TIMEOUT + 600
VERIFY_DELAYS = (0, 3, 8)
PROMPT = "hi"
SYSTEM_PROMPT = "Reply with OK."
# Never inherited: an API key or token would bill the API instead of starting the window, a base URL or a
# third-party provider switch would send the request elsewhere, and a parent Claude session's variables
# would make the CLI think it runs inside that session.
STRIP_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_", "CLAUDECODE")
KEEP = {"CLAUDE_CODE_GIT_BASH_PATH"}  # Windows: where Claude Code finds bash

_lock = threading.Lock()
_busy: set[str] = set()  # resolved login dirs with a start in flight
_states: dict[str, dict[str, str]] = {}  # account key -> {"state", "message"} of the last start


class StartError(Exception):
    """Messages are safe to display and log."""


def status() -> dict[str, dict[str, str]]:
    with _lock:
        return {k: dict(v) for k, v in _states.items()}


def command(exe: str) -> list[str]:
    """The cheapest request: Haiku, no tools, no settings, hooks, plugins, MCP or skills, nothing saved.
    Not --bare: it ignores OAuth logins (it would fail or bill an API key)."""
    return [exe, "-p", PROMPT, "--model", "haiku", "--tools", "", "--system-prompt", SYSTEM_PROMPT,
            "--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands",
            "--no-session-persistence", "--output-format", "json"]


def environment(account: Account, exe: str | None) -> dict[str, str]:
    """`account` is already effective(): the default slot unsets CLAUDE_CONFIG_DIR, like `redline run`."""
    env = child_env(account.env())
    for k in list(env):
        up = k.upper()
        if up.startswith(STRIP_PREFIXES) and up not in KEEP:
            del env[k]
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"  # no telemetry, update checks or error reports
    return cli_env(exe, env)


def _window(u: Usage, name: str):
    return next((w for w in u.windows if w.name == name), None)


def _left(w) -> str:
    reset = w.resets_at if w.resets_at.tzinfo else w.resets_at.replace(tzinfo=timezone.utc)
    s = max(0, int((reset - datetime.now(timezone.utc)).total_seconds()))
    h, m = divmod(s // 60, 60)
    return f"↻ {h}h {m:02d}m" if h else f"↻ {m}m"


def startable(u: Usage) -> bool:
    """Logged in, live numbers, an idle 5h window and weekly quota left."""
    five, week = _window(u, "5h"), _window(u, "7d")
    return (u.provider == "claude" and u.ok and not u.stale and five is not None and five.resets_at is None
            and not (week is not None and spent(week.used_percent)))


def _preflight(u: Usage) -> bool:
    """True: a request is needed. False: the window already counts down."""
    if not u.ok or u.stale:
        raise StartError("读不到当前用量，请先刷新或登录")
    five, week = _window(u, "5h"), _window(u, "7d")
    if five is None:
        raise StartError("没有 5h 窗口")
    if five.resets_at is not None:
        return False
    if week is not None and spent(week.used_percent):
        raise StartError("周额度已用完")
    return True


def start(name: str, log: Callable[[str, str], None], on_done: Callable[[], None] | None = None,
          provider: str | None = "claude") -> str:
    """Queue one start. One in flight per login: a second click (or an alias of the login) is told so."""
    return _queue(name, log, on_done, provider)[0]


def _queue(name, log, on_done, provider) -> tuple[str, bool]:
    try:
        account = find_account(load_accounts(), name, provider)
    except KeyError as exc:
        raise StartError(exc.args[0]) from None
    if account.provider != "claude":
        raise StartError("5h start applies to Claude accounts only")
    exe = find_cli("claude")
    if not exe:
        raise StartError("Claude Code CLI not found")
    login = _login_key(effective(account))
    with _lock:
        if login in _busy:
            return f"{account.name}: 5h 计时正在开启", False
        _busy.add(login)
        _states[account.key] = {"state": "starting", "message": "正在开启 5h 计时"}
    worker = threading.Thread(target=_work, args=(account, exe, login, log, on_done),
                              name="redline-5h-start", daemon=True)
    try:
        worker.start()
    except Exception:
        _finish(account, login, "error", "后台任务启动失败")
        raise StartError("could not start the background job; try again") from None
    return f"{account.name}: 正在开启 5h 计时…", True


def _login_key(account: Account) -> str:
    try:
        return str(account.home_path.resolve())
    except OSError:
        return str(account.home_path)


def _finish(account: Account, login: str, state: str, message: str) -> None:
    with _lock:
        _busy.discard(login)
        _states[account.key] = {"state": state, "message": message}


def _safe_log(log, level: str, text: str) -> None:
    try:
        log(level, text)
    except Exception:
        pass


def _set(account: Account, state: str, message: str) -> None:
    with _lock:
        _states[account.key] = {"state": state, "message": message}


def _fresh_token(account: Account) -> None:
    """Refresh a token that could come due during the run now (the provider takes the CLI's lock for it)."""
    creds = claude_provider.load_creds(account)
    if creds is None or creds.source != "file":
        return  # no login: the CLI reports it; Keychain logins are the CLI's to refresh
    exp = creds.oauth.get("expiresAt")
    if exp and exp / 1000 < time.time() + FRESH_FOR:
        claude_provider.refresh(creds, force=True, manual=True)


def _work(account: Account, exe: str, login: str, log, on_done) -> None:
    state, message = "error", "5h 计时未能开启"
    try:
        current = effective(account)
        if not _preflight(fetch_usage(account, refresh_tokens=True)):
            state, message = "success", "5h 已在计时"
            _safe_log(log, "ok", f"{account.name}: {message}")
            return
        _fresh_token(current)
        try:
            # Like a refresh: no default-login switch or redline refresh moves this login mid-request.
            with cli_lock(current.home_path):
                reason = _run(exe, current)
        except LockBusy:
            raise StartError("登录正忙，请稍后再试") from None
        _set(account, "verifying", "正在确认 5h 计时")
        u = None
        for delay in VERIFY_DELAYS:  # the request may have landed even when the CLI reported an error
            if delay:
                time.sleep(delay)
            u = fetch_usage(account, refresh_tokens=True)
            five = _window(u, "5h") if u.ok and not u.stale else None
            if five is not None and five.resets_at is not None:
                state, message = "success", f"5h 计时已开始 · {_left(five)}"
                _safe_log(log, "ok", f"{account.name}: {message}")
                return
            if reason:  # the CLI already said why: one check is enough
                break
        message = f"5h 未开始 · {reason or ('读不到用量' if u is not None and not u.ok else '用量仍显示 idle')}"
        _safe_log(log, "error", f"{account.name}: {message}")
    except StartError as exc:
        message = f"5h 未开始 · {exc}"
        _safe_log(log, "error", f"{account.name}: {message}")
    except Exception:
        # Provider/CLI exceptions can carry tokens or response bodies: log a fixed message.
        _safe_log(log, "error", f"{account.name}: {message}")
    finally:
        _finish(account, login, state, message)
        if on_done:
            try:
                on_done()
            except Exception:
                pass


def _reason(out: bytes, err: bytes, code: int) -> str:
    """'' when the CLI answered; else its own short message (e.g. the weekly limit)."""
    text = out.decode("utf-8", "replace").strip()
    try:
        doc = json.loads(text.splitlines()[-1]) if text else None
    except ValueError:
        doc = None
    if isinstance(doc, dict):
        if not doc.get("is_error") and code == 0:
            return ""
        text = str(doc.get("result") or doc.get("error") or doc.get("subtype") or "")
    elif code == 0 and text:
        return ""
    text = text or err.decode("utf-8", "replace").strip() or f"CLI exit {code}"
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), text)
    return line if len(line) <= 160 else line[:159] + "…"


def _run(exe: str, account: Account) -> str:
    """Run the request in an empty dir (no project CLAUDE.md). Returns the failure reason, '' on success."""
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {"start_new_session": True}
    with tempfile.TemporaryDirectory(prefix="redline-5h-", ignore_cleanup_errors=True) as cwd:
        try:
            process = subprocess.Popen(command(exe), cwd=cwd, env=environment(account, exe),
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       **flags)
        except OSError:
            raise StartError("Claude Code CLI could not be started") from None
        try:
            out, err = process.communicate(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            _kill(process)
            return "请求超时"
    return _reason(out or b"", err or b"", process.returncode)


def _kill(process: subprocess.Popen) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run([str(Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32/taskkill.exe"),
                            "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=5,
                           creationflags=subprocess.CREATE_NO_WINDOW, check=False)
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.communicate(timeout=5)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        pass


def main(account: Account) -> int:
    """`redline start <name>`: the same job, in the foreground."""
    done = threading.Event()
    result: dict[str, str] = {}

    def log(level: str, text: str) -> None:
        result["level"] = level
        print(f"[{level}] {text}", flush=True)

    try:
        message, queued = _queue(account.name, log, done.set, account.provider)
    except StartError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(message, flush=True)
    if not queued:
        return 2
    if not done.wait(TIMEOUT + 120):
        return 2
    return 0 if result.get("level") == "ok" else 2
