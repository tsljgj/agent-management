"""Regressions for the monitor / usage model / weekly countdown review (identity, rate, races)."""

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from redline import actions, countdown
from redline.config import Account, save_accounts
from redline.models import Usage, Window
from redline.monitor import Monitor
from redline.providers import codex


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("REDLINE_HOME", str(tmp_path / "redline"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(actions, "account_details", lambda: {})
    monkeypatch.setattr(countdown, "_states", {})
    monkeypatch.setattr(countdown, "_identities", {})
    monkeypatch.setattr(countdown, "_busy", set())
    return home


def now():
    return datetime.now(timezone.utc)


def fed():
    """A monitor whose next poll returns whatever the test put in `m.feed`."""
    m = Monitor(collect=lambda accounts, refresh_tokens=False: list(m.feed))
    m.feed = []
    return m


def alerts(m):
    return [e["text"] for e in m.events if e["alert"]]


def claude(name, pct=None, ok=True, key=None, **kwargs):
    windows = [] if pct is None else [Window("5h", float(pct), now() + timedelta(hours=2))]
    return Usage(name, "claude", ok, windows=windows, identity_key=key, **kwargs)


def unstarted(name, key="id-1"):
    return Usage(name, "codex", True, identity_key=key, windows=[Window("7d", 0, reset_at_raw=0)])


# ------------------------------------------------------------ 1: identity + pruning


def test_claude_identity_change_resets_rate_history_and_threshold_baseline():
    m = fed()
    m.feed = [claude("a@x", 10, key="id-a", email="a@x")]
    m.poll()
    key = ("claude", "a@x")
    m._samples[key] = [(t - 600, p) for t, p in m._samples[key]]
    m.rename("a@x", "b@x", "claude")  # adopt_email_names after the dir was re-logged as b
    m.feed = [claude("b@x", 85, key="id-b", email="b@x")]
    m.poll()
    assert m.usages[0].rate is None            # not diffed against a's 10% from 10 minutes ago
    assert alerts(m) == []                     # a's 10% is not b's baseline for "hit 85%"
    assert len(m._samples[("claude", "b@x")]) == 1


def test_claude_failed_fetch_of_another_login_does_not_show_cached_numbers():
    m = fed()
    m.feed = [claude("w", 91, key="id-old", email="old@x", plan="max 20x")]
    m.poll()
    m.feed = [claude("w", ok=False, key="id-new", error="HTTP 429")]
    m.poll()
    u = m.usages[0]
    assert not u.stale and u.windows == [] and u.email is None and u.plan is None
    assert m.max_percent("5h") is None
    # an unknown identity (old cache, unreadable login) keeps the usual last-good behaviour
    m.feed = [claude("w", 50, key="id-new")]
    m.poll()
    m.feed = [claude("w", ok=False, error="HTTP 429")]
    m.poll()
    assert m.usages[0].stale and m.usages[0].windows[0].used_percent == 50


def test_failed_fetch_that_knows_the_plan_now_does_not_show_the_cached_one():
    m = fed()
    m.feed = [claude("w", 30, key="id-a", email="a@x", plan="max 20x", renews_at="2026-09-14T00:00:00+00:00",
                     renews_kind="start")]
    m.poll()
    m.feed = [claude("w", ok=False, key="id-a", plan="free", error="rate limited by the usage endpoint")]
    m.poll()
    u = m.usages[0]
    assert u.stale and u.windows[0].used_percent == 30 and u.plan == "free" and u.renews_at is None
    m.feed = [claude("w", ok=False, key="id-a", error="HTTP 500")]  # nothing new about the plan: the cached one
    m.poll()
    assert m.usages[0].plan == "max 20x"


def test_removed_account_state_is_pruned_so_a_reused_name_starts_clean():
    m = fed()
    m.feed = [claude("work", 91, email="old@x"), claude("other", 5), unstarted("cx")]
    m.poll()
    assert ("codex", "cx") in m._weekly_reminders
    m.feed = [claude("other", 5)]  # "work" and "cx" were removed
    m.poll()
    keys = [*m._last_ok, *m._samples, *m._weekly_reminders, *(k[:2] for k in m._last_pct)]
    assert ("claude", "work") not in keys and ("codex", "cx") not in keys
    assert ("claude", "work") not in Monitor(collect=lambda a, refresh_tokens=False: [])._last_ok
    m.feed = [claude("work", ok=False, error="not logged in")]  # a new login reuses the name
    m.poll()
    assert not m.usages[0].stale and m.usages[0].windows == [] and m.usages[0].email is None


# ------------------------------------------------------------ 2: tray peak


def test_tray_peak_ignores_stale_windows_that_already_reset():
    m = fed()
    m.feed = [Usage("idle", "claude", True, windows=[Window("5h", 100.0, now() - timedelta(hours=6))]),
              claude("busy", 20)]
    m.poll()
    m.feed = [Usage("idle", "claude", False, error="access token expired"), claude("busy", 20)]
    m.poll()
    assert m.usages[0].stale
    assert m.max_percent("5h") == 20.0


# ------------------------------------------------------------ 5: rename vs. in-flight poll


def test_rename_during_an_inflight_poll_is_kept_and_does_not_re_alert():
    m = Monitor(collect=lambda accounts, refresh_tokens=False: [unstarted("old")])
    m.poll()
    assert len(alerts(m)) == 1

    def collect(accounts, refresh_tokens=False):
        result = [unstarted("old")]       # fetched under the name current when the poll began
        m.rename("old", "new", "codex")   # console rename lands mid-fetch
        return result

    m._collect = collect
    m.poll()
    assert len(alerts(m)) == 1
    assert [u.account for u in m.usages] == ["new"]
    keys = [*m._last_ok, *m._samples, *m._weekly_reminders, *(k[:2] for k in m._last_pct)]
    assert ("codex", "old") not in keys and ("codex", "new") in m._weekly_reminders
    saved = json.loads(m._last_ok_path().read_text(encoding="utf-8"))
    assert [d["account"] for d in saved] == ["new"]


def test_polling_continues_after_an_unexpected_error():
    calls = []

    def collect(accounts, refresh_tokens=False):
        calls.append(1)
        if len(calls) == 1:
            return [None]  # breaks the processing inside poll()
        m.stop()
        return []

    m = Monitor(interval=0, collect=collect)
    m._run()
    assert len(calls) == 2
    assert any(e["level"] == "error" for e in m.events)
    assert m._pending_renames is None


# ------------------------------------------------------------ 6: duplicate window labels


def test_duplicate_codex_window_labels_are_unique_and_do_not_alert():
    doc = {"rate_limit": {"primary_window": {"limit_window_seconds": 18000, "used_percent": 5,
                                             "reset_after_seconds": 100}},
           "additional_rate_limits": [
               {"rate_limit": {"primary_window": {"limit_window_seconds": 18000, "used_percent": 90,
                                                  "reset_after_seconds": 100}}},
               {"rate_limit": {"primary_window": {"limit_window_seconds": 18000, "used_percent": 10,
                                                  "reset_after_seconds": 100}}},
           ]}
    names = [w.name for w in codex.parse_usage(doc)[0]]
    assert names[0] == "5h" and len(set(names)) == 3
    m = Monitor(collect=lambda a, refresh_tokens=False: [
        Usage("w", "codex", True, identity_key="k", windows=codex.parse_usage(doc)[0])])
    for _ in range(4):
        m.poll()
    assert alerts(m) == []
    named = {"additional_rate_limits": [
        {"limit_name": "mini", "rate_limit": {"primary_window": {"limit_window_seconds": 18000}}},
        {"limit_name": "spark", "rate_limit": {"primary_window": {"limit_window_seconds": 18000}}}]}
    assert [w.name for w in codex.parse_usage(named)[0]] == ["mini 5h", "spark 5h"]


# ------------------------------------------------------------ 7, 8: usage rate


def test_rate_is_not_computed_across_different_windows():
    m = Monitor(collect=lambda a, refresh_tokens=False: [])
    t0 = 1_000_000.0
    m._track_rate(Usage("a", "claude", True, windows=[Window("5h", 10.0), Window("7d", 60.0)]), now=t0)
    u = Usage("a", "claude", True, windows=[Window("7d", 60.0)])  # 5h utilization null this time
    m._track_rate(u, now=t0 + 300)
    assert u.rate is None


def test_rate_after_a_long_gap_uses_only_fresh_samples():
    m = Monitor(collect=lambda a, refresh_tokens=False: [])
    t = 1_000_000.0
    m._track_rate(Usage("a", "claude", True, windows=[Window("5h", 50.0)]), now=t)
    t += 10 * 3600  # asleep
    levels = []
    for i, pct in enumerate([55, 60, 65, 70, 75, 80]):  # +5% every 4 min = 75 %/h
        u = Usage("a", "claude", True, windows=[Window("5h", float(pct))])
        m._track_rate(u, now=t + i * 240)
        levels.append(u.rate and u.rate["level"])
    assert levels == [None] + ["high"] * 5


# ------------------------------------------------------------ 13: thresholds across a restart


def test_threshold_crossing_across_a_restart_alerts_only_from_a_recent_same_login_fetch():
    m = fed()
    m.feed = [claude("fresh", 75, key="id"), claude("old", 75, key="id"), claude("relog", 75, key="id-a")]
    m.poll()
    path = m._last_ok_path()
    docs = json.loads(path.read_text(encoding="utf-8"))
    for d in docs:
        if d["account"] == "old":  # last fetched before a long shutdown: may predate a reset
            d["fetched_at"] = (now() - timedelta(minutes=20)).isoformat()
    path.write_text(json.dumps(docs), encoding="utf-8")
    restarted = fed()
    restarted.feed = [claude("fresh", 85, key="id"), claude("old", 85, key="id"), claude("relog", 85, key="id-b")]
    restarted.poll()
    assert alerts(restarted) == ["fresh 5h usage hit 85% (>= 80%)"]


# ------------------------------------------------------------ countdown (9, 10, 11) and runner (12)


def jwt(claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"e30.{payload}.sig"


def write_auth(home: Path, account_id: str) -> None:
    home.mkdir(exist_ok=True)
    user = f"user-{account_id}"
    tokens = {"access_token": jwt({"exp": time.time() + 3600}),
              "id_token": jwt({"sub": user, codex.AUTH_CLAIM: {"chatgpt_account_id": account_id,
                                                                "chatgpt_user_id": user}}),
              "account_id": account_id, "refresh_token": "r"}
    (home / "auth.json").write_text(json.dumps({"tokens": tokens}))


@pytest.fixture
def cx(tmp_path):
    acct = Account("test", "codex", str(tmp_path / "test"))
    write_auth(acct.home_path, "account-one")
    save_accounts([acct])
    return acct


def weekly(acct, percent=0, reset=None):
    return Usage(acct.name, "codex", True, windows=[Window("7d", percent, reset)])


def identity_of(acct):
    identity = countdown._auth_identity(json.loads((acct.home_path / "auth.json").read_text()))
    countdown._identities[acct.key] = identity
    return identity


class Clock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def time(self):
        return time.time()

    def sleep(self, s):
        self.t += s


def test_turn_is_not_sent_when_no_time_is_left_to_verify_it(cx, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(countdown, "time", clock)
    turns = []

    def slow_fetch(acct, refresh_tokens=False):
        clock.t += 60  # refresh + retry, within _fetch_checked's budget
        return weekly(acct)  # 0% with no reset: "unknown", a turn is allowed

    def slow_install(deadline, log):
        clock.t += 115  # first-use npm install, under its 120 s cap
        return "node", Path("entry.js")

    monkeypatch.setattr(countdown, "fetch_usage", slow_fetch)
    monkeypatch.setattr(countdown, "_ensure_sdk", slow_install)
    monkeypatch.setattr(countdown, "_run_turn", lambda *a: turns.append(clock.t))
    countdown._work(cx, identity_of(cx), lambda *a: None, None)
    assert turns == []
    state = countdown.status()[cx.key]
    assert state["state"] == "error" and "nothing was sent" in state["message"]


def test_finished_job_status_is_not_mirrored_onto_a_relogged_card(cx, tmp_path, monkeypatch):
    other = Account("other", "codex", str(tmp_path / "other"))
    other.home_path.mkdir()
    shutil.copy(cx.home_path / "auth.json", other.home_path / "auth.json")
    save_accounts([cx, other])

    def run(name, result):
        monkeypatch.setattr(countdown, "fetch_usage", lambda a, refresh_tokens=False: result(a))
        done = threading.Event()
        countdown.start_countdown(name, lambda *a: None, done.set)
        assert done.wait(5)

    run(cx.name, lambda a: weekly(a, percent=3))  # test's own job is refused
    write_auth(cx.home_path, "account-two")        # test is re-logged as another ChatGPT account
    run(other.name, lambda a: weekly(a, reset=now() + timedelta(days=7)))  # a job for the old login
    st = countdown.status()
    assert st[other.key]["state"] == "success"
    assert st[cx.key]["state"] == "error"


def test_temp_dir_cleanup_failure_after_the_turn_still_verifies(cx, tmp_path, monkeypatch):
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    real_rmtree = shutil.rmtree

    def locked_rmtree(path, *args, **kwargs):
        if "redline-codex-" not in str(path):
            return real_rmtree(path, *args, **kwargs)
        try:
            raise OSError("a session file is still in use")  # lingering child or AV scan
        except OSError as exc:
            if "onexc" in kwargs:
                kwargs["onexc"](os.rmdir, path, exc)
            else:
                kwargs["onerror"](os.rmdir, path, sys.exc_info())

    monkeypatch.setattr(shutil, "rmtree", locked_rmtree)
    values = iter([weekly(cx), weekly(cx, reset=now() + timedelta(days=7))])
    monkeypatch.setattr(countdown, "fetch_usage", lambda *a, **k: next(values))
    monkeypatch.setattr(countdown, "_ensure_sdk", lambda *a: ("node", Path("sdk.js")))
    turns = []
    monkeypatch.setattr(countdown, "_run_turn", lambda *a: turns.append(a))
    countdown._work(cx, identity_of(cx), lambda *a: None, None)
    assert len(turns) == 1
    assert countdown.status()[cx.key]["state"] == "success"


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js not installed")
@pytest.mark.parametrize("message, kind", [
    ("context window exceeded: too many input tokens", "sdk"),
    ("stream disconnected after max_output_tokens", "sdk"),
    ("rate limit reached: 30000 tokens per minute", "sdk"),
    ("401 Unauthorized", "auth"),
    ("invalid_token: provided authentication token is expired", "auth"),
    ("Your access token has expired", "auth"),
    ("Not logged in", "auth"),
])
def test_runner_reports_auth_only_for_auth_errors(tmp_path, message, kind):
    stub = tmp_path / "stub.mjs"
    stub.write_text("export class Codex { constructor() { throw Error(%s) } }" % json.dumps(message))
    runner = Path(countdown.__file__).parent / "assets/start_codex.mjs"
    result = subprocess.run([shutil.which("node"), str(runner), str(stub), str(tmp_path), "3"],
                            capture_output=True, timeout=10)
    assert json.loads(result.stdout) == {"ok": False, "error": kind}
