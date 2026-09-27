"""System tray app: live usage icon + a cyberpunk console window.

Windows: lives in the notification area ("show hidden icons"); left-click opens the
console as a frameless flyout above the taskbar (Edge WebView2 via pywebview).
Without pywebview installed the console opens in the default browser instead.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

from . import actions, autostart, update
from ._build import BUILD
from .applog import log
from .config import load_accounts, load_settings, redline_home, save_setting
from .models import Usage
from .monitor import Monitor
from .web import TOKEN_HEADER, ConsoleServer

WIN_W, WIN_H = 520, 620
MIN_W, MIN_H = 300, 160
TOOLTIP_MAX = 120  # Windows caps tray tooltips at 128 chars


def _single_instance() -> object | None:
    """Return a handle that must stay alive, or None if another instance is running."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = k32.CreateMutexW(None, False, "Local\\redline")
        if not handle:
            return None
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            # We got a handle to the *other* instance's mutex. It must be closed, or it
            # keeps the mutex alive after that instance exits and we'd wait on ourselves.
            k32.CloseHandle(handle)
            return None
        return handle
    import fcntl

    path = redline_home() / "tray.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def _message_box(text: str) -> None:
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, "redline", 0x40)
    else:
        print(text, file=sys.stderr)


def tooltip(usages: list[Usage]) -> str:
    if not usages:
        return "redline - no accounts"
    parts = []
    for u in usages:
        if not u.ok and not u.stale:
            parts.append(f"{u.account} !")
            continue
        w = next((w for w in u.windows if w.name == "5h"), u.windows[0] if u.windows else None)
        mark = "?" if u.stale else ""
        parts.append(f"{u.account} {w.used_percent:.0f}%{mark}" if w and w.used_percent is not None else u.account)
    stamp = max(u.fetched_at for u in usages).astimezone().strftime("%H:%M")
    text = f"redline · {stamp}\n" + "\n".join(parts)
    return text if len(text) <= TOOLTIP_MAX else text[: TOOLTIP_MAX - 1] + "…"


class JsApi:
    """Exposed to the page as window.pywebview.api (underscore attrs are not exported)."""

    def __init__(self, app: "TrayApp"):
        self._app = app

    def hide(self):
        self._app.hide()

    def minimize(self):
        if self._app.window:
            self._app.window.minimize()
            self._app.visible = False
            self._app.minimized = True  # still on screen as far as updates are concerned

    # The window is frameless (no OS resize border), so the page drives resizing.
    def resize(self, width, height, edge=""):
        w = self._app.window
        if not w:
            return
        from webview.window import FixPoint

        fix = FixPoint.NORTH | FixPoint.WEST
        if "w" in edge:
            fix = (fix & ~FixPoint.WEST) | FixPoint.EAST  # dragging the left edge keeps the right edge put
        if "n" in edge:
            fix = (fix & ~FixPoint.NORTH) | FixPoint.SOUTH
        w.resize(max(MIN_W, int(width)), max(MIN_H, int(height)), fix)

    def save_size(self, width, height):
        save_setting("window_size", [max(MIN_W, int(width)), max(MIN_H, int(height))])

    def fit(self, width, height):
        """Auto-fit the height to the content (kept anchored to the bottom, near the tray)."""
        w = self._app.window
        if not w or load_settings().get("window_size"):
            return
        from webview.window import FixPoint

        limit = self._app.screen_height() * 0.85 if self._app.screen_height() else 900
        w.resize(max(MIN_W, int(width)), int(min(max(MIN_H, height), limit)), FixPoint.SOUTH | FixPoint.WEST)

    def reset_size(self):
        save_setting("window_size", None)


