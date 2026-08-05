"""Positions must keep their RSI across a reload from the DB.

`current_positions` is a holding-only table with no rsi column, so records
read back from it carry rsi=None. Every /api/positions request rebuilt the
in-memory positions from those records, wiping the RSI the tick had set — so
the Positions table showed "-" while the RSI Monitor, fed by the same
rsi_values, showed the number.
"""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal

from fastapi.testclient import TestClient

os.environ.setdefault("DASHBOARD_PASSWORD", "test")

from src.core.types import Market  # noqa: E402
from src.data.storage import Storage  # noqa: E402
from src.web import app as web_app  # noqa: E402
from src.web.app import DashboardState  # noqa: E402


def _record(symbol="AAPL"):
    return {
        "symbol": symbol, "market": "NASDAQ", "quantity": 10,
        "avg_price": 100.0,
    }


def test_a_db_reload_keeps_the_rsi_the_tick_set():
    state = DashboardState()
    state.update_rsi("AAPL", 42.5, price=110.0, market="NASDAQ")

    state.replace_positions_from_records([_record()])

    assert state.positions["AAPL"].rsi == 42.5


def test_a_record_that_carries_rsi_still_wins():
    # The tick path passes rsi on the record; it must not be shadowed by a
    # stale rsi_values entry.
    state = DashboardState()
    state.update_rsi("AAPL", 42.5)

    state.replace_positions_from_records([{**_record(), "rsi": 61.0}])

    assert state.positions["AAPL"].rsi == 61.0
    assert state.rsi_values["AAPL"] == 61.0


def test_an_unknown_symbol_stays_none():
    state = DashboardState()

    state.replace_positions_from_records([_record("NVDA")])

    assert state.positions["NVDA"].rsi is None


def test_api_positions_serves_the_rsi(tmp_path, monkeypatch):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'pos.db'}"

    async def seed():
        storage = Storage(db_url)
        await storage.initialize()
        await storage.replace_current_positions_for_market(
            Market.NASDAQ,
            [{"symbol": "AAPL", "quantity": 10, "avg_price": Decimal("100")}],
        )
        await storage.close()

    asyncio.run(seed())

    state = DashboardState()
    state.db_url = db_url
    state.update_rsi("AAPL", 42.5, price=110.0, market="NASDAQ")
    monkeypatch.setattr(web_app, "AUTH_PASSWORD", "test")
    monkeypatch.setattr(web_app, "MOCK_MODE", True)
    monkeypatch.setattr(web_app, "dashboard_state", state)

    body = TestClient(web_app.create_app()).get("/api/positions").json()

    assert body[0]["symbol"] == "AAPL"
    assert body[0]["rsi"] == 42.5
