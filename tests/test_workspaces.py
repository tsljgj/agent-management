"""Workspaces phase 1: read-only discovery, drift, `redline ws`, and the console payload."""

import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

from redline import workspaces as ws
from redline.config import Account, save_accounts

from test_countdown_ui import run_console_js  # noqa: F401  (fixture)


def file_uri(p: Path) -> str:
    """How VS Code writes a folder: file:///c%3A/Users/... (drive letter lower-cased and encoded)."""
    s = str(p).replace("\\", "/")
    if len(s) > 1 and s[1] == ":":
        return "file:///" + s[0].lower() + "%3A" + quote(s[2:], safe="/")
    return "file://" + quote(s, safe="/")


def write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def signed_in(d: Path, email: str, claude_json: Path | None = None) -> None:
    write(claude_json or d / ".claude.json", json.dumps({"oauthAccount": {"emailAddress": email}, "projects": {}}))
    write(d / ".credentials.json", "{}")  # only its existence matters; never read


@pytest.fixture
def machine(tmp_path, monkeypatch):
    home, appdata = tmp_path / "home", tmp_path / "appdata"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(appdata))
    m = {k: tmp_path / v for k, v in {
        "quant_cfg": "cfg-quant", "os_cfg": "cfg-osworld", "lab_cfg": "cfg-lab", "lab_codex": "codex-lab",
        "exp": ".claude-experiment-1", "exp2": ".claude-experiment-2",
        "quant_repo": "repos/quant", "os_repo": "repos/OSWorld", "lab_repo": "repos/lab",
        "other": "repos/other", "plain": "repos/plain"}.items()}
    for k in ("quant_repo", "os_repo", "lab_repo", "other", "plain"):
        m[k].mkdir(parents=True)
    m["home"], m["appdata"] = home, appdata
    write(home / ".claude" / "profiles.json", json.dumps({"version": 1, "profiles": [
        {"name": "default", "config_dir": str(home / ".claude"), "repo": None, "profile": None},
        {"name": "quant", "config_dir": str(m["quant_cfg"]), "repo": str(m["quant_repo"]), "profile": "quant"},
        {"name": "osworld", "config_dir": str(m["os_cfg"]), "repo": str(m["os_repo"]), "profile": "osworld"},
        {"name": "exp", "config_dir": str(m["exp"]), "repo": str(m["plain"]), "profile": None},
    ]}))
    user = appdata / "Code" / "User"
    write(user / "globalStorage" / "storage.json", json.dumps({
        "userDataProfiles": [{"location": "-aa", "name": "quant"}, {"location": "-bb", "name": "osworld"},
                             {"location": "-cc", "name": "plain"}, {"location": "-dd", "name": "lab"},
                             {"location": "-ee", "name": "exp"}],
        "profileAssociations": {"workspaces": {
            file_uri(m["quant_repo"]): "-aa", file_uri(m["other"]): "-aa",
            file_uri(m["os_repo"]): "__default__profile__",
            file_uri(m["plain"]): "-cc", file_uri(m["lab_repo"]): "-dd",
            "vscode-remote://ssh-remote%2Bbox/home/me": "-aa",
        }, "emptyWindows": {}},
    }))

    def settings(loc: str, claude: Path | None = None, codex: Path | None = None) -> None:
        env = (f'"claudeCode.environmentVariables": [\n  // which login this Profile uses\n'
               f'  {{"name": "CLAUDE_CONFIG_DIR", "value": {json.dumps(str(claude))}}},\n],\n' if claude else "")
        term = (f'"terminal.integrated.env.{ws._terminal_env_key()}": {{"CODEX_HOME": {json.dumps(str(codex))},}},\n'
                if codex else "")
        write(user / "profiles" / loc / "settings.json",
              '{\n  /* profile settings */\n  "code-runner.executorMap": {"vbscript": "cscript //Nologo",},\n'
              f'  {env}{term}  "editor.fontSize": 13,\n}}\n')

    settings("-aa", claude=m["quant_cfg"])
    settings("-bb", claude=m["os_cfg"])
    settings("-cc")
    settings("-dd", claude=m["lab_cfg"], codex=m["lab_codex"])
    settings("-ee", claude=m["exp2"])

    signed_in(home / ".claude", "a@x.com", claude_json=home / ".claude.json")
    signed_in(m["quant_cfg"], "a@x.com")
    signed_in(m["lab_cfg"], "b@x.com")
    m["os_cfg"].mkdir()
    save_accounts([Account("a@x.com", "claude", str(tmp_path / "acct-a")),
                   Account("b@x.com", "claude", str(tmp_path / "acct-b"))])
    return m


