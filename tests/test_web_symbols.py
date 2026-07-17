"""Regression tests for the symbols management page rendering.

Guards against the bug where every symbol in a strategy config shared the
config id as its only DOM key, so the +/- weight buttons (and toggle/delete
DOM updates) always hit the first row of the group instead of the clicked one.
"""

from src.web.app import templates


def _render_symbols(symbols):
    return templates.env.get_template("symbols.html").render(
        symbols=symbols,
        markets=["nasdaq"],
        strategy_names=["rsi"],
        active_page="symbols",
    )


def test_rows_sharing_config_id_are_uniquely_addressable():
    # Two symbols in the same strategy config: both carry the same config id.
    symbols = [
        {"id": 1, "symbol": "AAPL", "market": "nasdaq",
         "strategy_name": "rsi", "enabled": True, "max_weight": 20.0},
        {"id": 1, "symbol": "MSFT", "market": "nasdaq",
         "strategy_name": "rsi", "enabled": True, "max_weight": 20.0},
    ]

    html = _render_symbols(symbols)

    # Both rows share data-id but must be distinguishable by data-symbol, so
    # querySelector can target the exact row that was clicked.
    assert html.count('data-id="1"') == 2
    assert 'data-symbol="AAPL"' in html
    assert 'data-symbol="MSFT"' in html


def test_row_lookups_disambiguate_by_symbol():
    html = _render_symbols([
        {"id": 1, "symbol": "AAPL", "market": "nasdaq",
         "strategy_name": "rsi", "enabled": True, "max_weight": 20.0},
    ])

    # Every .sym-row lookup must key on data-symbol, not data-id alone,
    # otherwise it resolves to the first row in a multi-symbol group.
    assert '.sym-row[data-id="${id}"]`' not in html
    assert html.count('[data-id="${id}"][data-symbol="${symbol}"]') == 3
