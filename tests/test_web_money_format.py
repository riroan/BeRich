"""The sign belongs outside the currency symbol: +$N / -$N, never $-N."""

import re

import pytest

from src.web.app import _usd_signed


@pytest.mark.parametrize("value,expected", [
    (1234.5, "+$1,234.50"),
    (-1234.5, "-$1,234.50"),
    (0, "$0.00"),
    # Rounds away to zero but is still negative. script.js formatPnl says
    # "-$0.00" here too, and the two must agree or the number visibly flips
    # when the first WebSocket update lands.
    (-0.004, "-$0.00"),
    (None, "-"),
])
def test_usd_signed_puts_the_sign_before_the_symbol(value, expected):
    assert _usd_signed(value) == expected


def test_no_template_renders_the_sign_inside_the_symbol():
    """`${{ "{:+,.2f}".format(x) }}` renders "$-1,234.50". Six templates had
    drifted into it; keep them from drifting back."""
    from pathlib import Path

    templates = Path("src/web/templates")
    offenders = [
        f"{path.name}:{i}"
        for path in templates.glob("*.html")
        for i, line in enumerate(path.read_text().splitlines(), 1)
        if re.search(r'\$\{\{\s*"\{:[+]', line)
    ]
    assert offenders == []