def by_name(rows):
    return {w["name"]: w for w in rows}


def kinds(w):
    return sorted(d["kind"] for d in w["drift"])


# ------------------------------------------------------------ readers


def test_strip_jsonc_keeps_strings_and_drops_comments_and_trailing_commas():
    text = '{"a": "x // y", /* c */ "b": [1, 2,], // tail\n "c": "/* not */", "d": {"e": 1,},}'
    assert json.loads(ws.strip_jsonc(text)) == {"a": "x // y", "b": [1, 2], "c": "/* not */", "d": {"e": 1}}


@pytest.mark.skipif(os.name != "nt", reason="drive letters")
def test_uri_to_path_decodes_drive_letter_and_escapes():
    assert ws.uri_to_path("file:///d%3A/cn-equity-research-canonical") == r"D:\cn-equity-research-canonical"
    assert ws.uri_to_path("file:///c%3A/Users/ALW/Desktop/Uncommon/%40%23%24") == r"C:\Users\ALW\Desktop\Uncommon\@#$"
    assert ws.uri_to_path("vscode-remote://ssh-remote%2Bbox/home") is None


def test_missing_files_give_only_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "h"))
    found = ws.discover()
    assert [w["name"] for w in found] == ["default"]
    assert found[0]["slots"] == {"claude": "~/.claude", "codex": "~/.codex"}


# ------------------------------------------------------------ discovery


def test_discovery(machine):
    found = by_name(ws.discover())
    assert list(found) == ["default", "quant", "osworld", "lab"]
    assert found["quant"]["profile"] == "quant" and found["quant"]["source"] == "profiles.json"
    assert ws.same_path(found["quant"]["repos"][0], machine["quant_repo"])
    assert ws.same_path(found["quant"]["slots"]["claude"], machine["quant_cfg"])
    # a Profile that sets a login dir nobody registered is a workspace too; its folders are its repos
    lab = found["lab"]
    assert lab["source"] == "vscode" and lab["profile"] == "lab"
    assert ws.same_path(lab["slots"]["claude"], machine["lab_cfg"])
    assert ws.same_path(lab["slots"]["codex"], machine["lab_codex"])
    assert [ws.path_key(r) for r in lab["repos"]] == [ws.path_key(machine["lab_repo"])]


def test_experiment_dirs_are_never_workspaces(machine):
    found = ws.discover()
    assert not any(".claude-experiment" in json.dumps(w).lower() for w in found)
    assert "exp" not in by_name(found)
    # a Profile setting nothing (plain) uses the default logins
    assert "plain" not in by_name(found)


# ------------------------------------------------------------ drift


