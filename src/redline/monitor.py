"""Background poller shared by the tray app and the web dashboard.

Keeps the latest snapshot of every account, an event log for the console, and
fires threshold alerts (e.g. "work 5h crossed 90%") exactly once per crossing.
"""

from __future__ import annotations

import itertools
import json
import math
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Callable

from .config import load_accounts, redline_home
from .models import Usage, weekly_countdown

DEFAULT_THRESHOLDS = (80, 95)
FORCE_FLOOR = 15  # seconds; even a manual refresh can't hit upstream more often than this
RESET_BELOW = 20  # a window that was >= first threshold and is now below this "has reset"
WEEKLY_REMINDER_INTERVAL = 24 * 60 * 60

# Usage rate: how fast the short window (5h; the first window if there is none) is filling,
# measured over the last RATE_LOOKBACK seconds. "pace" 1.0 = on track to use exactly the whole
# window over its length (5h: 20%/h). Levels: idle (no change), low < 0.5, mid < 1.25, high.
RATE_LOOKBACK = 30 * 60
RATE_MIN_SPAN = 4 * 60  # need at least this much history before saying anything
RATE_LEVELS = ((0.5, "low"), (1.25, "mid"))


def _window_hours(name: str) -> float | None:
    import re

    m = re.fullmatch(r"(\d+(?:\.\d+)?)([hd])", name)
    return float(m.group(1)) * (24 if m.group(2) == "d" else 1) if m else None


def rate_window(u: Usage):
    return next((w for w in u.windows if w.name == "5h"), None) or next(
        (w for w in u.windows if _window_hours(w.name)), None)


def usage_rate(samples, now: float) -> dict | None:
    """samples: [(t, percent), ...] oldest first, all from the current window (no reset in between)."""
    recent = [s for s in samples if s[0] >= now - RATE_LOOKBACK]
    older = [s for s in samples if s[0] < now - RATE_LOOKBACK]
    base = older[-1] if older else (recent[0] if recent else None)  # span the whole lookback when we can
    if base is None or not recent:
        return None
    t1, p1 = samples[-1]
    span = t1 - base[0]
    if span < RATE_MIN_SPAN:
        return None
    per_hour = max(0.0, p1 - base[1]) / (span / 3600)
    return {"per_hour": round(per_hour, 1)}


def rate_level(per_hour: float, hours: float) -> str:
    if per_hour <= 0:
        return "idle"
    pace = per_hour * hours / 100
    return next((name for limit, name in RATE_LEVELS if pace < limit), "high")