class TrayApp:
    def __init__(self, monitor: Monitor, server: ConsoleServer, webview_mod=None):
        import pystray

        from .icon import render_icon

        self.pystray = pystray
        self.render_icon = render_icon
        self.monitor = monitor
        self.server = server
        self.webview = webview_mod
        self.window = None
        self.visible = False
        self.minimized = False
        self.quitting = False
        self._pending_update = None  # a release waiting for the window to be hidden
        self._handoff_window: dict = {}  # how the window was before a self-update restarted us
        self.status = "syncing…"

        M, Item = pystray.Menu, pystray.MenuItem
        self.icon = pystray.Icon(
            "redline",
            render_icon(None),
            "redline",
            menu=M(
                Item(lambda _: self.status, None, enabled=False),
                Item("Open console", self.toggle, default=True),
                Item("Web (Chrome profile)", M(lambda: self._account_items(self._web))),
                Item("Jack in (terminal)", M(lambda: self._account_items(self._launcher))),
                M.SEPARATOR,
                Item("Refresh now", lambda: threading.Thread(target=monitor.refresh_now, daemon=True).start()),
                Item("Scan for accounts", self._scan),
                Item("Wake all (refresh / log in)", self._wake),
                Item(
                    "Auto-refresh expired tokens",
                    self._toggle_auto_refresh,
                    checked=lambda _: self.monitor.refresh_tokens,
                ),
                Item("Open console in browser", lambda: webbrowser.open(server.url)),
                M.SEPARATOR,
                Item(
                    "Start with Windows",
                    self._toggle_autostart,
                    checked=lambda _: autostart.is_enabled(),
                    visible=autostart.supported(),
                ),
                Item(lambda _: f"Check for updates (build {BUILD})" if BUILD else "Check for updates",
                     lambda: threading.Thread(target=self.check_updates, args=(True,), daemon=True).start(),
                     visible=update.is_packaged()),
                Item(
                    "Auto-update",
                    self._toggle_auto_update,
                    checked=lambda _: load_settings()["auto_update"],
                    visible=update.is_packaged(),
                ),
                Item("Open log file", lambda: self._open_log()),
                Item("Quit", self.quit),
            ),
        )
        monitor.listeners.append(self.on_update)
        server.extra_actions["update"] = self._update_action
        server.extra_actions["show"] = lambda body: (threading.Thread(target=self.show, daemon=True).start(), "shown")[1]
        server.extra_actions["open-log"] = lambda body: self._open_log()
        if update.is_packaged():
            self._set_update_meta(None)

    # ------------------------------------------------------------ menu

    def _account_items(self, make_handler):
        Item = self.pystray.MenuItem
        by_name = {f"{u.provider}:{u.account}": u for u in self.monitor.usages}
        items = []
        for a in load_accounts():
            u = by_name.get(a.key)
            w = next((w for w in u.windows if w.name == "5h"), None) if u and (u.ok or u.stale) else None
            label = f"{a.name}  ({a.provider}{f', 5h {w.used_percent:.0f}%' if w and w.used_percent is not None else ''})"
            items.append(Item(label, make_handler(a.key)))
        return items or [Item("no accounts", None, enabled=False)]

    def _web(self, name: str):
        def go(icon=None, item=None):
            try:
                self.monitor.log("info", actions.dispatch("web", {"name": name}))
            except Exception as e:
                self._notify(str(e))
        return go

    def _scan(self, icon=None, item=None):
        def run():
            msg = actions.dispatch("scan", {})
            for line in msg.splitlines():
                self.monitor.log("out", line)
            self._notify(msg.splitlines()[-1])
            self.monitor.poll()
        threading.Thread(target=run, daemon=True).start()

    def _wake(self, icon=None, item=None):
        mon = self.monitor
        msg = actions.dispatch("wake", {
            "log": lambda level, text: mon.log(level, text, alert=level == "crit"),
            "on_done": mon.poll,
        })
        mon.log("sys", msg)
        self.show()  # progress is shown in the console log

    def _toggle_auto_refresh(self, icon=None, item=None):
        self.monitor.refresh_tokens = not self.monitor.refresh_tokens
        save_setting("auto_refresh", self.monitor.refresh_tokens)
        threading.Thread(target=self.monitor.poll, daemon=True).start()

    def _launcher(self, name: str):
        def go(icon=None, item=None):
            try:
                actions.launch(name)
            except actions.ActionError as e:
                self._notify(str(e))
        return go

    def _toggle_autostart(self, icon=None, item=None):
        autostart.set_enabled(not autostart.is_enabled())

    # ------------------------------------------------------------ updates

    def on_update(self, usages: list[Usage], alerts: list[dict]) -> None:
        peak = self.monitor.max_percent("5h")
        fault = any(not u.ok for u in usages)
        self.icon.icon = self.render_icon(peak, fault=fault)
        self.icon.title = tooltip(usages)
        ok = sum(u.ok for u in usages)
        self.status = f"5h peak {peak:.0f}%  ·  {ok}/{len(usages)} online" if peak is not None else f"{ok}/{len(usages)} online"
        try:
            self.icon.update_menu()
        except Exception:
            pass
        for a in alerts:
            self._notify(a["text"])

    def _notify(self, text: str) -> None:
        if getattr(self.icon, "HAS_NOTIFICATION", False):
            try:
                self.icon.notify(text, "redline")
            except Exception:
                pass

    # ------------------------------------------------------------ window

    def toggle(self, icon=None, item=None):
        if self.visible:
            self.hide()
        else:
            self.show()

    def show(self):
        if self.window is None:
            if self.webview is None:
                webbrowser.open(self.server.url)
            else:
                self._show_when_ready = True  # GUI not up yet: show right after it starts
            return
        try:
            self.window.show()
            self.window.restore()
        except Exception:
            log.exception("show failed")
            return
        self.visible = True
        self.minimized = False
        self.monitor.meta["window_visible"] = True

    def hide(self):
        if self.window is not None:
            self.window.hide()
        self.visible = False
        self.minimized = False
        self.monitor.meta["window_visible"] = False
        rel, self._pending_update = self._pending_update, None
        if rel is not None and not self.quitting:  # the user is done looking: install now, unseen
            threading.Thread(target=self._install, args=(rel, False), daemon=True).start()

    def _on_closing(self):
        if self.quitting:
            return True
        self.hide()
        return False  # cancel the close; keep living in the tray

    # ------------------------------------------------------------ self-update

    UPDATE_EVERY = 6 * 3600

    def _update_loop(self):
        time.sleep(15)  # let the first sync finish
        while not self.quitting:
            self.check_updates(manual=False)
            time.sleep(self.UPDATE_EVERY)

    def _set_update_meta(self, latest: int | None, state: str = "") -> None:
        self.monitor.meta["update"] = {"build": BUILD, "latest": latest, "state": state, "checked": time.time()}

    def check_updates(self, manual: bool = False, install: bool | None = None) -> str:
        try:
            rel, status = update.check()
            self._set_update_meta(rel.build if rel else BUILD)
        except Exception as e:
            status = f"update check failed: {e}"
            if manual:
                self.monitor.log("warn", status)
                self._notify(status)
            return status
        if rel is None:
            if manual:
                self.monitor.log("info", status)
                self._notify(status)
            return status
        want = load_settings()["auto_update"] if install is None else install
        if not (want and update.can_self_update()):
            self.monitor.log("sys", status + "  (type `update` to install)")
            self._notify(f"redline build {rel.build} is available")
            return status
        if not manual and (self.visible or self.minimized):
            # Restarting would close the console under the user: wait until they hide it.
            self._pending_update = rel
            self._set_update_meta(rel.build, "pending")
            self.monitor.log("sys", f"build {rel.build} will install when you close this window (or click the version now)")
            return f"build {rel.build} is waiting for the window to be hidden"
        return self._install(rel, manual)

    def _install(self, rel, manual: bool) -> str:
        self._pending_update = None
        self._set_update_meta(rel.build, "installing")
        log.info("update: build %s -> %s (manual=%s)", BUILD, rel.build, manual)
        self.monitor.log("sys", f"installing build {rel.build}…")
        if manual:
            self._notify(f"Updating redline to build {rel.build}…")
        window = {"show": self.visible}
        try:
            if self.visible and self.window is not None:
                window.update(x=int(self.window.x), y=int(self.window.y))
        except Exception:
            pass
        try:
            update.install(rel, extra_args=["--show"] if self.visible else [], window=window)
        except Exception as e:
            self._set_update_meta(rel.build, "failed")
            log.exception("update failed")
            msg = f"update failed: {e}"
            self.monitor.log("error", msg)
            self._notify(msg)
            return msg
        threading.Timer(0.5, self.quit).start()  # the new exe is already starting
        return f"installing build {rel.build}, restarting…"

    def _update_action(self, body: dict) -> str:
        return self.check_updates(manual=True, install=True)

    def _open_log(self) -> str:
        from .applog import log_path

        p = log_path()
        try:
            if sys.platform == "win32":
                os.startfile(str(p))  # opens in Notepad (or the .log handler)
            else:
                webbrowser.open(p.as_uri())
        except OSError as e:
            return f"log: {p} ({e})"
        return f"log: {p}"

    def _toggle_auto_update(self, icon=None, item=None):
        save_setting("auto_update", not load_settings()["auto_update"])

    def quit(self, icon=None, item=None):
        from . import instance

        log.info("quit")
        self.quitting = True
        # Whatever happens below, make sure this process really ends (it holds the
        # single-instance lock the next/updated redline is waiting for).
        killer = threading.Timer(5, os._exit, args=(0,))
        killer.daemon = True
        killer.start()
        instance.clear_state()
        try:
            self.server.shutdown()  # first: a new instance must not find us answering
        except Exception:
            pass
        self.monitor.stop()
        for step in (self.icon.stop, lambda: self.window and self.window.destroy()):
            try:
                step()
            except Exception:
                log.exception("quit step failed")

    # ------------------------------------------------------------ run

    def _size(self) -> tuple[int, int]:
        saved = load_settings().get("window_size")
        if isinstance(saved, list) and len(saved) == 2:
            return max(MIN_W, int(saved[0])), max(MIN_H, int(saved[1]))
        return WIN_W, WIN_H

    def screen_height(self) -> int:
        try:
            return int(self.webview.screens[0].height)
        except Exception:
            return 0

    def _position(self, w: int, h: int) -> tuple[int | None, int | None]:
        hw = self._handoff_window
        try:
            s = self.webview.screens[0]
            if isinstance(hw.get("x"), int) and isinstance(hw.get("y"), int):
                # restarted by an update: reopen where it was (kept on screen)
                return min(max(0, hw["x"]), max(0, s.width - w)), min(max(0, hw["y"]), max(0, s.height - h))
            return max(0, s.width - w - 12), max(0, s.height - h - 60)
        except Exception:
            return None, None

    def run(self, updated_from: int | None = None, show: bool = False, handoff_window: dict | None = None) -> int:
        from . import instance

        self._handoff_window = handoff_window or {}

        instance.write_state(self.server.port, self.server.token)
        threading.Thread(target=update.cleanup_old, daemon=True).start()
        if autostart.is_enabled():
            try:
                autostart.set_enabled(True)  # refresh the command (adds --background)
            except OSError:
                pass
        if updated_from is not None:
            self.monitor.log("ok", f"updated: build {updated_from} -> {BUILD}")
            if show:  # a background update stays silent; the version tag and the log say it
                self._notify(f"redline updated to build {BUILD}")
        if update.is_packaged():
            threading.Thread(target=self._update_loop, name="redline-update", daemon=True).start()
        self._show_when_ready = show
        self.monitor.meta["pid"] = os.getpid()
        self.monitor.meta["window_visible"] = False
        if not load_accounts():  # first launch: pick up whatever is already on this machine
            try:
                actions.scan()
            except Exception:
                pass
        self.monitor.start()
        if self.webview is None:
            self.icon.run()
            return 0
        width, height = self._size()
        x, y = self._position(width, height)
        self.window = self.webview.create_window(
            "redline",
            self.server.url,
            js_api=JsApi(self),
            width=width,
            height=height,
            x=x,
            y=y,
            min_size=(MIN_W, MIN_H),
            hidden=True,
            frameless=True,
            easy_drag=False,
            background_color="#030507",
        )
        self.window.events.closing += self._on_closing
        threading.Thread(target=self.icon.run, name="redline", daemon=True).start()
        storage = redline_home() / "webview"
        if self.quitting:  # e.g. an update was installed while we were still starting up
            log.info("quit requested before the GUI started")
            return 0
        log.info("gui starting (show=%s)", self._show_when_ready)
        self.webview.start(self._gui_started, private_mode=False, storage_path=str(storage))
        log.info("gui stopped")
        return 0

    def _gui_started(self):
        """Runs in its own thread once the GUI loop is up (window exists, can be shown)."""
        log.info("gui started")
        if getattr(self, "_show_when_ready", False):
            self._show_when_ready = False
            self.show()


