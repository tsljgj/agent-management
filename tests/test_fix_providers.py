"""Regression tests: token writes, refresh locking, provider identity, wake and Codex sign-in."""

import http.client
import json
import os
import socket
import sys
import threading
import time
import types
import urllib.parse
import urllib.request

import pytest

from redline import actions, codex_oauth, http as rl_http, login, slots
from redline.config import Account, load_accounts, save_accounts
from redline.http import HTTPStatusError
from redline.models import ProviderError
from redline.providers import _util, claude, codex, fetch_usage, identity, load_identity_key, refresh_account
from redline.providers._util import cli_lock, write_json_atomic
from test_accounts import _jwt, home, make_chrome, make_claude, make_codex  # noqa: F401

no_slots = pytest.mark.skipif(sys.platform == "darwin", reason="no slot switching on macOS")


@pytest.fixture(autouse=True)
def fresh_provider_state(monkeypatch):
    monkeypatch.setattr(claude, "_refresh_blocked", {})
    monkeypatch.setattr(claude, "_profiles", {})
    monkeypatch.setattr(claude, "_rejected", {}, raising=False)
    monkeypatch.setattr(codex, "_rejected", {}, raising=False)


def _creds(d):
    return json.loads((d / ".credentials.json").read_text())["claudeAiOauth"]


def _set_claude_json(path, email, uuid=None):
    acct = {"emailAddress": email, **({"accountUuid": uuid} if uuid else {})}
    path.write_text(json.dumps({"oauthAccount": acct}))


def _default_a_with(home, **kw):
    """a@x.com is the default (its login in ~/.claude, its email in ~/.claude.json)."""
    make_claude(home / ".claude", email="a@x.com", **kw)
    (home / ".claude" / ".claude.json").unlink()
    _set_claude_json(home / ".claude.json", "a@x.com")


USAGE = {"five_hour": {"utilization": 10, "resets_at": "2030-01-01T00:00:00Z"}}


# ------------------------------------------------------------ 1. token writes survive a busy file


def test_atomic_write_retries_while_the_target_is_busy(tmp_path, monkeypatch):
    path = tmp_path / "auth.json"
    path.write_text("{}")
    real, calls = os.replace, []

    def busy(src, dst):  # Windows: someone has the file open without FILE_SHARE_DELETE
        calls.append(dst)
        if len(calls) < 4:
            raise PermissionError(13, "Access is denied")
        real(src, dst)

    monkeypatch.setattr(_util.os, "replace", busy)
    write_json_atomic(path, {"tokens": {"refresh_token": "new"}})
    assert json.loads(path.read_text())["tokens"]["refresh_token"] == "new"
    assert len(calls) == 4 and not (tmp_path / "auth.json.redline.tmp").exists()


def test_atomic_write_overwrites_in_place_when_the_target_stays_busy(tmp_path, monkeypatch):
    path = tmp_path / "auth.json"
    path.write_text("{}")
    monkeypatch.setattr(_util, "REPLACE_WAIT", 0.2, raising=False)
    monkeypatch.setattr(_util.os, "replace", lambda s, d: (_ for _ in ()).throw(PermissionError(13, "denied")))
    write_json_atomic(path, {"tokens": {"refresh_token": "new"}})
    assert json.loads(path.read_text())["tokens"]["refresh_token"] == "new"
    assert not (tmp_path / "auth.json.redline.tmp").exists()  # live tokens never linger in a stray file


@pytest.mark.skipif(sys.platform != "win32", reason="Windows share-mode semantics")
def test_rotated_tokens_are_saved_while_a_reader_holds_the_file(home, monkeypatch):
    monkeypatch.setattr(_util, "REPLACE_WAIT", 0.3, raising=False)
    cd = make_claude(home / "c", expires_in=-10)
    xd = make_codex(home / "x", exp_in=-10)
    held = []

    def fake(method, url, **kw):  # a read in another thread opens the file right as we write back
        f = cd / ".credentials.json" if url == claude.TOKEN_URL else xd / "auth.json"
        held.append(open(f, encoding="utf-8"))
        return {"access_token": "at-new", "refresh_token": "rt-new", "id_token": _jwt({"email": "x@y.com"}),
                "expires_in": 3600}

    monkeypatch.setattr(claude, "request_json", fake)
    monkeypatch.setattr(codex, "request_json", fake)
    try:
        claude.refresh_account(Account("c", "claude", str(cd)), manual=True)
        codex.refresh_account(Account("x", "codex", str(xd)))
    finally:
        for f in held:
            f.close()
    assert _creds(cd)["refreshToken"] == "rt-new"
    assert json.loads((xd / "auth.json").read_text())["tokens"]["refresh_token"] == "rt-new"
    assert not list(home.rglob("*.redline.tmp"))


