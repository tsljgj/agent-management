import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from redline import slots
from redline.config import Account, save_accounts
from redline.models import ProviderError, Usage, Window
from redline.monitor import Monitor
from redline.providers import codex, fetch_usage, load_identity_key


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("REDLINE_HOME", str(tmp_path / "redline"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    return home


def jwt(claims):
    part = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"e30.{part}.sig"


def auth(home, account_id="account-a", email="a@example.test", access_claims=None, id_claims=None):
    home.mkdir(parents=True, exist_ok=True)
    tokens = {"id_token": jwt(id_claims if id_claims is not None else {"email": email}),
              "access_token": jwt(access_claims or {"exp": 2_000_000_000}), "refresh_token": "refresh-token"}
    if account_id is not None:
        tokens["account_id"] = account_id
    (home / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": None, "tokens": tokens}), encoding="utf-8")


def good(account, refresh_tokens=False):
    return Usage(account.name, account.provider, True, email="a@example.test", plan="pro",
                 windows=[Window("7d", 42, datetime.now(timezone.utc) + timedelta(days=2))])


def bad(account, refresh_tokens=False):
    raise ProviderError("network unavailable")


def account(home):
    return Account("display-name", "codex", str(home / "login"))


def test_new_login_under_same_name_cannot_show_previous_identity_quota(isolated_home, monkeypatch):
    a = account(isolated_home)
    auth(a.home_path)
    monkeypatch.setattr(codex, "fetch_usage", good)
    previous = fetch_usage(a)
    assert previous.ok and previous.identity_key
    m = Monitor(collect=lambda *args, **kwargs: [])
    m._with_last_good(previous)
    auth(a.home_path, "account-b", "b@example.test")
    monkeypatch.setattr(codex, "fetch_usage", bad)
    failed = fetch_usage(a)
    shown = m._with_last_good(failed)
    assert failed.identity_key != previous.identity_key
    assert shown is failed and not shown.stale and not shown.windows
    assert shown.email is None and shown.plan is None


def test_rotated_tokens_with_same_identity_can_reuse_cached_quota(isolated_home, monkeypatch):
    a = account(isolated_home)
    auth(a.home_path)
    monkeypatch.setattr(codex, "fetch_usage", good)
    previous = fetch_usage(a)
    m = Monitor(collect=lambda *args, **kwargs: [])
    m._with_last_good(previous)
    auth(a.home_path, access_claims={"exp": 2_000_010_000, "refresh-generation": 2})
    monkeypatch.setattr(codex, "fetch_usage", bad)
    failed = fetch_usage(a)
    shown = m._with_last_good(failed)
    assert previous.identity_key == failed.identity_key == shown.identity_key
    assert shown.stale and shown.windows[0].used_percent == 42


def test_effective_default_slot_supplies_identity(isolated_home, monkeypatch):
    a = account(isolated_home)
    auth(a.home_path)
    save_accounts([a])
    before = load_identity_key(a)
    slots.activate(a.key)
    assert not (a.home_path / "auth.json").exists()
    assert load_identity_key(a) == before
    monkeypatch.setattr(codex, "fetch_usage", bad)
    assert fetch_usage(a).identity_key == before


def test_legacy_codex_cache_without_identity_is_not_reused(isolated_home):
    previous = good(account(isolated_home))
    m = Monitor(collect=lambda *args, **kwargs: [])
    m._with_last_good(previous)
    m._save_last_ok()
    restarted = Monitor(collect=lambda *args, **kwargs: [])
    failed = Usage(previous.account, "codex", False, error="network unavailable", identity_key="known-identity")
    assert restarted._with_last_good(failed) is failed
    previous.identity_key = "known-identity"
    restarted._with_last_good(previous)
    failed.identity_key = None
    assert restarted._with_last_good(failed) is failed


def test_same_identity_survives_cache_round_trip(isolated_home):
    a = account(isolated_home)
    auth(a.home_path)
    previous = good(a)
    previous.identity_key = load_identity_key(a)
    m = Monitor(collect=lambda *args, **kwargs: [])
    m._with_last_good(previous)
    m._save_last_ok()
    restarted = Monitor(collect=lambda *args, **kwargs: [])
    failed = Usage(a.name, "codex", False, error="offline", identity_key=load_identity_key(a))
    shown = restarted._with_last_good(failed)
    assert shown.stale and shown.identity_key == previous.identity_key


def test_claude_cache_behavior_stays_unchanged(isolated_home):
    m = Monitor(collect=lambda *args, **kwargs: [])
    previous = Usage("claude", "claude", True, windows=[Window("5h", 35)])
    m._with_last_good(previous)
    shown = m._with_last_good(Usage("claude", "claude", False, error="offline"))
    assert shown.stale and shown.windows[0].used_percent == 35
    assert "identity_key" not in shown.to_dict()


def test_fallback_identity_uses_stable_jwt_account_or_subject_or_email(isolated_home):
    a = account(isolated_home)
    auth(a.home_path, account_id=None,
         id_claims={codex.AUTH_CLAIM: {"chatgpt_account_id": "jwt-account"}, "email": "a@example.test"})
    account_key = load_identity_key(a)
    auth(a.home_path, "jwt-account")
    assert load_identity_key(a) == account_key
    auth(a.home_path, account_id=None, id_claims={"sub": "subject-a", "email": "a@example.test"})
    subject_key = load_identity_key(a)
    auth(a.home_path, account_id=None, id_claims={"sub": "subject-a", "email": "new@example.test"})
    assert load_identity_key(a) == subject_key
    auth(a.home_path, account_id=None, email="A@EXAMPLE.TEST")
    email_key = load_identity_key(a)
    auth(a.home_path, account_id=None, email="a@example.test", access_claims={"exp": 2_000_010_000})
    assert load_identity_key(a) == email_key
    assert email_key != subject_key != account_key
    assert len(email_key) == 64 and "example" not in email_key


def test_api_key_identity_is_separate_and_does_not_expose_key(isolated_home, monkeypatch):
    a = account(isolated_home)
    auth(a.home_path)
    chatgpt_key = load_identity_key(a)
    (a.home_path / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "fake-api-secret"}), encoding="utf-8")
    monkeypatch.setattr(codex, "fetch_usage", bad)
    failed = fetch_usage(a)
    assert failed.identity_key and failed.identity_key != chatgpt_key
    assert "fake-api-secret" not in json.dumps(failed.to_dict())


def test_login_changed_during_fetch_does_not_attach_old_quota_to_new_identity(isolated_home, monkeypatch):
    a = account(isolated_home)
    auth(a.home_path)

    def change_login(account, refresh_tokens=False):
        auth(account.home_path, "account-b", "b@example.test")
        return good(account)

    monkeypatch.setattr(codex, "fetch_usage", change_login)
    fetched = fetch_usage(a)
    assert not fetched.ok and not fetched.windows and fetched.email is None
    assert fetched.identity_key == load_identity_key(a)
    assert "login changed" in fetched.error
