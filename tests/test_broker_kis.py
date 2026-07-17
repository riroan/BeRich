"""Tests for KISBroker connect-time task gating"""

import asyncio
import pytest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from src.broker.kis.client import (
    KISBroker, _canon_odno, _marketable_limit_price, _overseas_quote_excd,
)
from src.broker.kis.mapper import KISMapper
from src.core.events import EventType
from src.core.exceptions import BrokerError, OrderError
from src.core.types import (
    Order, OrderSide, OrderType, OrderStatus, Market,
)
from src.utils.scheduler import Session

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

    def test_real_exchange_code_overrides_requested_market(self):
        row = {
            "ovrs_pdno": "IAU",
            "ovrs_cblc_qty": "2",
            "pchs_avg_pric": "82.9049",
            "now_pric2": "84.10",
            "ovrs_excg_cd": "AMEX",
        }

        pos = KISMapper.map_overseas_position(row, Market.NASDAQ)

        assert pos.market == Market.AMEX

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

    @pytest.mark.asyncio
    async def test_filters_positions_to_actual_exchange(self):
        """KIS can return all overseas holdings for an exchange request.

        Rows must use their own exchange code and the caller's market should
        only receive matching rows, otherwise current_positions duplicates
        every holding under NASDAQ/NYSE/AMEX.
        """
        broker = _make_broker(hts_id="")
        broker._auth.get_headers = MagicMock(return_value={})
        payload = {
            "rt_cd": "0",
            "output1": [
                {
                    "ovrs_pdno": "GOOG", "ovrs_cblc_qty": "1",
                    "pchs_avg_pric": "355.53", "now_pric2": "360.00",
                    "ovrs_excg_cd": "NASD",
                },
                {
                    "ovrs_pdno": "CVX", "ovrs_cblc_qty": "3",
                    "pchs_avg_pric": "175.64", "now_pric2": "177.00",
                    "ovrs_excg_cd": "NYSE",
                },
                {
                    "ovrs_pdno": "IAU", "ovrs_cblc_qty": "2",
                    "pchs_avg_pric": "82.9049", "now_pric2": "84.10",
                    "ovrs_excg_cd": "AMEX",
                },
            ],
        }
        broker._session = MagicMock()
        broker._session.get = MagicMock(return_value=_FakeResp(payload))

        out = await broker._get_overseas_positions(Market.NASDAQ)

        assert [p.symbol for p in out] == ["GOOG"]
        assert out[0].market == Market.NASDAQ


class TestGetOverseasBalanceUsesSummary:
    """Regression: dashboard Invested/P&L stuck at $0 every tick.

    KIS present-balance (CTRP6504R) output1 does NOT carry
    evlu_amt / evlu_pfls_amt, so summing output1 gave stock_eval=0
    and profit_loss=0 forever. The authoritative USD totals live in
    output3 (evlu_amt_smtl / evlu_pfls_amt_smtl).
    """

    @pytest.mark.asyncio
    async def test_stock_eval_and_pnl_come_from_output3(self):
        broker = _make_broker(hts_id="")
        broker._auth.get_headers = MagicMock(return_value={})
        # Payload mirrors a real logged response: output1 rows lack
        # evlu_amt; the USD totals are only in output3.
        payload = {
            "rt_cd": "0",
            "msg1": "조회되었습니다",
            "output1": [
                {"pdno": "BAC", "ovrs_cblc_qty": "3"},
                {"pdno": "IAU", "ovrs_cblc_qty": "1"},
                {"pdno": "XBI", "ovrs_cblc_qty": "2"},
            ],
            "output2": [
                {"crcy_cd": "USD", "frcr_dncl_amt_2": "4352.390000"},
            ],
            "output3": {
                "pchs_amt_smtl": "485",
                "evlu_amt_smtl": "491",
                "evlu_pfls_amt_smtl": "6",
            },
        }
        broker._session = MagicMock()
        broker._session.get = MagicMock(return_value=_FakeResp(payload))

        bal = await broker._get_overseas_balance()

        assert bal["cash"] == Decimal("4352.390000")
        assert bal["stocks_eval"] == Decimal("491")
        assert bal["profit_loss"] == Decimal("6")
        assert bal["total_eval"] == Decimal("4843.390000")
        # Dashboard derives INVESTED = total_eval - cash
        assert bal["total_eval"] - bal["cash"] == Decimal("491")

    @pytest.mark.asyncio
    async def test_output3_as_single_element_list(self):
        # KIS sometimes wraps output3 in a list.
        broker = _make_broker(hts_id="")
        broker._auth.get_headers = MagicMock(return_value={})
        payload = {
            "rt_cd": "0",
            "output1": [],
            "output2": [{"crcy_cd": "USD", "frcr_dncl_amt_2": "1000"}],
            "output3": [{"evlu_amt_smtl": "250", "evlu_pfls_amt_smtl": "-12"}],
        }
        broker._session = MagicMock()
        broker._session.get = MagicMock(return_value=_FakeResp(payload))

        bal = await broker._get_overseas_balance()

        assert bal["stocks_eval"] == Decimal("250")
        assert bal["profit_loss"] == Decimal("-12")
        assert bal["total_eval"] == Decimal("1250")


