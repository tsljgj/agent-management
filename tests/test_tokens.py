import json
from datetime import date

from redline import tokens


def _claude_line(mid, req, ts, model="claude-opus-4-5", i=10, o=5, cw=100, cr=1000):
    return json.dumps({
        "type": "assistant", "requestId": req, "timestamp": ts,
        "message": {"id": mid, "model": model, "usage": {
            "input_tokens": i, "output_tokens": o,
            "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr}},
    })


def _write(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_claude_dedup_across_lines_and_files(tmp_path):
    home = tmp_path / "c1"
    line = _claude_line("m1", "r1", "2026-11-20T12:00:00Z")
    _write(home / "projects" / "p" / "a.jsonl", [line, line, '{"type":"user"}'])
    # a resumed session copies the message into a new file; a second account home has it too
    _write(home / "projects" / "p" / "b.jsonl", [line, _claude_line("m2", "r2", "2026-11-21T12:00:00Z")])
    _write(tmp_path / "c2" / "projects" / "q" / "x.jsonl", [line])
    days = tokens.scan({"claude": [home, tmp_path / "c2"]})
    total = sum(tokens._total(c) for d in days.values() for m in d["claude"].values() for c in [m])
    assert total == 2 * (10 + 5 + 100 + 1000)


def test_codex_running_totals_become_deltas(tmp_path):
    home = tmp_path / "x"
    def tc(ts, inp, cached, out):
        return json.dumps({"timestamp": ts, "type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out}}}})
    _write(home / "sessions" / "2026" / "11" / "20" / "rollout-1.jsonl", [
        json.dumps({"type": "turn_context", "payload": {"model": "gpt-5-codex"}}),
        tc("2026-11-20T10:00:00Z", 100, 40, 10),
        tc("2026-11-20T10:00:01Z", 100, 40, 10),  # repeated event: no growth
        tc("2026-11-20T11:00:00Z", 300, 140, 30),
    ])
    days = tokens.scan({"codex": [home]})
    (day,) = days
    c = days[day]["codex"]["gpt-5-codex"]
    assert c == {"input": 160, "output": 30, "cache_write": 0, "cache_read": 140}


def test_ledger_keeps_days_whose_transcripts_were_deleted(tmp_path):
    home = tmp_path / "c"
    f = home / "projects" / "p" / "a.jsonl"
    _write(f, [_claude_line("m1", "r1", "2026-10-01T12:00:00Z")])
    tokens.update({"claude": [home]})
    f.unlink()
    _write(home / "projects" / "p" / "b.jsonl", [_claude_line("m2", "r2", "2026-11-01T12:00:00Z")])
    ledger = tokens.update({"claude": [home]})
    s = tokens.export(ledger, today=date(2026, 11, 2))
    assert s["totals"]["total"] == 2 * 1115
    assert s["active_days"] == 2
    assert s["streak"] == {"current": 1, "longest": 1}


def test_export_has_no_private_fields(tmp_path):
    home = tmp_path / "secret-user@example.com"
    _write(home / "projects" / "-home-me-private-repo" / "a.jsonl", [_claude_line("m1", "r1", "2026-11-20T12:00:00Z")])
    s = tokens.export(tokens.update({"claude": [home]}), today=date(2026, 11, 21), daily_days=7)
    text = json.dumps(s)
    assert "example.com" not in text and "private-repo" not in text
    assert len(s["daily"]) == 7 and sum(d["tokens"] for d in s["daily"]) == 1115


def test_human():
    assert tokens.human(999) == "999"
    assert tokens.human(1_234_567) == "1.23M"
    assert tokens.human(45_600_000_000) == "45.6B"
