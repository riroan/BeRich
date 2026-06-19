"""Tests for dashboard equity synchronization helpers."""

from datetime import datetime
from decimal import Decimal

from src.bot.dashboard_sync import calculate_us_settlement_adjustment
from src.core.types import Fill, Market, OrderSide


def _fill(
    *,
    side: OrderSide,
    market: Market = Market.NASDAQ,
    quantity: int = 1,
    price: str = "100",
    commission: str = "0",
    timestamp: datetime = datetime(2026, 6, 19, 4, 47),
) -> Fill:
    return Fill(
        order_id=f"{side.value}-1",
        symbol="AAPL",
        market=market,
        side=side,
        quantity=quantity,
        price=Decimal(price),
        commission=Decimal(commission),
        timestamp=timestamp,
    )


def test_us_settlement_adjustment_applies_pending_cash_movements():
    fills = [
        _fill(side=OrderSide.SELL, quantity=2, price="50", commission="1"),
        _fill(side=OrderSide.BUY, quantity=1, price="40", commission="0.50"),
        _fill(side=OrderSide.SELL, market=Market.KRX, quantity=1, price="100"),
    ]

    adjustment = calculate_us_settlement_adjustment(
        fills=fills,
        now=datetime(2026, 6, 19, 12, 0),
        settlement_business_days=1,
    )

    assert adjustment == Decimal("58.50")


def test_us_settlement_adjustment_expires_after_settlement_date():
    adjustment = calculate_us_settlement_adjustment(
        fills=[
            _fill(side=OrderSide.SELL, quantity=2, price="50"),
            _fill(side=OrderSide.BUY, quantity=1, price="40"),
        ],
        now=datetime(2026, 6, 24, 12, 0),
        settlement_business_days=1,
    )

    assert adjustment == Decimal("0.00")
