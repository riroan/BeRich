"""Tests for the RSI + Heikin-Ashi backtest strategy."""

import numpy as np
import pandas as pd
import pytest

from scripts.backtest_engine import run_simulation
from scripts.backtest_ha import HeikinAshiFlipBacktest
from scripts.backtest_registry import BACKTESTS, build_from_request
from scripts.backtest_rsi import RSIMeanReversionBacktest
from scripts.backtest_rsi_ha import RSIHeikinAshiBacktest
from src.web.app import BacktestRequest


def _df(closes, start="2022-01-03"):
    closes = list(closes)
    opens = [closes[0]] + closes[:-1]
    return pd.DataFrame(
        {
            "Open": opens,
            "High": [max(o, c) * 1.005 for o, c in zip(opens, closes)],
            "Low": [min(o, c) * 0.995 for o, c in zip(opens, closes)],
            "Close": closes,
            "Volume": [1000] * len(closes),
        },
        index=pd.bdate_range(start, periods=len(closes)),
    )


def _decline_then_rally():
    """Oversold on the way down, then a rally that turns HA green."""
    return list(np.linspace(200, 120, 45)) + list(np.linspace(120, 190, 25))


class TestRegistry:
    def test_it_is_registered(self):
        assert BACKTESTS["rsi_ha"] is RSIHeikinAshiBacktest

    def test_it_inherits_the_rsi_parameters(self):
        body = BacktestRequest(
            strategy="rsi_ha", symbol="AAPL", market="nasdaq",
            start_date="2022-01-01", end_date="2023-01-01", rsi_period=9,
        )
        strategy = build_from_request(body)
        assert isinstance(strategy, RSIHeikinAshiBacktest)
        assert strategy.rsi_period == 9
        assert strategy.avg_down_levels == [[30, 0.5], [25, 0.3], [20, 0.2]]

    def test_its_rsi_ladders_are_still_validated(self):
        """It reads them, so a broken ladder must be rejected — unlike HA."""
        common = dict(
            symbol="AAPL", market="nasdaq",
            start_date="2022-01-01", end_date="2023-01-01", sell_levels=[],
        )
        with pytest.raises(ValueError, match="level_invalid"):
            BacktestRequest(strategy="rsi_ha", **common)
        # HA reads none of them, so the same request is fine there.
        assert BacktestRequest(strategy="ha", **common).strategy == "ha"


class TestEntryGate:
    def test_it_buys_no_more_often_than_the_rsi_strategy(self):
        closes = _decline_then_rally()
        rsi = run_simulation(_df(closes), "T", RSIMeanReversionBacktest({}))
        both = run_simulation(_df(closes), "T", RSIHeikinAshiBacktest({}))
        assert both["num_buys"] <= rsi["num_buys"]

    def test_every_buy_lands_on_a_confirmed_bullish_candle(self):
        closes = _decline_then_rally()
        df = _df(closes)
        strategy = RSIHeikinAshiBacktest({})
        prepared = strategy.prepare(df.copy())
        bullish = (prepared["HA_close"] > prepared["HA_open"]).tolist()
        dates = [d.strftime("%Y-%m-%d") for d in prepared.index]

        result = run_simulation(df, "T", RSIHeikinAshiBacktest({}))

        assert result["num_buys"] > 0, "gate blocked everything — bad fixture"
        for trade in result["buy_trades"]:
            i = dates.index(trade["date"])
            # The PREVIOUS bar is the confirmed one, matching the live gate.
            assert i >= 1 and bullish[i - 1], trade["date"]

    def test_a_pure_decline_never_buys(self):
        """RSI screams oversold the whole way down; HA stays red."""
        result = run_simulation(
            _df(np.linspace(200, 80, 60)), "T", RSIHeikinAshiBacktest({}),
        )
        assert result["num_buys"] == 0

    def test_a_blocked_bar_leaves_the_ladder_armed(self):
        """Dropping the signal must not spend the rung."""
        strategy = RSIHeikinAshiBacktest({})
        df = _df(np.linspace(200, 80, 60))
        run_simulation(df, "T", strategy)
        assert strategy.buy_stage == 0


class TestSharedShape:
    def test_exits_are_not_gated(self):
        """Sells must still fire on red candles, or the ladder is useless."""
        closes = _decline_then_rally() + list(np.linspace(190, 130, 20))
        result = run_simulation(_df(closes), "T", RSIHeikinAshiBacktest({}))
        assert result["num_buys"] > 0
        assert result["num_sells"] > 0

    def test_the_payload_matches_the_other_strategies(self):
        closes = _decline_then_rally()
        keys = {
            name: set(run_simulation(_df(closes), "T", cls({})))
            for name, cls in (
                ("rsi", RSIMeanReversionBacktest),
                ("ha", HeikinAshiFlipBacktest),
                ("rsi_ha", RSIHeikinAshiBacktest),
            )
        }
        assert keys["rsi"] == keys["ha"] == keys["rsi_ha"]

    def test_the_indicator_panel_is_populated(self):
        closes = _decline_then_rally()
        result = run_simulation(_df(closes), "T", RSIHeikinAshiBacktest({}))
        assert len(result["rsi_values"]) == len(result["dates"]) > 0
