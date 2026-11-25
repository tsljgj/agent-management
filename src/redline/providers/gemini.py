"""Gemini CLI (Google login): the Code Assist quota, read-only.

Credentials: <gemini dir>/oauth_creds.json (access_token, refresh_token, expiry_date in ms), written
by the Gemini CLI itself; the signed-in email is in google_accounts.json ("active").
Quota:       POST https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota
             -> {"buckets": [{"modelId", "tokenType", "remainingFraction", "resetTime"}, ...]}
Tier:        POST https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist
             -> {"currentTier": {"id", "name"}, "paidTier"?: {...}, "cloudaicompanionProject"}

These are the endpoints the Gemini CLI's own /stats uses; Google documents neither. The shapes here
follow open-source quota readers (CodexBar, hermes-agent), not a spec, so every field is optional.

Gemini logins are not redline accounts: there is no login, switch or wake for them. redline only
looks at the Gemini dirs it is told about (settings "gemini_homes", default ~/.gemini) and shows
what each one's quota is. The credentials file is never written: an expired access token is renewed
in memory only (with the Gemini CLI's own OAuth client, read from the installed CLI; nothing is
stored in redline), so the CLI's copy is never touched.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import shutil
import threading
import time
from pathlib import Path

from ..http import HTTPStatusError, request_json
from ..models import ProviderError, Usage, Window
from ._util import parse_iso, read_json

ENDPOINT = "https://cloudcode-pa.googleapis.com/v1internal"
QUOTA_URL = ENDPOINT + ":retrieveUserQuota"
LOAD_URL = ENDPOINT + ":loadCodeAssist"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DEFAULT_HOME = "~/.gemini"
TIER_TTL = 30 * 60

# tier id -> plan name shown; any other tier shows its own name (or id)
TIERS = {
    "free-tier": "free",
    "legacy-tier": "legacy",
    "standard-tier": "standard",
    "g1-pro-tier": "ai pro",
    "g1-ultra-tier": "ai ultra",
}


# ---------------------------------------------------------------- where the logins are


def homes() -> list[Path]:
    """The Gemini dirs to look at: settings "gemini_homes" (a list of paths), else ~/.gemini."""
    try:
        from ..config import load_settings

        listed = load_settings().get("gemini_homes")
    except Exception:
        listed = None
    if not isinstance(listed, list) or not listed:
        listed = [DEFAULT_HOME]
    out, seen = [], set()
    for p in listed:
        if not isinstance(p, str) or not p.strip():
            continue
        path = Path(p).expanduser()
        if str(path) not in seen:
            seen.add(str(path))
            out.append(path)
    return out


def logged_in_homes() -> list[Path]:
    return [h for h in homes() if (h / "oauth_creds.json").is_file()]


def jwt_claims(token) -> dict:
    if not isinstance(token, str) or token.count(".") < 2:
        return {}
    part = token.split(".")[1]
    try:
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (ValueError, TypeError):
        return {}


def email_of(home: Path, creds: dict | None = None) -> str | None:
    try:
        doc = read_json(home / "google_accounts.json")
    except (OSError, ValueError):
        doc = None
    active = (doc or {}).get("active") if isinstance(doc, dict) else None
    if isinstance(active, str) and active.strip():
        return active.strip()
    email = jwt_claims((creds or {}).get("id_token")).get("email")
    return email if isinstance(email, str) and email else None


def name_for(home: Path, email: str | None, taken: set[str]) -> str:
    # the part before the @ keeps the row short (the full address is in the details)
    base = (email.split("@")[0] or email) if email else (
        "gemini" if home == Path(DEFAULT_HOME).expanduser() else home.parent.name or "gemini")
    name, n = base, 2
    while name in taken:
        name, n = f"{base}-{n}", n + 1
    return name


def identity_key(email: str | None, home: Path) -> str:
    material = json.dumps(["gemini", "email" if email else "home", (email or str(home)).casefold()])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- the access token


_client: dict[str, tuple[str, str] | None] = {}  # "found" -> (client id, secret) or None
_tokens: dict[str, tuple[str, float]] = {}  # hash(refresh token) -> (access token, expires epoch), memory only
_lock = threading.Lock()

_ID_RE = re.compile(rb"OAUTH_CLIENT_ID\s*=\s*['\"]([0-9A-Za-z._-]+\.apps\.googleusercontent\.com)['\"]")
_SECRET_RE = re.compile(rb"OAUTH_CLIENT_SECRET\s*=\s*['\"]([0-9A-Za-z_-]+)['\"]")


def _cli_files() -> list[Path]:
    """Files of the installed Gemini CLI that define its OAuth client (older builds: oauth2.js;
    newer ones bundle everything into bundle/gemini.js)."""
    roots: list[Path] = []
    exe = shutil.which("gemini")
    if exe:
        p = Path(exe).resolve()
        for parent in [p.parent, *p.parents][:6]:
            roots += [parent / "node_modules", parent / "lib" / "node_modules", parent]
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(Path(appdata) / "npm" / "node_modules")
    roots += [Path(p) for p in ("/usr/local/lib/node_modules", "/usr/lib/node_modules", "/opt/homebrew/lib/node_modules")]
    roots.append(Path("~/.npm-global/lib/node_modules").expanduser())
    rel = [
        "@google/gemini-cli/bundle/gemini.js",
        "@google/gemini-cli/node_modules/@google/gemini-cli-core/dist/src/code_assist/oauth2.js",
        "@google/gemini-cli-core/dist/src/code_assist/oauth2.js",
    ]
    out, seen = [], set()
    for r in roots:
        for x in rel:
            f = r / x
            if str(f) not in seen:
                seen.add(str(f))
                out.append(f)
    return out


def oauth_client() -> tuple[str, str] | None:
    """The Gemini CLI's public OAuth client (it ships in the CLI's source), read from the installed
    CLI once; GEMINI_OAUTH_CLIENT_ID / GEMINI_OAUTH_CLIENT_SECRET override it. None: not found."""
    env_id, env_secret = os.environ.get("GEMINI_OAUTH_CLIENT_ID"), os.environ.get("GEMINI_OAUTH_CLIENT_SECRET")
    if env_id and env_secret:
        return env_id, env_secret
    if "found" in _client:
        return _client["found"]
    found = None
    for f in _cli_files():
        try:
            if not f.is_file() or f.stat().st_size > 64 * 1024 * 1024:
                continue
            data = f.read_bytes()
        except OSError:
            continue
        cid, sec = _ID_RE.search(data), _SECRET_RE.search(data)
        if cid and sec:
            found = (cid.group(1).decode(), sec.group(1).decode())
            break
    _client["found"] = found
    return found


EXPIRED = "access token expired; run `gemini` once to renew it"


def access_token(creds: dict) -> str:
    """A live access token for these credentials. Never writes the credentials file."""
    tok, exp = creds.get("access_token"), creds.get("expiry_date")
    exp_s = exp / 1000 if isinstance(exp, (int, float)) and exp > 1e11 else exp
    if isinstance(tok, str) and tok and (not isinstance(exp_s, (int, float)) or exp_s > time.time() + 60):
        return tok
    rt = creds.get("refresh_token")
    if not isinstance(rt, str) or not rt:
        raise ProviderError(EXPIRED)
    key = hashlib.sha256(rt.encode()).hexdigest()
    with _lock:
        hit = _tokens.get(key)
        if hit and hit[1] > time.time() + 60:
            return hit[0]
    client = oauth_client()
    if client is None:
        raise ProviderError(EXPIRED + " (the installed Gemini CLI wasn't found, so redline can't renew it itself)")
    try:
        resp = request_json("POST", TOKEN_URL, form_body={
            "grant_type": "refresh_token", "refresh_token": rt, "client_id": client[0], "client_secret": client[1]})
    except HTTPStatusError as e:
        if e.status in (400, 401) and "invalid_grant" in e.body:
            raise ProviderError("the Gemini login was revoked; run `gemini` and log in again") from None
        raise ProviderError(f"renewing the Gemini token failed (HTTP {e.status})") from None
    new = resp.get("access_token")
    if not isinstance(new, str) or not new:
        raise ProviderError("renewing the Gemini token returned no access token")
    life = resp.get("expires_in") if isinstance(resp.get("expires_in"), (int, float)) else 3000
    with _lock:
        _tokens[key] = (new, time.time() + life)
    return new


# ---------------------------------------------------------------- tier and quota


_tiers: dict[str, tuple[float, dict]] = {}  # identity -> (read at, loadCodeAssist answer)


def _headers(token: str) -> dict[str, str]:
    from .. import __version__

    return {"Authorization": f"Bearer {token}", "User-Agent": f"redline/{__version__}"}


def load_code_assist(key: str, token: str, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    hit = _tiers.get(key)
    if hit and now - hit[0] < TIER_TTL:
        return hit[1]
    meta = {"ideType": "IDE_UNSPECIFIED", "platform": "PLATFORM_UNSPECIFIED", "pluginType": "GEMINI"}
    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT_ID")
    body: dict = {"metadata": meta}
    if project:
        body["cloudaicompanionProject"] = project
        meta["duetProject"] = project
    try:
        doc = request_json("POST", LOAD_URL, headers=_headers(token), json_body=body) or {}
    except ProviderError:
        return hit[1] if hit else {}
    _tiers[key] = (now, doc if isinstance(doc, dict) else {})
    return _tiers[key][1]


def plan_of(doc: dict) -> str | None:
    for k in ("paidTier", "currentTier"):
        t = doc.get(k)
        if isinstance(t, dict) and (t.get("id") or t.get("name")):
            tid = t.get("id")
            return TIERS.get(tid) or (t.get("name") or tid).strip().lower()
    return None


def project_of(doc: dict) -> str | None:
    p = doc.get("cloudaicompanionProject")
    if isinstance(p, dict):
        p = p.get("id") or p.get("name")
    return p if isinstance(p, str) and p else (
        os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT_ID") or None)


def family(model: str) -> str:
    m = model.lower()
    for name in ("pro", "flash-lite", "flash"):
        if f"-{name}" in m or m.startswith(name):
            return name
    return m


def parse_quota(doc) -> tuple[list[Window], dict]:
    """One window per model family (pro, flash, flash-lite...): the bucket with the least left wins.
    Percent used = (1 - remainingFraction) * 100. Per-model numbers go to extra["models"]."""
    buckets = doc.get("buckets") if isinstance(doc, dict) else None
    if not isinstance(buckets, list):
        return [], {}
    worst: dict[str, tuple[float, object]] = {}
    models: dict[str, str] = {}
    for b in buckets:
        if not isinstance(b, dict):
            continue
        frac = b.get("remainingFraction")
        model = b.get("modelId")
        if not isinstance(frac, (int, float)) or isinstance(frac, bool) or not isinstance(model, str) or not model:
            continue
        used = round(max(0.0, min(1.0, 1 - float(frac))) * 100, 4)
        reset = parse_iso(b.get("resetTime")) if isinstance(b.get("resetTime"), str) else None
        fam = family(model)
        label = model + (f" ({b['tokenType'].lower()})" if isinstance(b.get("tokenType"), str)
                         and b["tokenType"] not in ("REQUESTS", "") else "")
        models[label] = f"{math.floor(max(0.0, min(1.0, float(frac))) * 100)}% left"  # rounded down, never overstated
        if fam not in worst or used > worst[fam][0]:
            worst[fam] = (used, reset)
    order = {"pro": 0, "flash": 1, "flash-lite": 2}
    windows = [Window(fam, used, reset) for fam, (used, reset) in sorted(worst.items(), key=lambda kv: (order.get(kv[0], 9), kv[0]))]
    return windows, ({"models": models} if models else {})


def fetch_usage(home: Path, name: str) -> Usage:
    """Never raises."""
    creds = None
    email = None
    try:
        creds = read_json(home / "oauth_creds.json")
        if not isinstance(creds, dict):
            raise ProviderError(f"not logged in (no oauth_creds.json in {home}); run `gemini` and log in with Google")
        email = email_of(home, creds)
        key = identity_key(email, home)
        token = access_token(creds)
        tier = load_code_assist(key, token)
        project = project_of(tier)
        try:
            doc = request_json("POST", QUOTA_URL, headers=_headers(token), json_body={"project": project} if project else {})
        except HTTPStatusError as e:
            if e.status == 401:
                raise ProviderError("unauthorized; run `gemini` once to renew the login") from None
            if e.status == 403:
                raise ProviderError("the quota endpoint refused this login (HTTP 403)") from None
            if e.status == 429:
                raise ProviderError("rate limited by the quota endpoint") from None
            raise ProviderError(f"HTTP {e.status} from the Gemini quota endpoint") from None
        windows, extra = parse_quota(doc)
        if project:
            extra["project"] = project
        return Usage(account=name, provider="gemini", ok=True, email=email, plan=plan_of(tier),
                     windows=windows, extra=extra, identity_key=key)
    except ProviderError as e:
        return Usage(account=name, provider="gemini", ok=False, email=email, error=str(e),
                     identity_key=identity_key(email, home))
    except Exception as e:  # defensive: Gemini must never break the poll
        return Usage(account=name, provider="gemini", ok=False, email=email, error=f"{type(e).__name__}: {e}",
                     identity_key=identity_key(email, home))


def collect(taken: set[str] | None = None) -> list[Usage]:
    """A usage for every Gemini dir with a login."""
    taken = set(taken or ())
    out = []
    for home in logged_in_homes():
        try:
            creds = read_json(home / "oauth_creds.json")
        except (OSError, ValueError):
            creds = None
        name = name_for(home, email_of(home, creds if isinstance(creds, dict) else None), taken)
        taken.add(name)
        out.append(fetch_usage(home, name))
    return out
