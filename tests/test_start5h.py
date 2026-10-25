"""Claude 5h one-click start, and percentages that never round up.

No real CLI, network or window: the subprocess, the usage fetch and the worker thread are mocked.
"""

import contextlib
import json
import subprocess
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from redline import actions, start5h
from redline.config import DEFAULT_HOMES, Account, save_accounts
from redline.models import Usage, Window, pct, spent


def _future(hours=5):
    return datetime.now(timezone.utc) + timedelta(hours=hours) - timedelta(seconds=30)


def claude_usage(five_reset=None, five=0.0, week=10.0, ok=True, stale=False):
    return Usage("work", "claude", ok, stale=stale,
                 windows=[Window("5h", five, five_reset), Window("7d", week, _future(100))])


@pytest.fixture
def acct(tmp_path, monkeypatch):
    monkeypatch.setattr(start5h, "_busy", set())
    monkeypatch.setattr(start5h, "_states", {})
    home = tmp_path / "claude-work"
    home.mkdir()
    a = Account("work", "claude", str(home))
    save_accounts([a])
    monkeypatch.setattr(start5h, "find_cli", lambda name: r"C:\fake\claude.exe")
    monkeypatch.setattr(start5h.time, "sleep", lambda s: None)
    monkeypatch.setattr(start5h, "_fresh_token", lambda account: None)

    @contextlib.contextmanager
    def fake_tmp(prefix="", ignore_cleanup_errors=False):
        d = tmp_path / "cwd"
        d.mkdir(exist_ok=True)
        yield str(d)

    monkeypatch.setattr(start5h.tempfile, "TemporaryDirectory", fake_tmp)
    return a


class FakePopen:
    """Records the call; communicate() returns `out` or raises `exc`."""
    calls = []
    out = b""
    code = 0
    exc = None
    during = None  # callable run inside communicate (e.g. to look at the lock)

    def __init__(self, args, **kw):
        FakePopen.calls.append((args, kw))
        self.pid = 4242
        self.returncode = FakePopen.code

    def communicate(self, timeout=None):
        if FakePopen.during:
            FakePopen.during()
        if FakePopen.exc:
            raise FakePopen.exc
        return FakePopen.out, b""


@pytest.fixture
def popen(monkeypatch):
    FakePopen.calls, FakePopen.out, FakePopen.code, FakePopen.exc, FakePopen.during = [], b"", 0, None, None
    monkeypatch.setattr(start5h.subprocess, "Popen", FakePopen)
    return FakePopen


def fetches(monkeypatch, *usages):
    seq = list(usages)
    calls = []

    def fetch(account, refresh_tokens=False):
        calls.append(refresh_tokens)
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(start5h, "fetch_usage", fetch)
    return calls


def run_sync(monkeypatch):
    """Run the worker inline instead of in a thread."""
    class T:
        def __init__(self, target, args, **kw):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(start5h.threading, "Thread", T)


# ------------------------------------------------------------ command line and env


def test_command_is_cheap_print_mode_and_never_bare():
    cmd = start5h.command("claude.exe")
    assert "--bare" not in cmd
    pairs = {cmd[i]: cmd[i + 1] for i in range(1, len(cmd) - 1)}
    assert pairs["-p"] == "hi" and pairs["--model"] == "haiku"
    assert pairs["--tools"] == "" and pairs["--setting-sources"] == ""
    for flag in ("--no-session-persistence", "--strict-mcp-config", "--system-prompt"):
        assert flag in cmd


