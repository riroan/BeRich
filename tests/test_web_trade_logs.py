"""Tests for dashboard trade log timestamps."""

from datetime import datetime

from src.web.app import DashboardState


class TestDashboardTradeLogs:
    def test_add_trade_log_uses_supplied_timestamp(self):
        state = DashboardState()
        trade_time = datetime(2024, 1, 2, 3, 4, 5)

        state.add_trade_log(
            symbol="AAPL",
            market="NASDAQ",
            action="buy",
            price=150.0,
            quantity=10,
            trigger_rule="historical",
            timestamp=trade_time,
        )

        assert state.trade_logs[0].timestamp == "2024-01-02 03:04:05"
        assert state.trade_points["AAPL"][0]["time"] == "2024-01-02 03:04"


def test_trades_page_lists_each_symbol_once_in_filter():
    from fastapi.testclient import TestClient

    from src.web import app as web_app

    original = (web_app.AUTH_PASSWORD, web_app.MOCK_MODE, web_app.dashboard_state)
    web_app.AUTH_PASSWORD = "test"
    web_app.MOCK_MODE = True
    state = DashboardState()
    for symbol in ("TQQQ", "SOXL", "TQQQ"):
        state.add_trade_log(
            symbol=symbol, market="NASDAQ", action="buy", price=1.0,
            quantity=1, trigger_rule="historical",
        )
    web_app.dashboard_state = state
    try:
        html = TestClient(web_app.create_app()).get("/trades").text
    finally:
        web_app.AUTH_PASSWORD, web_app.MOCK_MODE, web_app.dashboard_state = original

    assert html.count('<option value="TQQQ">') == 1
    assert html.index('<option value="SOXL">') < html.index('<option value="TQQQ">')
    assert html.count('data-symbol="TQQQ"') == 2
