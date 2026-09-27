"""Terminal rendering of usage snapshots."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from .models import Usage

BAR_WIDTH = 20


def _color_enabled(stream) -> bool:
    return stream.isatty() and os.environ.get("NO_COLOR") is None


def _c(text: str, code: str, on: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if on else text


def bar(percent: float | None, width: int = BAR_WIDTH) -> str:
    if percent is None:
        return "?" * width
    filled = round(max(0.0, min(100.0, percent)) / 100 * width)
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
            pct = "  ?%" if w.used_percent is None else f"{w.used_percent:3.0f}%"
            code = _pct_color(w.used_percent)
            lines.append(
                f"    {w.name:<10} {_c(bar(w.used_percent), code, color)} "
                f"{_c(pct, code, color)}  {humanize_delta(w.resets_at)}"
            )
        for k, v in u.extra.items():
            lines.append(f"    {_c(k + ':', '2', color)} {v}")
        lines.append("")
    return "\n".join(lines)
