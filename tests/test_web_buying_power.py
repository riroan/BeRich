"""The main screen shows Buying Power (KIS 주문가능금액) and Cash (예수금) apart."""

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


def test_main_screen_shows_buying_power_and_cash_apart():
    html, snapshot = _home(Decimal("1961.02"))

    assert 'id="buying-power-usd">$1,961.02<' in html
    assert 'id="hero-buying-power">$1,961.02<' in html
    assert 'id="cash-usd">$903.17<' in html
    assert 'id="hero-cash">$903.17<' in html
    assert ">Buying Power<" in html and ">Cash<" in html
    assert snapshot["buying_power_usd"] == 1961.02
    assert snapshot["cash_usd"] == 903.17


def test_unknown_buying_power_shows_a_dash_not_the_cash():
    html, snapshot = _home(None)

    assert 'id="buying-power-usd">—<' in html
    assert 'id="cash-usd">$903.17<' in html
    assert snapshot["buying_power_usd"] is None
