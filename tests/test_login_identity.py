"""Configured Codex identities stay visible when the saved login belongs to someone else."""

import pytest

from redline import actions, browsers, codex_oauth, login
from redline.config import Account, load_accounts, save_accounts
from test_accounts import home, make_codex  # noqa: F401 (isolated autouse fixture)


@pytest.mark.parametrize("name,note,expected", [
    ("alias", " intended@example.test ", "intended@example.test"),
    ("named@example.test", "personal account", "named@example.test"),
    ("named@example.test", "note@example.test", "note@example.test"),
    ("alias", "ask owner@example.test", None),
])
def test_expected_identity_comes_from_email_fields(home, name, note, expected):
    assert login.expected_email(Account(name, "codex", str(home / "account"), note=note)) == expected


def test_corrective_profile_resolution_follows_intended_email(home):
    acct = Account("free@example.test", "codex", str(make_codex(home / "account", email="pro@example.test")))
    wrong = browsers.Profile("chrome", "Default", "Pro", "pro@example.test")
    intended = browsers.Profile("chrome", "Profile 2", "Free", "free@example.test")
    assert login.resolve_profile(acct, [wrong, intended]) is intended
    # Do not route a corrective login back to the known wrong identity.
    assert login.resolve_profile(acct, [wrong]) is None
    acct.browser_profile = wrong.spec
    assert login.resolve_profile(acct, [wrong, intended]) is wrong


def test_details_distinguish_wrong_login_from_aliases_and_shared_quota(home, monkeypatch):
    from redline import sessions

    monkeypatch.setattr(sessions, "recent", lambda a: [])
    accts = [
        Account("free@example.test", "codex", str(make_codex(home / "wrong", email="pro@example.test"))),
        Account("Work", "codex", str(make_codex(home / "alias", email="alias@example.test"))),
        Account("CASE@EXAMPLE.TEST", "codex", str(make_codex(home / "case", email="case@example.test"))),
        Account("missing@example.test", "codex", str(home / "missing")),
    ]
    save_accounts(accts)
    details = actions.account_details()
    wrong = details[accts[0].key]
    assert wrong["email"] == "pro@example.test"
    assert wrong["expected_email"] == "free@example.test" and wrong["login_mismatch"]
    assert wrong["shared_quota_with"]  # Same workspace ID alone must not create a mismatch.
    for acct in accts[1:]:
        assert not details[acct.key]["login_mismatch"]
    assert details[accts[1].key]["expected_email"] == ""


@pytest.mark.parametrize("name,note", [
    ("free@example.test", ""),
    ("temporary", "free@example.test"),
])
def test_auto_naming_does_not_hide_wrong_identity(home, name, note):
    acct = Account(name, "codex", str(make_codex(home / "wrong", email="pro@example.test")),
                   note=note, auto_name=True)
    save_accounts([acct])
    assert actions.adopt_email_names() == []
    assert load_accounts()[0].name == name


@pytest.mark.parametrize("expires_in", [3600, -10])
def test_wake_does_not_accept_valid_or_expired_wrong_identity(home, monkeypatch, expires_in):
    acct = Account("free@example.test", "codex",
                   str(make_codex(home / "wrong", email="pro@example.test", exp_in=expires_in)))
    save_accounts([acct])
    monkeypatch.setattr(login, "refresh_account", lambda *a, **k: pytest.fail("wrong identity must not refresh"))
    monkeypatch.setattr(codex_oauth, "login", lambda *a, **k: pytest.fail("must request an explicit corrective login"))
    logs = []
    results = login.WakeJob([acct], lambda level, text: logs.append((level, text))).run()
    assert results == {acct.name: "identity-mismatch"}
    assert any("expected free@example.test" in text and "pro@example.test" in text for _, text in logs)
    assert any("0/1 online" in text for _, text in logs)


@pytest.mark.parametrize("returned_email,expected_result", [
    ("pro@example.test", "identity-mismatch"),
    ("FREE@EXAMPLE.TEST", "logged-in"),
])
def test_codex_login_only_reports_success_for_intended_identity(home, monkeypatch, returned_email, expected_result):
    acct = Account("free@example.test", "codex", str(home / "account"), note="free@example.test")
    save_accounts([acct])

    def complete_login(codex_home, open_url, **kwargs):
        make_codex(codex_home, email=returned_email)
        return returned_email

    monkeypatch.setattr(codex_oauth, "login", complete_login)
    logs = []
    results = login.WakeJob([acct], lambda level, text: logs.append((level, text)), force=True).run()
    assert results == {acct.name: expected_result}
    assert any(f"{int(expected_result == 'logged-in')}/1 online" in text for _, text in logs)
    if expected_result == "identity-mismatch":
        assert not any(level == "ok" for level, _ in logs)


def test_generic_alias_remains_online(home):
    acct = Account("Work", "codex", str(make_codex(home / "alias", email="person@example.test")))
    save_accounts([acct])
    assert login.WakeJob([acct], lambda *a: None).run() == {"Work": "ok"}
