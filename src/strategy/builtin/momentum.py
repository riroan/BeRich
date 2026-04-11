from decimal import Decimal
import pandas as pd

from src.core.types import Signal, SignalType
from src.strategy.base import BaseStrategy


class MomentumStrategy(BaseStrategy):
    """Momentum strategy using RSI and Moving Averages"""

    @property
    def name(self) -> str:
        return "Momentum"

    @property
    def required_history(self) -> int:
        return 50

    async def calculate_signal(self, symbol: str) -> Signal | None:
        df = self.get_dataframe(symbol)
        if len(df) < self.required_history:
            return None

        # Calculate indicators
        rsi_period = self.params.get("rsi_period", 14)
        fast_ma = self.params.get("fast_ma", 10)
        slow_ma = self.params.get("slow_ma", 20)

        # RSI
        df["rsi"] = self._calculate_rsi(df["close"], rsi_period)

        # Moving averages
        df["fast_ma"] = df["close"].rolling(window=fast_ma).mean()
        df["slow_ma"] = df["close"].rolling(window=slow_ma).mean()

        # Get current values
        current_rsi = df["rsi"].iloc[-1]
        current_fast = df["fast_ma"].iloc[-1]
        current_slow = df["slow_ma"].iloc[-1]
        prev_fast = df["fast_ma"].iloc[-2]
        prev_slow = df["slow_ma"].iloc[-2]
        current_price = df["close"].iloc[-1]

        current_position = self.get_position(symbol)

        # Buy signal: Golden cross + RSI exiting oversold
        if (
            prev_fast <= prev_slow
            and current_fast > current_slow
            and current_rsi > 30
            and current_position == 0
        ):
            strength = min(1.0, (current_rsi - 30) / 40)
            return Signal(
                signal_type=SignalType.ENTRY_LONG,
                symbol=symbol,
                market=self.market,
                strength=strength,
                target_price=Decimal(str(current_price * 1.05)),
                stop_loss=Decimal(str(current_price * 0.97)),
                metadata={"rsi": float(current_rsi), "reason": "golden_cross"},
            )

        # Sell signal: Dead cross or RSI overbought
        if (
            (prev_fast >= prev_slow and current_fast < current_slow)
            or current_rsi > 70
        ) and current_position > 0:
            return Signal(
                signal_type=SignalType.EXIT_LONG,
                symbol=symbol,
                market=self.market,
                strength=0.8,
                metadata={"rsi": float(current_rsi), "reason": "dead_cross_or_overbought"},
            )

        return None

    def _calculate_rsi(self, prices: pd.Series, period: int = 14) -> pd.Series:
        """Calculate RSI indicator using Wilder's smoothing method"""
        delta = prices.diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)

        # Wilder's smoothing (EMA with alpha = 1/period)
        alpha = 1 / period
        avg_gain = gain.ewm(alpha=alpha, min_periods=period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=alpha, min_periods=period, adjust=False).mean()

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi
