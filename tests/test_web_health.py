"""Kubernetes probe endpoints: /healthz (liveness) and /readyz (readiness)."""

import asyncio
import threading
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from src.web import app as web_app


@pytest.fixture
def state(monkeypatch):
    s = web_app.DashboardState()
    monkeypatch.setattr(web_app, "dashboard_state", s)
    monkeypatch.setattr(web_app, "AUTH_PASSWORD", "test-password")
    return s


@pytest.fixture
def bot_loop():
    """A running event loop in another thread, like the bot's."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=2)
    loop.close()


def _client():
    return TestClient(web_app.create_app())


def test_healthz_is_public_and_cheap(state):
    response = _client().get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_without_bot_is_ready(state):
    response = _client().get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {}}


def test_readyz_ready_when_loop_answers_and_market_closed(state, bot_loop):
    state.bot_loop = bot_loop
    state.market_open_check = lambda: False

    response = _client().get("/readyz")

    assert response.status_code == 200
    assert response.json()["checks"] == {"bot_loop": "ok", "ticks": "market closed"}


def test_readyz_fails_when_ticks_stale_during_market_hours(state, bot_loop):
    state.bot_loop = bot_loop
    state.market_open_check = lambda: True
    state.last_strategy_run = datetime.now() - timedelta(minutes=30)

    response = _client().get("/readyz")

    assert response.status_code == 503
    assert response.json()["checks"]["ticks"] == "stale"


def test_readyz_ok_with_recent_tick_during_market_hours(state, bot_loop):
    state.bot_loop = bot_loop
    state.market_open_check = lambda: True
    state.last_strategy_run = datetime.now() - timedelta(seconds=20)

    assert _client().get("/readyz").status_code == 200


def test_readyz_fails_when_bot_loop_stopped(state):
    loop = asyncio.new_event_loop()  # never started
    state.bot_loop = loop
    try:
        response = _client().get("/readyz")
    finally:
        loop.close()

    assert response.status_code == 503
    assert response.json()["checks"]["bot_loop"] == "unresponsive"


def test_readyz_does_not_leak_account_data(state):
    state.cash_usd = 12345
    body = _client().get("/readyz").text
    assert "12345" not in body
