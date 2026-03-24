"""
RSI Mean Reversion Strategy

Rules:
- Buy: RSI <= 30 (oversold), with averaging down
- Sell: RSI >= 65/70/75 (staged selling) OR Stop Loss -10%
- Reset stages after cooldown period
"""

from typing import Optional
from decimal import Decimal
from datetime import datetime, timedelta
import pandas as pd
import logging

from src.core.types import Signal, SignalType, Market
from src.strategy.base import BaseStrategy

logger = logging.getLogger(__name__)


class RSIMeanReversionStrategy(BaseStrategy):
    """RSI Mean Reversion Strategy with Stop Loss, Averaging Down, and Staged Selling"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Track entry prices for stop loss (average price)
        self._entry_prices: dict[str, Decimal] = {}
        # Track buy stages (how many times we've bought)
        self._buy_stages: dict[str, int] = {}
        # Track sell stages (how many times we've sold)
        self._sell_stages: dict[str, int] = {}
        # Track total invested amount
        self._invested: dict[str, Decimal] = {}
        # Track last buy time for cooldown reset
        self._last_buy_time: dict[str, datetime] = {}

    @property
    def name(self) -> str:
        return "RSI_MeanReversion"

    @property
    def required_history(self) -> int:
        return 30

    async def calculate_signal(self, symbol: str) -> Optional[Signal]:
        df = self.get_dataframe(symbol)
        if len(df) < self.required_history:
            return None

        # Parameters
        rsi_period = self.params.get("rsi_period", 14)
        stop_loss_pct = self.params.get("stop_loss", -10)
        cooldown_days = self.params.get("cooldown_days", 1)

        # Averaging down levels: [(RSI threshold, portion)]
        avg_down_levels = self.params.get("avg_down_levels", [
            (30, 0.5),
            (25, 0.3),
            (20, 0.2),
        ])

        # Staged selling levels: [(RSI threshold, portion of current holdings)]
        # 100주 기준: 70→30주 매도(70남음), 75→28주 매도(42남음), 80→21주 매도(21남음)
        # 최종 ~21% 보유 유지
        sell_levels = self.params.get("sell_levels", [
            (70, 0.3),   # RSI 70: 현재 보유량의 30% 매도
            (75, 0.4),   # RSI 75: 현재 보유량의 40% 매도
            (80, 0.5),   # RSI 80: 현재 보유량의 50% 매도
        ])

        # Calculate RSI
        df["rsi"] = self._calculate_rsi(df["close"], rsi_period)

        current_rsi = df["rsi"].iloc[-1]
        current_price = Decimal(str(df["close"].iloc[-1]))
        current_position = self.get_position(symbol)
        current_buy_stage = self._buy_stages.get(symbol, 0)
        current_sell_stage = self._sell_stages.get(symbol, 0)

        # Check cooldown reset for buy stages
        if symbol in self._last_buy_time:
            time_since_last_buy = datetime.now() - self._last_buy_time[symbol]
            if time_since_last_buy >= timedelta(days=cooldown_days):
                self._buy_stages[symbol] = 0
                current_buy_stage = 0

        # Check stop loss first (if in position)
        if current_position > 0 and symbol in self._entry_prices:
            avg_price = self._entry_prices[symbol]
            pnl_pct = float((current_price - avg_price) / avg_price * 100)

            if pnl_pct <= stop_loss_pct:
                logger.info(
                    f"[{symbol}] *** STOP LOSS TRIGGERED *** | "
                    f"PnL: {pnl_pct:.1f}% <= {stop_loss_pct}% | "
                    f"Avg: {avg_price:,} → Current: {current_price:,}"
                )
                self._reset_position(symbol)
                return Signal(
                    signal_type=SignalType.EXIT_LONG,
                    symbol=symbol,
                    market=self.market,
                    strength=1.0,  # 전량 매도
                    metadata={
                        "rsi": float(current_rsi),
                        "reason": "stop_loss",
                        "pnl_pct": pnl_pct,
                        "avg_price": float(avg_price),
                        "sell_portion": 1.0,
                    },
                )

        # Staged selling: Check each sell level
        if current_position > 0:
            next_sell_threshold = None
            for stage, (rsi_threshold, portion) in enumerate(sell_levels):
                if current_sell_stage > stage:
                    continue
                next_sell_threshold = rsi_threshold
                break

            avg_price = self._entry_prices.get(symbol, current_price)
            pnl_pct = float(
                (current_price - avg_price) / avg_price * 100
            ) if avg_price else 0

            logger.info(
                f"[{symbol}] SELL CHECK | RSI: {current_rsi:.1f} | "
                f"Threshold: {next_sell_threshold} | "
                f"Stage: {current_sell_stage}/{len(sell_levels)} | PnL: {pnl_pct:.1f}%"
            )

            for stage, (rsi_threshold, portion) in enumerate(sell_levels):
                # Skip if already sold at this stage
                if current_sell_stage > stage:
                    continue

                # Check if RSI is above threshold
                if current_rsi >= rsi_threshold:
                    self._sell_stages[symbol] = stage + 1

                    avg_price = self._entry_prices.get(symbol, current_price)
                    pnl_pct = float((current_price - avg_price) / avg_price * 100)

                    # Check if this is the last sell stage
                    is_final_sell = (stage + 1) >= len(sell_levels)

                    logger.info(
                        f"[{symbol}] *** SELL SIGNAL GENERATED *** | "
                        f"RSI: {current_rsi:.1f} >= {rsi_threshold} | "
                        f"Stage {stage + 1}/{len(sell_levels)} | "
                        f"Portion: {portion*100:.0f}% | PnL: {pnl_pct:.1f}%"
                    )

                    # If final stage, reset everything
                    if is_final_sell:
                        self._reset_position(symbol)

                    return Signal(
                        signal_type=SignalType.EXIT_LONG,
                        symbol=symbol,
                        market=self.market,
                        strength=portion,  # Portion to sell
                        metadata={
                            "rsi": float(current_rsi),
                            "reason": f"staged_sell_{stage + 1}",
                            "pnl_pct": pnl_pct,
                            "sell_portion": portion,
                            "stage": stage + 1,
                            "total_stages": len(sell_levels),
                            "is_final": is_final_sell,
                        },
                    )

        # Buy signals: Check each averaging down level
        next_buy_threshold = None
        for stage, (rsi_threshold, portion) in enumerate(avg_down_levels):
            if current_buy_stage > stage:
                continue
            next_buy_threshold = rsi_threshold
            break

        # Log buy condition check
        logger.info(
            f"[{symbol}] BUY CHECK | RSI: {current_rsi:.1f} | "
            f"Threshold: {next_buy_threshold} | "
            f"Stage: {current_buy_stage}/{len(avg_down_levels)} | Pos: {current_position}"
        )

        for stage, (rsi_threshold, portion) in enumerate(avg_down_levels):
            if current_buy_stage > stage:
                continue

            if current_rsi <= rsi_threshold:
                self._buy_stages[symbol] = stage + 1
                self._last_buy_time[symbol] = datetime.now()

                # Reset sell stages when buying
                self._sell_stages[symbol] = 0

                # Calculate new average price
                old_invested = self._invested.get(symbol, Decimal("0"))
                new_investment = current_price * Decimal(str(portion))
                total_invested = old_invested + new_investment
                self._invested[symbol] = total_invested

                if old_invested > 0 and symbol in self._entry_prices:
                    old_avg = self._entry_prices[symbol]
                    total = old_avg * old_invested + current_price * new_investment
                    new_avg = total / total_invested
                    self._entry_prices[symbol] = new_avg
                else:
                    self._entry_prices[symbol] = current_price

                logger.info(
                    f"[{symbol}] *** BUY SIGNAL GENERATED *** | "
                    f"RSI: {current_rsi:.1f} <= {rsi_threshold} | "
                    f"Stage {stage + 1}/{len(avg_down_levels)} | "
                    f"Portion: {portion*100:.0f}% | Price: {current_price:,}"
                )

                return Signal(
                    signal_type=SignalType.ENTRY_LONG,
                    symbol=symbol,
                    market=self.market,
                    strength=portion,
                    target_price=current_price * Decimal("1.10"),
                    stop_loss=current_price * Decimal(str(1 + stop_loss_pct / 100)),
                    metadata={
                        "rsi": float(current_rsi),
                        "reason": f"avg_down_stage_{stage + 1}",
                        "entry_price": float(current_price),
                        "avg_price": float(self._entry_prices[symbol]),
                        "stage": stage + 1,
                        "total_stages": len(avg_down_levels),
                    },
                )

        return None

    def sync_position(
        self,
        symbol: str,
        quantity: int,
        avg_price: Decimal,
    ) -> None:
        """Sync position from broker and restore strategy state"""
        if symbol not in self.symbols:
            return

        if quantity <= 0:
            # No position, reset state
            self._reset_position(symbol)
            return

        # Restore position
        self._positions[symbol] = quantity
        self._entry_prices[symbol] = avg_price

        # Assume at least 1 buy stage completed
        self._buy_stages[symbol] = 1
        self._sell_stages[symbol] = 0
        self._invested[symbol] = avg_price * Decimal(str(quantity))

        logger.info(
            f"[{symbol}] Position synced | "
            f"Qty: {quantity} | Avg: {avg_price:,.0f} | "
            f"Value: {avg_price * quantity:,.0f}"
        )

    def _reset_position(self, symbol: str) -> None:
        """Reset all tracking for a symbol after full exit"""
        if symbol in self._entry_prices:
            del self._entry_prices[symbol]
        if symbol in self._buy_stages:
            del self._buy_stages[symbol]
        if symbol in self._sell_stages:
            del self._sell_stages[symbol]
        if symbol in self._invested:
            del self._invested[symbol]
        if symbol in self._last_buy_time:
            del self._last_buy_time[symbol]

    def _calculate_rsi(self, prices: pd.Series, period: int = 14) -> pd.Series:
        """Calculate RSI indicator"""
        delta = prices.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()

        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        return rsi

    def get_current_rsi(self, symbol: str) -> Optional[float]:
        """Get current RSI value for a symbol"""
        df = self.get_dataframe(symbol)
        if len(df) < self.required_history:
            return None

        rsi_period = self.params.get("rsi_period", 14)
        rsi = self._calculate_rsi(df["close"], rsi_period)
        return float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else None

    async def on_fill(self, fill) -> None:
        """Track fills"""
        await super().on_fill(fill)

        if fill.side.value == "buy":
            self._last_buy_time[fill.symbol] = datetime.now()
            # Reset sell stages on buy
            self._sell_stages[fill.symbol] = 0
