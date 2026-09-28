import json
import os
import urllib.error
import urllib.request

import pytest

from redline import actions
from redline.config import load_accounts
from redline.models import Usage, Window
from redline.monitor import Monitor
from redline.web import TOKEN_HEADER, ConsoleServer


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("REDLINE_HOME", str(tmp_path / "am"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()


def _fake_collect(series):
    """Each call returns the next 5h percentage for account 'work'."""
    it = iter(series)

    def collect(accounts, refresh_tokens=False):
        return [Usage("work", "claude", True, windows=[Window("5h", next(it))])]

    return collect


def test_monitor_alerts_once_per_crossing_and_on_reset():
    m = Monitor(collect=_fake_collect([50, 85, 88, 97, 99, 5]))
    seen = []
    m.listeners.append(lambda u, alerts: seen.append([a["text"] for a in alerts]))
    for _ in range(6):
        m.poll()
    assert seen[0] == []                       # first sample: no baseline
    assert "hit 85%" in seen[1][0]             # crossed 80
    assert seen[2] == []                       # still above 80, no repeat
    assert "hit 97%" in seen[3][0]
    assert [e["level"] for e in m.events if e["alert"]] == ["warn", "crit", "ok"]
    assert seen[4] == []
    assert "reset" in seen[5][0]
    assert m.max_percent("5h") == 5


def test_monitor_force_floor():
    calls = []
    m = Monitor(collect=lambda a, refresh_tokens=False: calls.append(1) or [])
    m.poll()
    m.poll(force=True)  # within FORCE_FLOOR -> skipped
    assert len(calls) == 1


def test_monitor_payload_events_since():
    m = Monitor(collect=_fake_collect([10, 20]))
    m.poll()
    p1 = m.payload()
    last = p1["events"][-1]["id"]
    m.poll()
    p2 = m.payload(since=last)
    assert all(e["id"] > last for e in p2["events"]) and p2["events"]


def test_monitor_keeps_last_good_numbers_across_restarts():
    from datetime import datetime, timedelta, timezone

    reset = datetime.now(timezone.utc) + timedelta(hours=2)
    good = [Usage("work", "claude", True, email="w@x.com", windows=[Window("5h", 42.0, reset)])]
    Monitor(collect=lambda a, refresh_tokens=False: good).poll()

    # new process (e.g. after a self-update); the token has expired meanwhile
    bad = [Usage("work", "claude", False, error="access token expired; renewing it was rate limited")]
    m = Monitor(collect=lambda a, refresh_tokens=False: bad)
    m.poll()
    u = m.usages[0]
    assert u.stale and not u.ok and u.email == "w@x.com"
    assert u.windows[0].used_percent == 42.0 and u.windows[0].resets_at == reset
    assert "expired" in u.error


def test_usage_rate_levels():
    from redline.monitor import rate_level

    m = Monitor(collect=lambda a, refresh_tokens=False: [])
    t0 = 1_000_000.0

    def feed(minutes, pct, name="5h"):
        u = Usage("work", "claude", True, windows=[Window(name, pct)])
        m._track_rate(u, now=t0 + minutes * 60)
        return u.rate

    assert feed(0, 10) is None            # no history yet
    assert feed(2, 10) is None            # too little history
    assert feed(10, 10)["level"] == "idle"
    r = feed(30, 20)                      # +10% in 30 min = 20%/h on a 5h window
    assert r["level"] == "mid" and r["per_hour"] == 20.0 and r["window"] == "5h" and r["pace"] == 1.0
    assert feed(60, 45)["level"] == "high"  # +25% in the last 30 min = 50%/h
    assert feed(62, 3) is None            # the window reset: start over
    assert rate_level(4, 5) == "low" and rate_level(0.2, 168) == "low" and rate_level(0.6, 168) == "mid" and rate_level(1.0, 168) == "high"


def test_usage_rate_is_sent_and_dropped_when_stale():
    seq = iter([[Usage("work", "claude", True, windows=[Window("5h", 10.0)])],
                [Usage("work", "claude", True, windows=[Window("5h", 30.0)])],
                [Usage("work", "claude", False, error="network error")]])
    m = Monitor(collect=lambda a, refresh_tokens=False: next(seq))
    clock = iter([0.0, 600.0, 1200.0])
    orig = m._track_rate
    m._track_rate = lambda u: orig(u, now=next(clock))
    m.poll()
    m.poll()
    assert m.payload()["usages"][0]["rate"]["level"] == "high"  # +20% in 10 min
    m.poll()
    u = m.payload()["usages"][0]
    assert u["stale"] and u["rate"] is None


def test_usage_rate_history_survives_restarts():
    import time as _t

    now = _t.time()
    seq = iter([[Usage("work", "claude", True, windows=[Window("5h", 10.0)])]])
    m = Monitor(collect=lambda a, refresh_tokens=False: next(seq))
    orig = m._track_rate
    m._track_rate = lambda u: orig(u, now=now - 600)
    m.poll()

    # new process (self-update) a few minutes later: the gauge works on its first poll
    m2 = Monitor(collect=lambda a, refresh_tokens=False: [Usage("work", "claude", True, windows=[Window("5h", 30.0)])])
    m2.poll()
    assert m2.payload()["usages"][0]["rate"]["level"] == "high"  # +20% in 10 min


@pytest.fixture
def server():
    m = Monitor(collect=_fake_collect([42] * 10))
    m.poll()
    s = ConsoleServer(m, "127.0.0.1", 0).start_background()
    yield s
    s.shutdown()


def _req(url, token=None, host=None, data=None):
    h = {}
    if token:
        h[TOKEN_HEADER] = token
    if host:
        h["Host"] = host
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, headers=h, data=body, method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_server_page_embeds_boot(server):
    code, html = _req(server.url)
    assert code == 200 and server.token in html and "/*__BOOT__*/" not in html


def test_server_requires_token(server):
    assert _req(server.url + "api/usage")[0] == 401
    assert _req(server.url + "api/usage", token="wrong")[0] == 401
    assert _req(server.url + "api/action", data={"action": "import"})[0] == 401
    code, body = _req(server.url + "api/usage", token=server.token)
    assert code == 200 and json.loads(body)["usages"][0]["windows"][0]["used_percent"] == 42


def test_server_rejects_foreign_host(server):
    assert _req(server.url, host="evil.example:80")[0] == 403
    assert _req(server.url + "api/usage", token=server.token, host="evil.example")[0] == 403


def test_server_actions(server):
    code, body = _req(server.url + "api/action", token=server.token, data={"action": "add", "provider": "codex", "name": "cx"})
    assert code == 200 and json.loads(body)["ok"]
    assert [a.name for a in load_accounts()] == ["cx"]
    body = json.loads(_req(server.url + "api/action", token=server.token, data={"action": "add", "provider": "codex", "name": "cx"})[1])
    assert not body["ok"] and "already exists" in body["message"]
    body = json.loads(_req(server.url + "api/action", token=server.token, data={"action": "rm-rf"})[1])
    assert not body["ok"]


def test_actions_disabled_off_loopback():
    m = Monitor(collect=lambda a, refresh_tokens=False: [])
    s = ConsoleServer(m, "0.0.0.0", 0).start_background()
    try:
        code, body = _req(f"http://127.0.0.1:{s.port}/api/action", token=s.token, data={"action": "import"})
        assert code == 403
    finally:
        s.shutdown()


def test_dispatch_validation():
    with pytest.raises(actions.ActionError):
        actions.dispatch("add", {"provider": "claude", "name": "../evil"})
    with pytest.raises(actions.ActionError):
        actions.dispatch("launch", {"name": "ghost"})


def test_tooltip_truncates():
    from redline.tray import TOOLTIP_MAX, tooltip

    us = [Usage(f"account-{i}", "claude", True, windows=[Window("5h", 50.0)]) for i in range(20)]
    t = tooltip(us)
    assert len(t) <= TOOLTIP_MAX and t.startswith("redline · ") and "\naccount-0 50%" in t


def test_icon_renders():
    pytest.importorskip("PIL")
    from redline.icon import render_icon

    assert render_icon(None).size == (64, 64)
    assert render_icon(97.0, size=16).getpixel((8, 1))[3] > 0


def test_update_button_backend():
    """The console's version chip reads monitor.meta and triggers the tray's update action."""
    m = Monitor(collect=lambda a, refresh_tokens=False: [])
    m.meta["update"] = {"build": 5, "latest": 6, "state": "", "checked": 0}
    calls = []
    s = ConsoleServer(m, "127.0.0.1", 0, extra_actions={"update": lambda body: calls.append(body) or "installing build 6"})
    s.start_background()
    try:
        code, body = _req(s.url + "api/usage", token=s.token)
        assert json.loads(body)["meta"]["update"]["latest"] == 6
        code, body = _req(s.url + "api/action", token=s.token, data={"action": "update"})
        assert json.loads(body) == {"ok": True, "message": "installing build 6"} and len(calls) == 1
    finally:
        s.shutdown()


def test_child_env_resets_pyinstaller_state(monkeypatch):
    import sys as _sys

    from redline.procenv import child_env

    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", "C:/Temp/_MEI123")
    monkeypatch.setenv("_PYI_PARENT_PROCESS_LEVEL", "1")
    monkeypatch.setenv("_MEIPASS2", "C:/Temp/_MEI123")
    monkeypatch.setattr(_sys, "frozen", True, raising=False)
    env = child_env({"CLAUDE_CONFIG_DIR": "x"})
    assert not any(k.startswith(("_PYI_", "_MEIPASS")) for k in env)
    assert env["PYINSTALLER_RESET_ENVIRONMENT"] == "1" and env["CLAUDE_CONFIG_DIR"] == "x"


def test_second_launch_asks_running_instance_to_show(tmp_path, monkeypatch):
    from redline import instance

    shown = []
    m = Monitor(collect=lambda a, refresh_tokens=False: [])
    s = ConsoleServer(m, "127.0.0.1", 0, extra_actions={"show": lambda body: shown.append(1) or "shown"}).start_background()
    try:
        instance.write_state(s.port, s.token)
        assert instance.read_state()["port"] == s.port
        assert instance.ask_running_to_show() and shown == [1]
        instance.clear_state()
        assert instance.read_state() is None and not instance.ask_running_to_show()
    finally:
        s.shutdown()
    # a recorded instance that no longer answers -> False (caller offers a restart)
    instance.write_state(1, "dead")
    assert not instance.ask_running_to_show(timeout=0.5)


def test_acquire_instance_handoff_and_takeover(monkeypatch):
    from redline import instance, tray

    locks = iter([None])
    monkeypatch.setattr(tray, "_single_instance", lambda: next(locks, None))
    monkeypatch.setattr(instance, "ask_running_to_show", lambda timeout=3.0: True)
    assert tray._acquire_instance(after_update=False) is None  # live instance -> just hand over

    # stuck instance: user confirms -> stuck processes are killed and we take the lock
    seq = iter([None, None, "LOCK"])
    monkeypatch.setattr(tray, "_single_instance", lambda: next(seq, "LOCK"))
    monkeypatch.setattr(instance, "ask_running_to_show", lambda timeout=3.0: False)
    monkeypatch.setattr(instance, "stuck_pids", lambda: [111, 222])
    killed = []
    monkeypatch.setattr(instance, "kill", lambda pids: killed.extend(pids))
    monkeypatch.setattr(instance, "ask_yes_no", lambda text: True)
    monkeypatch.setattr(tray.time, "sleep", lambda s: None)
    assert tray._acquire_instance(after_update=False) == "LOCK" and killed == [111, 222]

    # user says no -> nothing is killed
    killed.clear()
    monkeypatch.setattr(tray, "_single_instance", lambda: None)
    monkeypatch.setattr(instance, "ask_yes_no", lambda text: False)
    assert tray._acquire_instance(after_update=False) is None and killed == []


@pytest.mark.skipif(os.name != "nt", reason="tasklist is Windows-only")
def test_stuck_pids_lists_other_processes_of_same_image():
    import subprocess
    import sys as _sys

    from redline import instance

    child = subprocess.Popen([_sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        pids = instance.stuck_pids()
        assert child.pid in pids and os.getpid() not in pids
    finally:
        child.kill()


@pytest.mark.skipif(os.name != "nt", reason="named mutex is Windows-only")
def test_single_instance_lock_is_released_when_holder_exits():
    """Regression: polling the lock used to leak a mutex handle, so after the old
    instance exited we kept the mutex alive ourselves and waited forever."""
    import subprocess
    import sys as _sys
    import time as _time

    from redline import tray

    holder = subprocess.Popen([_sys.executable, "-c",
                               "from redline.tray import _single_instance as s; import time; h = s(); "
                               "print('held' if h else 'busy', flush=True); time.sleep(4)"],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        for _ in range(5):  # poll a few times while it is held
            assert tray._single_instance() is None
            _time.sleep(0.2)
    finally:
        holder.wait(10)
    assert tray._single_instance() is not None


def test_console_page_is_well_formed():
    """Cheap guard against broken edits of the single-file console (e.g. an unclosed CSS rule)."""
    import shutil
    import subprocess
    import tempfile

    from redline.web import console_html

    html = console_html()
    css = html[html.index("<style>"):html.index("</style>")]
    assert css.count("{") == css.count("}")
    node = shutil.which("node")
    if node:  # a real syntax check of the page script
        js = html[html.index("<script>") + len("<script>"):html.index("</script>")]
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(js)
        try:
            r = subprocess.run([node, "--check", f.name], capture_output=True, text=True)
            assert r.returncode == 0, r.stderr
        finally:
            os.unlink(f.name)


def test_auto_update_waits_until_the_window_is_hidden(monkeypatch):
    """A background update must not close the console under the user; it installs on hide."""
    from redline import tray, update

    rel = update.Release(99, "build-99", "n", "u", "s", "h")
    installed = []
    monkeypatch.setattr(update, "check", lambda: (rel, "build 99 is available"))
    monkeypatch.setattr(update, "can_self_update", lambda: True)
    monkeypatch.setattr(update, "install", lambda r, extra_args=None, window=None: installed.append((r.build, window)))
    monkeypatch.setattr(tray.threading, "Timer", lambda *a, **k: type("T", (), {"start": lambda self: None})())
    monkeypatch.setattr(tray.threading, "Thread", lambda target, args=(), **k: type("T", (), {"start": lambda self: target(*args)})())

    app = tray.TrayApp.__new__(tray.TrayApp)
    app.monitor = Monitor(collect=lambda a, refresh_tokens=False: [])
    app.window, app.visible, app.minimized, app.quitting = None, True, False, False
    app._pending_update, app._notify = None, lambda text: None

    app.check_updates(manual=False, install=True)
    assert installed == [] and app.monitor.meta["update"]["state"] == "pending"
    app.hide()
    assert installed == [(99, {"show": False})]

    installed.clear()
    app.visible = True
    app.check_updates(manual=True, install=True)  # clicking the version installs right away
    assert installed and installed[0][1]["show"] is True