def test_drift_kinds(machine):
    rows = by_name(ws.status())
    assert kinds(rows["osworld"]) == ["no_login", "wrong_profile"]
    wrong = next(d for d in rows["osworld"]["drift"] if d["kind"] == "wrong_profile")
    assert wrong["text"] == "OSWorld opens in the Default profile"
    assert wrong["hint"].startswith("code --profile osworld ")
    assert kinds(rows["quant"]) == ["extra_folders", "shared"]
    extra = next(d for d in rows["quant"]["drift"] if d["kind"] == "extra_folders")
    assert extra["text"].startswith("+1 folders")
    assert [ws.path_key(f) for f in rows["quant"]["extra_folders"]] == [ws.path_key(machine["other"])]
    assert kinds(rows["default"]) == ["shared"]
    shared = next(d for d in rows["default"]["drift"] if d["kind"] == "shared")
    assert shared["text"] == "claude: same account as quant (shared quota)"
    # lab is signed in to Claude (b@x.com); its Codex dir has no login
    assert kinds(rows["lab"]) == ["no_login"]
    assert rows["lab"]["drift"][0]["provider"] == "codex"
    assert rows["lab"]["slots"]["claude"].account.name == "b@x.com"
    assert rows["default"]["slots"]["claude"].account.name == "a@x.com"


def test_profile_pointing_elsewhere_or_missing(machine):
    p = machine["home"] / ".claude" / "profiles.json"
    doc = json.loads(p.read_text(encoding="utf-8"))
    doc["profiles"][1]["profile"] = "osworld"  # quant registered on osworld's Profile
    doc["profiles"].append({"name": "gone", "config_dir": str(machine["lab_cfg"].parent / "cfg-gone"),
                            "repo": None, "profile": "nope"})
    p.write_text(json.dumps(doc), encoding="utf-8")
    rows = by_name(ws.status())
    assert "profile" in kinds(rows["quant"])
    gone = next(d for d in rows["gone"]["drift"] if d["kind"] == "profile")
    assert gone["text"] == "VS Code has no profile nope"


def test_no_login_when_credentials_are_gone(machine):
    (machine["quant_cfg"] / ".credentials.json").unlink()  # .claude.json still names a@x.com
    rows = by_name(ws.status())
    assert "no_login" in kinds(rows["quant"])
    assert "shared" not in kinds(rows["quant"]) and "shared" not in kinds(rows["default"])


# ------------------------------------------------------------ cwd -> workspace, CLI


def test_cwd_finds_workspace_like_git(machine):
    rows = ws.status()
    sub = machine["quant_repo"] / "src" / "pkg"
    sub.mkdir(parents=True)
    assert ws.workspace_for(str(sub), rows)["name"] == "quant"
    assert ws.workspace_for(str(machine["quant_repo"]), rows)["name"] == "quant"
    assert ws.workspace_for(str(machine["quant_repo"]) + "-old", rows) is None
    assert ws.workspace_for(str(machine["other"]), rows) is None


