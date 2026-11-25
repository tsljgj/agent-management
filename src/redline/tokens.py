"""How many tokens all accounts have used, counted from the CLIs' own transcripts.

Claude Code: every assistant line in <home>/projects/**/*.jsonl carries message.usage. A streamed
  reply is written as several lines with the same usage, and a resumed session copies earlier
  messages into its new file, so each (message.id, requestId) is counted once across all files.
Codex: <home>/sessions/**/rollout-*.jsonl has token_count events with the session's running
  total; the growth between two events is attributed to the day of the later one.

Claude Code deletes transcripts older than cleanupPeriodDays (30 by default), so a scan alone
forgets old days. Every scan is merged into a ledger ($REDLINE_HOME/tokens.json) that keeps, for
each day, provider and model, the largest count ever seen: the total only grows.

export() is the public summary (no account names, emails, paths or prompts): totals, per
provider and model, and per-day counts. publish() writes it to a GitHub repo with the gh CLI.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .config import DEFAULT_HOMES, load_accounts, redline_home

KINDS = ("input", "output", "cache_write", "cache_read")
SCHEMA = 1


def ledger_path() -> Path:
    return redline_home() / "tokens.json"


def _empty() -> dict:
    return {k: 0 for k in KINDS}


def _total(c: dict) -> int:
    return sum(int(c.get(k, 0)) for k in KINDS)


def _day(ts: str | None) -> str | None:
    """A transcript timestamp (UTC ISO) as the local calendar day."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().date().isoformat()


def _add(days: dict, day: str, provider: str, model: str, counts: dict) -> None:
    slot = days.setdefault(day, {}).setdefault(provider, {}).setdefault(model or "unknown", _empty())
    for k in KINDS:
        slot[k] += int(counts.get(k) or 0)


def _jsonl(root: Path):
    if not root.is_dir():
        return
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if name.endswith(".jsonl"):
                yield Path(dirpath) / name


def scan_claude(home: Path, days: dict, seen: set) -> None:
    for path in _jsonl(home / "projects"):
        try:
            f = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with f:
            for line in f:
                if '"usage"' not in line or '"assistant"' not in line:
                    continue
                try:
                    doc = json.loads(line)
                except ValueError:
                    continue
                msg = doc.get("message")
                if doc.get("type") != "assistant" or not isinstance(msg, dict):
                    continue
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue
                key = (msg.get("id"), doc.get("requestId"))
                if key != (None, None):
                    if key in seen:
                        continue
                    seen.add(key)
                day = _day(doc.get("timestamp"))
                model = msg.get("model") or "unknown"
                if not day or model == "<synthetic>":
                    continue
                _add(days, day, "claude", model, {
                    "input": usage.get("input_tokens"),
                    "output": usage.get("output_tokens"),
                    "cache_write": usage.get("cache_creation_input_tokens"),
                    "cache_read": usage.get("cache_read_input_tokens"),
                })


def scan_codex(home: Path, days: dict, seen: set) -> None:
    for path in _jsonl(home / "sessions"):
        if path.name in seen:  # the same rollout copied into another home
            continue
        seen.add(path.name)
        try:
            f = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        model, prev = "unknown", None
        with f:
            for line in f:
                if '"turn_context"' not in line and '"token_count"' not in line:
                    continue
                try:
                    doc = json.loads(line)
                except ValueError:
                    continue
                payload = doc.get("payload") or {}
                if doc.get("type") == "turn_context":
                    model = payload.get("model") or model
                    continue
                if payload.get("type") != "token_count":
                    continue
                total = (payload.get("info") or {}).get("total_token_usage")
                day = _day(doc.get("timestamp"))
                if not isinstance(total, dict) or not day:
                    continue
                now = {
                    "cache_read": int(total.get("cached_input_tokens") or 0),
                    "output": int(total.get("output_tokens") or 0),
                }
                now["input"] = max(0, int(total.get("input_tokens") or 0) - now["cache_read"])
                before = prev or {k: 0 for k in now}
                delta = {k: max(0, now[k] - before[k]) for k in now}
                prev = now
                if any(delta.values()):
                    _add(days, day, "codex", model, delta)


def homes() -> dict[str, list[Path]]:
    """Every directory that may hold transcripts: each account's home and the CLIs' defaults."""
    out: dict[str, list[Path]] = {p: [Path(h).expanduser()] for p, h in DEFAULT_HOMES.items()}
    try:
        accounts = load_accounts()
    except Exception:
        accounts = []
    for a in accounts:
        if a.provider in out:
            out[a.provider].append(a.home_path)
    for p, dirs in out.items():
        uniq: dict[str, Path] = {}
        for d in dirs:
            try:
                uniq.setdefault(str(d.resolve()), d)
            except OSError:
                uniq.setdefault(str(d), d)
        out[p] = list(uniq.values())
    return out


def scan(dirs: dict[str, list[Path]] | None = None) -> dict:
    """{day: {provider: {model: {kind: n}}}} from the transcripts on disk now."""
    dirs = dirs if dirs is not None else homes()
    days: dict = {}
    seen_claude: set = set()
    seen_codex: set = set()
    for home in dirs.get("claude", []):
        scan_claude(home, days, seen_claude)
    for home in dirs.get("codex", []):
        scan_codex(home, days, seen_codex)
    return days


