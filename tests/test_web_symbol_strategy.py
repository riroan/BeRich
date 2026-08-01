"""Tests for editing a symbol's strategy in place, and weight on add.

Before this, changing which strategy a symbol belonged to meant deleting the
symbol and re-adding it under the other strategy, which lost its weight.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from src.data.storage import Storage
from src.web import app as web_app
from src.web.app import DashboardState


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'strategies.db'}"

    async def seed():
        storage = Storage(db_url)
        await storage.initialize()
        await storage.create_strategy_config(
            name="rsi", class_path="src.strategy.rsi.RSIStrategy",
            market="nasdaq",
            symbols=[{"symbol": "AAPL", "max_weight": 35.0, "enabled": False}],
            params={},
        )
        await storage.create_strategy_config(
            name="momentum", class_path="src.strategy.mom.MomStrategy",
            market="nasdaq", symbols=[{"symbol": "MSFT", "max_weight": 10.0}],
            params={},
        )
        await storage.create_strategy_config(
            name="krx_rsi", class_path="src.strategy.rsi.RSIStrategy",
            market="krx", symbols=[], params={},
        )
        await storage.close()

    asyncio.run(seed())

    state = DashboardState()
    state.db_url = db_url
    monkeypatch.setattr(web_app, "AUTH_PASSWORD", "test")
    monkeypatch.setattr(web_app, "MOCK_MODE", True)
    monkeypatch.setattr(web_app, "dashboard_state", state)
    return TestClient(web_app.create_app())


def test_move_symbol_to_another_strategy_keeps_weight_and_enabled(client):
    strategies = {
        s["name"]: s for s in client.get("/api/strategies").json()["strategies"]
    }

    resp = client.post(
        f"/api/symbols/{strategies['rsi']['id']}/strategy?symbol=AAPL",
        json={"strategy_name": "momentum"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"symbol": "AAPL", "strategy_name": "momentum"}

    after = {
        s["name"]: s for s in client.get("/api/strategies").json()["strategies"]
    }
    assert after["rsi"]["symbols"] == []
    # The entry moves whole, so weight and the per-symbol enabled flag survive.
    assert after["momentum"]["symbols"] == [
        {"symbol": "MSFT", "max_weight": 10.0},
        {
            "symbol": "AAPL", "max_weight": 35.0, "enabled": False,
            "market": "nasdaq",
        },
    ]


def test_move_into_a_strategy_of_another_market_keeps_the_symbol_market(client):
    # A strategy holds several markets now, so this move is allowed. What
    # must not happen is AAPL silently becoming a KRX symbol.
    strategies = {
        s["name"]: s for s in client.get("/api/strategies").json()["strategies"]
    }

    resp = client.post(
        f"/api/symbols/{strategies['rsi']['id']}/strategy?symbol=AAPL",
        json={"strategy_name": "krx_rsi"},
    )

    assert resp.status_code == 200

    after = {
        s["name"]: s for s in client.get("/api/strategies").json()["strategies"]
    }
    assert after["rsi"]["symbols"] == []
    moved = after["krx_rsi"]["symbols"][0]
    assert moved["symbol"] == "AAPL"
    assert moved["market"] == "nasdaq"


def test_symbols_api_reports_the_symbol_market_not_the_config_market(client):
    strategies = {
        s["name"]: s for s in client.get("/api/strategies").json()["strategies"]
    }
    client.post(
        f"/api/symbols/{strategies['rsi']['id']}/strategy?symbol=AAPL",
        json={"strategy_name": "krx_rsi"},
    )

    rows = {
        r["symbol"]: r for r in client.get("/api/symbols").json()["symbols"]
    }

    assert rows["AAPL"]["strategy_name"] == "krx_rsi"
    assert rows["AAPL"]["market"] == "nasdaq"


def test_move_symbol_already_in_target_is_rejected(client):
    strategies = {
        s["name"]: s for s in client.get("/api/strategies").json()["strategies"]
    }
    client.post(
        f"/api/symbols/{strategies['rsi']['id']}/strategy?symbol=AAPL",
        json={"strategy_name": "momentum"},
    )
    # AAPL now lives in momentum; moving it there again must not duplicate it.
    resp = client.post(
        f"/api/symbols/{strategies['momentum']['id']}/strategy?symbol=MSFT",
        json={"strategy_name": "momentum"},
    )

    assert resp.status_code == 200
    after = {
        s["name"]: s for s in client.get("/api/strategies").json()["strategies"]
    }
    assert [s["symbol"] for s in after["momentum"]["symbols"]] == [
        "MSFT", "AAPL",
    ]


def test_add_symbol_uses_supplied_weight(client):
    resp = client.post(
        "/api/symbols",
        json={
            "symbol": "TSLA", "market": "nasdaq", "strategy_name": "momentum",
            "max_weight": 7.5,
        },
    )

    assert resp.status_code == 200
    after = {
        s["name"]: s for s in client.get("/api/strategies").json()["strategies"]
    }
    assert {
        "symbol": "TSLA", "market": "nasdaq", "max_weight": 7.5,
    } in after["momentum"]["symbols"]


def test_add_symbol_defaults_weight_when_omitted(client):
    client.post(
        "/api/symbols",
        json={
            "symbol": "TSLA", "market": "nasdaq", "strategy_name": "momentum",
        },
    )

    after = {
        s["name"]: s for s in client.get("/api/strategies").json()["strategies"]
    }
    assert {
        "symbol": "TSLA", "market": "nasdaq", "max_weight": 20.0,
    } in after["momentum"]["symbols"]


def test_add_symbol_rejects_out_of_range_weight(client):
    resp = client.post(
        "/api/symbols",
        json={
            "symbol": "TSLA", "market": "nasdaq", "strategy_name": "momentum",
            "max_weight": 0,
        },
    )

    assert resp.status_code == 400
    assert "between 1 and 100" in resp.json()["detail"]
