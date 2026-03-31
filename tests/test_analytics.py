"""Tests for analytics module"""

import pytest
from datetime import datetime, timedelta
from decimal import Decimal

from src.analytics.reports import ReportGenerator, PeriodReport
from src.analytics.drawdown import DrawdownAnalyzer, DrawdownAnalysis
from src.analytics.statistics import TradeStatistics, OverallStats


class TestReportGenerator:
    """Test cases for ReportGenerator"""

    @pytest.fixture
    def sample_fills(self):
        """Create sample fills data (all within today)"""
        today = datetime.now().replace(hour=12, minute=0, second=0)
        return [
            {
                "symbol": "AAPL",
                "side": "sell",
                "price": 150,
                "quantity": 10,
                "pnl": 100,
                "timestamp": today.replace(hour=10).isoformat(),
            },
            {
                "symbol": "AAPL",
                "side": "sell",
                "price": 155,
                "quantity": 5,
                "pnl": -50,
                "timestamp": today.replace(hour=11).isoformat(),
            },
            {
                "symbol": "MSFT",
                "side": "sell",
                "price": 300,
                "quantity": 3,
                "pnl": 75,
                "timestamp": today.replace(hour=12).isoformat(),
            },
        ]

    @pytest.fixture
    def sample_equity_history(self):
        """Create sample equity history"""
        now = datetime.now()
        return [
            {"timestamp": (now - timedelta(days=2)).isoformat(), "total_usd": 10000},
            {"timestamp": (now - timedelta(days=1)).isoformat(), "total_usd": 10100},
            {"timestamp": now.isoformat(), "total_usd": 10150},
        ]

    def test_generate_daily_report(self, sample_fills, sample_equity_history):
        """Test daily report generation"""
        gen = ReportGenerator(sample_fills, sample_equity_history)
        report = gen.generate_daily_report()

        assert isinstance(report, PeriodReport)
        assert report.period_type == "daily"
        assert report.total_trades == 3
        assert report.winning_trades == 2
        assert report.losing_trades == 1

    def test_generate_weekly_report(self, sample_fills, sample_equity_history):
        """Test weekly report generation"""
        gen = ReportGenerator(sample_fills, sample_equity_history)
        report = gen.generate_weekly_report()

        assert isinstance(report, PeriodReport)
        assert report.period_type == "weekly"

    def test_generate_monthly_report(self, sample_fills, sample_equity_history):
        """Test monthly report generation"""
        gen = ReportGenerator(sample_fills, sample_equity_history)
        report = gen.generate_monthly_report()

        assert isinstance(report, PeriodReport)
        assert report.period_type == "monthly"

    def test_empty_fills(self):
        """Test with empty fills"""
        gen = ReportGenerator([], [])
        report = gen.generate_daily_report()

        assert report.total_trades == 0
        assert report.win_rate == 0

    def test_win_rate_calculation(self, sample_fills, sample_equity_history):
        """Test win rate calculation"""
        gen = ReportGenerator(sample_fills, sample_equity_history)
        report = gen.generate_daily_report()

        # 2 wins out of 3 trades = 66.67%
        assert report.win_rate == pytest.approx(66.67, rel=0.01)

    def test_by_symbol_breakdown(self, sample_fills, sample_equity_history):
        """Test per-symbol breakdown"""
        gen = ReportGenerator(sample_fills, sample_equity_history)
        report = gen.generate_daily_report()

        assert "AAPL" in report.by_symbol
        assert "MSFT" in report.by_symbol
        assert report.by_symbol["AAPL"]["trades"] == 2
        assert report.by_symbol["MSFT"]["trades"] == 1


class TestDrawdownAnalyzer:
    """Test cases for DrawdownAnalyzer"""

    @pytest.fixture
    def sample_equity_history(self):
        """Create sample equity history with drawdown"""
        now = datetime.now()
        return [
            {"timestamp": (now - timedelta(days=10)).isoformat(), "total_usd": 10000},
            {"timestamp": (now - timedelta(days=9)).isoformat(), "total_usd": 10500},  # Peak
            {"timestamp": (now - timedelta(days=8)).isoformat(), "total_usd": 10200},  # Drawdown starts
            {"timestamp": (now - timedelta(days=7)).isoformat(), "total_usd": 9800},   # More drawdown
            {"timestamp": (now - timedelta(days=6)).isoformat(), "total_usd": 9500},   # Bottom
            {"timestamp": (now - timedelta(days=5)).isoformat(), "total_usd": 9700},
            {"timestamp": (now - timedelta(days=4)).isoformat(), "total_usd": 10000},
            {"timestamp": (now - timedelta(days=3)).isoformat(), "total_usd": 10300},
            {"timestamp": (now - timedelta(days=2)).isoformat(), "total_usd": 10600},  # New peak
            {"timestamp": (now - timedelta(days=1)).isoformat(), "total_usd": 10400},  # Current drawdown
            {"timestamp": now.isoformat(), "total_usd": 10300},
        ]

    def test_analyze_drawdown(self, sample_equity_history):
        """Test drawdown analysis"""
        analyzer = DrawdownAnalyzer(sample_equity_history)
        analysis = analyzer.analyze("usd")

        assert isinstance(analysis, DrawdownAnalysis)
        assert analysis.current_equity == Decimal("10300")
        assert analysis.peak_equity == Decimal("10600")

    def test_mdd_calculation(self, sample_equity_history):
        """Test MDD calculation"""
        analyzer = DrawdownAnalyzer(sample_equity_history)
        analysis = analyzer.analyze("usd")

        # MDD should be from 10500 to 9500 = ~9.52%
        assert analysis.mdd_pct == pytest.approx(9.52, rel=0.1)

    def test_current_drawdown(self, sample_equity_history):
        """Test current drawdown calculation"""
        analyzer = DrawdownAnalyzer(sample_equity_history)
        analysis = analyzer.analyze("usd")

        # Current: 10300, Peak: 10600 = ~2.83% drawdown
        assert analysis.current_drawdown_pct == pytest.approx(2.83, rel=0.1)

    def test_alert_thresholds(self, sample_equity_history):
        """Test alert threshold detection"""
        analyzer = DrawdownAnalyzer(sample_equity_history)
        analysis = analyzer.analyze("usd")

        # Current drawdown is ~2.83%, below warning threshold
        assert analysis.alert_triggered is False

    def test_empty_history(self):
        """Test with empty history"""
        analyzer = DrawdownAnalyzer([])
        analysis = analyzer.analyze("usd")

        assert analysis.current_equity == Decimal("0")
        assert analysis.mdd_pct == 0.0

    def test_drawdown_history(self, sample_equity_history):
        """Test drawdown history generation"""
        analyzer = DrawdownAnalyzer(sample_equity_history)
        analysis = analyzer.analyze("usd")

        assert len(analysis.drawdown_history) == len(sample_equity_history)
        assert all("drawdown_pct" in p for p in analysis.drawdown_history)


