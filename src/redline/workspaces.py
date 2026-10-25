"""Workspaces: which repos use which login dir (phase 1: read-only discovery and status).

A workspace is a name, some repo roots, one login dir ("slot") per provider and a VS Code
Profile. Discovery reads claude_profile.py's ~/.claude/profiles.json and VS Code's profile
files and never writes either. The registry is $REDLINE_HOME/workspaces.json:

  {"version": 1, "workspaces": [
    {"name": "default", "profile": null, "repos": [], "slots": {"claude": "~/.claude", "codex": "~/.codex"},
     "source": "built-in"},
    {"name": "quant", "profile": "quant", "repos": ["D:\\\\repo"], "slots": {"claude": "D:\\\\cfg"},
     "source": "profiles.json", "user": ["repos"]}]}

`user` pins fields the user edited (true = all of them); `scan` reports what it would have
changed there but leaves them alone. Who is signed in to a slot is never stored: it is read live
from the slot (Claude: the email Claude Code cached in .claude.json; Codex: the redline account
whose login lives there). Tokens are never read.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .config import DEFAULT_HOMES, EDIT_LOCK, HOME_ENV_VARS, Account, load_accounts, redline_home

DEFAULT = "default"
VSCODE_DEFAULT_PROFILE = "__default__profile__"
EXPERIMENT_PREFIX = ".claude-experiment"
FIELDS = ("profile", "repos", "slots")
VERSION = 1


# ------------------------------------------------------------ paths


def registry_path() -> Path:
    return redline_home() / "workspaces.json"


def profiles_path() -> Path:
    """claude_profile.py's registry, where a fresh machine would have it."""
    return Path("~/.claude/profiles.json").expanduser()


def vscode_user_dir() -> Path:
    if os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "Code" / "User"
    if sys.platform == "win32":
        return Path.home() / "AppData" / "Roaming" / "Code" / "User"
    if sys.platform == "darwin":
        return Path("~/Library/Application Support/Code/User").expanduser()
    return Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser() / "Code" / "User"


def expand(p: str) -> str:
    return os.path.normpath(os.path.expanduser(os.path.expandvars(str(p))))


def path_key(p: str) -> str:
    return os.path.normcase(expand(p))


def same_path(a: str | None, b: str | None) -> bool:
    return bool(a and b) and path_key(a) == path_key(b)


def is_experiment(p: str) -> bool:
    return any(part.lower().startswith(EXPERIMENT_PREFIX) for part in Path(expand(p)).parts)


def is_default_slot(provider: str, p: str) -> bool:
    return same_path(p, DEFAULT_HOMES[provider])


def basename(p: str) -> str:
    return Path(expand(p)).name or expand(p)


# ------------------------------------------------------------ loose readers


def strip_jsonc(text: str) -> str:
    """VS Code's JSON-with-comments to JSON: drop // and /* */ comments and trailing commas.

    Strings are copied verbatim (settings hold things like "cscript //Nologo").
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    s = "".join(out)
    # trailing commas, again skipping strings
    res: list[str] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == '"':
            j = i + 1
            while j < n and s[j] != '"':
                j += 2 if s[j] == "\\" else 1
            res.append(s[i:j + 1])
            i = j + 1
            continue
        if c == ",":
            k = i + 1
            while k < n and s[k] in " \t\r\n":
                k += 1
            if k < n and s[k] in "}]":
                i += 1
                continue
        res.append(c)
        i += 1
    return "".join(res)


def read_jsonc(p: Path) -> dict | None:
    """A JSON object from a file that may have comments, trailing commas or a BOM; None if unusable."""
    try:
        text = Path(p).read_text(encoding="utf-8-sig")
    except (OSError, ValueError):
        return None
    try:
        doc = json.loads(strip_jsonc(text))
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def uri_to_path(uri: str) -> str | None:
    """file:///d%3A/repo -> D:\\repo. Remote and untitled URIs -> None."""
    try:
        parts = urlsplit(uri)
    except ValueError:
        return None
    if parts.scheme != "file":
        return None
    path = unquote(parts.path)
    if parts.netloc and parts.netloc.lower() != "localhost":
        path = f"//{parts.netloc}{path}"  # UNC share
    if re.match(r"^/[A-Za-z]:", path):
        path = path[1].upper() + path[2:]
    return os.path.normpath(path)


