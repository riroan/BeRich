"""Repeated alerts: the same failure is sent once, then held back."""

from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core import decisions
from src.core.events import Event, EventBus, EventType
from src.core.exceptions import OrderError
from src.core.types import Market, Signal, SignalType
from src.execution.order_manager import OrderManager
from src.utils.notifier import (
    DiscordNotifier, REPEAT_WINDOW, STOP_LOSS_REPEAT_WINDOW, failure_signature,
)

T0 = datetime(2026, 11, 4, 9, 0)


def _notifier():
    n = DiscordNotifier(webhook_url="https://example.invalid/hook")
    n.send = AsyncMock(return_value=True)
    return n


def test_signature_prefers_the_error_code_and_masks_numbers():
    assert failure_signature("anything", "APBK0952") == "code:APBK0952"
    assert (
        failure_signature("주문가능금액 1,234,567원 초과")
        == failure_signature("주문가능금액  1,200,000원 초과")
    )
    assert failure_signature("주문가능금액 초과") != failure_signature("가격 제한폭 초과")


@pytest.mark.asyncio
async def test_same_failure_is_sent_once_per_window_then_summarised():
    n = _notifier()
    key = ("order_failed", "AAPL", "buy", "code:X")
    await n.send_throttled(key, "실패", 0, now=T0)
    for minutes in range(1, 60):
        await n.send_throttled(key, "실패", 0, now=T0 + timedelta(minutes=minutes))
    assert n.send.await_count == 1

    await n.send_throttled(key, "실패", 0, now=T0 + REPEAT_WINDOW)
    assert n.send.await_count == 2
    assert "같은 알림 59회 생략" in n.send.await_args.args[0]


@pytest.mark.asyncio
async def test_a_different_reason_or_symbol_is_sent_right_away():
    n = _notifier()
    with patch("src.utils.notifier.datetime") as dt:
        dt.now.return_value = T0
        await n.notify_order_failed("AAPL", "buy", "주문가능금액 초과")
        await n.notify_order_failed("AAPL", "buy", "주문가능금액 초과")
        await n.notify_order_failed("AAPL", "buy", "가격 제한폭 초과")
        await n.notify_order_failed("NVDA", "buy", "주문가능금액 초과")
        await n.notify_order_failed("AAPL", "sell", "주문가능금액 초과")
    assert n.send.await_count == 4


@pytest.mark.asyncio
async def test_failed_stop_loss_repeats_after_fifteen_minutes():
    n = _notifier()
    key = ("order_failed", "AAPL", "sell", "x")
    await n.send_throttled(key, "손절 실패", 0, window=STOP_LOSS_REPEAT_WINDOW, now=T0)
    await n.send_throttled(key, "손절 실패", 0, window=STOP_LOSS_REPEAT_WINDOW,
                           now=T0 + STOP_LOSS_REPEAT_WINDOW)
    assert n.send.await_count == 2


@pytest.mark.asyncio
async def test_a_success_makes_the_next_failure_news_again():
    n = _notifier()
    await n.notify_order_failed("AAPL", "buy", "주문가능금액 초과")
    n.reset_order_failures("AAPL", "buy")
    await n.notify_order_failed("AAPL", "buy", "주문가능금액 초과")
    assert n.send.await_count == 2


# ---------- order manager: rejected buys back off, alert once ----------

def _event():
    signal = Signal(
        signal_type=SignalType.ENTRY_LONG, symbol="AAPL", market=Market.NASDAQ,
        strength=0.5, metadata={"reason": "avg_down_stage_2"},
    )
    return Event(
        event_type=EventType.SIGNAL_GENERATED, source="test",
        data={"signal": signal, "strategy": "test"}, timestamp=datetime.now(),
    )


@pytest.mark.asyncio
async def test_insufficient_funds_alerts_once_and_pauses_that_symbols_buys():
    decisions.clear()
    broker = AsyncMock()
    broker.submit_order = AsyncMock(side_effect=OrderError(
        "Overseas order rejected: 주문가능금액을 초과 합니다", code="APBK0952",
    ))
    notifier = _notifier()
    manager = OrderManager(
        event_bus=MagicMock(spec=EventBus), broker=broker,
        risk_manager=MagicMock(validate_order=MagicMock(return_value=(True, None))),
        storage=AsyncMock(), notifier=notifier,
    )
    manager._signal_to_order = AsyncMock(return_value=MagicMock(
        symbol="AAPL", market=Market.NASDAQ, quantity=5, price=Decimal("100"),
        side=__import__("src.core.types", fromlist=["OrderSide"]).OrderSide.BUY,
    ))
    dashboard = MagicMock(trading_paused=False)

    with patch("src.execution.order_manager.get_dashboard_state", return_value=dashboard):
        await manager._on_signal(_event())
        manager._recent_orders.clear()  # past the 60s dedup window
        await manager._on_signal(_event())

    assert broker.submit_order.await_count == 1   # second signal held back
    assert notifier.send.await_count == 1
    assert decisions.recent()[0]["detail"].startswith("현금 부족")
