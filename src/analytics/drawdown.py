"""Drawdown analysis - MDD tracking and alerts"""

from datetime import datetime
from decimal import Decimal
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


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

    def analyze(self, currency: str = "usd") -> DrawdownAnalysis:
        """Perform complete drawdown analysis"""
        analysis = DrawdownAnalysis()

        if not self.equity_history:
            return analysis

        key = f"total_{currency}"

        # Track peak and drawdowns
        peak = Decimal("0")
        peak_time = None
        mdd = Decimal("0")
        mdd_pct = 0.0
        mdd_start = None
        mdd_bottom_time = None

        drawdown_points = []
        drawdown_durations = []
        current_dd_start = None

        for point in self.equity_history:
            timestamp = point.get("timestamp")
            if isinstance(timestamp, str):
                try:
                    ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                except ValueError:
                    continue
            else:
                ts = timestamp

            equity = Decimal(str(point.get(key, 0) or 0))
            if equity <= 0:
                continue

            # Update peak
            if equity > peak:
                # If we were in drawdown, record duration
                if current_dd_start and peak > 0:
                    duration = (ts - current_dd_start).days
                    drawdown_durations.append(duration)

                peak = equity
                peak_time = ts
                current_dd_start = None

            # Calculate drawdown
            drawdown = peak - equity
            drawdown_pct = float(drawdown / peak * 100) if peak > 0 else 0.0

            # Track MDD
            if drawdown > mdd:
                mdd = drawdown
                mdd_pct = drawdown_pct
                mdd_start = peak_time
                mdd_bottom_time = ts

            # Track current drawdown start
            if drawdown > 0 and current_dd_start is None:
                current_dd_start = ts

            drawdown_points.append(DrawdownPoint(
                timestamp=ts,
                equity=equity,
                peak=peak,
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

        # Calculate average drawdown
        dd_values = [p.drawdown_pct for p in drawdown_points if p.drawdown_pct > 0]
        if dd_values:
            analysis.avg_drawdown_pct = sum(dd_values) / len(dd_values)

        # Max drawdown duration
        if drawdown_durations:
            analysis.max_drawdown_duration_days = max(drawdown_durations)

        # Current drawdown duration
        if current_dd_start:
            analysis.current_drawdown_duration_days = (datetime.now() - current_dd_start).days

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
            equity = point.get("total_usd", 0) or point.get("total_krw", 0)
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
