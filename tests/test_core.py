"""Tests for core module: types, events, and exceptions"""

import pytest
import asyncio
from decimal import Decimal
from datetime import datetime
from unittest.mock import MagicMock, AsyncMock

from src.core.types import (
    Market,
    OrderSide,
    OrderType,
    OrderStatus,
    SignalType,
    Order,
    Position,
    Signal,
    Quote,
    Bar,
    Fill,
)
from src.core.events import EventType, Event, EventBus
from src.core.exceptions import (
    TradingBotError,
    BrokerError,
    AuthenticationError,
    OrderError,
    RiskLimitError,
    ConfigurationError,
    DataError,
)


class TestEventType:
    """Test EventType enum"""

    def test_market_data_events(self):
        assert EventType.QUOTE_UPDATE is not None
        assert EventType.BAR_UPDATE is not None

    def test_order_events(self):
        assert EventType.ORDER_SUBMITTED is not None
        assert EventType.ORDER_FILLED is not None
        assert EventType.ORDER_PARTIAL_FILLED is not None
        assert EventType.ORDER_CANCELLED is not None
        assert EventType.ORDER_REJECTED is not None

    def test_position_events(self):
        assert EventType.POSITION_OPENED is not None
        assert EventType.POSITION_CLOSED is not None
        assert EventType.POSITION_UPDATED is not None

    def test_signal_events(self):
        assert EventType.SIGNAL_GENERATED is not None

    def test_system_events(self):
        assert EventType.BROKER_CONNECTED is not None
        assert EventType.BROKER_DISCONNECTED is not None
        assert EventType.RISK_LIMIT_BREACHED is not None
        assert EventType.ERROR is not None

    def test_all_values_unique(self):
        values = [e.value for e in EventType]
        assert len(values) == len(set(values))


class TestEvent:
    """Test Event dataclass"""

    def test_event_creation(self):
        now = datetime.now()
        event = Event(
            event_type=EventType.QUOTE_UPDATE,
            data={"symbol": "AAPL", "price": 150.0},
            timestamp=now,
            source="test",
        )
        assert event.event_type == EventType.QUOTE_UPDATE
        assert event.data["symbol"] == "AAPL"
        assert event.timestamp == now
        assert event.source == "test"

    def test_event_with_none_data(self):
        event = Event(
            event_type=EventType.ERROR,
            data=None,
            timestamp=datetime.now(),
            source="test",
        )
        assert event.data is None