def self_test(monitor: Monitor, server: ConsoleServer) -> int:
    """Smoke test for packaged builds: deps import, icon renders, server + auth work."""
    out = os.environ.get("REDLINE_SELFTEST_OUT")
    results: dict[str, object] = {}
    try:
        import pystray  # noqa: F401

        results["pystray"] = True
        try:
            import webview  # noqa: F401

            results["webview"] = True
        except ImportError:
            results["webview"] = False
        from .icon import render_icon

        results["icon"] = render_icon(42.0).size == (64, 64)
        with urllib.request.urlopen(server.url, timeout=5) as r:
            html = r.read().decode()
        results["page"] = server.token in html and "REDLINE" in html
        try:
            urllib.request.urlopen(server.url + "api/usage", timeout=5)
            results["auth"] = False
        except urllib.error.HTTPError as e:
            results["auth"] = e.code == 401
        req = urllib.request.Request(server.url + "api/usage", headers={TOKEN_HEADER: server.token})
        with urllib.request.urlopen(req, timeout=5) as r:
            results["api"] = "usages" in json.loads(r.read())
        results["ok"] = all(v is True for k, v in results.items() if k != "webview")
        results["build"] = BUILD
    except Exception as e:  # report instead of crashing (no console in --noconsole builds)
        results["ok"] = False
        results["error"] = f"{type(e).__name__}: {e}"
    text = json.dumps(results)
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
    elif sys.stdout:
        print(text)
    server.shutdown()
    return 0 if results.get("ok") else 1


