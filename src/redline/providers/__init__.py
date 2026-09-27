from __future__ import annotations

from ..config import Account
from ..models import ProviderError, Usage
from . import claude, codex

_MODULES = {"claude": claude, "codex": codex}


def has_credentials(account: Account) -> bool:
    try:
        return _MODULES[account.provider].has_credentials(account)
    except Exception:
        return False


def identity(account: Account) -> str | None:
    """Email the account is logged in as (from local files, no network)."""
    try:
        return _MODULES[account.provider].identity(account)
    except Exception:
        return None


def token_state(account: Account) -> str:
    try:
        return _MODULES[account.provider].token_state(account)
    except Exception:
        return "missing"


def load_fingerprint(account: Account) -> str | None:
    """Identifies the current access token (changes after a login/refresh)."""
    import hashlib

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


def refresh_account(account: Account) -> None:
    _MODULES[account.provider].refresh_account(account)


def fetch_usage(account: Account, refresh_tokens: bool = False) -> Usage:
    """Never raises: failures come back as Usage(ok=False, error=...)."""
    try:
        return _MODULES[account.provider].fetch_usage(account, refresh_tokens=refresh_tokens)
    except ProviderError as e:
        return Usage(account=account.name, provider=account.provider, ok=False, error=str(e))
    except Exception as e:  # defensive: one broken account must not kill the dashboard
        return Usage(
            account=account.name, provider=account.provider, ok=False,
            error=f"{type(e).__name__}: {e}",
        )
