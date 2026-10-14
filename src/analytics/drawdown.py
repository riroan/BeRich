"""Drawdown analysis - MDD tracking and alerts"""

from datetime import datetime
from decimal import Decimal
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


def equity_usd_value(point: dict) -> float:
    """The USD value an equity_history point actually represents.

    Prefers ``adjusted_total_usd`` (execution-basis, settlement-adjusted)
    and falls back to ``total_usd`` when it's absent. Shared by
    DrawdownAnalyzer and the web dashboard's TWR/return calculations so
    both read the same number for the same point.
    """
    value = point.get("adjusted_total_usd")
    if value is None:
        value = point.get("total_usd", 0)
    return value or 0


@dataclass
class DrawdownPoint:
    """Single drawdown measurement"""
    timestamp: datetime
    equity: Decimal
    peak: Decimal
    drawdown: Decimal  # Absolute value
    drawdown_pct: float  # Percentage


@dataclass
class DrawdownAnalysis:
    """Complete drawdown analysis"""
    # Current state
    current_equity: Decimal = Decimal("0")
    peak_equity: Decimal = Decimal("0")
    current_drawdown: Decimal = Decimal("0")
    current_drawdown_pct: float = 0.0

    # Maximum drawdown
    mdd: Decimal = Decimal("0")
    mdd_pct: float = 0.0
    mdd_start: datetime | None = None
    mdd_bottom: datetime | None = None
    mdd_recovered: datetime | None = None

    # Statistics
    avg_drawdown_pct: float = 0.0
    max_drawdown_duration_days: int = 0
    current_drawdown_duration_days: int = 0

    # History for charting
    drawdown_history: list = None

    # Alert thresholds
    alert_triggered: bool = False
    alert_level: str = ""  # warning, danger, critical

    def __post_init__(self):
        if self.drawdown_history is None:
            self.drawdown_history = []


