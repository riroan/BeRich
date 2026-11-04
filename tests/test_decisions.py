"""Activity feed: why a signal did or did not become an order."""

from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core import decisions
from src.core.events import Event, EventBus, EventType
from src.core.types import Market, Signal, SignalType
from src.execution.order_manager import OrderManager

PATCH_DASHBOARD = "src.execution.order_manager.get_dashboard_state"


@pytest.fixture(autouse=True)
def _clean_feed():
    decisions.clear()
    yield
    decisions.clear()


def test_newest_first_and_limited():
    t0 = datetime(2026, 11, 4, 9, 0)
    for i in range(3):
        decisions.record(f"S{i}", decisions.ORDER, "buy", "주문", now=t0 + timedelta(minutes=i))

    assert [d["symbol"] for d in decisions.recent(2)] == ["S2", "S1"]


def test_a_repeating_block_is_recorded_once_per_window():
    t0 = datetime(2026, 11, 4, 9, 0)
    for minutes in (0, 1, 29):
        decisions.record("X", decisions.BLOCKED, "buy", "쿨다운", now=t0 + timedelta(minutes=minutes))
    assert len(decisions.recent()) == 1

    decisions.record("X", decisions.BLOCKED, "buy", "쿨다운", now=t0 + timedelta(minutes=31))
    assert len(decisions.recent()) == 2


def test_orders_are_never_deduplicated():
    t0 = datetime(2026, 11, 4, 9, 0)
    decisions.record("X", decisions.ORDER, "buy", "단계 매수 1주 주문", now=t0)
    decisions.record("X", decisions.ORDER, "buy", "단계 매수 1주 주문", now=t0)
    assert len(decisions.recent()) == 2


def _manager(validate=(True, None)):
    broker = AsyncMock()
    broker.get_current_price = AsyncMock(return_value=Decimal("100"))
    broker.get_positions = AsyncMock(return_value=[])
    risk = MagicMock()
    risk.validate_order = MagicMock(return_value=validate)
    return OrderManager(
        event_bus=MagicMock(spec=EventBus), broker=broker,
        risk_manager=risk, storage=AsyncMock(),
    )


def _event():
    signal = Signal(
        signal_type=SignalType.ENTRY_LONG, symbol="AAPL", market=Market.NASDAQ,
        strength=0.5, metadata={"reason": "avg_down_stage_1"},
    )
    return Event(
        event_type=EventType.SIGNAL_GENERATED,
        data={"signal": signal, "strategy": "test"}, timestamp=datetime.now(),
        source="test",
    )


@pytest.mark.asyncio
async def test_a_paused_signal_shows_up_as_blocked():
    dashboard = MagicMock(trading_paused=True)
    with patch(PATCH_DASHBOARD, return_value=dashboard):
        await _manager()._on_signal(_event())

    entry = decisions.recent()[0]
    assert (entry["symbol"], entry["outcome"], entry["action"]) == ("AAPL", "blocked", "buy")
    assert "일시정지" in entry["detail"]


@pytest.mark.asyncio
async def test_a_risk_rejection_shows_up_with_its_limit():
    manager = _manager(validate=(False, "Daily loss limit exceeded: -400 (limit -300)"))
    manager._signal_to_order = AsyncMock(return_value=MagicMock(quantity=1))
    with patch(PATCH_DASHBOARD, return_value=MagicMock(trading_paused=False)):
        await manager._on_signal(_event())

    entry = decisions.recent()[0]
    assert entry["outcome"] == "rejected"
    assert entry["detail"] == "리스크 한도: Daily loss limit exceeded"