class TestQueryOverseasFillPriceFallback:
    """Audit MEDIUM: ft_ccld_unpr3 or avg_prvs or "0" stopped on the
    truthy string "0", so a 0-priced primary field hid avg_prvs."""

    @pytest.mark.asyncio
    async def test_zero_primary_falls_through_to_avg_prvs(self):
        broker = _make_broker(hts_id="")
        broker._auth.get_headers = MagicMock(return_value={})
        order = _open_order("123")  # quantity=5, filled_quantity=0
        payload = {
            "rt_cd": "0",
            "output": [{
                "odno": "123", "ft_ccld_qty": "5", "nccs_qty": "0",
                "ft_ccld_unpr3": "0", "avg_prvs": "27.55",
            }],
        }
        broker._session = MagicMock()
        broker._session.get = MagicMock(return_value=_FakeResp(payload))

        advanced = await broker._query_overseas_fill(order)

        assert advanced is True
        assert order.filled_quantity == 5
        assert order.filled_avg_price == Decimal("27.55")
        assert order.status == OrderStatus.FILLED

    @pytest.mark.asyncio
    async def test_primary_price_used_when_present(self):
        broker = _make_broker(hts_id="")
        broker._auth.get_headers = MagicMock(return_value={})
        order = _open_order("123")
        payload = {
            "rt_cd": "0",
            "output": [{
                "odno": "123", "ft_ccld_qty": "5", "nccs_qty": "0",
                "ft_ccld_unpr3": "30.10", "avg_prvs": "27.55",
            }],
        }
        broker._session = MagicMock()
        broker._session.get = MagicMock(return_value=_FakeResp(payload))

        await broker._query_overseas_fill(order)

        assert order.filled_avg_price == Decimal("30.10")


class TestOverseasBalanceFallbackFailsSafe:
    """Audit HIGH: fallback used to return cash=0/total=0, overwriting a
    good balance and tripping 'USD unavailable -> reject all orders'.
    It must raise so the caller keeps the last known-good balance."""

    @pytest.mark.asyncio
    async def test_fallback_raises(self):
        broker = _make_broker(hts_id="")
        with pytest.raises(BrokerError):
            await broker._get_overseas_balance_fallback()

    @pytest.mark.asyncio
    async def test_present_balance_failure_propagates_not_zeroed(self):
        broker = _make_broker(hts_id="")
        broker._auth.get_headers = MagicMock(return_value={})
        payload = {"rt_cd": "1", "msg1": "API down"}
        broker._session = MagicMock()
        broker._session.get = MagicMock(return_value=_FakeResp(payload))

        # Must raise (caller keeps last balance), NOT return zeros.
        with pytest.raises(BrokerError):
            await broker._get_overseas_balance()


class TestSessionOrderRouting:
    """US 24h: submit_order routes by current session (Phase 3)."""

    @pytest.mark.asyncio
    async def test_daytime_session_routes_to_day_order(self):
        broker = _make_broker(hts_id="")
        broker._auth.ensure_authenticated = AsyncMock()
        broker._submit_overseas_day_order = AsyncMock(return_value="DAY1")
        broker._submit_overseas_order = AsyncMock(return_value="REG1")
        order = _open_order("x", Market.NASDAQ)

        with patch(
            "src.broker.kis.client.get_current_session",
            return_value=Session.DAY_MARKET,
        ):
            oid = await broker.submit_order(order)

        assert oid == "DAY1"
        broker._submit_overseas_day_order.assert_awaited_once()
        broker._submit_overseas_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_pre_after_regular_route_to_regular_endpoint(self):
        broker = _make_broker(hts_id="")
        broker._auth.ensure_authenticated = AsyncMock()
        broker._submit_overseas_order = AsyncMock(return_value="REG1")
        order = _open_order("x", Market.NASDAQ)

        for sess in (Session.PRE, Session.REGULAR, Session.AFTER):
            broker._submit_overseas_order.reset_mock()
            with patch(
                "src.broker.kis.client.get_current_session",
                return_value=sess,
            ):
                await broker.submit_order(order)
            broker._submit_overseas_order.assert_awaited_once_with(order)

    @pytest.mark.asyncio
    async def test_overseas_priceless_order_rejected(self):
        broker = _make_broker(hts_id="")
        order = Order(
            symbol="AAPL", market=Market.NASDAQ, side=OrderSide.BUY,
            order_type=OrderType.MARKET, quantity=5, price=None,
        )
        with pytest.raises(OrderError):
            await broker._submit_overseas_order(order)

    @pytest.mark.asyncio
    async def test_daytime_order_rejected_in_paper_mode(self):
        broker = _make_broker(hts_id="")  # paper_trading=True
        order = _open_order("x", Market.NASDAQ)
        with pytest.raises(OrderError):
            await broker._submit_overseas_day_order(order)


class _PriceResp:
    """Minimal async-context-manager stand-in for aiohttp's response."""

    def __init__(self, payload: dict):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload


class TestOverseasQuoteEXCD:
    """주간거래(DAY_MARKET)만 확장 'B' 코드, 그 외(정규/프리/애프터)는 정규 코드."""

    def test_day_market_uses_extended_codes(self):
        assert _overseas_quote_excd(Market.NASDAQ, Session.DAY_MARKET) == "BAQ"
        assert _overseas_quote_excd(Market.NYSE, Session.DAY_MARKET) == "BAY"
        assert _overseas_quote_excd(Market.AMEX, Session.DAY_MARKET) == "BAA"

    def test_regular_pre_after_use_regular_codes(self):
        # PRE/AFTER는 실제 미국 장외 거래 시간이라 정규 venue가 라이브.
        # B venue는 그 시간엔 닫혀 종가에 고정되므로 쓰면 안 됨.
        for sess in (Session.REGULAR, Session.PRE, Session.AFTER):
            assert _overseas_quote_excd(Market.NASDAQ, sess) == "NAS"
            assert _overseas_quote_excd(Market.NYSE, sess) == "NYS"
            assert _overseas_quote_excd(Market.AMEX, sess) == "AMS"

    def _capture_excd(self, broker, captured):
        broker._auth.get_headers = MagicMock(return_value={})

        def _get(url, headers=None, params=None):
            captured["params"] = params
            return _PriceResp({"rt_cd": "0", "output": {"last": "123.45"}})

        broker._session = MagicMock()
        broker._session.get = _get

    @pytest.mark.asyncio
    async def test_daymarket_quote_uses_extended_excd(self):
        broker = _make_broker(hts_id="")
        captured = {}
        self._capture_excd(broker, captured)
        with patch(
            "src.broker.kis.client.get_current_session",
            return_value=Session.DAY_MARKET,
        ):
            price = await broker._get_overseas_price("AAPL", Market.NASDAQ)
        assert price == Decimal("123.45")
        assert captured["params"]["EXCD"] == "BAQ"

    @pytest.mark.asyncio
    async def test_regular_quote_uses_regular_excd(self):
        broker = _make_broker(hts_id="")
        captured = {}
        self._capture_excd(broker, captured)
        with patch(
            "src.broker.kis.client.get_current_session",
            return_value=Session.REGULAR,
        ):
            await broker._get_overseas_price("AAPL", Market.NASDAQ)
        assert captured["params"]["EXCD"] == "NAS"