def test_ws_status_in_repo_shows_that_workspace(machine, monkeypatch, capsys):
    from redline.cli import main

    monkeypatch.chdir(machine["os_repo"])
    assert main(["ws", "status"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("osworld ") and "quant" not in out
    assert "not signed in" in out and "opens in the Default profile" in out

    monkeypatch.chdir(machine["other"])
    assert main(["ws", "status"]) == 0
    out = capsys.readouterr().out
    assert {line.split()[0] for line in out.splitlines() if not line.startswith(" ")} == {
        "default", "quant", "osworld", "lab"}
    assert "= quant (shared quota)" in out and "+1 folders" in out


def test_ws_status_json_shape(machine, monkeypatch, capsys):
    from redline.cli import main

    monkeypatch.chdir(machine["quant_repo"])
    assert main(["ws", "status", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["current"] == "quant"
    assert [w["name"] for w in doc["workspaces"]] == ["quant"]
    q = doc["workspaces"][0]
    assert set(q) == {"name", "profile", "repos", "extra_folders", "source", "slots", "drift"}
    assert set(q["slots"]["claude"]) == {"dir", "signed_in", "email", "account", "usage"}
    assert q["slots"]["claude"]["email"] == "a@x.com" and q["slots"]["claude"]["account"] == "a@x.com"
    assert q["slots"]["claude"]["usage"] is None  # no usage cache yet: no network either
    assert {d["kind"] for d in q["drift"]} == {"extra_folders", "shared"}
    assert all(set(d) == {"kind", "provider", "text", "hint"} for d in q["drift"])

    assert main(["ws", "status", "lab", "--json"]) == 0
    assert [w["name"] for w in json.loads(capsys.readouterr().out)["workspaces"]] == ["lab"]
    assert main(["ws", "status", "nope"]) == 1


def test_ws_status_uses_usage_cache(machine, capsys):
    from datetime import datetime, timedelta, timezone

    from redline.cli import main
    from redline.config import redline_home
    from redline.models import Usage, Window

    soon = datetime.now(timezone.utc) + timedelta(hours=1, minutes=20, seconds=30)
    u = Usage("a@x.com", "claude", True, email="a@x.com", windows=[Window("5h", 42, soon), Window("7d", 76, None)])
    (redline_home() / "last_usage.json").write_text(json.dumps([u.to_dict()]), encoding="utf-8")
    assert main(["ws", "status", "quant"]) == 0
    out = capsys.readouterr().out
    assert "5h  42% ↻1h20m" in out and "7d  76%" in out


def test_ws_list(machine, capsys):
    from redline.cli import main

    assert main(["ws", "list"]) == 0
    out = capsys.readouterr().out
    assert "quant" in out and "claude: a@x.com" in out and "claude: —" in out


# ------------------------------------------------------------ scan and user edits


def test_scan_persists_and_reports(machine, capsys):
    from redline.cli import main

    assert not ws.registry_path().exists()
    assert main(["ws", "scan", "--dry-run"]) == 0
    assert not ws.registry_path().exists()
    assert main(["ws", "scan"]) == 0
    out = capsys.readouterr().out
    assert "+ quant" in out and "+ lab" in out
    doc = json.loads(ws.registry_path().read_text(encoding="utf-8"))
    assert doc["version"] == 1 and [w["name"] for w in doc["workspaces"]] == ["default", "quant", "osworld", "lab"]
    assert ws.scan()[1] == []  # nothing changed


def test_scan_does_not_clobber_user_edits(machine):
    ws.scan()
    doc = json.loads(ws.registry_path().read_text(encoding="utf-8"))
    w = by_name(doc["workspaces"])
    w["quant"]["repos"].append(str(machine["other"]))
    w["quant"]["user"] = ["repos"]
    w["osworld"]["profile"] = "osworld-2"
    w["osworld"]["user"] = True
    doc["workspaces"].append({"name": "mine", "profile": None, "repos": [str(machine["plain"])],
                              "slots": {"claude": str(machine["lab_cfg"])}, "source": "user"})
    ws.registry_path().write_text(json.dumps(doc), encoding="utf-8")

    # discovery changes: quant's login dir moves; lab's Profile goes away
    p = machine["home"] / ".claude" / "profiles.json"
    reg = json.loads(p.read_text(encoding="utf-8"))
    reg["profiles"][1]["config_dir"] = str(machine["quant_cfg"]) + "-2"
    p.write_text(json.dumps(reg), encoding="utf-8")
    storage = machine["appdata"] / "Code" / "User" / "globalStorage" / "storage.json"
    st = json.loads(storage.read_text(encoding="utf-8"))
    st["userDataProfiles"] = [x for x in st["userDataProfiles"] if x["name"] != "lab"]
    storage.write_text(json.dumps(st), encoding="utf-8")

    merged, changes = ws.scan()
    m = by_name(merged)
    assert len(m["quant"]["repos"]) == 2  # the user's repo list stays
    assert ws.same_path(m["quant"]["slots"]["claude"], str(machine["quant_cfg"]) + "-2")  # not pinned: updated
    assert m["osworld"]["profile"] == "osworld-2"
    assert "mine" in m and "lab" not in m
    assert any(c.startswith("= quant: kept your repos") for c in changes)
    assert any(c.startswith("= osworld: kept your profile") for c in changes)
    assert any(c.startswith("~ quant: slots") for c in changes)
    assert "- lab: no longer found" in changes
    on_disk = by_name(json.loads(ws.registry_path().read_text(encoding="utf-8"))["workspaces"])
    assert on_disk["quant"]["user"] == ["repos"] and on_disk["osworld"]["user"] is True


def test_damaged_registry_is_not_overwritten(machine):
    ws.registry_path().parent.mkdir(parents=True, exist_ok=True)
    ws.registry_path().write_text("{oops", encoding="utf-8")
    with pytest.raises(ValueError):
        ws.scan()
    assert ws.registry_path().read_text(encoding="utf-8") == "{oops"


def test_scan_never_writes_vscode_or_profiles(machine):
    before = {p: p.read_bytes() for root in (machine["appdata"], machine["home"] / ".claude")
              for p in root.rglob("*") if p.is_file()}
    ws.scan()
    ws.status()
    after = {p: p.read_bytes() for root in (machine["appdata"], machine["home"] / ".claude")
             for p in root.rglob("*") if p.is_file()}
    assert after == before


# ------------------------------------------------------------ console


def test_console_payload_has_names_but_no_paths(machine):
    from redline.monitor import Monitor

    mon = Monitor(collect=lambda accounts, refresh_tokens=False: [])
    mon.poll()
    meta = mon.payload()["meta"]
    view = meta["workspaces"]
    assert [w["name"] for w in view["list"]] == ["default", "quant", "osworld", "lab"]
    assert by_name(view["list"])["quant"]["accounts"] == {"claude": "a@x.com"}
    assert meta["accounts"]["claude:a@x.com"]["workspaces"] == ["default", "quant"]
    assert meta["accounts"]["claude:b@x.com"]["workspaces"] == ["lab"]
    shorts = [n["short"] for n in view["notices"]]
    assert shorts.count("default、quant 共享额度") == 1  # one notice for both workspaces
    assert "OSWorld 在 Default Profile 中打开" in shorts
    text = json.dumps(view, ensure_ascii=False) + json.dumps(
        [a["workspaces"] for a in meta["accounts"].values()], ensure_ascii=False)
    tmp = str(machine["home"].parent)
    for needle in (tmp, tmp.replace("\\", "/"), "\\\\", ":/", "cfg-", "repos/", ".json", "CLAUDE_CONFIG_DIR"):
        assert needle not in text, needle


def test_console_shows_workspace_and_drift(run_console_js):  # noqa: F811
    run_console_js(r"""
      TAB = "claude";
      DATA = {usages: [{account: "a@x.com", provider: "claude", ok: true, stale: false, email: "a@x.com", windows: []}],
              meta: {accounts: {"claude:a@x.com": {workspaces: ["default", "quant"], projects: []}},
                     workspaces: {list: [], notices: [
                       {kind: "shared", provider: "claude", short: "default、quant 共享额度", tip: "同一个账号"},
                       {kind: "wrong_profile", provider: null, short: "OSWorld 在 Default Profile 中打开", tip: "用 osworld Profile 重新打开 OSWorld"},
                       {kind: "no_login", provider: "codex", short: "lab 的 Codex 未登录", tip: "x"}]}}};
      render();
      assert.equal(elements["#wsNotice"].classList.contains("on"), true);
      assert.match(elements["#wsNotice"].innerHTML, /title="同一个账号">default、quant 共享额度</);
      assert.match(elements["#wsNotice"].innerHTML, /OSWorld 在 Default Profile 中打开/);
      assert.doesNotMatch(elements["#wsNotice"].innerHTML, /Codex 未登录/);
      assert.match(details(DATA.usages[0]), /<span>workspace<\/span><b class="" title="default · quant">default · quant<\/b>/);
      TAB = "codex"; render();
      assert.match(elements["#wsNotice"].innerHTML, /lab 的 Codex 未登录/);
      TAB = "plans"; render();
      assert.equal(elements["#wsNotice"].classList.contains("on"), false);
    """)
