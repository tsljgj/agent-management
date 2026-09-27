"""OpenAI Codex CLI (ChatGPT Plus / Pro / Team login).

Credentials: $CODEX_HOME/auth.json
Usage:       GET https://chatgpt.com/backend-api/wham/usage
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

from ..config import Account
from ..http import HTTPStatusError, request_json
from ..models import ProviderError, Usage, Window
from ._util import from_epoch, read_json, write_json_atomic

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
USER_AGENT = "redline"
AUTH_CLAIM = "https://api.openai.com/auth"
PROFILE_CLAIM = "https://api.openai.com/profile"


def _auth_path(account: Account):
    return account.home_path / "auth.json"


def load_auth(account: Account) -> dict | None:
    doc = read_json(_auth_path(account))
    if doc and (doc.get("tokens") or {}).get("access_token"):
        return doc
    return None


def has_credentials(account: Account) -> bool:
    return load_auth(account) is not None


def jwt_claims(token: str | None) -> dict:
    """Decode a JWT payload without verifying it (display purposes only)."""
    if not token or token.count(".") < 2:
        return {}
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return {}


def _window_label(seconds: int | None, fallback: str) -> str:
    if not seconds:
        return fallback
    hours = seconds / 3600
    if hours < 24:
        return f"{hours:g}h"
    return f"{hours / 24:g}d"


def _parse_rate_limit(rl: dict | None, prefix: str = "") -> list[Window]:
    out: list[Window] = []
    if not isinstance(rl, dict):
        return out
    now = datetime.now(timezone.utc).timestamp()
    for key, fallback in (("primary_window", "primary"), ("secondary_window", "secondary")):
        w = rl.get(key)
        if not isinstance(w, dict) or w.get("used_percent") is None:
            continue
        reset = w.get("reset_at")
        if reset is None and w.get("reset_after_seconds") is not None:
            reset = now + w["reset_after_seconds"]
        label = (prefix + _window_label(w.get("limit_window_seconds"), fallback)).strip()
        out.append(Window(label, float(w["used_percent"]), from_epoch(reset)))
    return out


def parse_usage(doc: dict) -> tuple[list[Window], dict]:
    windows = _parse_rate_limit(doc.get("rate_limit"))
    for extra_rl in doc.get("additional_rate_limits") or []:
        if isinstance(extra_rl, dict):
            name = extra_rl.get("limit_name") or extra_rl.get("metered_feature") or "extra"
            windows += _parse_rate_limit(extra_rl.get("rate_limit"), prefix=f"{name} ")
    extra: dict = {}
    credits = doc.get("credits")
    if isinstance(credits, dict) and credits.get("has_credits"):
        extra["credits"] = "unlimited" if credits.get("unlimited") else str(credits.get("balance"))
    rl = doc.get("rate_limit") or {}
    if rl.get("limit_reached"):
        extra["status"] = "LIMIT REACHED"
    return windows, extra


def refresh(account: Account, auth: dict) -> dict:
    """Refresh tokens and write them back to auth.json.

    OpenAI refresh tokens are single-use, so the new ones must be persisted or the
    Codex CLI will fail with `refresh_token_reused`.
    """
    rt = (auth.get("tokens") or {}).get("refresh_token")
    if not rt:
        raise ProviderError("token rejected and no refresh token stored; run `codex login` again")
    resp = request_json(
        "POST", TOKEN_URL,
        json_body={"client_id": CLIENT_ID, "grant_type": "refresh_token", "refresh_token": rt},
    )
    latest = read_json(_auth_path(account)) or auth
    tokens = dict(latest.get("tokens") or {})
    for k in ("id_token", "access_token", "refresh_token"):
        if resp.get(k):
            tokens[k] = resp[k]
    latest["tokens"] = tokens
    latest["last_refresh"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    write_json_atomic(_auth_path(account), latest)
    return latest


def identity(account: Account) -> str | None:
    auth = load_auth(account)
    if not auth:
        return None
    c = jwt_claims(auth["tokens"].get("id_token"))
    return c.get("email") or (c.get(PROFILE_CLAIM) or {}).get("email")


def token_state(account: Account) -> str:
    auth = load_auth(account)
    if auth is None:
        return "missing"
    exp = jwt_claims(auth["tokens"]["access_token"]).get("exp")
    if exp and exp < datetime.now(timezone.utc).timestamp() + 60:
        return "expired" if auth["tokens"].get("refresh_token") else "missing"
    return "ok"


def refresh_account(account: Account) -> None:
    auth = load_auth(account)
    if auth is None:
        raise ProviderError("not logged in")
    refresh(account, auth)


def _headers(auth: dict) -> dict[str, str]:
    tokens = auth["tokens"]
    h = {"Authorization": f"Bearer {tokens['access_token']}", "User-Agent": USER_AGENT}
    account_id = tokens.get("account_id") or jwt_claims(tokens.get("id_token")).get(AUTH_CLAIM, {}).get(
        "chatgpt_account_id"
    )
    if account_id:
        h["ChatGPT-Account-Id"] = account_id
    if jwt_claims(tokens.get("id_token")).get(AUTH_CLAIM, {}).get("chatgpt_account_is_fedramp"):
        h["X-OpenAI-Fedramp"] = "true"
    return h


def fetch_usage(account: Account, refresh_tokens: bool = False) -> Usage:
    auth = load_auth(account)
    if auth is None:
        doc = read_json(_auth_path(account))
        if doc and doc.get("OPENAI_API_KEY"):
            raise ProviderError("logged in with an API key; usage limits only exist for ChatGPT logins")
        raise ProviderError(
            f"not logged in (no ChatGPT tokens in {_auth_path(account)}; if you use "
            f"cli_auth_credentials_store=keyring this tool can't read them); run `redline login {account.name}`"
        )

    exp = jwt_claims(auth["tokens"]["access_token"]).get("exp")
    if exp and exp < datetime.now(timezone.utc).timestamp() + 60 and refresh_tokens:
        auth = refresh(account, auth)

    try:
        doc = request_json("GET", USAGE_URL, headers=_headers(auth))
    except HTTPStatusError as e:
        if e.status == 401 and refresh_tokens:
            auth = refresh(account, auth)
            doc = request_json("GET", USAGE_URL, headers=_headers(auth))
        elif e.status == 401:
            raise ProviderError("unauthorized (token expired); run `codex` in this account or pass --refresh-tokens") from None
        else:
            raise

    windows, extra = parse_usage(doc)
    id_claims = jwt_claims(auth["tokens"].get("id_token"))
    email = id_claims.get("email") or (id_claims.get(PROFILE_CLAIM) or {}).get("email")
    plan = doc.get("plan_type") or (id_claims.get(AUTH_CLAIM) or {}).get("chatgpt_plan_type")
    return Usage(
        account=account.name, provider="codex", ok=True,
        email=email, plan=plan, windows=windows, extra=extra,
    )
