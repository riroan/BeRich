"""The main screen shows KIS 주문가능금액 where Cash (예수금) used to be."""

from decimal import Decimal

from fastapi.testclient import TestClient

from src.web import app as web_app
from src.web.app import DashboardState


def _home(buying_power: Decimal | None) -> tuple[str, dict]:
    original = (web_app.AUTH_PASSWORD, web_app.MOCK_MODE, web_app.dashboard_state)
    web_app.AUTH_PASSWORD = "test"
    web_app.MOCK_MODE = True
    state = DashboardState()
    state.balance_usd = Decimal("10000")
    state.cash_usd = Decimal("903.17")
    state.buying_power_usd = buying_power
    web_app.dashboard_state = state
    try:
        response = TestClient(web_app.create_app()).get("/")
        snapshot = web_app.get_dashboard_snapshot()
    finally:
        web_app.AUTH_PASSWORD, web_app.MOCK_MODE, web_app.dashboard_state = original
    assert response.status_code == 200
    return response.text, snapshot


def test_main_screen_shows_buying_power():
    html, snapshot = _home(Decimal("1961.02"))

    assert 'id="cash-usd">$1,961.02<' in html
    assert 'id="hero-cash">$1,961.02<' in html
    assert ">Buying Power<" in html
    assert snapshot["buying_power_usd"] == 1961.02


def test_main_screen_falls_back_to_cash_without_buying_power():
    html, snapshot = _home(None)

    assert 'id="cash-usd">$903.17<' in html
    assert snapshot["buying_power_usd"] == 903.17


def test_portfolio_cash_shows_buying_power():
    original = (web_app.AUTH_PASSWORD, web_app.MOCK_MODE, web_app.dashboard_state)
    web_app.AUTH_PASSWORD = "test"
    web_app.MOCK_MODE = True
    state = DashboardState()
    state.balance_usd = Decimal("10000")
    state.cash_usd = Decimal("903.17")
    state.buying_power_usd = Decimal("1961.02")
    web_app.dashboard_state = state
    try:
        html = TestClient(web_app.create_app()).get("/portfolio").text
    finally:
        web_app.AUTH_PASSWORD, web_app.MOCK_MODE, web_app.dashboard_state = original

    assert ">Cash<" in html
    assert "$1,961.02" in html
    assert "$903.17" not in html
