from __future__ import annotations

from ..config import Account
from ..models import ProviderError, Usage
from . import claude, codex

_MODULES = {"claude": claude, "codex": codex}


def _eff(account: Account) -> Account:
    """Where the account's login lives right now (the default dir while it is the default)."""
    from ..slots import effective

    try:
        return effective(account)
    except Exception:
        return account


def has_credentials(account: Account) -> bool:
    try:
        return _MODULES[account.provider].has_credentials(_eff(account))
    except Exception:
        return False


def identity(account: Account) -> str | None:
    """Email the account is logged in as (from local files, no network)."""
    try:
        return _MODULES[account.provider].identity(_eff(account))
    except Exception:
        return None


def token_state(account: Account) -> str:
    try:
        return _MODULES[account.provider].token_state(_eff(account))
    except Exception:
        return "missing"


def load_fingerprint(account: Account) -> str | None:
    """Identifies the current access token (changes after a login/refresh)."""
    import hashlib

    account = _eff(account)
    try:
        if account.provider == "claude":
            c = claude.load_creds(account)
            tok = c.oauth.get("accessToken") if c else None
        else:
            a = codex.load_auth(account)
            tok = a["tokens"]["access_token"] if a else None
    except Exception:
        return None
    return hashlib.sha256(tok.encode()).hexdigest()[:16] if tok else None


def _codex_identity_key(account: Account) -> str | None:
    """Hash the selected ChatGPT identity, never a rotating access token."""
    import hashlib
    import json

    from ._util import read_json

    def key(kind, value):
        material = json.dumps(["codex", kind, value], separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    try:
        auth = read_json(account.home_path / "auth.json")
        if not isinstance(auth, dict):
            return None
        tokens = auth.get("tokens") or {}
        if not isinstance(tokens, dict) or not tokens.get("access_token"):
            api_key = auth.get("OPENAI_API_KEY")
            return key("api-key", api_key) if isinstance(api_key, str) and api_key else None
        id_claims = codex.jwt_claims(tokens.get("id_token"))
        access_claims = codex.jwt_claims(tokens.get("access_token"))
        claims = [id_claims, access_claims]
        account_id = tokens.get("account_id") or next(
            ((c.get(codex.AUTH_CLAIM) or {}).get("chatgpt_account_id") for c in claims
             if isinstance(c.get(codex.AUTH_CLAIM), dict)
             and (c.get(codex.AUTH_CLAIM) or {}).get("chatgpt_account_id")), None)
        if isinstance(account_id, str) and account_id:
            user = codex.chatgpt_user_id(tokens)  # the workspace alone would merge Team seats
            return key("chatgpt-account", [account_id, user] if user else account_id)
        subject = next((c.get("sub") for c in claims if isinstance(c.get("sub"), str) and c["sub"]), None)
        email = next((c.get("email") or (c.get(codex.PROFILE_CLAIM) or {}).get("email") for c in claims
                      if c.get("email") or (isinstance(c.get(codex.PROFILE_CLAIM), dict)
                                            and c[codex.PROFILE_CLAIM].get("email"))), None)
        if subject:
            return key("subject", subject)
        if isinstance(email, str) and email:
            return key("email", email.strip().casefold())
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def _claude_identity_key(account: Account) -> str | None:
    """Hash of the login Claude Code cached for this home (account uuid, else email); never a token."""
    import hashlib
    import json

    try:
        uuid, email = claude.login_ref(account)
    except Exception:
        return None
    kind, value = ("account", uuid) if uuid else ("email", email)
    if not value:
        return None
    material = json.dumps(["claude", kind, value], separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _identity_key(account: Account) -> str | None:
    """Identity of the login in exactly this account's dir (no default-slot resolution)."""
    if account.provider == "codex":
        return _codex_identity_key(account)
    if account.provider == "claude":
        return _claude_identity_key(account)
    return None


def load_identity_key(account: Account) -> str | None:
    """Stable anonymized identity used to validate an account's cached usage."""
    return _identity_key(_eff(account))


def refresh_account(account: Account, manual: bool = False) -> None:
    _MODULES[account.provider].refresh_account(_eff(account), manual=manual)


def fetch_usage(account: Account, refresh_tokens: bool = False) -> Usage:
    """Never raises: failures come back as Usage(ok=False, error=...)."""
    effective = _eff(account)
    before = _identity_key(effective)
    try:
        u = _MODULES[account.provider].fetch_usage(effective, refresh_tokens=refresh_tokens)
    except ProviderError as e:
        u = Usage(account=account.name, provider=account.provider, ok=False, error=str(e))
    except Exception as e:  # defensive: one broken account must not kill the dashboard
        u = Usage(
            account=account.name, provider=account.provider, ok=False,
            error=f"{type(e).__name__}: {e}",
        )
    current = load_identity_key(account)
    changed = before != current
    if account.provider == "claude":
        # Claude's email comes from the dir after the fetch: if that dir changed hands meanwhile
        # (a default switch), the numbers and the email may belong to two different logins.
        changed = changed or before != _identity_key(effective)
    if u.ok and changed:
        u = Usage(account=account.name, provider=account.provider, ok=False,
                  error="account login changed while fetching usage; refresh to verify")
    u.identity_key = current
    return u
