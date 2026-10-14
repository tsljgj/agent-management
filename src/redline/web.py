"""Local HTTP server behind the console (used by both `redline serve` and the tray app).

Security: the server binds to 127.0.0.1 by default, rejects foreign Host headers
(DNS rebinding) and requires a per-process token header on every API call (CSRF),
because /api/action can open terminals on this machine.
"""

from __future__ import annotations

import json
import secrets
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlsplit

from . import __version__, actions
from ._build import BUILD
from .config import find_account, load_accounts
from .monitor import Monitor

LOOPBACK = {"127.0.0.1", "localhost", "::1"}
TOKEN_HEADER = "X-Redline-Token"
MAX_BODY = 64 * 1024


def console_html() -> str:
    return resources.files("redline").joinpath("assets/console.html").read_text(encoding="utf-8")


def _rename_target(body: dict) -> tuple[str, str] | None:
    """(provider, plain name) of the account a rename request refers to, resolved the way the
    rename itself resolves it ("codex:work" works from either tab), or None if it matches none."""
    try:
        acct = find_account(load_accounts(), str(body.get("name", "")), body.get("provider") or None)
    except (KeyError, OSError, ValueError):
        return None
    return acct.provider, acct.name


class _HTTPServer(ThreadingHTTPServer):
    if sys.platform == "win32":
        # Windows lets SO_REUSEADDR bind a port another socket is still listening on. Claim the
        # port exclusively instead, so a busy port fails the bind (the tray then picks another).
        allow_reuse_address = False

        def server_bind(self):
            self.socket.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", -5), 1)
            super().server_bind()


class ConsoleServer:
    def __init__(self, monitor: Monitor, host: str = "127.0.0.1", port: int = 0, allow_actions: bool | None = None,
                 extra_boot: dict | None = None, extra_actions: dict | None = None):
        self.monitor = monitor
        self.token = secrets.token_urlsafe(24)
        self.loopback = host in LOOPBACK
        # Actions spawn processes on *this* machine: only offer them on a loopback bind.
        self.allow_actions = self.loopback if allow_actions is None else allow_actions
        self.extra_boot = extra_boot or {}
        self.extra_actions = extra_actions or {}  # action name -> fn(body) -> message (tray-only features)
        self.httpd = _HTTPServer((host, port), self._handler())
        self.host = host
        self.port = self.httpd.server_address[1]

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.host in ("0.0.0.0", "") else self.host
        return f"http://{host}:{self.port}/"

    def page(self) -> bytes:
        boot = {
            "token": self.token,
            "actions": self.allow_actions,
            "version": __version__,
            "build": BUILD,
            "platform": sys.platform,
            **self.extra_boot,
        }
        html = console_html().replace("/*__BOOT__*/{}", json.dumps(boot))
        return html.encode("utf-8")

    def start_background(self) -> "ConsoleServer":
        threading.Thread(target=self.httpd.serve_forever, name="redline-http", daemon=True).start()
        return self

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()  # free the port now (the next instance wants it back)

    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, code: int, body: bytes, ctype: str):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, obj) -> None:
                self._send(code, json.dumps(obj).encode(), "application/json")

            def _host_ok(self) -> bool:
                if not server.loopback:
                    return True
                host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
                return host in LOOPBACK

            def _authed(self) -> bool:
                return secrets.compare_digest(self.headers.get(TOKEN_HEADER, ""), server.token)

            def do_GET(self):
                if not self._host_ok():
                    return self._send(403, b"bad host", "text/plain")
                parts = urlsplit(self.path)
                if parts.path == "/":
                    return self._send(200, server.page(), "text/html; charset=utf-8")
                if parts.path == "/api/candidates":
                    if not self._authed():
                        return self._json(401, {"error": "missing token"})
                    prov = (parse_qs(parts.query).get("provider") or ["claude"])[0]
                    try:
                        return self._json(200, actions.candidates(prov))
                    except actions.ActionError as e:
                        return self._json(400, {"error": str(e)})
                if parts.path == "/api/usage":
                    if not self._authed():
                        return self._json(401, {"error": "missing token"})
                    q = parse_qs(parts.query)
                    since = int((q.get("since") or ["0"])[0] or 0)
                    from .countdown import status

                    payload = server.monitor.payload(force="1" in q.get("force", []), since=since)
                    payload["meta"]["countdown"] = status()
                    return self._json(200, payload)
                self._send(404, b"not found", "text/plain")

            def do_POST(self):
                if not self._host_ok():
                    return self._send(403, b"bad host", "text/plain")
                if not self._authed():
                    return self._json(401, {"error": "missing token"})
                if urlsplit(self.path).path != "/api/action":
                    return self._send(404, b"not found", "text/plain")
                if not server.allow_actions:
                    return self._json(403, {"ok": False, "message": "actions are disabled on non-loopback servers"})
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    return self._json(413, {"ok": False, "message": "body too large"})
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                    if not isinstance(body, dict):
                        raise ValueError("body must be an object")
                    for k in ("log", "on_done"):  # never accept callables from the client
                        body.pop(k, None)
                    if body.get("action") in ("wake", "login", "add-profile", "start-countdown"):
                        mon = server.monitor
                        countdown_action = body.get("action") == "start-countdown"
                        body["log"] = lambda level, text: mon.log(
                            level, text, alert=level == "crit" or countdown_action and level in ("ok", "error"))
                        body["on_done"] = lambda: mon.poll()
                    name = str(body.get("action", ""))
                    handler = server.extra_actions.get(name)
                    target = _rename_target(body) if name == "rename" and not handler else None
                    msg = handler(body) if handler else actions.dispatch(name, body)
                    if target is not None:  # the monitor keys state by (provider, plain name)
                        server.monitor.rename(target[1], str(body.get("new", "")).strip(), target[0])
                except actions.ActionError as e:
                    return self._json(200, {"ok": False, "message": str(e)})
                except (json.JSONDecodeError, OSError, ValueError) as e:
                    return self._json(200, {"ok": False, "message": f"{type(e).__name__}: {e}"})
                if body.get("action") in ("add", "add-profile", "remove", "restore", "import", "scan", "bind", "use", "default"):
                    threading.Thread(target=server.monitor.poll, daemon=True).start()
                elif body.get("action") == "renew":  # local only: refresh the details, no network
                    server.monitor.meta["accounts"] = actions.account_details()
                return self._json(200, {"ok": True, "message": msg})

            def log_message(self, *args):
                pass

        return Handler


def serve(host: str, port: int, interval: int = 120, refresh_tokens: bool = False) -> None:
    monitor = Monitor(interval=interval, refresh_tokens=refresh_tokens).start()
    srv = ConsoleServer(monitor, host, port)
    print(f"redline console on {srv.url}  (Ctrl-C to stop)")
    try:
        srv.httpd.serve_forever()
    except KeyboardInterrupt:
        pass
