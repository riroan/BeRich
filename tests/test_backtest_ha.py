"""Heikin-Ashi backtest strategy + the strategy-agnostic engine contract."""

import numpy as np
import pandas as pd
import pytest

from scripts.backtest_engine import BacktestStrategy, run_simulation
from scripts.backtest_ha import HeikinAshiFlipBacktest
from scripts.backtest_registry import (
    BACKTESTS,
    build_backtest,
    build_from_request,
)
from scripts.backtest_rsi import RSIMeanReversionBacktest
from src.web.app import BacktestRequest


def _df(closes, start="2022-01-03"):
    """OHLC frame whose bar bodies follow `closes` (open = previous close)."""
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


def _flat(n, price=100.0):
    return [price] * n


class TestRegistry:
    def test_the_strategies_are_registered(self):
        assert set(BACKTESTS) == {"rsi", "ha", "rsi_ha"}

    def test_build_by_key(self):
        assert isinstance(build_backtest("rsi", {}), RSIMeanReversionBacktest)
        assert isinstance(build_backtest("ha", {}), HeikinAshiFlipBacktest)

    def test_unknown_key_is_rejected(self):
        with pytest.raises(ValueError, match="unknown backtest strategy"):
            build_backtest("nope", {})

    def test_every_registered_strategy_implements_the_abc(self):
        for key, cls in BACKTESTS.items():
            assert issubclass(cls, BacktestStrategy)
            assert cls.key == key
            strategy = cls({})
            assert strategy.name


class TestRequestParams:
    """HA takes no settings — it must neither read nor be validated by them."""

    def _req(self, **kw):
        return BacktestRequest(
            symbol="AAPL", market="nasdaq",
            start_date="2022-01-01", end_date="2023-01-01", **kw,
        )

    def test_ha_reads_no_params(self):
        strategy = build_from_request(self._req(strategy="ha"))
        assert isinstance(strategy, HeikinAshiFlipBacktest)
        assert strategy.params == {}

    def test_rsi_reads_its_own_params(self):
        strategy = build_from_request(self._req(strategy="rsi", rsi_period=9))
        assert isinstance(strategy, RSIMeanReversionBacktest)
        assert strategy.rsi_period == 9
        # buy_levels is renamed to what the simulator/live strategy call it
        assert strategy.avg_down_levels == [[30, 0.5], [25, 0.3], [20, 0.2]]

    def test_ha_is_not_rejected_for_rsi_level_shapes(self):
        """An empty ladder is invalid for RSI but meaningless for HA."""
        with pytest.raises(ValueError, match="level_invalid"):
            self._req(strategy="rsi", sell_levels=[])
        assert self._req(strategy="ha", sell_levels=[]).strategy == "ha"

    def test_date_validation_still_applies_to_every_strategy(self):
        with pytest.raises(ValueError, match="date_range_invalid"):
            BacktestRequest(
                strategy="ha", symbol="AAPL", market="nasdaq",
                start_date="2023-01-01", end_date="2022-01-01",
            )