_ENV_REF = re.compile(r"\$\{env:([^}]+)\}")


def _setting_path(v) -> str | None:
    if not isinstance(v, str) or not v.strip():
        return None
    return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), m.group(0)), v.strip())


def _claude_env(settings: dict) -> str | None:
    v = settings.get("claudeCode.environmentVariables")
    if isinstance(v, list):
        for item in v:
            if isinstance(item, dict) and item.get("name") == "CLAUDE_CONFIG_DIR":
                return _setting_path(item.get("value"))
    elif isinstance(v, dict):
        return _setting_path(v.get("CLAUDE_CONFIG_DIR"))
    return None


def _terminal_env_key() -> str:
    return {"win32": "windows", "darwin": "osx"}.get(sys.platform, "linux")


def _codex_env(settings: dict) -> str | None:
    v = settings.get(f"terminal.integrated.env.{_terminal_env_key()}")
    return _setting_path(v.get("CODEX_HOME")) if isinstance(v, dict) else None


# ------------------------------------------------------------ VS Code


@dataclass
class VSCode:
    found: bool = False
    profiles: list[str] = field(default_factory=list)  # named Profiles (Default excluded)
    # Profile name (None = Default) -> {"claude": dir | None, "codex": dir | None}
    env: dict = field(default_factory=dict)
    folders: list[tuple[str, str | None]] = field(default_factory=list)  # (folder, Profile name; None = Default)

    def folders_of(self, profile: str | None) -> list[str]:
        return [f for f, p in self.folders if p == profile]

    def profile_of(self, folder: str) -> tuple[bool, str | None]:
        """(associated at all, Profile name)."""
        for f, p in self.folders:
            if same_path(f, folder):
                return True, p
        return False, None


def read_vscode() -> VSCode:
    user = vscode_user_dir()
    storage = read_jsonc(user / "globalStorage" / "storage.json")
    vs = VSCode(found=storage is not None)
    storage = storage or {}
    ids: dict[str, str] = {}
    locations: dict[str, str] = {}
    for p in storage.get("userDataProfiles") or []:
        if not isinstance(p, dict) or not isinstance(p.get("name"), str) or not isinstance(p.get("location"), str):
            continue
        locations[p["name"]] = p["location"]
        ids[p["location"]] = p["name"]
        if isinstance(p.get("id"), str):
            ids[p["id"]] = p["name"]
    vs.profiles = list(locations)
    default_settings = read_jsonc(user / "settings.json") or {}
    vs.env[None] = {"claude": _claude_env(default_settings), "codex": _codex_env(default_settings)}
    for name, loc in locations.items():
        s = read_jsonc(user / "profiles" / loc / "settings.json") or {}
        vs.env[name] = {"claude": _claude_env(s), "codex": _codex_env(s)}
    assoc = (storage.get("profileAssociations") or {})
    assoc = assoc.get("workspaces") if isinstance(assoc, dict) else None
    for uri, pid in (assoc or {}).items():
        path = uri_to_path(uri) if isinstance(uri, str) else None
        if path is None or not isinstance(pid, str):
            continue
        vs.folders.append((path, None if pid == VSCODE_DEFAULT_PROFILE else ids.get(pid, pid)))
    return vs


# ------------------------------------------------------------ discovery


def _default_ws() -> dict:
    return {"name": DEFAULT, "profile": None, "repos": [],
            "slots": {"claude": DEFAULT_HOMES["claude"], "codex": DEFAULT_HOMES["codex"]}, "source": "built-in"}


