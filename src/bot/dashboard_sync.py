"""Dashboard synchronization for trading bot"""

import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Iterable, TYPE_CHECKING
from zoneinfo import ZoneInfo
import logging

from src.core.types import Fill, Market, OrderSide
from src.utils.scheduler import is_us_market_holiday
from src.web.app import broadcast_update

if TYPE_CHECKING:
    from src.bot.core import TradingBot

logger = logging.getLogger(__name__)

US_MARKETS = {Market.NASDAQ, Market.NYSE, Market.AMEX}
KST = ZoneInfo("Asia/Seoul")
ET = ZoneInfo("America/New_York")


def _as_datetime(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _us_trade_date(timestamp: datetime | str) -> date:
    ts = _as_datetime(timestamp)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=KST)
    return ts.astimezone(ET).date()


def _add_us_trading_days(start: date, days: int) -> date:
    current = start
    remaining = max(days, 0)
    while remaining > 0:
        current += timedelta(days=1)
        if not is_us_market_holiday(current):
            remaining -= 1
    return current


def _is_us_market(market: Market | str) -> bool:
    if isinstance(market, Market):
        return market in US_MARKETS
    try:
        return Market.from_string(str(market)) in US_MARKETS
    except ValueError:
        return False


def calculate_us_settlement_adjustment(
    fills: Iterable[Fill],
    now: datetime,
    settlement_business_days: int = 1,
) -> Decimal:
    """Cash adjustment for US fills that are executed but not yet settled.

    KIS balance snapshots can temporarily miss sale proceeds or double-count
    freshly bought positions while settlement catches up. Keep raw balances,
    but derive an adjusted equity value by applying pending cash movement.
    """
    if settlement_business_days <= 0:
        return Decimal("0.00")

    now_us_date = _us_trade_date(now)
    adjustment = Decimal("0")

    for fill in fills:
        if not _is_us_market(fill.market):
            continue

        settlement_date = _add_us_trading_days(
            _us_trade_date(fill.timestamp),
            settlement_business_days,
        )
        if now_us_date > settlement_date:
            continue

        notional = Decimal(str(fill.quantity)) * Decimal(str(fill.price))
        commission = Decimal(str(fill.commission or 0))
        side = fill.side.value if isinstance(fill.side, OrderSide) else str(fill.side)

        if side.lower() == OrderSide.SELL.value:
            adjustment += notional - commission
        elif side.lower() == OrderSide.BUY.value:
            adjustment -= notional + commission

    return adjustment.quantize(Decimal("0.01"))


