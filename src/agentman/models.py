from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


@dataclass
class Window:
    """One rate-limit window, e.g. the 5-hour session or the weekly cap."""

    name: str  # "5h", "7d", "7d opus", ...
    used_percent: float | None
    resets_at: datetime | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["resets_at"] = self.resets_at.isoformat() if self.resets_at else None
        return d


@dataclass
class Usage:
    account: str
    provider: str
    ok: bool
    email: str | None = None
    plan: str | None = None
    windows: list[Window] = field(default_factory=list)
    extra: dict = field(default_factory=dict)  # provider-specific (credits, extra usage...)
    error: str | None = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "account": self.account,
            "provider": self.provider,
            "ok": self.ok,
            "email": self.email,
            "plan": self.plan,
            "windows": [w.to_dict() for w in self.windows],
            "extra": self.extra,
            "error": self.error,
            "fetched_at": self.fetched_at.isoformat(),
        }


class ProviderError(Exception):
    pass
