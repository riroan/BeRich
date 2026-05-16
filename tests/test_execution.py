"""Tests for OrderManager execution module"""

import pytest
from decimal import Decimal
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from src.core.types import (
    Signal,
    SignalType,
    Market,
    Order,
    OrderSide,
    OrderType,
)
from src.core.events import EventBus, Event, EventType
from src.execution.order_manager import OrderManager

PATCH_DASHBOARD = "src.execution.order_manager.get_dashboard_state"


@pytest.fixture
def event_bus():
    return MagicMock(spec=EventBus)


@pytest.fixture
def broker():
    mock = AsyncMock()
    mock.get_current_price = AsyncMock(
        return_value=Decimal("100"),
    )
    mock.get_positions = AsyncMock(return_value=[])
    mock.submit_order = AsyncMock(return_value="order-123")
    mock.cancel_order = AsyncMock(return_value=True)
    return mock


@pytest.fixture
def risk_manager():
    mock = MagicMock()
    mock.validate_order = MagicMock(return_value=(True, None))
    return mock


@pytest.fixture
def storage():
    mock = AsyncMock()
    mock.save_order = AsyncMock()
    return mock


@pytest.fixture
def order_manager(event_bus, broker, risk_manager, storage):
    return OrderManager(
        event_bus=event_bus,
        broker=broker,
        risk_manager=risk_manager,
        storage=storage,
    )


def _make_signal(
    signal_type=SignalType.ENTRY_LONG,
    symbol="AAPL",
    market=Market.NASDAQ,
    strength=0.5,
):
    return Signal(
        signal_type=signal_type,
        symbol=symbol,
        market=market,
        strength=strength,
    )


def _make_event(signal, strategy="test"):
    return Event(
        event_type=EventType.SIGNAL_GENERATED,
        data={"signal": signal, "strategy": strategy},
        timestamp=datetime.now(),
        source="test",
    )


class TestOrderManagerInit:
    """Test OrderManager initialization"""

    def test_init_stores_dependencies(
        self, event_bus, broker, risk_manager, storage,
    ):
        om = OrderManager(
            event_bus=event_bus,
            broker=broker,
            risk_manager=risk_manager,
            storage=storage,
        )
        assert om.event_bus is event_bus
        assert om.broker is broker
        assert om.risk_manager is risk_manager
        assert om.storage is storage

    def test_init_empty_order_dicts(self, order_manager):
        assert order_manager._pending_orders == {}
        assert order_manager._active_orders == {}

    @pytest.mark.asyncio
    async def test_init_default_trading_enabled(self, order_manager):
        assert await order_manager._is_trading_enabled() is True

    @pytest.mark.asyncio
    async def test_init_custom_trading_enabled(
        self, event_bus, broker, risk_manager, storage,
    ):
        om = OrderManager(
            event_bus=event_bus,
            broker=broker,
            risk_manager=risk_manager,
            storage=storage,
            is_trading_enabled=AsyncMock(return_value=False),
        )
        assert await om._is_trading_enabled() is False

    def test_init_notifier_none_by_default(self, order_manager):
        assert order_manager.notifier is None

    def test_init_with_notifier(
        self, event_bus, broker, risk_manager, storage,
    ):
        notifier = MagicMock()
        om = OrderManager(
            event_bus=event_bus,
            broker=broker,
            risk_manager=risk_manager,
            storage=storage,
            notifier=notifier,
        )
        assert om.notifier is notifier


class TestSignalToOrder:
    """Test _signal_to_order conversion"""

    @pytest.mark.asyncio
    async def test_entry_long_creates_buy(
        self, order_manager, broker,
    ):
        """ENTRY_LONG signal should produce a BUY order"""
        broker.get_current_price.return_value = Decimal("50")
        signal = _make_signal(
            signal_type=SignalType.ENTRY_LONG,
            strength=0.5,
        )

        with patch.object(
            order_manager,
            "_calculate_buy_quantity",
            new_callable=AsyncMock,
            return_value=10,
        ):
            order = await order_manager._signal_to_order(signal)

        assert order is not None
        assert order.side == OrderSide.BUY
        assert order.symbol == "AAPL"
        assert order.market == Market.NASDAQ
        assert order.order_type == OrderType.MARKET
        assert order.quantity == 10
        assert order.price == Decimal("50")

    @pytest.mark.asyncio
    async def test_exit_long_creates_sell(
        self, order_manager, broker,
    ):
        """EXIT_LONG signal should produce a SELL order"""
        broker.get_current_price.return_value = Decimal("120")

        position = MagicMock()
        position.symbol = "AAPL"
        position.quantity = Decimal("100")
        broker.get_positions.return_value = [position]

        signal = _make_signal(
            signal_type=SignalType.EXIT_LONG,
            strength=1.0,
        )
        order = await order_manager._signal_to_order(signal)

        assert order is not None
        assert order.side == OrderSide.SELL
        assert order.symbol == "AAPL"
        assert order.quantity == 100
        assert order.price == Decimal("120")

    @pytest.mark.asyncio
    async def test_exit_long_partial_sell(
        self, order_manager, broker,
    ):
        """EXIT_LONG with strength < 1.0 sells partial"""
        broker.get_current_price.return_value = Decimal("100")

        position = MagicMock()
        position.symbol = "AAPL"
        position.quantity = Decimal("100")
        broker.get_positions.return_value = [position]

        signal = _make_signal(
            signal_type=SignalType.EXIT_LONG,
            strength=0.3,
        )
        order = await order_manager._signal_to_order(signal)

        assert order is not None
        assert order.side == OrderSide.SELL
        assert order.quantity == 30  # 100 * 0.3

    @pytest.mark.asyncio
    async def test_exit_long_no_position_returns_none(
        self, order_manager, broker,
    ):
        """EXIT_LONG with no position should return None"""
        broker.get_current_price.return_value = Decimal("100")
        broker.get_positions.return_value = []

        signal = _make_signal(
            signal_type=SignalType.EXIT_LONG,
            strength=1.0,
        )
        order = await order_manager._signal_to_order(signal)
        assert order is None

    @pytest.mark.asyncio
    async def test_hold_signal_returns_none(self, order_manager):
        """HOLD signal should return None"""
        signal = _make_signal(
            signal_type=SignalType.HOLD,
            strength=0.0,
        )
        order = await order_manager._signal_to_order(signal)
        assert order is None

    @pytest.mark.asyncio
    async def test_entry_long_zero_qty_returns_none(
        self, order_manager, broker,
    ):
        """ENTRY_LONG with zero calculated quantity returns None"""
        broker.get_current_price.return_value = Decimal("50")
        signal = _make_signal(strength=0.5)

        with patch.object(
            order_manager,
            "_calculate_buy_quantity",
            new_callable=AsyncMock,
            return_value=0,
        ):
            order = await order_manager._signal_to_order(signal)

        assert order is None

    @pytest.mark.asyncio
    async def test_price_fetch_failure_returns_none(
        self, order_manager, broker,
    ):
        """If broker.get_current_price raises, return None"""
        broker.get_current_price.side_effect = Exception(
            "connection error",
        )
        signal = _make_signal()

        order = await order_manager._signal_to_order(signal)
        assert order is None


