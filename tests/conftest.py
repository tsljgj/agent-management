import pytest


@pytest.fixture(autouse=True)
def _private_redline_home(tmp_path_factory, monkeypatch):
    """No test reads or writes the real ~/.redline (stored Console sessions live there).
    Tests that need another home still set or delete REDLINE_HOME themselves."""
    monkeypatch.setenv("REDLINE_HOME", str(tmp_path_factory.mktemp("redline-home")))
