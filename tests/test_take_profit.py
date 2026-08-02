"""Take-profit exit: price-based, independent of RSI.

The sell ladder only fires on RSI, so a position that drifts up without
overheating enough never reaches it. This is the exit that ignores RSI, and
it must behave identically in the live strategy and the backtest simulator.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pandas as pd
import pytest

os.environ.setdefault("DASHBOARD_PASSWORD", "test")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from scripts.backtest_rsi import _run_simulation  # noqa: E402
from src.core.types import Market, trade_action  # noqa: E402
from src.strategy.builtin.rsi_mean_reversion import (  # noqa: E402
    RSIMeanReversionStrategy,
)


def _closes() -> list[float]:
    """Quiet stretch, a decline deep enough to buy, then a long recovery.

    The zigzag matters: perfectly flat prices pin Wilder RSI to 0 and trigger
    a buy on every bar.
    """
    base = [100 + (0.4 if i % 2 else -0.4) for i in range(30)]
    decline = [100 * (1 - 0.012 * i) for i in range(1, 16)]
    low = decline[-1]
    rally = [low * (1 + 0.012 * i) for i in range(1, 51)]
    return [round(c, 2) for c in base + decline + rally]


CLOSES = _closes()


def _df(closes: list[float]) -> pd.DataFrame:
    idx = pd.bdate_range(start="2022-01-03", periods=len(closes))
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [c * 1.01 for c in closes],
            "Low": [c * 0.99 for c in closes],
            "Close": closes,
            "Volume": [1000] * len(closes),
        },
        index=idx,
    )


def _params(**overrides) -> dict:
    p = {
        "rsi_period": 14,
        "stop_loss": -100,
        "cooldown_days": 999,
        "avg_down_levels": [[30, 1.0]],
        # out of reach, so only take-profit can close the position
        "sell_levels": [[99, 1.0]],
    }
    p.update(overrides)
    return p


def _sim(**param_overrides):
    return _run_simulation(
        _df(CLOSES), "X", _params(**param_overrides),
        initial_capital=1_000_000,
    )


def test_backtest_take_profit_closes_the_position():
    result = _sim(take_profit=20)

    taken = [
        t for t in result["trades"] if t.sell_reason == "take_profit"
    ]
    assert len(taken) == 1
    assert taken[0].pnl_pct >= 20


def test_backtest_take_profit_beats_the_rsi_ladder_to_the_exit():
    # The whole point: it exits while RSI is still under the sell threshold,
    # somewhere the ladder alone would have held on.
    result = _sim(take_profit=20)
    exit_date = next(
        t.sell_date for t in result["trades"]
        if t.sell_reason == "take_profit"
    )

    rsi_at_exit = dict(zip(result["dates"], result["rsi_values"]))[
        exit_date.strftime("%Y-%m-%d")
    ]
    assert rsi_at_exit < 99


def test_backtest_without_take_profit_holds_the_position():
    result = _sim()

    reasons = [t["reason"] for t in result["sell_trades"]]
    assert "take_profit" not in reasons
    assert reasons == ["end_of_period"]


def test_backtest_take_profit_caps_the_run():
    # Honest tradeoff: the exit gives up the rest of the move. If this ever
    # stops being true the fixture no longer covers the interesting case.
    assert _sim(take_profit=20)["total_return_pct"] < _sim()[
        "total_return_pct"
    ]


def _live(take_profit=None):
    params = _params()
    if take_profit is not None:
        params["take_profit"] = take_profit
    strategy = RSIMeanReversionStrategy(
        symbols=["X"], market=Market.NASDAQ, params=params,
    )
    idx = pd.bdate_range(start="2022-01-03", periods=len(CLOSES))
    strategy._daily_bars["X"] = pd.DataFrame({"close": CLOSES}, index=idx)
    strategy._positions["X"] = 10
    strategy._entry_prices["X"] = Decimal("100")
    return strategy


@pytest.mark.asyncio
async def test_live_take_profit_exits_the_full_position():
    signal = await _live(take_profit=20).calculate_signal("X")

    assert signal is not None
    assert signal.metadata["reason"] == "take_profit"
    assert signal.metadata["sell_portion"] == 1.0
    assert signal.strength == 1.0
    assert signal.metadata["pnl_pct"] >= 20


@pytest.mark.asyncio
async def test_live_take_profit_off_never_uses_that_reason():
    signal = await _live().calculate_signal("X")

    assert signal is None or signal.metadata["reason"] != "take_profit"


@pytest.mark.asyncio
async def test_live_take_profit_does_not_fire_below_threshold():
    signal = await _live(take_profit=200).calculate_signal("X")

    assert signal is None or signal.metadata["reason"] != "take_profit"


@pytest.mark.asyncio
async def test_live_stop_loss_still_wins_when_both_could_apply():
    # Guards the ordering: a position deep underwater must exit as a loss,
    # never get relabelled by a take-profit check placed above it.
    strategy = _live(take_profit=20)
    strategy.params["stop_loss"] = -5
    strategy._entry_prices["X"] = Decimal("1000")

    signal = await strategy.calculate_signal("X")

    assert signal is not None
    assert signal.metadata["reason"] == "stop_loss"


def test_take_profit_gets_its_own_trade_log_label():
    # Otherwise it renders as a plain "sell", indistinguishable from the
    # RSI ladder in the trade log.
    assert trade_action("sell", "take_profit") == "take_profit"
    assert trade_action("sell", "stop_loss") == "stop_loss"
    assert trade_action("sell", "staged_sell_2") == "partial_sell"


# ---------- staged ladders ----------

STAGED = {
    "take_profit_levels": [[20, 0.3], [30, 0.5], [50, 1.0]],
    "stop_loss_levels": [[-10, 0.3], [-15, 0.5], [-20, 1.0]],
}


@pytest.mark.asyncio
async def test_staged_take_profit_sells_only_its_portion():
    strategy = _live()
    strategy.params.update(STAGED)
    strategy._entry_prices["X"] = Decimal("100")  # final close ~131 => +31%

    signal = await strategy.calculate_signal("X")

    # +31% clears stage 1 (20%) and stage 2 (30%), and the ladder advances
    # one rung at a time, so stage 1 fires first.
    assert signal.metadata["reason"] == "take_profit"
    assert signal.metadata["stage"] == 1
    assert signal.strength == 0.3


@pytest.mark.asyncio
async def test_take_profit_ladder_advances_and_does_not_repeat():
    strategy = _live()
    strategy.params.update(STAGED)
    strategy._entry_prices["X"] = Decimal("100")

    strategy._tp_stages["X"] = 1
    second = await strategy.calculate_signal("X")
    assert second.metadata["stage"] == 2
    assert second.strength == 0.5

    # +31% never reaches the 50% rung, so the ladder stops there rather than
    # re-selling the stages it already took.
    strategy._tp_stages["X"] = 2
    assert await strategy.calculate_signal("X") is None


@pytest.mark.asyncio
async def test_staged_stop_loss_sells_only_its_portion():
    strategy = _live()
    strategy.params.update(STAGED)
    strategy._entry_prices["X"] = Decimal("200")  # ~131 close => -34%

    signal = await strategy.calculate_signal("X")

    assert signal.metadata["reason"] == "stop_loss"
    assert signal.metadata["stage"] == 1
    assert signal.strength == 0.3


@pytest.mark.asyncio
async def test_ladder_stage_advances_on_fill_not_on_signal():
    from src.core.types import Fill, OrderSide

    strategy = _live()
    strategy.params.update(STAGED)
    strategy._entry_prices["X"] = Decimal("100")

    await strategy.calculate_signal("X")
    assert strategy._tp_stages.get("X", 0) == 0, "signal alone must not advance"

    await strategy.on_fill(Fill(
        order_id="1", symbol="X", market=Market.NASDAQ, side=OrderSide.SELL,
        quantity=3, price=Decimal("131"), commission=Decimal("0"),
        timestamp=__import__("datetime").datetime.now(),
        metadata={"reason": "take_profit", "stage": 1},
        reason="take_profit",
    ))
    assert strategy._tp_stages["X"] == 1
    # The PnL ladders must not disturb the RSI ladder's cooldown clock.
    assert "X" not in strategy._last_sell_time


@pytest.mark.asyncio
async def test_scalar_take_profit_still_exits_everything():
    # Back-compat: a plain `take_profit: 20` is one stage selling 100%.
    signal = await _live(take_profit=20).calculate_signal("X")

    assert signal.metadata["reason"] == "take_profit"
    assert signal.strength == 1.0


def test_backtest_staged_take_profit_scales_out():
    result = _run_simulation(
        _df(CLOSES), "X",
        _params(take_profit_levels=[[20, 0.3], [30, 0.5], [50, 1.0]]),
        initial_capital=1_000_000,
    )

    taken = [t for t in result["trades"] if t.sell_reason == "take_profit"]
    assert len(taken) > 1, "a staged ladder should exit across several trades"
    assert taken[0].portion == 0.3
    assert taken[0].pnl_pct < taken[1].pnl_pct


def test_backtest_staged_ladder_never_sells_zero_shares():
    # The 1-share floor: a 3-share position times 0.3 truncates to 0.
    result = _run_simulation(
        _df(CLOSES), "X",
        _params(take_profit_levels=[[20, 0.3], [30, 0.5], [50, 1.0]]),
        initial_capital=400,
    )

    assert all(t.shares >= 1 for t in result["trades"])


@pytest.mark.asyncio
async def test_cleared_stop_loss_ladder_does_not_rearm_the_default():
    # Clearing every row in the UI sends []. If that fell through to the
    # scalar default, a config with no stop would silently get -10%.
    strategy = _live()
    strategy.params["stop_loss_levels"] = []
    strategy._entry_prices["X"] = Decimal("1000")  # ~-87%

    signal = await strategy.calculate_signal("X")

    assert signal is None or signal.metadata["reason"] != "stop_loss"


def test_backtest_cleared_stop_loss_ladder_matches_live():
    result = _run_simulation(
        _df(CLOSES), "X",
        # no scalar fallback, no ladder: nothing may exit on the loss side
        {k: v for k, v in _params(stop_loss_levels=[]).items()
         if k != "stop_loss"},
        initial_capital=1_000_000,
    )

    assert all(t.sell_reason != "stop_loss" for t in result["trades"])


# ---------- partial fills must not spend a rung ----------

async def _sell_fill(strategy, *, qty, reason, stage, complete):
    from src.core.types import Fill, OrderSide

    await strategy.on_fill(Fill(
        order_id="1", symbol="X", market=Market.NASDAQ, side=OrderSide.SELL,
        quantity=qty, price=Decimal("60"), commission=Decimal("0"),
        timestamp=__import__("datetime").datetime.now(),
        metadata={"reason": reason, "stage": stage}, reason=reason,
        complete=complete,
    ))


@pytest.mark.asyncio
async def test_a_partial_stop_loss_fill_leaves_the_stop_armed():
    """The one-rung case: any scalar stop_loss normalises to a single rung.

    If a partial fill retires it, the shares that did not sell are never
    stopped out again — not on the next tick, and not after a restart, since
    the spent rung is persisted.
    """
    strategy = _live()
    strategy.params["stop_loss"] = -10
    strategy._entry_prices["X"] = Decimal("1000")   # deep loss

    first = await strategy.calculate_signal("X")
    assert first.metadata["reason"] == "stop_loss"

    # 10 of the 10 requested never filled; only 4 did.
    await _sell_fill(strategy, qty=4, reason="stop_loss", stage=1,
                     complete=False)

    assert strategy._sl_stages.get("X", 0) == 0
    assert strategy.get_position("X") == 6

    again = await strategy.calculate_signal("X")
    assert again is not None
    assert again.metadata["reason"] == "stop_loss"


@pytest.mark.asyncio
async def test_a_complete_fill_does_spend_the_rung():
    strategy = _live()
    strategy.params.update(STAGED)
    strategy._entry_prices["X"] = Decimal("1000")

    await _sell_fill(strategy, qty=3, reason="stop_loss", stage=1,
                     complete=True)

    assert strategy._sl_stages["X"] == 1


@pytest.mark.asyncio
async def test_the_same_guard_covers_the_rsi_sell_ladder():
    # staged_sell advances through the same branch and takes the same
    # partial-fill events, so fixing only the PnL ladders would leave a
    # sibling caller broken.
    strategy = _live()

    await _sell_fill(strategy, qty=2, reason="staged_sell_1", stage=1,
                     complete=False)
    assert strategy._sell_stages.get("X", 0) == 0
    assert "X" not in strategy._last_sell_time

    await _sell_fill(strategy, qty=2, reason="staged_sell_1", stage=1,
                     complete=True)
    assert strategy._sell_stages["X"] == 1
