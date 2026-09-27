"""ChatGPT (Codex) sign-in without the Codex CLI.

Same OAuth flow as `codex login` (codex-rs/login/src/server.rs): PKCE, a local
callback server on 127.0.0.1:1455 (fallback 1457, the registered ports), a
form-encoded code exchange at auth.openai.com, and an auth.json in CODEX_HOME in
the CLI's format. Lets redline log Codex accounts in even when `codex` isn't on
PATH (e.g. Codex only installed as an IDE extension or app).
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable

from .models import ProviderError
from .providers._util import read_json, write_json_atomic
from .providers.codex import AUTH_CLAIM, CLIENT_ID, ORIGINATOR, jwt_claims

ISSUER = "https://auth.openai.com"
PORTS = (1455, 1457)
SCOPE = "openid profile email offline_access api.connectors.read api.connectors.invoke"

PAGE = """<!doctype html><meta charset="utf-8"><title>redline</title>
<body style="background:#07090b;color:#d6eef2;font:15px/1.6 monospace;display:grid;place-items:center;height:90vh">
<div><b style="color:#00e5ff">REDLINE//</b><br>{msg}<br><span style="color:#50646a">You can close this tab.</span></div>"""


def pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(redirect_uri: str, challenge: str, state: str) -> str:
    q = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": ORIGINATOR,
    }
    return f"{ISSUER}/oauth/authorize?{urllib.parse.urlencode(q, quote_via=urllib.parse.quote)}"


def exchange_code(code: str, redirect_uri: str, verifier: str) -> dict:
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }).encode()
    req = urllib.request.Request(f"{ISSUER}/oauth/token", data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded",
                                          "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise ProviderError(f"token exchange failed: HTTP {e.code} {e.read()[:200]!r}") from None
    except urllib.error.URLError as e:
        raise ProviderError(f"token exchange failed: {e.reason}") from None


def save_auth(codex_home: Path, tokens: dict) -> str | None:
    """Write auth.json the way `codex login` does; returns the account email."""
    id_token = tokens["id_token"]
    claims = jwt_claims(id_token)
    doc = read_json(codex_home / "auth.json") or {}
    doc.update({
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": doc.get("OPENAI_API_KEY"),
        "tokens": {
            "id_token": id_token,
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "account_id": (claims.get(AUTH_CLAIM) or {}).get("chatgpt_account_id"),
        },
        "last_refresh": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    })
    codex_home.mkdir(parents=True, exist_ok=True)
    write_json_atomic(codex_home / "auth.json", doc)
    return claims.get("email") or (claims.get("https://api.openai.com/profile") or {}).get("email")


def login(codex_home: Path, open_url: Callable[[str], None], cancelled: threading.Event | None = None,
          timeout: float = 300) -> str | None:
    """Run the browser sign-in; blocks until done. Returns the signed-in email."""
    server = None
    for port in PORTS:
        try:
            server = HTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler)
            break
        except OSError:
            continue
    if server is None:
        raise ProviderError("ports 1455/1457 are busy (is another Codex login open?)")
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}/auth/callback"
    verifier, challenge = pkce()
    state = secrets.token_urlsafe(32)
    result: dict = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _page(self, code: int, msg: str):
            body = PAGE.format(msg=msg).encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            if u.path != "/auth/callback":
                return self._page(404, "not found")
            q = urllib.parse.parse_qs(u.query)
            if (q.get("state") or [""])[0] != state:
                return self._page(400, "State mismatch - please start the login again.")
            if q.get("error"):
                result["error"] = f"{q['error'][0]}: {(q.get('error_description') or [''])[0]}"
                done.set()
                return self._page(400, "Sign-in was not completed: " + result["error"])
            code = (q.get("code") or [""])[0]
            if not code:
                return self._page(400, "Missing authorization code.")
            try:
                result["email"] = save_auth(codex_home, exchange_code(code, redirect_uri, verifier))
                self._page(200, f"Signed in{' as ' + result['email'] if result.get('email') else ''}.")
            except (ProviderError, KeyError, OSError) as e:
                result["error"] = str(e)
                self._page(500, "Sign-in failed: " + str(e))
            done.set()

    server.RequestHandlerClass = Handler
    server.timeout = 1
    try:
        open_url(authorize_url(redirect_uri, challenge, state))
        waited = 0.0
        while not done.is_set():
            if cancelled is not None and cancelled.is_set():
                raise ProviderError("cancelled")
            if waited >= timeout:
                raise ProviderError(f"no sign-in within {int(timeout // 60)} min")
            server.handle_request()  # returns after a request or the 1s timeout
            waited += 1
    finally:
        server.server_close()
    if "error" in result:
        raise ProviderError(result["error"])
    return result.get("email")
