"""Monthly API credit of a Claude Max / Team plan, read from its linked Claude Console org.

The plan pays a monthly credit (Max 5x $100, Max 20x $200, no rollover) into a Console organization.
redline's OAuth login can't see that org; the Console's own web API can, with its session cookie,
which `redline console <name>` captures once (see console_signin). Stored per login, never per
account name, so a login dir that changes hands can't show someone else's credit.

Standard library only: this runs in the core monitor.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .http import HTTPStatusError, request_json
from .models import ProviderError, Usage
from .providers._util import parse_iso, read_json, token_id, write_json_atomic

API_BASE = "https://api.anthropic.com"
ORGS_URL = API_BASE + "/api/organizations"
CREDIT_PROGRAM = "subscription_api_credit"
SESSION_COOKIE = "sessionKey"
CREDIT_TTL = 10 * 60  # the balance changes slowly; the main poll runs every 2 min
CREDIT_RETRY = 2 * 60  # a read that failed (network, 5xx, 429, a bot check) is tried again sooner

# credit["state"]: "ok" fresh · "stale" last value, the latest read failed · "reconnect" the Console
# session was refused (401/403) · "none" read fine, but this month's plan credit isn't there
STATES = ("ok", "stale", "reconnect", "none")


# ---------------------------------------------------------------- parsing (pure)


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _amount(tranche: dict, structured: str, flat: str) -> float | None:
    """Major units (dollars) of one tranche amount; None when the document doesn't say."""
    obj = tranche.get(structured)
    if isinstance(obj, dict):
        for kind in ("credits", "money"):
            m = obj.get(kind)
            if isinstance(m, dict) and _number(m.get("amount_minor")):
                exp = m.get("exponent")
                exp = exp if isinstance(exp, int) and not isinstance(exp, bool) and 0 <= exp <= 6 else 2
                return m["amount_minor"] / 10 ** exp
    v = tranche.get(flat)
    return v / 100 if _number(v) else None


def plan_tranches(doc) -> list[dict]:
    if not isinstance(doc, dict) or not isinstance(doc.get("promo_tranches"), list):
        return []
    return [t for t in doc["promo_tranches"] if isinstance(t, dict) and t.get("program_id") == CREDIT_PROGRAM]


def parse_credit(doc, now: datetime | None = None) -> dict:
    """The plan's monthly credit from a prepaid/credits document.

    Only promo tranches of the subscription program count (cloud credits and other programs don't).
    A tranche whose expiry has passed is gone (no rollover). A missing amount is unknown, not 0.
    """
    now = now or datetime.now(timezone.utc)
    live = []
    for t in plan_tranches(doc):
        exp = parse_iso(t.get("expires_at")) if isinstance(t.get("expires_at"), str) else None
        if exp is not None and exp <= now:
            continue
        live.append((t, exp))
    if not live:
        # Between one month's credit lapsing and the next one being paid in there is none; say when
        # the last one lapsed, so the console can tell "not paid yet" from "never had one".
        gone = [e for e in (parse_iso(t.get("expires_at")) if isinstance(t.get("expires_at"), str) else None
                            for t in plan_tranches(doc)) if e is not None]
        out = {"state": "none", "granted": None, "remaining": None, "currency": None, "expires_at": None}
        if gone:
            out["lapsed_at"] = max(gone).isoformat()
        return out

    def total(structured, flat):
        vals = [_amount(t, structured, flat) for t, _ in live]
        return None if any(v is None for v in vals) else round(sum(vals), 6)

    known = [e for _, e in live if e is not None]
    currencies = {t.get("currency") for t, _ in live if isinstance(t.get("currency"), str)}
    return {
        "state": "ok",
        "granted": total("granted", "granted_amount_minor_units"),
        "remaining": total("remaining", "remaining_amount_minor_units"),
        "currency": currencies.pop() if len(currencies) == 1 else None,
        "expires_at": min(known).isoformat() if known else None,  # the soonest to lapse
    }


def has_plan_credit(doc) -> bool:
    return bool(plan_tranches(doc))


def same_org(orgs, claude_org_uuid: str | None) -> bool:
    """Does this Console session belong to the login whose claude.ai org is claude_org_uuid?"""
    if not claude_org_uuid or not isinstance(orgs, list):
        return False
    return any(isinstance(o, dict) and o.get("uuid") == claude_org_uuid for o in orgs)


def console_orgs(orgs) -> list[dict]:
    if not isinstance(orgs, list):
        return []
    return [o for o in orgs if isinstance(o, dict) and isinstance(o.get("uuid"), str) and o["uuid"]
            and isinstance(o.get("capabilities"), list) and "api" in o["capabilities"]]


def pick_console_org(orgs, balances: dict) -> str | None:
    """The org the plan pays its credit into: an API org whose balance has the subscription
    credit; failing that the first API org (it may simply not have been paid yet)."""
    cands = console_orgs(orgs)
    for o in cands:
        if has_plan_credit(balances.get(o["uuid"])):
            return o["uuid"]
    return cands[0]["uuid"] if cands else None


# ---------------------------------------------------------------- session store


def _dir() -> Path:
    from .config import redline_home

    return redline_home() / "console"


def _slug(identity_key: str) -> str:
    return token_id(identity_key)  # a file name whatever the key holds


def session_path(identity_key: str) -> Path:
    return _dir() / f"{_slug(identity_key)}.json"


