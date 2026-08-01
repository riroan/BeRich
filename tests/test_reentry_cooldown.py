"""Re-entry cooldown after a full exit.

A full exit calls `_reset_position`, which wipes the symbol's stage and
cooldown state. The next tick therefore sees a brand-new symbol and stage 1
fires on RSI alone — right after a stop-loss, when RSI is guaranteed to be
low because the stop-loss fired on a crash. The stop-loss churns the position
instead of leaving it.

`reentry_cooldown_days` gates that. It defaults to 0, which keeps the old
behaviour, so turning it on is a deliberate act.
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
from src.core.types import Fill, Market, OrderSide, SignalType  # noqa: E402
from src.strategy.builtin.rsi_mean_reversion import (  # noqa: E402
    RSIMeanReversionStrategy,
)

# Quiet, then a crash deep enough to trip a -10% stop with RSI on the floor.
CLOSES = [100 + (0.4 if i % 2 else -0.4) for i in range(30)] + [
    100 * (1 - 0.03 * i) for i in range(1, 16)
]
INDEX = pd.bdate_range(start="2022-01-03", periods=len(CLOSES))

PARAMS = {
    "rsi_period": 14,
    "stop_loss": -10,
    "cooldown_days": 3,
    "avg_down_levels": [[35, 0.5], [30, 0.7], [25, 1.0]],
    "sell_levels": [[65, 0.5], [70, 0.5], [75, 0.9]],
}


class _FrozenClock(datetime_mod.datetime):
    _now = datetime_mod.datetime(2022, 1, 3)

    @classmethod
    def now(cls, tz=None):
        return cls._now


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    monkeypatch.setattr(rsi_mod, "datetime", _FrozenClock)


def _strategy(**overrides):
    strategy = RSIMeanReversionStrategy(
        symbols=["X"], market=Market.NASDAQ, params={**PARAMS, **overrides},
    )
    strategy._daily_bars["X"] = pd.DataFrame({"close": CLOSES}, index=INDEX)
    strategy._positions["X"] = 100
    strategy._entry_prices["X"] = Decimal("100")
    strategy._buy_stages["X"] = 1
    _FrozenClock._now = INDEX[-1].to_pydatetime()
    return strategy


async def _exit_then_next(strategy, days_later: int):
    """Take the stop-loss, then ask again `days_later` days on."""
    signal = await strategy.calculate_signal("X")
    assert signal.metadata["reason"] == "stop_loss"

    await strategy.on_fill(Fill(
        order_id="1", symbol="X", market=Market.NASDAQ, side=OrderSide.SELL,
        quantity=strategy.get_position("X"),
        price=Decimal(str(CLOSES[-1])), commission=Decimal("0"),
        timestamp=_FrozenClock._now, metadata=dict(signal.metadata),
        reason="stop_loss",
    ))
    assert strategy.get_position("X") == 0

    _FrozenClock._now += datetime_mod.timedelta(days=days_later)
    return await strategy.calculate_signal("X")


def test_without_the_gate_a_stop_loss_re_buys_immediately():
    # The behaviour that is live today: the exit realises the loss and the
    # next tick buys the same falling symbol back.
    after = asyncio.run(_exit_then_next(_strategy(), days_later=0))

    assert after is not None
    assert after.signal_type is SignalType.ENTRY_LONG
    assert after.metadata["reason"] == "avg_down_stage_1"


def test_the_gate_blocks_re_entry_inside_the_window():
    after = asyncio.run(
        _exit_then_next(_strategy(reentry_cooldown_days=5), days_later=2),
    )

    assert after is None


def test_the_gate_expires():
    after = asyncio.run(
        _exit_then_next(_strategy(reentry_cooldown_days=5), days_later=6),
    )

    assert after is not None
    assert after.signal_type is SignalType.ENTRY_LONG


def test_the_gate_does_not_touch_an_open_position():
    # It keys on "flat since an exit", so averaging down into a position we
    # still hold must be unaffected.
    strategy = _strategy(reentry_cooldown_days=5)
    strategy._entry_prices["X"] = Decimal("60")   # small loss, no stop
    strategy._last_exit_time["X"] = _FrozenClock._now

    signal = asyncio.run(strategy.calculate_signal("X"))

    assert signal is not None
    assert signal.signal_type is SignalType.ENTRY_LONG


def test_exit_memory_survives_the_state_reset_that_causes_the_bug():
    # _reset_position wipes everything else about the symbol. If it wiped
    # this too the gate would never see an exit had happened.
    strategy = _strategy(reentry_cooldown_days=5)
    strategy._last_exit_time["X"] = _FrozenClock._now
    strategy._reset_position("X")

    assert "X" in strategy._last_exit_time
    assert "X" not in strategy._buy_stages


def test_a_new_position_clears_the_exit_memory():
    strategy = _strategy(reentry_cooldown_days=5)
    strategy._last_exit_time["X"] = _FrozenClock._now
    strategy._positions["X"] = 0

    asyncio.run(strategy.on_fill(Fill(
        order_id="2", symbol="X", market=Market.NASDAQ, side=OrderSide.BUY,
        quantity=10, price=Decimal("50"), commission=Decimal("0"),
        timestamp=_FrozenClock._now, metadata={"stage": 1},
        reason="avg_down_stage_1",
    )))

    assert "X" not in strategy._last_exit_time


# ---------- the backtest must apply the same rule ----------

def _ohlc():
    return pd.DataFrame(
        {
            "Open": CLOSES, "High": [c * 1.01 for c in CLOSES],
            "Low": [c * 0.99 for c in CLOSES], "Close": CLOSES,
            "Volume": [1000] * len(CLOSES),
        },
        index=INDEX,
    )


def _buy_count(**overrides):
    result = _run_simulation(
        _ohlc(), "X", {**PARAMS, **overrides}, initial_capital=1_000_000,
    )
    return len(result["buy_trades"])


def test_backtest_applies_the_same_gate():
    # Same knob, same direction, or the simulator stops predicting the bot.
    assert _buy_count(reentry_cooldown_days=30) < _buy_count()


def test_backtest_default_is_unchanged():
    assert _buy_count(reentry_cooldown_days=0) == _buy_count()
