from typing import Callable
from datetime import datetime, timedelta
from decimal import Decimal
import logging

from src.core.types import (
    Order,
    Signal,
    SignalType,
    OrderSide,
    OrderType,
)
from src.core.events import EventBus, Event, EventType
from src.broker.kis.client import KISBroker
from src.risk.manager import RiskManager
from src.data.storage import Storage
from src.utils.notifier import DiscordNotifier
from src.web.app import get_dashboard_state

logger = logging.getLogger(__name__)


class OrderManager:
    """Order manager - converts signals to orders"""

    def __init__(
        self,
        event_bus: EventBus,
        broker: KISBroker,
        risk_manager: RiskManager,
        storage: Storage,
        is_trading_enabled: Callable = None,
        notifier: DiscordNotifier | None = None,
    ):
        self.event_bus = event_bus
        self.broker = broker
        self.risk_manager = risk_manager
        self.storage = storage
        async def _default_enabled():
            return True
        self._is_trading_enabled = is_trading_enabled or _default_enabled
        self.notifier = notifier

        self._pending_orders: dict[str, Order] = {}
        self._active_orders: dict[str, Order] = {}
        # FIX-001: Duplicate order prevention
        # Key: (symbol, side) → timestamp of last order
        self._recent_orders: dict[tuple[str, str], datetime] = {}
        self._dedup_window = timedelta(seconds=60)

    async def start(self) -> None:
        """Start order manager"""
        self.event_bus.subscribe(EventType.SIGNAL_GENERATED, self._on_signal)
        self.event_bus.subscribe(EventType.ORDER_FILLED, self._on_fill)
        self.event_bus.subscribe(EventType.ORDER_PARTIAL_FILLED, self._on_partial_fill)
        logger.info("Order manager started")

    async def stop(self) -> None:
        """Stop order manager"""
        await self.cancel_all_orders()
        logger.info("Order manager stopped")

    async def _on_signal(self, event: Event) -> None:
        """Handle signal events"""
        signal: Signal = event.data["signal"]
        strategy_name: str = event.data["strategy"]

        # Check if trading is enabled (warmup check)
        if not await self._is_trading_enabled():
            logger.info(f"[WARMUP] Signal ignored: {signal.signal_type.name} {signal.symbol}")
            return

        # Check if trading is paused
        dashboard = get_dashboard_state()
        if dashboard.trading_paused:
            logger.info(f"[PAUSED] Signal ignored: {signal.signal_type.name} {signal.symbol}")
            return

        logger.info(f"Processing signal from {strategy_name}: {signal.signal_type.name} {signal.symbol}")

        # FIX-001: Duplicate order prevention
        side_key = "buy" if signal.signal_type == SignalType.ENTRY_LONG else "sell"
        dedup_key = (signal.symbol, side_key)
        now = datetime.now()
        if (
            (last_order_time := self._recent_orders.get(dedup_key))
            and (now - last_order_time) < self._dedup_window
        ):
            logger.warning(
                f"[DEDUP] Duplicate signal ignored: {side_key} {signal.symbol} "
                f"(last order {(now - last_order_time).seconds}s ago)"
            )
            return

        # Convert signal to order
        order = await self._signal_to_order(signal)
        if not order:
            logger.debug("No order generated for signal")
            return

        # Validate with risk manager
        is_valid, reject_reason = self.risk_manager.validate_order(order)
        if not is_valid:
            logger.warning(f"Order rejected by risk manager: {reject_reason}")
            return

        # FIX-001: Record order timestamp for dedup
        self._recent_orders[dedup_key] = now

        # Submit order
        logger.info(
            f"*** SUBMITTING ORDER *** | {order.side.value.upper()} {order.symbol} | "
            f"Qty: {order.quantity} | Price: {order.price:,} | "
            f"Value: {order.quantity * order.price:,}"
        )
        await self._submit_order(order, signal_metadata=signal.metadata)

    async def _signal_to_order(self, signal: Signal) -> Order | None:
        """Convert signal to order"""
        # Determine order side
        if signal.signal_type == SignalType.ENTRY_LONG:
            side = OrderSide.BUY
        elif signal.signal_type == SignalType.EXIT_LONG:
            side = OrderSide.SELL
        else:
            return None

        # Get current price
        try:
            price = await self.broker.get_current_price(signal.symbol, signal.market)
        except Exception as e:
            logger.error(f"Failed to get price for {signal.symbol}: {e}")
            return None

        # Calculate buy quantity based on portfolio weight
        if signal.signal_type == SignalType.ENTRY_LONG:
            quantity = await self._calculate_buy_quantity(
                signal.symbol, price, signal.strength,
            )
            if quantity <= 0:
                return None

        # For exit signals, use current position (with portion support)
        if signal.signal_type == SignalType.EXIT_LONG:
            quantity = 0
            positions = await self.broker.get_positions(signal.market)
            for pos in positions:
                if pos.symbol == signal.symbol:
                    sell_portion = signal.strength if signal.strength else 1.0
                    quantity = int(pos.quantity * Decimal(str(sell_portion)))

                    if quantity == 0 and pos.quantity > 0:
                        quantity = 1

                    logger.info(
                        f"Selling {sell_portion*100:.0f}% of {signal.symbol}: "
                        f"{quantity}/{pos.quantity} shares"
                    )
                    break

            if quantity <= 0:
                logger.debug(f"No position to sell for {signal.symbol}")
                return None

        return Order(
            symbol=signal.symbol,
            market=signal.market,
            side=side,
            order_type=OrderType.MARKET,
            quantity=quantity,
            price=price,
        )

    async def _calculate_buy_quantity(
        self, symbol: str, price, stage_portion: float,
    ) -> int:
        """Calculate buy quantity: total_value × max_weight × stage_portion / price

        Example: $10,000 total × 20% weight × 30% stage = $600 → 3 shares at $200
        """
        dashboard = get_dashboard_state()
        total_value = float(dashboard.balance_usd)
        if total_value <= 0:
            logger.debug(f"[{symbol}] No portfolio value")
            return 0

        # Get max_weight from strategy_configs DB
        max_weight = 20.0  # default
        if dashboard.storage:
            try:
                configs = (
                    await dashboard.storage
                    .get_all_strategy_configs()
                )
                for cfg in configs:
                    for s in cfg.get("symbols", []):
                        sym = (
                            s["symbol"]
                            if isinstance(s, dict) else s
                        )
                        if sym == symbol:
                            max_weight = (
                                s.get("max_weight", 20.0)
                                if isinstance(s, dict)
                                else 20.0
                            )
                            break
            except Exception:
                pass

        # Check current position value
        current_value = 0.0
        if (pos := dashboard.positions.get(symbol)):
            current_value = pos.current_price * pos.quantity

        # Max allowed value for this symbol
        max_symbol_value = total_value * max_weight / 100

        # Already at or over limit
        if current_value >= max_symbol_value:
            logger.info(
                f"[{symbol}] Skipping buy: weight "
                f"{current_value / total_value * 100:.1f}% >= "
                f"limit {max_weight:.0f}%"
            )
            return 0

        # Target buy amount = total × weight × stage portion
        buy_amount = total_value * (max_weight / 100) * stage_portion

        # Check available cash
        cash = float(dashboard.cash_usd + dashboard.cash_krw)

        # Don't exceed available cash
        buy_amount = min(buy_amount, cash)

        # Don't exceed remaining weight room
        remaining_room = max_symbol_value - current_value
        buy_amount = min(buy_amount, remaining_room)

        if buy_amount <= 0:
            return 0

        quantity = int(buy_amount / float(price))

        logger.info(
            f"[{symbol}] Buy calc: "
            f"${total_value:,.0f} × {max_weight:.0f}% × "
            f"{stage_portion * 100:.0f}% = ${buy_amount:,.0f} "
            f"→ {quantity} shares @ ${float(price):,.2f}"
        )

        return quantity

    async def _submit_order(self, order: Order, signal_metadata: dict = None) -> None:
        """Submit order to broker"""
        try:
            # FIX-003: Attach PnL before submission
            if signal_metadata and order.side == OrderSide.SELL:
                if (pnl := signal_metadata.get("pnl")) is not None:
                    order.realized_pnl = pnl

            # FIX-003: Register in active_orders BEFORE submit to prevent lost fills
            order_id = await self.broker.submit_order(order)
            self._active_orders[order_id] = order
            await self.storage.save_order(order)
            logger.info(f"Order submitted: {order_id}")

            # Add to dashboard trade log
            dashboard = get_dashboard_state()
            action = "buy" if order.side == OrderSide.BUY else "sell"
            if signal_metadata:
                if signal_metadata.get("reason") == "stop_loss":
                    action = "stop_loss"
                elif "staged_sell" in str(signal_metadata.get("reason", "")):
                    action = "partial_sell"

            trigger_rule = signal_metadata.get("reason", "manual") if signal_metadata else "manual"
            rsi = signal_metadata.get("rsi") if signal_metadata else None

            dashboard.add_trade_log(
                symbol=order.symbol,
                market=order.market.value.upper(),
                action=action,
                price=float(order.price),
                quantity=order.quantity,
                trigger_rule=trigger_rule,
                result="success",
                rsi=rsi,
            )

            # Add signal to dashboard
            dashboard.add_signal({
                "type": "ENTRY_LONG" if order.side == OrderSide.BUY else "EXIT_LONG",
                "symbol": order.symbol,
                "rsi": rsi,
                "reason": trigger_rule,
            })

            # Add order to dashboard
            dashboard.add_order({
                "symbol": order.symbol,
                "side": order.side.value.upper(),
                "quantity": order.quantity,
                "price": float(order.price),
            })

            # Send Discord notification based on order type
            if self.notifier:
                market_type = "KRX" if order.market.value == "krx" else "USD"

                if order.side == OrderSide.BUY:
                    # Buy execution notification
                    stage = signal_metadata.get("stage", 1) if signal_metadata else 1
                    total_stages = signal_metadata.get("total_stages", 3) if signal_metadata else 3
                    await self.notifier.notify_buy_executed(
                        symbol=order.symbol,
                        price=order.price,
                        quantity=order.quantity,
                        rsi=rsi or 0,
                        stage=stage,
                        total_stages=total_stages,
                        market=market_type,
                    )
                elif action == "stop_loss":
                    # Stop loss execution notification (HIGH PRIORITY)
                    pnl = signal_metadata.get("pnl", Decimal("0")) if signal_metadata else Decimal("0")
                    pnl_pct = signal_metadata.get("pnl_pct", 0) if signal_metadata else 0
                    await self.notifier.notify_stop_loss_executed(
                        symbol=order.symbol,
                        price=order.price,
                        quantity=order.quantity,
                        pnl=pnl,
                        pnl_pct=pnl_pct,
                        market=market_type,
                    )
                else:
                    # Sell execution notification
                    stage = signal_metadata.get("stage", 1) if signal_metadata else 1
                    total_stages = signal_metadata.get("total_stages", 3) if signal_metadata else 3
                    pnl = signal_metadata.get("pnl", Decimal("0")) if signal_metadata else Decimal("0")
                    pnl_pct = signal_metadata.get("pnl_pct", 0) if signal_metadata else 0
                    is_partial = action == "partial_sell"
                    await self.notifier.notify_sell_executed(
                        symbol=order.symbol,
                        price=order.price,
                        quantity=order.quantity,
                        rsi=rsi or 0,
                        pnl=pnl,
                        pnl_pct=pnl_pct,
                        stage=stage,
                        total_stages=total_stages,
                        is_partial=is_partial,
                        market=market_type,
                    )
        except Exception as e:
            logger.error(f"Failed to submit order: {e}")

            # Add failed trade to dashboard
            dashboard = get_dashboard_state()
            dashboard.add_trade_log(
                symbol=order.symbol,
                market=order.market.value.upper(),
                action="buy" if order.side == OrderSide.BUY else "sell",
                price=float(order.price),
                quantity=order.quantity,
                trigger_rule="failed",
                result="failed",
            )

            # Send order failed notification (HIGH PRIORITY)
            if self.notifier:
                await self.notifier.notify_order_failed(
                    symbol=order.symbol,
                    side=order.side.value,
                    reason=str(e),
                )

            await self.event_bus.publish(
                Event(
                    event_type=EventType.ERROR,
                    data={"error": str(e), "order": order},
                    timestamp=datetime.now(),
                    source="OrderManager",
                )
            )

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an order"""
        order = self._active_orders.get(order_id)
        if not order:
            return False

        success = await self.broker.cancel_order(order_id, order.market)
        if success:
            del self._active_orders[order_id]
            await self.storage.save_order(order)
        return success

    async def cancel_all_orders(self, symbol: str | None = None) -> int:
        """Cancel all orders"""
        cancelled = 0
        for order_id, order in list(self._active_orders.items()):
            if symbol and order.symbol != symbol:
                continue
            if await self.cancel_order(order_id):
                cancelled += 1
        return cancelled

    async def _on_fill(self, event: Event) -> None:
        """Handle fill events"""
        order: Order = event.data
        if order.order_id in self._active_orders:
            del self._active_orders[order.order_id]

        # Record PnL
        if order.filled_avg_price and order.side == OrderSide.SELL:
            pnl = getattr(order, "realized_pnl", None)
            if pnl is not None:
                self.risk_manager.record_trade(Decimal(str(pnl)))
            else:
                self.risk_manager.record_trade(Decimal("0"))

        await self.storage.save_order(order)

    async def _on_partial_fill(self, event: Event) -> None:
        """Handle partial fill events"""
        order: Order = event.data
        await self.storage.save_order(order)
