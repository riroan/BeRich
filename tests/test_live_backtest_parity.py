"""The live strategy and the backtest simulator must decide identically.

`src/strategy/rsi_rules.py` says so in its docstring, but nothing enforced it:
the two run different loops over the same resolvers. This replays one close
series through both and compares the decisions.

It is also the characterization gate for refactoring. A refactor that changes
what the bot trades fails `test_decision_sequence_is_unchanged`, even if both
implementations change together and parity still holds.
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

CAPITAL = 1_000_000.0
START = "2022-01-03"

# The live NASDAQ_RSI_MeanReversion parameters.
LIVE_PARAMS = {
    "rsi_period": 14,
    "rsi_method": "wilder",
    "stop_loss": -100,
    "cooldown_days": 3,
    "avg_down_levels": [[35, 0.5], [30, 0.7], [25, 1.0]],
    "sell_levels": [[65, 0.5], [70, 0.5], [75, 0.9]],
}


def _closes() -> list[float]:
    """Quiet, then a decline that buys the ladder down, then a long recovery."""
    base = [100 + (0.4 if i % 2 else -0.4) for i in range(30)]
    down = [100 * (1 - 0.015 * i) for i in range(1, 21)]
    up = [down[-1] * (1 + 0.018 * i) for i in range(1, 41)]
    return [round(c, 2) for c in base + down + up]


CLOSES = _closes()
INDEX = pd.bdate_range(start=START, periods=len(CLOSES))


class _FrozenClock(datetime_mod.datetime):
    """The live cooldown reads the wall clock, so a replay must own it.

    Without this the strategy cannot be driven faster than real time: every
    bar lands within milliseconds, no cooldown ever elapses, and the sell
    ladder stalls after one cycle. That is why no parity test existed.
    """

    _now = datetime_mod.datetime(2022, 1, 3)

    @classmethod
    def now(cls, tz=None):
        return cls._now


def _ohlc() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Open": CLOSES,
            "High": [c * 1.01 for c in CLOSES],
            "Low": [c * 0.99 for c in CLOSES],
            "Close": CLOSES,
            "Volume": [1000] * len(CLOSES),
        },
        index=INDEX,
    )


async def _replay_live(params: dict) -> list[tuple]:
    """Drive the live strategy bar by bar, sizing exactly like the backtest.

    Sizing is deliberately copied rather than shared: the point is to hold
    everything except the trading RULES constant, so any difference that
    shows up is a rule difference.
    """
    strategy = RSIMeanReversionStrategy(
        symbols=["X"], market=Market.NASDAQ, params=params,
    )
    capital = CAPITAL
    decisions: list[tuple] = []

    for i, price in enumerate(CLOSES):
        _FrozenClock._now = INDEX[i].to_pydatetime()
        strategy._daily_bars["X"] = pd.DataFrame(
            {"close": CLOSES[: i + 1]}, index=INDEX[: i + 1],
        )
        strategy._live_price.pop("X", None)

        signal = await strategy.calculate_signal("X")
        if signal is None:
            continue

        position = strategy.get_position("X")
        reason = signal.metadata.get("reason", "")

        if signal.signal_type is SignalType.ENTRY_LONG:
            room = max(CAPITAL - position * price, 0)
            quantity = int(min(room * signal.strength, capital) / price)
            if quantity <= 0:
                continue
            capital -= quantity * price
            side = OrderSide.BUY
        else:
            quantity = int(position * Decimal(str(signal.strength)))
            if quantity == 0 and position > 0:
                quantity = 1
            if quantity <= 0:
                continue
            capital += quantity * price
            side = OrderSide.SELL

        decisions.append((i, reason, round(signal.strength, 3), quantity))
        await strategy.on_fill(Fill(
            order_id=str(i), symbol="X", market=Market.NASDAQ, side=side,
            quantity=quantity, price=Decimal(str(price)),
            commission=Decimal("0"), timestamp=INDEX[i].to_pydatetime(),
            metadata=dict(signal.metadata), reason=reason,
        ))

    return decisions


def _replay_backtest(params: dict) -> list[tuple]:
    result = _run_simulation(_ohlc(), "X", params, initial_capital=CAPITAL)
    bar_of = {d.date(): i for i, d in enumerate(INDEX)}

    decisions = [
        (bar_of[pd.Timestamp(b["date"]).date()],
         f"avg_down_stage_{b['stage']}", None, None)
        for b in result["buy_trades"]
    ]
    decisions += [
        (bar_of[t.sell_date.date()], t.sell_reason,
         round(t.portion, 3), t.shares)
        for t in result["trades"] if t.sell_date is not None
    ]
    return sorted(decisions)


def _normalize(reason: str) -> str:
    """The two sides label the same event differently."""
    return reason.replace("sell_stage_", "staged_sell_")


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    monkeypatch.setattr(rsi_mod, "datetime", _FrozenClock)


def _both(params: dict) -> tuple[list, list]:
    return asyncio.run(_replay_live(params)), _replay_backtest(params)


# ---------- parity ----------

def test_sell_decisions_match_bar_for_bar():
    live, backtest = _both(LIVE_PARAMS)

    live_sells = [
        (bar, _normalize(reason), portion, qty)
        for bar, reason, portion, qty in live
        if not reason.startswith("avg_down")
    ]
    backtest_sells = [
        (bar, _normalize(reason), portion, qty)
        for bar, reason, portion, qty in backtest
        if not reason.startswith("avg_down")
    ]

    # Same bar, same rung, same portion, same share count.
    assert live_sells == backtest_sells
    assert live_sells, "fixture must actually exercise the sell ladder"


def test_buy_decisions_match_bar_for_bar():
    live, backtest = _both(LIVE_PARAMS)

    live_buys = [
        (bar, reason) for bar, reason, _, _ in live
        if reason.startswith("avg_down")
    ]
    backtest_buys = [
        (bar, reason) for bar, reason, _, _ in backtest
        if reason.startswith("avg_down")
    ]

    assert live_buys == backtest_buys
    assert live_buys, "fixture must actually exercise the buy ladder"


def test_parity_holds_without_the_cooldown_too():
    # Cooldown gates ladder repetition on both sides through different
    # clocks, so it gets its own run.
    live, backtest = _both({**LIVE_PARAMS, "cooldown_days": 0})

    def sells(rows):
        return [
            (bar, _normalize(reason), portion, qty)
            for bar, reason, portion, qty in rows
            if not reason.startswith("avg_down")
        ]

    assert sells(live) == sells(backtest)
    # Removing the gate must actually change something, or this run is just
    # the previous test again.
    assert sells(live) != sells(asyncio.run(_replay_live(LIVE_PARAMS)))


# ---------- characterization ----------

def test_decision_sequence_is_unchanged():
    """Golden output. Parity alone cannot catch both sides moving together.

    Update this list ONLY when a trading rule is deliberately changed, and
    say so in the commit.
    """
    live = asyncio.run(_replay_live(LIVE_PARAMS))

    assert live == [
        (32, "avg_down_stage_1", 0.5, 5235),
        (33, "avg_down_stage_2", 0.7, 3782),
        (35, "avg_down_stage_3", 1.0, 1588),
        (63, "staged_sell_1", 0.5, 5302),
        (65, "staged_sell_2", 0.5, 2651),
        (68, "staged_sell_3", 0.9, 2386),
        (70, "staged_sell_1", 0.5, 133),
        (71, "staged_sell_2", 0.5, 66),
        (72, "staged_sell_3", 0.9, 60),
        (75, "staged_sell_1", 0.5, 3),
        (76, "staged_sell_2", 0.5, 2),
        (77, "staged_sell_3", 0.9, 1),
        (80, "staged_sell_1", 0.5, 1),
    ]


# ---------- known divergences, asserted so they cannot drift silently ----------

def test_the_two_sides_still_label_the_same_event_differently():
    live, backtest = _both(LIVE_PARAMS)

    live_reasons = {r for _, r, p, _ in live if p is not None}
    backtest_reasons = {r for _, r, p, _ in backtest if p is not None}

    assert live_reasons and backtest_reasons
    assert live_reasons.isdisjoint(backtest_reasons), (
        "if the labels were unified, drop _normalize and this test"
    )


def test_the_defaults_disagree_when_params_are_omitted():
    """A backtest run without explicit levels simulates a different strategy.

    Live defaults to selling at RSI 70/75/80, the simulator at 65/70/75. The
    live configs always pass their own levels, so this only bites ad-hoc runs
    — which is exactly where it is least likely to be noticed.
    """
    import inspect

    from scripts.backtest_rsi import RSIMeanReversionBacktest

    live_src = inspect.getsource(rsi_mod.RSIMeanReversionStrategy.calculate_signal)
    assert "(70, 0.3)" in live_src and "(75, 0.4)" in live_src

    assert "[[65, 0.3], [70, 0.3], [75, 0.4]]" in inspect.getsource(
        RSIMeanReversionBacktest
    )
