from datetime import datetime, timedelta, timezone

import pytest

from redline.models import Usage, Window, weekly_countdown
from redline.providers import codex


NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


def usage(window=None, **kwargs):
    return Usage("account", "codex", True, windows=[window] if window else [], **kwargs)


def parsed_window(**fields):
    windows, _ = codex.parse_usage({"rate_limit": {"primary_window": {
        "limit_window_seconds": 604800, "used_percent": 0, **fields,
    }}})
    return windows[0]


@pytest.mark.parametrize("percent", [0, 1, 100, None])
def test_future_reset_is_active_even_with_zero_or_unknown_usage(percent):
    reset = NOW + timedelta(days=7)
    state = weekly_countdown(usage(Window("7d", percent, reset)), NOW)
    assert state["state"] == "active"
    assert state["resets_at"] == reset.isoformat()


@pytest.mark.parametrize("fields", [
    {"reset_at": 0}, {"reset_after_seconds": 0},
    {"reset_at": 0, "reset_after_seconds": 0},
])
def test_explicit_zero_reset_and_zero_usage_mark_unstarted(fields):
    window = parsed_window(**fields)
    assert window.resets_at is None
    assert weekly_countdown(usage(window), NOW)["state"] == "unstarted"


@pytest.mark.parametrize("fields", [
    {}, {"reset_at": None}, {"reset_after_seconds": None},
    {"reset_at": -1}, {"reset_after_seconds": -1},
    {"reset_at": "invalid"}, {"reset_at": False},
    {"reset_at": float("inf")},
])
def test_missing_null_invalid_and_negative_resets_are_unknown(fields):
    window = parsed_window(**fields)
    assert window.resets_at is None
    assert weekly_countdown(usage(window), NOW)["state"] == "unknown"


@pytest.mark.parametrize("percent", [1, None])
def test_zero_reset_without_confirmed_zero_usage_is_unknown(percent):
    window = parsed_window(reset_at=0, used_percent=percent)
    assert weekly_countdown(usage(window), NOW)["state"] == "unknown"


@pytest.mark.parametrize("reset", [NOW, NOW - timedelta(days=1)])
def test_elapsed_reset_is_unknown(reset):
    assert weekly_countdown(usage(Window("7d", 0, reset)), NOW)["state"] == "unknown"


def test_cached_or_failed_usage_cannot_confirm_started_or_unstarted():
    for window in (Window("7d", 0, NOW + timedelta(days=1)), parsed_window(reset_at=0)):
        u = usage(window)
        u.stale = True
        assert weekly_countdown(u, NOW)["state"] == "unknown"
        u.stale = False
        u.ok = False
        assert weekly_countdown(u, NOW)["state"] == "unknown"


def test_absent_weekly_window_and_api_key_accounts_are_unavailable():
    assert weekly_countdown(usage(), NOW)["state"] == "unavailable"
    assert weekly_countdown(usage(Window("5h", 0)), NOW)["state"] == "unavailable"
    u = usage()
    u.ok = False
    u.error = "logged in with an API key; usage limits only exist for ChatGPT logins"
    assert weekly_countdown(u, NOW)["state"] == "unavailable"
    u.error = "network timeout"
    assert weekly_countdown(u, NOW)["state"] == "unknown"


def test_countdown_uses_only_main_weekly_window():
    u = usage(Window("review 7d", 0, NOW + timedelta(days=1)))
    assert weekly_countdown(u, NOW)["state"] == "unavailable"


def test_parser_keeps_weekly_window_with_missing_percent():
    windows, _ = codex.parse_usage({"rate_limit": {
        "secondary_window": {"limit_window_seconds": 604800, "reset_at": 0},
    }})
    assert len(windows) == 1 and windows[0].name == "7d"
    assert windows[0].used_percent is None
    assert weekly_countdown(usage(windows[0]), NOW)["state"] == "unknown"


def test_positive_relative_reset_survives_zero_epoch_sentinel():
    window = parsed_window(reset_at=0, reset_after_seconds=60)
    assert window.reset_at_raw == 0 and window.reset_after_seconds_raw == 60
    assert window.resets_at is not None
    assert weekly_countdown(usage(window))["state"] == "active"


def test_unknown_duration_does_not_guess_weekly_from_window_position():
    windows, _ = codex.parse_usage({"rate_limit": {"secondary_window": {"used_percent": 0, "reset_at": 0}}})
    assert windows[0].name == "secondary"
    assert weekly_countdown(usage(windows[0]), NOW)["state"] == "unknown"


@pytest.mark.parametrize("position", ["primary_window", "secondary_window"])
@pytest.mark.parametrize("duration", [None, 0, -1, "invalid"])
def test_missing_duration_does_not_silently_hide_a_possible_weekly_limit(position, duration):
    windows, _ = codex.parse_usage({"rate_limit": {position: {
        "limit_window_seconds": duration, "used_percent": 0,
        "reset_at": (NOW + timedelta(days=1)).timestamp(),
    }}})
    # A reset for an unidentified window proves neither a weekly countdown nor
    # the absence of weekly quota. Keep it visible for verification.
    state = weekly_countdown(usage(windows[0]), NOW)
    assert state["state"] == "unknown"
    assert state["resets_at"] is None


def test_known_weekly_window_takes_precedence_over_an_untyped_window():
    u = usage(Window("7d", 0, reset_at_raw=0))
    u.windows.insert(0, Window("primary", 0))
    assert weekly_countdown(u, NOW)["state"] == "unstarted"


def test_untyped_additional_limits_do_not_imply_a_missing_main_weekly_limit():
    assert weekly_countdown(usage(Window("review secondary", 0)), NOW)["state"] == "unavailable"


def test_zero_sentinel_survives_cache_round_trip_and_state_is_recomputed():
    u = usage(parsed_window(reset_at=0, reset_after_seconds=0))
    d = u.to_dict()
    assert d["weekly_countdown"]["state"] == "unstarted"
    assert d["windows"][0]["reset_at_raw"] == 0
    d["weekly_countdown"] = {"state": "active"}
    restored = Usage.from_dict(d)
    assert restored.windows[0].reset_after_seconds_raw == 0
    assert restored.to_dict()["weekly_countdown"]["state"] == "unstarted"


def test_existing_claude_window_json_stays_unchanged():
    assert Window("7d", 0).to_dict() == {"name": "7d", "used_percent": 0, "resets_at": None}
    assert weekly_countdown(Usage("claude", "claude", True), NOW)["state"] == "unavailable"
