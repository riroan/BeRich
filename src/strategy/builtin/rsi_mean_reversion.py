"""
RSI Mean Reversion Strategy (Daily RSI)

Rules:
- Buy: Daily RSI <= 30 (oversold), with averaging down
- Sell: Daily RSI >= 65/70/75 (staged selling) OR Stop Loss -10%
- Reset stages after cooldown period

Note: RSI is calculated from daily bars, not intraday data.
Current price updates today's daily close for real-time RSI estimation.
"""

from typing import Optional
from decimal import Decimal
from datetime import datetime, timedelta
import pandas as pd
import logging

from src.core.types import Signal, SignalType
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
        # Track last buy time for cooldown reset
        self._last_buy_time: dict[str, datetime] = {}
        # Track whether RSI recovered since last full buy cycle
        self._rsi_recovered: dict[str, bool] = {}
        # Store daily bars separately for RSI calculation
        self._daily_bars: dict[str, pd.DataFrame] = {}

    @property
    def name(self) -> str:
        return "RSI_MeanReversion"

    @property
    def required_history(self) -> int:
        return 30

    def initialize(self, historical_bars: dict[str, list]) -> None:
        """Initialize with daily historical data"""
        super().initialize(historical_bars)
        # Store daily bars separately for RSI calculation
        for symbol, bars in historical_bars.items():
            if not bars:
                continue
            df = pd.DataFrame([
                {
                    "timestamp": b.timestamp,
                    "open": float(b.open),
                    "high": float(b.high),
                    "low": float(b.low),
                    "close": float(b.close),
                    "volume": b.volume,
                }
                for b in bars
            ])
            df.set_index("timestamp", inplace=True)
            self._daily_bars[symbol] = df
            logger.info(f"[{symbol}] Loaded {len(df)} daily bars for RSI")

    def update_daily_close(self, symbol: str, current_price: float) -> None:
        """Update today's close price for RSI calculation"""
        if symbol not in self._daily_bars:
            return

        df = self._daily_bars[symbol]
        if len(df) == 0:
            return

        today = datetime.now().date()
        last_date = df.index[-1].date() if hasattr(df.index[-1], 'date') else df.index[-1]

        if last_date == today:
            # Update today's close
            df.iloc[-1, df.columns.get_loc("close")] = current_price
            # Update high/low if needed
            if current_price > df.iloc[-1]["high"]:
                df.iloc[-1, df.columns.get_loc("high")] = current_price
            if current_price < df.iloc[-1]["low"]:
                df.iloc[-1, df.columns.get_loc("low")] = current_price
        else:
            # Add new row for today
            new_row = pd.DataFrame([{
                "timestamp": datetime.now(),
                "open": current_price,
                "high": current_price,
                "low": current_price,
                "close": current_price,
                "volume": 0,
            }]).set_index("timestamp")
            self._daily_bars[symbol] = pd.concat([df, new_row]).tail(100)

    def get_daily_dataframe(self, symbol: str) -> pd.DataFrame:
        """Get daily DataFrame for RSI calculation"""
        return self._daily_bars.get(symbol, pd.DataFrame())

    async def on_bar(self, bar) -> Optional[Signal]:
        """Override to skip update_bar (we use daily data via update_daily_close)"""
        if bar.symbol not in self.symbols:
            return None
        # Don't call update_bar - daily close is updated separately
        return await self.calculate_signal(bar.symbol)

    async def calculate_signal(self, symbol: str) -> Optional[Signal]:
        # Use daily bars for RSI calculation
        df = self.get_daily_dataframe(symbol)
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

        # Track RSI recovery (RSI >= 50 after completing all buy stages)
        recovery_rsi = self.params.get("recovery_rsi", 50)
        if current_buy_stage >= len(avg_down_levels):
            if current_rsi >= recovery_rsi:
                self._rsi_recovered[symbol] = True

        # Check cooldown reset for buy stages
        # Requires: cooldown elapsed AND RSI recovered once
        if symbol in self._last_buy_time:
            time_since_last_buy = datetime.now() - self._last_buy_time[symbol]
            rsi_recovered = self._rsi_recovered.get(symbol, False)
            if time_since_last_buy >= timedelta(days=cooldown_days) and rsi_recovered:
                self._buy_stages[symbol] = 0
                self._sell_stages[symbol] = 0
                self._rsi_recovered[symbol] = False
                current_buy_stage = 0
                logger.info(
                    f"[{symbol}] Buy/sell stages reset "
                    f"(cooldown + RSI recovery)"
                )

        # Check stop loss first (if in position)
        if current_position > 0 and symbol in self._entry_prices:
            avg_price = self._entry_prices[symbol]
            if not avg_price:
                avg_price = current_price
            pnl_pct = float((current_price - avg_price) / avg_price * 100)

            if pnl_pct <= stop_loss_pct:
                # Calculate actual PnL
                pnl = (current_price - avg_price) * current_position
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
                        "pnl": pnl,
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

                    # Calculate PnL for the sold portion
                    sell_qty = int(current_position * Decimal(str(portion)))
                    pnl = (current_price - avg_price) * sell_qty

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
                            "pnl": pnl,
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

                # FIX-007: Don't set entry price on signal generation
                # Entry price is set ONLY via on_fill() or sync_position()
                # to ensure it reflects actual fill price, not signal price

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
                        "avg_price": float(self._entry_prices.get(symbol, current_price)),
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
        if symbol in self._last_buy_time:
            del self._last_buy_time[symbol]
        if symbol in self._rsi_recovered:
            del self._rsi_recovered[symbol]

    def _calculate_rsi(self, prices: pd.Series, period: int = 14) -> pd.Series:
        """Calculate RSI indicator using Wilder's smoothing method"""
        delta = prices.diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)

        # Wilder's smoothing (EMA with alpha = 1/period)
        avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()

        rs = avg_gain / avg_loss.replace(0, 1e-10)  # Avoid division by zero
        rsi = 100 - (100 / (1 + rs))
        return rsi

    def get_current_rsi(self, symbol: str) -> Optional[float]:
        """Get current RSI value for a symbol (based on daily data)"""
        df = self.get_daily_dataframe(symbol)
        if len(df) < self.required_history:
            return None

        rsi_period = self.params.get("rsi_period", 14)
        rsi = self._calculate_rsi(df["close"], rsi_period)
        return float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else None

    async def on_fill(self, fill) -> None:
        """Track fills and update average price"""
        symbol = fill.symbol

        if fill.side.value == "buy":
            # Calculate new average price before updating position
            old_qty = self._positions.get(symbol, 0)
            old_avg = self._entry_prices.get(symbol, Decimal("0"))
            new_qty = fill.quantity
            new_price = fill.price

            if old_qty > 0 and old_avg > 0:
                # Weighted average: (old_qty * old_avg + new_qty * new_price) / total_qty
                total_qty = old_qty + new_qty
                new_avg = (old_avg * old_qty + new_price * new_qty) / total_qty
                self._entry_prices[symbol] = new_avg
            else:
                self._entry_prices[symbol] = new_price

            self._last_buy_time[symbol] = datetime.now()
            self._sell_stages[symbol] = 0

        await super().on_fill(fill)