# ------------------------------------------------------------ 2. default home identity


@no_slots
def test_default_claude_identity_ignores_redlines_own_claude_config_dir(home, monkeypatch):
    _default_a_with(home)
    acct = Account("a@x.com", "claude", "~/.claude")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "quant"))  # redline started from another workspace
    assert identity(acct) == "a@x.com"
    assert slots.email_in("claude", home / ".claude") == "a@x.com"


# ------------------------------------------------------------ 3. refresh lock + login moved


def test_lock_stays_fresh_while_a_slow_refresh_runs(home, monkeypatch):
    monkeypatch.setattr(cli_lock, "STALE", 0.6)
    monkeypatch.setattr(cli_lock, "UPDATE", 0.1, raising=False)
    acct = Account("c", "claude", str(make_claude(home / "c", expires_in=-10)))
    used, second = [], {}

    def other_refresher():
        try:
            claude.refresh_account(acct)
            second["ok"] = True
        except Exception as e:  # pragma: no cover - reported by the assert below
            second["error"] = e

    t = threading.Thread(target=other_refresher)

    def slow_token_endpoint(method, url, json_body=None, **kw):
        used.append(json_body["refresh_token"])
        if len(used) == 1:
            t.start()        # the monitor / a CLI refresh arrives meanwhile
            time.sleep(1.5)  # far past STALE
            return {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600}
        return {"access_token": "at-2", "refresh_token": "rt-2", "expires_in": 3600}

    monkeypatch.setattr(claude, "request_json", slow_token_endpoint)
    claude.refresh_account(acct)
    t.join(15)
    assert used == ["rt-c"]  # the single-use refresh token went out once
    assert second == {"ok": True} and _creds(home / "c")["refreshToken"] == "rt-1"
    assert not (home / "c.lock").exists()


def test_holder_does_not_remove_a_lock_it_lost(tmp_path):
    target = tmp_path / "cfg"
    target.mkdir()
    a = cli_lock(target)
    a.__enter__()
    old = time.time() - 11  # a was suspended past STALE (laptop asleep)
    os.utime(a.lock, (old, old))
    b = cli_lock(target)
    b.__enter__()
    a.__exit__(None, None, None)
    assert b.lock.exists()  # b's critical section keeps its lock
    b.__exit__(None, None, None)
    assert not b.lock.exists()


def test_claude_refresh_does_not_write_over_a_login_that_replaced_it(home, monkeypatch):
    d = make_claude(home / "b", email="b@x.com", expires_in=-10)

    def token_endpoint(method, url, **kw):  # a fresh login of z lands while b's POST is in flight
        make_claude(home / "z", email="z@x.com")
        (d / ".credentials.json").write_text((home / "z" / ".credentials.json").read_text())
        _set_claude_json(d / ".claude.json", "z@x.com")
        return {"access_token": "at-b2", "refresh_token": "rt-b2", "expires_in": 3600}

    monkeypatch.setattr(claude, "request_json", token_endpoint)
    with pytest.raises(ProviderError, match="login moved"):
        claude.refresh_account(Account("b", "claude", str(d)), manual=True)
    assert _creds(d)["refreshToken"] == "rt-z"  # z's login is intact


def test_codex_refresh_does_not_write_over_a_login_that_replaced_it(home, monkeypatch):
    d = make_codex(home / "e", email="e@x.com", exp_in=-10)

    def token_endpoint(method, url, **kw):
        doc = json.loads((d / "auth.json").read_text())
        doc["tokens"].update(id_token=_jwt({"email": "f@x.com"}), refresh_token="r-f", account_id="acc-f")
        (d / "auth.json").write_text(json.dumps(doc))
        return {"access_token": _jwt({"exp": time.time() + 3600}), "refresh_token": "r-e2", "id_token": _jwt({"email": "e@x.com"})}

    monkeypatch.setattr(codex, "request_json", token_endpoint)
    with pytest.raises(ProviderError, match="login moved"):
        codex.refresh_account(Account("e", "codex", str(d)))
    tokens = json.loads((d / "auth.json").read_text())["tokens"]
    assert tokens["refresh_token"] == "r-f" and codex.jwt_claims(tokens["id_token"])["email"] == "f@x.com"


