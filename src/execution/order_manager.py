from typing import Dict, Optional, Callable
from datetime import datetime
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
        is_trading_enabled: Callable[[], bool] = None,
        notifier: Optional[DiscordNotifier] = None,
    ):
        self.event_bus = event_bus
        self.broker = broker
        self.risk_manager = risk_manager
        self.storage = storage
        self._is_trading_enabled = is_trading_enabled or (lambda: True)
        self.notifier = notifier

        self._pending_orders: Dict[str, Order] = {}
        self._active_orders: Dict[str, Order] = {}

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
        if not self._is_trading_enabled():
            logger.info(f"[WARMUP] Signal ignored: {signal.signal_type.name} {signal.symbol}")
            return

        logger.info(f"Processing signal from {strategy_name}: {signal.signal_type.name} {signal.symbol}")

        # Convert signal to order
        order = await self._signal_to_order(signal)
        if not order:
            logger.debug(f"No order generated for signal")
            return

        # Validate with risk manager
        is_valid, reject_reason = self.risk_manager.validate_order(order)
        if not is_valid:
            logger.warning(f"Order rejected by risk manager: {reject_reason}")
            return

        # Submit order
        logger.info(
            f"*** SUBMITTING ORDER *** | {order.side.value.upper()} {order.symbol} | "
            f"Qty: {order.quantity} | Price: {order.price:,} | "
            f"Value: {order.quantity * order.price:,}"
        )
        await self._submit_order(order, signal_metadata=signal.metadata)

    async def _signal_to_order(self, signal: Signal) -> Optional[Order]:
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

        # Update available cash before calculating position size (for buy orders)
        if signal.signal_type == SignalType.ENTRY_LONG:
            try:
                balance = await self.broker.get_account_balance(signal.market)
                self.risk_manager.update_available_cash(balance["cash"])
                logger.debug(f"Available cash: {balance['cash']:,}")
            except Exception as e:
                logger.error(f"Failed to get account balance: {e}")
                return None

        # Calculate position size
        quantity = self.risk_manager.calculate_position_size(
            symbol=signal.symbol,
            price=price,
            signal_strength=signal.strength,
        )

        if quantity <= 0:
            logger.debug(f"Position size is 0 for {signal.symbol}")
            return None

        # For exit signals, use current position (with portion support)
        if signal.signal_type == SignalType.EXIT_LONG:
            positions = await self.broker.get_positions(signal.market)
            for pos in positions:
                if pos.symbol == signal.symbol:
                    # Use signal strength as sell portion (default: 100%)
                    sell_portion = signal.strength if signal.strength else 1.0
                    quantity = int(pos.quantity * Decimal(str(sell_portion)))

                    # Ensure at least 1 share if we have position
                    if quantity == 0 and pos.quantity > 0:
                        quantity = 1

                    logger.info(
                        f"Selling {sell_portion*100:.0f}% of {signal.symbol}: "
                        f"{quantity}/{pos.quantity} shares"
                    )
                    break

        return Order(
            symbol=signal.symbol,
            market=signal.market,
            side=side,
            order_type=OrderType.MARKET,
            quantity=quantity,
            price=price,
        )

    async def _submit_order(self, order: Order, signal_metadata: dict = None) -> None:
        """Submit order to broker"""
        try:
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

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> int:
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
        if order.filled_avg_price:
            self.risk_manager.record_trade(Decimal("0"))  # TODO: Calculate actual PnL

        await self.storage.save_order(order)

    async def _on_partial_fill(self, event: Event) -> None:
        """Handle partial fill events"""
        order: Order = event.data
        await self.storage.save_order(order)