class DashboardSyncMixin:
    """Mixin for dashboard synchronization methods"""

    async def update_dashboard_positions(self: "TradingBot") -> None:
        """Update dashboard with current positions"""
        try:
            strategy_states = self._get_strategy_states()

            # Skip KRX entirely when running US-only — fetching KRX
            # positions/balance every tick is pure wasted API there.
            us_only = bool(self.scheduler and self.scheduler.us_only)
            markets = [Market.NASDAQ, Market.NYSE, Market.AMEX]
            if not us_only:
                markets.insert(0, Market.KRX)

            stored_positions = False
            storage_failed = False
            for market in markets:
                positions = await self._update_market_positions(
                    market,
                    strategy_states,
                )
                if positions is not None:
                    self.dashboard.replace_positions_from_records(
                        positions,
                        market=market.value.upper(),
                    )
                    if self.storage:
                        try:
                            replace_positions = (
                                self.storage.replace_current_positions_for_market
                            )
                            changed = await replace_positions(market, positions)
                            stored_positions = stored_positions or changed
                            # NOTE: price_rsi는 tick 경로(_process_symbol_tick)가
                            # 단일 소스로 기록한다. 여기서 잔고 API 평가가
                            # (ovrs_now_pric1)를 추가로 저장하면 같은 테이블에
                            # 출처/정밀도 다른 가격이 섞이고, 그 행의 rsi는 tick
                            # 가격으로 계산된 값이라 (가격,rsi)가 어긋난다 →
                            # 차트 빗살/노이즈. 그래서 저장하지 않는다.
                        except Exception as e:
                            storage_failed = True
                            logger.warning(
                                "Failed to save current positions for "
                                f"{market.value}: {e}"
                            )
                await asyncio.sleep(1)

            if stored_positions and not storage_failed:
                await self.load_current_positions()

            await self._update_balances(us_only)

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
                        "stage_cooldown_days": int(
                            strategy.params.get("cooldown_days", 1)
                        ),
                        "last_buy_time": strategy._last_buy_time.get(symbol),
                        "last_sell_time": getattr(
                            strategy, "_last_sell_time", {},
                        ).get(symbol),
                        "stop_loss_pct": strategy.params.get("stop_loss", -10),
                    }
        return strategy_states

    async def _update_market_positions(
        self: "TradingBot", market: Market, strategy_states: dict
    ) -> list[dict] | None:
        """Update positions for a specific market"""
        try:
            positions = await self.broker.get_positions(market)
            dashboard_positions = []
            for pos in positions:
                rsi = self.dashboard.rsi_values.get(pos.symbol)
                state = strategy_states.get(pos.symbol, {})
                last_buy_date = None
                if state.get("last_buy_time"):
                    last_buy_date = state["last_buy_time"].isoformat(
                        timespec="seconds"
                    )
                last_sell_date = None
                if state.get("last_sell_time"):
                    last_sell_date = state["last_sell_time"].isoformat(
                        timespec="seconds"
                    )

                pnl_pct = 0
                if pos.avg_entry_price > 0:
                    pnl_pct = float(
                        (pos.current_price - pos.avg_entry_price)
                        / pos.avg_entry_price
                        * 100
                    )

                stop_loss_pct = state.get("stop_loss_pct", -10.0)
                pnl = float(
                    (pos.current_price - pos.avg_entry_price) * pos.quantity
                )
                dashboard_positions.append({
                    "symbol": pos.symbol,
                    "market": market.value.upper(),
                    "quantity": pos.quantity,
                    "avg_price": float(pos.avg_entry_price),
                    "current_price": float(pos.current_price),
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "rsi": rsi,
                    "buy_stage": state.get("buy_stage", 0),
                    "sell_stage": state.get("sell_stage", 0),
                    "max_buy_stages": state.get("max_buy_stages", 3),
                    "max_sell_stages": state.get("max_sell_stages", 3),
                    "stage_cooldown_days": state.get("stage_cooldown_days", 0),
                    "last_buy_date": last_buy_date,
                    "last_sell_date": last_sell_date,
                    "stop_loss_pct": stop_loss_pct,
                    "stop_loss_distance": pnl_pct - stop_loss_pct,
                })

                await self._check_stop_loss_alert(pos, pnl_pct, stop_loss_pct)
            return dashboard_positions

        except Exception as e:
            logger.debug(f"Failed to update positions for {market.value}: {e}")
            return None

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

    async def _fetch_and_apply_balance(
        self: "TradingBot", market: Market,
    ) -> dict:
        """Fetch balance for market and update corresponding dashboard fields"""
        balance = await self.broker.get_account_balance(market)
        logger.debug(f"{market.value} balance response: {balance}")
        if market == Market.KRX:
            self.dashboard.balance_krw = balance.get("total_eval", Decimal("0"))
            self.dashboard.cash_krw = balance.get("cash", Decimal("0"))
            self.dashboard.pnl_krw = balance.get("profit_loss", Decimal("0"))
        else:
            self.dashboard.balance_usd = balance.get("total_eval", Decimal("0"))
            self.dashboard.cash_usd = balance.get("cash", Decimal("0"))
            self.dashboard.pnl_usd = balance.get("profit_loss", Decimal("0"))
        return balance

    async def _update_balances(
        self: "TradingBot", us_only: bool = False,
    ) -> None:
        """Update account balances"""
        try:
            if not us_only:
                await self._fetch_and_apply_balance(Market.KRX)
                await asyncio.sleep(1)
            await self._fetch_and_apply_balance(Market.NASDAQ)

            # Keep risk equity tracking live USD balance so the
            # equity-pct limits scale intraday.
            if self.risk_manager and self.dashboard.balance_usd > 0:
                self.risk_manager.update_account_value(
                    self.dashboard.balance_usd
                )
                self._zero_equity_ticks = 0
            elif self.risk_manager:
                # Risk equity is 0 → every order rejected (fail-safe).
                # Make the otherwise-silent deadlock loud (throttled).
                n = getattr(self, "_zero_equity_ticks", 0) + 1
                self._zero_equity_ticks = n
                if n == 1 or n == 5 or n % 30 == 0:
                    logger.critical(
                        f"USD balance unavailable for {n} tick(s) — bot is "
                        f"rejecting ALL orders (risk equity = 0)"
                    )
                    if self.notifier:
                        await self.notifier.notify_account_error(
                            error=(
                                f"USD balance unavailable for {n} ticks — "
                                f"all orders rejected (risk equity = 0)"
                            ),
                        )

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
        settlement_adjustment_usd = await self._get_us_settlement_adjustment()
        adjusted_total_usd = self.dashboard.balance_usd + settlement_adjustment_usd

        await self.storage.save_equity_snapshot(
            total_krw=self.dashboard.balance_krw,
            total_usd=self.dashboard.balance_usd,
            cash_krw=self.dashboard.cash_krw,
            cash_usd=self.dashboard.cash_usd,
            position_value_krw=position_value_krw,
            position_value_usd=position_value_usd,
            adjusted_total_usd=adjusted_total_usd,
            settlement_adjustment_usd=settlement_adjustment_usd,
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
                "adjusted_total_usd": float(adjusted_total_usd),
                "settlement_adjustment_usd": float(settlement_adjustment_usd),
            }
        )

        # Keep only last 1000 points in memory
        if len(self.dashboard.equity_history) > 1000:
            self.dashboard.equity_history = self.dashboard.equity_history[-1000:]

    async def _get_us_settlement_adjustment(self: "TradingBot") -> Decimal:
        """Calculate pending USD cash movement from recent US fills."""
        if not self.storage:
            return Decimal("0")

        raw_days = 1
        if getattr(self, "config", None):
            try:
                raw_days = self.config.get("trading.us_settlement_business_days", 1)
            except Exception:
                raw_days = 1
        try:
            settlement_days = max(int(raw_days), 0)
        except (TypeError, ValueError):
            settlement_days = 1

        now = datetime.now()
        lookback_days = max(10, settlement_days * 4 + 7)

        try:
            fills = await self.storage.get_fills(
                start=now - timedelta(days=lookback_days),
                end=now,
            )
        except Exception as e:
            logger.warning(f"Failed to calculate settlement adjustment: {e}")
            return Decimal("0")

        return calculate_us_settlement_adjustment(
            fills=fills,
            now=now,
            settlement_business_days=settlement_days,
        )

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

        # Calculate uptime from bot start time
        if self._bot_start_time:
            uptime = datetime.now() - self._bot_start_time
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