def discover(vs: VSCode | None = None) -> list[dict]:
    """Workspaces as found on this machine right now (reads only)."""
    vs = vs or read_vscode()
    ws: dict[str, dict] = {DEFAULT: _default_ws()}

    doc = read_jsonc(profiles_path()) or {}
    entries = doc.get("profiles") if isinstance(doc.get("profiles"), list) else []
    for e in entries:
        if not isinstance(e, dict):
            continue
        cdir = e.get("config_dir")
        if not isinstance(cdir, str) or not cdir.strip() or is_experiment(cdir):
            continue
        repo = e.get("repo") if isinstance(e.get("repo"), str) and e["repo"].strip() else None
        if is_default_slot("claude", cdir):
            if repo and not any(same_path(repo, r) for r in ws[DEFAULT]["repos"]):
                ws[DEFAULT]["repos"].append(repo)
            continue
        name = str(e.get("name") or "").strip() or basename(cdir)
        if name in ws:
            continue  # a second entry under one name: the first wins
        ws[name] = {"name": name, "profile": e.get("profile") if isinstance(e.get("profile"), str) else None,
                    "repos": [repo] if repo else [], "slots": {"claude": cdir}, "source": "profiles.json"}

    for pname in vs.profiles:
        env = vs.env.get(pname) or {}
        cdir = env.get("claude") if env.get("claude") and not is_experiment(env["claude"]) else None
        xdir = env.get("codex") if env.get("codex") and not is_experiment(env["codex"]) else None
        if not cdir and not xdir:
            continue  # a Profile that sets nothing uses the default logins
        target = None
        if cdir:
            target = next((w for w in ws.values() if same_path(w["slots"].get("claude"), cdir)), None)
        if target is None:
            target = next((w for w in ws.values() if w["profile"] == pname), None)
        if target is None:
            if pname in ws:
                continue
            target = ws[pname] = {"name": pname, "profile": pname, "repos": vs.folders_of(pname),
                                  "slots": {"claude": cdir} if cdir else {}, "source": "vscode"}
        if target["profile"] is None and target["name"] != DEFAULT:
            target["profile"] = pname
        if xdir and "codex" not in target["slots"]:
            target["slots"]["codex"] = xdir
    return list(ws.values())


# ------------------------------------------------------------ registry


def load_registry() -> dict | None:
    """workspaces.json; None when missing. A damaged file raises ValueError (never overwritten)."""
    p = registry_path()
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:
        raise ValueError(f"{p} can't be read: {e}") from None
    try:
        doc = json.loads(text)
    except ValueError:
        raise ValueError(f"{p} is not valid JSON; fix or delete it, then run `redline ws scan`") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("workspaces"), list):
        raise ValueError(f"{p} has no \"workspaces\" list; fix or delete it, then run `redline ws scan`")
    doc["workspaces"] = [w for w in doc["workspaces"] if isinstance(w, dict) and isinstance(w.get("name"), str)]
    return doc


def _save_registry(workspaces: list[dict]) -> None:
    p = registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps({"version": VERSION, "workspaces": workspaces}, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    tmp.replace(p)


def _pinned(w: dict) -> set[str]:
    u = w.get("user")
    if u is True or w.get("source") == "user":
        return set(FIELDS)
    if isinstance(u, list):
        return {f for f in u if f in FIELDS}
    return set()


def _fmt(v) -> str:
    if isinstance(v, list):
        return "[" + ", ".join(map(str, v)) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k}: {x}" for k, x in v.items()) + "}"
    return "none" if v is None else str(v)


