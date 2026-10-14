import base64
import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from redline import countdown, slots
from redline.config import Account, save_accounts
from redline.models import Usage, Window


def jwt(claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"e30.{payload}.sig"


@pytest.fixture
def account(tmp_path, monkeypatch):
    monkeypatch.setenv("REDLINE_HOME", str(tmp_path / "redline"))
    monkeypatch.setattr(countdown, "_states", {})
    monkeypatch.setattr(countdown, "_identities", {})
    monkeypatch.setattr(countdown, "_busy", set())
    home = tmp_path / "account"
    home.mkdir()
    auth = {"OPENAI_API_KEY": "original-api-key", "tokens": {
        "access_token": jwt({"exp": time.time() + 3600}),
        "id_token": jwt({countdown.codex.AUTH_CLAIM: {"chatgpt_account_id": "account-one"}}),
        "account_id": "account-one", "refresh_token": "never-copy-real-refresh-token"},
        "last_refresh": "2020-01-01T00:00:00Z"}
    (home / "auth.json").write_text(json.dumps(auth))
    acct = Account("test", "codex", str(home))
    save_accounts([acct])
    return acct


def usage(account, percent=0, reset=None, **kwargs):
    return Usage(account.name, "codex", kwargs.pop("ok", True),
                 windows=[Window("7d", percent, reset)], **kwargs)


def test_mismatched_email_cannot_start_a_turn_under_another_account(account, monkeypatch):
    account.note = "free@example.test"
    save_accounts([account])
    path = account.home_path / "auth.json"
    auth = json.loads(path.read_text())
    auth["tokens"]["id_token"] = jwt({
        "email": "paid@example.test",
        countdown.codex.AUTH_CLAIM: {"chatgpt_account_id": "account-one"},
    })
    path.write_text(json.dumps(auth))
    original = path.read_bytes()
    monkeypatch.setattr(countdown.threading, "Thread", lambda **kwargs: pytest.fail("must not queue a turn"))
    with pytest.raises(countdown.CountdownError, match="different email"):
        countdown.start_countdown(account.name, lambda *args: None)
    assert countdown.status() == {}
    assert not countdown._busy
    assert path.read_bytes() == original


def run_worker(account, monkeypatch, values, turn=None):
    identity = countdown._auth_identity(json.loads((account.home_path / "auth.json").read_text()))
    countdown._identities[account.key] = identity
    countdown._busy.add(identity)
    monkeypatch.setattr(countdown, "VERIFY_DELAYS", (0, 0, 0))
    values = iter(values)
    monkeypatch.setattr(countdown, "fetch_usage", lambda *a, **k: next(values))
    monkeypatch.setattr(countdown, "_ensure_sdk", lambda *a: ("node", Path("sdk.js")))
    turns, logs, completed = [], [], []

    def fake_turn(*args):
        turns.append(args)
        if turn:
            turn(*args)

    monkeypatch.setattr(countdown, "_run_turn", fake_turn)
    countdown._work(account, identity, lambda level, message: logs.append(message),
                    lambda: completed.append(True))
    assert identity not in countdown._busy
    assert completed == [True]
    return countdown.status()[account.key], turns, logs


def test_zero_usage_with_future_reset_never_sends_turn(account, monkeypatch):
    reset = datetime.now(timezone.utc) + timedelta(days=7)
    state, turns, _ = run_worker(account, monkeypatch, [usage(account, reset=reset)])
    assert state["state"] == "success" and "already active" in state["message"]
    assert turns == []


@pytest.mark.parametrize("u", [
    lambda a: usage(a, ok=False, error="secret token in raw HTTP response"),
    lambda a: usage(a, stale=True),
    lambda a: usage(a, percent=3),
    lambda a: Usage(a.name, "codex", True),
    lambda a: usage(a, percent=None),
])
def test_unknown_or_failed_usage_cannot_spend_quota(account, monkeypatch, u):
    state, turns, logs = run_worker(account, monkeypatch, [u(account)])
    assert state["state"] == "error" and not turns
    assert "secret token" not in " ".join(logs) + state["message"]


@pytest.mark.parametrize("previous", [None, datetime(2020, 1, 1, tzinfo=timezone.utc)])
def test_one_turn_and_only_usage_polls_confirm_activation(account, monkeypatch, previous):
    reset = datetime.now(timezone.utc) + timedelta(days=7)
    state, turns, _ = run_worker(account, monkeypatch,
                               [usage(account, reset=previous), usage(account), usage(account, reset=reset)])
    assert state["state"] == "success" and len(turns) == 1
    assert not turns[0][2].exists()  # temporary auth and session files are removed


def test_turn_completion_alone_does_not_claim_activation(account, monkeypatch):
    state, turns, _ = run_worker(account, monkeypatch, [usage(account)] * 4)
    assert state["state"] == "error" and "no active weekly reset" in state["message"]
    assert len(turns) == 1


def test_exhausted_short_window_refuses_weekly_turn(account, monkeypatch):
    u = usage(account)
    u.windows.append(Window("5h", 100, datetime.now(timezone.utc) + timedelta(hours=3)))
    state, turns, _ = run_worker(account, monkeypatch, [u])
    assert state["state"] == "error" and "5h quota is exhausted" in state["message"]
    assert not turns


def test_login_change_during_preflight_cannot_report_other_account_as_active(account, monkeypatch):
    identity = countdown._auth_identity(json.loads((account.home_path / "auth.json").read_text()))
    countdown._identities[account.key] = identity
    countdown._busy.add(identity)

    def fetch(*args, **kwargs):
        path = account.home_path / "auth.json"
        auth = json.loads(path.read_text())
        auth["tokens"]["account_id"] = "other-account"
        auth["tokens"]["id_token"] = jwt({countdown.codex.AUTH_CLAIM: {"chatgpt_account_id": "other-account"}})
        path.write_text(json.dumps(auth))
        return usage(account, reset=datetime.now(timezone.utc) + timedelta(days=7))

    monkeypatch.setattr(countdown, "fetch_usage", fetch)
    monkeypatch.setattr(countdown, "_run_turn", lambda *a: pytest.fail("no SDK turn expected"))
    countdown._work(account, identity, lambda *a: None, None)
    assert countdown.status()[account.key]["state"] == "error"
    assert "login changed" in countdown.status()[account.key]["message"]


def test_login_change_after_turn_cannot_verify_other_account(account, monkeypatch):
    def change_login(*args):
        path = account.home_path / "auth.json"
        auth = json.loads(path.read_text())
        auth["tokens"]["account_id"] = "other-account"
        auth["tokens"]["id_token"] = jwt({countdown.codex.AUTH_CLAIM: {"chatgpt_account_id": "other-account"}})
        path.write_text(json.dumps(auth))

    state, turns, _ = run_worker(account, monkeypatch,
                                [usage(account), usage(account, reset=datetime.now(timezone.utc) + timedelta(days=7))],
                                change_login)
    assert state["state"] == "error" and "login changed" in state["message"]
    assert len(turns) == 1


def test_raw_sdk_exception_is_not_logged(account, monkeypatch):
    def fail(*args):
        raise RuntimeError("Bearer sensitive-token\nprivate SDK stderr")

    state, turns, logs = run_worker(account, monkeypatch, [usage(account)], fail)
    assert state["state"] == "error" and len(turns) == 1
    assert "sensitive" not in str(logs) + state["message"]


def test_snapshot_excludes_api_key_refresh_token_and_configuration(account, tmp_path):
    original = (account.home_path / "auth.json").read_bytes()
    (account.home_path / "config.toml").write_text('model_provider="third-party"')
    home = tmp_path / "temporary"
    home.mkdir()
    identity = countdown._auth_identity(json.loads(original))
    countdown._snapshot_auth(account, identity, home)
    snapshot = json.loads((home / "auth.json").read_text())
    assert snapshot["auth_mode"] == "chatgpt" and snapshot["OPENAI_API_KEY"] is None
    assert snapshot["tokens"]["account_id"] == "account-one"
    assert snapshot["tokens"]["refresh_token"] == ""
    assert "never-copy-real-refresh-token" not in json.dumps(snapshot)
    assert list(p.name for p in home.iterdir()) == ["auth.json"]
    assert (account.home_path / "auth.json").read_bytes() == original


def test_snapshot_follows_default_slot_without_switching_it(account, tmp_path, monkeypatch):
    slot = tmp_path / "default"
    slot.mkdir()
    (account.home_path / "auth.json").replace(slot / "auth.json")
    monkeypatch.setitem(slots.DEFAULT_HOMES, "codex", str(slot))
    slots._save_state({"codex": {"home": str(account.home_path.resolve())}})
    original = (slot / "auth.json").read_bytes()
    destination = tmp_path / "snapshot"
    destination.mkdir()
    countdown._snapshot_auth(account, countdown._auth_identity(json.loads(original)), destination)
    assert (slot / "auth.json").read_bytes() == original
    assert not (account.home_path / "auth.json").exists()
    assert slots.is_active(account)


def test_expired_snapshot_refreshes_original_before_copying(account, tmp_path, monkeypatch):
    path = account.home_path / "auth.json"
    auth = json.loads(path.read_text())
    auth["tokens"]["access_token"] = jwt({"exp": time.time() - 1})
    path.write_text(json.dumps(auth))
    identity = countdown._auth_identity(auth)
    calls = []

    def refresh(current, old):
        calls.append(current.home_path)
        assert old["tokens"]["refresh_token"] == "never-copy-real-refresh-token"
        old["tokens"]["access_token"] = jwt({"exp": time.time() + 3600})
        old["tokens"]["refresh_token"] = "rotated-original-refresh-token"
        path.write_text(json.dumps(old))

    monkeypatch.setattr(countdown.codex, "refresh", refresh)
    home = tmp_path / "snapshot"
    home.mkdir()
    countdown._snapshot_auth(account, identity, home)
    assert calls == [account.home_path]
    assert "rotated-original-refresh-token" in path.read_text()
    assert "rotated-original-refresh-token" not in (home / "auth.json").read_text()


def test_changed_login_fails_closed(account, tmp_path):
    home = tmp_path / "snapshot"
    home.mkdir()
    with pytest.raises(countdown.CountdownError, match="login changed"):
        countdown._snapshot_auth(account, "different-identity", home)
    assert not list(home.iterdir())


def test_account_aliases_share_busy_lock_and_retry_after_failure(account, tmp_path, monkeypatch):
    other = Account("alias", "codex", str(tmp_path / "alias"))
    other.home_path.mkdir()
    (other.home_path / "auth.json").write_bytes((account.home_path / "auth.json").read_bytes())
    save_accounts([account, other])
    started, finish = threading.Event(), threading.Event()
    original_worker = countdown._work

    def blocked_worker(acct, identity, log, done):
        started.set()
        assert finish.wait(3)
        countdown._set_state(identity, "error", "Try again.")
        with countdown._lock:
            countdown._busy.discard(identity)
        if done:
            done()

    monkeypatch.setattr(countdown, "_work", blocked_worker)
    finished = threading.Event()
    countdown.start_countdown(account.name, lambda *a: None, finished.set)
    assert started.wait(2)
    assert "already" in countdown.start_countdown(other.name, lambda *a: None)
    assert countdown.status()[other.key]["state"] == "starting"
    finish.set()
    assert finished.wait(2)
    assert countdown.status()[other.key]["state"] == "error"
    assert not countdown._busy
    # A failed activation releases the identity lock, including for another alias.
    monkeypatch.setattr(countdown, "_work", original_worker)
    monkeypatch.setattr(countdown, "fetch_usage", lambda *a, **k: usage(other, percent=3))
    retried = threading.Event()
    assert "checking" in countdown.start_countdown(other.name, lambda *a: None, retried.set)
    assert retried.wait(2)


def test_clean_env_removes_api_and_provider_overrides(tmp_path, monkeypatch):
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "NODE_OPTIONS", "CODEX_HOME", "NPM_CONFIG_REGISTRY"):
        monkeypatch.setenv(key, "sensitive-value")
    env = countdown._clean_env(tmp_path, tmp_path / "codex")
    assert env["CODEX_HOME"] == str(tmp_path / "codex")
    assert env["HOME"] == env["USERPROFILE"] == str(tmp_path)
    assert "sensitive-value" not in json.dumps(env)


