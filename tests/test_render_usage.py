from io import StringIO

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