def merge(old: list[dict], found: list[dict]) -> tuple[list[dict], list[str]]:
    """Fold a fresh discovery into the registry. Returns (workspaces, change lines)."""
    left = {w["name"]: w for w in old}
    out, changes = [], []
    for f in found:
        o = left.pop(f["name"], None)
        if o is None:
            out.append(f)
            changes.append(f"+ {f['name']}")
            continue
        pinned, w = _pinned(o), dict(o)
        for k in FIELDS:
            if o.get(k) == f.get(k):
                continue
            if k in pinned:
                changes.append(f"= {f['name']}: kept your {k} {_fmt(o.get(k))} (scan found {_fmt(f.get(k))})")
            else:
                changes.append(f"~ {f['name']}: {k} {_fmt(o.get(k))} -> {_fmt(f.get(k))}")
                w[k] = f.get(k)
        if not pinned and o.get("source") != f.get("source"):
            w["source"] = f.get("source")
        out.append(w)
    for name, o in left.items():
        if _pinned(o):
            out.append(o)
        else:
            changes.append(f"- {name}: no longer found")
    if not any(w["name"] == DEFAULT for w in out):
        out.insert(0, _default_ws())
    return out, changes


def scan(dry_run: bool = False, vs: VSCode | None = None) -> tuple[list[dict], list[str]]:
    """Rediscover and update workspaces.json (never VS Code's files or profiles.json)."""
    found = discover(vs)
    with EDIT_LOCK:
        reg = load_registry()
        old = reg["workspaces"] if reg else []
        merged, changes = merge(old, found)
        if not dry_run and (reg is None or merged != old):
            _save_registry(merged)
    return merged, changes


def load_workspaces(vs: VSCode | None = None) -> list[dict]:
    """The registry; the first use on a machine runs a scan to create it."""
    reg = load_registry()
    if reg is None:
        return scan(vs=vs)[0]
    wss = reg["workspaces"]
    if not any(w["name"] == DEFAULT for w in wss):
        wss.insert(0, _default_ws())
    return wss


def workspace_for(path: str, workspaces: list[dict]) -> dict | None:
    """The workspace whose repo root contains `path` (deepest root wins), like git finding .git."""
    here = path_key(path)
    best, depth = None, -1
    for w in workspaces:
        for r in w.get("repos") or []:
            root = path_key(r)
            if here == root or here.startswith(root.rstrip(os.sep) + os.sep):
                if len(root) > depth:
                    best, depth = w, len(root)
    return best


# ------------------------------------------------------------ who is signed in where


@dataclass
class Slot:
    provider: str
    dir: str
    email: str | None = None
    account: Account | None = None
    signed_in: bool = False


def _same_email(a: str | None, b: str | None) -> bool:
    return bool(a and b) and a.strip().casefold() == b.strip().casefold()


def _claude_slot(d: str, accounts: list[Account]) -> Slot:
    from .login import expected_email
    from .providers import identity
    from .slots import email_in

    s = Slot("claude", d)
    s.email = email_in("claude", d)  # .claude.json's oauthAccount (~/.claude.json for the default dir)
    # The login file is only checked for existence, never opened.
    s.signed_in = bool(s.email) and (sys.platform == "darwin" or (Path(d) / ".credentials.json").exists())
    if s.email:
        mine = [a for a in accounts if a.provider == "claude"]
        s.account = (next((a for a in mine if _same_email(expected_email(a), s.email)), None)
                     or next((a for a in mine if _same_email(identity(a), s.email)), None))
    return s


def _codex_slot(d: str, accounts: list[Account], usage: dict) -> Slot:
    """Codex keeps the email only inside its tokens, so go by which account's login lives here."""
    from .login import expected_email
    from .slots import effective

    s = Slot("codex", d)
    s.signed_in = (Path(d) / "auth.json").exists()
    for a in accounts:
        if a.provider != "codex":
            continue
        try:
            here = same_path(str(effective(a).home_path), d)
        except Exception:
            here = False
        if here:
            s.account = a
            cached = usage.get((a.provider, a.name))
            s.email = expected_email(a) or (cached.email if cached else None)
            break
    return s


