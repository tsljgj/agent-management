"""Regressions for the tray / console / self-update / single-instance review.

Nothing here kills a process, shows a dialog, writes the registry, starts the real tray or
talks to the network: kill, dialogs and autostart writes are refused by an autouse fixture,
and the only HTTP goes to throwaway servers on 127.0.0.1.
"""

import hashlib
import json
import os
import runpy
import subprocess
import sys
import threading
import types
import urllib.request
from pathlib import Path

import pytest

from redline import actions, applog, autostart, instance, tray, update
from redline.config import load_accounts, load_settings
from redline.models import ProviderError, Usage, Window
from redline.monitor import Monitor
from redline.web import TOKEN_HEADER, ConsoleServer
from test_countdown_ui import run_console_js  # noqa: F401  (fixture: the console's JS under node)

ROOT = Path(__file__).parents[1]
ENTRY = ROOT / "scripts" / "redline_tray.py"
REAL_KILL = instance.kill
REL = update.Release(99, "build-99", "n", "u", "s", "")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("REDLINE_HOME", str(tmp_path / "am"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.delenv("REDLINE_UPDATE_FEED", raising=False)
    (tmp_path / "home").mkdir()


@pytest.fixture(autouse=True)
def boxes(monkeypatch):
    """No real kills, dialogs or registry writes, whatever a test forgets to stub."""
    def refuse_kill(pids):
        raise AssertionError(f"a test tried to kill {pids}")

    shown = []
    monkeypatch.setattr(instance, "kill", refuse_kill)
    monkeypatch.setattr(instance, "ask_yes_no", lambda text: pytest.fail("unexpected dialog: " + text))
    monkeypatch.setattr(tray, "_message_box", shown.append)
    monkeypatch.setattr(autostart, "set_enabled", lambda on: pytest.fail("registry write"))
    return shown


@pytest.fixture
def console_js(run_console_js, monkeypatch):  # noqa: F811
    """run_console_js, plus: the scenario must reach its end. A promise that never settles
    would otherwise just let node exit 0 and the test pass without having checked anything."""
    real, results = subprocess.run, []

    def recording(*a, **k):
        results.append(real(*a, **k))
        return results[-1]

    def run(scenario):
        monkeypatch.setattr(subprocess, "run", recording)
        try:
            run_console_js(scenario)
        finally:
            monkeypatch.setattr(subprocess, "run", real)
        assert "SCENARIO-DONE" in results[-1].stdout, results[-1].stdout

    return run


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += s


def _proc(pid, ppid, name, cmd, session=1):
    return {"pid": pid, "ppid": ppid, "session": session, "name": name, "cmd": cmd}


def _post(server, body):
    req = urllib.request.Request(server.url + "api/action", method="POST", data=json.dumps(body).encode(),
                                 headers={TOKEN_HEADER: server.token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def _fake_exe(tmp_path, monkeypatch, payload=b"NEW BUILD"):
    exe = tmp_path / "app" / "redline.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"OLD BUILD")
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(update, "can_self_update", lambda: True)
    good = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(update, "_get_any", lambda url, timeout=20: (good + "  redline.exe").encode())
    return exe, good


def _bare_app():
    app = tray.TrayApp.__new__(tray.TrayApp)  # no pystray, no window
    app.monitor = Monitor(collect=lambda a, refresh_tokens=False: [])
    app.window, app.visible, app.minimized, app.quitting = None, False, False, False
    app._pending_update, app._notify = None, lambda text: None
    return app


# ------------------------------------------------ 1/2: who gets killed when the lock is stuck


def test_stuck_instance_is_only_ever_a_redline_tray():
    me = 100
    procs = [
        _proc(1, 0, "explorer.exe", r"C:\Windows\explorer.exe"),
        _proc(90, 1, "cmd.exe", "cmd.exe"),
        _proc(95, 90, "redline.exe", r'"C:\Py\Scripts\redline.exe" tray'),  # pip launcher that started us
        _proc(me, 95, "python.exe", r'"C:\Py\python.exe" "C:\Py\Scripts\redline.exe" tray'),
        _proc(110, me, "cmd.exe", "cmd.exe /k claude"),  # a terminal we started ...
        _proc(111, 110, "redline.exe", r'"C:\x\redline.exe" --background'),  # ... and a redline run in it
        _proc(200, 1, "python.exe", 'python.exe -c "import time; time.sleep(60)"'),  # unrelated python
        _proc(201, 1, "python.exe", "python.exe -m jupyter lab"),
        _proc(202, 1, "pythonw.exe", 'pythonw.exe -c "print(1)" -m redline tray'),  # -c: not redline
        _proc(300, 1, "redline.exe", r'"C:\Apps\redline.exe" --background'),  # stuck exe: bootloader
        _proc(301, 300, "redline.exe", r'"C:\Apps\redline.exe" --background'),  # ... and its python
        _proc(302, 1, "redline.exe", r'"C:\Apps\redline.exe" "https://claude.ai/oauth?x=1"'),  # login callback
        _proc(303, 1, "redline.exe", r'"C:\Py\Scripts\redline.exe" usage'),  # another CLI command
        _proc(400, 1, "pythonw.exe", r'"C:\Py\pythonw.exe" -m redline tray --background'),  # stuck source tray
        _proc(401, 1, "python.exe", r"python.exe scripts\redline_tray.py --background"),
        _proc(500, 1, "redline.exe", r'"C:\Apps\redline.exe"', session=2),  # another user's session
    ]
    assert instance.pick_stuck(procs, me) == [300, 301, 400, 401]


def test_after_an_update_only_the_old_instance_that_started_us_may_go():
    procs = [
        _proc(1, 0, "explorer.exe", "explorer.exe"),
        _proc(5001, 1, "redline.exe", r'"C:\Apps\redline.exe"'),  # old instance: bootloader
        _proc(5003, 5001, "redline.exe", r'"C:\Apps\redline.exe"'),  # old instance: python child
        _proc(6001, 5003, "cmd.exe", "cmd.exe /k claude"),  # a terminal it started: never
        _proc(7001, 5003, "redline.exe", r'"C:\Apps\redline.exe" --updated-from 8'),  # our bootloader
        _proc(7003, 7001, "redline.exe", r'"C:\Apps\redline.exe" --updated-from 8'),  # us
    ]
    assert instance.pick_stuck(procs, 7003) == []  # ancestors are off limits ...
    assert instance.pick_stuck(procs, 7003, previous=5003) == [5001, 5003]  # ... but the one we replace


@pytest.mark.skipif(os.name != "nt", reason="Windows process table")
def test_restart_prompt_ends_redline_and_no_other_python(monkeypatch):
    me, parent = os.getpid(), os.getppid()
    table = [
        _proc(parent, 1, "cmd.exe", "cmd.exe"),
        _proc(me, parent, "python.exe", "python.exe -m redline tray"),
        _proc(201, 1, "python.exe", 'python.exe -c "import time; time.sleep(60)"'),
        _proc(203, 1, "pythonw.exe", r"pythonw.exe C:\tools\notebook.py"),
        _proc(301, 1, "redline.exe", r'"C:\Users\me\Desktop\redline.exe" --background'),
        _proc(305, 301, "redline.exe", r'"C:\Users\me\Desktop\redline.exe" --background'),
        _proc(309, 305, "cmd.exe", "cmd.exe /k claude"),
    ]
    killed = []
    monkeypatch.setattr(instance, "_processes", lambda: table)
    monkeypatch.setattr(instance, "kill", killed.extend)
    monkeypatch.setattr(instance, "ask_running_to_show", lambda timeout=3.0: False)
    monkeypatch.setattr(instance, "ask_yes_no", lambda text: True)
    monkeypatch.setattr(tray, "_single_instance", lambda: "LOCK" if killed else None)
    monkeypatch.setattr(tray, "time", FakeClock())
    assert tray._acquire_instance(after_update=False) == "LOCK"
    assert killed == [301, 305]


@pytest.mark.skipif(os.name != "nt", reason="Windows process table")
def test_after_update_takeover_spares_us_and_the_old_ones_terminals(monkeypatch):
    me, parent = os.getpid(), os.getppid()
    table = [
        _proc(1, 0, "explorer.exe", "explorer.exe"),
        _proc(5001, 1, "redline.exe", r'"C:\Apps\redline.exe"'),
        _proc(5003, 5001, "redline.exe", r'"C:\Apps\redline.exe"'),
        _proc(6001, 5003, "cmd.exe", "cmd.exe /k claude"),
        _proc(parent, 5003, "redline.exe", r'"C:\Apps\redline.exe" --updated-from 8'),
        _proc(me, parent, "redline.exe", r'"C:\Apps\redline.exe" --updated-from 8'),
    ]
    killed = []
    monkeypatch.setattr(instance, "_processes", lambda: table)
    monkeypatch.setattr(instance, "kill", killed.extend)
    monkeypatch.setattr(tray, "_single_instance", lambda: "LOCK" if killed else None)
    monkeypatch.setattr(tray, "time", FakeClock())
    assert tray._acquire_instance(after_update=True, show=False, previous=5003) == "LOCK"
    assert killed == [5001, 5003]


def test_nothing_identifiable_means_nothing_is_killed(monkeypatch, boxes):
    monkeypatch.setattr(instance, "stuck_pids", lambda *a: [])
    monkeypatch.setattr(instance, "ask_running_to_show", lambda timeout=3.0: False)
    monkeypatch.setattr(tray, "_single_instance", lambda: None)
    monkeypatch.setattr(tray, "time", FakeClock())
    assert tray._acquire_instance(after_update=False) is None
    assert boxes and "could not be identified" in boxes[0]


@pytest.mark.skipif(os.name != "nt", reason="taskkill")
def test_kill_is_not_a_tree_kill(monkeypatch):
    calls = []
    monkeypatch.setattr(instance, "kill", REAL_KILL)
    monkeypatch.setattr(instance.subprocess, "run", lambda args, **kw: calls.append(args))
    instance.kill([111, 222])
    assert calls == [["taskkill", "/F", "/PID", "111"], ["taskkill", "/F", "/PID", "222"]]


@pytest.mark.skipif(os.name != "nt", reason="Windows process table")
def test_real_process_table_never_offers_a_plain_python_process(monkeypatch):
    # read-only: one snapshot of the process table; nothing is killed or contacted
    bystander = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    real, tables = instance._processes, []
    monkeypatch.setattr(instance, "_processes", lambda: tables.append(real()) or tables[-1])
    try:
        stuck = instance.stuck_pids()
    finally:
        bystander.stdin.close()
        bystander.wait(10)
    assert tables[0] and {os.getpid(), bystander.pid} <= {p["pid"] for p in tables[0]}
    assert bystander.pid not in stuck and os.getpid() not in stuck


# ------------------------------------------------ 3: one install at a time, verified on disk


def test_only_one_install_runs_at_a_time(monkeypatch):
    started, release, installs = threading.Event(), threading.Event(), []

    def slow_install(rel, extra_args=None, window=None):
        installs.append(rel.build)
        started.set()
        release.wait(5)

    monkeypatch.setattr(update, "install", slow_install)
    monkeypatch.setattr(update, "check", lambda: pytest.fail("checked for updates while installing"))
    monkeypatch.setattr(tray.threading, "Timer", lambda *a, **k: types.SimpleNamespace(start=lambda: None))
    app, first = _bare_app(), {}
    t = threading.Thread(target=lambda: first.setdefault("msg", app._install(REL, True)))
    t.start()
    try:
        assert started.wait(5)
        assert "already" in app._install(REL, True)  # a second click on the version
        assert "already" in app.check_updates(manual=True, install=True)  # the menu or the update loop
    finally:
        release.set()
        t.join(5)
    assert installs == [99] and "restarting" in first["msg"]
    assert not tray.TrayApp._install_lock.locked()


def test_install_verifies_the_file_on_disk_not_the_stream(tmp_path, monkeypatch):
    exe, good = _fake_exe(tmp_path, monkeypatch)

    def torn(url, dest):  # the stream hashed fine, but what is on disk was overwritten meanwhile
        dest.write_bytes(b"NEW\0\0\0\0\0\0")
        return good

    monkeypatch.setattr(update, "_download", torn)
    monkeypatch.setattr(update, "launch_detached", lambda *a, **k: pytest.fail("launched a corrupt exe"))
    with pytest.raises(ProviderError):
        update.install(REL)
    assert exe.read_bytes() == b"OLD BUILD"
    assert sorted(p.name for p in exe.parent.iterdir()) == ["redline.exe"]


def test_parallel_installs_download_to_files_of_their_own(tmp_path, monkeypatch):
    exe, good = _fake_exe(tmp_path, monkeypatch)
    both, first_done, dests, launched, errors = threading.Barrier(2, timeout=5), threading.Event(), [], [], []

    def download(url, dest):
        dests.append(dest)
        both.wait()  # both downloads are in progress at the same time
        assert all(d.exists() for d in dests)
        if dest == dests[1]:
            first_done.wait(5)
        dest.write_bytes(b"NEW BUILD")
        return good

    def launch(exe_path, args, reset_env=True):
        launched.append(exe_path)
        first_done.set()

    monkeypatch.setattr(update, "_download", download)
    monkeypatch.setattr(update, "launch_detached", launch)

    def run():
        try:
            update.install(REL)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert errors == [] and len(launched) == 2
    assert len(set(dests)) == 2 and all(d.parent == exe.parent and d.name != "redline.exe.download" for d in dests)
    assert exe.read_bytes() == b"NEW BUILD" and not list(exe.parent.glob("*.download"))


def test_cleanup_removes_interrupted_downloads(tmp_path, monkeypatch):
    exe, _ = _fake_exe(tmp_path, monkeypatch)
    monkeypatch.setattr(update, "is_packaged", lambda: True)
    for name in ("redline.exe.old", "redline.exe.download", "redline.exe.k3j9.download", "notes.download"):
        (exe.parent / name).write_bytes(b"x")
    update.cleanup_old()
    assert sorted(p.name for p in exe.parent.iterdir()) == ["notes.download", "redline.exe"]


def test_console_sends_one_update_request_at_a_time(console_js):
    console_js(r"""
(async () => {
  const sent = [];
  fetch = async (path, opts) => { sent.push(JSON.parse(opts.body));
    return {ok: true, json: async () => ({ok: true, message: "installing"})}; };
  const first = checkUpdate(), second = checkUpdate();   // double-click: the 2nd while the 1st is in flight
  assert.equal(sent.length, 1);
  assert.equal(elements["#ver"].disabled, true);
  await Promise.all([first, second]);
  assert.equal(elements["#ver"].disabled, false);
  DATA = {usages: [], meta: {update: {build: 8, latest: 9, state: "installing"}}};
  await run("update");                       // the server already says it is installing
  assert.equal(sent.length, 1);
  DATA = {usages: [], meta: {update: {build: 8, latest: 9, state: ""}}};
  await run("update");
  assert.deepEqual(sent, [{action: "update"}, {action: "update"}]);
  console.log("SCENARIO-DONE");
})();
""")


# ------------------------------------------------ 4/14: only main builds are offered


def test_updater_ignores_prereleases_and_drafts(tmp_path, monkeypatch):
    feed = tmp_path / "feed.json"
    monkeypatch.setenv("REDLINE_UPDATE_FEED", str(feed))
    doc = {"tag_name": "build-50", "name": "b", "html_url": "", "assets": [
        {"name": "redline.exe", "browser_download_url": "x"},
        {"name": "redline.exe.sha256", "browser_download_url": "y"}]}
    feed.write_text(json.dumps(doc), encoding="utf-8")
    assert update.latest().build == 50
    for flag in ("prerelease", "draft"):
        feed.write_text(json.dumps({**doc, flag: True}), encoding="utf-8")
        assert update.latest() is None


def test_ci_marks_only_main_builds_latest():
    text = (ROOT / ".github" / "workflows" / "windows-tray.yml").read_text(encoding="utf-8")
    publish = text.split("- name: Publish release", 1)[1].split("- name:", 1)[0]
    attach = text.split("- name: Attach to versioned release", 1)[1]
    assert "make_latest: ${{ github.ref == 'refs/heads/main' }}" in publish
    assert "prerelease: ${{ github.ref != 'refs/heads/main' }}" in publish
    assert "make_latest: false" in attach


# ------------------------------------------------ 5 (console half): never wake with names: []


def test_console_does_not_wake_an_empty_tab(console_js):
    console_js(r"""
(async () => {
  const sent = [];
  fetch = async (path, opts) => { sent.push(JSON.parse(opts.body));
    return {ok: true, json: async () => ({ok: true, message: "waking"})}; };
  DATA = {usages: [{account: "me@x.com", provider: "claude", ok: false, stale: false,
                    error: "not logged in", windows: []}], meta: {}};
  TAB = "codex";
  await run("wake");
  elements["#wake"].onclick();               // a stale ⚡: nothing on this tab needs it
  TAB = "plans";
  await run("wake");
  assert.equal(sent.length, 0);
  assert.ok(elements["#log"].children.some(d => /nothing to wake on this tab/.test(d.textContent)));
  TAB = "claude";
  elements["#wake"].onclick();
  await run("wake");
  assert.deepEqual(sent, [{action: "wake", names: ["me@x.com"], provider: "claude"},
                          {action: "wake", names: ["me@x.com"], provider: "claude"}]);
  console.log("SCENARIO-DONE");
})();
""")


# ------------------------------------------------ 6: `redline.exe tray --background`


@pytest.mark.parametrize("argv, expected", [
    (["tray", "--background"], ["tray", "--background"]),
    (["--background"], ["tray", "--background"]),
    (["tray"], ["tray"]),
    ([], ["tray"]),
    (["https://claude.ai/cb?code=1"], ["https://claude.ai/cb?code=1"]),
])
def test_entry_script_accepts_an_explicit_tray(monkeypatch, argv, expected):
    from redline import cli

    seen = []
    monkeypatch.setattr(cli, "main", lambda args=None: seen.append(args) or 0)
    monkeypatch.setattr(sys, "argv", ["redline.exe", *argv])
    with pytest.raises(SystemExit):
        runpy.run_path(str(ENTRY), run_name="__main__")
    assert seen == [expected]
    if expected[0] == "tray":
        cli.build_parser().parse_args(expected)  # argparse would exit 2 on `tray tray`


# ------------------------------------------------ 7: a relaunch that fails is rolled back


def test_failed_relaunch_restores_the_working_exe(tmp_path, monkeypatch):
    exe, good = _fake_exe(tmp_path, monkeypatch)
    monkeypatch.setattr(update, "_download", lambda url, dest: (dest.write_bytes(b"NEW BUILD"), good)[1])

    def blocked(*a, **k):
        raise OSError(225, "Operation did not complete successfully because the file contains a virus")

    monkeypatch.setattr(update, "launch_detached", blocked)
    with pytest.raises(OSError):
        update.install(REL)
    assert exe.read_bytes() == b"OLD BUILD"
    assert sorted(p.name for p in exe.parent.iterdir()) == ["redline.exe"]
    assert update.take_handoff() is None  # the next launch must not look like an update relaunch


# ------------------------------------------------ 8: rename by reference keeps the monitor's state


def test_rename_by_reference_carries_the_monitor_state(monkeypatch):
    actions.add_account("codex", "work")
    actions.add_account("claude", "work")
    monkeypatch.setattr("redline.actions.adopt_email_names", lambda: [])
    m = Monitor(collect=lambda a, refresh_tokens=False: [Usage("work", "codex", True, windows=[Window("5h", 40.0)]),
                                                         Usage("work", "claude", True, windows=[Window("5h", 10.0)])])
    m.poll()
    s = ConsoleServer(m, "127.0.0.1", 0).start_background()
    try:
        # typed on the CLAUDE tab, addressed to the codex account
        assert _post(s, {"action": "rename", "name": "codex:work", "new": "w2", "provider": "claude"})["ok"]
        # the console's double-click rename on the CODEX tab
        assert _post(s, {"action": "rename", "name": "w2", "new": "w3", "provider": "codex"})["ok"]
    finally:
        s.shutdown()
    assert sorted(a.key for a in load_accounts()) == ["claude:work", "codex:w3"]
    assert set(m._last_ok) == {("codex", "w3"), ("claude", "work")}
    assert sorted((u.provider, u.account) for u in m.usages) == [("claude", "work"), ("codex", "w3")]


def test_console_rename_names_the_rows_provider(console_js):
    console_js(r"""
(async () => {
  const sent = [], handlers = {};
  fetch = async (path, opts) => { if (!opts.body) throw new Error("offline");
    sent.push(JSON.parse(opts.body)); return {ok: true, json: async () => ({ok: true, message: "renamed"})}; };
  const inp = {value: "work", focus() {}, select() {}, addEventListener(type, fn) { handlers[type] = fn; }};
  const nm = {innerHTML: "", textContent: "work", querySelector: () => nm.innerHTML ? inp : null};
  const card = {dataset: {name: "work"}, querySelector: sel => sel === ".nm" ? nm : null};
  TAB = "codex";
  startRename(card);
  TAB = "claude";                            // the tab changes before the edit is committed
  inp.value = "w2";
  handlers.keydown({key: "Enter", preventDefault() {}, stopPropagation() {}});
  for (let i = 0; i < 20; i++) await Promise.resolve();
  assert.deepEqual(sent, [{action: "rename", provider: "codex", name: "work", new: "w2"}]);
  console.log("SCENARIO-DONE");
})();
""")


# ------------------------------------------------ 9/10/11: start-up and handover


def test_second_launch_gives_a_starting_instance_time_to_answer(monkeypatch):
    answers, asked = iter([False, False, True]), []
    monkeypatch.setattr(tray, "_single_instance", lambda: None)
    monkeypatch.setattr(tray, "time", FakeClock())
    monkeypatch.setattr(instance, "ask_running_to_show", lambda timeout=3.0: asked.append(1) or next(answers))
    monkeypatch.setattr(instance, "stuck_pids", lambda *a: pytest.fail("a starting instance was treated as stuck"))
    assert tray._acquire_instance(after_update=False) is None and len(asked) == 3


def test_background_launch_hands_over_without_opening_the_console(monkeypatch):
    monkeypatch.setattr(tray, "_single_instance", lambda: None)
    monkeypatch.setattr(instance, "ask_running_to_show", lambda timeout=3.0: pytest.fail("asked to show"))
    monkeypatch.setattr(instance, "ping_running", lambda timeout=3.0: True)
    assert tray._acquire_instance(after_update=False, show=False) is None


def test_ping_running_does_not_show_the_window():
    shown = []
    m = Monitor(collect=lambda a, refresh_tokens=False: [])
    s = ConsoleServer(m, "127.0.0.1", 0, extra_actions={"show": lambda body: shown.append(1) or "shown"}).start_background()
    try:
        assert not instance.ping_running()  # no instance.json
        instance.write_state(s.port, s.token)
        assert instance.ping_running() and shown == []
        instance.write_state(s.port, "not-the-token")
        assert not instance.ping_running()
    finally:
        s.shutdown()


def test_console_keeps_its_port_across_restarts():
    m = Monitor(collect=lambda a, refresh_tokens=False: [])
    first = tray._console_server(m, load_settings(), {})
    port = first.port
    first.httpd.server_close()
    assert load_settings()["console_port"] == port
    second = tray._console_server(m, load_settings(), {})
    try:
        assert second.port == port  # same origin: the page's tab / sort / details survive
        busy = tray._console_server(m, load_settings(), {})  # e.g. the old instance still holds it
        busy.httpd.server_close()
        assert busy.port != port and load_settings()["console_port"] == busy.port
    finally:
        second.httpd.server_close()


def test_run_tray_announces_itself_before_the_gui(monkeypatch):
    monkeypatch.setitem(sys.modules, "pystray", types.ModuleType("pystray"))
    monkeypatch.setitem(sys.modules, "webview", None)  # -> browser fallback, no GUI
    monkeypatch.setattr(applog, "setup", lambda: None)
    calls, seen = [], []
    monkeypatch.setattr(tray, "_acquire_instance",
                        lambda after_update, show=True, previous=None: calls.append((after_update, show, previous)) or "LOCK")

    class FakeApp:
        def __init__(self, monitor, server, webview):
            self.server = server
            seen.append((instance.read_state(), server))

        def run(self, **kw):
            self.server.shutdown()  # ... and the instance exits
            return 0

    monkeypatch.setattr(tray, "TrayApp", FakeApp)
    update.write_handoff({"show": False})  # started by a self-update
    assert tray.run_tray(show=False) == 0
    assert tray.run_tray(show=True) == 0  # a later normal start
    assert calls == [(True, False, os.getpid()), (False, True, None)]
    (state1, server1), (state2, server2) = seen
    assert (state1["port"], state1["token"]) == (server1.port, server1.token)  # written before the GUI
    assert (state2["port"], state2["token"]) == (server2.port, server2.token)
    assert server2.port == server1.port  # the console's origin did not change


def test_run_tray_second_launch_does_not_bind_a_server(monkeypatch):
    monkeypatch.setitem(sys.modules, "pystray", types.ModuleType("pystray"))
    monkeypatch.setitem(sys.modules, "webview", None)
    monkeypatch.setattr(applog, "setup", lambda: None)
    monkeypatch.setattr(tray, "_acquire_instance", lambda after_update, show=True, previous=None: None)
    monkeypatch.setattr(tray, "ConsoleServer", lambda *a, **k: pytest.fail("bound a server we don't need"))
    assert tray.run_tray(show=True) == 0 and instance.read_state() is None


# ------------------------------------------------ 12: autostart stays with the copy the user chose


def test_autostart_refresh_only_touches_this_copys_entry(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Apps\redline\redline.exe")
    written = []
    monkeypatch.setattr(autostart, "set_enabled", written.append)
    cases = [
        (None, False),  # off: stays off
        (r'"C:\Apps\redline\redline.exe" --background', False),  # already current
        (r'"C:\Apps\redline\redline.exe"', True),  # ours, from an older build: gets --background
        (r'"C:\Users\me\Downloads\redline.exe" --background', False),  # another copy
        (r'"C:\Py\pythonw.exe" -m redline tray --background', False),  # a source checkout
    ]
    for value, rewrite in cases:
        written.clear()
        monkeypatch.setattr(autostart, "registered", lambda v=value: v)
        assert autostart.refresh() is rewrite and written == ([True] if rewrite else []), value
