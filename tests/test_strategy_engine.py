"""StrategyEngine fill handling — idempotent, delta-based, partial-safe.

Regression coverage for the two CRITICAL audit findings:
- duplicate ORDER_FILLED double-counted the strategy position
- partial-then-cancelled orders left real shares untracked
"""

import pytest
from decimal import Decimal
from datetime import datetime
from unittest.mock import MagicMock

from src.strategy.engine import StrategyEngine
from src.core.events import Event, EventType
from src.core.types import Market, Order, OrderSide, OrderType


class _StubStrategy:
    def __init__(self, symbols):
        self.symbols = symbols
        self.name = "stub"
        self.fills = []

    async def on_fill(self, fill):
        self.fills.append(fill)


def _engine_with(strategy) -> StrategyEngine:
    eng = StrategyEngine(
        event_bus=MagicMock(), broker=MagicMock(), notifier=None,
    )
    eng.register_strategy(strategy)
    return eng


def _order(order_id, filled_qty, symbol="AAPL", side=OrderSide.BUY):
    o = Order(
        symbol=symbol, market=Market.NASDAQ, side=side,
        order_type=OrderType.MARKET, quantity=100,
        price=Decimal("10"), order_id=order_id,
    )
    o.filled_quantity = filled_qty
    o.filled_avg_price = Decimal("10")
    return o


def _ev(order, etype=EventType.ORDER_FILLED):
    return Event(event_type=etype, data=order,
                 timestamp=datetime.now(), source="t")


@pytest.mark.asyncio
async def test_cumulative_fills_applied_as_delta_and_idempotent():
    s = _StubStrategy(["AAPL"])
    eng = _engine_with(s)

    await eng._on_fill(_ev(_order("o1", 60), EventType.ORDER_PARTIAL_FILLED))
    await eng._on_fill(_ev(_order("o1", 100), EventType.ORDER_FILLED))
    await eng._on_fill(_ev(_order("o1", 100), EventType.ORDER_FILLED))  # dup

    # 60 then 40 — never the cumulative 100 twice (no double-count)
    assert [f.quantity for f in s.fills] == [60, 40]


@pytest.mark.asyncio
async def test_partial_then_cancel_keeps_real_shares():
    s = _StubStrategy(["AAPL"])
    eng = _engine_with(s)

    # partial fills 40, remainder later cancelled (no ORDER_FILLED ever)
    await eng._on_fill(_ev(_order("o2", 40), EventType.ORDER_PARTIAL_FILLED))

    # strategy still accounts the 40 real shares (previously: 0)
    assert sum(f.quantity for f in s.fills) == 40


@pytest.mark.asyncio
async def test_fill_for_unrelated_symbol_ignored():
    s = _StubStrategy(["MSFT"])
    eng = _engine_with(s)

    await eng._on_fill(_ev(_order("o3", 10, symbol="AAPL")))

    assert s.fills == []
