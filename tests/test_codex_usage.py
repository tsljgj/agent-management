"""Codex usage variants returned for Free plans and banked usage resets."""

import base64
import json
from datetime import datetime, timezone

import pytest

from redline.config import Account
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