def load_ledger() -> dict:
    try:
        data = json.loads(ledger_path().read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("days"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"schema": SCHEMA, "days": {}}


def merge(ledger: dict, scanned: dict) -> dict:
    """Keep the larger count per (day, provider, model): deleted transcripts never lower a day."""
    days = ledger.setdefault("days", {})
    for day, provs in scanned.items():
        for prov, models in provs.items():
            for model, counts in models.items():
                slot = days.setdefault(day, {}).setdefault(prov, {})
                old = slot.get(model)
                if old is None or _total(counts) >= _total(old):
                    slot[model] = dict(counts)
    ledger["schema"] = SCHEMA
    return ledger


def update(dirs: dict[str, list[Path]] | None = None) -> dict:
    ledger = merge(load_ledger(), scan(dirs))
    ledger["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(ledger, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return ledger


def _streaks(active: list[str], today: date) -> tuple[int, int]:
    """(current, longest) runs of consecutive active days; current may end today or yesterday."""
    ds = sorted({date.fromisoformat(d) for d in active})
    longest = run = 0
    prev = None
    for d in ds:
        run = run + 1 if prev and d - prev == timedelta(days=1) else 1
        longest = max(longest, run)
        prev = d
    current = 0
    if ds and today - ds[-1] <= timedelta(days=1):
        current, d = 0, ds[-1]
        s = set(ds)
        while d in s:
            current += 1
            d -= timedelta(days=1)
    return current, longest


def export(ledger: dict, today: date | None = None, daily_days: int = 365) -> dict:
    """The public summary. Nothing in it names an account, a project or a prompt."""
    today = today or date.today()
    days = ledger.get("days", {})
    totals = _empty()
    by_provider: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    per_day: dict[str, int] = {}
    for day, provs in days.items():
        for prov, models in provs.items():
            for model, counts in models.items():
                for bucket in (totals, by_provider.setdefault(prov, _empty()),
                               by_model.setdefault(model, _empty())):
                    for k in KINDS:
                        bucket[k] += int(counts.get(k, 0))
                per_day[day] = per_day.get(day, 0) + _total(counts)
    active = [d for d, n in per_day.items() if n > 0]
    current, longest = _streaks(active, today)
    start = today - timedelta(days=daily_days - 1)
    daily = [{"date": (start + timedelta(days=i)).isoformat(),
              "tokens": per_day.get((start + timedelta(days=i)).isoformat(), 0)}
             for i in range(daily_days)]
    with_total = lambda d: {**d, "total": _total(d)}  # noqa: E731
    models = sorted(by_model.items(), key=lambda kv: -_total(kv[1]))
    return {
        "schema": SCHEMA,
        "source": "redline",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "first_day": min(active) if active else None,
        "active_days": len(active),
        "streak": {"current": current, "longest": longest},
        "totals": with_total(totals),
        "by_provider": {p: with_total(c) for p, c in sorted(by_provider.items())},
        "by_model": [{"model": m, **with_total(c)} for m, c in models],
        "daily": daily,
    }


def human(n: int) -> str:
    for unit, size in (("T", 10**12), ("B", 10**9), ("M", 10**6), ("K", 10**3)):
        if n >= size:
            v = n / size
            return f"{v:.2f}{unit}" if v < 10 else f"{v:.1f}{unit}" if v < 100 else f"{v:.0f}{unit}"
    return str(n)


def render(summary: dict) -> str:
    t = summary["totals"]
    lines = [
        f"TOKENS  {human(t['total'])}  ({t['total']:,})",
        f"  input {human(t['input'])} · output {human(t['output'])} · "
        f"cache write {human(t['cache_write'])} · cache read {human(t['cache_read'])}",
        f"  since {summary['first_day'] or '-'} · {summary['active_days']} active days · "
        f"streak {summary['streak']['current']}d (longest {summary['streak']['longest']}d)",
    ]
    for prov, c in summary["by_provider"].items():
        lines.append(f"  {prov:<7}{human(c['total']):>8}")
    for m in summary["by_model"][:6]:
        lines.append(f"    {m['model']:<32}{human(m['total']):>8}")
    return "\n".join(lines) + "\n"


def publish(summary: dict, repo: str, path: str = "stats/tokens.json", gh: str | None = None) -> str:
    """Commit the summary to <repo>:<path> through the gh CLI (uses its login; no token stored here)."""
    gh = gh or shutil.which("gh")
    if not gh:
        raise RuntimeError("publishing needs the GitHub CLI (gh), signed in: https://cli.github.com")
    api = f"repos/{repo}/contents/{path}"
    sha = None
    r = subprocess.run([gh, "api", api, "--jq", ".sha"], capture_output=True, text=True)
    if r.returncode == 0:
        sha = r.stdout.strip() or None
    body = {
        "message": f"tokens: {human(summary['totals']['total'])}",
        "content": base64.b64encode(json.dumps(summary, indent=1).encode()).decode(),
    }
    if sha:
        body["sha"] = sha
    r = subprocess.run([gh, "api", "-X", "PUT", api, "--input", "-"],
                       input=json.dumps(body), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or "gh api failed")
    return f"https://github.com/{repo}/blob/HEAD/{path}"
