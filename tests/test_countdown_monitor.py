import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from redline.models import Usage, Window
from redline.monitor import Monitor, WEEKLY_REMINDER_INTERVAL


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("REDLINE_HOME", str(tmp_path / "am"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()


def usage(state="unstarted", account="work", provider="codex", **kwargs):
    window = {
        "unstarted": Window("7d", 0, reset_at_raw=0),
        "active": Window("7d", 0, datetime.now(timezone.utc) + timedelta(days=7)),
        "unknown": Window("7d", 0),
    }.get(state)
    if provider == "codex":
        kwargs.setdefault("identity_key", "test-chatgpt-account")
    return Usage(account, provider, True, windows=[window] if window else [], **kwargs)


def monitor_for(series):
    it = iter(series)
    return Monitor(collect=lambda accounts, refresh_tokens=False: next(it))


def alerts(m):
    return [e for e in m.events if e["alert"]]


def test_unstarted_alerts_once_until_a_new_weekly_period():
    m = monitor_for([[usage()], [usage()], [usage("active")], [usage()]])
    seen = []
    m.listeners.append(lambda usages, notifications: seen.append(notifications))
    for _ in range(4):
        m.poll()
    assert [len(a) for a in seen] == [1, 0, 0, 1]
    assert all(a["level"] == "warn" for a in alerts(m))
    assert 'work weekly 尚未开始计时；点「开启计时」' in seen[0][0]["text"]


def test_unknown_fault_and_stale_snapshots_do_not_reset_a_reminder():
    failed = Usage("work", "codex", False, error="network timeout", identity_key="test-chatgpt-account")
    m = monitor_for([[usage()], [failed], [usage("unknown")],
                     [usage("active", stale=True)], [usage()]])
    m.poll()
    m.poll()
    assert m.usages[0].stale and not m.usages[0].ok
    for _ in range(3):
        m.poll()
    assert len(alerts(m)) == 1


def test_only_fresh_unstarted_or_unknown_codex_usage_alerts():
    m = monitor_for([[usage("active"), usage("unknown", account="unknown"),
                      usage("unavailable", account="api"),
                      usage(account="cached", stale=True),
                      usage(account="claude", provider="claude"),
                      Usage("offline", "codex", False), usage(account="ready")]])
    m.poll()
    assert len(alerts(m)) == 2
    assert alerts(m)[0]["text"].startswith("unknown weekly 待确认")
    assert alerts(m)[1]["text"].startswith("ready weekly 尚未开始")


def test_wrong_identity_does_not_alert_for_another_accounts_quota(monkeypatch):
    from redline import actions

    monkeypatch.setattr(actions, "account_details", lambda: {
        "codex:wrong": {"login_mismatch": True},
        "codex:ready": {"login_mismatch": False},
    })
    first = usage(account="wrong")
    first.windows.insert(0, Window("5h", 50))
    second = usage(account="wrong")
    second.windows.insert(0, Window("5h", 99))
    m = monitor_for([[first, usage(account="ready")], [second, usage(account="ready")]])
    m.poll()
    m.poll()
    assert len(alerts(m)) == 1
    assert alerts(m)[0]["text"].startswith("ready weekly")
    assert ("codex", "wrong") not in m._weekly_reminders
    assert not any(key[:2] == ("codex", "wrong") for key in m._last_pct)


@pytest.mark.parametrize("window", [
    Window("7d", 0),
    Window("7d", None, reset_at_raw=0),
    Window("7d", 0, datetime(2000, 1, 1, tzinfo=timezone.utc)),
    Window("7d", 12),
    Window("secondary", 0, reset_at_raw=0),
])
def test_uncertain_weekly_data_alerts_without_claiming_unstarted(window):
    u = Usage("work", "codex", True, windows=[window], identity_key="test-chatgpt-account")
    m = monitor_for([[u], [u]])
    m.poll()
    m.poll()
    assert len(alerts(m)) == 1
    assert "weekly 待确认" in alerts(m)[0]["text"]
    assert "尚未开始" not in alerts(m)[0]["text"]
    assert u.to_dict()["weekly_countdown"]["state"] == "unknown"


def test_reminder_survives_restart_and_rearms_after_confirmed_active():
    m = monitor_for([[usage()]])
    m.poll()
    saved = json.loads(m._last_ok_path().read_text(encoding="utf-8"))
    assert saved[0]["weekly_countdown_reminded"] is True
    assert saved[0]["weekly_countdown_reminder"]["state"] == "unstarted"
    assert saved[0]["weekly_countdown_reminder"]["at"] > 0
    assert saved[0]["windows"][0]["reset_at_raw"] == 0

    restarted = monitor_for([[usage()], [usage("active")]])
    restarted.poll()
    assert alerts(restarted) == []
    restarted.poll()
    saved = json.loads(m._last_ok_path().read_text(encoding="utf-8"))
    assert saved[0]["weekly_countdown_reminded"] is False
    assert saved[0]["weekly_countdown_reminder"] is None

    next_period = monitor_for([[usage()]])
    next_period.poll()
    assert len(alerts(next_period)) == 1


def test_fault_after_restart_does_not_rearm_existing_reminder():
    monitor_for([[usage()]]).poll()
    restarted = monitor_for([[Usage("work", "codex", False, error="offline",
                                   identity_key="test-chatgpt-account")], [usage()]])
    restarted.poll()
    restarted.poll()
    assert alerts(restarted) == []


@pytest.mark.parametrize("state", ["unstarted", "unknown"])
def test_rename_carries_reminder_to_new_account_name(state):
    m = monitor_for([[usage(state)], [usage(state, account="renamed")]])
    m.poll()
    before = dict(m._weekly_reminders)
    m.rename("work", "renamed", "codex")
    m.poll()
    assert len(alerts(m)) == 1
    saved = json.loads(m._last_ok_path().read_text(encoding="utf-8"))
    assert saved[0]["account"] == "renamed"
    assert saved[0]["weekly_countdown_reminded"] is True
    assert saved[0]["weekly_countdown_reminder"] == {
        "state": state, "at": before[("codex", "work")][1],
    }


@pytest.mark.parametrize("state", ["unstarted", "unknown"])
def test_unresolved_reminder_repeats_after_one_day_without_moving_deadline(state, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    m = monitor_for([[usage(state)], [usage(state)], [usage(state)], [usage(state)]])
    m.poll()
    first = clock[0]
    clock[0] = first + WEEKLY_REMINDER_INTERVAL - 1
    m.poll()
    assert len(alerts(m)) == 1
    clock[0] += 1
    m.poll()
    assert len(alerts(m)) == 2
    m.poll()
    assert len(alerts(m)) == 2
    saved = json.loads(m._last_ok_path().read_text(encoding="utf-8"))
    assert saved[0]["weekly_countdown_reminder"] == {"state": state, "at": clock[0]}


def test_unknown_to_unstarted_warns_immediately_but_reverse_does_not(monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    m = monitor_for([[usage("unknown")], [usage()], [usage("unknown")], [usage()]])
    m.poll()
    clock[0] += 60
    m.poll()
    assert len(alerts(m)) == 2
    assert "待确认" in alerts(m)[0]["text"]
    assert "尚未开始" in alerts(m)[1]["text"]
    confirmed_at = clock[0]
    clock[0] += 60
    m.poll()
    m.poll()
    assert len(alerts(m)) == 2
    assert m._weekly_reminders[("codex", "work")] == ("unstarted", confirmed_at)


@pytest.mark.parametrize("state", ["unstarted", "unknown"])
def test_restart_keeps_warning_time_and_rechecks_after_unseen_weekly_cycle(state, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    monitor_for([[usage(state)]]).poll()
    clock[0] += 60
    restarted = monitor_for([[usage(state)]])
    restarted.poll()
    assert alerts(restarted) == []
    # The account could have become active and completed its period while this app was closed.
    clock[0] += 8 * WEEKLY_REMINDER_INTERVAL
    after_week = monitor_for([[usage(state)], [usage(state)]])
    after_week.poll()
    after_week.poll()
    assert len(alerts(after_week)) == 1


@pytest.mark.parametrize("age, expected", [(60, 0), (WEEKLY_REMINDER_INTERVAL + 1, 1)])
def test_legacy_boolean_reminder_migrates_using_cached_fetch_time(age, expected, monkeypatch):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    cached = usage(fetched_at=datetime.fromtimestamp(now - age, timezone.utc))
    p = Monitor._last_ok_path()
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps([{**cached.to_dict(), "weekly_countdown_reminded": True}]), encoding="utf-8")
    m = monitor_for([[usage()], [usage()]])
    m.poll()
    m.poll()
    assert len(alerts(m)) == expected
    saved = json.loads(p.read_text(encoding="utf-8"))
    assert saved[0]["weekly_countdown_reminder"] == {
        "state": "unstarted", "at": now if expected else now - age,
    }


@pytest.mark.parametrize("state", ["unstarted", "unknown"])
def test_threshold_alerts_continue_while_weekly_reminder_is_suppressed(state):
    first = usage(state)
    first.windows.insert(0, Window("5h", 50))
    second = usage(state)
    second.windows.insert(0, Window("5h", 85))
    m = monitor_for([[first], [second]])
    m.poll()
    m.poll()
    assert len(alerts(m)) == 2
    assert "usage hit 85%" in alerts(m)[1]["text"]


def test_fresh_identity_change_resets_rate_thresholds_and_weekly_reminder():
    first = usage(identity_key="old-identity-hash")
    first.windows.insert(0, Window("5h", 75))
    second = usage(identity_key="new-identity-hash")
    second.windows.insert(0, Window("5h", 85))
    m = monitor_for([[first], [second], [usage(identity_key="new-identity-hash")]])
    m.poll()
    m._samples[("codex", "work")] = [(time.time() - 600, 75)]
    m.poll()
    assert m.usages[0].rate is None
    assert len(m._samples[("codex", "work")]) == 1
    assert len(alerts(m)) == 2
    assert all("weekly 尚未开始" in a["text"] for a in alerts(m))
    m.poll()
    assert len(alerts(m)) == 2


def test_codex_cache_without_identity_cannot_supply_rate_or_reminder_state():
    legacy = usage(identity_key=None)
    legacy.windows.insert(0, Window("5h", 75))
    m = monitor_for([])
    p = m._last_ok_path()
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps([{**legacy.to_dict(),
                             "rate_samples": [[time.time() - 600, 75]],
                             "weekly_countdown_reminded": True}]), encoding="utf-8")
    fresh = usage()
    fresh.windows.insert(0, Window("5h", 85))
    restarted = monitor_for([[fresh]])
    assert restarted._samples == {}
    restarted.poll()
    assert restarted.usages[0].rate is None
    assert len(alerts(restarted)) == 1
