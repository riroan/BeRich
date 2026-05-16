"""Tests for KISBroker connect-time task gating"""

import asyncio
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from src.broker.kis.client import KISBroker
from src.core.types import (
    Order, OrderSide, OrderType, OrderStatus, Market,
)

CLIENT_SESSION = "src.broker.kis.client.aiohttp.ClientSession"


def _make_broker(hts_id: str) -> KISBroker:
    broker = KISBroker(
        event_bus=AsyncMock(),
        app_key="k",
        app_secret="s",
        account_no="12345678-01",
        paper_trading=True,
        hts_id=hts_id,
    )
    broker._auth.authenticate = AsyncMock()
    return broker


async def _idle() -> None:
    await asyncio.sleep(3600)


def _open_order(order_id: str, market: Market = Market.NASDAQ) -> Order:
    return Order(
        symbol="AAPL",
        market=market,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=5,
        price=Decimal("100"),
        order_id=order_id,
        status=OrderStatus.SUBMITTED,
    )


class TestConnectTaskGating:
    """Poller runs regardless of HTS_ID; WS listener stays gated on it."""

    @pytest.mark.asyncio
    async def test_poller_runs_without_hts_id(self):
        broker = _make_broker(hts_id="")

        with patch.object(broker, "_open_orders_poller", _idle), \
             patch.object(broker, "_execution_listener", _idle), \
             patch(CLIENT_SESSION, return_value=MagicMock()):
            await broker.connect()

        try:
            assert broker._exec_poll_task is not None
            assert not broker._exec_poll_task.done()
            # WS listener must NOT start without an HTS ID
            assert broker._exec_listener_task is None
        finally:
            broker._exec_poll_task.cancel()

    @pytest.mark.asyncio
    async def test_both_run_with_hts_id(self):
        broker = _make_broker(hts_id="ABC12345")

        with patch.object(broker, "_open_orders_poller", _idle), \
             patch.object(broker, "_execution_listener", _idle), \
             patch(CLIENT_SESSION, return_value=MagicMock()):
            await broker.connect()

        try:
            assert broker._exec_poll_task is not None
            assert not broker._exec_poll_task.done()
            assert broker._exec_listener_task is not None
            assert not broker._exec_listener_task.done()
        finally:
            broker._exec_poll_task.cancel()
            broker._exec_listener_task.cancel()


class TestReconcileOpenOrders:
    """Startup reconciliation: persist historical fills, don't replay them."""

    @pytest.mark.asyncio
    async def test_historical_fill_returned_not_emitted(self):
        broker = _make_broker(hts_id="")
        broker._query_overseas_fill = AsyncMock(return_value=True)
        order = _open_order("O1")

        changed = await broker.reconcile_open_orders([order])

        assert changed == [order]
        # Not added to the live poller set, and no ORDER_FILLED emitted
        # (sync_positions restores the position — replay would double-count).
        assert "O1" not in broker._orders
        broker.event_bus.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_still_open_registered_for_live_poller(self):
        broker = _make_broker(hts_id="")
        broker._query_overseas_fill = AsyncMock(return_value=False)
        order = _open_order("O2")

        changed = await broker.reconcile_open_orders([order])

        assert changed == []
        assert broker._orders["O2"] is order

    @pytest.mark.asyncio
    async def test_krx_skipped(self):
        broker = _make_broker(hts_id="")
        broker._query_overseas_fill = AsyncMock(return_value=True)
        order = _open_order("O3", market=Market.KRX)

        changed = await broker.reconcile_open_orders([order])

        assert changed == []
        assert "O3" not in broker._orders
        broker._query_overseas_fill.assert_not_awaited()
