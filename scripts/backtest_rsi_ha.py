#!/usr/bin/env python3
"""RSI + Heikin-Ashi Strategy Backtest

Mirrors src/strategy/builtin/rsi_heikin_ashi.py: the RSI Mean Reversion
simulation with one extra condition on entries — the last CONFIRMED daily
HA candle must be bullish. Exits are never gated.

Timing matters here. The live strategy computes RSI from a frame that
includes the live forming price, but reads the HA colour off `_daily_bars`,
the confirmed base — so at any moment during day t it is pairing today's
RSI with bar t-1's candle. The backtest reproduces that: bar i's RSI, bar
i-1's colour.
"""

import sys
from pathlib import Path
from typing import Iterator, Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.strategy.builtin.heikin_ashi_flip import heikin_ashi
from scripts.backtest_engine import BacktestSignal, run_symbol_async
from scripts.backtest_rsi import RSIMeanReversionBacktest


class RSIHeikinAshiBacktest(RSIMeanReversionBacktest):
    """Entries need the RSI ladder AND a bullish HA candle."""

    key = "rsi_ha"

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self._bullish: list[bool] = []
        self._index: dict = {}

    @property
    def name(self) -> str:
        return "RSI + Heikin-Ashi"

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        # HA on the FULL frame, before the parent drops the RSI warm-up, so
        # the recursive HA open is seeded from the same history the live
        # strategy has in _daily_bars.
        ha = heikin_ashi(
            df.rename(columns={
                "Open": "open", "High": "high", "Low": "low", "Close": "close",
            })
        )
        df["HA_open"] = ha["ha_open"].to_numpy()
        df["HA_close"] = ha["ha_close"].to_numpy()

        df = super().prepare(df)   # adds RSI, drops the warm-up rows

        self._bullish = (df["HA_close"] > df["HA_open"]).tolist()
        self._index = {ts: i for i, ts in enumerate(df.index)}
        return df

    def _confirmed_bullish(self, date) -> bool:
        """Colour of the last bar that has closed before this one."""
        i = self._index.get(date)
        if i is None or i < 1:
            return False   # no confirmed candle yet — same as the live gate
        return self._bullish[i - 1]

    def decide(self, date, row) -> Iterator[BacktestSignal]:
        allow_buy = self._confirmed_bullish(date)
        for signal in super().decide(date, row):
            if signal.side == "buy" and not allow_buy:
                # Dropping it before the engine sees it means no fill, and
                # every stage counter advances on the fill — so the rung
                # stays armed for the next bar, exactly as it does live.
                continue
            yield signal


async def backtest_symbol_async(
    symbol: str,
    market: str,
    start_date: str,
    end_date: str,
    params: dict,
    storage,
    initial_capital: float = 10_000_000,
) -> tuple[Optional[dict], Optional[str]]:
    """Web async entry for the combined strategy."""
    return await run_symbol_async(
        symbol, market, start_date, end_date,
        RSIHeikinAshiBacktest(params), storage, initial_capital,
    )
