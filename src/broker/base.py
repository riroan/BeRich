"""Broker protocol shared by trading components."""

from decimal import Decimal
from typing import Protocol

from src.core.types import Bar, Market, Order, Position


class Broker(Protocol):
    """Async broker interface used by TradingBot, StrategyEngine, and OrderManager."""

    paper_trading: bool

    @property
    def is_connected(self) -> bool:
        """Whether the broker is connected."""
        ...

    @property
    def account_no(self) -> str:
        """Account identifier for logs/dashboard."""
        ...

    async def connect(self) -> None:
        ...

    async def disconnect(self) -> None:
        ...

    async def get_current_price(
        self,
        symbol: str,
        market: Market = Market.KRX,
    ) -> Decimal:
        ...

    async def get_historical_bars(
        self,
        symbol: str,
        market: Market = Market.KRX,
        days: int = 100,
    ) -> list[Bar]:
        ...

    async def get_positions(
        self,
        market: Market,
    ) -> list[Position]:
        ...

    async def get_account_balance(
        self,
        market: Market,
    ) -> dict:
        ...

    async def submit_order(self, order: Order) -> str:
        ...

    async def cancel_order(
        self,
        order_id: str,
        market: Market,
    ) -> bool:
        ...
