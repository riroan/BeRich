"""Dashboard synchronization for trading bot"""

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING
import logging

from src.core.types import Market
from src.web.app import broadcast_update

if TYPE_CHECKING:
    from src.bot.core import TradingBot

logger = logging.getLogger(__name__)


class DashboardSyncMixin:
    """Mixin for dashboard synchronization methods"""

    async def update_dashboard_positions(self: "TradingBot") -> None:
        """Update dashboard with current positions"""
        try:
            strategy_states = self._get_strategy_states()

            for market in [Market.KRX, Market.NASDAQ, Market.NYSE, Market.AMEX]:
                await self._update_market_positions(market, strategy_states)
                await asyncio.sleep(1)

            await self._update_balances()

        except Exception as e:
            logger.debug(f"Error updating dashboard positions: {e}")

    def _get_strategy_states(self: "TradingBot") -> dict:
        """Get strategy states for all symbols"""
        strategy_states = {}
        for strategy in self.strategy_engine.get_strategies():
            if hasattr(strategy, "_buy_stages"):
                for symbol in strategy.symbols:
                    strategy_states[symbol] = {
                        "buy_stage": strategy._buy_stages.get(symbol, 0),
                        "sell_stage": strategy._sell_stages.get(symbol, 0),
                        "max_buy_stages": len(
                            strategy.params.get(
                                "avg_down_levels", [(30, 0.5), (25, 0.3), (20, 0.2)]
                            )
                        ),
                        "max_sell_stages": len(
                            strategy.params.get(
                                "sell_levels", [(70, 0.3), (75, 0.4), (80, 0.5)]
                            )
                        ),
                        "last_buy_time": strategy._last_buy_time.get(symbol),
                        "stop_loss_pct": strategy.params.get("stop_loss", -10),
                    }
        return strategy_states

    async def _update_market_positions(
        self: "TradingBot", market: Market, strategy_states: dict
    ) -> None:
        """Update positions for a specific market"""
        try:
            positions = await self.broker.get_positions(market)
            for pos in positions:
                rsi = self.dashboard.rsi_values.get(pos.symbol)
                state = strategy_states.get(pos.symbol, {})
                last_buy_date = None
                if state.get("last_buy_time"):
                    last_buy_date = state["last_buy_time"].strftime("%m-%d %H:%M")

                pnl_pct = 0
                if pos.avg_entry_price > 0:
                    pnl_pct = float(
                        (pos.current_price - pos.avg_entry_price)
                        / pos.avg_entry_price
                        * 100
                    )

                stop_loss_pct = state.get("stop_loss_pct", -10.0)

                self.dashboard.update_position(
                    symbol=pos.symbol,
                    market=market.value.upper(),
                    quantity=pos.quantity,
                    avg_price=float(pos.avg_entry_price),
                    current_price=float(pos.current_price),
                    rsi=rsi,
                    buy_stage=state.get("buy_stage", 0),
                    sell_stage=state.get("sell_stage", 0),
                    max_buy_stages=state.get("max_buy_stages", 3),
                    max_sell_stages=state.get("max_sell_stages", 3),
                    last_buy_date=last_buy_date,
                    stop_loss_pct=stop_loss_pct,
                )

                await self._check_stop_loss_alert(pos, pnl_pct, stop_loss_pct)

        except Exception:
            pass  # Skip markets with no positions

    async def _check_stop_loss_alert(
        self: "TradingBot", pos, pnl_pct: float, stop_loss_pct: float
    ) -> None:
        """Check and send stop loss alerts"""
        distance_to_stop = pnl_pct - stop_loss_pct
        alert_attr = f"_stop_loss_alert_{pos.symbol}"

        if distance_to_stop <= 2.0 and distance_to_stop > 0:
            if self.notifier and not hasattr(self, alert_attr):
                await self.notifier.notify_stop_loss_imminent(
                    symbol=pos.symbol,
                    current_pnl_pct=pnl_pct,
                    stop_loss_pct=stop_loss_pct,
                    distance_pct=distance_to_stop,
                )
                setattr(self, alert_attr, True)
        else:
            if hasattr(self, alert_attr):
                delattr(self, alert_attr)

    async def _update_balances(self: "TradingBot") -> None:
        """Update account balances"""
        try:
            krw_balance = await self.broker.get_account_balance(Market.KRX)
            self.dashboard.balance_krw = krw_balance.get("total_eval", Decimal("0"))
            self.dashboard.cash_krw = krw_balance.get("cash", Decimal("0"))
            self.dashboard.pnl_krw = krw_balance.get("profit_loss", Decimal("0"))

            await asyncio.sleep(1)

            usd_balance = await self.broker.get_account_balance(Market.NASDAQ)
            self.dashboard.balance_usd = usd_balance.get("total_eval", Decimal("0"))
            self.dashboard.cash_usd = usd_balance.get("cash", Decimal("0"))
            self.dashboard.pnl_usd = usd_balance.get("profit_loss", Decimal("0"))

            await self._save_equity_snapshot()
            await self._check_low_cash_alert()

        except Exception as e:
            logger.error(f"Failed to update balances: {e}")
            if self.notifier:
                await self.notifier.notify_account_error(error=str(e))

    async def _save_equity_snapshot(self: "TradingBot") -> None:
        """Save equity snapshot periodically"""
        self._equity_save_counter += 1
        if self._equity_save_counter < self._equity_save_interval:
            return

        self._equity_save_counter = 0

        position_value_krw = self.dashboard.balance_krw - self.dashboard.cash_krw
        position_value_usd = self.dashboard.balance_usd - self.dashboard.cash_usd

        await self.storage.save_equity_snapshot(
            total_krw=self.dashboard.balance_krw,
            total_usd=self.dashboard.balance_usd,
            cash_krw=self.dashboard.cash_krw,
            cash_usd=self.dashboard.cash_usd,
            position_value_krw=position_value_krw,
            position_value_usd=position_value_usd,
        )

        self.dashboard.equity_history.append(
            {
                "timestamp": datetime.now().isoformat(),
                "total_krw": float(self.dashboard.balance_krw),
                "total_usd": float(self.dashboard.balance_usd),
                "cash_krw": float(self.dashboard.cash_krw),
                "cash_usd": float(self.dashboard.cash_usd),
                "position_value_krw": float(position_value_krw),
                "position_value_usd": float(position_value_usd),
            }
        )

        # Keep only last 1000 points in memory
        if len(self.dashboard.equity_history) > 1000:
            self.dashboard.equity_history = self.dashboard.equity_history[-1000:]

    async def _check_low_cash_alert(self: "TradingBot") -> None:
        """Check and send low cash ratio alerts"""
        if self.dashboard.balance_usd <= 0:
            return

        cash_ratio = float(
            self.dashboard.cash_usd / self.dashboard.balance_usd * 100
        )
        min_cash_ratio = 10.0

        if cash_ratio < min_cash_ratio:
            if self.notifier and not hasattr(self, "_low_cash_alert"):
                await self.notifier.notify_low_cash_ratio(
                    cash_ratio=cash_ratio,
                    min_ratio=min_cash_ratio,
                )
                self._low_cash_alert = True
        else:
            if hasattr(self, "_low_cash_alert"):
                delattr(self, "_low_cash_alert")

    async def update_dashboard_status(self: "TradingBot") -> None:
        """Update dashboard with current bot status"""
        strategy_names = [s.name for s in self.strategy_engine.get_strategies()]

        # Calculate uptime
        if await self._warmup.is_complete() or self._warmup._start_time:
            start = self._warmup._start_time or datetime.now()
            uptime = datetime.now() - start
            hours, remainder = divmod(int(uptime.total_seconds()), 3600)
            minutes = remainder // 60
            uptime_str = f"{hours}h {minutes}m"
        else:
            uptime_str = "0m"

        self.dashboard.set_bot_status(
            running=self._running,
            paper_trading=self.broker.paper_trading,
            strategies=strategy_names,
            uptime=uptime_str,
            warmup_remaining=await self._warmup.get_remaining_str(),
        )

        self.dashboard.account_value = self.risk_manager.account_value
        self.dashboard.last_strategy_run = datetime.now()
        self.dashboard.update_system_status(
            auto_trading=await self._warmup.is_complete(),
            api_connected=True,
            account_tradable=True,
            data_ok=True,
        )

    async def broadcast_tick_update(self: "TradingBot") -> None:
        """Broadcast tick update to WebSocket clients"""
        try:
            await broadcast_update("tick")
        except Exception as e:
            logger.debug(f"WebSocket broadcast failed: {e}")
