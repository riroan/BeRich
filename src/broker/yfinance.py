"""KIS-free paper broker backed by yfinance market data."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pandas as pd
import yfinance as yf

from src.core.events import Event, EventBus, EventType
from src.core.types import Bar, Fill, Market, Order, OrderSide, OrderStatus, Position

logger = logging.getLogger("TradingBot")

USD_MARKETS = {Market.NASDAQ, Market.NYSE, Market.AMEX}


def to_yfinance_symbol(symbol: str, market: Market) -> str:
    """Map a BeRich symbol/market pair to a yfinance ticker."""
    normalized = symbol.strip().upper()
    if market in USD_MARKETS:
        return normalized
    if market == Market.KRX:
        if normalized.endswith((".KS", ".KQ")):
            return normalized
        if normalized.isdigit() and len(normalized) == 6:
            return f"{normalized}.KS"
    return normalized


def row_to_bar(symbol: str, market: Market, timestamp, row) -> Bar:
    """Convert a yfinance OHLCV row into BeRich's Bar type."""
    return Bar(
        symbol=symbol,
        market=market,
        open=Decimal(str(row["Open"])),
        high=Decimal(str(row["High"])),
        low=Decimal(str(row["Low"])),
        close=Decimal(str(row["Close"])),
        volume=int(row.get("Volume", 0) or 0),
        timestamp=(timestamp.to_pydatetime() if hasattr(timestamp, "to_pydatetime") else timestamp),
        timeframe="1d",
    )