@no_slots
def test_slow_refresh_and_a_switch_lose_neither_login(home, monkeypatch):
    make_claude(home / ".claude", email="a@x.com")
    (home / ".claude" / ".claude.json").unlink()
    _set_claude_json(home / ".claude.json", "a@x.com")
    make_claude(home / "b", email="b@x.com", expires_in=-100)
    make_claude(home / "c", email="c@x.com")
    save_accounts([Account("a@x.com", "claude", "~/.claude", note="a@x.com"),
                   Account("b@x.com", "claude", str(home / "b"), note="b@x.com"),
                   Account("c@x.com", "claude", str(home / "c"), note="c@x.com")])
    slots.activate("claude:b@x.com")
    b = next(a for a in load_accounts() if a.name == "b@x.com")

    def slow_token_endpoint(method, url, **kw):  # the holder stalled past STALE; a switch to c went through
        old = time.time() - 11
        os.utime(home / ".claude.lock", (old, old))
        slots.activate("claude:c@x.com")
        return {"access_token": "at-b2", "refresh_token": "rt-b2", "expires_in": 3600}

    monkeypatch.setattr(claude, "request_json", slow_token_endpoint)
    with pytest.raises(ProviderError, match="login moved"):
        refresh_account(b, manual=True)
    assert _creds(home / ".claude")["refreshToken"] == "rt-c"  # c's login, in the slot, untouched
    assert json.loads((home / ".claude.json").read_text())["oauthAccount"]["emailAddress"] == "c@x.com"


def test_claude_refresh_does_not_adopt_another_login(home, monkeypatch):
    d = make_claude(home / "a", email="a@x.com", expires_in=-10)
    acct = Account("a", "claude", str(d))
    creds = claude.load_creds(acct)
    make_claude(home / "z", email="z@x.com")  # another login replaces a's before the refresh gets the lock
    (d / ".credentials.json").write_text((home / "z" / ".credentials.json").read_text())
    _set_claude_json(d / ".claude.json", "z@x.com")
    monkeypatch.setattr(claude, "request_json", lambda *a, **k: pytest.fail("no refresh expected"))
    with pytest.raises(ProviderError, match="login moved"):
        claude.refresh(creds)
    assert creds.oauth["accessToken"] == "at-a"


@no_slots
def test_switch_while_a_poll_waits_for_the_lock_does_not_show_the_other_login(home, monkeypatch):
    _default_a_with(home, expires_in=-10)
    make_claude(home / "c", email="c@x.com")
    save_accounts([Account("a@x.com", "claude", "~/.claude"), Account("c@x.com", "claude", str(home / "c"))])
    a = next(x for x in load_accounts() if x.name == "a@x.com")
    real_lock, switched = claude.cli_lock, []

    class lock_after_switch(real_lock):
        def __enter__(self):
            if not switched:  # the user makes c the default while this poll waits for the lock
                switched.append(slots.activate("claude:c@x.com"))
            return super().__enter__()

    monkeypatch.setattr(claude, "cli_lock", lock_after_switch)
    monkeypatch.setattr(claude, "request_json", lambda m, url, **kw: USAGE)
    u = fetch_usage(a, refresh_tokens=True)
    assert switched and not u.ok and u.email != "c@x.com"


def test_codex_refresh_does_not_adopt_another_login(home, monkeypatch):
    d = make_codex(home / "e", email="e@x.com", exp_in=-10)
    acct = Account("e", "codex", str(d))
    auth = codex.load_auth(acct)
    make_codex(d, email="f@x.com")
    doc = json.loads((d / "auth.json").read_text())
    doc["tokens"].update(refresh_token="r-f", account_id="acc-f")
    (d / "auth.json").write_text(json.dumps(doc))
    monkeypatch.setattr(codex, "request_json", lambda *a, **k: pytest.fail("no refresh expected"))
    with pytest.raises(ProviderError, match="login moved"):
        codex.refresh(acct, auth)


# ------------------------------------------------------------ 4. network errors are ProviderErrors


class _Resp:
    def __init__(self, doc):
        self.raw = json.dumps(doc).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.raw


def test_server_that_never_answers_is_a_provider_error(monkeypatch):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)  # the handshake completes, but nobody ever answers
    monkeypatch.setattr(rl_http, "TIMEOUT", 1)
    try:
        with pytest.raises(ProviderError, match="network error"):
            rl_http.request_json("POST", f"http://127.0.0.1:{srv.getsockname()[1]}/v1/oauth/token", json_body={})
    finally:
        srv.close()


