"""Tests for KISBroker connect-time task gating"""

import asyncio
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from src.broker.kis.client import (
    KISBroker, _canon_odno, _marketable_limit_price,
)
from src.broker.kis.mapper import KISMapper
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


class TestOdnoMatching:
    """ODNO is sometimes zero-padded by KIS — matching must tolerate it."""

    def test_canon_strips_leading_zeros(self):
        assert _canon_odno("0000123456") == "123456"
        assert _canon_odno("123456") == "123456"
        assert _canon_odno("") == "0"
        assert _canon_odno(None) == "0"

    def test_find_order_by_odno_tolerates_padding(self):
        b = _make_broker(hts_id="")
        o = _open_order("0000123456")
        b._orders["0000123456"] = o
        # padded stored, unpadded query (the poller/reconcile bug class)
        assert b._find_order_by_odno("123456") is o
        assert b._find_order_by_odno("0000123456") is o
        assert b._find_order_by_odno("999") is None


class TestMarketableLimit:
    """B1.2 — orders priced through the market by the slippage buffer."""

    def test_buy_pays_up_sell_gives_up(self):
        assert _marketable_limit_price(
            Decimal("100"), OrderSide.BUY, 0.01
        ) == Decimal("101.00")
        assert _marketable_limit_price(
            Decimal("100"), OrderSide.SELL, 0.01
        ) == Decimal("99.00")


class TestMapOverseasPositionFallback:
    """Regression: portfolio showed QTY but $0.00 avg/current price.

    KIS overseas inquire-balance (TTTS3012R) output1 carries
    `pchs_avg_pric` / `now_pric2`, NOT `avg_unpr3` / `ovrs_now_pric1`.
    The old `data.get(k, "0") or data.get(alt, "0")` chain short-
    circuited on the truthy string "0", so the real price fields were
    never read and value/invested collapsed to 0.
    """

    def test_real_inquire_balance_row_keeps_prices(self):
        row = {
            "ovrs_pdno": "BAC",
            "ovrs_item_name": "BANK OF AMERICA",
            "ovrs_cblc_qty": "3",
            "ord_psbl_qty": "3",
            "pchs_avg_pric": "27.5500",
            "now_pric2": "28.1000",
            "frcr_evlu_pfls_amt": "1.65",
            "ovrs_excg_cd": "NYSE",
        }
        pos = KISMapper.map_overseas_position(row, Market.NYSE)

        assert pos.symbol == "BAC"
        assert pos.market == Market.NYSE
        assert pos.quantity == 3
        assert pos.avg_entry_price == Decimal("27.5500")
        assert pos.current_price == Decimal("28.1000")
        assert pos.unrealized_pnl == Decimal("1.65")

    def test_primary_price_keys_still_win_when_present(self):
        row = {
            "ovrs_pdno": "AAPL",
            "ovrs_cblc_qty": "10",
            "avg_unpr3": "190.00",
            "pchs_avg_pric": "0",
            "ovrs_now_pric1": "205.50",
            "now_pric2": "0",
        }
        pos = KISMapper.map_overseas_position(row, Market.NASDAQ)

        assert pos.avg_entry_price == Decimal("190.00")
        assert pos.current_price == Decimal("205.50")

    def test_qty_falls_through_to_filled_when_balance_zero(self):
        # Just filled, not yet settled into ovrs_cblc_qty.
        row = {
            "ovrs_pdno": "MSFT",
            "ovrs_cblc_qty": "0",
            "ccld_qty": "5",
            "pchs_avg_pric": "410.00",
            "now_pric2": "412.30",
        }
        pos = KISMapper.map_overseas_position(row, Market.NASDAQ)

        assert pos.quantity == 5
        assert pos.avg_entry_price == Decimal("410.00")


class _FakeResp:
    """Minimal async-context-manager stand-in for aiohttp's response."""

    def __init__(self, payload: dict):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload


class TestGetOverseasPositionsFilter:
    """Same truthy-"0" bug class as the mapper, in the include guard:
    a just-filled (unsettled) position must not be silently dropped."""

    @pytest.mark.asyncio
    async def test_just_filled_position_not_dropped(self):
        broker = _make_broker(hts_id="")
        broker._auth.get_headers = MagicMock(return_value={})
        payload = {
            "rt_cd": "0",
            "output1": [
                # balance not yet settled, but 3 shares filled
                {
                    "ovrs_pdno": "BAC", "ovrs_cblc_qty": "0",
                    "ccld_qty": "3", "pchs_avg_pric": "27.55",
                    "now_pric2": "28.10",
                },
                # genuinely empty row — must be excluded
                {
                    "ovrs_pdno": "ZZZ", "ovrs_cblc_qty": "0",
                    "ccld_qty": "0", "ord_qty": "0",
                },
            ],
        }
        broker._session = MagicMock()
        broker._session.get = MagicMock(return_value=_FakeResp(payload))

        out = await broker._get_overseas_positions(Market.NYSE)

        assert [p.symbol for p in out] == ["BAC"]
        assert out[0].quantity == 3
        assert out[0].avg_entry_price == Decimal("27.55")
        assert out[0].current_price == Decimal("28.10")