class DrawdownAnalyzer:
    """Analyzes drawdown from equity history"""

    # Alert thresholds
    WARNING_THRESHOLD = 5.0   # 5% drawdown
    DANGER_THRESHOLD = 10.0   # 10% drawdown
    CRITICAL_THRESHOLD = 15.0  # 15% drawdown

    def __init__(self, equity_history: list):
        self.equity_history = equity_history

    @staticmethod
    def _equity_value(point: dict, currency: str = "usd"):
        if currency == "usd":
            return equity_usd_value(point)
        return point.get(f"total_{currency}", 0)

    def analyze(self, currency: str = "usd") -> DrawdownAnalysis:
        """Perform complete drawdown analysis.

        When USD points carry a ``twr_index`` (see the web layer's
        ``flow_adjusted_series``), drawdown percentages and durations are
        measured on that deposit-neutral index, so a withdrawal is not a
        drawdown and a deposit is not a new peak. Dollar figures are then
        expressed in the account's current capital: the amount the equity
        would have to gain to get back to the index peak.
        """
        analysis = DrawdownAnalysis()

        if not self.equity_history:
            return analysis

        peak = Decimal("0")  # peak of the drawdown basis
        peak_time = None
        mdd = Decimal("0")
        mdd_pct = 0.0
        mdd_start = None
        mdd_bottom_time = None
        mdd_recovered = None

        drawdown_points = []
        drawdown_durations = []
        in_drawdown = False

        for point in self.equity_history:
            timestamp = point.get("timestamp")
            if isinstance(timestamp, str):
                try:
                    ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                except ValueError:
                    continue
            else:
                ts = timestamp

            equity = Decimal(str(self._equity_value(point, currency) or 0))
            if equity <= 0:
                continue
            basis = equity
            if currency == "usd" and point.get("twr_index"):
                basis = Decimal(str(point["twr_index"]))

            # Reaching the old peak again (not only exceeding it) ends the
            # drawdown episode, which runs from the peak to the recovery.
            if basis >= peak:
                if in_drawdown and peak_time is not None:
                    drawdown_durations.append((ts - peak_time).days)
                    if mdd_start == peak_time and mdd_recovered is None:
                        mdd_recovered = ts
                    in_drawdown = False
                peak = basis
                peak_time = ts

            drawdown_pct = float((peak - basis) / peak * 100)
            # Dollars needed to climb back to the peak
            if basis is equity:
                drawdown = peak - equity
            else:
                drawdown = equity * (peak / basis - 1)
            if drawdown_pct > 0:
                in_drawdown = True

            # MDD is the deepest drawdown in percent, not the largest dollar
            # drop, which favors whenever the account was biggest
            if drawdown_pct > mdd_pct:
                mdd = drawdown
                mdd_pct = drawdown_pct
                mdd_start = peak_time
                mdd_bottom_time = ts
                mdd_recovered = None

            drawdown_points.append(DrawdownPoint(
                timestamp=ts,
                equity=equity,
                peak=equity + drawdown,
                drawdown=drawdown,
                drawdown_pct=drawdown_pct,
            ))

        if not drawdown_points:
            return analysis

        # Fill analysis
        latest = drawdown_points[-1]
        analysis.current_equity = latest.equity
        analysis.peak_equity = latest.peak
        analysis.current_drawdown = latest.drawdown
        analysis.current_drawdown_pct = latest.drawdown_pct

        analysis.mdd = mdd
        analysis.mdd_pct = mdd_pct
        analysis.mdd_start = mdd_start
        analysis.mdd_bottom = mdd_bottom_time
        analysis.mdd_recovered = mdd_recovered

        # Calculate average drawdown
        dd_values = [p.drawdown_pct for p in drawdown_points if p.drawdown_pct > 0]
        if dd_values:
            analysis.avg_drawdown_pct = sum(dd_values) / len(dd_values)

        # Current drawdown duration, counted from the peak
        if in_drawdown and peak_time is not None:
            now = datetime.now(peak_time.tzinfo)
            analysis.current_drawdown_duration_days = (now - peak_time).days
            drawdown_durations.append(analysis.current_drawdown_duration_days)

        # Max drawdown duration, the ongoing episode included
        if drawdown_durations:
            analysis.max_drawdown_duration_days = max(drawdown_durations)

        # Convert to serializable format for history
        analysis.drawdown_history = [
            {
                "timestamp": p.timestamp.isoformat(),
                "equity": float(p.equity),
                "peak": float(p.peak),
                "drawdown": float(p.drawdown),
                "drawdown_pct": p.drawdown_pct,
            }
            for p in drawdown_points
        ]

        # Check alert level
        analysis.alert_triggered, analysis.alert_level = self._check_alert(
            analysis.current_drawdown_pct
        )

        return analysis

    def _check_alert(self, drawdown_pct: float) -> tuple[bool, str]:
        """Check if drawdown triggers an alert"""
        if drawdown_pct >= self.CRITICAL_THRESHOLD:
            return True, "critical"
        elif drawdown_pct >= self.DANGER_THRESHOLD:
            return True, "danger"
        elif drawdown_pct >= self.WARNING_THRESHOLD:
            return True, "warning"
        return False, ""

    def get_recovery_estimate(self, analysis: DrawdownAnalysis) -> dict | None:
        """Estimate time to recovery based on historical data"""
        if analysis.current_drawdown_pct <= 0:
            return None

        # Simple estimate based on average daily return
        if len(self.equity_history) < 2:
            return None

        # Calculate average daily return
        returns = []
        prev_equity = None

        for point in self.equity_history:
            equity = (
                self._equity_value(point, "usd")
                or point.get("total_krw", 0)
            )
            if prev_equity and prev_equity > 0:
                daily_return = (equity - prev_equity) / prev_equity
                returns.append(daily_return)
            prev_equity = equity

        if not returns:
            return None

        avg_daily_return = sum(returns) / len(returns)

        if avg_daily_return <= 0:
            return {"days": None, "probability": "low", "message": "Recovery uncertain with current performance"}

        # Required return to recover
        required_return = analysis.current_drawdown_pct / 100

        # Estimate days (simplified compound calculation)
        import math
        try:
            days_to_recover = math.log(1 + required_return) / math.log(1 + avg_daily_return)
            return {
                "days": int(days_to_recover),
                "probability": "medium" if days_to_recover < 30 else "low",
                "avg_daily_return": avg_daily_return * 100,
            }
        except (ValueError, ZeroDivisionError):
            return None
