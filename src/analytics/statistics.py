"""Trade statistics - win rate by symbol, time period, etc."""

from datetime import datetime, timedelta
from decimal import Decimal
from dataclasses import dataclass, field
from collections import defaultdict
import logging

logger = logging.getLogger(__name__)


@dataclass
class SymbolStats:
    """Statistics for a single symbol"""
    symbol: str
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    total_pnl: Decimal = Decimal("0")
    avg_pnl: Decimal = Decimal("0")
    avg_win: Decimal = Decimal("0")
    avg_loss: Decimal = Decimal("0")
    best_trade: Decimal = Decimal("0")
    worst_trade: Decimal = Decimal("0")
    profit_factor: float = 0.0
    avg_hold_time_hours: float = 0.0


@dataclass
class TimeStats:
    """Statistics for a time period"""
    period: str  # hour, day_of_week, month
    label: str
    total_trades: int = 0
    winning_trades: int = 0
    win_rate: float = 0.0
    total_pnl: Decimal = Decimal("0")


@dataclass
class OverallStats:
    """Overall trading statistics"""
    # Basic counts
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0

    # P&L
    total_pnl: Decimal = Decimal("0")
    avg_pnl: Decimal = Decimal("0")
    avg_win: Decimal = Decimal("0")
    avg_loss: Decimal = Decimal("0")
    best_trade: Decimal = Decimal("0")
    worst_trade: Decimal = Decimal("0")
    profit_factor: float = 0.0

    # Streaks
    current_streak: int = 0  # Positive = wins, negative = losses
    max_win_streak: int = 0
    max_loss_streak: int = 0

    # By symbol
    by_symbol: list = field(default_factory=list)

    # By time
    by_hour: list = field(default_factory=list)
    by_day_of_week: list = field(default_factory=list)
    by_month: list = field(default_factory=list)

    # Recent performance
    last_7_days_win_rate: float = 0.0
    last_30_days_win_rate: float = 0.0


