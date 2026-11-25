"""Terminal rendering of usage snapshots."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import math

from .models import Usage, pct

BAR_WIDTH = 20


def _color_enabled(stream) -> bool:
    return stream.isatty() and os.environ.get("NO_COLOR") is None


def _c(text: str, code: str, on: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if on else text


def bar(percent: float | None, width: int = BAR_WIDTH) -> str:
    if percent is None:
        return "?" * width
    # rounded down: the bar is full only at 100%
    filled = math.floor(max(0.0, min(100.0, percent)) / 100 * width)
    return "█" * filled + "░" * (width - filled)


def humanize_delta(when: datetime | None, now: datetime | None = None) -> str:
    if when is None:
        return ""
    now = now or datetime.now(timezone.utc)
    secs = int((when - now).total_seconds())
    if secs <= 0:
        return "resets now"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        s = f"{days}d{hours}h"
    elif hours:
        s = f"{hours}h{mins:02d}m"
    else:
        s = f"{mins}m"
    return f"resets in {s}"


def _pct_color(p: float | None) -> str:
    if p is None:
        return "2"
    if p >= 90:
        return "31"  # red
    if p >= 70:
        return "33"  # yellow
    return "32"  # green


def _usd(v: float | None) -> str:
    return "$?" if v is None else f"${v:,.2f}"


def _can_print(stream, text: str) -> bool:
    try:
        text.encode(getattr(stream, "encoding", None) or "utf-8")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def credit_line(c: dict | None, now: datetime | None = None, name: str = "", arrow: str = "↻") -> str | None:
    """`API credit  $0.01 / $200.00 · $199.99 left · ↻ 11-21 27d` (None when no Console is connected)."""
    if not isinstance(c, dict):
        return None
    now = now or datetime.now(timezone.utc)
    state = c.get("state")
    exp = None
    if isinstance(c.get("expires_at"), str):
        try:
            exp = datetime.fromisoformat(c["expires_at"])
            exp = exp if exp.tzinfo else exp.replace(tzinfo=timezone.utc)
        except ValueError:
            exp = None
    if state == "none" or (exp is not None and exp <= now):  # this month's credit isn't there (or lapsed)
        text = "–"
    else:
        granted, remaining = c.get("granted"), c.get("remaining")
        used = round(granted - remaining, 2) if granted is not None and remaining is not None else None
        text = f"{_usd(used)} / {_usd(granted)} · {_usd(remaining)} left"
        if exp is not None:
            secs = (exp - now).total_seconds()
            days = int(secs // 86400)
            left = f"{days}d" if days else f"{int(secs // 3600)}h" if secs >= 3600 else f"{max(1, int(secs // 60))}m"
            text += f" · {arrow} {exp.astimezone():%m-%d} {left}"
    if state == "reconnect":
        text += (" · reconnect: redline console " + name) if name else " · reconnect"
    elif state == "stale":
        text += " · stale"
    return f"API credit  {text}"


def render_table(usages: list[Usage], stream=None) -> str:
    stream = stream or sys.stdout
    color = _color_enabled(stream)
    lines: list[str] = []
    name_w = max([len(u.account) for u in usages] + [7])
    for u in usages:
        who = " ".join(x for x in [u.email or "", f"({u.plan})" if u.plan else ""] if x)
        head = f"{_c(u.account.ljust(name_w), '1', color)}  {u.provider:<6}  {who}"
        lines.append(head)
        if not u.ok:
            lines.append("    " + _c(f"error: {u.error}", "31", color))
        elif not u.windows:
            lines.append("    " + _c("no usage windows reported", "2", color))
        for w in u.windows:
            pct_text = f"{pct(w.used_percent):>4}"
            code = _pct_color(w.used_percent)
            lines.append(
                f"    {w.name:<10} {_c(bar(w.used_percent), code, color)} "
                f"{_c(pct_text, code, color)}  {humanize_delta(w.resets_at)}"
            )
        for k, v in u.extra.items():
            if k == "models" and isinstance(v, dict):  # Gemini: what is left of each model
                lines.append(f"    {_c('models:', '2', color)} " + " · ".join(f"{m} {left}" for m, left in v.items()))
                continue
            if k == "banked_resets" and isinstance(v, dict):
                available = v.get("available")
                text = str(available) if available is not None else "unknown"
                lines.append(f"    {_c('Banked resets:', '2', color)} {text}")
                credits = v.get("credits") or []
                for i, credit in enumerate(credits, 1):
                    expiry = credit.get("expires_at")
                    date = datetime.fromisoformat(expiry) if expiry else None
                    expires = date.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z") if date else "unknown"
                    if date and date <= datetime.now(timezone.utc):
                        expires += " (expired)"
                    title = f" · {credit['title']}" if credit.get("title") else ""
                    lines.append(f"      Reset #{i}{title}: expires {expires}")
                missing = max(0, available - len(credits)) if available is not None else None
                if missing or ("credits" not in v and available != 0):
                    note = f"{missing} reset(s)" if missing else "resets"
                    lines.append(f"      Expiry details unavailable for {note}; refresh to retry.")
                continue
            lines.append(f"    {_c(k + ':', '2', color)} {v}")
        credit = credit_line(u.credit, name=u.account, arrow="↻" if _can_print(stream, "↻") else "->")
        if credit:
            lines.append("    " + credit)
        lines.append("")
    return "\n".join(lines)
