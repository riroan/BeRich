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


def test_deposit_flow_does_not_count_as_return():
    state = DashboardState()
    state.equity_history = [
        {"timestamp": "2026-06-01T00:00:00", "total_usd": 1000},
        {"timestamp": "2026-06-02T00:00:00", "total_usd": 1100},
        # $1000 deposited between here and the next snapshot
        {"timestamp": "2026-06-03T00:00:00", "total_usd": 2100},
        {"timestamp": "2026-06-04T00:00:00", "total_usd": 2310},
    ]
    state.cash_flows = [
        {
            "timestamp": "2026-06-02T12:00:00",
            "amount_usd": 1000.0,
            "flow_type": "adjustment",
        },
    ]

    state.calculate_performance()

    # 10% gain, flat interval around the deposit, then 10% gain: 21% TWR
    # (naive first-vs-last math would report +131%)
    assert state.performance.total_return_pct == pytest.approx(21.0)
    assert state.performance.mdd == pytest.approx(0.0)


def test_withdrawal_flow_does_not_count_as_loss():
    state = DashboardState()
    state.equity_history = [
        {"timestamp": "2026-06-01T00:00:00", "total_usd": 1000},
        {"timestamp": "2026-06-02T00:00:00", "total_usd": 500},
    ]
    state.cash_flows = [
        {
            "timestamp": "2026-06-01T12:00:00",
            "amount_usd": -500.0,
            "flow_type": "adjustment",
        },
    ]

    state.calculate_performance()

    assert state.performance.total_return_pct == pytest.approx(0.0)
    assert state.performance.mdd == pytest.approx(0.0)


def test_initial_principal_registration_does_not_change_metrics():
    state = DashboardState()
    state.equity_history = [
        {"timestamp": "2026-06-01T00:00:00", "total_usd": 1000},
        {"timestamp": "2026-06-02T00:00:00", "total_usd": 1100},
    ]
    state.cash_flows = [
        {
            "timestamp": "2026-06-01T12:00:00",
            "amount_usd": 1000.0,
            "flow_type": "initial",
        },
    ]

    state.calculate_performance()

    assert state.performance.total_return_pct == pytest.approx(10.0)


def test_principal_api_records_delta_ledger(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'flows.db'}"

    async def init_db():
        storage = Storage(db_url)
        await storage.initialize()
        await storage.close()

    asyncio.run(init_db())

    original_auth_password = web_app.AUTH_PASSWORD
    original_mock_mode = web_app.MOCK_MODE
    original_dashboard_state = web_app.dashboard_state

    web_app.AUTH_PASSWORD = "test"
    web_app.MOCK_MODE = True
    web_app.dashboard_state = DashboardState()
    web_app.dashboard_state.db_url = db_url

    try:
        client = TestClient(web_app.create_app())
        first = client.post("/api/principal", json={"principal_usd": 1000})
        second = client.post("/api/principal", json={"principal_usd": 1500})
        current = client.get("/api/principal")
    finally:
        web_app.AUTH_PASSWORD = original_auth_password
        web_app.MOCK_MODE = original_mock_mode
        web_app.dashboard_state = original_dashboard_state

    assert first.status_code == 200
    assert first.json()["principal_usd"] == pytest.approx(1000.0)
    assert second.json()["principal_usd"] == pytest.approx(1500.0)

    flows = current.json()["flows"]
    assert current.json()["principal_usd"] == pytest.approx(1500.0)
    assert [f["flow_type"] for f in flows] == ["initial", "adjustment"]
    assert flows[1]["amount_usd"] == pytest.approx(500.0)


def test_performance_page_computes_metrics_from_db_equity(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'equity.db'}"

    async def seed_db():
        from sqlalchemy import select
        from src.data.models import EquitySnapshot

        storage = Storage(db_url)
        await storage.initialize()
        rows = [
            (datetime(2026, 7, 1, 10, 0), "1000"),
            (datetime(2026, 7, 2, 10, 0), "1100"),
            (datetime(2026, 7, 3, 10, 0), "2100"),
        ]
        for _, usd in rows:
            await storage.save_equity_snapshot(
                total_krw=Decimal("0"), total_usd=Decimal(usd),
                cash_krw=Decimal("0"), cash_usd=Decimal(usd),
                position_value_krw=Decimal("0"),
                position_value_usd=Decimal("0"),
            )
        async with storage.async_session() as session:
            snaps = (await session.execute(
                select(EquitySnapshot).order_by(EquitySnapshot.id)
            )).scalars().all()
            for snap, (ts, _) in zip(snaps, rows):
                snap.timestamp = ts
            await session.commit()
        # $1000 deposited before the 2100 snapshot
        await storage.save_cash_flow(
            Decimal("1000"), flow_type="adjustment",
            timestamp=datetime(2026, 7, 2, 15, 0),
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
        response = TestClient(web_app.create_app()).get("/performance")
    finally:
        web_app.AUTH_PASSWORD = original_auth_password
        web_app.MOCK_MODE = original_mock_mode
        web_app.dashboard_state = original_dashboard_state

    assert response.status_code == 200
    # 10% then flat around the deposit — not the naive +110%
    assert "+10.00%" in response.text


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
