"""Tests for the Heikin-Ashi flip strategy"""

from datetime import datetime, timedelta

import pandas as pd
import pytest

from src.core.types import Bar, Market, SignalType
from src.strategy.builtin.heikin_ashi_flip import (
    HeikinAshiFlipStrategy,
    heikin_ashi,
)


def _bar(ts, o, h, low, c, symbol="AAPL"):
    return Bar(
        symbol=symbol,
        market=Market.NASDAQ,
        open=o,
        high=h,
        low=low,
        close=c,
        volume=1000,
        timestamp=ts,
        timeframe="1d",
    )


def _flat_series(n, price=100.0, start=None):
    """n bars of a doji-ish drift-free series — no HA flips of its own."""
    start = start or datetime(2026, 1, 1)
    return [
        _bar(start + timedelta(days=i), price, price, price, price)
        for i in range(n)
    ]


class TestHeikinAshi:
    def test_first_bar_seeds_from_open_close(self):
        df = pd.DataFrame(
            {"open": [10.0], "high": [12.0], "low": [9.0], "close": [11.0]}
        )
        ha = heikin_ashi(df)
        assert ha["ha_close"].iloc[0] == pytest.approx(10.5)   # (10+12+9+11)/4
        assert ha["ha_open"].iloc[0] == pytest.approx(10.5)    # (10+11)/2

    def test_open_is_previous_midpoint(self):
        df = pd.DataFrame(
            {
                "open": [10.0, 11.0],
                "high": [12.0, 15.0],
                "low": [9.0, 10.0],
                "close": [11.0, 14.0],
            }
        )
        ha = heikin_ashi(df)
        assert ha["ha_close"].iloc[1] == pytest.approx(12.5)   # (11+15+10+14)/4
        assert ha["ha_open"].iloc[1] == pytest.approx(10.5)    # (10.5+10.5)/2