class YFinanceBroker:
    """Paper broker that uses yfinance for prices and persists virtual state."""

    def __init__(
        self,
        event_bus: EventBus,
        initial_cash_krw: Decimal = Decimal("0"),
        initial_cash_usd: Decimal = Decimal("10000"),
        state_path: str | Path = "data/yfinance_paper_state.json",
    ):
        self.event_bus = event_bus
        self.paper_trading = True
        self._connected = False
        self._initial_cash = {
            "krw": Decimal(str(initial_cash_krw)),
            "usd": Decimal(str(initial_cash_usd)),
        }
        self._cash: dict[str, Decimal] = dict(self._initial_cash)
        self._positions: dict[str, dict] = {}
        self._orders: dict[str, Order] = {}
        self._fills: list[Fill] = []
        self._state_lock = asyncio.Lock()
        self.state_path = Path(state_path)

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def account_no(self) -> str:
        return "YFINANCE-PAPER"

    async def connect(self) -> None:
        """Connect the paper broker and load persisted state if present."""
        self._load_state()
        self._connected = True
        logger.info(
            "YFinance paper broker connected | Cash KRW: %s, USD: $%s",
            f"{self._cash['krw']:,.0f}",
            f"{self._cash['usd']:,.2f}",
        )
        await self.event_bus.publish(
            Event(
                event_type=EventType.BROKER_CONNECTED,
                data={"broker": "yfinance", "paper_trading": True},
                timestamp=datetime.now(),
                source="YFinanceBroker",
            )
        )

    async def disconnect(self) -> None:
        self._connected = False
        logger.info("YFinance paper broker disconnected")

    def reset_state(self) -> None:
        """Reset paper account state and remove persisted state file."""
        self._reset_in_memory_state()
        if self.state_path.exists():
            self.state_path.unlink()

    def _reset_in_memory_state(self) -> None:
        """Restore the in-memory paper account to its initial empty state."""
        self._cash = dict(self._initial_cash)
        self._positions = {}
        self._orders = {}
        self._fills = []

    def _history(
        self,
        symbol: str,
        market: Market,
        period: str,
        interval: str,
    ) -> pd.DataFrame:
        ticker = to_yfinance_symbol(symbol, market)
        return yf.Ticker(ticker).history(
            period=period,
            interval=interval,
            auto_adjust=False,
        )

    async def get_current_price(
        self,
        symbol: str,
        market: Market = Market.KRX,
    ) -> Decimal:
        """Get the latest available close price from yfinance."""
        df = self._history(symbol, market, period="5d", interval="1d")
        if df.empty or "Close" not in df:
            raise ValueError(f"No yfinance price data for {symbol} ({market.value})")
        close_values = df["Close"].dropna()
        if close_values.empty:
            raise ValueError(f"No yfinance close price for {symbol} ({market.value})")
        return Decimal(str(close_values.iloc[-1]))

    async def get_historical_bars(
        self,
        symbol: str,
        market: Market = Market.KRX,
        days: int = 100,
    ) -> list[Bar]:
        """Get daily historical bars from yfinance."""
        period_days = max(days * 2, 120)
        df = self._history(symbol, market, period=f"{period_days}d", interval="1d")
        if df.empty:
            raise ValueError(f"No yfinance historical data for {symbol} ({market.value})")
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        return [row_to_bar(symbol, market, idx, row) for idx, row in df.tail(days).iterrows()]

    async def submit_order(self, order: Order) -> str:
        """Simulate immediate order execution at the current yfinance price."""
        async with self._state_lock:
            order_id = f"YF-PAPER-{uuid4().hex[:8].upper()}"
            order.order_id = order_id
            order.status = OrderStatus.SUBMITTED
            pnl = None

            try:
                fill_price = await self.get_current_price(order.symbol, order.market)
                if fill_price <= Decimal("0"):
                    raise ValueError(f"Non-positive yfinance price: {fill_price}")
            except Exception as exc:
                logger.warning(
                    "[YF PAPER] Rejecting %s %s: failed to get fill price: %s",
                    order.side.value,
                    order.symbol,
                    exc,
                )
                order.status = OrderStatus.REJECTED
                self._orders[order_id] = order
                self._save_state()
                return order_id

            cash_key = self._cash_key(order.market)

            if order.side == OrderSide.BUY:
                cost = fill_price * order.quantity
                if cost > self._cash[cash_key]:
                    logger.warning(
                        "[YF PAPER] Insufficient cash for %s: need %s, have %s",
                        order.symbol,
                        f"{cost:,.2f}",
                        f"{self._cash[cash_key]:,.2f}",
                    )
                    order.status = OrderStatus.REJECTED
                    self._orders[order_id] = order
                    self._save_state()
                    return order_id

                self._cash[cash_key] -= cost
                if pos := self._positions.get(order.symbol):
                    old_qty = pos["quantity"]
                    new_qty = old_qty + order.quantity
                    pos["avg_price"] = (
                        (pos["avg_price"] * old_qty) + (fill_price * order.quantity)
                    ) / new_qty
                    pos["quantity"] = new_qty
                else:
                    self._positions[order.symbol] = {
                        "quantity": order.quantity,
                        "avg_price": fill_price,
                        "market": order.market,
                    }

            elif order.side == OrderSide.SELL:
                pos = self._positions.get(order.symbol)
                if not pos or pos["quantity"] < order.quantity:
                    logger.warning("[YF PAPER] Insufficient position for %s", order.symbol)
                    order.status = OrderStatus.REJECTED
                    self._orders[order_id] = order
                    self._save_state()
                    return order_id

                proceeds = fill_price * order.quantity
                self._cash[cash_key] += proceeds
                pnl = (fill_price - pos["avg_price"]) * order.quantity
                pos["quantity"] -= order.quantity
                if pos["quantity"] <= 0:
                    del self._positions[order.symbol]

            order.status = OrderStatus.FILLED
            order.filled_quantity = order.quantity
            order.filled_avg_price = fill_price
            self._orders[order_id] = order

            current_rsi = None
            try:
                from src.web.app import get_dashboard_state

                current_rsi = get_dashboard_state().rsi_values.get(order.symbol)
            except Exception:
                pass

            self._fills.append(
                Fill(
                    order_id=order_id,
                    symbol=order.symbol,
                    market=order.market,
                    side=order.side,
                    quantity=order.quantity,
                    price=fill_price,
                    commission=Decimal("0"),
                    timestamp=datetime.now(),
                    pnl=pnl,
                    rsi=current_rsi,
                )
            )
            self._save_state()
            await self.event_bus.publish(
                Event(
                    event_type=EventType.ORDER_FILLED,
                    data=order,
                    timestamp=datetime.now(),
                    source="YFinanceBroker",
                )
            )
            return order_id

    async def get_positions(
        self,
        market: Market = Market.KRX,
    ) -> list[Position]:
        positions = []
        for symbol, pos in self._positions.items():
            if pos["market"] != market or pos["quantity"] <= 0:
                continue
            try:
                current_price = await self.get_current_price(symbol, market)
            except Exception:
                current_price = pos["avg_price"]
            positions.append(
                Position(
                    symbol=symbol,
                    market=market,
                    quantity=pos["quantity"],
                    avg_entry_price=pos["avg_price"],
                    current_price=current_price,
                    unrealized_pnl=(current_price - pos["avg_price"]) * pos["quantity"],
                )
            )
        return positions

    async def get_account_balance(
        self,
        market: Market = Market.KRX,
    ) -> dict:
        cash_key = self._cash_key(market)
        relevant_markets = USD_MARKETS if cash_key == "usd" else {Market.KRX}
        cash = self._cash[cash_key]
        position_value = Decimal("0")
        cost_basis = Decimal("0")

        for symbol, pos in self._positions.items():
            if pos["market"] not in relevant_markets:
                continue
            try:
                price = await self.get_current_price(symbol, pos["market"])
            except Exception:
                price = pos["avg_price"]
            position_value += price * pos["quantity"]
            cost_basis += pos["avg_price"] * pos["quantity"]

        profit_loss = position_value - cost_basis
        total_eval = cash + position_value
        return {
            "total_eval": total_eval,
            "cash": cash,
            "stocks_eval": position_value,
            "profit_loss": profit_loss,
            "total_usd": total_eval if cash_key == "usd" else Decimal("0"),
            "total_krw": total_eval if cash_key == "krw" else Decimal("0"),
        }

    async def cancel_order(
        self,
        order_id: str,
        market: Market = Market.KRX,
    ) -> bool:
        async with self._state_lock:
            order = self._orders.get(order_id)
            if not order or order.status == OrderStatus.FILLED:
                return False
            order.status = OrderStatus.CANCELLED
            self._save_state()
            return True

    def _cash_key(self, market: Market) -> str:
        return "krw" if market == Market.KRX else "usd"

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cash": {key: str(value) for key, value in self._cash.items()},
            "positions": {
                symbol: {
                    "quantity": pos["quantity"],
                    "avg_price": str(pos["avg_price"]),
                    "market": pos["market"].value,
                }
                for symbol, pos in self._positions.items()
            },
            "orders": {
                order_id: self._order_to_dict(order) for order_id, order in self._orders.items()
            },
            "fills": [self._fill_to_dict(fill) for fill in self._fills],
        }
        tmp_path = self.state_path.with_suffix(f"{self.state_path.suffix}.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        os.replace(tmp_path, self.state_path)

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text())
            self._cash = {
                "krw": Decimal(str(data.get("cash", {}).get("krw", self._initial_cash["krw"]))),
                "usd": Decimal(str(data.get("cash", {}).get("usd", self._initial_cash["usd"]))),
            }
            self._positions = {
                symbol: {
                    "quantity": int(pos["quantity"]),
                    "avg_price": Decimal(str(pos["avg_price"])),
                    "market": Market.from_string(pos["market"]),
                }
                for symbol, pos in data.get("positions", {}).items()
            }
            self._orders = {
                order_id: self._order_from_dict(order_data)
                for order_id, order_data in data.get("orders", {}).items()
            }
            self._fills = [self._fill_from_dict(fill_data) for fill_data in data.get("fills", [])]
        except (json.JSONDecodeError, OSError, KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "Failed to load yfinance paper state from %s; starting fresh: %s",
                self.state_path,
                exc,
            )
            self._reset_in_memory_state()

    def _order_to_dict(self, order: Order) -> dict:
        return {
            "symbol": order.symbol,
            "market": order.market.value,
            "side": order.side.value,
            "order_type": order.order_type.value,
            "quantity": order.quantity,
            "price": str(order.price) if order.price is not None else None,
            "order_id": order.order_id,
            "status": order.status.value,
            "created_at": order.created_at.isoformat(),
            "filled_quantity": order.filled_quantity,
            "filled_avg_price": (
                str(order.filled_avg_price) if order.filled_avg_price is not None else None
            ),
        }

    def _order_from_dict(self, data: dict) -> Order:
        from src.core.types import OrderType

        return Order(
            symbol=data["symbol"],
            market=Market.from_string(data["market"]),
            side=OrderSide(data["side"]),
            order_type=OrderType(data["order_type"]),
            quantity=int(data["quantity"]),
            price=(Decimal(str(data["price"])) if data.get("price") is not None else None),
            order_id=data.get("order_id"),
            status=OrderStatus(data.get("status", OrderStatus.PENDING.value)),
            created_at=datetime.fromisoformat(data["created_at"]),
            filled_quantity=int(data.get("filled_quantity", 0)),
            filled_avg_price=(
                Decimal(str(data["filled_avg_price"]))
                if data.get("filled_avg_price") is not None
                else None
            ),
        )

    def _fill_to_dict(self, fill: Fill) -> dict:
        return {
            "order_id": fill.order_id,
            "symbol": fill.symbol,
            "market": fill.market.value,
            "side": fill.side.value,
            "quantity": fill.quantity,
            "price": str(fill.price),
            "commission": str(fill.commission),
            "timestamp": fill.timestamp.isoformat(),
            "pnl": str(fill.pnl) if fill.pnl is not None else None,
            "rsi": fill.rsi,
        }

    def _fill_from_dict(self, data: dict) -> Fill:
        return Fill(
            order_id=data["order_id"],
            symbol=data["symbol"],
            market=Market.from_string(data["market"]),
            side=OrderSide(data["side"]),
            quantity=int(data["quantity"]),
            price=Decimal(str(data["price"])),
            commission=Decimal(str(data.get("commission", "0"))),
            timestamp=datetime.fromisoformat(data["timestamp"]),
            pnl=(Decimal(str(data["pnl"])) if data.get("pnl") is not None else None),
            rsi=data.get("rsi"),
        )
