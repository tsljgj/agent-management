"""Claude Code (Pro / Max / Team subscription via OAuth).

Credentials: $CLAUDE_CONFIG_DIR/.credentials.json (Linux/Windows) or the macOS
Keychain item "Claude Code-credentials[-<sha256(dir)[:8]>]".
Usage:       GET https://api.anthropic.com/api/oauth/usage
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from ..config import Account
from ..http import HTTPStatusError, request_json
from ..models import ProviderError, Usage, Window
from ._util import cli_lock, parse_iso, read_json, write_json_atomic

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_BETA = "oauth-2025-04-20"
USER_AGENT = "claude-code/2.1.0"

# response key -> display name, in display order
WINDOW_KEYS = [
    ("five_hour", "5h"),
    ("seven_day", "7d"),
    ("seven_day_opus", "7d opus"),
    ("seven_day_sonnet", "7d sonnet"),
]


@dataclass
class Creds:
    data: dict  # whole credentials document (other keys like mcpOAuth must be preserved)
    source: str  # "file" or "keychain"
    path: Path | None = None

    @property
    def oauth(self) -> dict:
        return self.data.get("claudeAiOauth") or {}

    @property
    def expired(self) -> bool:
        exp = self.oauth.get("expiresAt")
        return bool(exp) and exp / 1000 < time.time() + 60


def keychain_service(account: Account) -> str:
    if account.is_default_home:
        return "Claude Code-credentials"
    # Claude Code hashes the literal CLAUDE_CONFIG_DIR value; redline always exports
    # the expanded absolute path, so hash exactly that.
    raw = unicodedata.normalize("NFC", str(account.home_path))
    return "Claude Code-credentials-" + hashlib.sha256(raw.encode()).hexdigest()[:8]


def _read_keychain(account: Account) -> dict | None:
    if sys.platform != "darwin":
        return None
    user = os.environ.get("USER", "")
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-a", user, "-w", "-s", keychain_service(account)],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        return json.loads(out.stdout.strip())
    except json.JSONDecodeError:
        return None


def load_creds(account: Account) -> Creds | None:
    path = account.home_path / ".credentials.json"
    data = read_json(path)
    if data and data.get("claudeAiOauth"):
        return Creds(data=data, source="file", path=path)
    data = _read_keychain(account)
    if data and data.get("claudeAiOauth"):
        return Creds(data=data, source="keychain")
    return None


def has_credentials(account: Account) -> bool:
    c = load_creds(account)
    return bool(c and c.oauth.get("accessToken"))


def _cached_identity(account: Account) -> tuple[str | None, str | None]:
    """Email / org type cached by Claude Code in its .claude.json (no network needed)."""
    if account.is_default_home and "CLAUDE_CONFIG_DIR" not in os.environ:
        candidates = [Path("~/.claude.json").expanduser(), account.home_path / ".config.json"]
    else:
        candidates = [account.home_path / ".claude.json", account.home_path / ".config.json"]
    for p in candidates:
        try:
            doc = read_json(p)
        except (ValueError, OSError):  # JSONDecodeError / UnicodeDecodeError are ValueErrors
            continue
        acct = (doc or {}).get("oauthAccount") or {}
        if acct.get("emailAddress"):
            return acct.get("emailAddress"), acct.get("organizationName")
    return None, None


def identity(account: Account) -> str | None:
    return _cached_identity(account)[0]


def token_state(account: Account) -> str:
    """"ok", "expired" (refreshable) or "missing" (needs an interactive login)."""
    c = load_creds(account)
    if c is None or not c.oauth.get("accessToken"):
        return "missing"
    if c.expired:
        return "expired" if c.oauth.get("refreshToken") and c.source == "file" else "missing"
    return "ok"


def refresh_account(account: Account) -> None:
    c = load_creds(account)
    if c is None:
        raise ProviderError("not logged in")
    refresh(c)


def refresh(creds: Creds, force: bool = False) -> None:
    """Refresh the access token and persist the rotated tokens back to the file.

    Claude rotates refresh tokens, so writing back is mandatory, otherwise the CLI's
    copy becomes invalid. Keychain-stored credentials are left for the CLI to refresh.
    """
    if creds.source != "file" or creds.path is None:
        raise ProviderError(
            "access token expired (stored in Keychain); run `claude` in this account once to refresh it"
        )
    with cli_lock(creds.path.parent):
        # Another process (usually the CLI itself) may have refreshed while we waited.
        latest = read_json(creds.path)
        if latest is None:  # moved away meanwhile (default-login switch): never write a second copy
            raise ProviderError("login moved while refreshing; retry")
        fresh = Creds(latest, "file", creds.path)
        rotated = fresh.oauth.get("accessToken") != creds.oauth.get("accessToken")
        if fresh.oauth and (rotated or (not force and not fresh.expired)):
            creds.data = latest
            return
        _refresh_locked(creds)


def _refresh_locked(creds: Creds) -> None:
    rt = creds.oauth.get("refreshToken")
    if not rt:
        raise ProviderError("access token expired and no refresh token is stored; log in again")
    body = {"grant_type": "refresh_token", "refresh_token": rt, "client_id": CLIENT_ID}
    scopes = creds.oauth.get("scopes")
    if scopes:
        body["scope"] = " ".join(scopes)
    resp = request_json("POST", TOKEN_URL, headers={"User-Agent": USER_AGENT}, json_body=body)
    if not resp.get("access_token"):
        raise ProviderError(f"token refresh returned no access_token: {list(resp)}")
    # Re-read right before writing so we don't clobber a concurrent CLI write of other keys.
    latest = read_json(creds.path) or creds.data
    oauth = dict(latest.get("claudeAiOauth") or {})
    oauth["accessToken"] = resp["access_token"]
    if resp.get("refresh_token"):
        oauth["refreshToken"] = resp["refresh_token"]
    if resp.get("expires_in"):
        oauth["expiresAt"] = int((time.time() + int(resp["expires_in"])) * 1000)
    if resp.get("scope"):
        oauth["scopes"] = resp["scope"].split()
    latest["claudeAiOauth"] = oauth
    write_json_atomic(creds.path, latest)
    creds.data = latest


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": OAUTH_BETA,
        "User-Agent": USER_AGENT,
    }


def parse_usage(doc: dict) -> tuple[list[Window], dict]:
    windows: list[Window] = []
    for key, label in WINDOW_KEYS:
        w = doc.get(key)
        if isinstance(w, dict) and w.get("utilization") is not None:
            windows.append(Window(label, float(w["utilization"]), parse_iso(w.get("resets_at"))))
    # Newer model-scoped weekly limits.
    for lim in doc.get("limits") or []:
        if not isinstance(lim, dict) or lim.get("percent") is None:
            continue
        model = ((lim.get("scope") or {}).get("model") or {})
        name = model.get("display_name") or model.get("id") or lim.get("kind") or "limit"
        label = f"{lim.get('group') or ''} {name}".strip()
        if any(w.name == label for w in windows):
            continue
        windows.append(Window(label, float(lim["percent"]), parse_iso(lim.get("resets_at"))))
    extra: dict = {}
    eu = doc.get("extra_usage")
    if isinstance(eu, dict) and eu.get("is_enabled"):
        used, limit = eu.get("used_credits"), eu.get("monthly_limit")
        cur = eu.get("currency") or "USD"
        if used is not None and limit:
            # Values are reported in cents.
            extra["extra usage"] = f"{used / 100:.2f} / {limit / 100:.2f} {cur}"
        elif eu.get("utilization") is not None:
            extra["extra usage"] = f"{eu['utilization']:.0f}%"
    return windows, extra


def fetch_usage(account: Account, refresh_tokens: bool = False) -> Usage:
    creds = load_creds(account)
    if creds is None:
        raise ProviderError(f"not logged in (no credentials in {account.home_path}); run `redline login {account.name}`")
    oauth = creds.oauth
    if creds.expired:
        if not refresh_tokens:
            raise ProviderError(
                "access token expired; run `claude` in this account once, or pass --refresh-tokens"
            )
        refresh(creds)
        oauth = creds.oauth
    token = oauth.get("accessToken")
    if not token:
        raise ProviderError("credentials have no access token; log in again")

    try:
        doc = request_json("GET", USAGE_URL, headers=_headers(token))
    except HTTPStatusError as e:
        if e.status == 401 and refresh_tokens and creds.source == "file":
            refresh(creds, force=True)
            doc = request_json("GET", USAGE_URL, headers=_headers(creds.oauth["accessToken"]))
        elif e.status == 401:
            raise ProviderError("unauthorized (token expired or revoked); run `claude` in this account or pass --refresh-tokens") from None
        elif e.status == 403:
            raise ProviderError("forbidden: token lacks the user:profile scope (re-run `claude /login`)") from None
        elif e.status == 429:
            raise ProviderError("rate limited by the usage endpoint; poll less often") from None
        else:
            raise

    windows, extra = parse_usage(doc)
    email, org = _cached_identity(account)
    plan = oauth.get("subscriptionType")
    tier = oauth.get("rateLimitTier") or ""
    if plan == "max" and "20x" in tier:
        plan = "max 20x"
    elif plan == "max" and "5x" in tier:
        plan = "max 5x"
    if email is None:
        try:
            prof = request_json("GET", PROFILE_URL, headers=_headers(creds.oauth["accessToken"]))
            a = prof.get("account") or {}
            email = a.get("email_address") or a.get("email")
        except ProviderError:
            pass
    return Usage(
        account=account.name, provider="claude", ok=True,
        email=email, plan=plan, windows=windows, extra=extra,
    )
