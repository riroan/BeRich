"""The sign belongs outside the currency symbol: +$N / -$N, never $-N."""

import re
from pathlib import Path

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


def test_usd_signed_decimals_rounds_not_truncates():
    """Main-page P&L (hero, balance card, sticky header) passes decimals=0
    for whole-dollar display — must still round like the 2-decimal default,
    not floor."""
    assert _usd_signed(743.49, 0) == "+$743"
    assert _usd_signed(743.51, 0) == "+$744"
    assert _usd_signed(-743.51, 0) == "-$744"


def test_main_page_pnl_spots_render_whole_dollars():
    """hero-pnl, pnl-usd, and sticky-pnl-usd all show the same pnl_usd —
    they must agree on decimals or the number visibly changes digits
    switching between page load and the first WebSocket tick."""
    for path in [
        Path("src/web/templates/index.html"),
        Path("src/web/templates/_header.html"),
    ]:
        text = path.read_text()
        assert "pnl_usd|usd_signed }}" not in text
        assert "sticky_pnl|usd_signed }}" not in text


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
