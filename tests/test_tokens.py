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


def test_console_command_remembers_repo_and_publishes_daily(tmp_path, monkeypatch):
    from redline import actions
    from redline.config import load_settings

    calls = []
    monkeypatch.setattr(tokens, "homes", lambda: {"claude": [tmp_path / "none"]})
    monkeypatch.setattr(tokens, "publish", lambda summary, repo, path="stats/tokens.json": calls.append(repo) or f"https://github.com/{repo}")
    assert "TOKENS" in actions.dispatch("tokens", {})
    out = actions.dispatch("tokens", {"arg": "publish me/me"})
    assert "published" in out and calls == ["me/me"]
    assert load_settings()["tokens_repo"] == "me/me"
    assert tokens.publish_if_due() is None  # just published
    assert tokens.publish_if_due(now=load_settings()["tokens_published_at"] + tokens.PUBLISH_EVERY + 1)
    assert calls == ["me/me", "me/me"]
    actions.dispatch("tokens", {"arg": "off"})
    assert tokens.publish_if_due(now=10**12) is None


def test_publish_rejects_a_bad_repo():
    import pytest

    with pytest.raises(RuntimeError):
        tokens.publish({"totals": {"total": 1}}, "not-a-repo", gh="gh")


def test_gemini_chats_json_and_jsonl(tmp_path):
    home = tmp_path / "g"
    tok = {"input": 1000, "output": 50, "cached": 600, "thoughts": 20, "tool": 5, "total": 1075}
    msg = {"id": "a", "timestamp": "2026-11-20T12:00:00Z", "type": "gemini", "model": "gemini-3-pro", "tokens": tok}
    # legacy: one JSON object with every message
    chats = home / "tmp" / "proj1" / "chats"
    chats.mkdir(parents=True)
    (chats / "session-1.json").write_text(json.dumps({"sessionId": "s", "messages": [
        {"id": "u", "timestamp": "2026-11-20T11:59:00Z", "type": "user", "content": "hi"}, msg]}))
    # new: appended records; a later record of the same id replaces the earlier one,
    # the same message also reached a resumed file, and a subagent chat sits one level down
    _write(chats / "session-2.jsonl", [
        json.dumps({"sessionId": "t", "projectHash": "h"}),
        json.dumps({**msg, "id": "b", "tokens": None}),
        json.dumps({**msg, "id": "b"}),
        json.dumps({"$set": {"messages": [msg]}}),
    ])
    _write(chats / "parent" / "session-3.jsonl", [json.dumps({**msg, "id": "c"})])
    days = tokens.scan({"gemini": [home]})
    c = days["2026-11-20"]["gemini"]["gemini-3-pro"] if "2026-11-20" in days else next(iter(days.values()))["gemini"]["gemini-3-pro"]
    assert c == {"input": 3 * 405, "output": 3 * 70, "cache_write": 0, "cache_read": 3 * 600}


def test_removed_accounts_still_count(tmp_path, monkeypatch):
    from redline import config

    gone = config.Account(name="old", provider="claude", home=str(tmp_path / "old"))
    monkeypatch.setattr(tokens, "load_accounts", lambda: [])
    monkeypatch.setattr(tokens, "load_removed", lambda: [gone])
    assert any(str(p).endswith("old") for p in tokens.homes()["claude"])
