"""Discovery, browser profiles, token refresh locking, wake/login orchestration."""

import base64
import json
import os
import time
from pathlib import Path

import pytest

from redline import actions, browsers, cli, login
from redline.config import Account, load_accounts, load_settings, redline_home, save_accounts, save_setting
from redline.discover import discover, suggest_name
from redline.models import Usage, Window
from redline.monitor import Monitor
from redline.providers import claude, codex, token_state
from redline.providers._util import LockBusy, cli_lock


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(h / ".config"))
    monkeypatch.delenv("REDLINE_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    # Only ever see the fake Chrome below, never the machine's real browsers (CI has Edge).
    monkeypatch.setattr(browsers, "_candidates",
                        lambda: [("chrome", "Google Chrome", h / ".config" / "google-chrome", [])])
    return h


def _jwt(claims):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


def make_claude(d: Path, email="c@x.com", expires_in=3600, cached_email=True):
    d.mkdir(parents=True, exist_ok=True)
    (d / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": f"at-{d.name}", "refreshToken": f"rt-{d.name}",
        "expiresAt": int((time.time() + expires_in) * 1000), "scopes": ["user:profile"], "subscriptionType": "max",
    }}))
    if cached_email:
        (d / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": email}}))
    return d


def make_codex(d: Path, email="x@y.com", exp_in=3600):
    d.mkdir(parents=True, exist_ok=True)
    (d / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": None, "last_refresh": "2026-01-01T00:00:00Z", "tokens": {
        "id_token": _jwt({"email": email}), "access_token": _jwt({"exp": time.time() + exp_in}),
        "refresh_token": "r", "account_id": "a"}}))
    return d


def make_chrome(home: Path, profiles: dict[str, str]):
    ud = home / ".config" / "google-chrome"
    ud.mkdir(parents=True)
    cache = {d: {"name": f"Person {i}", "user_name": email} for i, (d, email) in enumerate(profiles.items())}
    (ud / "Local State").write_text(json.dumps({"profile": {"info_cache": cache}}))
    return ud


# ------------------------------------------------------------ config


def test_legacy_home_fallback(home):
    legacy = home / ".agentman"
    legacy.mkdir()
    (legacy / "config.json").write_text('{"accounts": []}')
    assert redline_home() == legacy
    (home / ".redline").mkdir()
    assert redline_home() == home / ".redline"


def test_settings_roundtrip_keeps_accounts(home):
    save_accounts([Account("a", "claude", "/x")])
    assert load_settings()["auto_refresh"] is True
    save_setting("auto_refresh", False)
    assert load_settings()["auto_refresh"] is False
    assert [a.name for a in load_accounts()] == ["a"]


def test_unknown_account_keys_are_ignored(home):
    p = home / ".redline" / "config.json"
    p.parent.mkdir()
    p.write_text(json.dumps({"accounts": [{"name": "a", "provider": "codex", "home": "/x", "future": 1}]}))
    assert load_accounts()[0].name == "a"


# ------------------------------------------------------------ discovery


def test_discover_finds_default_siblings_profiles_and_deep_dirs(home):
    make_claude(home / ".claude", cached_email=False)
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "main@x.com"}}))
    make_claude(home / ".claude-work", email="work@x.com")
    make_claude(home / "accounts" / "alt" / "claude", email="alt@x.com")
    make_codex(home / ".codex", email="me@x.com")
    make_claude(home / "elsewhere" / "cfg", email="alias@x.com")
    (home / ".zshrc").write_text('alias cw="CLAUDE_CONFIG_DIR=$HOME/elsewhere/cfg claude"\n')
    # noise that must be ignored
    (home / "proj" / ".claude").mkdir(parents=True)
    (home / "proj" / ".claude" / "settings.json").write_text("{}")
    (home / "other").mkdir()
    (home / "other" / "auth.json").write_text(json.dumps({"tokens": {"access_token": "t"}}))
    (home / "node_modules" / "x").mkdir(parents=True)
    make_claude(home / "node_modules" / "x", email="nope@x.com")

    found = {(f.provider, f.home.name): f for f in discover()}
    emails = sorted(f.email for f in found.values())
    assert emails == ["alias@x.com", "alt@x.com", "main@x.com", "me@x.com", "work@x.com"]
    assert found[("claude", "cfg")].source == "profile:.zshrc"
    assert found[("claude", ".claude-work")].logged_in


