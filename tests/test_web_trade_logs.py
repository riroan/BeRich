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
