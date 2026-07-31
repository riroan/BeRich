"""Regression test for stale KIS tokens in symbol validation.

The dashboard used to hold a copy of the broker's access token, snapshotted
once at bot startup. KIS tokens expire after 24h, so on any pod that had been
up longer than that every "Add symbol" call sent a dead token and the user saw
"Invalid symbol 'TSLA': 기간이 만료된 token 입니다."
"""

from datetime import datetime, timedelta

import aiohttp
from fastapi.testclient import TestClient

from src.broker.kis.auth import KISAuth
from src.web import app as web_app
from src.web.app import DashboardState


class _FakeResponse:
    status = 200

    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return ""


class _FakeSession:
    """Serves a fresh token on POST and records the token used on GET."""

    sent_authorizations: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, json=None):
        return _FakeResponse(
            {"access_token": "fresh-token", "expires_in": 86400},
        )

    def get(self, url, headers=None, params=None):
        _FakeSession.sent_authorizations.append(headers["authorization"])
        return _FakeResponse({"rt_cd": "0", "output": {"last": "100"}})


def test_expired_kis_token_is_refreshed_before_symbol_validation(
    tmp_path, monkeypatch,
):
    auth = KISAuth(
        app_key="key",
        app_secret="secret",
        account_no="123",
        base_url="https://openapivts.koreainvestment.com:29443",
    )
    # Token snapshotted at startup, now long past its 24h TTL.
    auth._access_token = "stale-token"
    auth._token_expires_at = datetime.now() - timedelta(hours=1)

    state = DashboardState()
    state.db_url = f"sqlite+aiosqlite:///{tmp_path / 'symbols.db'}"
    state.kis_config = {
        "app_key": "key", "app_secret": "secret", "paper_trading": True,
    }
    state.kis_auth = auth

    _FakeSession.sent_authorizations = []
    monkeypatch.setattr(aiohttp, "ClientSession", _FakeSession)
    monkeypatch.setattr(web_app, "AUTH_PASSWORD", "test")
    monkeypatch.setattr(web_app, "MOCK_MODE", True)
    monkeypatch.setattr(web_app, "dashboard_state", state)

    TestClient(web_app.create_app()).post(
        "/api/symbols",
        json={
            "symbol": "TSLA", "market": "nasdaq", "strategy_name": "rsi",
        },
    )

    # The price lookup must carry the re-issued token, not the startup copy.
    assert _FakeSession.sent_authorizations == ["Bearer fresh-token"]
    assert auth._access_token == "fresh-token"