class TestTradeStatistics:
    """Test cases for TradeStatistics"""

    @pytest.fixture
    def sample_fills(self):
        """Create sample fills for statistics"""
        now = datetime.now()
        return [
            {"symbol": "AAPL", "pnl": 100, "timestamp": now.isoformat()},
            {"symbol": "AAPL", "pnl": 50, "timestamp": now.isoformat()},
            {"symbol": "AAPL", "pnl": -30, "timestamp": now.isoformat()},
            {"symbol": "MSFT", "pnl": 200, "timestamp": now.isoformat()},
            {"symbol": "MSFT", "pnl": -100, "timestamp": now.isoformat()},
            {"symbol": "GOOGL", "pnl": 150, "timestamp": now.isoformat()},
        ]

    def test_calculate_statistics(self, sample_fills):
        """Test basic statistics calculation"""
        calc = TradeStatistics(sample_fills)
        stats = calc.calculate()

        assert isinstance(stats, OverallStats)
        assert stats.total_trades == 6
        assert stats.winning_trades == 4
        assert stats.losing_trades == 2

    def test_win_rate(self, sample_fills):
        """Test win rate calculation"""
        calc = TradeStatistics(sample_fills)
        stats = calc.calculate()

        # 4 wins out of 6 = 66.67%
        assert stats.win_rate == pytest.approx(66.67, rel=0.01)

    def test_pnl_calculations(self, sample_fills):
        """Test P&L calculations"""
        calc = TradeStatistics(sample_fills)
        stats = calc.calculate()

        # Total: 100 + 50 - 30 + 200 - 100 + 150 = 370
        assert stats.total_pnl == Decimal("370")
        assert stats.best_trade == Decimal("200")
        assert stats.worst_trade == Decimal("-100")

    def test_profit_factor(self, sample_fills):
        """Test profit factor calculation"""
        calc = TradeStatistics(sample_fills)
        stats = calc.calculate()

        # Wins: 100 + 50 + 200 + 150 = 500
        # Losses: 30 + 100 = 130
        # PF = 500 / 130 = 3.85
        assert stats.profit_factor == pytest.approx(3.85, rel=0.01)

    def test_by_symbol_stats(self, sample_fills):
        """Test per-symbol statistics"""
        calc = TradeStatistics(sample_fills)
        stats = calc.calculate()

        assert len(stats.by_symbol) == 3

        # Find AAPL stats
        aapl_stats = next(s for s in stats.by_symbol if s.symbol == "AAPL")
        assert aapl_stats.total_trades == 3
        assert aapl_stats.winning_trades == 2

    def test_streaks(self):
        """Test streak calculation"""
        fills = [
            {"symbol": "AAPL", "pnl": 100, "timestamp": datetime.now().isoformat()},
            {"symbol": "AAPL", "pnl": 50, "timestamp": datetime.now().isoformat()},
            {"symbol": "AAPL", "pnl": 25, "timestamp": datetime.now().isoformat()},
        ]
        calc = TradeStatistics(fills)
        stats = calc.calculate()

        assert stats.current_streak == 3
        assert stats.max_win_streak == 3
        assert stats.max_loss_streak == 0

    def test_empty_fills(self):
        """Test with empty fills"""
        calc = TradeStatistics([])
        stats = calc.calculate()

        assert stats.total_trades == 0
        assert stats.win_rate == 0

    def test_best_performing_symbols(self, sample_fills):
        """Test getting best performing symbols"""
        calc = TradeStatistics(sample_fills)
        best = calc.get_best_performing_symbols(2)

        assert len(best) == 2
        # GOOGL should be first (150 profit)
        assert best[0].symbol == "GOOGL"

    def test_worst_performing_symbols(self, sample_fills):
        """Test getting worst performing symbols"""
        calc = TradeStatistics(sample_fills)
        worst = calc.get_worst_performing_symbols(2)

        assert len(worst) == 2