class TradeStatistics:
    """Calculates comprehensive trade statistics"""

    DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    def __init__(self, fills: list):
        self.fills = fills

    def calculate(self) -> OverallStats:
        """Calculate all statistics"""
        stats = OverallStats()

        # Filter sells with P&L
        trades = [f for f in self.fills if f.get("pnl") is not None]

        if not trades:
            return stats

        # Basic calculations
        wins = []
        losses = []
        by_symbol = defaultdict(lambda: {"wins": [], "losses": [], "trades": []})
        by_hour = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": Decimal("0")})
        by_dow = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": Decimal("0")})
        by_month = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": Decimal("0")})

        # Track streaks
        current_streak = 0
        max_win_streak = 0
        max_loss_streak = 0

        for trade in trades:
            pnl = Decimal(str(trade.get("pnl", 0)))
            symbol = trade.get("symbol", "UNKNOWN")
            timestamp = trade.get("timestamp")

            if isinstance(timestamp, str):
                try:
                    ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                except ValueError:
                    ts = datetime.now()
            else:
                ts = timestamp or datetime.now()

            # Track wins/losses
            if pnl >= 0:
                wins.append(pnl)
                by_symbol[symbol]["wins"].append(pnl)
                by_hour[ts.hour]["wins"] += 1
                by_dow[ts.weekday()]["wins"] += 1
                by_month[ts.month]["wins"] += 1

                if current_streak >= 0:
                    current_streak += 1
                else:
                    current_streak = 1
                max_win_streak = max(max_win_streak, current_streak)
            else:
                losses.append(pnl)
                by_symbol[symbol]["losses"].append(pnl)
                by_hour[ts.hour]["losses"] += 1
                by_dow[ts.weekday()]["losses"] += 1
                by_month[ts.month]["losses"] += 1

                if current_streak <= 0:
                    current_streak -= 1
                else:
                    current_streak = -1
                max_loss_streak = max(max_loss_streak, abs(current_streak))

            by_symbol[symbol]["trades"].append(trade)
            by_hour[ts.hour]["pnl"] += pnl
            by_dow[ts.weekday()]["pnl"] += pnl
            by_month[ts.month]["pnl"] += pnl

        # Fill overall stats
        stats.total_trades = len(trades)
        stats.winning_trades = len(wins)
        stats.losing_trades = len(losses)
        stats.win_rate = len(wins) / len(trades) * 100 if trades else 0

        all_pnl = wins + losses
        stats.total_pnl = sum(all_pnl)
        stats.avg_pnl = stats.total_pnl / len(all_pnl) if all_pnl else Decimal("0")

        if wins:
            stats.avg_win = sum(wins) / len(wins)
            stats.best_trade = max(wins)

        if losses:
            stats.avg_loss = sum(losses) / len(losses)
            stats.worst_trade = min(losses)

        total_wins = sum(wins) if wins else Decimal("0")
        total_losses = abs(sum(losses)) if losses else Decimal("0")
        if total_losses > 0:
            stats.profit_factor = float(total_wins / total_losses)

        stats.current_streak = current_streak
        stats.max_win_streak = max_win_streak
        stats.max_loss_streak = max_loss_streak

        # By symbol
        stats.by_symbol = self._calculate_symbol_stats(by_symbol)

        # By time
        stats.by_hour = self._calculate_time_stats(by_hour, "hour",
            {i: f"{i:02d}:00" for i in range(24)})
        stats.by_day_of_week = self._calculate_time_stats(by_dow, "day_of_week",
            {i: self.DAY_NAMES[i] for i in range(7)})
        stats.by_month = self._calculate_time_stats(by_month, "month",
            {i: self.MONTH_NAMES[i-1] for i in range(1, 13)})

        # Recent performance
        stats.last_7_days_win_rate = self._calculate_recent_win_rate(trades, 7)
        stats.last_30_days_win_rate = self._calculate_recent_win_rate(trades, 30)

        return stats

    def _calculate_symbol_stats(self, by_symbol: dict) -> list[SymbolStats]:
        """Calculate per-symbol statistics"""
        results = []

        for symbol, data in by_symbol.items():
            wins = data["wins"]
            losses = data["losses"]
            all_trades = wins + losses

            stat = SymbolStats(symbol=symbol)
            stat.total_trades = len(all_trades)
            stat.winning_trades = len(wins)
            stat.losing_trades = len(losses)
            stat.win_rate = len(wins) / len(all_trades) * 100 if all_trades else 0
            stat.total_pnl = sum(all_trades)
            stat.avg_pnl = stat.total_pnl / len(all_trades) if all_trades else Decimal("0")

            if wins:
                stat.avg_win = sum(wins) / len(wins)
                stat.best_trade = max(wins)

            if losses:
                stat.avg_loss = sum(losses) / len(losses)
                stat.worst_trade = min(losses)

            total_wins = sum(wins) if wins else Decimal("0")
            total_losses = abs(sum(losses)) if losses else Decimal("0")
            if total_losses > 0:
                stat.profit_factor = float(total_wins / total_losses)

            results.append(stat)

        # Sort by total trades descending
        results.sort(key=lambda x: x.total_trades, reverse=True)
        return results

    def _calculate_time_stats(
        self, data: dict, period: str, labels: dict
    ) -> list[TimeStats]:
        """Calculate time-based statistics"""
        results = []

        for key, values in data.items():
            total = values["wins"] + values["losses"]
            if total == 0:
                continue

            stat = TimeStats(
                period=period,
                label=labels.get(key, str(key)),
                total_trades=total,
                winning_trades=values["wins"],
                win_rate=values["wins"] / total * 100,
                total_pnl=values["pnl"],
            )
            results.append(stat)

        return results

    def _calculate_recent_win_rate(self, trades: list, days: int) -> float:
        """Calculate win rate for recent N days"""
        cutoff = datetime.now() - timedelta(days=days)

        recent_trades = []
        for trade in trades:
            timestamp = trade.get("timestamp")
            if isinstance(timestamp, str):
                try:
                    ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                except ValueError:
                    continue
            else:
                ts = timestamp

            if ts and ts.replace(tzinfo=None) >= cutoff:
                recent_trades.append(trade)

        if not recent_trades:
            return 0.0

        wins = sum(1 for t in recent_trades if Decimal(str(t.get("pnl", 0))) >= 0)
        return wins / len(recent_trades) * 100

    def get_best_performing_symbols(self, top_n: int = 5) -> list[SymbolStats]:
        """Get top N best performing symbols by P&L"""
        stats = self.calculate()
        sorted_symbols = sorted(
            stats.by_symbol,
            key=lambda x: float(x.total_pnl),
            reverse=True
        )
        return sorted_symbols[:top_n]

    def get_worst_performing_symbols(self, top_n: int = 5) -> list[SymbolStats]:
        """Get top N worst performing symbols by P&L"""
        stats = self.calculate()
        sorted_symbols = sorted(
            stats.by_symbol,
            key=lambda x: float(x.total_pnl)
        )
        return sorted_symbols[:top_n]