class Monitor:
    def __init__(
        self,
        interval: int = 120,
        refresh_tokens: bool = False,
        thresholds: tuple[int, ...] = DEFAULT_THRESHOLDS,
        collect: Callable[..., list[Usage]] | None = None,
    ):
        if collect is None:
            from .cli import collect
        self._collect = collect
        self.interval = interval
        self.refresh_tokens = refresh_tokens
        self.thresholds = tuple(sorted(thresholds))
        self.listeners: list[Callable[[list[Usage], list[dict]], None]] = []

        self._poll_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ids = itertools.count(1)

        self.usages: list[Usage] = []
        self.at = 0.0
        self.events: deque[dict] = deque(maxlen=200)
        self.next_at = 0.0
        self.meta: dict = {}  # extra status for the console (e.g. {"update": {...}} from the tray)
        self._last_pct: dict[tuple[str, str, str], float] = {}  # (provider, account, window)
        self._samples: dict[tuple[str, str], list[tuple[float, float]]] = {}  # (provider, account) -> [(t, %)]
        # Last warning state and time. A missed active period must not suppress reminders forever.
        self._weekly_reminders: dict[tuple[str, str], tuple[str, float]] = {}
        self._last_ok: dict[tuple[str, str], Usage] = self._load_last_ok()  # (provider, account)

    # ------------------------------------------------------------ lifecycle

    def start(self) -> "Monitor":
        self._thread = threading.Thread(target=self._run, name="redline-monitor", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll()
            self.next_at = time.time() + self.interval
            self._wake.wait(self.interval)
            self._wake.clear()

    # ------------------------------------------------------------ polling

    def poll(self, force: bool = False) -> None:
        with self._poll_lock:
            if force and time.time() - self.at < FORCE_FLOOR:
                return
            try:
                from .slots import reconcile

                for msg in reconcile():  # VS Code / `codex login` put another account in the default slot
                    self.log("warn", msg, alert=True)
            except Exception:
                pass
            try:
                from .actions import adopt_email_names

                for prov, old, new in adopt_email_names():
                    self.rename(old, new, prov)
                    self.log("info", f"{old} -> {new}")
            except Exception:
                pass
            try:
                usages = self._collect(load_accounts(), refresh_tokens=self.refresh_tokens)
            except Exception as e:  # config file broken etc. -- keep the loop alive
                self.log("error", f"sync failed: {e}")
                return
            for u in usages:
                self._reset_changed_codex_identity(u)
                self._track_rate(u)
            usages = [self._with_last_good(u) for u in usages]
            try:
                from .actions import account_details

                self.meta["accounts"] = account_details()
            except Exception:
                pass
            alerts = self._diff(usages)
            if any(u.ok for u in usages):
                self._save_last_ok()
            with self._state_lock:
                self.usages = usages
                self.at = time.time()
            bad = sum(not u.ok for u in usages)
            self.log(
                "warn" if bad else "info",
                f"sync {len(usages) - bad}/{len(usages)} nodes ok" + (f", {bad} fault" if bad else ""),
            )
            for a in alerts:
                self.log(a["level"], a["text"], alert=True)
        for fn in self.listeners:
            try:
                fn(usages, alerts)
            except Exception:
                pass

    # Last good numbers survive restarts (self-updates, reboots): an account whose token has
    # expired while nothing used it would otherwise show no numbers at all until it is used again.
    @staticmethod
    def _last_ok_path():
        return redline_home() / "last_usage.json"

    def _load_last_ok(self) -> dict[tuple[str, str], Usage]:
        try:
            docs = json.loads(self._last_ok_path().read_text(encoding="utf-8"))
            out = {}
            for d in docs:
                key = (d["provider"], d["account"])
                out[key] = Usage.from_dict(d)
                identity_known = d["provider"] != "codex" or bool(out[key].identity_key)
                if identity_known:
                    reminder = d.get("weekly_countdown_reminder")
                    if (isinstance(reminder, dict)
                            and reminder.get("state") in ("unstarted", "unknown")
                            and isinstance(reminder.get("at"), (int, float))
                            and not isinstance(reminder["at"], bool)
                            and math.isfinite(reminder["at"])):
                        self._weekly_reminders[key] = (reminder["state"], min(reminder["at"], time.time()))
                    elif d.get("weekly_countdown_reminded") is True:
                        # Older caches only stored a boolean. Their last successful fetch is
                        # a conservative upper bound on when that warning was shown.
                        fetched = out[key].fetched_at
                        if fetched.tzinfo is None:
                            fetched = fetched.replace(tzinfo=timezone.utc)
                        self._weekly_reminders[key] = ("unstarted", min(fetched.timestamp(), time.time()))
                # the rate history too, so a restart (self-update) doesn't blank the gauge for minutes
                cutoff = time.time() - RATE_LOOKBACK * 2
                hist = [(float(t), float(p)) for t, p in d.get("rate_samples") or [] if float(t) > cutoff]
                if hist and identity_known:
                    self._samples[key] = hist
            return out
        except (OSError, ValueError, KeyError, TypeError):
            return {}

    def _save_last_ok(self) -> None:
        try:
            p = self._last_ok_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            docs = [{**u.to_dict(), "rate_samples": self._samples.get(k, []),
                     "weekly_countdown_reminded": k in self._weekly_reminders,
                     "weekly_countdown_reminder": {
                         "state": self._weekly_reminders[k][0], "at": self._weekly_reminders[k][1],
                     } if k in self._weekly_reminders else None}
                    for k, u in self._last_ok.items()]
            tmp.write_text(json.dumps(docs, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(p)
        except OSError:
            pass

    def _reset_changed_codex_identity(self, u: Usage) -> None:
        if u.provider != "codex" or not u.ok or u.stale:
            return
        key = (u.provider, u.account)
        prev = self._last_ok.get(key)
        if prev is None or prev.identity_key == u.identity_key:
            return
        self._samples.pop(key, None)
        self._weekly_reminders.pop(key, None)
        for k in [k for k in self._last_pct if k[:2] == key]:
            self._last_pct.pop(k)
        u.rate = None

    def _track_rate(self, u: Usage, now: float | None = None) -> None:
        if not u.ok:
            return
        w = rate_window(u)
        hours = _window_hours(w.name) if w else None
        if w is None or w.used_percent is None or not hours:
            return
        now = time.time() if now is None else now
        key = (u.provider, u.account)
        hist = self._samples.setdefault(key, [])
        if hist and w.used_percent < hist[-1][1] - 0.5:  # the window reset: start over
            hist.clear()
        hist.append((now, w.used_percent))
        del hist[:max(0, len([s for s in hist if s[0] < now - RATE_LOOKBACK]) - 1)]  # keep one older sample
        r = usage_rate(hist, now)
        if r is not None:
            u.rate = {**r, "level": rate_level(r["per_hour"], hours), "window": w.name,
                      "pace": round(r["per_hour"] * hours / 100, 2)}

    def _with_last_good(self, u: Usage) -> Usage:
        """Keep showing an account's last good numbers when a fetch fails (429, offline, expired...)."""
        if u.ok:
            self._last_ok[(u.provider, u.account)] = u
            return u
        prev = self._last_ok.get((u.provider, u.account))
        if prev is None or not prev.windows:
            return u
        if u.provider == "codex" and (
            not u.identity_key or not prev.identity_key or u.identity_key != prev.identity_key
        ):
            # A login directory can now belong to a different account. Old caches or
            # an unreadable current identity cannot safely supply its usage numbers.
            return u
        return Usage(
            account=u.account, provider=u.provider, ok=False, email=prev.email, plan=prev.plan,
            windows=prev.windows, extra=prev.extra, error=u.error, fetched_at=prev.fetched_at, stale=True,
            renews_at=prev.renews_at, renews_kind=prev.renews_kind,
            identity_key=u.identity_key,
        )

    def rename(self, old: str, new: str, provider: str | None = None) -> None:
        """Carry an account's cached state over to its new name (instant UI, no re-alerts)."""
        match = lambda p, n: n == old and (provider is None or p == provider)  # noqa: E731
        with self._state_lock:
            for k in [k for k in self._samples if match(*k)]:
                self._samples[(k[0], new)] = self._samples.pop(k)
            for k in [k for k in self._weekly_reminders if match(*k)]:
                self._weekly_reminders[(k[0], new)] = self._weekly_reminders.pop(k)
            for k in [k for k in self._last_ok if match(*k)]:
                u = self._last_ok.pop(k)
                u.account = new
                self._last_ok[(k[0], new)] = u
                self._save_last_ok()
            for k in [k for k in self._last_pct if match(k[0], k[1])]:
                self._last_pct[(k[0], new, k[2])] = self._last_pct.pop(k)
            for u in self.usages:
                if match(u.provider, u.account):
                    u.account = new
            accts = self.meta.get("accounts") or {}
            for k in [k for k in accts if match(*k.split(":", 1))]:
                accts[f"{k.split(':', 1)[0]}:{new}"] = accts.pop(k)
            self.at = time.time()  # makes the console re-render right away

    def refresh_now(self) -> None:
        self.poll(force=True)

    def _diff(self, usages: list[Usage]) -> list[dict]:
        alerts = []
        now = time.time()
        first = self.thresholds[0] if self.thresholds else 101
        for u in usages:
            if not u.ok:  # includes stale snapshots: don't re-alert on old numbers
                continue
            details = (self.meta.get("accounts") or {}).get(f"{u.provider}:{u.account}", {})
            if u.provider == "codex" and details.get("login_mismatch") is True:
                # These numbers belong to a different login, not the named account.
                continue
            if not u.stale:
                key = (u.provider, u.account)
                state = weekly_countdown(u)["state"]
                if state in ("unstarted", "unknown"):
                    previous = self._weekly_reminders.get(key)
                    # Confirmed inactivity is new actionable evidence after an uncertain
                    # warning; the reverse transition must not double-notify a transient gap.
                    if (previous is None or now - previous[1] >= WEEKLY_REMINDER_INTERVAL
                            or previous[0] == "unknown" and state == "unstarted"):
                        self._weekly_reminders[key] = (state, now)
                        text = (f'{u.account} weekly 尚未开始计时；点「开启计时」发送一次短请求'
                                if state == "unstarted" else
                                f"{u.account} weekly 待确认；请刷新用量核验计时是否已开启")
                        alerts.append({"level": "warn", "text": text})
                elif state == "active":
                    self._weekly_reminders.pop(key, None)
            for w in u.windows:
                if w.used_percent is None:
                    continue
                key = (u.provider, u.account, w.name)
                prev = self._last_pct.get(key)
                cur = w.used_percent
                self._last_pct[key] = cur
                if prev is None:
                    continue
                crossed = [t for t in self.thresholds if prev < t <= cur]
                if crossed:
                    t = crossed[-1]
                    alerts.append({
                        "level": "crit" if t == self.thresholds[-1] else "warn",
                        "text": f"{u.account} {w.name} usage hit {cur:.0f}% (>= {t}%)",
                    })
                elif prev >= first and cur < RESET_BELOW:
                    alerts.append({"level": "ok", "text": f"{u.account} {w.name} window reset -> {cur:.0f}%"})
        return alerts

    # ------------------------------------------------------------ views

    def log(self, level: str, text: str, alert: bool = False) -> None:
        with self._state_lock:
            self.events.append({
                "id": next(self._ids),
                "ts": datetime.now(timezone.utc).isoformat(),
                "level": level,
                "text": text,
                "alert": alert,
            })

    def payload(self, force: bool = False, since: int = 0) -> dict:
        if force:
            self.refresh_now()
        with self._state_lock:
            return {
                "fetched_at": self.at,
                "next_at": self.next_at,
                "now": time.time(),
                "interval": self.interval,
                "auto_refresh": self.refresh_tokens,
                "usages": [u.to_dict() for u in self.usages],
                "events": [e for e in self.events if e["id"] > since],
                "meta": dict(self.meta),
            }

    def max_percent(self, window_prefix: str = "5h") -> float | None:
        """Highest usage of the short (session) window across healthy accounts."""
        vals = [
            w.used_percent
            for u in self.usages if u.ok or u.stale
            for w in u.windows if w.used_percent is not None and w.name == window_prefix
        ]
        return max(vals) if vals else None
