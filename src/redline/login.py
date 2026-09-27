"""One-click "wake": bring every account online.

  ok       -> nothing to do
  expired  -> refresh the token in place (under the CLI's own lock), no browser needed
  missing  -> interactive OAuth login, one account at a time, in the Chrome profile
              bound to that account (so each Google account signs in to the right one)

The browser is routed through redline itself: we set BROWSER=<redline> and
REDLINE_ACCOUNT=<name> for the login subprocess, and `redline <url>` opens the URL in
the account's Chrome profile (see browsers.py). Claude Code honours BROWSER on every
OS (`claude auth login` hands it the localhost-callback URL); Codex only off
Windows/macOS, so there we also open the URL it prints ourselves.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from typing import Callable

from . import browsers
from .config import Account, find_account, load_accounts, redline_home
from .models import ProviderError
from .procenv import child_env
from .providers import identity, load_fingerprint, refresh_account, token_state

LOGIN_TIMEOUT = 300
URL_RE = re.compile(r"https://\S+")
Log = Callable[[str, str], None]


# ------------------------------------------------------------ browser routing


def resolve_profile(acct: Account, profiles: list[browsers.Profile] | None = None) -> browsers.Profile | None:
    if acct.browser_profile:
        return browsers.find_profile(acct.browser_profile, profiles)
    for email in (identity(acct), acct.note if "@" in acct.note else None):
        if email:
            p = browsers.find_profile(email, profiles)
            if p:
                return p
    return None


def open_web(acct: Account, url: str | None = None) -> str:
    url = url or browsers.URLS[acct.provider]
    prof = resolve_profile(acct)
    if prof is None:
        webbrowser.open(url)
        return f"opened {url} in the default browser (no browser profile bound to {acct.name}; use `bind`)"
    browsers.open_in_profile(prof, url)
    return f"opened {url} in {prof.browser} profile “{prof.name}” ({prof.email or prof.directory})"


def browser_helper() -> str | None:
    """An executable that, given a URL, opens it in REDLINE_ACCOUNT's profile."""
    if getattr(sys, "frozen", False):
        return sys.executable
    return shutil.which("redline")


def handle_browser_callback(url: str) -> int:
    """Entry point when a CLI invokes us as $BROWSER."""
    url = url.strip().strip('"').strip("'")
    name = os.environ.get("REDLINE_ACCOUNT")
    try:
        acct = find_account(load_accounts(), name) if name else None
    except KeyError:
        acct = None
    try:
        if acct is not None:
            open_web(acct, url)
        else:
            webbrowser.open(url)
    except Exception:
        webbrowser.open(url)
    return 0


def is_url(arg: str) -> bool:
    return arg.strip().strip('"').strip("'").startswith(("http://", "https://"))


# ------------------------------------------------------------ CLI capabilities

_auth_login_cache: dict[str, bool] = {}


def claude_has_auth_login() -> bool:
    exe = shutil.which("claude")
    if not exe:
        return False
    if exe not in _auth_login_cache:
        try:
            out = subprocess.run([exe, "auth", "--help"], capture_output=True, text=True, timeout=20,
                                 stdin=subprocess.DEVNULL, **_no_window())
            _auth_login_cache[exe] = out.returncode == 0 and "login" in (out.stdout + out.stderr)
        except (OSError, subprocess.TimeoutExpired):
            _auth_login_cache[exe] = False
    return _auth_login_cache[exe]


def _no_window() -> dict:
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


# ------------------------------------------------------------ the job