def test_env_points_at_the_login_and_drops_api_keys(acct, monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL",
              "CLAUDE_CODE_USE_BEDROCK", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        monkeypatch.setenv(k, "leak")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", r"C:\somewhere\else")
    monkeypatch.setenv("CLAUDE_CODE_GIT_BASH_PATH", r"C:\Git\bin\bash.exe")
    env = start5h.environment(acct, r"C:\fake\claude.exe")
    assert env["CLAUDE_CONFIG_DIR"] == str(acct.home_path)
    up = {k.upper() for k in env}
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL",
              "CLAUDE_CODE_USE_BEDROCK", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        assert k not in up
    assert env["CLAUDE_CODE_GIT_BASH_PATH"] == r"C:\Git\bin\bash.exe"


def test_default_slot_account_runs_without_claude_config_dir(monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", r"C:\somewhere\else")
    slot = Account("main", "claude", DEFAULT_HOMES["claude"])
    env = start5h.environment(slot, None)
    assert "CLAUDE_CONFIG_DIR" not in {k.upper() for k in env}


def test_run_uses_the_effective_login_an_empty_cwd_and_the_lock(acct, popen, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-leak")
    seen = {}
    popen.during = lambda: seen.update(locked=acct.home_path.with_name(acct.home_path.name + ".lock").is_dir())
    popen.out = json.dumps({"type": "result", "is_error": False, "result": "OK"}).encode()
    fetches(monkeypatch, claude_usage(None), claude_usage(_future()))
    run_sync(monkeypatch)
    start5h.start("work", lambda *a: None)
    (args, kw), = popen.calls
    assert args == start5h.command(r"C:\fake\claude.exe")
    assert kw["env"]["CLAUDE_CONFIG_DIR"] == str(acct.home_path)
    assert "ANTHROPIC_API_KEY" not in {k.upper() for k in kw["env"]}
    assert kw["cwd"].endswith("cwd") and kw["stdin"] == subprocess.DEVNULL
    assert seen["locked"] is True  # cli_lock on the login dir while the CLI runs
    assert not acct.home_path.with_name(acct.home_path.name + ".lock").exists()


# ------------------------------------------------------------ one in flight


def test_one_start_in_flight_per_login(acct, monkeypatch, tmp_path):
    alias = Account("alias", "claude", acct.home)  # another card for the same login dir
    save_accounts([acct, alias])
    queued = []

    class Held:
        def __init__(self, target, args, **kw):
            queued.append(args)

        def start(self):
            pass

    monkeypatch.setattr(start5h.threading, "Thread", Held)
    assert "正在开启" in start5h.start("work", lambda *a: None)
    assert "正在开启" in start5h.start("work", lambda *a: None)
    assert "正在开启" in start5h.start("alias", lambda *a: None)
    assert len(queued) == 1
    assert start5h.status()["claude:work"]["state"] == "starting"
    start5h._finish(acct, queued[0][2], "error", "x")  # the job ends: a new click may start again
    start5h.start("work", lambda *a: None)
    assert len(queued) == 2


def test_only_claude_accounts_and_an_installed_cli(acct, monkeypatch):
    save_accounts([acct, Account("cx", "codex", acct.home + "-codex")])
    with pytest.raises(start5h.StartError):
        start5h.start("cx", lambda *a: None, provider="codex")
    monkeypatch.setattr(start5h, "find_cli", lambda name: None)
    with pytest.raises(start5h.StartError, match="not found"):
        start5h.start("work", lambda *a: None)
    assert start5h._busy == set()


# ------------------------------------------------------------ verification


def test_success_is_a_reset_time_in_the_refreshed_usage(acct, popen, monkeypatch):
    popen.out = json.dumps({"type": "result", "is_error": False, "result": "OK"}).encode()
    calls = fetches(monkeypatch, claude_usage(None), claude_usage(None), claude_usage(_future()))
    run_sync(monkeypatch)
    logs, done = [], []
    start5h.start("work", lambda level, text: logs.append((level, text)), on_done=lambda: done.append(1))
    assert calls == [True, True, True]  # preflight, then re-checks until the reset shows up
    assert logs[-1][0] == "ok" and "work: 5h 计时已开始 · ↻ 4h 59m" == logs[-1][1]
    assert start5h.status()["claude:work"]["state"] == "success"
    assert start5h._busy == set() and done == [1]


def test_weekly_limit_message_from_the_cli_is_the_reason(acct, popen, monkeypatch):
    popen.code = 1
    popen.out = json.dumps({"type": "result", "is_error": True,
                            "result": "You've hit your weekly limit · resets Oct 30, 9am"}).encode()
    calls = fetches(monkeypatch, claude_usage(None))
    run_sync(monkeypatch)
    logs = []
    start5h.start("work", lambda level, text: logs.append((level, text)))
    assert calls == [True, True]  # one check after a CLI error
    assert logs[-1] == ("error", "work: 5h 未开始 · You've hit your weekly limit · resets Oct 30, 9am")
    assert start5h.status()["claude:work"]["state"] == "error"
    assert start5h._busy == set()


def test_timeout_kills_the_cli_and_still_verifies(acct, popen, monkeypatch):
    popen.exc = subprocess.TimeoutExpired("claude", 90)
    killed = []
    monkeypatch.setattr(start5h, "_kill", lambda p: killed.append(p.pid))
    fetches(monkeypatch, claude_usage(None))
    run_sync(monkeypatch)
    logs = []
    start5h.start("work", lambda level, text: logs.append((level, text)))
    assert killed == [4242]
    assert logs[-1] == ("error", "work: 5h 未开始 · 请求超时")


def test_a_request_that_landed_despite_a_cli_error_counts(acct, popen, monkeypatch):
    popen.exc = subprocess.TimeoutExpired("claude", 90)
    monkeypatch.setattr(start5h, "_kill", lambda p: None)
    fetches(monkeypatch, claude_usage(None), claude_usage(_future()))
    run_sync(monkeypatch)
    logs = []
    start5h.start("work", lambda level, text: logs.append((level, text)))
    assert logs[-1][0] == "ok" and "5h 计时已开始" in logs[-1][1]


def test_no_request_when_already_counting_or_weekly_spent_or_unreadable(acct, popen, monkeypatch):
    run_sync(monkeypatch)
    for usage, level, text in [
        (claude_usage(_future()), "ok", "5h 已在计时"),
        (claude_usage(None, week=100), "error", "周额度已用完"),
        (claude_usage(None, ok=False), "error", "读不到当前用量"),
        (claude_usage(None, stale=True), "error", "读不到当前用量"),
    ]:
        fetches(monkeypatch, usage)
        logs = []
        start5h.start("work", lambda lv, t: logs.append((lv, t)))
        assert logs[-1][0] == level and text in logs[-1][1]
    assert popen.calls == []


def test_unexpected_errors_are_logged_without_details(acct, popen, monkeypatch):
    def boom(account, refresh_tokens=False):
        raise RuntimeError("secret-token-xyz")

    monkeypatch.setattr(start5h, "fetch_usage", boom)
    run_sync(monkeypatch)
    logs = []
    start5h.start("work", lambda lv, t: logs.append((lv, t)))
    assert logs[-1][0] == "error" and "secret" not in logs[-1][1]
    assert start5h._busy == set()


def test_a_token_close_to_expiry_is_refreshed_before_the_run(tmp_path, monkeypatch):
    from redline.providers import claude

    home = tmp_path / "h"
    home.mkdir()
    a = Account("work", "claude", str(home))
    creds = {"claudeAiOauth": {"accessToken": "a", "refreshToken": "r", "expiresAt": int((time.time() + 120) * 1000)}}
    (home / ".credentials.json").write_text(json.dumps(creds))
    seen = []
    monkeypatch.setattr(claude, "refresh", lambda c, force=False, manual=False: seen.append((force, manual)))
    start5h._fresh_token(a)
    assert seen == [(True, True)]
    creds["claudeAiOauth"]["expiresAt"] = int((time.time() + 3600) * 1000)
    (home / ".credentials.json").write_text(json.dumps(creds))
    start5h._fresh_token(a)
    assert seen == [(True, True)]


def test_reason_parsing():
    ok = json.dumps({"type": "result", "is_error": False, "result": "OK"}).encode()
    assert start5h._reason(ok, b"", 0) == ""
    assert start5h._reason(b"", b"Invalid API key\nmore", 1) == "Invalid API key"
    assert start5h._reason(b"", b"", 3) == "CLI exit 3"
    assert len(start5h._reason(b"x" * 500, b"", 1)) == 160


def test_startable():
    assert start5h.startable(claude_usage(None))
    assert start5h.startable(claude_usage(None, week=99.6))
    assert not start5h.startable(claude_usage(None, week=100))
    assert not start5h.startable(claude_usage(_future()))
    assert not start5h.startable(claude_usage(None, stale=True))


# ------------------------------------------------------------ plumbing


def test_dispatch_needs_a_log_sink_and_maps_errors(acct, monkeypatch):
    with pytest.raises(actions.ActionError, match="log sink"):
        actions.dispatch("start-5h", {"name": "work"})
    received = {}

    def fake(name, log, on_done=None, provider=None):
        received.update(name=name, provider=provider)
        raise start5h.StartError("nope")

    monkeypatch.setattr(start5h, "start", fake)
    with pytest.raises(actions.ActionError, match="nope"):
        actions.dispatch("start-5h", {"name": "work", "log": lambda *a: None})
    assert received == {"name": "work", "provider": "claude"}


@pytest.fixture
def server(tmp_path, monkeypatch):
    from redline.monitor import Monitor
    from redline.web import ConsoleServer

    monitor = Monitor(collect=lambda accounts, refresh_tokens=False: [])
    srv = ConsoleServer(monitor).start_background()
    yield srv
    srv.shutdown()


def _req(srv, path, body=None):
    from redline.web import TOKEN_HEADER

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(srv.url + path, data=data, headers={TOKEN_HEADER: srv.token})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


def test_console_action_gets_server_callbacks_and_status_in_usage(server, monkeypatch):
    received = {}

    def fake(name, log, on_done=None, provider=None):
        received.update(name=name, provider=provider, on_done=on_done)
        log("ok", "work: 5h 计时已开始 · ↻ 4h 59m")
        return "queued"

    monkeypatch.setattr(start5h, "start", fake)
    r = _req(server, "api/action", {"action": "start-5h", "provider": "claude", "name": "work", "log": "x"})
    assert r == {"ok": True, "message": "queued"}
    assert callable(received["on_done"]) and received["provider"] == "claude"
    assert server.monitor.events[-1]["alert"] is True  # shown as a toast
    monkeypatch.setattr(start5h, "status", lambda: {"claude:work": {"state": "verifying", "message": "m"}})
    assert _req(server, "api/usage")["meta"]["start5h"] == {"claude:work": {"state": "verifying", "message": "m"}}


def test_cli_start_command(acct, monkeypatch):
    from redline import cli

    seen = []
    monkeypatch.setattr(start5h, "main", lambda account: seen.append(account.key) or 0)
    assert cli.main(["start", "work"]) == 0
    assert seen == ["claude:work"]


def test_cli_main_waits_for_the_job(acct, popen, monkeypatch, capsys):
    popen.out = json.dumps({"type": "result", "is_error": False, "result": "OK"}).encode()
    fetches(monkeypatch, claude_usage(None), claude_usage(_future()))
    run_sync(monkeypatch)
    assert start5h.main(acct) == 0
    assert "5h 计时已开始" in capsys.readouterr().out


# ------------------------------------------------------------ never round up


def test_percentages_round_down_and_max_means_100():
    assert pct(99.6) == "99%" and pct(99.99) == "99%" and pct(100) == "100%" and pct(None) == "?"
    assert not spent(99.9) and spent(100) and not spent(None)


def test_cli_table_and_bar_round_down():
    from redline.render import BAR_WIDTH, bar, render_table

    assert "░" in bar(99.6) and bar(100) == "█" * BAR_WIDTH
    assert bar(97.4).count("█") == 19  # 19.48 cells: not rounded up to a full bar
    out = render_table([claude_usage(None, five=99.6)])
    assert " 99%" in out and "100%" not in out


def test_tray_text_rounds_down():
    from redline import tray

    text = tray.tooltip([claude_usage(None, five=99.6)])
    assert "work 99%" in text and "100%" not in text


def test_tray_ring_closes_only_at_100():
    pytest.importorskip("PIL")
    from redline.icon import render_icon

    # just left of 12 o'clock on the ring: the last part of the sweep
    probe = (124, 18)
    near = render_icon(99.6, size=256).getpixel(probe)
    full = render_icon(100, size=256).getpixel(probe)
    assert full[:3] == (255, 59, 92)
    assert near[:3] != full[:3]