def test_sdk_install_is_pinned_local_and_has_no_lifecycle_scripts(account, monkeypatch):
    node = "/runtime/node"
    monkeypatch.setattr(countdown, "find_cli", lambda name: node if name == "node" else None)
    monkeypatch.setattr(countdown, "_npm_script", lambda *a: Path("npm-cli.js"))
    calls = []

    def install(args, **kwargs):
        calls.append((args, kwargs))
        target = Path(args[args.index("--prefix") + 1])
        package = target / "node_modules/@openai/codex-sdk"
        (package / "dist").mkdir(parents=True)
        (package / "dist/index.js").write_text("export class Codex {}")
        (package / "package.json").write_text(json.dumps({"version": countdown.SDK_VERSION}))
        return subprocess.CompletedProcess(args, 0, b"")

    monkeypatch.setattr(countdown, "_process", install)
    deadline = time.monotonic() + 300
    _, entry = countdown._ensure_sdk(deadline, lambda *a: None)
    countdown._ensure_sdk(deadline, lambda *a: None)
    assert len(calls) == 1 and entry.exists()
    args, kwargs = calls[0]
    assert f"@openai/codex-sdk@{countdown.SDK_VERSION}" in args
    assert "--ignore-scripts" in args and "--save-exact" in args
    assert "-g" not in args and "--global" not in args
    assert kwargs["cwd"] == Path(os.environ["REDLINE_HOME"]) / "codex-sdk" / countdown.SDK_VERSION


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js not installed")
def test_runner_disables_tools_and_never_forwards_sdk_errors(tmp_path):
    stub = tmp_path / "stub.mjs"
    stub.write_text('''
export class Codex {
  constructor(options) {
    if (options.env.OPENAI_API_KEY || options.env.CODEX_API_KEY || options.env.OPENAI_BASE_URL) throw Error("unsafe credentials");
    if (options.config.model_provider !== "openai" || options.config.forced_login_method !== "chatgpt") throw Error("unsafe provider");
    if (options.config.features.shell_tool || options.config.features.unified_exec || options.config.features.apps) throw Error("unsafe tools");
  }
  startThread(options) {
    if (options.sandboxMode !== "read-only" || options.networkAccessEnabled || options.webSearchMode !== "disabled") throw Error("unsafe sandbox");
    return {runStreamed: async (prompt) => {
      if (prompt !== "Reply with exactly OK.") throw Error("unsafe prompt");
      return {events: (async function*() {yield {type:"turn.completed"};})()};
    }};
  }
}''')
    runner = Path(countdown.__file__).parent / "assets/start_codex.mjs"
    env = {**os.environ, "OPENAI_API_KEY": "secret-key", "CODEX_API_KEY": "secret-key",
           "OPENAI_BASE_URL": "https://untrusted.invalid"}
    args = [shutil.which("node"), str(runner), str(stub), str(tmp_path), "3"]
    result = subprocess.run(args, capture_output=True, env=env, timeout=10)
    assert result.returncode == 0 and json.loads(result.stdout) == {"ok": True}
    stub.write_text('export class Codex { constructor() { throw Error("invalid_token: secret-token raw CLI stderr") } }')
    result = subprocess.run(args, capture_output=True, env=env, timeout=10)
    assert json.loads(result.stdout) == {"ok": False, "error": "auth"}
    assert b"secret-token" not in result.stdout + result.stderr


