"""Connect a Claude account to its Claude Console org: one sign-in window, then the session is kept.

`redline console <name>` opens the Console in a pywebview (WebView2) window with this login's own
browser profile, waits for the sign-in, checks that the session belongs to this account (its
claude.ai org must be in the session's org list), picks the Console org the plan pays its credit
into, stores the session (credit.save_session) and closes the window.

The console's 连接 Console button runs the same command as a child process: pywebview fixes the
browser profile once per GUI loop, so a window in the tray's own loop would share one profile
(the console's) between every account. A child gets its own per login, and can't take the tray down.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from typing import Callable

from . import credit
from .config import Account
from .models import ProviderError

BILLING_URL = "https://platform.claude.com/settings/billing"
CONSOLE_HOST = "platform.claude.com"
SIGNIN_TIMEOUT = 15 * 60
POLL_EVERY = 2.0
PROBE_PATIENCE = 4  # polls to wait for a probe's answer before sending another
# evaluate_js doesn't await promises here: start the request, leave its answer in window.__x
PROBE_JS = ("window.__x=undefined;fetch('/api/organizations',{credentials:'include'})"
            ".then(r=>r.text().then(t=>{window.__x={s:r.status,b:t}}))"
            ".catch(e=>{window.__x={s:0,b:''}});")
READ_JS = "JSON.stringify(window.__x||null)"

# exit codes of `redline console`, and what the console says for each
OK, FAILED, MISMATCH, CANCELLED, NO_WEBVIEW, NOT_READY = 0, 1, 3, 4, 5, 6
MESSAGES = {
    OK: "Console 已连接",
    FAILED: "Console 连接失败",
    MISMATCH: "登录的是另一个账号，未保存",
    CANCELLED: "Console 登录已取消",
    NO_WEBVIEW: "需要 pywebview：pip install 'redline[tray]'",
    NOT_READY: "先登录或续期这个账号",
}


class SignInError(Exception):
    def __init__(self, code: int, detail: str = ""):
        self.code = code
        super().__init__(detail or MESSAGES.get(code, MESSAGES[FAILED]))


# ---------------------------------------------------------------- the decision (no window, no globals)


def complete(session_key: str, identity_key: str, claude_org_uuid: str | None,
             get_orgs: Callable[[str], list] = credit.get_orgs,
             get_balance: Callable[[str, str], dict] = credit.get_balance) -> str:
    """Check a captured session and store it for this login. Returns the Console org uuid.

    Raises SignInError(MISMATCH) when the session belongs to someone else: nothing is stored then.
    """
    if not session_key:
        raise SignInError(FAILED)
    orgs = get_orgs(session_key)
    if not credit.same_org(orgs, claude_org_uuid):
        raise SignInError(MISMATCH)
    balances = {}
    for o in credit.console_orgs(orgs):
        try:
            balances[o["uuid"]] = get_balance(session_key, o["uuid"])
        except ProviderError:
            balances[o["uuid"]] = None
    console_org = credit.pick_console_org(orgs, balances)
    if console_org is None:
        raise SignInError(FAILED, "这个账号没有 Console 组织")
    credit.save_session(identity_key, session_key, console_org, claude_org_uuid)
    return console_org


def session_cookie(cookies) -> str | None:
    """The Console session from window.get_cookies() (a list of http.cookies.SimpleCookie)."""
    for jar in cookies or []:
        try:
            morsel = jar.get(credit.SESSION_COOKIE)
        except AttributeError:
            continue
        if morsel is None or not morsel.value:
            continue
        domain = (morsel["domain"] or "").lstrip(".").lower()
        if domain and not (CONSOLE_HOST == domain or CONSOLE_HOST.endswith("." + domain)):
            continue
        return morsel.value
    return None


def _on_console(url) -> bool:
    from urllib.parse import urlsplit

    try:
        return (urlsplit(str(url or "")).hostname or "").lower() == CONSOLE_HOST
    except ValueError:
        return False


def watch(win, finish: Callable[[str], None], closed: Callable[[], bool] = lambda: False,
          sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
          timeout: float = SIGNIN_TIMEOUT) -> int:
    """Wait in the window until the Console answers the org list, then hand over its session.

    win needs get_current_url / run_js / evaluate_js / get_cookies (a pywebview Window).
    finish(session_key) checks and stores it (raises SignInError). Returns an exit code.
    """
    deadline = clock() + timeout
    started = False
    waited = 0  # polls since the last probe went out without an answer
    while clock() < deadline:
        sleep(POLL_EVERY)
        if closed():
            return CANCELLED
        key = None
        try:
            if not _on_console(win.get_current_url()):
                started = False  # signing in elsewhere (Google, the login page): ask again on return
                continue
            if started:
                raw = win.evaluate_js(READ_JS)
                probe = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(probe, dict) and probe.get("s") == 200:
                    key = session_cookie(win.get_cookies())
                if key:
                    break
                if probe is None and waited < PROBE_PATIENCE:  # still on its way
                    waited += 1
                    continue
            win.run_js(PROBE_JS)
            started, waited = True, 0
        except Exception:  # the page navigated mid-call, or the window is going away
            if closed():
                return CANCELLED
            started = False
    else:
        return CANCELLED
    finish(key)  # its errors (another account, the Console unreachable) end the sign-in
    return OK


# ---------------------------------------------------------------- the window (thin)


def run(account: Account) -> tuple[int, str]:
    """Open the sign-in window for this Claude account and wait for it. (exit code, message)"""
    from .providers import claude, load_identity_key
    from .slots import effective

    if account.provider != "claude":
        return FAILED, "Console 只适用于 Claude 账号"
    try:
        import webview
    except ImportError:
        return NO_WEBVIEW, MESSAGES[NO_WEBVIEW]
    identity_key = load_identity_key(account)
    try:
        claude_org = claude.org_uuid(effective(account)) if identity_key else None
    except ProviderError:
        claude_org = None
    if not identity_key or not claude_org or load_identity_key(account) != identity_key:
        return NOT_READY, MESSAGES[NOT_READY]

    storage = credit.webview_dir(identity_key)
    storage.mkdir(parents=True, exist_ok=True)
    win = webview.create_window(f"Claude Console · {account.name}", BILLING_URL, width=1000, height=760)
    state = {"closed": False, "code": CANCELLED, "detail": ""}

    def on_closed():
        state["closed"] = True

    win.events.closed += on_closed

    def finish(session_key: str) -> None:
        if load_identity_key(account) != identity_key:  # the account's login changed meanwhile
            raise SignInError(NOT_READY)
        try:
            complete(session_key, identity_key, claude_org)
        except SignInError as e:
            if e.code == MISMATCH:
                try:  # the next attempt starts signed out, not as the other account
                    win.clear_cookies()
                except Exception:
                    pass
            raise

    def body():
        try:
            state["code"] = watch(win, finish, closed=lambda: state["closed"])
        except SignInError as e:
            state["code"], state["detail"] = e.code, str(e)
        except Exception as e:  # the Console unreachable, or a bug: never leave the window hanging
            state["code"] = FAILED
            if not isinstance(e, ProviderError):
                state["detail"] = f"{MESSAGES[FAILED]}: {type(e).__name__}"
        finally:
            if not state["closed"]:
                try:
                    win.destroy()
                except Exception:
                    pass

    webview.start(body, private_mode=False, storage_path=str(storage))
    code = state["code"]
    return code, state["detail"] or MESSAGES.get(code, MESSAGES[FAILED])


# ---------------------------------------------------------------- from the console (tray / serve)

_running: set[str] = set()
_running_lock = threading.Lock()


def webview_available() -> bool:
    if getattr(sys, "frozen", False):
        return True  # bundled with the exe
    import importlib.util

    try:
        return importlib.util.find_spec("webview") is not None
    except (ImportError, ValueError):
        return False


def child_argv(ref: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "console", ref]
    return [sys.executable, "-m", "redline", "console", ref]


def start(account: Account, log: Callable[[str, str], None], on_done: Callable[[], None] | None = None,
          popen=subprocess.Popen) -> str:
    """Open the sign-in window in a child process; report the outcome to log, then on_done()."""
    from .actions import ActionError
    from .procenv import child_env

    if account.provider != "claude":
        raise ActionError("Console 只适用于 Claude 账号")
    if not webview_available():
        raise ActionError(MESSAGES[NO_WEBVIEW])
    with _running_lock:
        if account.key in _running:
            raise ActionError("登录窗口已经打开")
        _running.add(account.key)
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" and not getattr(sys, "frozen", False) else 0
        proc = popen(child_argv(account.key), env=child_env(), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    except OSError as e:
        with _running_lock:
            _running.discard(account.key)
        raise ActionError(f"{MESSAGES[FAILED]}: {e.strerror or type(e).__name__}") from None

    def wait():
        try:
            code = proc.wait()
            msg = f"{account.name}: {MESSAGES.get(code, MESSAGES[FAILED])}"
            log("ok" if code == OK else "warn" if code == CANCELLED else "error", msg)
        finally:
            with _running_lock:
                _running.discard(account.key)
            if on_done is not None:
                try:
                    on_done()
                except Exception:
                    pass

    threading.Thread(target=wait, name="redline-console-signin", daemon=True).start()
    return f"{account.name}: Console 登录窗口已打开"


def main(account: Account) -> int:
    code, msg = run(account)
    if sys.stdout is not None:
        try:
            print(msg)
        except OSError:
            pass
    return code