def test_suggest_names(home):
    taken = set()
    names = []
    for d, p in [(home / ".claude", "claude"), (home / ".claude-work", "claude"), (home / "a" / "claude", "claude"),
                 (home / ".codex", "codex"), (home / "b" / "personal", "codex")]:
        from redline.discover import Found
        n = suggest_name(Found(p, d, None, True, "x"), taken)
        taken.add(n)
        names.append(n)
    assert names == ["claude", "claude-work", "claude-a", "codex", "codex-personal"]
    assert suggest_name(Found("claude", home / ".claude", None, True, "x"), taken) == "claude-2"


def test_scan_registers_once(home):
    make_claude(home / ".claude-a", email="a@x.com")
    make_codex(home / ".codex")
    added, existing = actions.scan()
    assert sorted(a.name for a, _ in added) == ["claude-a", "codex"] and not existing
    added, existing = actions.scan()
    assert not added and len(existing) == 2


# ------------------------------------------------------------ browser profiles


def test_profiles_and_resolution(home):
    make_chrome(home, {"Default": "main@gmail.com", "Profile 2": "work@gmail.com", "Profile 3": ""})
    profs = browsers.all_profiles()
    assert [p.spec for p in profs] == ["chrome:Default", "chrome:Profile 2", "chrome:Profile 3"]
    assert browsers.find_profile("WORK@gmail.com").directory == "Profile 2"
    assert browsers.find_profile("Profile 3").email == ""
    assert browsers.find_profile("chrome:Default").email == "main@gmail.com"
    assert browsers.find_profile("nobody@gmail.com") is None

    make_claude(home / ".claude-w", email="work@gmail.com")
    acct = Account("w", "claude", str(home / ".claude-w"))
    assert login.resolve_profile(acct).directory == "Profile 2"  # matched by login email
    acct.browser_profile = "chrome:Default"
    assert login.resolve_profile(acct).directory == "Default"  # explicit binding wins


def test_bind_and_web(home, monkeypatch):
    make_chrome(home, {"Default": "main@gmail.com", "Profile 2": "work@gmail.com"})
    save_accounts([Account("w", "claude", str(home / "w"))])
    assert "Profile 2" in actions.bind("w", "work@gmail.com")
    assert load_accounts()[0].browser_profile == "chrome:Profile 2"
    with pytest.raises(actions.ActionError):
        actions.bind("w", "ghost@gmail.com")
    opened = []
    monkeypatch.setattr(browsers, "open_in_profile", lambda p, url: opened.append((p.directory, url)))
    msg = actions.dispatch("web", {"name": "w"})
    assert opened == [("Profile 2", "https://claude.ai/new")] and "work@gmail.com" in msg
    assert "work@gmail.com" in actions.profiles_text()


def test_browser_callback_routes_to_account_profile(home, monkeypatch):
    make_chrome(home, {"Profile 7": "alt@gmail.com"})
    save_accounts([Account("alt", "codex", str(home / "alt"), browser_profile="chrome:Profile 7")])
    opened = []
    monkeypatch.setattr(browsers, "open_in_profile", lambda p, url: opened.append((p.directory, url)))
    monkeypatch.setenv("REDLINE_ACCOUNT", "alt")
    assert cli.main(['"https://auth.openai.com/oauth/authorize?x=1"']) == 0
    assert opened == [("Profile 7", "https://auth.openai.com/oauth/authorize?x=1")]


# ------------------------------------------------------------ token refresh + lock


def test_cli_lock_waits_and_breaks_stale(tmp_path, monkeypatch):
    target = tmp_path / "cfg"
    target.mkdir()
    lock = tmp_path / "cfg.lock"
    lock.mkdir()
    monkeypatch.setattr(time, "sleep", lambda s: None)
    with pytest.raises(LockBusy):
        with cli_lock(target, retries=2):
            pass
    old = time.time() - 60
    os.utime(lock, (old, old))  # stale -> taken over
    with cli_lock(target):
        assert lock.exists()
    assert not lock.exists()