class TestPollDateWindow:
    """Regression: the poll window must start at the order's creation
    date. With a today-only window, an order left open across a date
    rollover was invisible to inquire-ccnl forever and stayed SUBMITTED
    (production: XLI order stuck for 10 days)."""

    def _capture_params(self, broker, captured):
        def fake_get(url, headers=None, params=None):
            captured["params"] = params
            return _FakeResp({"rt_cd": "0", "output": []})
        broker._session = MagicMock()
        broker._session.get = MagicMock(side_effect=fake_get)
        broker._auth.get_headers = MagicMock(return_value={})

    @pytest.mark.asyncio
    async def test_window_starts_at_order_creation_date(self):
        broker = _make_broker(hts_id="")
        captured = {}
        self._capture_params(broker, captured)
        order = _open_order("777")
        order.created_at = datetime.now() - timedelta(days=10)

        await broker._query_overseas_fill(order)

        assert captured["params"]["ORD_STRT_DT"] == (
            order.created_at.strftime("%Y%m%d")
        )
        assert captured["params"]["ORD_END_DT"] == (
            datetime.now().strftime("%Y%m%d")
        )

    @pytest.mark.asyncio
    async def test_window_is_today_for_fresh_order(self):
        broker = _make_broker(hts_id="")
        captured = {}
        self._capture_params(broker, captured)
        order = _open_order("778")  # created_at defaults to now

        await broker._query_overseas_fill(order)

        today = datetime.now().strftime("%Y%m%d")
        assert captured["params"]["ORD_STRT_DT"] == today
        assert captured["params"]["ORD_END_DT"] == today


class TestExpiredOrderTerminalPath:
    """Regression: an unfilled order older than 24h is dead at KIS (day
    orders never survive a day), but nothing moved it to a terminal
    state — it stayed SUBMITTED forever, holding the in-flight guard so
    every new same-side signal for the symbol was suppressed (XLE
    sell lockup)."""

    @pytest.mark.asyncio
    async def test_live_poll_cancels_expired_order(self):
        broker = _make_broker(hts_id="")
        broker._query_overseas_fill = AsyncMock(return_value=False)
        order = _open_order("E1")
        order.created_at = datetime.now() - timedelta(hours=25)

        await broker._poll_overseas_order(order)

        assert order.status == OrderStatus.CANCELLED
        broker.event_bus.publish.assert_awaited_once()
        event = broker.event_bus.publish.await_args.args[0]
        assert event.event_type == EventType.ORDER_CANCELLED

    @pytest.mark.asyncio
    async def test_live_poll_keeps_fresh_order_open(self):
        broker = _make_broker(hts_id="")
        broker._query_overseas_fill = AsyncMock(return_value=False)
        order = _open_order("E2")  # created_at = now → could still be live

        await broker._poll_overseas_order(order)

        assert order.status == OrderStatus.SUBMITTED
        broker.event_bus.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reconcile_marks_expired_order_terminal(self):
        broker = _make_broker(hts_id="")
        broker._query_overseas_fill = AsyncMock(return_value=False)
        order = _open_order("E3")
        order.created_at = datetime.now() - timedelta(days=10)

        changed = await broker.reconcile_open_orders([order])

        assert changed == [order]
        assert order.status == OrderStatus.CANCELLED
        # Not re-registered for the live poller, and no event replayed
        # (startup reconciliation persists via the caller, silently).
        assert "E3" not in broker._orders
        broker.event_bus.publish.assert_not_awaited()
