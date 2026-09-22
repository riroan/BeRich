"""Unsettled (T+1) sale proceeds show under Cash and stay out of Invested."""

from decimal import Decimal

from fastapi.testclient import TestClient

from src.web import app as web_app
from src.web.app import DashboardState


def _get(path: str, unsettled: str) -> str:
    original_auth_password = web_app.AUTH_PASSWORD
    original_mock_mode = web_app.MOCK_MODE
    original_dashboard_state = web_app.dashboard_state

    web_app.AUTH_PASSWORD = "test"
    web_app.MOCK_MODE = True
    state = DashboardState()
    state.balance_usd = Decimal("9553.87")
    state.cash_usd = Decimal("47.14")
    state.unsettled_usd = Decimal(unsettled)
    web_app.dashboard_state = state

    try:
        response = TestClient(web_app.create_app()).get(path)
    finally:
        web_app.AUTH_PASSWORD = original_auth_password
        web_app.MOCK_MODE = original_mock_mode
        web_app.dashboard_state = original_dashboard_state

    assert response.status_code == 200
    return response.text


def test_dashboard_shows_pending_under_cash():
    assert "+$1,134.73 pending" in _get("/", "1134.73")
    html = _get("/", "0")
    assert 'id="cash-usd-pending"></div>' in html
    assert 'id="hero-cash-sub">0%<' in html


def test_portfolio_invested_excludes_pending():
    html = _get("/portfolio", "1134.73")
    # 9553.87 total - 47.14 cash - 1134.73 pending
    assert "$8,372.00" in html
    assert "$9,506.73" not in html
    assert "+$1,134.73 pending" in html