class TestHeikinAshiFlipStrategy:
    @pytest.fixture
    def strategy(self):
        return HeikinAshiFlipStrategy(symbols=["AAPL"], market=Market.NASDAQ)

    def _init(self, strategy, bars):
        strategy.initialize({"AAPL": bars})

    @pytest.mark.asyncio
    async def test_buy_on_bearish_to_bullish_flip(self, strategy):
        bars = _flat_series(25)
        # One down bar (HA bearish), then a strong up bar (HA bullish).
        base = bars[-1].timestamp
        bars.append(_bar(base + timedelta(days=1), 100, 100, 95, 96))
        bars.append(_bar(base + timedelta(days=2), 97, 115, 97, 114))
        self._init(strategy, bars)

        signal = await strategy.calculate_signal("AAPL")
        assert signal is not None
        assert signal.signal_type == SignalType.ENTRY_LONG
        assert signal.strength == 1.0
        assert signal.metadata["reason"] == "ha_flip_up"

    @pytest.mark.asyncio
    async def test_sell_on_bullish_to_bearish_flip(self, strategy):
        bars = _flat_series(25)
        base = bars[-1].timestamp
        bars.append(_bar(base + timedelta(days=1), 100, 120, 100, 118))
        # HA smooths: a mild red bar stays green. It takes a real breakdown
        # (HA close 102.25 < HA open 104.75) to flip the color.
        bars.append(_bar(base + timedelta(days=2), 118, 118, 85, 88))
        self._init(strategy, bars)
        strategy._positions["AAPL"] = 10

        signal = await strategy.calculate_signal("AAPL")
        assert signal is not None
        assert signal.signal_type == SignalType.EXIT_LONG
        assert signal.strength == 1.0          # full exit
        assert signal.metadata["reason"] == "ha_flip_down"

    @pytest.mark.asyncio
    async def test_no_signal_without_a_flip(self, strategy):
        """Two bullish bars in a row is a trend, not a flip."""
        bars = _flat_series(25)
        base = bars[-1].timestamp
        bars.append(_bar(base + timedelta(days=1), 100, 115, 100, 114))
        bars.append(_bar(base + timedelta(days=2), 114, 130, 114, 129))
        self._init(strategy, bars)

        assert await strategy.calculate_signal("AAPL") is None

    @pytest.mark.asyncio
    async def test_buy_suppressed_while_already_long(self, strategy):
        bars = _flat_series(25)
        base = bars[-1].timestamp
        bars.append(_bar(base + timedelta(days=1), 100, 100, 95, 96))
        bars.append(_bar(base + timedelta(days=2), 97, 115, 97, 114))
        self._init(strategy, bars)
        strategy._positions["AAPL"] = 10

        assert await strategy.calculate_signal("AAPL") is None

    @pytest.mark.asyncio
    async def test_sell_suppressed_while_flat(self, strategy):
        bars = _flat_series(25)
        base = bars[-1].timestamp
        bars.append(_bar(base + timedelta(days=1), 100, 120, 100, 118))
        # HA smooths: a mild red bar stays green. It takes a real breakdown
        # (HA close 102.25 < HA open 104.75) to flip the color.
        bars.append(_bar(base + timedelta(days=2), 118, 118, 85, 88))
        self._init(strategy, bars)

        assert await strategy.calculate_signal("AAPL") is None

    @pytest.mark.asyncio
    async def test_no_signal_before_required_history(self, strategy):
        self._init(strategy, _flat_series(5))
        assert await strategy.calculate_signal("AAPL") is None

    @pytest.mark.asyncio
    async def test_ticks_do_not_enter_the_daily_base(self, strategy):
        """on_bar must not append tick bars — the HA base is daily-only."""
        bars = _flat_series(25)
        self._init(strategy, bars)
        before = len(strategy.get_dataframe("AAPL"))

        await strategy.on_bar(_bar(datetime(2026, 6, 1), 999, 999, 999, 999))

        assert len(strategy.get_dataframe("AAPL")) == before

    def test_confirm_daily_bar_appends_then_refreshes(self, strategy):
        bars = _flat_series(25)
        self._init(strategy, bars)
        before = len(strategy.get_dataframe("AAPL"))
        next_day = bars[-1].timestamp + timedelta(days=1)

        assert strategy.confirm_daily_bar(
            "AAPL", _bar(next_day, 100, 110, 99, 108)
        ) == "appended"
        assert len(strategy.get_dataframe("AAPL")) == before + 1
        assert strategy.last_confirmed_date("AAPL") == next_day.date()

        # Same date again = final close folded in, no new row.
        assert strategy.confirm_daily_bar(
            "AAPL", _bar(next_day, 100, 110, 99, 105)
        ) == "refreshed"
        assert len(strategy.get_dataframe("AAPL")) == before + 1
        assert strategy.get_dataframe("AAPL")["close"].iloc[-1] == 105

        # Older bar is stale.
        assert strategy.confirm_daily_bar(
            "AAPL", _bar(bars[-1].timestamp, 100, 110, 99, 101)
        ) == "skipped"
        assert len(strategy.get_dataframe("AAPL")) == before + 1

    def test_confirm_daily_bar_caps_the_window(self, strategy):
        self._init(strategy, _flat_series(strategy.history_window))
        last = datetime(2026, 1, 1) + timedelta(days=strategy.history_window)

        strategy.confirm_daily_bar("AAPL", _bar(last, 100, 110, 99, 108))

        assert len(strategy.get_dataframe("AAPL")) == strategy.history_window

    @pytest.mark.asyncio
    async def test_flip_fires_after_the_bar_is_confirmed(self, strategy):
        """The flip must come from a confirmed bar, not a forming one."""
        bars = _flat_series(25)
        base = bars[-1].timestamp
        bars.append(_bar(base + timedelta(days=1), 100, 100, 95, 96))
        self._init(strategy, bars)
        assert await strategy.calculate_signal("AAPL") is None

        strategy.confirm_daily_bar(
            "AAPL", _bar(base + timedelta(days=2), 97, 115, 97, 114)
        )
        signal = await strategy.calculate_signal("AAPL")
        assert signal is not None
        assert signal.signal_type == SignalType.ENTRY_LONG

    def test_registered_in_the_strategy_allowlist(self):
        from src.strategy import available_strategies

        assert (
            "src.strategy.builtin.heikin_ashi_flip.HeikinAshiFlipStrategy"
            in available_strategies()
        )
