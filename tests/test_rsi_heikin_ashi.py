"""Tests for the RSI + Heikin-Ashi combined strategy.

Buys need both conditions; everything else is inherited from
RSIMeanReversionStrategy and must keep working untouched.
"""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from src.core.types import Bar, Market, SignalType
from src.strategy.builtin.rsi_heikin_ashi import RSIHeikinAshiStrategy
from src.strategy.builtin.rsi_mean_reversion import RSIMeanReversionStrategy


PARAMS = {
    "rsi_period": 14,
    "stop_loss": -10,
    "avg_down_levels": [(35, 0.3), (30, 0.35), (25, 0.35)],
    "sell_levels": [(70, 0.3), (75, 0.4), (80, 0.5)],
}


def _bar(ts, o, h, low, c, symbol="AAPL"):
    return Bar(
        symbol=symbol, market=Market.NASDAQ, open=o, high=h, low=low,
        close=c, volume=1000, timestamp=ts, timeframe="1d",
    )


def _falling_bars(n=40, start=200.0, step=-3.0):
    """A steady decline — drives RSI into oversold with red HA candles."""
    base = datetime(2026, 1, 1)
    bars = []
    price = start
    for i in range(n):
        nxt = price + step
        bars.append(_bar(base + timedelta(days=i), price, max(price, nxt),
                         min(price, nxt), nxt))
        price = nxt
    return bars


@pytest.fixture
def strategy():
    return RSIHeikinAshiStrategy(
        symbols=["AAPL"], market=Market.NASDAQ, params=PARAMS,
    )


@pytest.fixture
def rsi_only():
    return RSIMeanReversionStrategy(
        symbols=["AAPL"], market=Market.NASDAQ, params=PARAMS,
    )


class TestEntryGate:
    @pytest.mark.asyncio
    async def test_red_ha_blocks_a_buy_the_rsi_strategy_would_take(
        self, strategy, rsi_only,
    ):
        bars = _falling_bars()
        strategy.initialize({"AAPL": bars})
        rsi_only.initialize({"AAPL": bars})

        assert strategy.heikin_ashi_bullish("AAPL") is False
        parent = await rsi_only.calculate_signal("AAPL")
        assert parent is not None and parent.signal_type == SignalType.ENTRY_LONG

        assert await strategy.calculate_signal("AAPL") is None

    @pytest.mark.asyncio
    async def test_green_ha_lets_the_same_buy_through(self, strategy):
        # Decline into oversold, then a rally strong enough to turn HA green
        # while RSI is still low enough to fire.
        bars = _falling_bars(40)
        last = bars[-1].timestamp
        price = float(bars[-1].close)
        for i in range(1, 4):
            nxt = price * 1.035
            bars.append(_bar(last + timedelta(days=i), price, nxt, price, nxt))
            price = nxt
        strategy.initialize({"AAPL": bars})

        assert strategy.heikin_ashi_bullish("AAPL") is True
        signal = await strategy.calculate_signal("AAPL")
        assert signal is not None
        assert signal.signal_type == SignalType.ENTRY_LONG
        assert signal.metadata["ha_close"] > signal.metadata["ha_open"]

    @pytest.mark.asyncio
    async def test_a_blocked_buy_leaves_the_ladder_armed(self, strategy):
        """No fill means no stage advance, so the rung retries next tick."""
        strategy.initialize({"AAPL": _falling_bars()})

        assert await strategy.calculate_signal("AAPL") is None
        assert strategy._buy_stages.get("AAPL", 0) == 0
        assert "AAPL" not in strategy._last_buy_time
        # Still blocked, still armed — not silently consumed.
        assert await strategy.calculate_signal("AAPL") is None
        assert strategy._buy_stages.get("AAPL", 0) == 0

    def test_the_gate_reads_confirmed_bars_only(self, strategy):
        """A forming candle must not flip the gate mid-session."""
        strategy.initialize({"AAPL": _falling_bars()})
        before = strategy.heikin_ashi_bullish("AAPL")

        # A live price that would look like a huge green candle.
        strategy.update_daily_close("AAPL", 9999.0)

        assert strategy.heikin_ashi_bullish("AAPL") == before

    def test_no_history_blocks_rather_than_guesses(self, strategy):
        assert strategy.heikin_ashi_bullish("AAPL") is None


class TestInheritedBehaviour:
    @pytest.mark.asyncio
    async def test_exits_are_never_gated(self, strategy):
        """A loss ladder must fire regardless of candle colour."""
        bars = _falling_bars(40)
        strategy.initialize({"AAPL": bars})
        strategy._positions["AAPL"] = 10
        strategy._entry_prices["AAPL"] = Decimal("1000")  # deep loss

        signal = await strategy.calculate_signal("AAPL")

        assert signal is not None
        assert signal.signal_type == SignalType.EXIT_LONG
        assert signal.metadata["reason"] == "stop_loss"

    def test_it_keeps_the_rsi_parameters(self, strategy):
        assert strategy.params["avg_down_levels"] == PARAMS["avg_down_levels"]
        assert strategy.params["sell_levels"] == PARAMS["sell_levels"]
        assert strategy.required_history == 20
        assert strategy.history_window == 100

    def test_it_is_registered_despite_being_a_sub_subclass(self):
        from src.strategy import available_strategies

        assert (
            "src.strategy.builtin.rsi_heikin_ashi.RSIHeikinAshiStrategy"
            in available_strategies()
        )

    def test_every_builtin_is_still_registered(self):
        """Subset, not equality: the registry is process-global, so other
        tests' BaseStrategy doubles show up here too."""
        from src.strategy import available_strategies

        assert {
            "MomentumStrategy",
            "RSIMeanReversionStrategy",
            "HeikinAshiFlipStrategy",
            "RSIHeikinAshiStrategy",
        } <= set(available_strategies().values())
