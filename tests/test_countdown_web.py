import json
import urllib.request

import pytest

from redline import actions
from redline.config import Account, save_accounts
from redline.monitor import Monitor
from redline.web import ConsoleServer, TOKEN_HEADER


@pytest.fixture
def countdown_server(tmp_path, monkeypatch):
    monkeypatch.setenv("REDLINE_HOME", str(tmp_path))
    monitor = Monitor(collect=lambda accounts, refresh_tokens=False: [])
    server = ConsoleServer(monitor).start_background()
    yield server
    server.shutdown()
    server.httpd.server_close()


def post(server, body):
    req = urllib.request.Request(server.url + "api/action", data=json.dumps(body).encode(),
                                 headers={TOKEN_HEADER: server.token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.load(response)


def test_countdown_action_uses_server_callbacks(countdown_server, monkeypatch):
    from redline import countdown

    received = {}

    def start(name, log, on_done=None, provider=None):
        received.update(name=name, provider=provider, on_done=on_done)
        log("warn", "weekly 尚未开始")
        return "started"

    monkeypatch.setattr(countdown, "start_countdown", start)
    result = post(countdown_server, {"action": "start-countdown", "provider": "codex",
                                     "name": "work", "log": "client value", "on_done": "client value"})
    assert result == {"ok": True, "message": "started"}
    assert received["name"] == "work" and received["provider"] == "codex"
    assert callable(received["on_done"])
    assert countdown_server.monitor.events[-1]["text"] == "weekly 尚未开始"


def test_countdown_status_is_returned_in_usage(countdown_server, monkeypatch):
    from redline import countdown

    state = {"codex:work": {"state": "verifying", "message": "checking"}}
    monkeypatch.setattr(countdown, "status", lambda: state)
    request = urllib.request.Request(countdown_server.url + "api/usage",
                                     headers={TOKEN_HEADER: countdown_server.token})
    with urllib.request.urlopen(request, timeout=5) as response:
        payload = json.load(response)
    assert payload["meta"]["countdown"] == state
    assert "countdown" not in countdown_server.monitor.meta  # response only


def test_countdown_validation_error_is_a_json_result(countdown_server, monkeypatch):
    from redline import countdown

    def start(*args, **kwargs):
        raise countdown.CountdownError("A ChatGPT login is required")

    monkeypatch.setattr(countdown, "start_countdown", start)
    result = post(countdown_server, {"action": "start-countdown", "name": "work", "provider": "codex"})
    assert result == {"ok": False, "message": "A ChatGPT login is required"}


def test_countdown_completion_requests_a_toast(countdown_server, monkeypatch):
    from redline import countdown

    def start(name, log, **kwargs):
        log("ok", "weekly countdown confirmed active")
        return "finished"

    monkeypatch.setattr(countdown, "start_countdown", start)
    assert post(countdown_server, {"action": "start-countdown", "name": "work"})["ok"]
    assert countdown_server.monitor.events[-1]["alert"]


def test_readonly_server_refuses_countdown(countdown_server, monkeypatch):
    from redline import countdown

    countdown_server.allow_actions = False
    monkeypatch.setattr(countdown, "start_countdown", lambda *a, **kw: pytest.fail("must not run"))
    request = urllib.request.Request(countdown_server.url + "api/action",
                                     data=b'{"action":"start-countdown","name":"work"}',
                                     headers={TOKEN_HEADER: countdown_server.token})
    from urllib.error import HTTPError
    with pytest.raises(HTTPError) as result:
        urllib.request.urlopen(request, timeout=5)
    assert result.value.code == 403


def test_usage_action_uses_bound_account(tmp_path, monkeypatch):
    from redline import browsers, login

    account = Account("work", "codex", str(tmp_path))
    calls = []
    monkeypatch.setattr(actions, "get_account", lambda name, provider: account)
    monkeypatch.setattr(login, "open_web", lambda acct, url: calls.append((acct, url)) or "opened")
    actions.dispatch("web", {"name": "work", "provider": "codex", "usage": True})
    assert calls == [(account, browsers.USAGE_URLS["codex"])]


def test_shared_quota_metadata_uses_identity_not_display_name(tmp_path, monkeypatch):
    monkeypatch.setenv("REDLINE_HOME", str(tmp_path / "config"))
    from redline import browsers, sessions

    accounts = [Account(name, "codex", str(tmp_path / name)) for name in ("a", "b", "c")]
    for account, account_id in zip(accounts, ("private-bucket-id", "private-bucket-id", "different")):
        account.home_path.mkdir(parents=True)
        account.home_path.joinpath("auth.json").write_text(json.dumps({
            "tokens": {"access_token": "test", "account_id": account_id}}))
    save_accounts(accounts)
    monkeypatch.setattr(browsers, "all_profiles", lambda: [])
    monkeypatch.setattr(sessions, "recent", lambda account: [])
    metadata = actions.account_details()
    assert metadata["codex:a"]["shared_quota_with"] == ["b"]
    assert metadata["codex:b"]["shared_quota_with"] == ["a"]
    assert metadata["codex:c"]["shared_quota_with"] == []
    assert "private-bucket-id" not in json.dumps(metadata)
