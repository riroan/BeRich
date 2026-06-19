"""Backtest feature tests — _run_simulation, load_price_history, /api/backtest."""

from __future__ import annotations

import os
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest

# DASHBOARD_PASSWORD must be set before importing src.web.app
os.environ.setdefault("DASHBOARD_PASSWORD", "test")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from scripts.backtest_rsi import (  # noqa: E402
    _run_simulation,
    backtest_symbol,
    backtest_symbol_async,
    load_price_history,
)
from src.core.types import Bar, Market  # noqa: E402


# ---------- helpers ----------

def _make_df(closes: list[float], start: str = "2022-01-03") -> pd.DataFrame:
    """Build a minimal OHLCV df with a business-day index."""
    idx = pd.bdate_range(start=start, periods=len(closes))
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
        "stop_loss": -10,
        "cooldown_days": 1,
        "avg_down_levels": [[30, 0.5], [25, 0.3], [20, 0.2]],
        "sell_levels": [[65, 0.3], [70, 0.3], [75, 0.4]],
    }
    p.update(overrides)
    return p


def _bars(closes: list[float], start: str = "2022-01-03") -> list[Bar]:
    """Build Bar list as if from storage.get_bars()."""
    idx = pd.bdate_range(start=start, periods=len(closes))
    return [
        Bar(
            symbol="X",
            market=Market.KRX,
            open=Decimal(str(c)),
            high=Decimal(str(c * 1.01)),
            low=Decimal(str(c * 0.99)),
            close=Decimal(str(c)),
            volume=1000,
            timestamp=ts.to_pydatetime(),
            timeframe="1d",
        )
        for ts, c in zip(idx, closes)
    ]


# ---------- _run_simulation: core branches ----------

class TestRunSimulation:
    def test_stop_loss_triggers(self):
        # Sharp drop: 100 → 80 means -20% from buy at 100, triggers stop_loss at -10
        closes = [100.0] * 30 + [70.0] * 30  # huge drop, RSI low → buy → stop_loss
        df = _make_df(closes)
        result = _run_simulation(df, "TEST", _params())
        # Should have at least one stop_loss
        assert any(t["reason"] == "stop_loss" for t in result["sell_trades"])

    def test_buy_stage_progression(self):
        # Steep decline within long cooldown window — RSI keeps falling so
        # higher buy stages fire (with cooldown_days=1, the stage resets daily
        # and only stage 1 ever fires; raise cooldown to allow stage progression).
        closes = list(np.linspace(100, 40, 60))
        df = _make_df(closes)
        result = _run_simulation(df, "TEST", _params(cooldown_days=30))
        stages_seen = sorted({t["stage"] for t in result["buy_trades"]})
        assert 1 in stages_seen
        # All stages must be valid 1-3
        assert all(1 <= s <= 3 for s in stages_seen)

    def test_sell_stage_progression(self):
        # V-shape: deep dip (buy triggers) then recovery (sells trigger)
        down = list(np.linspace(100, 60, 25))
        up = list(np.linspace(60, 130, 35))
        df = _make_df(down + up)
        result = _run_simulation(df, "TEST", _params())
        sell_stages = [t["stage"] for t in result["sell_trades"] if t["reason"].startswith("sell_stage_")]
        assert len(sell_stages) >= 1

    def test_cooldown_reset(self):
        # Two distinct dip cycles separated by recovery + cooldown days
        closes = (
            list(np.linspace(100, 60, 20))   # first dip → buy
            + list(np.linspace(60, 110, 20)) # recover → sell stages
            + list(np.linspace(110, 65, 25)) # second dip after cooldown → re-buy
        )
        df = _make_df(closes)
        result = _run_simulation(df, "TEST", _params(cooldown_days=2))
        # Multiple buy events with stage 1 means cooldown reset and re-bought
        stage_1_buys = [t for t in result["buy_trades"] if t["stage"] == 1]
        assert len(stage_1_buys) >= 2

    def test_cooldown_reset_does_not_require_rsi_recovery_by_default(self):
        closes = list(np.linspace(100, 60, 60))
        df = _make_df(closes)
        result = _run_simulation(
            df,
            "TEST",
            _params(
                stop_loss=-99,
                cooldown_days=2,
                avg_down_levels=[[30, 0.1], [25, 0.1], [20, 0.1]],
            ),
        )

        stage_1_buys = [t for t in result["buy_trades"] if t["stage"] == 1]
        assert len(stage_1_buys) > 1

    def test_cooldown_reset_can_require_rsi_recovery(self):
        closes = list(np.linspace(100, 60, 60))
        df = _make_df(closes)
        result = _run_simulation(
            df,
            "TEST",
            _params(
                stop_loss=-99,
                cooldown_days=2,
                reset_requires_recovery=True,
                recovery_rsi=50,
                avg_down_levels=[[30, 0.1], [25, 0.1], [20, 0.1]],
            ),
        )

        stage_1_buys = [t for t in result["buy_trades"] if t["stage"] == 1]
        assert len(stage_1_buys) == 1

    def test_zero_trades_when_rsi_neutral(self):
        # Flat-ish prices keep RSI in 35-65 range → no buy/sell triggers
        closes = [100.0 + (i % 2) * 0.1 for i in range(80)]
        df = _make_df(closes)
        result = _run_simulation(df, "TEST", _params())
        assert result["num_trades"] == 0
        assert result["buy_trades"] == []
        assert result["sell_trades"] == []
        # Buy-and-hold computed from first vs last close
        assert "buy_hold_return_pct" in result

    def test_end_of_period_close(self):
        # Buy fires (RSI low) but no sell signal happens before data ends
        # → end_of_period close
        down = list(np.linspace(100, 60, 25))
        flat = [60.0] * 5  # don't recover enough to trigger sells
        df = _make_df(down + flat)
        result = _run_simulation(df, "TEST", _params())
        end_period = [t for t in result["sell_trades"] if t["reason"] == "end_of_period"]
        assert len(end_period) == 1


