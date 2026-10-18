"""Codex usage variants returned for Free plans and banked usage resets."""

import base64
import json
from datetime import datetime, timezone

import pytest

from redline.config import Account
from redline.http import HTTPStatusError
from redline.models import ProviderError, Usage
from redline.providers import codex


def _jwt(claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"e30.{payload}.signature"


def test_free_account_primary_weekly_window_and_plan(tmp_path, monkeypatch):
    account = Account("free", "codex", str(tmp_path))
    auth = {"tokens": {
        "access_token": _jwt({}),
        "id_token": _jwt({"email": "free@example.com", codex.AUTH_CLAIM: {"chatgpt_plan_type": "plus"}}),
    }}
    payload = {
        "plan_type": "free",
        "rate_limit": {
            "allowed": True,
            "limit_reached": False,
            "primary_window": {
                "used_percent": 25,
                "limit_window_seconds": 604800,
                "reset_at": 1893900000,
            },
            "secondary_window": None,
        },
        "additional_rate_limits": None,
        "credits": None,
        "rate_limit_reset_credits": None,
    }
    monkeypatch.setattr(codex, "load_auth", lambda _: auth)
    monkeypatch.setattr(codex, "request_json", lambda *args, **kwargs: payload)
    usage = codex.fetch_usage(account)

    assert usage.ok
    assert usage.plan == "free"
    assert usage.email == "free@example.com"
    assert [(w.name, w.used_percent) for w in usage.windows] == [("7d", 25)]
    assert usage.windows[0].resets_at == datetime.fromtimestamp(1893900000, timezone.utc)
    assert usage.extra == {}


def test_banked_resets_available_even_when_not_currently_applicable():
    windows, extra = codex.parse_usage({
        "rate_limit": None,
        "credits": {"balance": "12.5", "has_credits": True},
        "rate_limit_reset_credits": {"available_count": 3, "applicable_available_count": 0},
    })
    assert windows == []
    assert extra == {"credits": "12.5", "banked_resets": {"available": 3, "applicable": 0}}


@pytest.mark.parametrize(("raw", "expected"), [
    ({"available_count": 0, "applicable_available_count": 0}, {"available": 0, "applicable": 0}),
    ({"available_count": "3", "applicable_available_count": 2.0}, {"available": 3, "applicable": 2}),
    ({"available_count": 2}, {"available": 2}),
    ({"applicable_available_count": 0}, {"applicable": 0}),
    ({"available_count": 3, "applicable_available_count": None}, {"available": 3}),
])
def test_banked_reset_counts_preserve_zero_and_missing_values(raw, expected):
    _, extra = codex.parse_usage({"rate_limit_reset_credits": raw})
    assert extra["banked_resets"] == expected


@pytest.mark.parametrize("raw", [None, {}, [], 3, "3", {
    "available_count": True, "applicable_available_count": False,
}, {
    "available_count": -1, "applicable_available_count": 1.5,
}, {
    "available_count": "NaN", "applicable_available_count": "Infinity",
}])
def test_missing_or_invalid_banked_reset_counts_stay_unknown(raw):
    _, extra = codex.parse_usage({"rate_limit_reset_credits": raw})
    assert "banked_resets" not in extra


@pytest.mark.parametrize("rate_limit", [None, [], "unavailable"])
def test_unavailable_windows_do_not_hide_banked_resets(rate_limit):
    windows, extra = codex.parse_usage({
        "plan_type": "free",
        "rate_limit": rate_limit,
        "rate_limit_reset_credits": {"available_count": 1},
    })
    assert windows == []
    assert extra == {"banked_resets": {"available": 1}}


def test_banked_reset_details_preserve_each_available_credit_and_sort_by_expiry():
    result = codex.banked_resets({"available_count": 5, "credits": [
        {"id": "later", "status": "available", "title": "Monthly reset",
         "granted_at": "2026-10-07T23:23:28.303728Z", "expires_at": "2026-11-06T23:23:28.303728Z"},
        {"id": "unknown", "status": "available", "expires_at": None},
        {"id": "used", "status": "redeemed", "expires_at": "2026-10-01T00:00:00Z"},
        {"id": "earlier", "status": "available", "expires_at": "2026-10-20T10:30:00-04:00"},
        {"id": "expired", "status": "expired"},
        {"id": "pending", "status": "redeeming"},
        None, "bad row",
    ]})
    assert result["available"] == 5  # a capped list must not replace the total
    assert [c["id"] for c in result["credits"]] == ["earlier", "later", "unknown"]
    assert result["credits"][0]["expires_at"] == "2026-10-20T14:30:00+00:00"
    assert result["credits"][1]["expires_at"] == "2026-11-06T23:23:28.303728+00:00"
    assert result["credits"][1]["granted_at"] == "2026-10-07T23:23:28.303728+00:00"
    assert result["credits"][1]["title"] == "Monthly reset"
    assert result["credits"][2]["expires_at"] is None
    # Cache and HTTP payloads retain serializable dates across restarts.
    usage = Usage("work", "codex", True, extra={"banked_resets": result})
    assert Usage.from_dict(json.loads(json.dumps(usage.to_dict()))).extra == usage.extra


@pytest.mark.parametrize("expiry", [None, "", "bad date", {}, True, 0, -1, "NaN", "Infinity", 1e100])
def test_unknown_expiry_is_not_inferred_from_the_grant_date(expiry):
    result = codex.banked_resets({"credits": [{"status": "available",
        "granted_at": "2026-10-07T23:23:28Z", "expires_at": expiry}]})
    assert result["credits"][0]["expires_at"] is None


def test_epoch_expiry_and_empty_details_are_supported():
    result = codex.banked_resets({"credits": [{"status": "available", "expires_at": 1893900000}]})
    assert result["credits"][0]["expires_at"] == datetime.fromtimestamp(1893900000, timezone.utc).isoformat()
    assert codex.banked_resets({"available_count": 0, "credits": []}) == {"available": 0, "credits": []}
    assert codex.banked_resets({"available_count": 2, "credits": None}) == {"available": 2}


def _fetch_with_reset_details(monkeypatch, tmp_path, detail_response, count=3):
    account = Account("resets", "codex", str(tmp_path))
    auth = {"tokens": {"access_token": _jwt({}), "account_id": "test-workspace"}}
    monkeypatch.setattr(codex, "load_auth", lambda _: auth)
    calls = []

    def request(method, url, headers):
        calls.append(url)
        assert method == "GET"  # this feature must never consume a reset
        assert headers["ChatGPT-Account-Id"] == "test-workspace"
        if url == codex.USAGE_URL:
            return {"rate_limit": {"primary_window": {"used_percent": 25, "limit_window_seconds": 18000}},
                    "rate_limit_reset_credits": {"available_count": count, "applicable_available_count": 0}}
        assert url == codex.RESET_CREDITS_URL
        if isinstance(detail_response, Exception):
            raise detail_response
        return detail_response

    monkeypatch.setattr(codex, "request_json", request)
    return codex.fetch_usage(account), calls


def test_fetches_banked_reset_details_even_when_not_applicable(monkeypatch, tmp_path):
    usage, calls = _fetch_with_reset_details(monkeypatch, tmp_path, {
        "available_count": 2, "credits": [{"id": "monthly", "status": "available",
                                          "expires_at": "2026-11-06T23:23:28Z"}],
    })
    assert calls == [codex.USAGE_URL, codex.RESET_CREDITS_URL]
    assert usage.ok and usage.windows[0].used_percent == 25
    resets = usage.extra["banked_resets"]
    assert resets["available"] == 2
    assert resets["applicable"] == 0
    assert resets["credits"][0]["expires_at"] == "2026-11-06T23:23:28+00:00"


@pytest.mark.parametrize("response", [None, [], {}, {"credits": "invalid"},
    ProviderError("timeout"), HTTPStatusError(403, "denied", codex.RESET_CREDITS_URL),
    HTTPStatusError(429, "rate limited", codex.RESET_CREDITS_URL)])
def test_optional_details_failure_keeps_usage_and_counts(monkeypatch, tmp_path, response):
    usage, calls = _fetch_with_reset_details(monkeypatch, tmp_path, response)
    assert calls == [codex.USAGE_URL, codex.RESET_CREDITS_URL]
    assert usage.ok and usage.windows[0].used_percent == 25
    assert usage.extra["banked_resets"] == {"available": 3, "applicable": 0}


def test_zero_resets_does_not_request_details(monkeypatch, tmp_path):
    usage, calls = _fetch_with_reset_details(monkeypatch, tmp_path, None, count=0)
    assert calls == [codex.USAGE_URL]
    assert usage.extra["banked_resets"]["available"] == 0
