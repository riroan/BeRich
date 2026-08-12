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


def test_usd_signed_decimals_rounds_by_default():
    """decimals without truncate=True still rounds like the 2-decimal
    default, not floor."""
    assert _usd_signed(743.49, 0) == "+$743"
    assert _usd_signed(743.51, 0) == "+$744"
    assert _usd_signed(-743.51, 0) == "-$744"


def test_usd_signed_truncate_cuts_instead_of_rounds():
    """Main-page P&L wants the raw digits, not rounded up/down."""
    assert _usd_signed(743.567, 2, truncate=True) == "+$743.56"
    assert _usd_signed(-743.567, 2, truncate=True) == "-$743.56"
    # A value that's conceptually exact at the cut point must not lose a
    # cent to float representation error (743.5 as a float can print as
    # 743.4999999999999...); Decimal(str(x)) sidesteps that.
    assert _usd_signed(743.50, 2, truncate=True) == "+$743.50"
    # 0.1 + 0.2 == 0.30000000000000004 in float — the classic case that
    # would truncate to 0.29 if we multiplied the raw float by 100.
    assert _usd_signed(0.1 + 0.2, 2, truncate=True) == "+$0.30"


def test_main_page_pnl_spots_render_truncated_two_decimals():
    """hero-pnl, pnl-usd, and sticky-pnl-usd all show the same pnl_usd —
    they must agree on decimals/rounding-mode or the number visibly
    changes switching between page load and the first WebSocket tick."""
    for path in [
        Path("src/web/templates/index.html"),
        Path("src/web/templates/_header.html"),
    ]:
        text = path.read_text()
        assert "pnl_usd|usd_signed }}" not in text
        assert "sticky_pnl|usd_signed }}" not in text
        assert "usd_signed(0)" not in text
    assert (
        Path("src/web/templates/index.html").read_text()
        .count("pnl_usd|usd_signed(2, true)") == 2
    )
    assert (
        "sticky_pnl|usd_signed(2, true)"
        in Path("src/web/templates/_header.html").read_text()
    )


def test_main_page_pnl_websocket_updates_also_truncate():
    """The three main-page P&L DOM ids must pass truncate=true in script.js
    too, or the number rounds up a cent on the first WebSocket tick after
    matching the truncated server-rendered value on page load."""
    text = Path("src/web/static/script.js").read_text()
    assert text.count("this.formatUSD(data.pnl_usd, true, 2, true);") == 3


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
