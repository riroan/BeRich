"""Tests for dashboard performance metric rendering."""

import asyncio
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from src.core.types import Fill, Market, OrderSide
from src.data.storage import Storage
from src.web import app as web_app
from src.web.app import DashboardState


def _winning_sell_fills():
    return [
        {"side": "SELL", "pnl": 5.08},
        {"side": "SELL", "pnl": 6.27},
        {"side": "SELL", "pnl": 6.16},
        {"side": "SELL", "pnl": 29.26},
        {"side": "SELL", "pnl": 35.86},
        {"side": "BUY", "pnl": None},
    ]


def test_profit_factor_is_unbounded_when_no_losing_sells():
    state = DashboardState()
    state.fills = _winning_sell_fills()

    state.calculate_performance()

    assert state.performance.total_pnl == pytest.approx(82.63)
    assert state.performance.total_trades == 5
    assert state.performance.winning_trades == 5
    assert state.performance.losing_trades == 0
    assert state.performance.profit_factor is None


def test_performance_page_displays_infinity_for_no_losing_sells():
    original_auth_password = web_app.AUTH_PASSWORD
    original_mock_mode = web_app.MOCK_MODE
    original_dashboard_state = web_app.dashboard_state

    web_app.AUTH_PASSWORD = "test"
    web_app.MOCK_MODE = True
    web_app.dashboard_state = DashboardState()
    web_app.dashboard_state.fills = _winning_sell_fills()

    try:
        response = TestClient(web_app.create_app()).get("/performance")
    finally:
        web_app.AUTH_PASSWORD = original_auth_password
        web_app.MOCK_MODE = original_mock_mode
        web_app.dashboard_state = original_dashboard_state

    assert response.status_code == 200
    assert "$82.63" in response.text
    assert "∞" in response.text


def test_performance_prefers_adjusted_equity_history():
    state = DashboardState()
    state.equity_history = [
        {
            "timestamp": "2026-06-01T00:00:00",
            "total_usd": 1000,
            "adjusted_total_usd": 1000,
        },
        {
            "timestamp": "2026-06-02T00:00:00",
            "total_usd": 900,
            "adjusted_total_usd": 1200,
        },
    ]

    state.calculate_performance()

    assert state.performance.total_return_pct == pytest.approx(20.0)
    assert state.performance.mdd == pytest.approx(0.0)


def test_performance_page_loads_fills_from_db(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'fills.db'}"

    async def seed_db():
        storage = Storage(db_url)
        await storage.initialize()
        for idx, pnl in enumerate([5.08, 6.27, 6.16, 29.26, 35.86], start=1):
            await storage.save_fill(
                Fill(
                    order_id=f"sell-{idx}",
                    symbol="AAPL",
                    market=Market.NASDAQ,
                    side=OrderSide.SELL,
                    quantity=1,
                    price=Decimal("100"),
                    commission=Decimal("0"),
                    pnl=Decimal(str(pnl)),
                    timestamp=datetime(2026, 6, idx, 9, 30),
                )
            )
        await storage.save_fill(
            Fill(
                order_id="buy-1",
                symbol="AAPL",
                market=Market.NASDAQ,
                side=OrderSide.BUY,
                quantity=1,
                price=Decimal("95"),
                commission=Decimal("0"),
                pnl=None,
                timestamp=datetime(2026, 5, 31, 9, 30),
            )
        )
        await storage.close()

    asyncio.run(seed_db())

    original_auth_password = web_app.AUTH_PASSWORD
    original_mock_mode = web_app.MOCK_MODE
    original_dashboard_state = web_app.dashboard_state

    web_app.AUTH_PASSWORD = "test"
    web_app.MOCK_MODE = True
    web_app.dashboard_state = DashboardState()
    web_app.dashboard_state.db_url = db_url

    try:
        client = TestClient(web_app.create_app())
        response = client.get("/performance")
        trade_logs_response = client.get("/api/trade-logs")
    finally:
        web_app.AUTH_PASSWORD = original_auth_password
        web_app.MOCK_MODE = original_mock_mode
        web_app.dashboard_state = original_dashboard_state

    assert response.status_code == 200
    assert "$82.63" in response.text
    assert "∞" in response.text
    assert trade_logs_response.status_code == 200
    assert trade_logs_response.json()[0]["symbol"] == "AAPL"