class TestEventBus:
    """Test EventBus subscribe/publish/dispatch"""

    @pytest.fixture
    def event_bus(self):
        return EventBus()

    def test_subscribe(self, event_bus):
        handler = MagicMock()
        handler.__name__ = "mock_handler"
        event_bus.subscribe(EventType.QUOTE_UPDATE, handler)
        assert handler in event_bus._subscribers[EventType.QUOTE_UPDATE]

    def test_subscribe_multiple_handlers(self, event_bus):
        handler1 = MagicMock()
        handler1.__name__ = "handler1"
        handler2 = MagicMock()
        handler2.__name__ = "handler2"

        event_bus.subscribe(EventType.QUOTE_UPDATE, handler1)
        event_bus.subscribe(EventType.QUOTE_UPDATE, handler2)

        assert len(event_bus._subscribers[EventType.QUOTE_UPDATE]) == 2

    def test_unsubscribe(self, event_bus):
        handler = MagicMock()
        handler.__name__ = "mock_handler"
        event_bus.subscribe(EventType.QUOTE_UPDATE, handler)
        event_bus.unsubscribe(EventType.QUOTE_UPDATE, handler)

        assert handler not in event_bus._subscribers[EventType.QUOTE_UPDATE]

    def test_unsubscribe_nonexistent_type(self, event_bus):
        handler = MagicMock()
        # Should not raise when unsubscribing from a type with no subscribers
        event_bus.unsubscribe(EventType.QUOTE_UPDATE, handler)

    @pytest.mark.asyncio
    async def test_publish_puts_event_on_queue(self, event_bus):
        event = Event(
            event_type=EventType.QUOTE_UPDATE,
            data={"price": 100},
            timestamp=datetime.now(),
            source="test",
        )
        await event_bus.publish(event)
        assert not event_bus._queue.empty()

    @pytest.mark.asyncio
    async def test_dispatch_calls_sync_handler(self, event_bus):
        handler = MagicMock()
        handler.__name__ = "sync_handler"
        event_bus.subscribe(EventType.QUOTE_UPDATE, handler)

        event = Event(
            event_type=EventType.QUOTE_UPDATE,
            data={"price": 100},
            timestamp=datetime.now(),
            source="test",
        )
        await event_bus._dispatch(event)

        handler.assert_called_once_with(event)

    @pytest.mark.asyncio
    async def test_dispatch_calls_async_handler(self, event_bus):
        handler = AsyncMock()
        handler.__name__ = "async_handler"
        event_bus.subscribe(EventType.ORDER_FILLED, handler)

        event = Event(
            event_type=EventType.ORDER_FILLED,
            data={"order_id": "123"},
            timestamp=datetime.now(),
            source="test",
        )
        await event_bus._dispatch(event)

        handler.assert_called_once_with(event)

    @pytest.mark.asyncio
    async def test_dispatch_no_subscribers(self, event_bus):
        """Dispatch should succeed even with no subscribers"""
        event = Event(
            event_type=EventType.QUOTE_UPDATE,
            data={},
            timestamp=datetime.now(),
            source="test",
        )
        # Should not raise
        await event_bus._dispatch(event)

    @pytest.mark.asyncio
    async def test_dispatch_handler_error_creates_error_event(self, event_bus):
        """Handler errors should produce an ERROR event"""
        error_handler = AsyncMock()
        error_handler.__name__ = "error_handler"
        event_bus.subscribe(EventType.ERROR, error_handler)

        bad_handler = MagicMock(side_effect=ValueError("boom"))
        bad_handler.__name__ = "bad_handler"
        event_bus.subscribe(EventType.QUOTE_UPDATE, bad_handler)

        event = Event(
            event_type=EventType.QUOTE_UPDATE,
            data={},
            timestamp=datetime.now(),
            source="test",
        )
        await event_bus._dispatch(event)

        error_handler.assert_called_once()
        error_event = error_handler.call_args[0][0]
        assert error_event.event_type == EventType.ERROR
        assert "boom" in error_event.data["error"]

    @pytest.mark.asyncio
    async def test_dispatch_error_in_error_handler_no_recursion(self, event_bus):
        """Errors in ERROR handlers should not recurse"""
        bad_error_handler = MagicMock(side_effect=RuntimeError("nested boom"))
        bad_error_handler.__name__ = "bad_error_handler"
        event_bus.subscribe(EventType.ERROR, bad_error_handler)

        error_event = Event(
            event_type=EventType.ERROR,
            data={"error": "initial"},
            timestamp=datetime.now(),
            source="test",
        )
        # Should not raise or recurse infinitely
        await event_bus._dispatch(error_event)

    @pytest.mark.asyncio
    async def test_start_and_stop(self, event_bus):
        await event_bus.start()
        assert event_bus._running is True
        assert event_bus._task is not None

        await event_bus.stop()
        assert event_bus._running is False

    @pytest.mark.asyncio
    async def test_publish_and_process(self, event_bus):
        """Integration test: publish event and verify handler is called via the loop"""
        received = []

        async def handler(event):
            received.append(event)

        handler.__name__ = "handler"
        event_bus.subscribe(EventType.SIGNAL_GENERATED, handler)

        await event_bus.start()

        event = Event(
            event_type=EventType.SIGNAL_GENERATED,
            data={"signal": "buy"},
            timestamp=datetime.now(),
            source="test",
        )
        await event_bus.publish(event)

        # Give the loop time to process
        await asyncio.sleep(0.1)

        await event_bus.stop()

        assert len(received) == 1
        assert received[0].data["signal"] == "buy"


