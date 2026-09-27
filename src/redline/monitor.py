"""Background poller shared by the tray app and the web dashboard.

Keeps the latest snapshot of every account, an event log for the console, and
fires threshold alerts (e.g. "work 5h crossed 90%") exactly once per crossing.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Callable

from .config import load_accounts
from .models import Usage

DEFAULT_THRESHOLDS = (80, 95)
FORCE_FLOOR = 15  # seconds; even a manual refresh can't hit upstream more often than this
RESET_BELOW = 20  # a window that was >= first threshold and is now below this "has reset"


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
        self._last_ok: dict[tuple[str, str], Usage] = {}  # (provider, account)

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
            usages = [self._with_last_good(u) for u in usages]
            try:
                from .actions import account_details

                self.meta["accounts"] = account_details()
            except Exception:
                pass
            alerts = self._diff(usages)
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

    def _with_last_good(self, u: Usage) -> Usage:
        """Keep showing an account's last good numbers when a fetch fails (429, offline...)."""
        if u.ok:
            self._last_ok[(u.provider, u.account)] = u
            return u
        prev = self._last_ok.get((u.provider, u.account))
        if prev is None or not prev.windows:
            return u
        return Usage(
            account=u.account, provider=u.provider, ok=False, email=prev.email, plan=prev.plan,
            windows=prev.windows, extra=prev.extra, error=u.error, fetched_at=prev.fetched_at, stale=True,
        )

    def rename(self, old: str, new: str, provider: str | None = None) -> None:
        """Carry an account's cached state over to its new name (instant UI, no re-alerts)."""
        match = lambda p, n: n == old and (provider is None or p == provider)  # noqa: E731
        with self._state_lock:
            for k in [k for k in self._last_ok if match(*k)]:
                u = self._last_ok.pop(k)
                u.account = new
                self._last_ok[(k[0], new)] = u
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
        first = self.thresholds[0] if self.thresholds else 101
        for u in usages:
            if not u.ok:  # includes stale snapshots: don't re-alert on old numbers
                continue
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
