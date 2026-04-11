"""Trade report generation - daily/weekly/monthly summaries"""

from datetime import datetime, timedelta
from decimal import Decimal
from dataclasses import dataclass, field
import logging

logger = logging.getLogger(__name__)


@dataclass
class PeriodReport:
    """Report for a specific time period"""
    period_type: str  # daily, weekly, monthly
    start_date: datetime
    end_date: datetime

    # Trade counts
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0

    # P&L
    total_pnl: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")

    # Performance
    win_rate: float = 0.0
    avg_win: Decimal = Decimal("0")
    avg_loss: Decimal = Decimal("0")
    profit_factor: float = 0.0
    best_trade: Decimal = Decimal("0")
    worst_trade: Decimal = Decimal("0")

    # Volume
    total_buy_volume: Decimal = Decimal("0")
    total_sell_volume: Decimal = Decimal("0")

    # Returns
    return_pct: float = 0.0

    # By symbol breakdown
    by_symbol: dict = field(default_factory=dict)


class ReportGenerator:
    """Generates trading performance reports"""

    def __init__(self, fills: list, equity_history: list):
        self.fills = fills
        self.equity_history = equity_history

    def generate_daily_report(self, date: datetime | None = None) -> PeriodReport:
        """Generate daily report"""
        if date is None:
            date = datetime.now()

        start = datetime(date.year, date.month, date.day)
        end = start + timedelta(days=1)

        return self._generate_report("daily", start, end)

    def generate_weekly_report(self, date: datetime | None = None) -> PeriodReport:
        """Generate weekly report (Mon-Sun)"""
        if date is None:
            date = datetime.now()

        # Get Monday of current week
        start = date - timedelta(days=date.weekday())
        start = datetime(start.year, start.month, start.day)
        end = start + timedelta(days=7)

        return self._generate_report("weekly", start, end)

    def generate_monthly_report(self, date: datetime | None = None) -> PeriodReport:
        """Generate monthly report"""
        if date is None:
            date = datetime.now()

        start = datetime(date.year, date.month, 1)
        if date.month == 12:
            end = datetime(date.year + 1, 1, 1)
        else:
            end = datetime(date.year, date.month + 1, 1)

        return self._generate_report("monthly", start, end)

    def _generate_report(
        self, period_type: str, start: datetime, end: datetime
    ) -> PeriodReport:
        """Generate report for given period"""
        report = PeriodReport(
            period_type=period_type,
            start_date=start,
            end_date=end,
        )

        # Filter fills for period
        period_fills = self._filter_fills(start, end)

        if not period_fills:
            return report

        # Calculate metrics
        wins = []
        losses = []
        by_symbol = {}

        for fill in period_fills:
            symbol = fill.get("symbol", "")
            side = fill.get("side", "")
            price = Decimal(str(fill.get("price", 0)))
            quantity = fill.get("quantity", 0)
            pnl = fill.get("pnl")

            # Track volume
            volume = price * quantity
            if side == "buy":
                report.total_buy_volume += volume
            else:
                report.total_sell_volume += volume

            # Track P&L for sells
            if pnl is not None:
                pnl_decimal = Decimal(str(pnl))
                report.total_trades += 1
                report.realized_pnl += pnl_decimal

                if pnl_decimal >= 0:
                    report.winning_trades += 1
                    wins.append(pnl_decimal)
                else:
                    report.losing_trades += 1
                    losses.append(pnl_decimal)

                # By symbol
                if symbol not in by_symbol:
                    by_symbol[symbol] = {
                        "trades": 0,
                        "wins": 0,
                        "losses": 0,
                        "pnl": Decimal("0"),
                    }
                by_symbol[symbol]["trades"] += 1
                by_symbol[symbol]["pnl"] += pnl_decimal
                if pnl_decimal >= 0:
                    by_symbol[symbol]["wins"] += 1
                else:
                    by_symbol[symbol]["losses"] += 1

        # Calculate derived metrics
        if report.total_trades > 0:
            report.win_rate = report.winning_trades / report.total_trades * 100

        if wins:
            report.avg_win = sum(wins) / len(wins)
            report.best_trade = max(wins)

        if losses:
            report.avg_loss = sum(losses) / len(losses)
            report.worst_trade = min(losses)

        total_wins = sum(wins) if wins else Decimal("0")
        total_losses = abs(sum(losses)) if losses else Decimal("0")
        if total_losses > 0:
            report.profit_factor = float(total_wins / total_losses)

        report.total_pnl = report.realized_pnl
        report.by_symbol = by_symbol

        # Calculate return percentage from equity history
        report.return_pct = self._calculate_period_return(start, end)

        return report

    def _filter_fills(self, start: datetime, end: datetime) -> list:
        """Filter fills within date range"""
        result = []
        for fill in self.fills:
            timestamp = fill.get("timestamp")
            if timestamp:
                if isinstance(timestamp, str):
                    try:
                        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                else:
                    ts = timestamp

                # Remove timezone for comparison
                if hasattr(ts, 'replace'):
                    ts = ts.replace(tzinfo=None)

                if start <= ts < end:
                    result.append(fill)
        return result

    def _calculate_period_return(self, start: datetime, end: datetime) -> float:
        """Calculate return percentage for period from equity history"""
        if not self.equity_history:
            return 0.0

        start_equity = None
        end_equity = None

        for point in self.equity_history:
            timestamp = point.get("timestamp")
            if timestamp:
                if isinstance(timestamp, str):
                    try:
                        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                else:
                    ts = timestamp

                if hasattr(ts, 'replace'):
                    ts = ts.replace(tzinfo=None)

                total = point.get("total_usd", 0) or point.get("total_krw", 0)

                if ts >= start and start_equity is None:
                    start_equity = total
                if ts < end:
                    end_equity = total

        if start_equity and end_equity and start_equity > 0:
            return (end_equity - start_equity) / start_equity * 100

        return 0.0

    def get_recent_reports(self, days: int = 30) -> list[PeriodReport]:
        """Get daily reports for recent days"""
        reports = []
        today = datetime.now()

        for i in range(days):
            date = today - timedelta(days=i)
            report = self.generate_daily_report(date)
            if report.total_trades > 0:
                reports.append(report)

        return reports