# ---------- load_price_history: hybrid branches ----------

class TestLoadPriceHistory:
    @pytest.mark.asyncio
    async def test_db_hit_above_threshold(self):
        # ~260 weekdays in 1 year, give 240 bars (>80% threshold)
        closes = [100.0 + i * 0.1 for i in range(240)]
        storage = MagicMock()
        storage.get_bars = AsyncMock(return_value=_bars(closes))

        df, source = await load_price_history(
            "005930", "krx", "2022-01-03", "2023-01-03", storage,
        )
        assert source == "kis_db"
        # Decimal cast to float verified
        assert df["Close"].dtype == np.float64
        assert df["Open"].dtype == np.float64

    @pytest.mark.asyncio
    async def test_db_miss_falls_to_yfinance(self):
        storage = MagicMock()
        storage.get_bars = AsyncMock(return_value=[])

        yf_df = _make_df([100.0 + i * 0.1 for i in range(240)])
        with patch("scripts.backtest_rsi.yf.download", return_value=yf_df):
            df, source = await load_price_history(
                "005930", "krx", "2022-01-03", "2023-01-03", storage,
            )
        assert source == "yfinance"
        assert not df.empty

    @pytest.mark.asyncio
    async def test_db_partial_below_threshold_falls_back(self):
        # Only 50 bars when ~260 weekdays expected → below 80%
        storage = MagicMock()
        storage.get_bars = AsyncMock(return_value=_bars([100.0] * 50))

        yf_df = _make_df([100.0 + i * 0.1 for i in range(240)])
        with patch("scripts.backtest_rsi.yf.download", return_value=yf_df):
            df, source = await load_price_history(
                "005930", "krx", "2022-01-03", "2023-01-03", storage,
            )
        assert source == "yfinance"

    @pytest.mark.asyncio
    async def test_yfinance_empty_returns_none(self):
        storage = MagicMock()
        storage.get_bars = AsyncMock(return_value=[])

        with patch("scripts.backtest_rsi.yf.download", return_value=pd.DataFrame()):
            df, source = await load_price_history(
                "BOGUS", "krx", "2022-01-03", "2023-01-03", storage,
            )
        assert source == "none"
        assert df.empty

    @pytest.mark.asyncio
    async def test_decimal_cast_to_float(self):
        # Verify Decimal('1234.56') survives the Bar→DataFrame conversion as float
        storage = MagicMock()
        storage.get_bars = AsyncMock(return_value=_bars([1234.56] * 240))

        df, source = await load_price_history(
            "005930", "krx", "2022-01-03", "2023-01-03", storage,
        )
        assert source == "kis_db"
        assert df["Close"].iloc[0] == pytest.approx(1234.56)
        assert isinstance(df["Close"].iloc[0], (float, np.floating))

    @pytest.mark.asyncio
    async def test_invalid_market_raises(self):
        storage = MagicMock()
        with pytest.raises(ValueError, match="Unknown market"):
            await load_price_history(
                "005930", "KOSDAQ", "2022-01-03", "2023-01-03", storage,
            )

    @pytest.mark.asyncio
    async def test_yfinance_timeout_returns_timeout(self):
        import asyncio as aio
        storage = MagicMock()
        storage.get_bars = AsyncMock(return_value=[])

        # Make yf.download block long enough to exceed wait_for(timeout=10)
        # Use patch on asyncio.wait_for to immediately raise TimeoutError
        async def fake_wait_for(coro, timeout):
            # Cancel the coroutine and raise
            if hasattr(coro, "close"):
                coro.close()
            raise aio.TimeoutError()

        with patch("scripts.backtest_rsi.asyncio.wait_for", side_effect=fake_wait_for):
            df, source = await load_price_history(
                "005930", "krx", "2022-01-03", "2023-01-03", storage,
            )
        assert source == "timeout"


