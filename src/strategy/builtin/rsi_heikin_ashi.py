"""
RSI + Heikin-Ashi Strategy (Daily)

The RSI Mean Reversion strategy with one extra condition on entries: the
last CONFIRMED daily Heikin-Ashi candle must be bullish (양봉). Everything
else — the averaging-down ladder, staged sells, the stop-loss and
take-profit ladders, cooldowns, stage bookkeeping — is inherited unchanged,
and so are the parameters.

Only buys are gated. An exit must never be blocked by an indicator that
says "the trend still looks fine": the whole point of the loss ladder is
to fire when it does not.

The two conditions pull against each other by construction — RSI <= 35
means the price has been falling, a bullish HA candle means it has been
rising — so this trades far less than either parent. Measured over
2018-2026 on 14 symbols, only 6.6% of the RSI entry signals landed on a
bullish HA candle (122 of 1,862).
"""

import logging

import pandas as pd

from src.core.types import Signal, SignalType
from src.strategy.builtin.heikin_ashi_flip import heikin_ashi
from src.strategy.builtin.rsi_mean_reversion import RSIMeanReversionStrategy

logger = logging.getLogger(__name__)


class RSIHeikinAshiStrategy(RSIMeanReversionStrategy):
    """Buy only when the RSI ladder AND the HA candle colour both agree."""

    @property
    def name(self) -> str:
        return "RSI_HeikinAshi"

    def heikin_ashi_bullish(self, symbol: str) -> bool | None:
        """Colour of the last confirmed daily HA candle.

        None when there is not enough confirmed history to judge. Reads
        `_daily_bars` — the confirmed base — rather than
        get_daily_dataframe(), which layers the live forming price on top:
        a forming candle changes colour as the price moves, so gating on it
        would let the same RSI signal pass and fail within one session.
        """
        df = self._daily_bars.get(symbol)
        if df is None or len(df) < 2:
            return None
        ha = heikin_ashi(df)
        close, open_ = ha["ha_close"].iloc[-1], ha["ha_open"].iloc[-1]
        if pd.isna(close) or pd.isna(open_):
            return None
        return bool(close > open_)

    async def calculate_signal(self, symbol: str) -> Signal | None:
        signal = await super().calculate_signal(symbol)
        if signal is None or signal.signal_type != SignalType.ENTRY_LONG:
            return signal

        bullish = self.heikin_ashi_bullish(symbol)
        if not bullish:
            logger.info(
                f"[{symbol}] BUY BLOCKED by HA | "
                f"{'음봉' if bullish is False else '확정 일봉 부족'} | "
                f"RSI stage {signal.metadata.get('stage')} would have fired"
            )
            # No signal means no fill, and every stage counter advances on
            # the fill — so the rung stays armed for the next tick.
            return None

        ha = heikin_ashi(self._daily_bars[symbol])
        signal.metadata["ha_open"] = float(ha["ha_open"].iloc[-1])
        signal.metadata["ha_close"] = float(ha["ha_close"].iloc[-1])
        logger.info(
            f"[{symbol}] HA CONFIRMED (양봉) | "
            f"open {signal.metadata['ha_open']:,.2f} < "
            f"close {signal.metadata['ha_close']:,.2f}"
        )
        return signal
