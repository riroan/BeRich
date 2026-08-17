#!/usr/bin/env python3
"""Heikin-Ashi Flip Strategy Backtest

Buy when the HA candle turns 음봉 → 양봉, sell the whole position when it
turns 양봉 → 음봉. Mirrors src/strategy/builtin/heikin_ashi_flip.py: the
flip is read off completed daily bars, so the action lands on the bar
AFTER the one that flipped — the live strategy acts on the first tick of
the next session.
"""

import sys
from pathlib import Path
from typing import Iterator, Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.strategy.rsi_rules import calculate_rsi
from src.strategy.builtin.heikin_ashi_flip import heikin_ashi
from scripts.backtest_engine import (
    BacktestSignal,
    BacktestStrategy,
    run_symbol_async,
)


class HeikinAshiFlipBacktest(BacktestStrategy):
    """All-in on the bullish flip, all-out on the bearish one."""

    key = "ha"

    # The HA open is recursive, but its dependence on the seed halves each
    # bar, so the colors are settled well inside this many bars.
    WARMUP_BARS = 20

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self._bullish: list[bool] = []
        self._index: dict = {}

    @property
    def name(self) -> str:
        return "Heikin-Ashi Flip"

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        # Lowercase OHLC is what heikin_ashi() takes; the backtest frames are
        # capitalized (yfinance shape).
        ha = heikin_ashi(
            df.rename(columns={
                "Open": "open", "High": "high", "Low": "low", "Close": "close",
            })
        )
        df["HA_open"] = ha["ha_open"].to_numpy()
        df["HA_close"] = ha["ha_close"].to_numpy()
        # RSI for the chart only (see indicator_series). Computed BEFORE the
        # warm-up slice so the displayed series has no leading gap — the same
        # reason the HA open is computed on the full frame.
        df["RSI"] = calculate_rsi(df["Close"], 14, "wilder")

        # Colors come from the FULL frame; the warm-up bars are dropped from
        # trading but still serve as the two bars of colour context the first
        # tradable bar needs. Indexing off the full frame (via the offset)
        # keeps a flip right after the boundary from being lost.
        self._bullish = (df["HA_close"] > df["HA_open"]).tolist()
        sliced = df.iloc[self.WARMUP_BARS:] if len(df) > self.WARMUP_BARS else df
        offset = len(df) - len(sliced)
        self._index = {ts: i + offset for i, ts in enumerate(sliced.index)}
        return sliced

    def indicator_series(self, df: pd.DataFrame) -> list[float]:
        """RSI, for the chart's indicator panel only — no signal reads it.

        Mirrors the live strategy's get_current_rsi(): the result chart looks
        the same whichever strategy was simulated, so the two are comparable
        side by side. None where RSI has not warmed up yet (only reachable on
        a range shorter than the warm-up); the chart skips those points.
        """
        return [
            None if pd.isna(v) else round(float(v), 2) for v in df["RSI"]
        ]

    def decide(self, date, row) -> Iterator[BacktestSignal]:
        i = self._index.get(date)
        if i is None or i < 2:
            return

        # The flip sits between the two COMPLETED bars behind us and is acted
        # on here, one bar later — the live strategy sees the flip only once
        # the bar is confirmed after the close, and trades the next session.
        # Filling at this bar's close is the engine's convention.
        flip_up = self._bullish[i - 1] and not self._bullish[i - 2]
        flip_down = not self._bullish[i - 1] and self._bullish[i - 2]

        if flip_up and self.pos.shares == 0:
            yield BacktestSignal(
                side="buy", portion=1.0, reason="ha_flip_up", stage=1,
            )
        elif flip_down and self.pos.shares > 0:
            yield BacktestSignal(
                side="sell", portion=1.0, reason="ha_flip_down", stage=1,
            )


async def backtest_symbol_async(
    symbol: str,
    market: str,
    start_date: str,
    end_date: str,
    params: dict,
    storage,
    initial_capital: float = 10_000_000,
) -> tuple[Optional[dict], Optional[str]]:
    """Web async entry for the HA strategy (thin wrapper over the engine)."""
    return await run_symbol_async(
        symbol, market, start_date, end_date,
        HeikinAshiFlipBacktest(params), storage, initial_capital,
    )
