"""Gemini CLI quota: read-only, from the Gemini CLI's own login; the credentials file is never written."""

from __future__ import annotations

import json
import time

import pytest

from redline import cli
from redline.http import HTTPStatusError
from redline.providers import gemini


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("REDLINE_HOME", str(tmp_path / "rl"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    for k in ("GEMINI_OAUTH_CLIENT_ID", "GEMINI_OAUTH_CLIENT_SECRET", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_PROJECT_ID"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / "home").mkdir()
    gemini._tokens.clear(); gemini._tiers.clear(); gemini._client.clear()
    return tmp_path


def _login(tmp_path, email="me@gmail.com", expires_in=3600):
    home = tmp_path / "home" / ".gemini"
    home.mkdir(parents=True)
    creds = {"access_token": "ya29.old", "refresh_token": "1//rt", "token_type": "Bearer",
             "expiry_date": int((time.time() + expires_in) * 1000)}
    (home / "oauth_creds.json").write_text(json.dumps(creds))
    (home / "google_accounts.json").write_text(json.dumps({"active": email, "old": []}))
    return home


QUOTA = {"buckets": [
    {"modelId": "gemini-2.5-pro", "tokenType": "REQUESTS", "remainingFraction": 0.6, "resetTime": "2026-11-26T08:00:00Z"},
    {"modelId": "gemini-3-pro-preview", "tokenType": "REQUESTS", "remainingFraction": 0.25, "resetTime": "2026-11-26T09:00:00Z"},
    {"modelId": "gemini-2.5-flash", "tokenType": "REQUESTS", "remainingFraction": 0.95},
    {"modelId": "gemini-2.5-flash-lite", "remainingFraction": 1},
    {"modelId": "bad", "remainingFraction": "x"},
]}
LOAD = {"currentTier": {"id": "standard-tier"}, "paidTier": {"id": "g1-pro-tier", "name": "Gemini Code Assist in Google One AI Pro"},
        "cloudaicompanionProject": "proj-123"}


def test_quota_is_one_window_per_family_with_the_least_left_and_models_rounded_down():
    windows, extra = gemini.parse_quota(QUOTA)
    assert [(w.name, round(w.used_percent, 2)) for w in windows] == [("pro", 75.0), ("flash", 5.0), ("flash-lite", 0.0)]
    assert windows[0].resets_at.isoformat().startswith("2026-11-26T09:00")
    assert extra["models"]["gemini-2.5-pro"] == "60% left" and extra["models"]["gemini-3-pro-preview"] == "25% left"
    assert "bad" not in extra["models"]
    assert gemini.parse_quota({}) == ([], {}) and gemini.parse_quota(None) == ([], {})


def test_plan_prefers_the_paid_tier_and_falls_back_to_its_name():
    assert gemini.plan_of(LOAD) == "ai pro"
    assert gemini.plan_of({"currentTier": {"id": "free-tier"}}) == "free"
    assert gemini.plan_of({"currentTier": {"id": "x-tier", "name": "Some Tier"}}) == "some tier"
    assert gemini.plan_of({}) is None
    assert gemini.project_of(LOAD) == "proj-123" and gemini.project_of({"cloudaicompanionProject": {"id": "p2"}}) == "p2"


def test_fetch_reads_tier_and_quota_with_the_stored_token(tmp_path, monkeypatch):
    home = _login(tmp_path)
    calls = []

    def fake(method, url, headers=None, json_body=None, form_body=None):
        calls.append((url, headers["Authorization"], json_body))
        return LOAD if url == gemini.LOAD_URL else QUOTA

    monkeypatch.setattr(gemini, "request_json", fake)
    u = gemini.fetch_usage(home, "me")
    assert u.ok and u.provider == "gemini" and u.email == "me@gmail.com" and u.plan == "ai pro"
    assert [w.name for w in u.windows] == ["pro", "flash", "flash-lite"] and u.extra["project"] == "proj-123"
    assert calls[1] == (gemini.QUOTA_URL, "Bearer ya29.old", {"project": "proj-123"})
    gemini.fetch_usage(home, "me")
    assert [c[0] for c in calls].count(gemini.LOAD_URL) == 1   # the tier is cached


def test_an_expired_token_is_renewed_in_memory_only(tmp_path, monkeypatch):
    home = _login(tmp_path, expires_in=-60)
    before = (home / "oauth_creds.json").read_text()
    monkeypatch.setenv("GEMINI_OAUTH_CLIENT_ID", "id.apps.googleusercontent.com")
    monkeypatch.setenv("GEMINI_OAUTH_CLIENT_SECRET", "sec")
    seen = []

    def fake(method, url, headers=None, json_body=None, form_body=None):
        if url == gemini.TOKEN_URL:
            seen.append(form_body)
            return {"access_token": "ya29.new", "expires_in": 3599}
        assert headers["Authorization"] == "Bearer ya29.new"
        return LOAD if url == gemini.LOAD_URL else QUOTA

    monkeypatch.setattr(gemini, "request_json", fake)
    assert gemini.fetch_usage(home, "me").ok
    assert gemini.fetch_usage(home, "me").ok
    assert len(seen) == 1 and seen[0]["grant_type"] == "refresh_token" and seen[0]["client_id"].startswith("id.")
    assert (home / "oauth_creds.json").read_text() == before   # never written back


def test_failures_say_what_to_do_and_never_raise(tmp_path, monkeypatch):
    home = _login(tmp_path, expires_in=-60)
    monkeypatch.setattr(gemini, "_cli_files", lambda: [])
    monkeypatch.setattr(gemini, "request_json", lambda *a, **k: pytest.fail("no network without a client"))
    u = gemini.fetch_usage(home, "me")
    assert not u.ok and "run `gemini`" in u.error and u.email == "me@gmail.com"

    monkeypatch.setenv("GEMINI_OAUTH_CLIENT_ID", "id.apps.googleusercontent.com")
    monkeypatch.setenv("GEMINI_OAUTH_CLIENT_SECRET", "sec")

    def revoked(*a, **k):
        raise HTTPStatusError(400, '{"error": "invalid_grant"}', gemini.TOKEN_URL)

    monkeypatch.setattr(gemini, "request_json", revoked)
    assert "log in again" in gemini.fetch_usage(home, "me").error

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(gemini, "request_json", boom)
    assert not gemini.fetch_usage(home, "me").ok
    assert not gemini.fetch_usage(tmp_path / "nowhere", "x").ok


def test_the_client_is_read_from_the_installed_cli(tmp_path, monkeypatch):
    bundle = tmp_path / "gemini.js"
    bundle.write_bytes(b'var OAUTH_CLIENT_ID = "123-abc.apps.googleusercontent.com";\nvar OAUTH_CLIENT_SECRET = "GOCSPX-xyz";')
    monkeypatch.setattr(gemini, "_cli_files", lambda: [bundle])
    assert gemini.oauth_client() == ("123-abc.apps.googleusercontent.com", "GOCSPX-xyz")


def test_collect_includes_gemini_logins_named_by_the_address(tmp_path, monkeypatch):
    _login(tmp_path, email="alice@gmail.com")
    monkeypatch.setattr(gemini, "request_json", lambda m, url, **k: LOAD if url == gemini.LOAD_URL else QUOTA)
    out = cli.collect([])
    assert [(u.provider, u.account) for u in out] == [("gemini", "alice")]
    assert cli.collect([], gemini=False) == []
    assert gemini.name_for(tmp_path, "alice@gmail.com", {"alice"}) == "alice-2"