class TestTradingPauseCheck:
    """Test _on_signal respects dashboard.trading_paused"""

    @pytest.mark.asyncio
    async def test_signal_ignored_when_paused(
        self, order_manager,
    ):
        """Signals ignored when trading is paused"""
        dashboard_mock = MagicMock()
        dashboard_mock.trading_paused = True

        signal = _make_signal()
        event = _make_event(signal)

        with patch(PATCH_DASHBOARD, return_value=dashboard_mock):
            await order_manager._on_signal(event)

        order_manager.broker.get_current_price.assert_not_called()

    @pytest.mark.asyncio
    async def test_signal_processed_when_not_paused(
        self, order_manager,
    ):
        """Signals processed when trading is not paused"""
        dashboard_mock = MagicMock()
        dashboard_mock.trading_paused = False

        signal = _make_signal()
        event = _make_event(signal)

        with patch(
            PATCH_DASHBOARD, return_value=dashboard_mock,
        ), patch.object(
            order_manager,
            "_signal_to_order",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_sto:
            await order_manager._on_signal(event)
            mock_sto.assert_awaited_once_with(signal)


class TestWarmupCheck:
    """Test _on_signal respects is_trading_enabled"""

    @pytest.mark.asyncio
    async def test_signal_ignored_during_warmup(
        self, event_bus, broker, risk_manager, storage,
    ):
        """Signals ignored when is_trading_enabled is False"""
        om = OrderManager(
            event_bus=event_bus,
            broker=broker,
            risk_manager=risk_manager,
            storage=storage,
            is_trading_enabled=AsyncMock(return_value=False),
        )

        signal = _make_signal()
        event = _make_event(signal)

        await om._on_signal(event)

        broker.get_current_price.assert_not_called()

    @pytest.mark.asyncio
    async def test_signal_processed_after_warmup(
        self, event_bus, broker, risk_manager, storage,
    ):
        """Signals processed when is_trading_enabled is True"""
        om = OrderManager(
            event_bus=event_bus,
            broker=broker,
            risk_manager=risk_manager,
            storage=storage,
            is_trading_enabled=AsyncMock(return_value=True),
        )

        dashboard_mock = MagicMock()
        dashboard_mock.trading_paused = False

        signal = _make_signal()
        event = _make_event(signal)

        with patch(
            PATCH_DASHBOARD, return_value=dashboard_mock,
        ), patch.object(
            om,
            "_signal_to_order",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_sto:
            await om._on_signal(event)
            mock_sto.assert_awaited_once_with(signal)


class TestStart:
    """Test OrderManager.start subscribes to events"""

    @pytest.mark.asyncio
    async def test_start_subscribes_events(
        self, order_manager, event_bus,
    ):
        await order_manager.start()

        assert event_bus.subscribe.call_count == 5
        subscribed = [
            c.args[0]
            for c in event_bus.subscribe.call_args_list
        ]
        assert EventType.SIGNAL_GENERATED in subscribed
        assert EventType.ORDER_FILLED in subscribed
        assert EventType.ORDER_PARTIAL_FILLED in subscribed
        assert EventType.ORDER_CANCELLED in subscribed
        assert EventType.ORDER_REJECTED in subscribed


def _make_order(
    symbol="BAC",
    market=Market.NYSE,
    side=OrderSide.BUY,
    quantity=3,
    price=Decimal("49.50"),
    order_id=None,
):
    return Order(
        symbol=symbol,
        market=market,
        side=side,
        order_type=OrderType.MARKET,
        quantity=quantity,
        price=price,
        order_id=order_id,
    )


class TestTradeNotification:
    """Submit-time vs fill-time notification semantics"""

    @pytest.fixture
    def om_with_notifier(self, event_bus, broker, risk_manager, storage):
        notifier = AsyncMock()
        om = OrderManager(
            event_bus=event_bus,
            broker=broker,
            risk_manager=risk_manager,
            storage=storage,
            notifier=notifier,
        )
        return om, notifier

    @pytest.mark.asyncio
    async def test_submit_notifies_submitted_with_estimated_price(
        self, om_with_notifier,
    ):
        """_submit_order fires submitted=True with the order's est. price"""
        om, notifier = om_with_notifier
        order = _make_order(price=Decimal("49.50"), quantity=3)
        meta = {
            "reason": "avg_down_stage_1",
            "rsi": 33.5, "stage": 1, "total_stages": 3,
        }

        with patch(PATCH_DASHBOARD, return_value=MagicMock()):
            await om._submit_order(order, signal_metadata=meta)

        notifier.notify_buy_executed.assert_awaited_once()
        kw = notifier.notify_buy_executed.call_args.kwargs
        assert kw["submitted"] is True
        assert kw["price"] == Decimal("49.50")
        assert kw["quantity"] == 3
        # metadata retained for the eventual fill notification
        assert om._order_meta["order-123"]["rsi"] == 33.5

    @pytest.mark.asyncio
    async def test_fill_notifies_with_real_price_and_is_idempotent(
        self, om_with_notifier,
    ):
        """_on_fill fires submitted=False with the actual fill price/qty,
        once only even if ORDER_FILLED is delivered twice."""
        om, notifier = om_with_notifier
        order = _make_order(
            price=Decimal("49.50"), quantity=3, order_id="order-123",
        )
        order.filled_avg_price = Decimal("49.48")
        order.filled_quantity = 3
        om._order_meta["order-123"] = {
            "reason": "avg_down_stage_1",
            "rsi": 33.5, "stage": 1, "total_stages": 3,
        }
        event = Event(
            event_type=EventType.ORDER_FILLED,
            data=order,
            timestamp=datetime.now(),
            source="test",
        )

        await om._on_fill(event)
        await om._on_fill(event)  # duplicate delivery

        notifier.notify_buy_executed.assert_awaited_once()
        kw = notifier.notify_buy_executed.call_args.kwargs
        assert kw["submitted"] is False
        assert kw["price"] == Decimal("49.48")  # real fill, not 49.50
        assert kw["quantity"] == 3
        assert "order-123" not in om._order_meta

    @pytest.mark.asyncio
    async def test_fill_for_unknown_order_does_not_notify(
        self, om_with_notifier,
    ):
        """Orders not submitted by us (no meta) get no fill notification"""
        om, notifier = om_with_notifier
        order = _make_order(order_id="external-999")
        order.filled_avg_price = Decimal("10")
        order.filled_quantity = 1
        event = Event(
            event_type=EventType.ORDER_FILLED,
            data=order,
            timestamp=datetime.now(),
            source="test",
        )

        await om._on_fill(event)

        notifier.notify_buy_executed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stop_loss_submit_routes_to_stop_loss_notifier(
        self, om_with_notifier,
    ):
        """reason=stop_loss on a SELL routes to notify_stop_loss_executed"""
        om, notifier = om_with_notifier
        order = _make_order(
            side=OrderSide.SELL, quantity=10, price=Decimal("45"),
        )
        meta = {"reason": "stop_loss", "pnl": Decimal("-50"), "pnl_pct": -10}

        with patch(PATCH_DASHBOARD, return_value=MagicMock()):
            await om._submit_order(order, signal_metadata=meta)

        notifier.notify_stop_loss_executed.assert_awaited_once()
        assert (
            notifier.notify_stop_loss_executed.call_args.kwargs["submitted"]
            is True
        )
        notifier.notify_buy_executed.assert_not_awaited()


class TestFillAccounting:
    """B2.4/B2.5/B2.2 — fill persisted, PnL from fill price, idempotent."""

    @pytest.fixture
    def om(self, event_bus, broker, risk_manager, storage):
        return OrderManager(
            event_bus=event_bus, broker=broker,
            risk_manager=risk_manager, storage=storage,
            notifier=AsyncMock(),
        )

    @pytest.mark.asyncio
    async def test_sell_pnl_from_actual_fill_price(self, om):
        order = _make_order(side=OrderSide.SELL, quantity=10,
                            price=Decimal("100"), order_id="order-123")
        order.filled_avg_price = Decimal("104")
        order.filled_quantity = 10
        om._order_meta["order-123"] = {
            "reason": "staged_sell_1", "avg_price": 100.0, "rsi": 72.0,
        }
        ev = Event(event_type=EventType.ORDER_FILLED, data=order,
                   timestamp=datetime.now(), source="t")

        await om._on_fill(ev)

        # realized = (104 - 100) × 10 = 40, from the REAL fill price
        om.risk_manager.record_trade.assert_called_once_with(Decimal("40"))
        om.storage.save_fill.assert_awaited_once()
        saved = om.storage.save_fill.call_args.args[0]
        assert saved.price == Decimal("104")
        assert saved.quantity == 10
        assert saved.pnl == Decimal("40")

    @pytest.mark.asyncio
    async def test_duplicate_fill_no_double_pnl_or_fill(self, om):
        order = _make_order(side=OrderSide.SELL, quantity=5,
                            price=Decimal("50"), order_id="o1")
        order.filled_avg_price = Decimal("60")
        order.filled_quantity = 5
        om._order_meta["o1"] = {"reason": "staged_sell_1", "avg_price": 50.0}
        ev = Event(event_type=EventType.ORDER_FILLED, data=order,
                   timestamp=datetime.now(), source="t")

        await om._on_fill(ev)
        await om._on_fill(ev)  # redelivered

        assert om.risk_manager.record_trade.call_count == 1
        assert om.storage.save_fill.await_count == 1

    @pytest.mark.asyncio
    async def test_sell_pnl_falls_back_to_estimate(self, om):
        order = _make_order(side=OrderSide.SELL, quantity=4,
                            price=Decimal("10"), order_id="o2")
        order.filled_avg_price = Decimal("11")
        order.filled_quantity = 4
        order.realized_pnl = Decimal("7")  # signal-time estimate
        om._order_meta["o2"] = {"reason": "staged_sell_1"}  # no avg_price
        ev = Event(event_type=EventType.ORDER_FILLED, data=order,
                   timestamp=datetime.now(), source="t")

        await om._on_fill(ev)

        om.risk_manager.record_trade.assert_called_once_with(Decimal("7"))

    @pytest.mark.asyncio
    async def test_cancelled_order_frees_state(self, om):
        order = _make_order(order_id="c1")
        om._active_orders["c1"] = order
        om._order_meta["c1"] = {"reason": "x"}
        ev = Event(event_type=EventType.ORDER_CANCELLED, data=order,
                   timestamp=datetime.now(), source="t")

        await om._on_order_closed(ev)

        assert "c1" not in om._active_orders
        assert "c1" not in om._order_meta
        om.storage.save_order.assert_awaited()
