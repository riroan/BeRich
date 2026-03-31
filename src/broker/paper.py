"""Paper trading broker - simulates order execution without real money"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional
import logging

from src.core.types import (
    Market,
    Order,
    OrderSide,
    OrderStatus,
    Position,
    Fill,
    Bar,
)
from src.core.events import EventBus, Event, EventType

logger = logging.getLogger("TradingBot")


class PaperBroker:
    """Paper trading broker that simulates order execution.

    Uses real KIS API for price data, but executes orders internally.
    """

    def __init__(
        self,
        event_bus: EventBus,
        real_broker,
        initial_cash_krw: Decimal = Decimal("0"),
        initial_cash_usd: Decimal = Decimal("10000"),
    ):
        self.event_bus = event_bus
        self._real_broker = real_broker  # For price/history data
        self.paper_trading = True

        # Virtual account
        self._cash: Dict[str, Decimal] = {
            "krw": initial_cash_krw,
            "usd": initial_cash_usd,
        }

        # Virtual positions: {symbol: {quantity, avg_price, market}}
        self._positions: Dict[str, dict] = {}

        # Order tracking
        self._orders: Dict[str, Order] = {}
        self._fills: List[Fill] = []

        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def account_no(self) -> str:
        return "PAPER-000000"

    # ==================== Delegate to real broker ====================

    async def connect(self) -> None:
        """Connect (delegates to real broker for auth)"""
        await self._real_broker.connect()
        self._connected = True
        logger.info(
            f"Paper broker connected | "
            f"Cash KRW: {self._cash['krw']:,.0f}, "
            f"USD: ${self._cash['usd']:,.2f}"
        )

    async def disconnect(self) -> None:
        """Disconnect"""
        await self._real_broker.disconnect()
        self._connected = False

    async def get_current_price(
        self, symbol: str, market: Market = Market.KRX,
    ) -> Decimal:
        """Get real current price from KIS API"""
        return await self._real_broker.get_current_price(symbol, market)

    async def get_historical_bars(
        self,
        symbol: str,
        market: Market = Market.KRX,
        days: int = 100,
    ) -> list[Bar]:
        """Get real historical data from KIS API"""
        return await self._real_broker.get_historical_bars(
            symbol, market, days,
        )

    # ==================== Paper trading implementation ====================

    async def submit_order(self, order: Order) -> str:
        """Simulate order execution (immediate fill at current price)"""
        order_id = f"PAPER-{uuid.uuid4().hex[:8].upper()}"
        order.order_id = order_id
        order.status = OrderStatus.SUBMITTED

        # Get current price for fill
        try:
            fill_price = await self.get_current_price(
                order.symbol, order.market,
            )
        except Exception:
            fill_price = order.price or Decimal("0")

        # Determine cash key
        cash_key = "krw" if order.market == Market.KRX else "usd"

        if order.side == OrderSide.BUY:
            cost = fill_price * order.quantity
            if cost > self._cash[cash_key]:
                logger.warning(
                    f"[PAPER] Insufficient cash for {order.symbol}: "
                    f"need {cost:,.2f}, have {self._cash[cash_key]:,.2f}"
                )
                order.status = OrderStatus.REJECTED
                return order_id

            # Deduct cash
            self._cash[cash_key] -= cost

            # Update position
            if order.symbol in self._positions:
                pos = self._positions[order.symbol]
                old_qty = pos["quantity"]
                old_avg = pos["avg_price"]
                new_qty = old_qty + order.quantity
                if new_qty > 0:
                    new_avg = (
                        (old_avg * old_qty + fill_price * order.quantity)
                        / new_qty
                    )
                else:
                    new_avg = fill_price
                pos["quantity"] = new_qty
                pos["avg_price"] = new_avg
            else:
                self._positions[order.symbol] = {
                    "quantity": order.quantity,
                    "avg_price": fill_price,
                    "market": order.market,
                }

            logger.info(
                f"[PAPER] BUY {order.symbol} | "
                f"Qty: {order.quantity} | Price: {fill_price:,.2f} | "
                f"Cost: {cost:,.2f} | Cash: {self._cash[cash_key]:,.2f}"
            )

        elif order.side == OrderSide.SELL:
            pos = self._positions.get(order.symbol)
            if not pos or pos["quantity"] < order.quantity:
                logger.warning(
                    f"[PAPER] Insufficient position for {order.symbol}"
                )
                order.status = OrderStatus.REJECTED
                return order_id

            # Add cash
            proceeds = fill_price * order.quantity
            self._cash[cash_key] += proceeds

            # Calculate PnL
            pnl = (fill_price - pos["avg_price"]) * order.quantity

            # Update position
            pos["quantity"] -= order.quantity
            if pos["quantity"] <= 0:
                del self._positions[order.symbol]

            logger.info(
                f"[PAPER] SELL {order.symbol} | "
                f"Qty: {order.quantity} | Price: {fill_price:,.2f} | "
                f"PnL: {pnl:+,.2f} | Cash: {self._cash[cash_key]:,.2f}"
            )

        # Mark as filled
        order.status = OrderStatus.FILLED
        order.filled_quantity = order.quantity
        order.filled_avg_price = fill_price
        self._orders[order_id] = order

        # Create fill record
        fill = Fill(
            order_id=order_id,
            symbol=order.symbol,
            market=order.market,
            side=order.side,
            quantity=order.quantity,
            price=fill_price,
            commission=Decimal("0"),
            timestamp=datetime.now(),
            pnl=pnl if order.side == OrderSide.SELL else None,
        )
        self._fills.append(fill)

        # Emit fill event
        await self.event_bus.publish(
            Event(
                event_type=EventType.ORDER_FILLED,
                data=order,
                timestamp=datetime.now(),
                source="PaperBroker",
            )
        )

        return order_id

    async def get_positions(
        self, market: Market,
    ) -> List[Position]:
        """Get virtual positions"""
        positions = []
        for symbol, pos in self._positions.items():
            if pos["market"] != market:
                continue
            if pos["quantity"] <= 0:
                continue

            try:
                current_price = await self.get_current_price(
                    symbol, market,
                )
            except Exception:
                current_price = pos["avg_price"]

            unrealized_pnl = (
                (current_price - pos["avg_price"]) * pos["quantity"]
            )

            positions.append(Position(
                symbol=symbol,
                market=market,
                quantity=pos["quantity"],
                avg_entry_price=pos["avg_price"],
                current_price=current_price,
                unrealized_pnl=unrealized_pnl,
            ))

        return positions

    async def get_account_balance(
        self, market: Market,
    ) -> dict:
        """Get virtual account balance"""
        is_usd = market != Market.KRX
        cash_key = "usd" if is_usd else "krw"
        cash = self._cash[cash_key]

        # For USD markets, include all non-KRX positions
        usd_markets = {Market.NASDAQ, Market.NYSE, Market.AMEX}

        # Calculate total position value
        position_value = Decimal("0")
        for symbol, pos in self._positions.items():
            if is_usd and pos["market"] not in usd_markets:
                continue
            if not is_usd and pos["market"] != Market.KRX:
                continue
            try:
                price = await self.get_current_price(
                    symbol, pos["market"],
                )
                position_value += price * pos["quantity"]
            except Exception:
                position_value += pos["avg_price"] * pos["quantity"]

        total_eval = cash + position_value

        # Calculate P&L
        cost_basis = Decimal("0")
        for symbol, pos in self._positions.items():
            if is_usd and pos["market"] not in usd_markets:
                continue
            if not is_usd and pos["market"] != Market.KRX:
                continue
            cost_basis += pos["avg_price"] * pos["quantity"]
        profit_loss = position_value - cost_basis

        return {
            "total_eval": total_eval,
            "cash": cash,
            "stocks_eval": position_value,
            "profit_loss": profit_loss,
        }

    async def cancel_order(
        self, order_id: str, market: Market,
    ) -> bool:
        """Cancel a paper order (always succeeds)"""
        if order_id in self._orders:
            self._orders[order_id].status = OrderStatus.CANCELLED
            return True
        return False