def test_refresh_skips_when_cli_already_refreshed(home, monkeypatch):
    d = make_claude(home / ".claude-a", expires_in=-10)
    acct = Account("a", "claude", str(d))
    creds = claude.load_creds(acct)
    # the CLI refreshes in the meantime
    doc = json.loads((d / ".credentials.json").read_text())
    doc["claudeAiOauth"].update(accessToken="at-cli", expiresAt=int((time.time() + 3600) * 1000))
    (d / ".credentials.json").write_text(json.dumps(doc))
    monkeypatch.setattr(claude, "request_json", lambda *a, **k: pytest.fail("must not refresh again"))
    claude.refresh(creds)
    assert creds.oauth["accessToken"] == "at-cli"
    assert not (home / ".claude-a.lock").exists()


def test_token_state(home):
    assert token_state(Account("a", "claude", str(home / "nope"))) == "missing"
    assert token_state(Account("b", "claude", str(make_claude(home / "b")))) == "ok"
    assert token_state(Account("c", "claude", str(make_claude(home / "c", expires_in=-5)))) == "expired"
    assert token_state(Account("d", "codex", str(make_codex(home / "d", exp_in=-5)))) == "expired"


# ------------------------------------------------------------ monitor: stale data


def test_monitor_keeps_last_good_numbers():
    seq = iter([True, False])

    def collect(accts, refresh_tokens=False):
        if next(seq):
            return [Usage("w", "claude", True, email="e", windows=[Window("5h", 40.0)])]
        return [Usage("w", "claude", False, error="HTTP 429")]

    m = Monitor(collect=collect)
    m.poll()
    m.poll()
    u = m.usages[0]
    assert not u.ok and u.stale and u.windows[0].used_percent == 40.0 and u.error == "HTTP 429"
    assert m.payload()["usages"][0]["stale"] is True
    assert m.max_percent("5h") == 40.0


# ------------------------------------------------------------ wake


def test_wake_refreshes_expired_and_logs_in_missing(home, monkeypatch):
    make_chrome(home, {"Profile 2": "new@gmail.com"})
    ok = make_claude(home / "ok")
    exp = make_claude(home / "exp", expires_in=-10)
    new = home / "new"
    accts = [Account("ok", "claude", str(ok)), Account("exp", "claude", str(exp)),
             Account("new", "claude", str(new), browser_profile="chrome:Profile 2")]
    save_accounts(accts)

    def fake_request(method, url, **kw):
        assert url == claude.TOKEN_URL
        return {"access_token": "at-refreshed", "refresh_token": "rt2", "expires_in": 3600}

    monkeypatch.setattr(claude, "request_json", fake_request)
    spawned = []

    def fake_spawn(self, cmd, env, open_urls, prof):
        spawned.append((cmd, env["CLAUDE_CONFIG_DIR"], env["REDLINE_ACCOUNT"], prof.directory))
        make_claude(Path(env["CLAUDE_CONFIG_DIR"]), email="new@gmail.com")  # "user completes the login"
        self.proc = None

    monkeypatch.setattr(login.WakeJob, "_spawn_hidden", fake_spawn)
    monkeypatch.setattr(login, "claude_has_auth_login", lambda: True)
    monkeypatch.setattr(login.time, "sleep", lambda s: None)
    logs = []
    results = login.WakeJob(accts, lambda lv, t: logs.append((lv, t))).run()
    assert results == {"ok": "ok", "exp": "refreshed", "new": "logged-in"}
    assert spawned == [(["claude", "auth", "login", "--claudeai", "--email", "new@gmail.com"], str(new), "new", "Profile 2")]
    assert json.loads((exp / ".credentials.json").read_text())["claudeAiOauth"]["accessToken"] == "at-refreshed"
    assert any("3/3 online" in t for _, t in logs)


def test_wake_flags_duplicate_identity(home, monkeypatch):
    a = make_claude(home / "a", email="same@x.com")
    b = home / "b"
    accts = [Account("a", "claude", str(a)), Account("b", "claude", str(b))]
    save_accounts(accts)

    def fake_spawn(self, cmd, env, open_urls, prof):
        make_claude(Path(env["CLAUDE_CONFIG_DIR"]), email="same@x.com")
        self.proc = None

    monkeypatch.setattr(login.WakeJob, "_spawn_hidden", fake_spawn)
    monkeypatch.setattr(login, "claude_has_auth_login", lambda: True)
    monkeypatch.setattr(login.time, "sleep", lambda s: None)
    logs = []
    login.WakeJob([accts[1]], lambda lv, t: logs.append((lv, t))).run()
    assert any(lv == "crit" and "same account as a" in t for lv, t in logs)


