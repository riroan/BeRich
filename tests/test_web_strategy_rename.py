"""Tests for renaming a strategy config from the settings page.

Renaming used to be impossible: the row is looked up by name, so `name` in
the update body collided with the path parameter and was silently ignored.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from src.data.storage import Storage
from src.web import app as web_app
from src.web.app import DashboardState


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'rename.db'}"

    async def seed():
        storage = Storage(db_url)
        await storage.initialize()
        await storage.create_strategy_config(
            name="RSI_Old", class_path="src.strategy.rsi.RSIStrategy",
            market="nasdaq",
            symbols=[{"symbol": "AAPL", "max_weight": 12.0}],
            params={"rsi_period": 14},
        )
        await storage.create_strategy_config(
            name="Taken", class_path="src.strategy.rsi.RSIStrategy",
            market="nasdaq", symbols=[], params={},
        )
        await storage.close()

    asyncio.run(seed())

    state = DashboardState()
    state.db_url = db_url
    monkeypatch.setattr(web_app, "AUTH_PASSWORD", "test")
    monkeypatch.setattr(web_app, "MOCK_MODE", True)
    monkeypatch.setattr(web_app, "dashboard_state", state)
    return TestClient(web_app.create_app())


def _names(client):
    return {
        s["name"]: s for s in client.get("/api/strategies").json()["strategies"]
    }


def test_rename_keeps_symbols_and_params(client):
    resp = client.put(
        "/api/strategies/RSI_Old", json={"name": "RSI_New"},
    )

    assert resp.status_code == 200
    assert resp.json()["strategy"]["name"] == "RSI_New"

    after = _names(client)
    assert "RSI_Old" not in after
    assert after["RSI_New"]["symbols"] == [
        {"symbol": "AAPL", "max_weight": 12.0},
    ]
    assert after["RSI_New"]["params"] == {"rsi_period": 14}


def test_rename_and_params_update_together(client):
    resp = client.put(
        "/api/strategies/RSI_Old",
        json={"name": "RSI_New", "params": {"rsi_period": 21}},
    )

    assert resp.status_code == 200
    after = _names(client)
    assert after["RSI_New"]["params"] == {"rsi_period": 21}


def test_rename_to_existing_name_is_rejected(client):
    resp = client.put(
        "/api/strategies/RSI_Old", json={"name": "Taken"},
    )

    assert resp.status_code == 400
    assert "already exists" in resp.json()["detail"]
    assert "RSI_Old" in _names(client)


def test_saving_without_renaming_still_works(client):
    resp = client.put(
        "/api/strategies/RSI_Old",
        json={"name": "RSI_Old", "params": {"rsi_period": 9}},
    )

    assert resp.status_code == 200
    assert _names(client)["RSI_Old"]["params"] == {"rsi_period": 9}


@pytest.mark.parametrize("bad", [
    "has space",
    "quote'; alert(1);//",
    "<script>",
    "",
    "x" * 101,
])
def test_names_that_would_break_settings_markup_are_rejected(client, bad):
    # The name is interpolated into DOM ids and inline JS string literals on
    # the settings page, so a quote or angle bracket escapes its context.
    resp = client.put("/api/strategies/RSI_Old", json={"name": bad})

    assert resp.status_code == 400
    assert "RSI_Old" in _names(client)


def test_create_rejects_the_same_unsafe_names(client):
    resp = client.post(
        "/api/strategies",
        json={
            "name": "bad'; alert(1);//",
            "class_path": "src.strategy.rsi.RSIStrategy",
            "market": "nasdaq", "symbols": [], "params": {},
        },
    )

    assert resp.status_code == 400