class TestExceptions:
    """Test custom exception types"""

    def test_trading_bot_error_is_exception(self):
        assert issubclass(TradingBotError, Exception)

    def test_broker_error_inherits_trading_bot_error(self):
        assert issubclass(BrokerError, TradingBotError)

    def test_authentication_error_inherits_broker_error(self):
        assert issubclass(AuthenticationError, BrokerError)
        assert issubclass(AuthenticationError, TradingBotError)

    def test_order_error_inherits_broker_error(self):
        assert issubclass(OrderError, BrokerError)

    def test_risk_limit_error_inherits_trading_bot_error(self):
        assert issubclass(RiskLimitError, TradingBotError)

    def test_configuration_error_inherits_trading_bot_error(self):
        assert issubclass(ConfigurationError, TradingBotError)

    def test_data_error_inherits_trading_bot_error(self):
        assert issubclass(DataError, TradingBotError)

    def test_raise_and_catch_broker_error(self):
        with pytest.raises(BrokerError, match="connection failed"):
            raise BrokerError("connection failed")

    def test_catch_broker_error_as_trading_bot_error(self):
        with pytest.raises(TradingBotError):
            raise BrokerError("connection failed")

    def test_exception_message(self):
        err = OrderError("invalid quantity")
        assert str(err) == "invalid quantity"


class TestOrder:
    """Test Order dataclass"""

    def test_order_creation_market(self):
        order = Order(
            symbol="AAPL",
            market=Market.NASDAQ,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=100,
        )
        assert order.symbol == "AAPL"
        assert order.market == Market.NASDAQ
        assert order.side == OrderSide.BUY
        assert order.order_type == OrderType.MARKET
        assert order.quantity == 100
        assert order.price is None
        assert order.order_id is None
        assert order.status == OrderStatus.PENDING
        assert order.filled_quantity == 0
        assert order.filled_avg_price is None

    def test_order_creation_limit(self):
        order = Order(
            symbol="MSFT",
            market=Market.NYSE,
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            quantity=50,
            price=Decimal("300.50"),
        )
        assert order.price == Decimal("300.50")
        assert order.order_type == OrderType.LIMIT

    def test_order_defaults_created_at(self):
        order = Order(
            symbol="AAPL",
            market=Market.NASDAQ,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=10,
        )
        assert isinstance(order.created_at, datetime)

    def test_order_with_all_fields(self):
        now = datetime.now()
        order = Order(
            symbol="TSLA",
            market=Market.NASDAQ,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=200,
            price=Decimal("250.00"),
            order_id="ORD-001",
            status=OrderStatus.FILLED,
            created_at=now,
            filled_quantity=200,
            filled_avg_price=Decimal("249.80"),
        )
        assert order.order_id == "ORD-001"
        assert order.status == OrderStatus.FILLED
        assert order.filled_quantity == 200
        assert order.filled_avg_price == Decimal("249.80")


class TestSignal:
    """Test Signal dataclass"""

    def test_signal_creation_minimal(self):
        signal = Signal(
            signal_type=SignalType.ENTRY_LONG,
            symbol="AAPL",
            market=Market.NASDAQ,
            strength=0.8,
        )
        assert signal.signal_type == SignalType.ENTRY_LONG
        assert signal.symbol == "AAPL"
        assert signal.strength == 0.8
        assert signal.target_price is None
        assert signal.stop_loss is None
        assert signal.metadata == {}
        assert isinstance(signal.timestamp, datetime)

    def test_signal_creation_full(self):
        signal = Signal(
            signal_type=SignalType.EXIT_LONG,
            symbol="MSFT",
            market=Market.NYSE,
            strength=0.5,
            target_price=Decimal("400.00"),
            stop_loss=Decimal("350.00"),
            metadata={"reason": "rsi_overbought"},
        )
        assert signal.target_price == Decimal("400.00")
        assert signal.stop_loss == Decimal("350.00")
        assert signal.metadata["reason"] == "rsi_overbought"

    def test_signal_hold(self):
        signal = Signal(
            signal_type=SignalType.HOLD,
            symbol="AAPL",
            market=Market.NASDAQ,
            strength=0.0,
        )
        assert signal.signal_type == SignalType.HOLD
        assert signal.strength == 0.0


