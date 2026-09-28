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
    stale: bool = False  # windows are from an earlier successful fetch (this one failed)
    renews_at: str | None = None  # ISO date the subscription is paid until (renews or ends then)
    renews_kind: str | None = None  # "end": renews_at is the end of the paid period; "start": it's when
    #                                the subscription began (the console rolls it forward by months)
    rate: dict | None = None  # how fast the account is being used right now (see monitor.usage_rate)

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
            "stale": self.stale,
            "renews_at": self.renews_at,
            "renews_kind": self.renews_kind,
            "rate": self.rate,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Usage":
        def dt(v):
            return datetime.fromisoformat(v) if v else None

        return cls(
            account=d["account"], provider=d["provider"], ok=bool(d.get("ok")),
            email=d.get("email"), plan=d.get("plan"),
            windows=[Window(w["name"], w.get("used_percent"), dt(w.get("resets_at"))) for w in d.get("windows") or []],
            extra=d.get("extra") or {}, error=d.get("error"),
            fetched_at=dt(d.get("fetched_at")) or datetime.now(timezone.utc), stale=bool(d.get("stale")),
            renews_at=d.get("renews_at"), renews_kind=d.get("renews_kind"),
        )


class ProviderError(Exception):
    pass