@pytest.mark.parametrize("exc", [ConnectionResetError(10054, "reset"), http.client.RemoteDisconnected("closed"),
                                 http.client.IncompleteRead(b"")])
def test_dropped_connection_is_a_provider_error(monkeypatch, exc):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(exc))
    with pytest.raises(ProviderError, match="network error"):
        rl_http.request_json("GET", "https://example.invalid/x")


def test_one_refresh_timeout_does_not_end_the_wake(home, monkeypatch):
    a = Account("a", "claude", str(make_claude(home / "a", email="a@x.com", expires_in=-10)))
    b = Account("b", "claude", str(make_claude(home / "b", email="b@x.com", expires_in=-10)))
    save_accounts([a, b])
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("timed out")))
    logins = []

    def fake_login(self, acct):
        logins.append(acct.name)
        self._done(acct, "ok", "logged-in", f"{acct.name}: logged in")

    monkeypatch.setattr(login.WakeJob, "_login", fake_login)
    logs = []
    res = login.WakeJob([a, b], lambda lv, t: logs.append(t)).run()
    assert logins == ["a", "b"] and res == {"a": "logged-in", "b": "logged-in"}
    assert any("wake done" in t for t in logs)


def test_profile_timeout_keeps_a_good_usage(home, monkeypatch):
    acct = Account("c", "claude", str(make_claude(home / "c", cached_email=False)))

    def urlopen(req, timeout=None):
        if req.full_url == claude.PROFILE_URL:
            raise TimeoutError("timed out")
        return _Resp(USAGE)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    u = fetch_usage(acct)
    assert u.ok and u.windows[0].used_percent == 10


# ------------------------------------------------------------ 5. profile cache follows the login


def test_profile_cache_follows_the_login_not_the_directory(home, monkeypatch):
    d = make_claude(home / "shared", email="a@x.com")
    acct = Account("shared", "claude", str(d))
    dates = {"Bearer at-shared": "2025-01-15T00:00:00Z", "Bearer at-b": "2025-06-20T00:00:00Z"}

    def fake(method, url, headers=None, **kw):
        if url == claude.PROFILE_URL:
            return {"organization": {"subscription_created_at": dates[headers["Authorization"]]}}
        return USAGE

    monkeypatch.setattr(claude, "request_json", fake)
    assert fetch_usage(acct).renews_at.startswith("2025-01-15")
    make_claude(home / "b", email="b@x.com")  # the dir changes hands (a default switch, an outside login)
    (d / ".credentials.json").write_text((home / "b" / ".credentials.json").read_text())
    _set_claude_json(d / ".claude.json", "b@x.com")
    u = fetch_usage(acct)
    assert u.ok and u.email == "b@x.com" and u.renews_at.startswith("2025-06-20")


@no_slots
def test_new_default_shows_its_own_subscription(home, monkeypatch):
    _default_a_with(home)
    make_claude(home / "b", email="b@x.com")
    save_accounts([Account("a@x.com", "claude", "~/.claude"), Account("b@x.com", "claude", str(home / "b"))])
    dates = {"Bearer at-.claude": "2025-01-15T00:00:00Z", "Bearer at-b": "2025-06-20T00:00:00Z"}

    def fake(method, url, headers=None, **kw):
        if url == claude.PROFILE_URL:
            return {"organization": {"subscription_created_at": dates[headers["Authorization"]]}}
        return USAGE

    monkeypatch.setattr(claude, "request_json", fake)
    assert fetch_usage(load_accounts()[0]).renews_at.startswith("2025-01-15")
    slots.activate("claude:b@x.com")
    u = fetch_usage(next(a for a in load_accounts() if a.name == "b@x.com"))
    assert u.ok and u.email == "b@x.com" and u.renews_at.startswith("2025-06-20")


# ------------------------------------------------------------ 6. a rejected refresh token is not resent


def test_claude_rejected_refresh_token_is_not_resent_in_the_background(home, monkeypatch):
    d = make_claude(home / "dead", expires_in=-10)
    acct = Account("dead", "claude", str(d))
    posts = []

    def rejected(method, url, **kw):
        posts.append(url)
        raise HTTPStatusError(400, '{"error": "invalid_grant"}', url)

    monkeypatch.setattr(claude, "request_json", rejected)
    for _ in range(5):  # five background polls
        u = fetch_usage(acct, refresh_tokens=True)
        assert not u.ok and "log in again" in u.error
    assert posts == [claude.TOKEN_URL]
    with pytest.raises(ProviderError, match="log in again"):
        refresh_account(acct, manual=True)  # a wake still tries
    assert len(posts) == 2
    doc = json.loads((d / ".credentials.json").read_text())
    doc["claudeAiOauth"]["refreshToken"] = "rt-new-login"  # logged in again
    (d / ".credentials.json").write_text(json.dumps(doc))
    fetch_usage(acct, refresh_tokens=True)
    assert len(posts) == 3


