"""One strategy can hold symbols from several markets.

Market used to belong to the strategy config, so a strategy was locked to one
exchange. It belongs to the symbol now; the config's market survives only as
the fallback for entries written before the change.
"""

from __future__ import annotations

import pytest

from src.bot._utils import extract_symbol_markets, extract_symbols
from src.core.types import Bar, Market, SignalType
from src.strategy.base import BaseStrategy
from src.strategy.engine import StrategyEngine


MIXED = {
    "name": "Mixed",
    "market": "nasdaq",
    "symbols": [
        {"symbol": "AAPL", "market": "nasdaq"},
        {"symbol": "005930", "market": "krx"},
        {"symbol": "KO", "market": "nyse", "enabled": False},
    ],
}


def test_each_symbol_keeps_its_own_market():
    assert extract_symbol_markets(MIXED) == {
        "AAPL": Market.NASDAQ,
        "005930": Market.KRX,
    }
    # The disabled symbol is excluded, same rule as extract_symbols.
    assert extract_symbols(MIXED["symbols"]) == ["AAPL", "005930"]


def test_entries_without_a_market_fall_back_to_the_config():
    # Everything written before this change looks like one of these two.
    legacy = {
        "name": "Legacy",
        "market": "amex",
        "symbols": ["SPY", {"symbol": "GLD", "max_weight": 20.0}],
    }

    assert extract_symbol_markets(legacy) == {
        "SPY": Market.AMEX,
        "GLD": Market.AMEX,
    }


class _Probe(BaseStrategy):
    """Minimal strategy that reports the market it was asked to trade."""

    @property
    def name(self) -> str:
        return "Probe"

    async def calculate_signal(self, symbol: str):
        from src.core.types import Signal

        return Signal(
            signal_type=SignalType.ENTRY_LONG,
            symbol=symbol,
            market=self.market_for(symbol),
            strength=1.0,
        )


def _probe() -> _Probe:
    return _Probe(
        symbols=["AAPL", "005930"],
        symbol_markets={"AAPL": Market.NASDAQ, "005930": Market.KRX},
        config_name="Mixed",
    )


def test_strategy_reports_every_market_it_touches():
    strategy = _probe()

    assert strategy.markets == {Market.NASDAQ, Market.KRX}
    assert strategy.market_for("AAPL") == Market.NASDAQ
    assert strategy.market_for("005930") == Market.KRX
    assert strategy.market_for("NVDA") is None


def test_market_shorthand_still_applies_one_market_to_all():
    # Backtests and tests construct strategies this way.
    strategy = _Probe(symbols=["AAPL", "GOOG"], market=Market.NASDAQ)

    assert strategy.symbol_markets == {
        "AAPL": Market.NASDAQ, "GOOG": Market.NASDAQ,
    }


def test_strategy_needs_a_market_from_somewhere():
    with pytest.raises(ValueError):
        _Probe(symbols=["AAPL"])


@pytest.mark.asyncio
async def test_signals_carry_the_symbol_market_not_one_strategy_market():
    strategy = _probe()

    krx = await strategy.calculate_signal("005930")
    nasdaq = await strategy.calculate_signal("AAPL")

    # Orders route to the broker by this field, so a wrong market here sends
    # a KRX order down the US path.
    assert krx.market == Market.KRX
    assert nasdaq.market == Market.NASDAQ


@pytest.mark.asyncio
async def test_engine_routes_a_bar_by_the_symbol_market():
    from datetime import datetime
    from decimal import Decimal
    from unittest.mock import MagicMock

    engine = StrategyEngine(event_bus=MagicMock(), broker=MagicMock())
    strategy = _probe()
    engine.register_strategy(strategy)

    seen = []
    engine._emit_signal = lambda signal, name: seen.append(signal)

    def _bar(symbol, market):
        return Bar(
            symbol=symbol, market=market, timestamp=datetime.now(),
            open=Decimal("1"), high=Decimal("1"), low=Decimal("1"),
            close=Decimal("1"), volume=1, timeframe="1d",
        )

    await engine._on_bar(MagicMock(data=_bar("005930", Market.KRX)))
    assert [s.symbol for s in seen] == ["005930"]

    # Right symbol, wrong market — must not reach the strategy.
    await engine._on_bar(MagicMock(data=_bar("005930", Market.NASDAQ)))
    assert [s.symbol for s in seen] == ["005930"]

    # A symbol this strategy does not hold is ignored too.
    await engine._on_bar(MagicMock(data=_bar("NVDA", Market.NASDAQ)))
    assert [s.symbol for s in seen] == ["005930"]


def test_config_name_is_the_reload_identity():
    # Hot reload matches running instances to configs by this. It used to be
    # rebuilt from market + class name, which broke on rename and cannot
    # work at all once a strategy spans markets.
    assert _probe().name_with_market == "Mixed"
