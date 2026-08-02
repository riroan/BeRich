"""
RSI Mean Reversion Strategy (Daily RSI)

Rules:
- Buy: Daily RSI <= 30 (oversold), with averaging down
- Sell: Daily RSI >= 65/70/75 (staged selling) OR Stop Loss -10%
- Repeat current stages after cooldown period

Note: RSI is calculated from daily bars, not intraday data.
Current price updates today's daily close for real-time RSI estimation.
"""

from decimal import Decimal
from datetime import datetime, date, timedelta
import pandas as pd
import logging

from src.core.types import Signal, SignalType
from src.strategy.base import BaseStrategy
from src.strategy.rsi_rules import (
    calculate_rsi,
    as_levels,
    resolve_buy_stage,
    resolve_sell_stage,
    resolve_stop_loss_stage,
    resolve_take_profit_stage,
)

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
        # Track last buy time for buy-stage repetition cooldown
        self._last_buy_time: dict[str, datetime] = {}
        # Track last staged sell time for sell-stage repetition cooldown
        self._last_sell_time: dict[str, datetime] = {}
        # PnL-ladder stages (take profit / stop loss), independent of RSI
        self._tp_stages: dict[str, int] = {}
        self._sl_stages: dict[str, int] = {}
        # When a symbol last went flat. Deliberately survives
        # _reset_position — it is the one fact about a closed position that
        # still matters, and it gates re-entry.
        self._last_exit_time: dict[str, datetime] = {}
        # CONFIRMED regular-session daily bars (immutable base for RSI)
        self._daily_bars: dict[str, pd.DataFrame] = {}
        # Live/forming price — updated every tick in ALL sessions; layered
        # on top of the confirmed base as the most recent RSI point.
        self._live_price: dict[str, float] = {}
        # Date of the last confirmed bar, for confirmation-driven slide.
        self._last_confirmed_date: dict[str, date] = {}

    @property
    def name(self) -> str:
        return "RSI_MeanReversion"

    @property
    def required_history(self) -> int:
        # Single knob for the RSI window: bars fetched at init, the rolling
        # cap kept by confirm_daily_bar, AND the minimum before signals turn
        # on. init size == rolling cap, so RSI uses the same last-N daily bars
        # whether just restarted or long-running (restart-invariant). Must
        # exceed rsi_period (default 14); 20 also lets short-history symbols
        # (e.g. newly-listed ETFs like SPCX) start tracking.
        return 20

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
            if len(df):
                self._last_confirmed_date[symbol] = (
                    self._bar_date(df.index[-1])
                )
            logger.info(f"[{symbol}] Loaded {len(df)} daily bars for RSI")

    @staticmethod
    def _bar_date(index_value) -> date:
        return index_value.date() if hasattr(index_value, "date") else index_value

    def update_daily_close(self, symbol: str, current_price: float) -> None:
        """Update the live/forming price for RSI.

        C': the live slot is refreshed every tick in EVERY session. The
        confirmed base never slides here — sliding happens only when a new
        regular-session daily bar is confirmed (see confirm_daily_bar),
        not on the wall clock.
        """
        self._live_price[symbol] = float(current_price)

    def get_daily_dataframe(self, symbol: str) -> pd.DataFrame:
        """Confirmed base + the live forming row as the most recent point.

        Returns a fresh frame each call so the confirmed base is never
        mutated by downstream RSI computation.
        """
        base = self._daily_bars.get(symbol)
        if base is None or len(base) == 0:
            return pd.DataFrame()

        live = self._live_price.get(symbol)
        if live is None:
            return base

        forming = pd.DataFrame([{
            "open": live,
            "high": live,
            "low": live,
            "close": live,
            "volume": 0,
        }], index=[datetime.now()])
        return pd.concat([base, forming])

    def confirm_daily_bar(self, symbol: str, bar) -> str:
        """Fold a newly-confirmed regular-session daily bar into the base.

        This is the ONLY thing that slides the RSI window. Called by the
        post-close confirmation poll.

        Returns: "appended" (true slide to a newer date), "refreshed"
        (final close for the same date folded in), or "skipped" (stale).
        """
        if symbol not in self._daily_bars:
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
            self._daily_bars[symbol] = pd.concat(
                [self._daily_bars[symbol], new_row]
            ).tail(self.required_history)
            self._last_confirmed_date[symbol] = d
            logger.info(
                f"[{symbol}] RSI base slid → confirmed close {row['close']} "
                f"({d})"
            )
            return "appended"

        if d == last:
            df = self._daily_bars[symbol]
            for col, val in row.items():
                df.iloc[-1, df.columns.get_loc(col)] = val
            return "refreshed"

        return "skipped"

    def last_confirmed_date(self, symbol: str) -> date | None:
        """Date of the last confirmed regular-session bar (for the poll)."""
        return self._last_confirmed_date.get(symbol)

    async def on_bar(self, bar) -> Signal | None:
        """Override to skip update_bar (we use daily data via update_daily_close)"""
        if bar.symbol not in self.symbols:
            return None
        # Don't call update_bar - daily close is updated separately
        return await self.calculate_signal(bar.symbol)

    async def calculate_signal(self, symbol: str) -> Signal | None:
        # Use daily bars for RSI calculation
        df = self.get_daily_dataframe(symbol)
        if len(df) < self.required_history:
            return None

        # Parameters
        rsi_period = self.params.get("rsi_period", 14)
        # PnL ladders. `stop_loss_levels` / `take_profit_levels` stage the
        # exit; the plain `stop_loss` / `take_profit` scalars still work and
        # mean a single stage that exits everything.
        # An explicitly empty ladder means "no exit at this side". Only a
        # missing key falls back to the scalar, so clearing the levels in the
        # UI can never silently re-arm the -10 default.
        if "stop_loss_levels" in self.params:
            stop_loss_levels = self.params["stop_loss_levels"]
        else:
            stop_loss_levels = as_levels(self.params.get("stop_loss", -10))
        if "take_profit_levels" in self.params:
            take_profit_levels = self.params["take_profit_levels"]
        else:
            take_profit_levels = as_levels(self.params.get("take_profit"))
        cooldown_days = self.params.get("cooldown_days", 1)
        # A full exit wipes the symbol's state, so the next tick sees a
        # fresh symbol and stage 1 fires on RSI alone — right after a
        # stop-loss, when RSI is guaranteed to be low. 0 keeps that.
        reentry_cooldown_days = self.params.get("reentry_cooldown_days", 0)

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

        buy_repeat_ready = False
        if symbol in self._last_buy_time:
            time_since_last_buy = datetime.now() - self._last_buy_time[symbol]
            buy_repeat_ready = time_since_last_buy >= timedelta(days=cooldown_days)

        sell_repeat_ready = False
        if current_position > 0 and symbol in self._last_sell_time:
            time_since_last_sell = datetime.now() - self._last_sell_time[symbol]
            sell_repeat_ready = time_since_last_sell >= timedelta(days=cooldown_days)

        # Check stop loss first (if in position)
        if current_position > 0 and symbol in self._entry_prices:
            avg_price = self._entry_prices[symbol]
            if not avg_price:
                avg_price = current_price
            pnl_pct = float((current_price - avg_price) / avg_price * 100)

            # Loss ladder first: a position can only be on one side of the
            # entry, so the two ladders are mutually exclusive, but checking
            # losses first keeps the ordering explicit.
            for levels, stages, reason, resolve in (
                (
                    stop_loss_levels, self._sl_stages, "stop_loss",
                    resolve_stop_loss_stage,
                ),
                (
                    take_profit_levels, self._tp_stages, "take_profit",
                    resolve_take_profit_stage,
                ),
            ):
                if not levels:
                    continue
                stage_idx, _ = resolve(
                    pnl_pct, stages.get(symbol, 0), levels,
                )
                if stage_idx is None:
                    continue

                threshold, portion = levels[stage_idx]
                sell_qty = int(current_position * Decimal(str(portion)))
                # Order manager floors this to 1 share; mirror it so the
                # logged PnL matches what actually gets sold.
                if sell_qty <= 0 and current_position > 0:
                    sell_qty = 1
                pnl = (current_price - avg_price) * sell_qty
                logger.info(
                    f"[{symbol}] *** {reason.upper()} TRIGGERED *** | "
                    f"PnL: {pnl_pct:.1f}% vs {threshold}% | "
                    f"Stage {stage_idx + 1}/{len(levels)} | "
                    f"Portion: {portion * 100:.0f}% | "
                    f"Avg: {avg_price:,} → Current: {current_price:,}"
                )
                # State advance stays in on_fill: an exit that doesn't fill
                # (e.g. extended hours) must NOT move the ladder.
                return Signal(
                    signal_type=SignalType.EXIT_LONG,
                    symbol=symbol,
                    market=self.market_for(symbol),
                    strength=portion,
                    metadata={
                        "rsi": float(current_rsi),
                        "reason": reason,
                        "pnl": pnl,
                        "pnl_pct": pnl_pct,
                        "avg_price": float(avg_price),
                        "sell_portion": portion,
                        "stage": stage_idx + 1,
                        "total_stages": len(levels),
                    },
                )

        # Staged selling: progress to the next stage immediately when its
        # threshold is hit. Cooldown repeats the current stage, except after
        # the final stage where it restarts the ladder at stage 1.
        if current_position > 0:
            sell_stage_idx, next_sell_threshold = resolve_sell_stage(
                current_rsi, current_sell_stage, sell_levels, sell_repeat_ready,
            )

            avg_price = self._entry_prices.get(symbol, current_price)
            pnl_pct = float(
                (current_price - avg_price) / avg_price * 100
            ) if avg_price else 0

            logger.info(
                f"[{symbol}] SELL CHECK | RSI: {current_rsi:.1f} | "
                f"Threshold: {next_sell_threshold} | "
                f"Stage: {current_sell_stage}/{len(sell_levels)} | PnL: {pnl_pct:.1f}%"
            )

            if sell_stage_idx is not None:
                rsi_threshold, portion = sell_levels[sell_stage_idx]
                avg_price = self._entry_prices.get(symbol, current_price)
                pnl_pct = float((current_price - avg_price) / avg_price * 100)

                # Check if this is the last sell stage
                is_final_sell = (sell_stage_idx + 1) >= len(sell_levels)

                # Calculate PnL for the sold portion
                sell_qty = int(current_position * Decimal(str(portion)))
                pnl = (current_price - avg_price) * sell_qty

                logger.info(
                    f"[{symbol}] *** SELL SIGNAL GENERATED *** | "
                    f"RSI: {current_rsi:.1f} >= {rsi_threshold} | "
                    f"Stage {sell_stage_idx + 1}/{len(sell_levels)} | "
                    f"Portion: {portion*100:.0f}% | PnL: {pnl_pct:.1f}%"
                )

                # Stage advance + final-exit reset moved to on_fill
                # (target stage carried in metadata["stage"]).
                return Signal(
                    signal_type=SignalType.EXIT_LONG,
                    symbol=symbol,
                    market=self.market_for(symbol),
                    strength=portion,  # Portion to sell
                    metadata={
                        "rsi": float(current_rsi),
                        "reason": f"staged_sell_{sell_stage_idx + 1}",
                        "pnl": pnl,
                        "pnl_pct": pnl_pct,
                        "avg_price": float(avg_price),
                        "sell_portion": portion,
                        "stage": sell_stage_idx + 1,
                        "total_stages": len(sell_levels),
                        "is_final": is_final_sell,
                    },
                )

        if (
            reentry_cooldown_days
            and current_position <= 0
            and (last_exit := self._last_exit_time.get(symbol)) is not None
            and datetime.now() - last_exit
            < timedelta(days=reentry_cooldown_days)
        ):
            logger.info(
                f"[{symbol}] RE-ENTRY BLOCKED | exited "
                f"{(datetime.now() - last_exit).days}d ago < "
                f"{reentry_cooldown_days}d"
            )
            return None

        # Buy signals: progress to the next stage immediately when its
        # threshold is hit. Cooldown repeats the current stage, except after
        # the final stage where it restarts the ladder at stage 1.
        buy_stage_idx, next_buy_threshold = resolve_buy_stage(
            current_rsi, current_buy_stage, avg_down_levels, buy_repeat_ready,
        )

        # Log buy condition check
        logger.info(
            f"[{symbol}] BUY CHECK | RSI: {current_rsi:.1f} | "
            f"Threshold: {next_buy_threshold} | "
            f"Stage: {current_buy_stage}/{len(avg_down_levels)} | Pos: {current_position}"
        )

        if buy_stage_idx is not None:
            rsi_threshold, portion = avg_down_levels[buy_stage_idx]
            # Stage advance and last-buy time both move to on_fill
            # (target stage carried in metadata["stage"]).
            # Counters now reflect ACTUAL fills, so an unfilled order
            # leaves the stage retryable next tick (in-flight guard in
            # the order manager prevents duplicate orders meanwhile).

            # FIX-007: Don't set entry price on signal generation
            # Entry price is set ONLY via on_fill() or sync_position()
            # to ensure it reflects actual fill price, not signal price

            logger.info(
                f"[{symbol}] *** BUY SIGNAL GENERATED *** | "
                f"RSI: {current_rsi:.1f} <= {rsi_threshold} | "
                f"Stage {buy_stage_idx + 1}/{len(avg_down_levels)} | "
                f"Portion: {portion*100:.0f}% | Price: {current_price:,}"
            )

            return Signal(
                signal_type=SignalType.ENTRY_LONG,
                symbol=symbol,
                market=self.market_for(symbol),
                strength=portion,
                target_price=current_price * Decimal("1.10"),
                # First rung of the loss ladder — the level where an exit
                # first fires, which is what a protective stop means here.
                stop_loss=(
                    current_price
                    * Decimal(str(1 + stop_loss_levels[0][0] / 100))
                    if stop_loss_levels else None
                ),
                metadata={
                    "rsi": float(current_rsi),
                    "reason": f"avg_down_stage_{buy_stage_idx + 1}",
                    "entry_price": float(current_price),
                    "avg_price": float(self._entry_prices.get(symbol, current_price)),
                    "stage": buy_stage_idx + 1,
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
        self._tp_stages[symbol] = 0
        self._sl_stages[symbol] = 0

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
        if symbol in self._last_sell_time:
            del self._last_sell_time[symbol]
        if symbol in self._tp_stages:
            del self._tp_stages[symbol]
        if symbol in self._sl_stages:
            del self._sl_stages[symbol]

    def _calculate_rsi(self, prices: pd.Series, period: int = 14) -> pd.Series:
        """Calculate RSI using the method configured in strategy params"""
        return calculate_rsi(
            prices, period, self.params.get("rsi_method", "wilder"),
        )

    def get_current_rsi(self, symbol: str) -> float | None:
        """Get current RSI value for a symbol (based on daily data)"""
        df = self.get_daily_dataframe(symbol)
        if len(df) < self.required_history:
            return None

        rsi_period = self.params.get("rsi_period", 14)
        rsi = self._calculate_rsi(df["close"], rsi_period)
        return float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else None

    async def on_fill(self, fill) -> None:
        """Advance stage counters, average price, and resets — all on the
        ACTUAL fill (C' #8).

        Moving these off signal-generation means an unfilled order (e.g.
        extended-hours) never buries a stage or drops a still-held
        position. The target stage rides in ``fill.metadata["stage"]`` so
        it stays idempotent across partial fills (set, not increment).
        The order manager's in-flight guard prevents duplicate orders
        while a stage is pending.
        """
        symbol = fill.symbol
        meta = fill.metadata or {}
        reason = str(meta.get("reason", ""))
        target_stage = meta.get("stage")

        if fill.side.value == "buy":
            # Weighted average using the PRE-update position.
            old_qty = self._positions.get(symbol, 0)
            old_avg = self._entry_prices.get(symbol, Decimal("0"))
            new_qty = fill.quantity
            new_price = fill.price

            if old_qty > 0 and old_avg > 0:
                total_qty = old_qty + new_qty
                self._entry_prices[symbol] = (
                    (old_avg * old_qty + new_price * new_qty) / total_qty
                )
            else:
                self._entry_prices[symbol] = new_price

            if target_stage is not None:
                self._buy_stages[symbol] = target_stage
            self._last_buy_time[symbol] = datetime.now()
            if old_qty <= 0:
                self._sell_stages[symbol] = 0
                self._last_exit_time.pop(symbol, None)

        await super().on_fill(fill)  # updates _positions

        if fill.side.value == "sell":
            # Each ladder advances its own counter on the fill. The PnL
            # ladders deliberately do NOT touch _last_sell_time: that clock
            # only gates RSI-stage repetition, which they do not use.
            # A rung is spent only when the order that claimed it filled in
            # full. A partial fill moves the money but leaves the rung armed,
            # so the next tick re-sizes against what is actually still held.
            # Without this, one partial fill retires a one-rung ladder — any
            # scalar stop_loss — and the remainder is never sold.
            if target_stage is not None and fill.complete:
                if reason == "stop_loss":
                    self._sl_stages[symbol] = target_stage
                elif reason == "take_profit":
                    self._tp_stages[symbol] = target_stage
                else:
                    self._sell_stages[symbol] = target_stage
                    self._last_sell_time[symbol] = datetime.now()

            # Reset only once the position is actually flat, so a partial
            # fill (e.g. extended hours) never wipes ladder state.
            if self.get_position(symbol) <= 0:
                self._reset_position(symbol)
                self._last_exit_time[symbol] = datetime.now()