# ---------- Regression: backtest_symbol output unchanged ----------

class TestRegression:
    def test_backtest_symbol_uses_run_simulation(self):
        """backtest_symbol() must produce the same shape after refactor.
        Specifically: required keys present + numeric types correct.
        """
        df = _make_df(list(np.linspace(100, 60, 25)) + list(np.linspace(60, 110, 25)))
        with patch("scripts.backtest_rsi.yf.download", return_value=df):
            result = backtest_symbol(
                "TEST", "krx", "2022-01-03", "2022-04-03", _params(),
            )
        assert result is not None
        # Original fields preserved
        for key in (
            "symbol", "total_return_pct", "buy_hold_return_pct",
            "num_trades", "win_rate_pct", "final_capital", "trades",
        ):
            assert key in result
        # New fields added
        for key in ("prices", "dates", "rsi_values", "buy_trades", "sell_trades", "data_source"):
            assert key in result
        assert result["data_source"] == "yfinance"
        assert result["market"] == "krx"


# ---------- backtest_symbol_async wrapper ----------

class TestBacktestSymbolAsync:
    @pytest.mark.asyncio
    async def test_happy_path(self):
        # 240 bars covers a 1-year period above the 80% threshold
        closes = list(np.linspace(100, 60, 120)) + list(np.linspace(60, 110, 120))
        storage = MagicMock()
        storage.get_bars = AsyncMock(return_value=_bars(closes))

        result, err = await backtest_symbol_async(
            "005930", "krx", "2022-01-03", "2023-01-03", _params(), storage,
        )
        assert err is None
        assert result is not None
        assert result["data_source"] == "kis_db"

    @pytest.mark.asyncio
    async def test_ticker_not_found(self):
        storage = MagicMock()
        storage.get_bars = AsyncMock(return_value=[])

        with patch("scripts.backtest_rsi.yf.download", return_value=pd.DataFrame()):
            result, err = await backtest_symbol_async(
                "BOGUS", "krx", "2022-01-03", "2022-04-03", _params(), storage,
            )
        assert result is None
        assert err == "ticker_not_found"


# ---------- BacktestRequest validators ----------

class TestBacktestRequest:
    def test_valid_request(self):
        from src.web.app import BacktestRequest
        m = BacktestRequest(symbol="005930", start_date="2022-01-01", end_date="2024-01-01")
        assert m.symbol == "005930"
        assert m.market == "krx"
        assert m.rsi_period == 14

    def test_date_range_exceeds_5_years(self):
        from src.web.app import BacktestRequest
        with pytest.raises(Exception, match="date_range_invalid"):
            BacktestRequest(symbol="X", start_date="2018-01-01", end_date="2024-01-02")

    def test_end_before_start(self):
        from src.web.app import BacktestRequest
        with pytest.raises(Exception, match="date_range_invalid"):
            BacktestRequest(symbol="X", start_date="2024-01-01", end_date="2022-01-01")

    def test_buy_levels_portion_out_of_range(self):
        from src.web.app import BacktestRequest
        with pytest.raises(Exception, match="level_invalid"):
            BacktestRequest(
                symbol="X", start_date="2022-01-01", end_date="2023-01-01",
                buy_levels=[[30, 1.5]],
            )

    def test_sell_levels_portion_out_of_range(self):
        from src.web.app import BacktestRequest
        with pytest.raises(Exception, match="level_invalid"):
            BacktestRequest(
                symbol="X", start_date="2022-01-01", end_date="2023-01-01",
                sell_levels=[[65, -0.1]],
            )

    def test_rsi_period_out_of_range(self):
        from src.web.app import BacktestRequest
        with pytest.raises(Exception):
            BacktestRequest(symbol="X", start_date="2022-01-01", end_date="2023-01-01", rsi_period=4)
        with pytest.raises(Exception):
            BacktestRequest(symbol="X", start_date="2022-01-01", end_date="2023-01-01", rsi_period=31)
