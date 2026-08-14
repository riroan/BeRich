"""A partial take-profit sell must not be announced as a full sell.

take_profit_levels stages a take-profit ladder the same way sell_levels
does (portion of the position per stage, see rsi_mean_reversion.py:230-334),
but the Discord dispatch only recognized "staged_sell" in the reason string
as partial — take_profit fell through to the else branch with is_partial
always False, so a 30%-of-position take-profit fill got announced as
"전량 매도" (full sell).
"""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal

os.environ.setdefault("DASHBOARD_PASSWORD", "test")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from src.core.types import Market, Order, OrderSide, OrderType  # noqa: E402
from src.execution.order_manager import OrderManager  # noqa: E402


def _order_manager() -> OrderManager:
    # _send_trade_notification only touches self.notifier; the rest are
    # unused placeholders it never calls.
    return OrderManager(
        event_bus=None, broker=None, risk_manager=None, storage=None,
    )


def test_partial_take_profit_is_not_announced_as_full_sell():
    calls = []

    class _FakeNotifier:
        async def notify_sell_executed(self, **kwargs):
            calls.append(kwargs)
            return True

    om = _order_manager()
    om.notifier = _FakeNotifier()
    order = Order(
        symbol="AAPL", market=Market.NASDAQ, side=OrderSide.SELL,
        order_type=OrderType.MARKET, quantity=3,
    )

    asyncio.run(om._send_trade_notification(
        order,
        {
            "reason": "take_profit", "sell_portion": 0.3, "stage": 1,
            "total_stages": 3, "pnl": Decimal("30"), "pnl_pct": 20.0,
        },
        price=Decimal("100"), quantity=3, submitted=False,
    ))

    assert calls[0]["is_partial"] is True
