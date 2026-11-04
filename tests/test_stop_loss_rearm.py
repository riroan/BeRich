"""The loss ladder re-arms, so one stop-loss is not the last.

Its stage only reset when the position went flat. A partial stop (e.g.
-10% → sell 50%) therefore spent the ladder for good: the rest of the
position was never stopped again however far it fell. It now re-arms
cooldown_days after its last rung filled, and whenever a buy moves the
average it is measured from. A stop-loss exit also blocks re-entry for
stop_loss_reentry_days, and the backtest mirrors both rules.
"""

from __future__ import annotations

import asyncio
import datetime as datetime_mod
import os
from decimal import Decimal

import pandas as pd
import pytest

os.environ.setdefault("DASHBOARD_PASSWORD", "test")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import src.strategy.builtin.rsi_mean_reversion as rsi_mod  # noqa: E402
from scripts.backtest_rsi import _run_simulation  # noqa: E402
from src.core.types import Fill, Market, OrderSide  # noqa: E402
from src.strategy.builtin.rsi_mean_reversion import (  # noqa: E402
    RSIMeanReversionStrategy,
)

CLOSES = [100 + (0.4 if i % 2 else -0.4) for i in range(30)] + [
    100 * (1 - 0.012 * i) for i in range(1, 16)
]
INDEX = pd.bdate_range(start="2022-01-03", periods=len(CLOSES))

PARAMS = {
    "rsi_period": 14,
    "stop_loss_levels": [[-10, 0.5]],
    "cooldown_days": 2,
    "avg_down_levels": [[30, 1.0]],
    "sell_levels": [[99, 1.0]],
}


class _FrozenClock(datetime_mod.datetime):
    _now = datetime_mod.datetime(2022, 3, 1)

    @classmethod
    def now(cls, tz=None):
        return cls._now


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    monkeypatch.setattr(rsi_mod, "datetime", _FrozenClock)
    _FrozenClock._now = datetime_mod.datetime(2022, 3, 1)


def _strategy(**overrides) -> RSIMeanReversionStrategy:
    strategy = RSIMeanReversionStrategy(
        symbols=["X"], market=Market.NASDAQ, params={**PARAMS, **overrides},
    )
    strategy._daily_bars["X"] = pd.DataFrame({"close": CLOSES}, index=INDEX)
    strategy._positions["X"] = 10
    strategy._entry_prices["X"] = Decimal("100")
    strategy._buy_stages["X"] = 1
    return strategy


def _fill(side, qty, price, signal_meta) -> Fill:
    return Fill(
        order_id="o", symbol="X", market=Market.NASDAQ, side=side,
        quantity=qty, price=Decimal(str(price)), commission=Decimal("0"),
        timestamp=_FrozenClock._now, metadata=dict(signal_meta),
        reason=signal_meta.get("reason"),
    )


async def _take_partial_stop(strategy):
    strategy._live_price["X"] = 85.0  # -15%
    signal = await strategy.calculate_signal("X")
    assert signal.metadata["reason"] == "stop_loss"
    await strategy.on_fill(_fill(OrderSide.SELL, 5, 85, signal.metadata))
    assert strategy.get_position("X") == 5
    return strategy


async def _signal_at(strategy, price, days_later):
    _FrozenClock._now += datetime_mod.timedelta(days=days_later)
    strategy._live_price["X"] = price
    return await strategy.calculate_signal("X")


def test_partial_stop_holds_off_inside_the_cooldown():
    async def run():
        strategy = await _take_partial_stop(_strategy())
        return await _signal_at(strategy, 70.0, days_later=1)

    signal = asyncio.run(run())
    assert signal is None or signal.metadata.get("reason") != "stop_loss"


def test_partial_stop_fires_again_after_the_cooldown():
    # The bug: this stayed silent at -30%, -50%, ... forever.
    async def run():
        strategy = await _take_partial_stop(_strategy())
        return await _signal_at(strategy, 70.0, days_later=2)

    signal = asyncio.run(run())
    assert signal is not None
    assert signal.metadata["reason"] == "stop_loss"
    assert signal.strength == 0.5


def test_rearm_waits_at_least_a_day_even_with_zero_cooldown():
    async def run():
        strategy = await _take_partial_stop(_strategy(cooldown_days=0))
        return await _signal_at(strategy, 70.0, days_later=0)

    signal = asyncio.run(run())
    assert signal is None or signal.metadata.get("reason") != "stop_loss"


def test_a_buy_rearms_the_ladder_against_the_new_average():
    async def run():
        strategy = await _take_partial_stop(_strategy())
        await strategy.on_fill(_fill(
            OrderSide.BUY, 5, 85, {"reason": "avg_down_stage_1", "stage": 1},
        ))
        assert strategy._sl_stages["X"] == 0
        # average is now 92.5; -10% of it is 83.25
        return await _signal_at(strategy, 80.0, days_later=0)

    signal = asyncio.run(run())
    assert signal is not None
    assert signal.metadata["reason"] == "stop_loss"


def test_an_rsi_exit_does_not_start_the_stop_reentry_gate():
    async def run():
        strategy = _strategy()
        await strategy.on_fill(_fill(
            OrderSide.SELL, 10, 100, {"reason": "sell_stage_1", "stage": 1},
        ))
        return strategy

    strategy = asyncio.run(run())
    assert "X" not in strategy._last_stop_exit_time


# ---------- backtest mirrors the live rules ----------

def _crash_then_slide() -> pd.DataFrame:
    base = [100 + (0.4 if i % 2 else -0.4) for i in range(30)]
    slide = [100 * (1 - 0.02 * i) for i in range(1, 30)]
    closes = [round(c, 2) for c in base + slide]
    return pd.DataFrame(
        {
            "Open": closes, "High": closes, "Low": closes, "Close": closes,
            "Volume": [1000] * len(closes),
        },
        index=pd.bdate_range(start="2022-01-03", periods=len(closes)),
    )


def _stops(**overrides) -> int:
    result = _run_simulation(
        _crash_then_slide(), "X", {**PARAMS, **overrides},
        initial_capital=1_000_000,
    )
    return sum(1 for t in result["trades"] if t.sell_reason == "stop_loss")


def test_backtest_partial_stop_fires_more_than_once_in_a_long_slide():
    assert _stops() > 1


def test_backtest_reports_drawdown_and_stop_count():
    result = _run_simulation(
        _crash_then_slide(), "X", PARAMS, initial_capital=1_000_000,
    )
    stops = sum(1 for t in result["trades"] if t.sell_reason == "stop_loss")

    assert result["num_stop_losses"] == stops > 0
    # bought into a 58% slide: the account sat well below its peak
    assert 0 < result["max_drawdown_pct"] < 100