def test_codex_rejected_refresh_token_is_not_resent_in_the_background(home, monkeypatch):
    d = make_codex(home / "dead", exp_in=-10)
    acct = Account("dead", "codex", str(d))
    posts = []

    def rejected(method, url, **kw):
        posts.append(url)
        raise HTTPStatusError(401, '{"error": {"code": "refresh_token_reused"}}', url)

    monkeypatch.setattr(codex, "request_json", rejected)
    for _ in range(5):
        u = fetch_usage(acct, refresh_tokens=True)
        assert not u.ok and "log in again" in u.error
    assert posts == [codex.TOKEN_URL]
    with pytest.raises(ProviderError, match="log in again"):
        refresh_account(acct, manual=True)
    assert len(posts) == 2


# ------------------------------------------------------------ 7. login changed during a Claude fetch


def test_claude_login_changed_during_the_fetch_is_not_reported_as_its_usage(home, monkeypatch):
    d = make_claude(home / "a", email="a@x.com")
    acct = Account("a", "claude", str(d))

    def fake(method, url, **kw):
        if url == claude.USAGE_URL:  # someone logs z in to this dir while the request is in flight
            _set_claude_json(d / ".claude.json", "z@x.com")
        return USAGE

    monkeypatch.setattr(claude, "request_json", fake)
    u = fetch_usage(acct)
    assert not u.ok and "login changed while fetching usage" in u.error


@no_slots
def test_switch_during_a_claude_fetch_fails_the_poll(home, monkeypatch):
    _default_a_with(home)
    make_claude(home / "c", email="c@x.com")
    save_accounts([Account("a@x.com", "claude", "~/.claude"), Account("c@x.com", "claude", str(home / "c"))])
    a = next(x for x in load_accounts() if x.name == "a@x.com")

    def fake(method, url, **kw):
        if url == claude.USAGE_URL:
            slots.activate("claude:c@x.com")
        return USAGE

    monkeypatch.setattr(claude, "request_json", fake)
    u = fetch_usage(a)
    assert not u.ok and "login changed" in u.error


# ------------------------------------------------------------ 8. Claude identity key


def test_claude_identity_key_is_stable_anonymous_and_follows_the_login(home, monkeypatch):
    d = make_claude(home / "a", email="A@x.com")
    acct = Account("a", "claude", str(d))
    key = load_identity_key(acct)
    assert key and "a@x.com" not in key.lower()
    doc = json.loads((d / ".credentials.json").read_text())
    doc["claudeAiOauth"].update(accessToken="at-rotated", refreshToken="rt-rotated")
    (d / ".credentials.json").write_text(json.dumps(doc))
    assert load_identity_key(acct) == key  # tokens rotate, the login doesn't
    _set_claude_json(d / ".claude.json", "a@x.com")
    assert load_identity_key(acct) == key  # email case doesn't matter
    _set_claude_json(d / ".claude.json", "b@x.com")
    assert load_identity_key(acct) not in (None, key)
    _set_claude_json(d / ".claude.json", "a@x.com", uuid="uuid-1")
    with_uuid = load_identity_key(acct)
    _set_claude_json(d / ".claude.json", "renamed@x.com", uuid="uuid-1")
    assert load_identity_key(acct) == with_uuid  # the account uuid wins over the email
    (d / ".claude.json").unlink()
    assert load_identity_key(acct) is None
    monkeypatch.setattr(claude, "request_json", lambda m, url, **kw: USAGE)
    _set_claude_json(d / ".claude.json", "a@x.com", uuid="uuid-1")
    assert fetch_usage(acct).identity_key == with_uuid


# ------------------------------------------------------------ 9. wake: one account's process is not the next one's


class _DoneProc:
    args = ["claude"]
    returncode = 0
    pid = 1

    def poll(self):
        return 0


