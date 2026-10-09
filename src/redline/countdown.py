"""Start a Codex weekly window with one small, isolated subscription SDK turn.

Only an explicit user action calls this module. A completed turn is not proof of
activation: the official usage endpoint must subsequently report a future reset.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .clis import find_cli, fresh_path
from .config import Account, find_account, load_accounts, redline_home
from .models import Usage, weekly_countdown
from .providers import codex, fetch_usage
from .providers._util import cli_lock, read_json, write_json_atomic
from .slots import effective

SDK_VERSION = "0.162.0"
TURN_TIMEOUT = 60
TOTAL_TIMEOUT = 300
VERIFY_DELAYS = (0, 3, 8)

_lock = threading.Lock()
_install_lock = threading.Lock()
_states: dict[str, dict[str, str]] = {}
_identities: dict[str, str] = {}
_busy: set[str] = set()


class CountdownError(Exception):
    """Messages from this exception are deliberately safe to display/log."""


def status() -> dict[str, dict[str, str]]:
    with _lock:
        return {key: dict(value) for key, value in _states.items()}


def _set_state(identity: str, state: str, message: str) -> None:
    with _lock:
        for key, value in _identities.items():
            if value == identity:
                _states[key] = {"state": state, "message": message}


def _current(account: Account) -> Account:
    """Re-resolve the slot: a login may have moved since the button was clicked."""
    try:
        return effective(find_account(load_accounts(), account.key))
    except KeyError:
        raise CountdownError("Account was removed or renamed; refresh and try again.") from None


def _auth_identity(auth: dict | None) -> str:
    tokens = (auth or {}).get("tokens") or {}
    access, id_token = tokens.get("access_token"), tokens.get("id_token")
    claims = codex.jwt_claims(id_token).get(codex.AUTH_CLAIM) or {}
    account_id = tokens.get("account_id") or claims.get("chatgpt_account_id")
    if not access or not id_token or not account_id:
        raise CountdownError("A ChatGPT login with an account identity is required; log in first.")
    claim_id = claims.get("chatgpt_account_id")
    if claim_id and claim_id != account_id:
        raise CountdownError("The saved ChatGPT account identity is inconsistent; log in again.")
    return hashlib.sha256(str(account_id).encode()).hexdigest()


def start_countdown(
    name: str,
    log: Callable[[str, str], None],
    on_done: Callable[[], None] | None = None,
    provider: str | None = "codex",
) -> str:
    """Queue one turn; repeated clicks and aliases of one ChatGPT login share a lock."""
    try:
        account = find_account(load_accounts(), name, provider)
    except KeyError as exc:
        raise CountdownError(exc.args[0]) from None
    if account.provider != "codex":
        raise CountdownError("Weekly activation applies to Codex ChatGPT accounts only.")
    current = _current(account)
    with cli_lock(current.home_path):
        identity = _auth_identity(read_json(current.home_path / "auth.json"))
    with _lock:
        _identities[account.key] = identity
        if identity in _busy:
            active = next((_states[k] for k, v in _identities.items()
                           if v == identity and k in _states), None)
            _states[account.key] = dict(active or {"state": "starting", "message": "Checking weekly quota."})
            return "This ChatGPT account already has a weekly activation in progress."
        _busy.add(identity)
        _states[account.key] = {"state": "starting", "message": "Checking current weekly quota."}
    worker = threading.Thread(target=_work, args=(account, identity, log, on_done),
                              name="redline-codex-countdown", daemon=True)
    try:
        worker.start()
    except Exception:
        with _lock:
            _busy.discard(identity)
            _states[account.key] = {"state": "error", "message": "Could not start the background job; try again."}
        raise CountdownError("Could not start the background job; try again.") from None
    return f"{account.name}: checking weekly quota, then sending one small Codex SDK request if needed."


def _safe_log(log, level: str, text: str) -> None:
    try:
        log(level, text)
    except Exception:
        pass


def _preflight(usage: Usage) -> bool:
    """True means a turn is needed; an active 0% window already counts down."""
    state = weekly_countdown(usage)["state"]
    if state == "active":
        return False
    weekly = next((w for w in usage.windows if w.name == "7d"), None)
    if not usage.ok or usage.stale:
        raise CountdownError("Cannot read current Codex quota; refresh usage or log in before starting.")
    if weekly is None or weekly.used_percent is None:
        raise CountdownError("No usable weekly quota is reported for this account; refresh usage first.")
    # A zero percent fresh window with missing/expired reset is uncertain, but the
    # user's explicit button click may attempt one turn and then verify it.
    if state not in ("unstarted", "unknown") or weekly.used_percent != 0:
        raise CountdownError("Weekly state is uncertain and usage is nonzero; refresh usage before starting.")
    if any(w.name == "5h" and w.used_percent is not None and w.used_percent >= 100 for w in usage.windows):
        raise CountdownError("The 5h quota is exhausted; wait for its reset before starting weekly.")
    return True


def _remaining(deadline: float, maximum: float) -> float:
    remaining = min(maximum, deadline - time.monotonic())
    if remaining <= 0:
        raise CountdownError("Weekly activation timed out; refresh usage before retrying.")
    return remaining


def _check_identity(account: Account, identity: str) -> None:
    current = _current(account)
    with cli_lock(current.home_path):
        if _auth_identity(read_json(current.home_path / "auth.json")) != identity:
            raise CountdownError("This account's login changed; refresh and try again.")


def _fetch_checked(account: Account, identity: str, deadline: float) -> Usage:
    # A usage request can include a refresh plus a retry (three 20s HTTP calls).
    # Reserve that bounded time instead of letting the last poll overrun the job.
    if _remaining(deadline, 70) < 70:
        raise CountdownError("Weekly activation timed out; refresh usage before retrying.")
    _check_identity(account, identity)
    usage = fetch_usage(account, refresh_tokens=True)
    _check_identity(account, identity)
    _remaining(deadline, 1)
    return usage


def _work(account: Account, identity: str, log, on_done) -> None:
    deadline = time.monotonic() + TOTAL_TIMEOUT
    try:
        _safe_log(log, "info", f"{account.name}: checking Codex weekly quota")
        if not _preflight(_fetch_checked(account, identity, deadline)):
            _set_state(identity, "success", "Weekly countdown is already active; no SDK request was sent.")
            _safe_log(log, "ok", f"{account.name}: weekly countdown is already active")
            return
        prepared_at = time.monotonic()
        node, entry = _ensure_sdk(deadline, log)
        if time.monotonic() - prepared_at > 15:
            if not _preflight(_fetch_checked(account, identity, deadline)):
                _set_state(identity, "success", "Weekly countdown is already active; no SDK request was sent.")
                return
        _remaining(deadline, TURN_TIMEOUT)
        with tempfile.TemporaryDirectory(prefix="redline-codex-") as temp:
            root = Path(temp)
            home, cwd = root / "codex", root / "work"
            home.mkdir(mode=0o700)
            cwd.mkdir(mode=0o700)
            _snapshot_auth(account, identity, home)
            _set_state(identity, "starting", "Sending one small Codex SDK request.")
            _run_turn(node, entry, home, cwd, root, deadline)
        _set_state(identity, "verifying", "SDK request finished; verifying the weekly reset time.")
        _safe_log(log, "info", f"{account.name}: SDK request finished; verifying weekly countdown")
        for delay in VERIFY_DELAYS:
            _remaining(deadline, delay + 1)
            if delay:
                time.sleep(delay)
            usage = _fetch_checked(account, identity, deadline)
            if weekly_countdown(usage)["state"] == "active":
                _set_state(identity, "success", "Weekly countdown is active; confirmed by Codex usage.")
                _safe_log(log, "ok", f"{account.name}: weekly countdown confirmed active")
                return
        raise CountdownError("SDK request finished, but no active weekly reset is confirmed yet; refresh usage before retrying.")
    except CountdownError as exc:
        _set_state(identity, "error", str(exc))
        _safe_log(log, "error", f"{account.name}: {exc}")
    except Exception:
        # SDK/HTTP exceptions may contain tokens, response bodies or raw stderr.
        message = "Could not start or verify the weekly countdown; refresh usage and try again."
        _set_state(identity, "error", message)
        _safe_log(log, "error", f"{account.name}: {message}")
    finally:
        with _lock:
            _busy.discard(identity)
        if on_done:
            try:
                on_done()
            except Exception:
                pass


def _snapshot_auth(account: Account, identity: str, home: Path) -> None:
    for attempt in range(2):
        current = _current(account)
        with cli_lock(current.home_path):
            auth = read_json(current.home_path / "auth.json")
            if _auth_identity(auth) != identity:
                raise CountdownError("This account's login changed; refresh and try again.")
            tokens = auth["tokens"]
            exp = codex.jwt_claims(tokens["access_token"]).get("exp")
            needs_refresh = isinstance(exp, (int, float)) and exp < time.time() + TURN_TIMEOUT + 30
            if not needs_refresh:
                # Empty refresh_token satisfies the CLI's token schema but cannot
                # rotate the real login. No configuration or API key is copied.
                snapshot = {"auth_mode": "chatgpt", "OPENAI_API_KEY": None,
                            "tokens": {k: tokens.get(k) for k in ("access_token", "id_token", "account_id")},
                            "last_refresh": datetime.now(timezone.utc).isoformat()}
                snapshot["tokens"]["account_id"] = tokens.get("account_id") or (
                    codex.jwt_claims(tokens["id_token"]).get(codex.AUTH_CLAIM) or {}).get("chatgpt_account_id")
                snapshot["tokens"]["refresh_token"] = ""
                write_json_atomic(home / "auth.json", snapshot)
                return
        if attempt:
            raise CountdownError("ChatGPT credentials are still expired; log in again before starting.")
        # The provider refresh acquires its existing lock and persists the rotated
        # refresh token at the original home; the SDK never receives that token.
        codex.refresh(current, auth)


def _clean_env(root: Path, codex_home: Path | None = None) -> dict[str, str]:
    keep = {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "NUMBER_OF_PROCESSORS",
            "PROCESSOR_ARCHITECTURE", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR",
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
    env = {k: v for k, v in os.environ.items() if k.upper() in keep}
    env.update({"PATH": fresh_path(), "HOME": str(root), "USERPROFILE": str(root),
                "APPDATA": str(root / "appdata"), "LOCALAPPDATA": str(root / "local"),
                "XDG_CONFIG_HOME": str(root / "config"), "XDG_DATA_HOME": str(root / "data"),
                "XDG_CACHE_HOME": str(root / "cache"), "TMP": str(root), "TEMP": str(root),
                "TMPDIR": str(root)})
    if codex_home:
        env["CODEX_HOME"] = str(codex_home)
    return env


def _process(args: list[str], *, env: dict[str, str], cwd: Path, timeout: float) -> subprocess.CompletedProcess:
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {"start_new_session": True}
    process = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **flags)
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            if sys.platform == "win32":
                subprocess.run([str(Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32/taskkill.exe"),
                                "/PID", str(process.pid), "/T", "/F"], capture_output=True,
                               timeout=5, creationflags=subprocess.CREATE_NO_WINDOW, check=False)
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, subprocess.TimeoutExpired):
            pass
        finally:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                if process.stdout:
                    process.stdout.close()
        raise CountdownError("Weekly activation timed out; refresh usage before retrying.") from None
    return subprocess.CompletedProcess(args, process.returncode, stdout)


def _npm_script(node: str) -> Path | None:
    candidates = [Path(node).parent / "node_modules/npm/bin/npm-cli.js"]
    for name in ("npm", "npm.cmd"):
        npm = find_cli(name)
        if npm:
            path = Path(npm).resolve()
            candidates += [path, path.parent / "node_modules/npm/bin/npm-cli.js",
                           path.parent.parent / "lib/node_modules/npm/bin/npm-cli.js"]
    return next((p for p in candidates if p.name == "npm-cli.js" and p.is_file()), None)


def _ensure_sdk(deadline: float, log) -> tuple[str, Path]:
    node = find_cli("node")
    if not node:
        raise CountdownError("Install Node.js 18 or newer to enable the Codex SDK button.")
    target = redline_home() / "codex-sdk" / SDK_VERSION
    entry = target / "node_modules/@openai/codex-sdk/dist/index.js"
    package = target / "node_modules/@openai/codex-sdk/package.json"
    with _install_lock:
        _remaining(deadline, 1)
        try:
            ready = entry.is_file() and json.loads(package.read_text(encoding="utf-8")).get("version") == SDK_VERSION
        except (OSError, ValueError):
            ready = False
        if not ready:
            npm = _npm_script(node)
            if not npm:
                raise CountdownError("npm was not found; install Node.js with npm to enable the SDK button.")
            target.mkdir(parents=True, exist_ok=True)
            _safe_log(log, "info", f"Preparing the local Codex SDK {SDK_VERSION} (first use only)")
            with tempfile.TemporaryDirectory(prefix="redline-npm-") as temp:
                env = _clean_env(Path(temp))
                env["NPM_CONFIG_USERCONFIG"] = str(Path(temp) / "npmrc")
                result = _process([node, str(npm), "install", "--prefix", str(target),
                                   "--registry=https://registry.npmjs.org", "--ignore-scripts", "--no-audit",
                                   "--no-fund", "--save-exact", f"@openai/codex-sdk@{SDK_VERSION}"],
                                  env=env, cwd=target, timeout=_remaining(deadline, 120))
            if result.returncode or not entry.is_file():
                raise CountdownError("Could not install the local Codex SDK; check npm/network and try again.")
    return node, entry


def _run_turn(node: str, entry: Path, home: Path, cwd: Path, root: Path, deadline: float) -> None:
    runner = Path(__file__).parent / "assets/start_codex.mjs"
    result = _process([node, str(runner), str(entry), str(cwd), str(TURN_TIMEOUT)],
                      env=_clean_env(root, home), cwd=cwd, timeout=_remaining(deadline, TURN_TIMEOUT + 10))
    try:
        response = json.loads(result.stdout.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        response = {}
    if result.returncode or response.get("ok") is not True:
        message = {"auth": "Codex rejected the temporary ChatGPT credentials; log in again and retry.",
                   "timeout": "The Codex SDK request timed out; refresh usage before retrying.",
                   "unexpected-tools": "The SDK attempted a tool call; the request was stopped. Refresh usage before retrying."}
        raise CountdownError(message.get(response.get("error"),
                             "The Codex SDK request failed; check login/network and retry."))
