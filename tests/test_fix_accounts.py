"""Regressions from the accounts / default-login review: switching never loses or misfiles a login."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from redline import actions, cli, codex_oauth, sessions, slots
from redline.config import Account, load_accounts, save_accounts
from redline.models import ProviderError
from redline.monitor import Monitor
from redline.providers import identity, token_state
from test_accounts import _jwt, home, make_claude, make_codex  # noqa: F401  (autouse fixture)

pytestmark = pytest.mark.skipif(sys.platform == "darwin", reason="Claude logins live in the Keychain on macOS")


def _rt(d: Path) -> str:
    return json.loads((d / ".credentials.json").read_text())["claudeAiOauth"]["refreshToken"]


def _accts() -> dict[str, Account]:
    return {a.name: a for a in load_accounts()}


def _default_claude(home, email="a@x.com"):
    """~/.claude logged in as `email`, its email cached in ~/.claude.json like Claude Code does."""
    make_claude(home / ".claude", email=email)
    (home / ".claude" / ".claude.json").unlink()
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": email}}))


def _abc(home):
    _default_claude(home)
    make_claude(home / "b", email="b@x.com")
    make_claude(home / "c", email="c@x.com")
    save_accounts([Account("a@x.com", "claude", "~/.claude", note="a@x.com"),
                   Account("b@x.com", "claude", str(home / "b"), note="b@x.com"),
                   Account("c@x.com", "claude", str(home / "c"), note="c@x.com")])


# ------------------------------------------------------------ first switch


def test_first_switch_gives_an_empty_account_its_login_back(home):
    _default_claude(home, "b@x.com")
    actions.add_account("claude", "b@x.com", note="b@x.com")  # added from the + panel: no login
    make_claude(home / "c", email="c@x.com")
    actions.add_account("claude", "c@x.com", home=str(home / "c"), note="c@x.com")
    slots.activate("claude:c@x.com")
    b = _accts()["b@x.com"]
    assert token_state(b) == "ok" and _rt(b.home_path) == "rt-.claude"
    assert not (home / ".redline" / "stash").exists()


def test_scan_links_a_found_login_to_the_account_meant_for_it(home):
    _default_claude(home, "b@x.com")
    actions.add_account("claude", "b@x.com", note="b@x.com")
    added, _ = actions.scan()
    assert added == [] and sorted(_accts()) == ["b@x.com"]  # no "b@x.com-2"
    b = _accts()["b@x.com"]
    assert b.home_path == home / ".claude" and token_state(b) == "ok"


def test_first_switch_never_files_a_foreign_login_under_the_registered_account(home):
    _default_claude(home)
    make_claude(home / "c", email="c@x.com")
    save_accounts([Account("a@x.com", "claude", "~/.claude", note="a@x.com"),
                   Account("c@x.com", "claude", str(home / "c"), note="c@x.com")])
    _default_claude(home, "z@x.com")  # VS Code signed in as z before redline ever switched
    slots.activate("claude:c@x.com")
    accts = _accts()
    assert identity(accts["z@x.com"]) == "z@x.com" and _rt(accts["z@x.com"].home_path) == "rt-.claude"
    assert token_state(accts["a@x.com"]) == "missing"  # a's login was replaced by VS Code: the truth
    with pytest.raises(ProviderError, match="not logged in"):
        slots.activate("claude:a@x.com")


def test_failed_first_switch_changes_nothing_and_can_be_retried(home, monkeypatch):
    _abc(home)
    real, calls = slots.write_json_atomic, {"n": 0}

    def flaky(path, data):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(13, "in use", str(path))
        return real(path, data)

    monkeypatch.setattr(slots, "write_json_atomic", flaky)
    with pytest.raises(PermissionError):
        slots.activate("claude:b@x.com")
    a = _accts()["a@x.com"]
    assert a.home_path == home / ".claude" and token_state(a) == "ok" and not slots.managed("claude")
    slots.activate("claude:b@x.com")
    a = _accts()["a@x.com"]
    assert a.home_path != home / ".claude" and _rt(a.home_path) == "rt-.claude"


# ------------------------------------------------------------ Claude's cached email


def test_an_account_without_a_cached_email_never_inherits_the_previous_one(home):
    _default_claude(home)
    make_claude(home / "b", email="b@x.com", cached_email=False)
    make_claude(home / "c", email="c@x.com")
    save_accounts([Account("a@x.com", "claude", "~/.claude", note="a@x.com"),
                   Account("b@x.com", "claude", str(home / "b"), note="b@x.com"),
                   Account("c@x.com", "claude", str(home / "c"), note="c@x.com")])
    slots.activate("claude:b@x.com")
    assert identity(_accts()["b@x.com"]) is None and "email" not in slots.load_state()["claude"]
    slots.activate("claude:c@x.com")
    assert slots.email_in("claude", home / "b") is None  # not a@x.com
    slots.activate("claude:b@x.com")  # still possible


def test_a_late_cached_email_of_a_hand_named_default_is_not_a_new_account(home):
    _default_claude(home)
    make_claude(home / "w", email="w@x.com", cached_email=False)
    save_accounts([Account("a@x.com", "claude", "~/.claude", note="a@x.com"), Account("work", "claude", str(home / "w"))])
    slots.activate("claude:work")
    doc = json.loads((home / ".claude.json").read_text())
    doc["oauthAccount"] = {"emailAddress": "w@x.com"}  # Claude Code caches work's email later
    (home / ".claude.json").write_text(json.dumps(doc))
    assert slots.reconcile() == []
    assert sorted(_accts()) == ["a@x.com", "work"] and actions.account_details()["claude:work"]["default"]


def test_an_unreadable_claude_json_stops_the_switch_and_is_left_alone(home):
    _abc(home)
    broken = json.dumps({"oauthAccount": {"emailAddress": "a@x.com"}, "mcpServers": {"db": {}}})[:-5]
    (home / ".claude.json").write_text(broken)
    with pytest.raises(ProviderError, match="can't be read"):
        slots.activate("claude:b@x.com")
    assert (home / ".claude.json").read_text() == broken
    assert _rt(home / ".claude") == "rt-.claude" and _rt(home / "b") == "rt-b"


def test_redlines_own_claude_config_dir_doesnt_hide_the_slot_email(home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "elsewhere"))
    _default_claude(home, "b@x.com")
    assert slots.email_in("claude", home / ".claude") == "b@x.com"


# ------------------------------------------------------------ races and damaged state


def test_switch_reads_the_login_after_a_refresh_in_flight_finishes(home, monkeypatch):
    make_codex(home / ".codex", email="d@x.com")
    make_codex(home / "e", email="e@x.com")
    save_accounts([Account("d@x.com", "codex", "~/.codex", note="d@x.com"),
                   Account("e@x.com", "codex", str(home / "e"), note="e@x.com")])
    real = slots.cli_lock

    class refresh_finishes_while_we_wait(real):
        def __enter__(self):
            if self.lock.name == "e.lock":  # the monitor's refresh writes a new single-use token
                doc = json.loads((home / "e" / "auth.json").read_text())
                doc["tokens"]["refresh_token"] = "r2"
                (home / "e" / "auth.json").write_text(json.dumps(doc))
            return super().__enter__()

    monkeypatch.setattr(slots, "cli_lock", refresh_finishes_while_we_wait)
    slots.activate("codex:e@x.com")
    assert json.loads((home / ".codex" / "auth.json").read_text())["tokens"]["refresh_token"] == "r2"


def test_another_process_switching_meanwhile_is_seen(home, monkeypatch):
    for n in "bcd":
        make_claude(home / n, email=f"{n}@x.com")
    _default_claude(home)
    save_accounts([Account("a@x.com", "claude", "~/.claude", note="a@x.com")]
                  + [Account(f"{n}@x.com", "claude", str(home / n), note=f"{n}@x.com") for n in "bcd"])
    slots.activate("claude:b@x.com")
    real, fired = slots.cli_lock, {"done": False}

    class other_process(real):
        def __enter__(self):
            if not fired["done"] and self.lock.name == ".claude.lock":
                fired["done"] = True
                monkeypatch.setattr(slots, "cli_lock", real)
                slots._activate("claude:c@x.com")
                monkeypatch.setattr(slots, "cli_lock", other_process)
            return super().__enter__()

    monkeypatch.setattr(slots, "cli_lock", other_process)
    slots.activate("claude:d@x.com")
    assert (_rt(home / "b"), _rt(home / "c"), _rt(home / ".claude")) == ("rt-b", "rt-c", "rt-d")


def test_a_truncated_slots_json_falls_back_to_its_last_good_copy(home):
    _abc(home)
    slots.activate("claude:b@x.com")
    (home / ".redline" / "slots.json").write_text('{"claude": {"home": ')
    assert token_state(_accts()["b@x.com"]) == "ok"
    slots.activate("claude:c@x.com")
    assert _rt(home / "b") == "rt-b" and _rt(home / ".claude") == "rt-c"


def test_no_switch_on_a_guess_when_slots_json_and_its_copy_are_damaged(home):
    _abc(home)
    slots.activate("claude:b@x.com")
    for f in ("slots.json", "slots.bak"):
        (home / ".redline" / f).write_text("{")
    with pytest.raises(ProviderError, match="damaged"):
        slots.activate("claude:c@x.com")
    assert _rt(home / ".claude") == "rt-b" and _rt(home / "c") == "rt-c"


def test_no_switch_while_a_login_is_running(home, monkeypatch):
    from redline import login

    _abc(home)
    monkeypatch.setattr(login, "running", lambda provider=None: provider == "claude")
    with pytest.raises(ProviderError, match="login is in progress"):
        slots.activate("claude:b@x.com")


def test_a_switch_reports_what_reconcile_found(home):
    make_codex(home / ".codex", email="d@x.com")
    make_codex(home / "e", email="e@x.com")
    make_codex(home / "f", email="f@x.com")
    save_accounts([Account("d@x.com", "codex", "~/.codex"), Account("e@x.com", "codex", str(home / "e")),
                   Account("f@x.com", "codex", str(home / "f"))])
    slots.activate("codex:e@x.com")
    make_codex(home / ".codex", email="f@x.com")
    msg = actions.make_default("d@x.com", "codex")
    assert "e@x.com lost its login" in msg and "d@x.com is now the default" in msg


# ------------------------------------------------------------ scan, add, CLI, sessions


def test_scan_doesnt_bring_back_a_removed_account_through_a_backup(home):
    make_claude(home / ".claude-a", email="a@x.com")
    make_claude(home / ".claude-a-backup", email="a@x.com")
    actions.scan()
    actions.remove_account("a@x.com")
    added, _ = actions.scan()
    assert added == [] and load_accounts() == []
    added, _ = actions.scan(include_removed=True)
    assert [a.name for a, _ in added] == ["a@x.com"]


def test_add_stores_an_absolute_home(home, monkeypatch, tmp_path):
    make_claude(home / ".claude-work", email="w@x.com")
    monkeypatch.chdir(home)
    assert cli.main(["add", "claude", "work", "--home", ".claude-work"]) == 0
    monkeypatch.chdir(tmp_path)
    work = _accts()["work"]
    assert Path(work.home).is_absolute() and token_state(work) == "ok"


def test_cli_wake_logs_the_default_account_in_where_its_login_lives(home, monkeypatch):
    make_codex(home / ".codex", email="d@x.com")
    make_codex(home / "e", email="e@x.com")
    save_accounts([Account("d@x.com", "codex", "~/.codex", note="d@x.com"),
                   Account("e@x.com", "codex", str(home / "e"), note="e@x.com")])
    slots.activate("codex:e@x.com")
    seen = {}

    def fake_login(codex_home, open_url, cancelled=None, timeout=0):
        seen["home"] = Path(codex_home)
        return codex_oauth.save_auth(codex_home, {"id_token": _jwt({"email": "e@x.com"}),
                                                  "access_token": _jwt({"exp": 9e9}), "refresh_token": "new-e"})

    monkeypatch.setattr(codex_oauth, "login", fake_login)
    assert cli.main(["wake", "--force", "e@x.com"]) == 0
    assert seen["home"] == home / ".codex"


def test_wake_on_a_tab_without_accounts_wakes_nobody(home, monkeypatch):
    from redline import login

    make_claude(home / "c", email="me@x.com")
    save_accounts([Account("me@x.com", "claude", str(home / "c"))])
    started = []
    monkeypatch.setattr(login, "start_wake", lambda names, *a, **k: started.append(names) or "ok")
    msg = actions.dispatch("wake", {"names": None, "provider": "codex", "log": lambda *a: None})
    assert started == [] and "no codex accounts" in msg
    actions.dispatch("wake", {"names": None, "provider": "claude", "log": lambda *a: None})
    assert started == [["claude:me@x.com"]]


def _transcript(f: Path, cwd: str, age: float) -> None:
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"cwd": cwd, "type": "user"}) + "\n")
    t = time.time() - age
    os.utime(f, (t, t))


def test_a_project_used_by_two_defaults_in_turn_shows_up_for_both(home):
    _default_claude(home)
    make_claude(home / "b", email="b@x.com")
    save_accounts([Account("a@x.com", "claude", "~/.claude"), Account("b@x.com", "claude", str(home / "b"))])
    proj = home / ".claude" / "projects" / "-w-api"
    _transcript(proj / "s1.jsonl", "/w/api", 3600)
    slots.activate("claude:b@x.com")
    _transcript(proj / "s2.jsonl", "/w/api", 0)
    accts = _accts()
    assert [p["path"] for p in sessions.recent(accts["a@x.com"])] == ["/w/api"]
    assert [p["path"] for p in sessions.recent(accts["b@x.com"])] == ["/w/api"]


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe quoting")
def test_terminal_runs_a_cli_whose_path_has_a_space(home, monkeypatch, tmp_path):
    from redline import clis

    d = tmp_path / "John Smith" / "npm"
    d.mkdir(parents=True)
    marker = tmp_path / "ran.txt"
    (d / "claude.cmd").write_text(f'@echo ran %* > "{marker}"\r\n')
    monkeypatch.setattr(clis, "find_cli", lambda name: str(d / "claude.cmd"))
    seen = {}
    monkeypatch.setattr(actions.subprocess, "Popen", lambda args, **kw: seen.setdefault("args", args))
    actions.open_terminal(Account("w", "claude", str(home / "w")), ["claude", "/login"])
    monkeypatch.undo()
    # the same command line, with /C instead of /K and no new console window
    subprocess.run(seen["args"].replace("/S /K", "/S /C", 1), capture_output=True, timeout=30)
    assert marker.read_text().strip() == "ran /login"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows has no exec")
def test_exec_keeps_arguments_with_spaces(home, tmp_path, monkeypatch, capfd):
    child = tmp_path / "child.py"
    child.write_text("import sys; print(sys.argv[1:])")
    save_accounts([Account("x", "codex", str(tmp_path / "x"))])
    assert cli.main(["exec", "x", "--", sys.executable, str(child), "a b"]) == 0
    assert capfd.readouterr().out.strip() == "['a b']"


def test_monitor_still_reports_reconcile(home):
    make_codex(home / ".codex", email="d@x.com")
    make_codex(home / "e", email="e@x.com")
    save_accounts([Account("d@x.com", "codex", "~/.codex"), Account("e@x.com", "codex", str(home / "e"))])
    slots.activate("codex:e@x.com")
    make_codex(home / ".codex", email="g@x.com")
    m = Monitor(collect=lambda a, refresh_tokens=False: [])
    m.poll()
    assert any("g@x.com" in e["text"] for e in m.payload()["events"])


def test_no_switch_while_a_login_job_runs_however_it_was_started(home, monkeypatch):
    from redline import login

    make_codex(home / ".codex", email="d@x.com")
    make_codex(home / "e", email="e@x.com")
    make_codex(home / "f", email="f@x.com")
    save_accounts([Account("d@x.com", "codex", "~/.codex", note="d@x.com"),
                   Account("e@x.com", "codex", str(home / "e"), note="e@x.com"),
                   Account("f@x.com", "codex", str(home / "f"), note="f@x.com")])
    slots.activate("codex:e@x.com")
    refused = []

    def slow_login(codex_home, open_url, cancelled=None, timeout=0):
        try:  # the browser page is open; the user clicks "Use in VS Code" on f
            slots.activate("codex:f@x.com")
        except ProviderError as e:
            refused.append(str(e))
        return codex_oauth.save_auth(codex_home, {"id_token": _jwt({"email": "e@x.com"}),
                                                  "access_token": _jwt({"exp": 9e9}), "refresh_token": "new-e"})

    monkeypatch.setattr(codex_oauth, "login", slow_login)
    e = slots.effective(_accts()["e@x.com"])
    login.WakeJob([e], lambda *a: None, force=True).run()  # the CLI runs jobs like this, not via start_wake
    assert refused and "login is in progress" in refused[0]
    assert json.loads((home / "f" / "auth.json").read_text())["tokens"]["refresh_token"] == "r"
    assert not login.running()


def _seat(d: Path, email: str, user: str, workspace: str = "ws-1") -> Path:
    d.mkdir(parents=True, exist_ok=True)
    auth = {"https://api.openai.com/auth": {"chatgpt_account_id": workspace, "chatgpt_user_id": user}}
    (d / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": None, "auth_mode": "chatgpt", "tokens": {
        "id_token": _jwt({"email": email, **auth}), "access_token": _jwt({"exp": time.time() + 3600, **auth}),
        "refresh_token": "r", "account_id": workspace}}))
    return d


def test_two_seats_of_one_chatgpt_workspace_are_two_people(home, monkeypatch):
    from redline import countdown
    from redline.providers import load_identity_key

    monkeypatch.setattr(sessions, "recent", lambda a: [])
    save_accounts([Account("a@x.com", "codex", str(_seat(home / "a", "a@x.com", "user-a"))),
                   Account("b@x.com", "codex", str(_seat(home / "b", "b@x.com", "user-b"))),
                   Account("a2", "codex", str(_seat(home / "a2", "a@x.com", "user-a")))])
    accts = _accts()
    assert load_identity_key(accts["a@x.com"]) != load_identity_key(accts["b@x.com"])
    assert load_identity_key(accts["a@x.com"]) == load_identity_key(accts["a2"])
    auth = lambda n: json.loads((home / n / "auth.json").read_text())  # noqa: E731
    assert countdown._auth_identity(auth("a")) != countdown._auth_identity(auth("b"))
    det = actions.account_details()
    assert det["codex:b@x.com"]["shared_quota_with"] == []           # another seat: its own quota
    assert det["codex:a@x.com"]["shared_quota_with"] == ["a2"]       # the same person, logged in twice