def test_previous_accounts_finished_process_does_not_abort_the_next_login(home, monkeypatch):
    make_chrome(home, {"Profile 2": "y@gmail.com"})
    x = Account("x", "claude", str(home / "x"))
    y = Account("y", "claude", str(home / "y"), browser_profile="chrome:Profile 2")
    save_accounts([x, y])
    opened = []

    def fake_spawn(self, cmd, env, open_urls, prof):  # x: hidden `claude auth login`, finishes fine
        make_claude(home / "x", email="x@gmail.com")
        self._lines = ["x: Login successful."]
        self.proc = _DoneProc()

    clock = types.SimpleNamespace(t=0.0)

    def sleep(s):  # y's user pastes the code in the terminal after ~20 s
        clock.t += s
        if opened and clock.t >= 20 and not (home / "y" / ".credentials.json").exists():
            make_claude(home / "y", email="y@gmail.com")

    monkeypatch.setattr(login, "time", types.SimpleNamespace(monotonic=lambda: clock.t, sleep=sleep))
    monkeypatch.setattr(login.WakeJob, "_spawn_hidden", fake_spawn)
    monkeypatch.setattr(login, "claude_has_auth_login", lambda: True)
    monkeypatch.setattr(login, "browser_helper", lambda: None)  # no $BROWSER helper: y gets a terminal
    monkeypatch.setattr(actions, "open_terminal", lambda *a, **k: opened.append(a[0].name) or "opened")
    results = login.WakeJob([x, y], lambda lv, t: None).run()
    assert opened == ["y"]
    assert results == {"x": "logged-in", "y": "logged-in"}


# ------------------------------------------------------------ 10. Codex sign-in callback server


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _redirect_port(url):
    return urllib.parse.urlsplit(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["redirect_uri"][0]).port


@pytest.mark.skipif(sys.platform != "win32", reason="Windows SO_REUSEADDR semantics")
def test_second_codex_login_falls_back_to_the_other_port(tmp_path, monkeypatch):
    p1, p2 = _free_port(), _free_port()
    monkeypatch.setattr(codex_oauth, "PORTS", (p1, p2))
    first_url, first_cancel, errors = [], threading.Event(), []

    def first():
        try:
            codex_oauth.login(tmp_path / "one", first_url.append, cancelled=first_cancel, timeout=10)
        except ProviderError as e:
            errors.append(str(e))

    t = threading.Thread(target=first, daemon=True)
    t.start()
    for _ in range(50):
        if first_url:
            break
        time.sleep(0.1)
    second_url, second_cancel = [], threading.Event()
    second_cancel.set()
    with pytest.raises(ProviderError, match="cancelled"):
        codex_oauth.login(tmp_path / "two", second_url.append, cancelled=second_cancel, timeout=5)
    first_cancel.set()
    t.join(5)
    assert _redirect_port(first_url[0]) == p1 and _redirect_port(second_url[0]) == p2


def test_idle_connection_does_not_block_cancel(tmp_path, monkeypatch):
    port = _free_port()
    monkeypatch.setattr(codex_oauth, "PORTS", (port,))
    monkeypatch.setattr(codex_oauth, "CONNECTION_TIMEOUT", 0.5, raising=False)
    cancelled, started, out = threading.Event(), threading.Event(), []

    def run():
        try:
            codex_oauth.login(tmp_path / "cx", lambda u: started.set(), cancelled=cancelled, timeout=300)
        except ProviderError as e:
            out.append(str(e))

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert started.wait(5)
    idle = socket.create_connection(("127.0.0.1", port))  # a preconnect / port probe that never sends
    try:
        time.sleep(0.3)
        cancelled.set()
        t.join(4)
        assert not t.is_alive() and out == ["cancelled"]
    finally:
        idle.close()
        t.join(5)


# ------------------------------------------------------------ 11. a sign-in waits for an in-flight refresh


def test_codex_save_auth_waits_for_the_refresh_lock(home):
    d = make_codex(home / "cx", email="old@x.com")
    tokens = {"id_token": _jwt({"email": "new@x.com"}), "access_token": _jwt({"exp": time.time() + 3600}),
              "refresh_token": "r-new"}
    done = []
    with cli_lock(d):  # a background refresh of the old login holds the lock
        t = threading.Thread(target=lambda: done.append(codex_oauth.save_auth(d, tokens)), daemon=True)
        t.start()
        time.sleep(0.5)
        assert not done
        assert json.loads((d / "auth.json").read_text())["tokens"]["refresh_token"] == "r"
    t.join(10)
    assert done == ["new@x.com"]
    assert json.loads((d / "auth.json").read_text())["tokens"]["refresh_token"] == "r-new"
