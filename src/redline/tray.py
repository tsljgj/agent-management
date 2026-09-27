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
import urllib.error
import urllib.request
import webbrowser

from . import actions, autostart
from .config import load_accounts, load_settings, redline_home, save_setting
from .models import Usage
from .monitor import Monitor
from .web import TOKEN_HEADER, ConsoleServer

WIN_W, WIN_H = 520, 760
TOOLTIP_MAX = 120  # Windows caps tray tooltips at 128 chars


def _single_instance() -> object | None:
    """Return a handle that must stay alive, or None if another instance is running."""
    if sys.platform == "win32":
        import ctypes

        handle = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\redline")
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            return None
        return handle
    import fcntl

    path = redline_home() / "tray.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
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
        self.quitting = False
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
                Item("Quit", self.quit),
            ),
        )
        monitor.listeners.append(self.on_update)

    # ------------------------------------------------------------ menu

    def _account_items(self, make_handler):
        Item = self.pystray.MenuItem
        by_name = {u.account: u for u in self.monitor.usages}
        items = []
        for a in load_accounts():
            u = by_name.get(a.name)
            w = next((w for w in u.windows if w.name == "5h"), None) if u and (u.ok or u.stale) else None
            label = f"{a.name}  ({a.provider}{f', 5h {w.used_percent:.0f}%' if w and w.used_percent is not None else ''})"
            items.append(Item(label, make_handler(a.name)))
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
            webbrowser.open(self.server.url)
            return
        self.window.show()
        self.window.restore()
        self.visible = True

    def hide(self):
        if self.window is not None:
            self.window.hide()
        self.visible = False

    def _on_closing(self):
        if self.quitting:
            return True
        self.hide()
        return False  # cancel the close; keep living in the tray

    def quit(self, icon=None, item=None):
        self.quitting = True
        self.monitor.stop()
        self.icon.stop()
        if self.window is not None:
            self.window.destroy()
        self.server.shutdown()

    # ------------------------------------------------------------ run

    def _position(self) -> tuple[int | None, int | None]:
        try:
            s = self.webview.screens[0]
            return max(0, s.width - WIN_W - 12), max(0, s.height - WIN_H - 60)
        except Exception:
            return None, None

    def run(self) -> int:
        if not load_accounts():  # first launch: pick up whatever is already on this machine
            try:
                actions.scan()
            except Exception:
                pass
        self.monitor.start()
        if self.webview is None:
            self.icon.run()
            return 0
        x, y = self._position()
        self.window = self.webview.create_window(
            "redline",
            self.server.url,
            js_api=JsApi(self),
            width=WIN_W,
            height=WIN_H,
            x=x,
            y=y,
            min_size=(380, 480),
            hidden=True,
            frameless=True,
            easy_drag=False,
            background_color="#030507",
        )
        self.window.events.closing += self._on_closing
        threading.Thread(target=self.icon.run, name="redline", daemon=True).start()
        storage = redline_home() / "webview"
        self.webview.start(private_mode=False, storage_path=str(storage))
        return 0


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


def run_tray(interval: int | None = None, refresh_tokens: bool | None = None, self_test_mode: bool = False) -> int:
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

    settings = load_settings()
    monitor = Monitor(
        interval=interval or settings["interval"],
        refresh_tokens=settings["auto_refresh"] if refresh_tokens is None else refresh_tokens,
    )
    server = ConsoleServer(monitor, "127.0.0.1", 0, allow_actions=True, extra_boot={"tray": True}).start_background()
    if self_test_mode:
        return self_test(monitor, server)

    lock = _single_instance()
    if lock is None:
        _message_box("redline is already running in the system tray.")
        return 0
    return TrayApp(monitor, server, webview).run()