def webview_dir(identity_key: str) -> Path:
    """The sign-in window's own browser profile for this login (sessions never mix)."""
    return _dir() / "webview" / _slug(identity_key)


def load_session(identity_key: str | None) -> dict | None:
    if not identity_key:
        return None
    try:
        doc = read_json(session_path(identity_key))
    except (OSError, ValueError):
        return None
    if (not isinstance(doc, dict) or doc.get("identity_key") != identity_key
            or not isinstance(doc.get("session_key"), str) or not doc["session_key"]
            or not isinstance(doc.get("console_org_uuid"), str) or not doc["console_org_uuid"]):
        return None
    return doc


def save_session(identity_key: str, session_key: str, console_org_uuid: str, claude_org_uuid: str) -> Path:
    path = session_path(identity_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, {
        "identity_key": identity_key,
        "session_key": session_key,
        "console_org_uuid": console_org_uuid,
        "claude_org_uuid": claude_org_uuid,
        "connected_at": datetime.now(timezone.utc).isoformat(),
    })
    _cache.pop(identity_key, None)
    return path


# ---------------------------------------------------------------- Console web API


def _headers(session_key: str) -> dict[str, str]:
    from . import __version__

    # Without a User-Agent urllib sends "Python-urllib/3.x", which the edge in front of the Console
    # API answers with a bot-check page now and then (a 403 that is not about the session).
    return {"Cookie": f"{SESSION_COOKIE}={session_key}", "User-Agent": f"redline/{__version__}",
            "Accept": "application/json"}


def refused(e: HTTPStatusError) -> bool:
    """The Console says the session is no good (sign in again), as opposed to anything else.

    401 always is. A 403 is only when the API itself answers it (a JSON error); an HTML 403 is the
    edge's bot check or a block in between, which says nothing about the session: try again later.
    """
    if e.status == 401:
        return True
    if e.status != 403:
        return False
    body = (e.body or "").lstrip()
    return not body or body.startswith("{")


def get_orgs(session_key: str) -> list:
    doc = request_json("GET", ORGS_URL, headers=_headers(session_key))
    return doc if isinstance(doc, list) else []


def get_balance(session_key: str, org_uuid: str) -> dict:
    return request_json("GET", f"{ORGS_URL}/{org_uuid}/prepaid/credits", headers=_headers(session_key))


# ---------------------------------------------------------------- fetch with a cache

_cache: dict[str, tuple[float, str, dict]] = {}  # identity_key -> (read at, session id, credit)
_lock = threading.Lock()


def _stamp(credit: dict, state: str | None = None) -> dict:
    return {**credit, **({"state": state} if state else {}), "fetched_at": datetime.now(timezone.utc).isoformat()}


def fetch_credit(identity_key: str | None, now: float | None = None) -> dict | None:
    """This login's credit, None when no Console is connected. Never raises.

    Read at most every CREDIT_TTL per login, whatever the outcome; a new session (reconnect) reads
    at once. 401/403: "reconnect". Network or server trouble: the last value, marked "stale".
    """
    sess = load_session(identity_key)
    if sess is None:
        return None
    now = time.time() if now is None else now
    sid = token_id(sess["session_key"])
    with _lock:
        hit = _cache.get(identity_key)
        if hit and hit[1] == sid:
            ttl = CREDIT_RETRY if hit[2].get("state") == "stale" else CREDIT_TTL
            if now - hit[0] < ttl:
                return hit[2]
    prev = hit[2] if hit and hit[1] == sid else None
    try:
        credit = _stamp(parse_credit(get_balance(sess["session_key"], sess["console_org_uuid"])))
    except HTTPStatusError as e:
        state = "reconnect" if refused(e) else "stale"
        credit = carry(_stamp(_blank(), state), prev)
        _log(f"console credit: HTTP {e.status}" + ("" if state == "reconnect" else " (not a session refusal)"))
    except ProviderError as e:
        credit = carry(_stamp(_blank(), "stale"), prev)
        _log(f"console credit: {type(e).__name__}")
    except Exception as e:  # defensive: the credit must never break the usage poll
        credit = carry(_stamp(_blank(), "stale"), prev)
        _log(f"console credit: {type(e).__name__}")
    with _lock:
        _cache[identity_key] = (now, sid, credit)
    return credit


def _blank() -> dict:
    return {"state": "stale", "granted": None, "remaining": None, "currency": None, "expires_at": None}


def _log(text: str) -> None:  # status only: never the session key, never a response body
    try:
        from .applog import log

        log.info(text)
    except Exception:
        pass


def carry(new: dict | None, old: dict | None) -> dict | None:
    """A failed read ("stale" / "reconnect") keeps showing the last known amounts."""
    if not isinstance(new, dict) or new.get("state") not in ("stale", "reconnect") or not isinstance(old, dict):
        return new
    if new.get("remaining") is not None or old.get("state") == "none":
        return new
    keep = {k: old.get(k) for k in ("granted", "remaining", "currency", "expires_at")}
    if all(v is None for v in keep.values()):
        return new
    return {**new, **keep, "fetched_at": old.get("fetched_at") or new.get("fetched_at")}


def attach(u: Usage) -> Usage:
    """Add the credit to a Claude usage (by the login it was fetched for). Never raises."""
    if u.provider != "claude":
        return u
    try:
        u.credit = fetch_credit(u.identity_key)
    except Exception:
        u.credit = None
    return u
