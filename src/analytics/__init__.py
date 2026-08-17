"""Analytics module for trading performance analysis"""

from src.analytics.reports import ReportGenerator
from src.analytics.drawdown import DrawdownAnalyzer
from src.analytics.statistics import TradeStatistics
from src.analytics.tax import capital_gains_tax, capital_gains_tax_by_year

__all__ = [
    "ReportGenerator",
    "DrawdownAnalyzer",
    "TradeStatistics",
    "capital_gains_tax",
    "capital_gains_tax_by_year",
]