# ------------------------------------------------------------ remove / restore


def test_removed_accounts_stay_removed(home):
    make_claude(home / ".claude-exp1", email="e1@x.com")
    make_claude(home / ".claude-keep", email="k@x.com")
    actions.scan()
    assert sorted(a.name for a in load_accounts()) == ["claude-exp1", "claude-keep"]

    msg = actions.dispatch("remove", {"name": "claude-exp1"})
    assert "restore claude-exp1" in msg
    assert [a.name for a in load_accounts()] == ["claude-keep"]
    assert (home / ".claude-exp1" / ".credentials.json").exists()  # files untouched

    added, _ = actions.scan()
    assert added == []  # scan does not resurrect it
    assert "claude-exp1" in actions.dispatch("removed", {})

    actions.dispatch("restore", {"name": "claude-exp1"})
    assert sorted(a.name for a in load_accounts()) == ["claude-exp1", "claude-keep"]
    assert actions.dispatch("removed", {}) == "nothing removed"
    with pytest.raises(actions.ActionError):
        actions.dispatch("restore", {"name": "claude-exp1"})


def test_scan_all_brings_back_removed(home):
    make_codex(home / ".codex")
    actions.scan()
    actions.remove_account("codex")
    added, _ = actions.scan(include_removed=True)
    assert [a.name for a, _ in added] == ["codex"]
    assert actions.dispatch("removed", {}) == "nothing removed"


def test_cli_rm_many_and_restore(home, capsys):
    for n in ("a", "b", "c"):
        actions.add_account("claude", n)
    assert cli.main(["rm", "a", "b"]) == 0
    assert [a.name for a in load_accounts()] == ["c"]
    assert cli.main(["restore", "b"]) == 0
    assert sorted(a.name for a in load_accounts()) == ["b", "c"]


# ------------------------------------------------------------ self-update


def test_update_check_logic(monkeypatch):
    from redline import update

    monkeypatch.setattr(update, "is_packaged", lambda: False)
    rel, msg = update.check()
    assert rel is None and "source" in msg

    monkeypatch.setattr(update, "is_packaged", lambda: True)
    monkeypatch.setattr(update, "BUILD", 7)
    doc = {"tag_name": "build-9", "name": "redline build 9", "html_url": "h", "assets": [
        {"name": "redline.exe", "browser_download_url": "https://x/redline.exe"},
        {"name": "redline.exe.sha256", "browser_download_url": "https://x/redline.exe.sha256"}]}
    monkeypatch.setattr(update, "_get", lambda url, timeout=20: json.dumps(doc).encode())
    rel, msg = update.check()
    assert rel.build == 9 and rel.exe_url.endswith("redline.exe") and "7 -> 9" in msg

    doc["tag_name"] = "build-7"
    assert update.check() == (None, "up to date (build 7)")
    doc["tag_name"] = "v1.0"
    assert update.check()[0] is None
    doc["tag_name"] = "build-10"
    doc["assets"] = doc["assets"][:1]  # no checksum published -> never install
    assert update.check()[0] is None


def test_update_install_refuses_outside_packaged_windows(monkeypatch):
    from redline import update
    from redline.models import ProviderError

    monkeypatch.setattr(update, "can_self_update", lambda: False)
    with pytest.raises(ProviderError):
        update.install(update.Release(9, "build-9", "n", "u", "s", "h"))


