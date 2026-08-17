"""Positions must show the tick price, whichever path built them.

The page render rebuilds price/PnL from the latest price_rsi row (the tick
path). The bot's position sweep instead carries the KIS balance API's
evaluation price, which lags. Both write dashboard_state.positions, so the
WebSocket used to push the lagging price over the freshly rendered one — and
because the table sorts by P&L, the whole row ORDER reshuffled ~60s after
every refresh.

Measured on production 2026-08-17: one tick broadcast moved RAM from
$14.02/+28.8% to $13.20/+21.3% and SOXL from $148.24/+23.7% to
$144.95/+20.9%, dropping SPYU from 3rd place to 1st.
"""

from __future__ import annotations

import os

os.environ.setdefault("DASHBOARD_PASSWORD", "test")

from src.web.app import DashboardState  # noqa: E402


def _balance_record(symbol="RAM", current_price=13.20):
    """What the bot's sweep produces: the balance API's evaluation price."""
    return {
        "symbol": symbol,
        "market": "NASDAQ",
        "quantity": 10,
        "avg_price": 10.89,
        "current_price": current_price,
        "pnl": (current_price - 10.89) * 10,
        "pnl_pct": (current_price - 10.89) / 10.89 * 100,
        "stop_loss_pct": -10.0,
    }


def test_the_tick_price_wins_over_a_lagging_balance_price():
    state = DashboardState()
    state.update_rsi("RAM", 52.2, price=14.02, market="NASDAQ")

    state.replace_positions_from_records([_balance_record()])

    pos = state.positions["RAM"]
    assert pos.current_price == 14.02
    assert pos.pnl_pct == (14.02 - 10.89) / 10.89 * 100
    assert pos.pnl == (14.02 - 10.89) * 10


def test_stop_loss_distance_moves_with_the_corrected_pnl():
    state = DashboardState()
    state.update_rsi("RAM", 52.2, price=14.02, market="NASDAQ")

    state.replace_positions_from_records([_balance_record()])

    pos = state.positions["RAM"]
    assert pos.stop_loss_distance == pos.pnl_pct - pos.stop_loss_pct


def test_the_sort_order_no_longer_flips_between_the_two_paths():
    """The user-visible symptom: rows reshuffle after a WS update."""
    state = DashboardState()
    ticks = {"RAM": 14.02, "SOXL": 148.24, "SPYU": 37.18}
    for symbol, price in ticks.items():
        state.update_rsi(symbol, 50.0, price=price, market="NASDAQ")

    # Balance-API prices lag by different amounts per symbol.
    state.replace_positions_from_records([
        _balance_record("RAM", 13.20),
        _balance_record("SOXL", 144.95),
        _balance_record("SPYU", 37.16),
    ])

    by_pnl = sorted(
        state.positions.values(), key=lambda p: p.pnl_pct, reverse=True,
    )
    assert [p.symbol for p in by_pnl] == ["SOXL", "SPYU", "RAM"]
    # Every row carries the tick price, so a re-render cannot reorder them.
    assert [p.current_price for p in by_pnl] == [148.24, 37.18, 14.02]


def test_a_symbol_the_tick_never_saw_keeps_the_broker_price():
    state = DashboardState()

    state.replace_positions_from_records([_balance_record("NVDA", 190.0)])

    assert state.positions["NVDA"].current_price == 190.0


def test_a_zero_avg_price_is_left_alone():
    """No cost basis means no PnL to recompute; must not divide by zero."""
    state = DashboardState()
    state.update_rsi("RAM", 52.2, price=14.02, market="NASDAQ")

    record = _balance_record()
    record["avg_price"] = 0.0
    state.replace_positions_from_records([record])

    assert state.positions["RAM"].current_price == 13.20