def test_process_timeout_kills_child_and_discards_stderr(tmp_path, monkeypatch):
    calls = []

    class FakeProcess:
        pid = 123
        returncode = None

        def communicate(self, timeout):
            calls.append(("communicate", timeout))
            if len(calls) == 1:
                raise subprocess.TimeoutExpired("node", timeout, stderr=b"sensitive token")
            return b"", None

        def kill(self):
            calls.append(("kill",))

    monkeypatch.setattr(countdown.subprocess, "Popen", lambda *a, **k: FakeProcess())
    monkeypatch.setattr(countdown.subprocess, "run", lambda *a, **k: calls.append(("tree", a)))
    monkeypatch.setattr(countdown.os, "killpg", lambda *a: calls.append(("tree", a)), raising=False)
    with pytest.raises(countdown.CountdownError, match="timed out") as exc:
        countdown._process(["node"], env={}, cwd=tmp_path, timeout=1)
    assert "sensitive" not in str(exc.value)
    assert any(c[0] == "tree" for c in calls) and any(c[0] == "kill" for c in calls)


def test_process_timeout_still_kills_parent_when_tree_cleanup_fails(tmp_path, monkeypatch):
    killed = []

    class FakeProcess:
        pid = 456

        def communicate(self, timeout):
            if not killed:
                raise subprocess.TimeoutExpired("node", timeout)
            return b"", None

        def kill(self):
            killed.append(True)

    def fail_tree(*args, **kwargs):
        raise OSError("tree termination failed")

    monkeypatch.setattr(countdown.subprocess, "Popen", lambda *a, **k: FakeProcess())
    monkeypatch.setattr(countdown.subprocess, "run", fail_tree)
    monkeypatch.setattr(countdown.os, "killpg", fail_tree, raising=False)
    with pytest.raises(countdown.CountdownError, match="timed out"):
        countdown._process(["node"], env={}, cwd=tmp_path, timeout=1)
    assert killed == [True]
