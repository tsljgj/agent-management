import base64
import json
import sys
import time
from datetime import datetime, timezone

import pytest

from agentman import cli
from agentman.config import Account, load_accounts
from agentman.providers import claude, codex, fetch_usage


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTMAN_HOME", str(tmp_path / "am"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    (tmp_path / "home").mkdir()
    return tmp_path


def _jwt(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


def _claude_account(tmp_path, name="c1", expires_in=3600):
    home = tmp_path / name
    home.mkdir()
    (home / ".credentials.json").write_text(json.dumps({
        "claudeAiOauth": {
            "accessToken": "at-old", "refreshToken": "rt-old",
            "expiresAt": int((time.time() + expires_in) * 1000),
            "scopes": ["user:inference", "user:profile"],
            "subscriptionType": "max", "rateLimitTier": "default_claude_max_20x",
        },
        "mcpOAuth": {"keep": "me"},
    }))
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": f"{name}@x.com"}}))
    return Account(name=name, provider="claude", home=str(home))


CLAUDE_USAGE = {
    "five_hour": {"utilization": 42.0, "resets_at": "2030-01-01T10:00:00.123Z"},
    "seven_day": {"utilization": 12.5, "resets_at": "2030-01-05T00:00:00Z"},
    "seven_day_opus": None,
    "extra_usage": {"is_enabled": True, "monthly_limit": 5000, "used_credits": 1234, "currency": "USD"},
}

CODEX_USAGE = {
    "plan_type": "pro",
    "rate_limit": {
        "allowed": True, "limit_reached": False,
        "primary_window": {"used_percent": 30, "limit_window_seconds": 18000, "reset_after_seconds": 100, "reset_at": 1893456000},
        "secondary_window": {"used_percent": 70, "limit_window_seconds": 604800, "reset_after_seconds": 100, "reset_at": 1893900000},
    },
    "credits": {"has_credits": True, "unlimited": False, "balance": "12.5"},
}


def test_claude_parse_usage():
    windows, extra = claude.parse_usage(CLAUDE_USAGE)
    assert [(w.name, w.used_percent) for w in windows] == [("5h", 42.0), ("7d", 12.5)]
    assert windows[0].resets_at == datetime(2030, 1, 1, 10, 0, 0, 123000, tzinfo=timezone.utc)
    assert extra == {"extra usage": "12.34 / 50.00 USD"}


def test_codex_parse_usage():
    windows, extra = codex.parse_usage(CODEX_USAGE)
    assert [(w.name, w.used_percent) for w in windows] == [("5h", 30.0), ("7d", 70.0)]
    assert windows[1].resets_at == datetime.fromtimestamp(1893900000, tz=timezone.utc)
    assert extra == {"credits": "12.5"}


def test_keychain_service_name(tmp_path):
    default = Account("d", "claude", "~/.claude")
    assert claude.keychain_service(default) == "Claude Code-credentials"
    custom = Account("w", "claude", "/Users/me/.claude-work")
    svc = claude.keychain_service(custom)
    assert svc.startswith("Claude Code-credentials-") and len(svc) == len("Claude Code-credentials-") + 8


def test_claude_fetch(tmp_path, monkeypatch):
    acct = _claude_account(tmp_path)
    seen = {}

    def fake(method, url, headers=None, **kw):
        seen.update(headers)
        assert url == claude.USAGE_URL
        return CLAUDE_USAGE

    monkeypatch.setattr(claude, "request_json", fake)
    u = fetch_usage(acct)
    assert u.ok and u.email == "c1@x.com" and u.plan == "max 20x"
    assert seen["anthropic-beta"] == "oauth-2025-04-20" and seen["Authorization"] == "Bearer at-old"


def test_claude_expired_without_refresh_does_not_touch_tokens(tmp_path, monkeypatch):
    acct = _claude_account(tmp_path, expires_in=-10)
    monkeypatch.setattr(claude, "request_json", lambda *a, **k: pytest.fail("no network expected"))
    u = fetch_usage(acct)
    assert not u.ok and "expired" in u.error


def test_claude_refresh_writes_back_rotated_tokens(tmp_path, monkeypatch):
    acct = _claude_account(tmp_path, expires_in=-10)

    def fake(method, url, headers=None, json_body=None, **kw):
        if url == claude.TOKEN_URL:
            assert json_body["refresh_token"] == "rt-old" and json_body["client_id"] == claude.CLIENT_ID
            return {"access_token": "at-new", "refresh_token": "rt-new", "expires_in": 3600}
        assert headers["Authorization"] == "Bearer at-new"
        return CLAUDE_USAGE

    monkeypatch.setattr(claude, "request_json", fake)
    u = fetch_usage(acct, refresh_tokens=True)
    assert u.ok
    saved = json.loads((acct.home_path / ".credentials.json").read_text())
    assert saved["claudeAiOauth"]["refreshToken"] == "rt-new"
    assert saved["claudeAiOauth"]["subscriptionType"] == "max"
    assert saved["mcpOAuth"] == {"keep": "me"}
    if sys.platform != "win32":
        assert (acct.home_path / ".credentials.json").stat().st_mode & 0o777 == 0o600


def test_codex_fetch(tmp_path, monkeypatch):
    home = tmp_path / "cx"
    home.mkdir()
    id_token = _jwt({"email": "me@openai.test", "https://api.openai.com/auth": {"chatgpt_plan_type": "plus", "chatgpt_account_id": "acc-1"}})
    (home / "auth.json").write_text(json.dumps({
        "OPENAI_API_KEY": None,
        "tokens": {"id_token": id_token, "access_token": _jwt({"exp": time.time() + 3600}), "refresh_token": "r", "account_id": "acc-1"},
    }))
    seen = {}
    monkeypatch.setattr(codex, "request_json", lambda m, url, headers=None, **k: seen.update(headers) or CODEX_USAGE)
    u = fetch_usage(Account("cx", "codex", str(home)))
    assert u.ok and u.email == "me@openai.test" and u.plan == "pro"
    assert seen["ChatGPT-Account-Id"] == "acc-1"


def test_missing_credentials_is_reported(tmp_path):
    u = fetch_usage(Account("nope", "codex", str(tmp_path / "missing")))
    assert not u.ok and "not logged in" in u.error


def test_cli_add_list_usage_json(tmp_path, monkeypatch, capsys):
    assert cli.main(["add", "claude", "work", "--note", "work acct"]) == 0
    acct = load_accounts()[0]
    assert acct.home_path.is_dir() and acct.env() == {"CLAUDE_CONFIG_DIR": str(acct.home_path)}
    assert cli.main(["add", "claude", "work"]) == 1  # duplicate

    src = _claude_account(tmp_path, "src")
    (acct.home_path / ".credentials.json").write_text((src.home_path / ".credentials.json").read_text())
    monkeypatch.setattr(claude, "request_json", lambda *a, **k: CLAUDE_USAGE)
    capsys.readouterr()
    assert cli.main(["usage", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out[0]["account"] == "work" and out[0]["windows"][0]["used_percent"] == 42.0

    assert cli.main(["usage"]) == 0
    text = capsys.readouterr().out
    assert "work" in text and "42%" in text and "resets in" in text

    assert cli.main(["env", "work"]) == 0
    assert "export CLAUDE_CONFIG_DIR=" in capsys.readouterr().out
    assert cli.main(["rm", "work"]) == 0 and load_accounts() == []


def test_cli_import_default_homes(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    (home / ".codex").mkdir()
    (home / ".codex" / "auth.json").write_text(json.dumps({"tokens": {"access_token": "x"}}))
    assert cli.main(["import"]) == 0
    assert [a.name for a in load_accounts()] == ["codex-default"]
