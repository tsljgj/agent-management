"""Website actions open chat while the usage shortcut remains separate."""

from redline import actions, browsers, login
from redline.config import Account


def test_codex_website_opens_chat_in_bound_profile(tmp_path, monkeypatch):
    acct = Account("work", "codex", str(tmp_path), browser_profile="chrome:Profile 2")
    profile = browsers.Profile("chrome", "Profile 2", "Work", "work@example.com")
    opened = []
    monkeypatch.setattr(actions, "get_account", lambda name, provider: acct)
    monkeypatch.setattr(browsers, "find_profile", lambda spec, profiles=None: profile)
    monkeypatch.setattr(browsers, "open_in_profile", lambda prof, url: opened.append((prof, url)))

    result = actions.dispatch("web", {"name": "work", "provider": "codex"})

    assert opened == [(profile, "https://chatgpt.com/")]
    assert "Work" in result


def test_codex_website_opens_chat_in_default_browser(tmp_path, monkeypatch):
    acct = Account("work", "codex", str(tmp_path))
    opened = []
    monkeypatch.setattr(login, "resolve_profile", lambda account: None)
    monkeypatch.setattr(login.webbrowser, "open", opened.append)

    login.open_web(acct)

    assert opened == ["https://chatgpt.com/"]


def test_codex_usage_shortcut_keeps_usage_page(tmp_path, monkeypatch):
    acct = Account("work", "codex", str(tmp_path))
    opened = []
    monkeypatch.setattr(actions, "get_account", lambda name, provider: acct)
    monkeypatch.setattr(login, "resolve_profile", lambda account: None)
    monkeypatch.setattr(login.webbrowser, "open", opened.append)

    actions.dispatch("web", {"name": "work", "provider": "codex", "url": browsers.USAGE_URLS["codex"]})

    assert opened == ["https://chatgpt.com/codex/settings/usage"]