def read_slot(provider: str, d: str, accounts: list[Account], usage: dict) -> Slot:
    d = expand(d)
    return _claude_slot(d, accounts) if provider == "claude" else _codex_slot(d, accounts, usage)


def cached_usage() -> dict:
    """{(provider, account): Usage} from the monitor's last good numbers (no network)."""
    from .models import Usage

    try:
        docs = json.loads((redline_home() / "last_usage.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for d in docs if isinstance(docs, list) else []:
        try:
            out[(d["provider"], d["account"])] = Usage.from_dict(d)
        except (KeyError, TypeError, ValueError):
            continue
    return out


# ------------------------------------------------------------ status and drift


def _drift(kind: str, ws: str, text: str, hint: str, short: str, tip: str, provider: str | None = None) -> dict:
    """`text` / `hint` are for the terminal (may name folders); `short` / `tip` are for the console
    (names only, never a path)."""
    return {"kind": kind, "workspace": ws, "provider": provider, "text": text, "hint": hint,
            "short": short, "tip": tip}


def _quote(p: str) -> str:
    return f'"{p}"' if " " in p else p


def _with_env(var: str, value: str, cmd: str) -> str:
    if sys.platform == "win32":
        return f"$env:{var}='{value}'; {cmd}"
    return f"{var}={_quote(value)} {cmd}"


def _profile_label(p: str | None) -> str:
    return p or "Default"


def status(vs: VSCode | None = None, accounts: list[Account] | None = None, usage: dict | None = None) -> list[dict]:
    """Every workspace with who is signed in to each slot, and what has drifted."""
    vs = vs or read_vscode()
    wss = load_workspaces(vs)
    accounts = load_accounts() if accounts is None else accounts
    usage = cached_usage() if usage is None else usage
    ws_of_profile = {w["profile"]: w["name"] for w in wss if w.get("profile")}

    out = []
    for w in wss:
        name, profile = w["name"], w.get("profile")
        repos = [expand(r) for r in w.get("repos") or []]
        slots = {p: read_slot(p, d, accounts, usage) for p, d in (w.get("slots") or {}).items()
                 if p in DEFAULT_HOMES and isinstance(d, str) and d}
        drift: list[dict] = []
        for p, s in slots.items():
            default_dir = is_default_slot(p, s.dir)
            if s.signed_in or (default_dir and not Path(s.dir).is_dir()):
                continue  # an untouched default dir: that provider just isn't used here
            login = "claude auth login" if p == "claude" else "codex login"
            hint = login if default_dir else _with_env(HOME_ENV_VARS[p], s.dir, login)
            drift.append(_drift("no_login", name, f"{p}: not signed in", hint,
                                f"{name} 的 {p.capitalize()} 未登录", f"在 {name} 的窗口里登录一个 {p.capitalize()} 账号",
                                provider=p))
        if vs.found:
            for r in repos:
                known, at = vs.profile_of(r)
                if not known or at == profile or ws_of_profile.get(at, DEFAULT) == name:
                    continue
                drift.append(_drift(
                    "wrong_profile", name, f"{basename(r)} opens in the {_profile_label(at)} profile",
                    f"code --profile {_quote(_profile_label(profile))} {_quote(r)}",
                    f"{basename(r)} 在 {_profile_label(at)} Profile 中打开",
                    f"用 {_profile_label(profile)} Profile 重新打开 {basename(r)}"))
            extra = [f for f in vs.folders_of(profile) if not any(same_path(f, r) for r in repos)] if profile else []
            if extra:
                names = ", ".join(basename(f) for f in extra)
                drift.append(_drift(
                    "extra_folders", name, f"+{len(extra)} folders also open in profile {profile}: "
                    + ", ".join(extra),
                    f"list them under {name}'s repos in workspaces.json, or reopen with code --profile Default <folder>",
                    f"{name} +{len(extra)} 个文件夹", f"也在 {profile} Profile 中打开：{names}"))
            if profile and profile not in vs.profiles:
                drift.append(_drift("profile", name, f"VS Code has no profile {profile}",
                                    f"create it: code --profile {_quote(profile)}",
                                    f"找不到 {profile} Profile", f"VS Code 里没有名为 {profile} 的 Profile"))
            elif profile and "claude" in slots:
                set_dir = (vs.env.get(profile) or {}).get("claude")
                if not same_path(set_dir or DEFAULT_HOMES["claude"], slots["claude"].dir):
                    drift.append(_drift(
                        "profile", name, f"profile {profile} points Claude at "
                        f"{set_dir or 'the default dir'}, not {slots['claude'].dir}",
                        f"set CLAUDE_CONFIG_DIR in claudeCode.environmentVariables of profile {profile}",
                        f"{profile} Profile 没有指向 {name}", f"{profile} Profile 的 Claude 用的是另一个目录"))
        out.append({"name": name, "profile": profile, "repos": repos, "source": w.get("source"),
                    "extra_folders": [f for f in vs.folders_of(profile)
                                      if not any(same_path(f, r) for r in repos)] if profile else [],
                    "slots": slots, "drift": drift})

    # Two workspaces signed in to one account share one quota.
    holders: dict[tuple[str, str], list[str]] = {}
    for w in out:
        for p, s in w["slots"].items():
            who = s.account.key if s.account else (s.email or "").casefold()
            if s.signed_in and who:
                holders.setdefault((p, who), []).append(w["name"])
    for w in out:
        for p, s in w["slots"].items():
            who = s.account.key if s.account else (s.email or "").casefold()
            others = [n for n in holders.get((p, who), []) if n != w["name"]] if s.signed_in and who else []
            if others:
                s_label = s.email or (s.account.name if s.account else "?")
                group = "、".join(sorted([w["name"], *others]))
                w["drift"].append(_drift(
                    "shared", w["name"], f"{p}: same account as {', '.join(others)} (shared quota)",
                    f"sign {w['name']} or {others[0]} in with another account",
                    f"{group} 共享额度", f"{group} 登录的是同一个 {p.capitalize()} 账号 {s_label}", provider=p))
    return out


def _window(u, name: str) -> dict | None:
    w = next((w for w in (u.windows if u else []) if w.name == name), None)
    if w is None:
        return None
    return {"used_percent": w.used_percent, "resets_at": w.resets_at.isoformat() if w.resets_at else None}


def to_json(rows: list[dict], usage: dict, current: str | None = None) -> dict:
    """`ws status --json`."""
    def slot(s: Slot) -> dict:
        u = usage.get((s.account.provider, s.account.name)) if s.account else None
        return {"dir": s.dir, "signed_in": s.signed_in, "email": s.email,
                "account": s.account.name if s.account else None,
                "usage": {"5h": _window(u, "5h"), "7d": _window(u, "7d"),
                          "fetched_at": u.fetched_at.isoformat(), "stale": u.stale} if u else None}

    return {"current": current, "workspaces": [
        {"name": w["name"], "profile": w["profile"], "repos": w["repos"], "extra_folders": w["extra_folders"],
         "source": w["source"], "slots": {p: slot(s) for p, s in w["slots"].items()},
         "drift": [{k: d[k] for k in ("kind", "provider", "text", "hint")} for d in w["drift"]]}
        for w in rows]}


# ------------------------------------------------------------ terminal rendering


def _left(dt: datetime | None, now: datetime) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = int((dt - now).total_seconds())
    if secs <= 0:
        return ""
    d, rem = divmod(secs, 86400)
    h, m = rem // 3600, rem % 3600 // 60
    return f"↻{d}d" if d else f"↻{h}h{m:02d}m" if h else f"↻{m}m"


def _usage_text(u, now: datetime) -> str:
    if u is None:
        return ""
    parts = []
    for name in ("5h", "7d"):
        w = next((w for w in u.windows if w.name == name), None)
        if w is None or w.used_percent is None:
            continue
        ended = w.resets_at is not None and (w.resets_at if w.resets_at.tzinfo else
                                             w.resets_at.replace(tzinfo=timezone.utc)) <= now
        pct = 0 if (u.stale and ended) else w.used_percent
        parts.append(f"{name} {pct:>3.0f}% {'' if ended else _left(w.resets_at, now):<7}")
    return "  ".join(parts)


def render(rows: list[dict], usage: dict, all_rows: bool = True) -> str:
    now = datetime.now(timezone.utc)
    lines = []
    for w in rows:
        if w["name"] == DEFAULT and not w["repos"]:
            where = "(all other folders)"
        else:
            where = "  ".join(w["repos"]) or "(no repo)"
        if w["extra_folders"]:
            where += f"  +{len(w['extra_folders'])} folders"
        prof = f"profile {w['profile']}" if w["profile"] else ""
        lines.append(f"{w['name']:<10} {where:<48} {prof}".rstrip())
        shared = {d["provider"]: d for d in w["drift"] if d["kind"] == "shared"}
        no_login = {d["provider"]: d for d in w["drift"] if d["kind"] == "no_login"}
        for p, s in w["slots"].items():
            if not s.signed_in:
                who = "— not signed in"
                if p == "codex" and is_default_slot(p, s.dir) and not Path(s.dir).is_dir():
                    continue
            else:
                who = s.email or (s.account.name if s.account else "signed in (not in redline)")
            u = usage.get((s.account.provider, s.account.name)) if s.account else None
            tail = ("= " + shared[p]["text"].split("same account as ", 1)[1] if p in shared
                    else f"! fix: {no_login[p]['hint']}" if p in no_login else "✓" if s.signed_in else "")
            lines.append(f"  {p:<8} {who:<30} {_usage_text(u, now):<34} {tail}".rstrip())
        for d in w["drift"]:
            if d["kind"] in ("shared", "no_login"):
                continue  # already on the account's line
            lines.append(f"  {'!':<8} {d['text']:<64} fix: {d['hint']}")
    return "\n".join(lines) + "\n"


def render_list(rows: list[dict]) -> str:
    lines = []
    for w in rows:
        repos = ", ".join(w["repos"]) or ("(all other folders)" if w["name"] == DEFAULT else "(no repo)")
        accts = "  ".join(f"{p}: {s.email or (s.account.name if s.account else '—') if s.signed_in else '—'}"
                          for p, s in w["slots"].items())
        lines.append(f"{w['name']:<10} {_profile_label(w['profile']) if w['profile'] or w['name'] == DEFAULT else '-':<9} "
                     f"{repos}\n{'':<21}{accts}")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------ console


def console_view(accounts: list[Account] | None = None) -> dict:
    """What the console shows: names and short notices only, never a path.

    {"workspaces": [{"name", "accounts": {provider: account name | None}, "drift": [{kind, provider, short, tip}]}],
     "notices": [the drift of all workspaces, each once], "by_account": {"claude:name": ["default", "quant"]}}
    """
    rows = status(accounts=accounts)
    by_account: dict[str, list[str]] = {}
    out, notices, seen = [], [], set()
    for w in rows:
        accts = {}
        for p, s in w["slots"].items():
            accts[p] = s.account.name if (s.signed_in and s.account) else None
            if s.signed_in and s.account:
                by_account.setdefault(s.account.key, []).append(w["name"])
        drift = [{"kind": d["kind"], "provider": d["provider"], "short": d["short"], "tip": d["tip"]}
                 for d in w["drift"]]
        out.append({"name": w["name"], "accounts": accts, "drift": drift})
        for d in drift:  # a shared login shows up in each workspace: one notice is enough
            if (d["kind"], d["short"]) not in seen:
                seen.add((d["kind"], d["short"]))
                notices.append(d)
    return {"workspaces": out, "notices": notices, "by_account": by_account}