def test_update_install_swaps_and_verifies(tmp_path, monkeypatch):
    import hashlib
    import subprocess
    import sys as _sys

    from redline import update
    from redline.models import ProviderError

    exe = tmp_path / "redline.exe"
    exe.write_bytes(b"old build")
    payload = b"new build"
    monkeypatch.setattr(update, "can_self_update", lambda: True)
    monkeypatch.setattr(_sys, "executable", str(exe))
    monkeypatch.setattr(update, "_get", lambda url, timeout=20: (hashlib.sha256(payload).hexdigest() + "  redline.exe").encode())

    def fake_download(url, dest):
        dest.write_bytes(payload)
        return hashlib.sha256(payload).hexdigest()

    monkeypatch.setattr(update, "_download", fake_download)
    launched = []
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: launched.append(args))
    update.install(update.Release(9, "build-9", "n", "u", "s", "h"))
    assert exe.read_bytes() == payload and (tmp_path / "redline.exe.old").read_bytes() == b"old build"
    assert launched and launched[0][0] == str(exe) and "--updated-from" in launched[0]

    # a corrupted download must leave the current exe alone
    monkeypatch.setattr(update, "_download", lambda url, dest: dest.write_bytes(b"evil") or "bad")
    with pytest.raises(ProviderError):
        update.install(update.Release(10, "build-10", "n", "u", "s", "h"))
    assert exe.read_bytes() == payload and not (tmp_path / "redline.exe.download").exists()


# ------------------------------------------------------------ "+" panel


def test_candidates_offer_removed_and_unused_profiles(home):
    make_chrome(home, {"Default": "main@gmail.com", "Profile 2": "work@gmail.com", "Profile 3": "exp@gmail.com",
                       "Profile 4": ""})
    make_claude(home / ".claude", email="main@gmail.com", cached_email=False)
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "main@gmail.com"}}))
    make_claude(home / ".claude-exp", email="exp@gmail.com")
    actions.scan()
    actions.remove_account("claude-exp")

    c = actions.candidates("claude")
    assert [r["name"] for r in c["removed"]] == ["claude-exp"] and c["removed"][0]["email"] == "exp@gmail.com"
    # main@ is in use, exp@ is offered as a restore instead, Profile 4 has no Google account
    assert [p["email"] for p in c["profiles"]] == ["work@gmail.com"]
    # codex has nothing yet: every signed-in profile is a candidate
    assert sorted(p["email"] for p in actions.candidates("codex")["profiles"]) == ["exp@gmail.com", "main@gmail.com",
                                                                                   "work@gmail.com"]


def test_add_from_profile_binds_and_logs_in(home, monkeypatch):
    make_chrome(home, {"Profile 2": "zhihao.work@gmail.com"})
    started = []
    monkeypatch.setattr(login, "start_wake", lambda names, log, force=False, on_done=None: started.append((names, force)))
    msg = actions.dispatch("add-profile", {"provider": "codex", "profile": "chrome:Profile 2", "log": lambda *a: None})
    acct = load_accounts()[0]
    assert (acct.name, acct.provider, acct.browser_profile, acct.note) == \
        ("zhihao.work", "codex", "chrome:Profile 2", "zhihao.work@gmail.com")
    assert started == [(["zhihao.work"], True)] and "logging in" in msg
    # same Google account again -> unique name, no crash
    actions.dispatch("add-profile", {"provider": "claude", "profile": "chrome:Profile 2"})
    assert sorted(a.name for a in load_accounts()) == ["zhihao.work", "zhihao.work-2"]


def test_readding_a_removed_account_clears_the_tombstone(home):
    actions.add_account("claude", "work")
    actions.remove_account("work")
    assert actions.dispatch("removed", {}) != "nothing removed"
    actions.add_account("claude", "work")  # same name + same dir
    assert actions.dispatch("removed", {}) == "nothing removed"
    added, existing = actions.scan()
    assert [a.name for a in load_accounts()] == ["work"]


def test_candidates_endpoint(home):
    from redline.web import TOKEN_HEADER, ConsoleServer
    import urllib.request

    make_chrome(home, {"Default": "a@gmail.com"})
    m = Monitor(collect=lambda a, refresh_tokens=False: [])
    s = ConsoleServer(m, "127.0.0.1", 0).start_background()
    try:
        req = urllib.request.Request(s.url + "api/candidates?provider=codex", headers={TOKEN_HEADER: s.token})
        with urllib.request.urlopen(req, timeout=5) as r:
            doc = json.loads(r.read())
        assert doc["profiles"][0]["email"] == "a@gmail.com"
    finally:
        s.shutdown()
