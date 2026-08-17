#!/usr/bin/env python3
"""Backtestable strategies, by API key.

The live side discovers strategies through BaseStrategy.__subclasses__();
here the list is explicit, because the API key is part of the request
contract and a typo should fail at import, not at request time. Adding a
strategy is one import and one entry.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.backtest_engine import BacktestStrategy
from scripts.backtest_ha import HeikinAshiFlipBacktest
from scripts.backtest_rsi import RSIMeanReversionBacktest
from scripts.backtest_rsi_ha import RSIHeikinAshiBacktest

BACKTESTS: dict[str, type[BacktestStrategy]] = {
    RSIMeanReversionBacktest.key: RSIMeanReversionBacktest,
    HeikinAshiFlipBacktest.key: HeikinAshiFlipBacktest,
    RSIHeikinAshiBacktest.key: RSIHeikinAshiBacktest,
}


def backtest_class(key: str) -> type[BacktestStrategy]:
    """Look up the backtest strategy registered under an API key."""
    try:
        return BACKTESTS[key]
    except KeyError:
        raise ValueError(
            f"unknown backtest strategy '{key}'. "
            f"Available: {sorted(BACKTESTS)}"
        ) from None


def build_backtest(key: str, params: dict) -> BacktestStrategy:
    """Instantiate the backtest strategy for an API key."""
    return backtest_class(key)(params)


def build_from_request(body) -> BacktestStrategy:
    """Instantiate from an API request, letting the strategy pick its own
    params — a strategy with nothing to tune reads nothing."""
    cls = backtest_class(body.strategy)
    return cls(cls.params_from_request(body))
