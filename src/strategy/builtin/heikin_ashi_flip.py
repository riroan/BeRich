"""
Heikin-Ashi Flip Strategy (Daily)

Rules:
- Buy:  HA candle turns 음봉 → 양봉 (bearish → bullish). Full entry.
- Sell: HA candle turns 양봉 → 음봉 (bullish → bearish). Full exit.

The flip is read off CONFIRMED daily bars only. The forming bar is
deliberately excluded: an unfinished candle changes color as the price
moves, so signalling on it would flip the position back and forth
intraday. A flip is therefore acted on at the first tick of the session
AFTER the bar that flipped — same timing the backtest assumed.

Position state is the only de-dup: the buy condition stays true for the
whole session after a flip, but a filled buy makes get_position() > 0
and the condition stops firing. An unfilled order stays retryable on the
next tick (the order manager's in-flight guard prevents duplicates).
"""

from datetime import date
import logging

import numpy as np
import pandas as pd

from src.core.types import Signal, SignalType
from src.strategy.base import BaseStrategy
from src.strategy.rsi_rules import calculate_rsi

logger = logging.getLogger(__name__)


def heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    """HA close = the bar's OHLC average; HA open = the previous HA bar's
    midpoint. The open is recursive, but its dependence on the seed decays
    by half per bar, so any window past ~20 bars gives the same colors."""
    ha_close = (df["open"] + df["high"] + df["low"] + df["close"]) / 4
    closes = ha_close.to_numpy()
    ha_open = np.empty(len(df))
    ha_open[0] = (df["open"].iloc[0] + df["close"].iloc[0]) / 2
    for i in range(1, len(df)):
        ha_open[i] = (ha_open[i - 1] + closes[i - 1]) / 2
    return pd.DataFrame(
        {"ha_open": ha_open, "ha_close": closes}, index=df.index,
    )


class HeikinAshiFlipStrategy(BaseStrategy):
    """Buy on the HA bearish→bullish flip, sell on bullish→bearish."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Date of the last confirmed daily bar, for confirmation-driven slide.
        self._last_confirmed_date: dict[str, date] = {}

    @property
    def name(self) -> str:
        return "HeikinAshi_Flip"

    @property
    def required_history(self) -> int:
        # Two HA bars make a flip; 20 also covers the HA-open seed decay and
        # the RSI shown on the dashboard, and lets newly-listed symbols start
        # without waiting for a warm-up they have no data for.
        return 20

    @property
    def history_window(self) -> int:
        # One KIS dailyprice call, and the same rolling cap confirm_daily_bar
        # keeps — so the colors are identical whether just restarted or
        # long-running.
        return 100

    def initialize(self, historical_bars: dict[str, list]) -> None:
        """Load the confirmed daily base (BaseStrategy builds the frames)."""
        super().initialize(historical_bars)
        for symbol, df in self._bars.items():
            self._bars[symbol] = df.tail(self.history_window)
            if len(df):
                self._last_confirmed_date[symbol] = self._bar_date(df.index[-1])
            logger.info(
                f"[{symbol}] Loaded {len(self._bars[symbol])} daily bars for HA"
            )

    @staticmethod
    def _bar_date(index_value) -> date:
        return index_value.date() if hasattr(index_value, "date") else index_value

    async def on_bar(self, bar) -> Signal | None:
        """Skip update_bar: tick bars must not enter the daily HA base.

        _bars therefore holds only confirmed daily bars, appended by
        confirm_daily_bar.
        """
        if bar.symbol not in self.symbols:
            return None
        return await self.calculate_signal(bar.symbol)

    def confirm_daily_bar(self, symbol: str, bar) -> str:
        """Fold a newly-confirmed regular-session daily bar into the base.

        The ONLY thing that slides the HA window — called by the post-close
        confirmation poll (bot.core._run_daily_confirm_poll).

        Returns: "appended" (true slide to a newer date), "refreshed" (final
        close for the same date folded in), or "skipped" (stale).
        """
        if symbol not in self._bars:
            return "skipped"

        d = bar.timestamp.date() if hasattr(bar.timestamp, "date") else bar.timestamp
        last = self._last_confirmed_date.get(symbol)
        row = {
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
            "volume": bar.volume,
        }

        if last is None or d > last:
            new_row = pd.DataFrame([row], index=[bar.timestamp])
            self._bars[symbol] = pd.concat(
                [self._bars[symbol], new_row]
            ).tail(self.history_window)
            self._last_confirmed_date[symbol] = d
            logger.info(
                f"[{symbol}] HA base slid → confirmed close {row['close']} ({d})"
            )
            return "appended"

        if d == last:
            df = self._bars[symbol]
            for col, val in row.items():
                df.iloc[-1, df.columns.get_loc(col)] = val
            return "refreshed"

        return "skipped"

    def last_confirmed_date(self, symbol: str) -> date | None:
        """Date of the last confirmed regular-session bar (for the poll)."""
        return self._last_confirmed_date.get(symbol)

    async def calculate_signal(self, symbol: str) -> Signal | None:
        df = self.get_dataframe(symbol)
        if len(df) < self.required_history:
            return None

        ha = heikin_ashi(df)
        bullish = (ha["ha_close"] > ha["ha_open"]).to_numpy()
        flip_up = bullish[-1] and not bullish[-2]
        flip_down = not bullish[-1] and bullish[-2]

        position = self.get_position(symbol)
        price = df["close"].iloc[-1]

        if flip_up and position == 0:
            logger.info(
                f"[{symbol}] *** HA BUY SIGNAL *** | 음봉 → 양봉 | "
                f"HA open {ha['ha_open'].iloc[-1]:,.2f} < "
                f"close {ha['ha_close'].iloc[-1]:,.2f} | Price: {price:,}"
            )
            return Signal(
                signal_type=SignalType.ENTRY_LONG,
                symbol=symbol,
                market=self.market_for(symbol),
                strength=1.0,   # full remaining weight room
                metadata={
                    "reason": "ha_flip_up",
                    "ha_open": float(ha["ha_open"].iloc[-1]),
                    "ha_close": float(ha["ha_close"].iloc[-1]),
                    "entry_price": float(price),
                },
            )

        if flip_down and position > 0:
            logger.info(
                f"[{symbol}] *** HA SELL SIGNAL *** | 양봉 → 음봉 | "
                f"HA open {ha['ha_open'].iloc[-1]:,.2f} > "
                f"close {ha['ha_close'].iloc[-1]:,.2f} | Price: {price:,}"
            )
            return Signal(
                signal_type=SignalType.EXIT_LONG,
                symbol=symbol,
                market=self.market_for(symbol),
                strength=1.0,   # full exit
                metadata={
                    "reason": "ha_flip_down",
                    "ha_open": float(ha["ha_open"].iloc[-1]),
                    "ha_close": float(ha["ha_close"].iloc[-1]),
                    "sell_portion": 1.0,
                },
            )

        return None

    def get_current_rsi(self, symbol: str) -> float | None:
        """RSI is NOT used for signals. It exists because the tick handler
        stores whatever RSI a strategy can report, and the dashboard's price
        charts drop rows without one — a symbol traded only by this strategy
        would otherwise have an empty chart.
        """
        df = self.get_dataframe(symbol)
        if len(df) < self.required_history:
            return None
        rsi = calculate_rsi(
            df["close"],
            self.params.get("rsi_period", 14),
            self.params.get("rsi_method", "wilder"),
        )
        return float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else None
