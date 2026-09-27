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
