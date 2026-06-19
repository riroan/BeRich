"""Tests for DB-backed dashboard positions."""

import asyncio
from decimal import Decimal

from fastapi.testclient import TestClient

from src.core.types import Market
from src.data.storage import Storage
from src.web import app as web_app


def test_api_positions_reads_current_positions_from_db(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'positions.db'}"

    async def seed_db():
        storage = Storage(db_url)
        await storage.initialize()
        await storage.replace_current_positions_for_market(
            Market.NASDAQ,
            [
                    {
                        "symbol": "AAPL",
                        "quantity": 2,
                        "avg_price": 100,
                    },
                ],
        )
        await storage.save_price_rsi(
            symbol="AAPL",
            market=Market.NASDAQ,
            price=Decimal("110"),
            rsi=42.5,
        )
        await storage.close()

    asyncio.run(seed_db())

    original_auth_password = web_app.AUTH_PASSWORD
    original_mock_mode = web_app.MOCK_MODE
    original_db_url = web_app.dashboard_state.db_url
    original_positions = web_app.dashboard_state.positions
    original_rsi_values = web_app.dashboard_state.rsi_values
    original_rsi_prices = web_app.dashboard_state.rsi_prices
    web_app.AUTH_PASSWORD = "test"
    web_app.MOCK_MODE = True
    web_app.dashboard_state.db_url = db_url
    web_app.dashboard_state.positions = {}
    web_app.dashboard_state.rsi_values = {}
    web_app.dashboard_state.rsi_prices = {}

    try:
        response = TestClient(web_app.create_app()).get("/api/positions")
    finally:
        web_app.AUTH_PASSWORD = original_auth_password
        web_app.MOCK_MODE = original_mock_mode
        web_app.dashboard_state.db_url = original_db_url
        web_app.dashboard_state.positions = original_positions
        web_app.dashboard_state.rsi_values = original_rsi_values
        web_app.dashboard_state.rsi_prices = original_rsi_prices

    assert response.status_code == 200
    assert response.json()[0]["symbol"] == "AAPL"
    assert response.json()[0]["current_price"] == 110.0
    assert response.json()[0]["pnl_pct"] == 10.0
    assert response.json()[0]["rsi"] == 42.5