def restart_test(out: str, reset_env: bool = True) -> int:
    """CI check for self-update restarts: start a copy of ourselves the same way the
    updater does, then exit at once so our unpack dir gets deleted underneath it."""
    with open(out + ".parent", "w", encoding="utf-8") as f:
        f.write(getattr(sys, "_MEIPASS", ""))
    update.launch_detached(sys.executable, ["--self-test-child", out], reset_env=reset_env)
    return 0


def restart_test_child(out: str) -> int:
    time.sleep(5)  # the parent has exited and cleaned up by now
    import webview  # noqa: F401  - modules the real app needs, loaded only now
    from PIL import Image  # noqa: F401

    from .icon import render_icon

    render_icon(50.0)
    try:
        parent = open(out + ".parent", encoding="utf-8").read()
    except OSError:
        parent = ""
    mine = getattr(sys, "_MEIPASS", "")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"ok": True, "independent": bool(mine) and mine != parent, "meipass": mine, "parent": parent}, f)
    return 0


def run_tray(interval: int | None = None, refresh_tokens: bool | None = None, self_test_mode: bool = False,
             updated_from: int | None = None, show: bool = False) -> int:
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        print("the tray app needs extra packages:  pip install 'redline[tray]'", file=sys.stderr)
        return 1
    try:
        import webview
    except ImportError:
        webview = None

    from . import applog

    applog.setup()
    handoff = update.take_handoff(full=True) or {}
    if updated_from is None and handoff:
        updated_from = int(handoff.get("from", 0))  # started by a self-update that couldn't pass its arguments
    window = handoff.get("window") if isinstance(handoff.get("window"), dict) else {}
    if window.get("show"):
        show = True  # the console was open before the update: bring it back where it was
    log.info("start build=%s exe=%s args=%s updated_from=%s show=%s",
             BUILD, sys.executable, sys.argv[1:], updated_from, show)

    settings = load_settings()
    monitor = Monitor(
        interval=interval or settings["interval"],
        refresh_tokens=settings["auto_refresh"] if refresh_tokens is None else refresh_tokens,
    )
    server = ConsoleServer(monitor, "127.0.0.1", 0, allow_actions=True,
                           extra_boot={"tray": True, "manual_size": bool(settings.get("window_size"))}).start_background()
    if self_test_mode:
        return self_test(monitor, server)

    lock = _acquire_instance(updated_from is not None)
    if lock is None:
        log.info("another instance owns redline; exiting")
        server.shutdown()
        return 0
    log.info("instance lock acquired")
    return TrayApp(monitor, server, webview).run(updated_from=updated_from, show=show, handoff_window=window)


def _wait_for_lock(seconds: float):
    lock = _single_instance()
    deadline = time.time() + seconds
    while lock is None and time.time() < deadline:
        time.sleep(0.5)
        lock = _single_instance()
    return lock


def _acquire_instance(after_update: bool):
    """Become the one tray instance, or hand over to the one that's already running."""
    from . import instance

    # Right after a self-update the old process may still be exiting: wait for it.
    lock = _wait_for_lock(20 if after_update else 0)
    if lock is not None:
        return lock
    if not after_update and instance.ask_running_to_show():
        log.info("handed over to the running instance")
        return None  # the live instance just opened its window
    stuck = instance.stuck_pids()
    log.warning("lock busy; stuck redline processes: %s (after_update=%s)", stuck, after_update)
    if not after_update and not instance.ask_yes_no(
        "redline is already running but is not responding\n"
        "(probably left over from an earlier update).\n\nRestart it?"
    ):
        return None
    instance.kill(stuck)
    lock = _wait_for_lock(10)
    if lock is None:
        _message_box("Could not stop the old redline process.\nEnd redline.exe in Task Manager and try again.")
    return lock