class WakeJob:
    def __init__(self, accounts: list[Account], log: Log, force: bool = False,
                 on_done: Callable[[], None] | None = None):
        self.accounts = accounts
        self.log = log
        self.force = force  # log in again even if the token is fine (e.g. wrong account)
        self.on_done = on_done
        self.cancelled = threading.Event()
        self.proc: subprocess.Popen | None = None
        self.results: dict[str, str] = {}

    def cancel(self) -> None:
        self.cancelled.set()
        if self.proc and self.proc.poll() is None:
            self.proc.kill()

    def run(self) -> dict[str, str]:
        try:
            return self._run()
        finally:
            if self.on_done:
                try:
                    self.on_done()
                except Exception:
                    pass

    def _run(self) -> dict[str, str]:
        n = len(self.accounts)
        self.log("sys", f"wake: checking {n} account{'s' * (n != 1)}")
        queue = []
        for a in self.accounts:
            st = "missing" if self.force else token_state(a)
            if st == "ok":
                self._done(a, "ok", "ok", f"{a.name}: online" + (f" as {identity(a)}" if identity(a) else ""))
            elif st == "expired":
                try:
                    refresh_account(a)
                    self._done(a, "ok", "refreshed", f"{a.name}: token refreshed")
                except ProviderError as e:
                    self.log("warn", f"{a.name}: refresh failed ({e}); needs a login")
                    queue.append(a)
            else:
                queue.append(a)
        for i, a in enumerate(queue, 1):
            if self.cancelled.is_set():
                self.results[a.name] = "cancelled"
                continue
            self.log("sys", f"login [{i}/{len(queue)}] {a.name} ({a.provider})")
            try:
                self._login(a)
            except Exception as e:  # keep going with the next account
                self._done(a, "error", "failed", f"{a.name}: login failed: {e}")
        ok = sum(v in ("ok", "refreshed", "logged-in") for v in self.results.values())
        self.log("ok" if ok == n else "warn", f"wake done: {ok}/{n} online")
        return self.results

    def _done(self, a: Account, level: str, result: str, text: str) -> None:
        self.results[a.name] = result
        self.log(level, text)

    def _login(self, a: Account) -> None:
        profiles = browsers.all_profiles()
        prof = resolve_profile(a, profiles)
        if prof:
            self.log("info", f"{a.name}: browser -> {prof.browser} “{prof.name}” {prof.email}")
        else:
            self.log("warn", f"{a.name}: no browser profile bound, using the default browser "
                             f"(make sure it is signed in to the right Google account, or `bind {a.name} <email>`)")
        helper = browser_helper() if prof else None
        env = child_env({**a.env(), "REDLINE_ACCOUNT": a.key, "REDLINE_HOME": str(redline_home())})
        if helper:
            env["BROWSER"] = helper
        a.home_path.mkdir(parents=True, exist_ok=True)
        before = load_fingerprint(a)

        if a.provider == "claude" and claude_has_auth_login():
            cmd = ["claude", "auth", "login", "--claudeai"]
            if prof and prof.email:
                cmd += ["--email", prof.email]
            if helper or not prof:
                # Claude hands the localhost-callback URL to $BROWSER (our helper, or the
                # default browser when no profile is bound); nothing to type.
                self._spawn_hidden(cmd, env, open_urls=False, prof=prof)
            else:
                # No helper to route the browser: the printed URL is the paste-a-code
                # variant, so give the user a terminal to paste into.
                from .actions import open_terminal

                self.log("info", f"{a.name}: finish the login in the terminal window (paste the code)")
                open_terminal(a, cmd, extra_env=env)
        elif a.provider == "claude":
            from .actions import open_terminal

            self.log("info", f"{a.name}: this claude version has no `auth login`; opening a terminal with /login")
            open_terminal(a, ["claude", "/login"], extra_env=env)
        else:
            # Codex's browser opener ignores $BROWSER on Windows/macOS: open the printed URL ourselves.
            self._spawn_hidden(["codex", "login"], env,
                               open_urls=not helper or sys.platform in ("win32", "darwin"), prof=prof)

        deadline = time.monotonic() + LOGIN_TIMEOUT
        while time.monotonic() < deadline and not self.cancelled.is_set():
            fp = load_fingerprint(a)
            if fp and fp != before and token_state(a) == "ok":
                break
            if self.proc and self.proc.poll() not in (None, 0):
                raise ProviderError(f"{self.proc.args[0]} exited with {self.proc.returncode}: {self._tail()}")
            time.sleep(2)
        else:
            if self.proc and self.proc.poll() is None:
                self.proc.kill()
            if self.cancelled.is_set():
                self._done(a, "warn", "cancelled", f"{a.name}: cancelled")
            else:
                self._done(a, "error", "timeout", f"{a.name}: no login within {LOGIN_TIMEOUT // 60} min, skipped")
            return

        email = None
        for _ in range(5):  # the CLI writes the account email right after the tokens
            email = identity(a)
            if email:
                break
            time.sleep(1)
        self._done(a, "ok", "logged-in", f"{a.name}: logged in" + (f" as {email}" if email else ""))
        self._check_identity(a, email, prof)

    def _check_identity(self, a: Account, email: str | None, prof: browsers.Profile | None) -> None:
        if not email:
            return
        for other in load_accounts():
            if other.key != a.key and other.provider == a.provider and identity(other) == email:
                self.log("crit", f"{a.name} is logged in as {email} — the same account as {other.name}! "
                                 f"Sign in with the other Google account and run `login {a.name}` again.")
        if prof and prof.email and prof.email.lower() != email.lower():
            self.log("warn", f"{a.name}: logged in as {email} but its browser profile is {prof.email}")

    # -- subprocess plumbing

    def _spawn_hidden(self, cmd: list[str], env: dict, open_urls: bool, prof) -> None:
        exe = shutil.which(cmd[0])
        if not exe:
            raise ProviderError(f"{cmd[0]!r} not found on PATH")
        self._lines: list[str] = []
        self.proc = subprocess.Popen(
            [exe, *cmd[1:]], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", **_no_window(),
        )
        threading.Thread(target=self._pump, args=(open_urls, prof), daemon=True).start()

    def _pump(self, open_urls: bool, prof) -> None:
        opened = False
        for line in self.proc.stdout:
            line = line.rstrip()
            self._lines.append(line)
            m = URL_RE.search(line)
            if m and not opened and ("oauth" in m.group(0) or "authorize" in m.group(0)):
                url = m.group(0).rstrip(".,)'\"")
                if open_urls:
                    opened = True
                    try:
                        if prof:
                            browsers.open_in_profile(prof, url)
                        else:
                            webbrowser.open(url)
                    except Exception as e:
                        self.log("error", f"could not open browser: {e}; open manually: {url}")

    def _tail(self) -> str:
        return " | ".join(getattr(self, "_lines", [])[-3:])[:300]


_current: WakeJob | None = None
_lock = threading.Lock()


def start_wake(names: list[str] | None, log: Log, force: bool = False,
               on_done: Callable[[], None] | None = None, provider: str | None = None) -> str:
    """Run a wake job in the background (one at a time)."""
    global _current
    with _lock:
        if _current is not None and _current_thread and _current_thread.is_alive():
            return "a wake job is already running (use `wake cancel`)"
        accounts = load_accounts()
        if names:
            accounts = [find_account(accounts, n, provider) for n in names]
        if not accounts:
            return "no accounts; run `scan` first"
        _current = WakeJob(accounts, log, force=force, on_done=on_done)
        _start(_current)
    return f"{'logging in' if force else 'waking'} {', '.join(a.name for a in accounts)}…"


_current_thread: threading.Thread | None = None


def _start(job: WakeJob) -> None:
    global _current_thread
    _current_thread = threading.Thread(target=job.run, name="redline-wake", daemon=True)
    _current_thread.start()


def cancel_wake() -> str:
    if _current is None or not (_current_thread and _current_thread.is_alive()):
        return "no wake job running"
    _current.cancel()
    return "cancelling…"
