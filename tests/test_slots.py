"""Switching the default login (~/.claude, ~/.codex) and per-account project history."""

import json
import os
import time
from pathlib import Path

import pytest

from redline import actions, sessions, slots
from redline.config import Account, load_accounts, save_accounts
from redline.procenv import child_env
from redline.providers import identity, token_state
from test_accounts import home, make_claude, make_codex  # noqa: F401  (autouse fixture)

pytestmark = pytest.mark.skipif(os.sys.platform == "darwin", reason="Claude logins live in the Keychain on macOS")


def _setup(home):
    make_claude(home / ".claude", email="a@x.com")
    (home / ".claude" / ".claude.json").unlink()
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "a@x.com"}, "projects": {"p": 1}}))
    make_claude(home / "b", email="b@x.com")
    make_claude(home / "c", email="c@x.com")
    save_accounts([Account("a@x.com", "claude", "~/.claude"), Account("b@x.com", "claude", str(home / "b")),
                   Account("c@x.com", "claude", str(home / "c"))])


def _login(d: Path) -> str:
    return json.loads((d / ".credentials.json").read_text())["claudeAiOauth"]["refreshToken"]


def test_switch_moves_logins_and_never_duplicates(home):
    _setup(home)
    accts = {a.name: a for a in load_accounts()}
    assert actions.account_details()["claude:a@x.com"]["default"]
    assert "already" in slots.activate("claude:a@x.com")

    slots.activate("claude:b@x.com")
    accts = {a.name: a for a in load_accounts()}
    a_home = accts["a@x.com"].home_path
    assert a_home != home / ".claude"                    # the old default got a dir of its own
    assert _login(a_home) == "rt-.claude"                # ...with its login
    assert _login(home / ".claude") == "rt-b"            # b's login is now the default
    assert not (home / "b" / ".credentials.json").exists()  # exactly one copy
    doc = json.loads((home / ".claude.json").read_text())
    assert doc["oauthAccount"]["emailAddress"] == "b@x.com" and doc["projects"] == {"p": 1}
    # every account still resolves to its login and email
    for n in ("a@x.com", "b@x.com", "c@x.com"):
        assert token_state(accts[n]) == "ok" and identity(accts[n]) == n
    det = actions.account_details()
    assert det["claude:b@x.com"]["default"] and not det["claude:a@x.com"]["default"]
    # scan must not register ~/.claude again as a fourth account
    added, _ = actions.scan()
    assert not added

    slots.activate("claude:c@x.com")
    assert _login(home / "b") == "rt-b" and _login(home / ".claude") == "rt-c"
    slots.activate("claude:a@x.com")
    assert _login(home / ".claude") == "rt-.claude" and not (a_home / ".credentials.json").exists()
    assert _login(home / "c") == "rt-c"


def test_switch_refuses_logged_out_account(home):
    _setup(home)
    (home / "c" / ".credentials.json").unlink()
    with pytest.raises(Exception, match="not logged in"):
        slots.activate("claude:c@x.com")
    assert _login(home / ".claude") == "rt-.claude"


def test_codex_switch(home):
    make_codex(home / ".codex", email="d@x.com")
    make_codex(home / "e", email="e@x.com")
    save_accounts([Account("d@x.com", "codex", "~/.codex"), Account("e@x.com", "codex", str(home / "e"))])
    slots.activate("codex:e@x.com")
    accts = {a.name: a for a in load_accounts()}
    assert identity(accts["e@x.com"]) == "e@x.com" and identity(accts["d@x.com"]) == "d@x.com"
    assert not (home / "e" / "auth.json").exists()


def test_default_home_env_unsets_the_variable(home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/elsewhere")
    env = child_env(Account("x", "claude", "~/.claude").env())
    assert "CLAUDE_CONFIG_DIR" not in env
    assert child_env(Account("y", "claude", str(home / "y")).env())["CLAUDE_CONFIG_DIR"] == str(home / "y")


def _transcript(d: Path, cwd: str, age: float):
    d.mkdir(parents=True, exist_ok=True)
    f = d / "s.jsonl"
    f.write_text(json.dumps({"type": "summary"}) + "\n" + json.dumps({"cwd": cwd, "type": "user"}) + "\n")
    t = time.time() - age
    os.utime(f, (t, t))


def test_recent_projects_follow_who_was_default(home):
    _setup(home)
    _transcript(home / "b" / "projects" / "-w-api", "/w/api", 30)            # b via a terminal, running now
    _transcript(home / ".claude" / "projects" / "-w-old", "/w/old", 3600)    # default dir while a was default
    a = next(x for x in load_accounts() if x.name == "a@x.com")
    assert [p["path"] for p in sessions.recent(a)] == ["/w/old"]
    slots.activate("claude:b@x.com")
    _transcript(home / ".claude" / "projects" / "-w-ext", "/w/ext", 0)       # VS Code chat after the switch
    accts = {x.name: x for x in load_accounts()}
    b = sessions.recent(accts["b@x.com"])
    assert [p["path"] for p in b] == ["/w/ext", "/w/api"] and all(p["active"] for p in b)
    assert [p["path"] for p in sessions.recent(accts["a@x.com"])] == ["/w/old"]
    assert sessions.recent(accts["c@x.com"]) == []


def test_codex_recent_projects(home):
    make_codex(home / "e", email="e@x.com")
    day = time.strftime("%Y/%m/%d").split("/")
    d = home / "e" / "sessions" / day[0] / day[1] / day[2]
    d.mkdir(parents=True)
    (d / "rollout-1.jsonl").write_text(json.dumps({"type": "session_meta", "payload": {"cwd": "/w/cx"}}) + "\n")
    e = Account("e@x.com", "codex", str(home / "e"))
    assert [p["path"] for p in sessions.recent(e)] == ["/w/cx"]