class TestHeikinAshiBacktest:
    def test_flip_up_buys_and_flip_down_sells(self):
        warm = _flat(HeikinAshiFlipBacktest.WARMUP_BARS + 5)
        # A sustained rally flips HA green, a sustained crash flips it red.
        closes = warm + list(np.linspace(100, 160, 12)) + list(np.linspace(160, 70, 12))
        result = run_simulation(_df(closes), "TEST", HeikinAshiFlipBacktest({}))

        assert result["num_buys"] >= 1
        assert result["num_sells"] >= 1
        reasons = {t["reason"] for t in result["sell_trades"]}
        assert "ha_flip_down" in reasons

    def test_all_in_all_out(self):
        """The flip rule has no ladder: one buy fills, one sell empties."""
        warm = _flat(HeikinAshiFlipBacktest.WARMUP_BARS + 5)
        closes = warm + list(np.linspace(100, 160, 12)) + list(np.linspace(160, 70, 12))
        result = run_simulation(_df(closes), "TEST", HeikinAshiFlipBacktest({}))

        for trade in result["trades"]:
            assert trade.portion == 1.0

    def test_flat_series_never_trades(self):
        result = run_simulation(_df(_flat(80)), "TEST", HeikinAshiFlipBacktest({}))
        assert result["num_buys"] == 0
        assert result["num_trades"] == 0

    def test_indicator_panel_matches_the_rsi_backtest(self):
        """Both strategies feed the same chart, so results are comparable.

        RSI drives no HA signal — it is display only.
        """
        closes = _flat(40) + list(np.linspace(100, 140, 20))
        result = run_simulation(_df(closes), "TEST", HeikinAshiFlipBacktest({}))

        assert len(result["rsi_values"]) == len(result["dates"])
        assert all(v is not None for v in result["rsi_values"])
        assert all(0 <= v <= 100 for v in result["rsi_values"])

    def test_signal_lags_the_flip_by_one_bar(self):
        """No lookahead: the flip bar itself is never the fill bar.

        The live strategy only sees a flip once the bar is confirmed after
        the close, and trades the next session.
        """
        closes = _flat(30) + list(np.linspace(100, 150, 15))
        df = _df(closes)
        strategy = HeikinAshiFlipBacktest({})
        prepared = strategy.prepare(df.copy())
        bullish = (prepared["HA_close"] > prepared["HA_open"]).tolist()
        first_flip = next(
            i for i in range(1, len(bullish)) if bullish[i] and not bullish[i - 1]
        )

        result = run_simulation(df, "TEST", HeikinAshiFlipBacktest({}))
        assert result["num_buys"] >= 1
        first_buy = result["buy_trades"][0]["date"]
        assert first_buy == prepared.index[first_flip + 1].strftime("%Y-%m-%d")

    def test_ignores_rsi_params(self):
        """HA takes no parameters; the web sends the RSI ones regardless."""
        closes = _flat(30) + list(np.linspace(100, 150, 15))
        noisy = {"rsi_period": 7, "stop_loss": -3, "sell_levels": [[50, 1.0]]}
        assert (
            run_simulation(_df(closes), "T", HeikinAshiFlipBacktest(noisy))
            ["total_return_pct"]
            == run_simulation(_df(closes), "T", HeikinAshiFlipBacktest({}))
            ["total_return_pct"]
        )

    def test_flip_right_after_the_warmup_boundary_is_not_lost(self):
        """The warm-up slice must not eat the colour context.

        Colours are computed on the full frame, so the first tradable bar
        still has its two predecessors to compare against.
        """
        w = HeikinAshiFlipBacktest.WARMUP_BARS
        closes = _flat(w) + [100, 101, 140, 141, 142, 143]
        result = run_simulation(_df(closes), "TEST", HeikinAshiFlipBacktest({}))

        assert result["num_buys"] >= 1, "flip just past the warm-up was dropped"
        # Warm-up bars stay out of the traded/plotted range.
        assert len(result["dates"]) == len(closes) - w

    def test_empty_frame_returns_the_empty_result(self):
        empty = pd.DataFrame(
            {"Open": [], "High": [], "Low": [], "Close": [], "Volume": []},
            index=pd.DatetimeIndex([]),
        )
        result = run_simulation(empty, "TEST", HeikinAshiFlipBacktest({}))
        assert result["num_trades"] == 0
        assert result["final_capital"] == 10_000_000


class TestEngineContract:
    def test_payload_keys_match_across_strategies(self):
        """Both strategies must feed the same frontend."""
        closes = _flat(30) + list(np.linspace(100, 60, 30)) + list(np.linspace(60, 120, 30))
        rsi = run_simulation(_df(closes), "T", RSIMeanReversionBacktest({}))
        ha = run_simulation(_df(closes), "T", HeikinAshiFlipBacktest({}))
        assert set(rsi) == set(ha)

    def test_both_strategies_render_the_same_chart_panels(self):
        """Price + indicator are populated either way, so the two runs are
        visually comparable — only the trade markers should differ."""
        closes = _flat(40) + list(np.linspace(100, 60, 30)) + list(np.linspace(60, 120, 30))
        rsi = run_simulation(_df(closes), "T", RSIMeanReversionBacktest({}))
        ha = run_simulation(_df(closes), "T", HeikinAshiFlipBacktest({}))

        for result in (rsi, ha):
            for key in ("dates", "prices", "ohlc", "rsi_values"):
                assert len(result[key]) == len(result["dates"]) > 0, key
            assert all(v is not None for v in result["rsi_values"])

    def test_ohlc_is_shipped_for_the_candle_chart(self):
        """The chart's HA toggle needs OHLC, not just closes."""
        closes = _flat(30) + list(np.linspace(100, 140, 20))
        df = _df(closes)
        result = run_simulation(df, "T", RSIMeanReversionBacktest({}))

        assert len(result["ohlc"]) == len(result["dates"])
        for bar in result["ohlc"]:
            o, h, low, c = bar
            assert h >= max(o, c) and low <= min(o, c)
        # Close column must agree with the `prices` series the line chart uses.
        assert [b[3] for b in result["ohlc"]] == result["prices"]

    def test_close_only_frames_ship_no_ohlc(self):
        """Falls back to the line chart instead of shipping broken candles."""
        closes = _flat(30) + list(np.linspace(100, 140, 20))
        df = _df(closes)[["Close"]]
        result = run_simulation(df, "T", RSIMeanReversionBacktest({}))
        assert result["ohlc"] == []

    def test_capital_is_conserved(self):
        """Final capital must equal what the trades actually moved."""
        closes = _flat(30) + list(np.linspace(100, 160, 12)) + list(np.linspace(160, 70, 12))
        result = run_simulation(
            _df(closes), "T", HeikinAshiFlipBacktest({}), initial_capital=1_000_000,
        )
        expected = 1_000_000
        for trade in result["trades"]:
            expected += (trade.sell_price - trade.buy_price) * trade.shares
        assert result["final_capital"] == pytest.approx(expected, rel=1e-9)
