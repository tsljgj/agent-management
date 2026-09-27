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
