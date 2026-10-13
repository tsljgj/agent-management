from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


@dataclass
class Window:
    """One rate-limit window, e.g. the 5-hour session or the weekly cap."""

    name: str  # "5h", "7d", "7d opus", ...
    used_percent: float | None
    resets_at: datetime | None = None
    # Codex uses zero as a reset sentinel. Keep it distinct from a missing field,
    # including in the last-good cache; neither zero nor null is an epoch date.
    reset_at_raw: float | int | None = None
    reset_after_seconds_raw: float | int | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["resets_at"] = self.resets_at.isoformat() if self.resets_at else None
        for key in ("reset_at_raw", "reset_after_seconds_raw"):
            if d[key] is None:
                d.pop(key)
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
    # Stable, hashed login identity for matching cached Codex quota to its owner.
    # Access tokens rotate and are deliberately not used as this key.
    identity_key: str | None = None

    def to_dict(self) -> dict:
        d = {
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
            "weekly_countdown": weekly_countdown(self),
        }
        if self.identity_key is not None:
            d["identity_key"] = self.identity_key
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Usage":
        def dt(v):
            return datetime.fromisoformat(v) if v else None

        return cls(
            account=d["account"], provider=d["provider"], ok=bool(d.get("ok")),
            email=d.get("email"), plan=d.get("plan"),
            windows=[Window(w["name"], w.get("used_percent"), dt(w.get("resets_at")),
                            w.get("reset_at_raw"), w.get("reset_after_seconds_raw"))
                     for w in d.get("windows") or []],
            extra=d.get("extra") or {}, error=d.get("error"),
            fetched_at=dt(d.get("fetched_at")) or datetime.now(timezone.utc), stale=bool(d.get("stale")),
            renews_at=d.get("renews_at"), renews_kind=d.get("renews_kind"),
            identity_key=d.get("identity_key"),
        )


def weekly_countdown(u: Usage, now: datetime | None = None) -> dict:
    """Report whether a live Codex weekly window has actually begun counting down.

    Percentages are rounded upstream, so zero usage with a future reset is active.
    A cached or expired window cannot prove that a new weekly period has started.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    w = next((w for w in u.windows if w.name == "7d"), None)
    reset = w.resets_at if w else None
    if reset is not None and reset.tzinfo is None:
        reset = reset.replace(tzinfo=timezone.utc)

    def result(state: str, message: str) -> dict:
        return {"state": state, "message": message,
                "resets_at": reset.isoformat() if reset else None}

    if u.provider != "codex":
        return result("unavailable", "Weekly activation applies to Codex ChatGPT accounts.")
    if "api key" in (u.error or "").lower() or "api-key" in (u.error or "").lower():
        return result("unavailable", "API key accounts do not have a ChatGPT weekly countdown.")
    if not u.ok or u.stale:
        return result("unknown", "Refresh usage to verify the weekly countdown.")
    if w is None:
        if any(window.name in ("primary", "secondary") for window in u.windows):
            return result("unknown", "Usage window duration is missing; refresh usage to verify the weekly countdown.")
        return result("unavailable", "No weekly usage window is reported for this account.")
    if reset is not None:
        if reset > now:
            return result("active", "Weekly countdown is active.")
        return result("unknown", "The previous weekly reset has passed; refresh usage to verify.")
    if w.used_percent == 0 and (w.reset_at_raw == 0 or w.reset_after_seconds_raw == 0):
        return result("unstarted", "Weekly countdown has not started; run a small Codex request.")
    return result("unknown", "No weekly reset time is confirmed; try a small Codex request and verify.")


class ProviderError(Exception):
    pass