class TestPosition:
    """Test Position dataclass"""

    def test_position_creation(self):
        pos = Position(
            symbol="AAPL",
            market=Market.NASDAQ,
            quantity=100,
            avg_entry_price=Decimal("150.00"),
            current_price=Decimal("160.00"),
            unrealized_pnl=Decimal("1000.00"),
        )
        assert pos.symbol == "AAPL"
        assert pos.quantity == 100
        assert pos.avg_entry_price == Decimal("150.00")
        assert pos.current_price == Decimal("160.00")
        assert pos.unrealized_pnl == Decimal("1000.00")
        assert pos.realized_pnl == Decimal("0")

    def test_position_with_realized_pnl(self):
        pos = Position(
            symbol="MSFT",
            market=Market.NYSE,
            quantity=50,
            avg_entry_price=Decimal("300.00"),
            current_price=Decimal("310.00"),
            unrealized_pnl=Decimal("500.00"),
            realized_pnl=Decimal("200.00"),
        )
        assert pos.realized_pnl == Decimal("200.00")


class TestEnums:
    """Test enum types in types.py"""

    def test_market_values(self):
        assert Market.KRX.value == "krx"
        assert Market.NYSE.value == "nyse"
        assert Market.NASDAQ.value == "nasdaq"
        assert Market.AMEX.value == "amex"

    def test_order_side_values(self):
        assert OrderSide.BUY.value == "buy"
        assert OrderSide.SELL.value == "sell"

    def test_order_type_values(self):
        assert OrderType.MARKET.value == "market"
        assert OrderType.LIMIT.value == "limit"

    def test_order_status_values(self):
        assert OrderStatus.PENDING.value == "pending"
        assert OrderStatus.SUBMITTED.value == "submitted"
        assert OrderStatus.PARTIAL_FILLED.value == "partial_filled"
        assert OrderStatus.FILLED.value == "filled"
        assert OrderStatus.CANCELLED.value == "cancelled"
        assert OrderStatus.REJECTED.value == "rejected"

    def test_signal_type_members(self):
        assert SignalType.ENTRY_LONG is not None
        assert SignalType.EXIT_LONG is not None
        assert SignalType.HOLD is not None


class TestQuoteAndBar:
    """Test Quote and Bar dataclasses"""

    def test_quote_creation(self):
        now = datetime.now()
        quote = Quote(
            symbol="AAPL",
            market=Market.NASDAQ,
            bid_price=Decimal("149.90"),
            ask_price=Decimal("150.10"),
            bid_size=500,
            ask_size=300,
            last_price=Decimal("150.00"),
            last_size=100,
            timestamp=now,
        )
        assert quote.symbol == "AAPL"
        assert quote.bid_price == Decimal("149.90")
        assert quote.ask_price == Decimal("150.10")
        assert quote.last_price == Decimal("150.00")

    def test_bar_creation(self):
        now = datetime.now()
        bar = Bar(
            symbol="AAPL",
            market=Market.NASDAQ,
            open=Decimal("149.00"),
            high=Decimal("152.00"),
            low=Decimal("148.50"),
            close=Decimal("151.00"),
            volume=5000000,
            timestamp=now,
            timeframe="1d",
        )
        assert bar.open == Decimal("149.00")
        assert bar.high == Decimal("152.00")
        assert bar.close == Decimal("151.00")
        assert bar.timeframe == "1d"


class TestFill:
    """Test Fill dataclass"""

    def test_fill_creation(self):
        now = datetime.now()
        fill = Fill(
            order_id="ORD-001",
            symbol="AAPL",
            market=Market.NASDAQ,
            side=OrderSide.BUY,
            quantity=100,
            price=Decimal("150.00"),
            commission=Decimal("1.00"),
            timestamp=now,
        )
        assert fill.order_id == "ORD-001"
        assert fill.quantity == 100
        assert fill.price == Decimal("150.00")
        assert fill.commission == Decimal("1.00")
        assert fill.pnl is None

    def test_fill_with_pnl(self):
        fill = Fill(
            order_id="ORD-002",
            symbol="AAPL",
            market=Market.NASDAQ,
            side=OrderSide.SELL,
            quantity=100,
            price=Decimal("160.00"),
            commission=Decimal("1.00"),
            timestamp=datetime.now(),
            pnl=Decimal("999.00"),
        )
        assert fill.pnl == Decimal("999.00")
