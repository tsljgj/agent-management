from io import StringIO
from datetime import datetime

import pytest

from redline.models import Usage
from redline.render import render_table


@pytest.mark.parametrize("counts, expected", [
    ({"available": 3, "applicable": 0}, "Banked resets: 3 (applicable: 0)"),
    ({"available": 0}, "Banked resets: 0"),
    ({"applicable": 1}, "Banked resets: unknown (applicable: 1)"),
])
def test_banked_resets_render_separately_from_credits(counts, expected):
    u = Usage("free", "codex", True, plan="free",
              extra={"credits": "12.5", "banked_resets": counts})
    output = render_table([u], stream=StringIO())
    assert expected in output
    assert "credits: 12.5" in output
    assert "'available'" not in output


def test_absent_banked_resets_are_not_displayed_as_zero():
    assert "Banked resets" not in render_table([Usage("free", "codex", True)], stream=StringIO())


def test_each_banked_reset_expiry_is_rendered_with_unknown_and_missing_details():
    expiry = "2099-11-06T23:23:28+00:00"
    usage = Usage("work", "codex", True, extra={"banked_resets": {"available": 4, "credits": [
        {"title": "Monthly", "expires_at": expiry},
        {"expires_at": None},
        {"expires_at": "2000-01-01T00:00:00+00:00"},
    ]}})
    output = render_table([usage], stream=StringIO())
    assert "Reset #1 · Monthly: expires " + datetime.fromisoformat(expiry).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z") in output
    assert "Reset #2: expires unknown" in output
    assert "Reset #3: expires " in output and "(expired)" in output
    assert "Expiry details unavailable for 1 reset(s)" in output


def test_count_only_cache_shows_missing_expiry_but_zero_does_not():
    for count in [0, 2]:
        usage = Usage("work", "codex", True, extra={"banked_resets": {"available": count}})
        output = render_table([usage], stream=StringIO())
        assert ("Expiry details unavailable" in output) == (count > 0)
