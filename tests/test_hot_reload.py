"""Tests for strategy hot reload — the web thread must not trade blind."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bot.core import TradingBot
from src.web import app as web_app


def _bot_with_running_strategy(old_strategy, cfg):
    """A TradingBot stubbed down to just what reload_strategies touches."""
    bot = TradingBot.__new__(TradingBot)
    bot.storage = SimpleNamespace(
        get_all_strategy_configs=AsyncMock(return_value=[cfg])
    )
    bot.strategy_engine = SimpleNamespace(
        _strategies=[old_strategy],
        get_strategies=lambda: [old_strategy],
    )
    bot.broker = SimpleNamespace(get_historical_bars=AsyncMock(return_value=[]))
    bot.dashboard = SimpleNamespace(
        strategy_names=[], strategy_instances=[],
    )
    bot.restore_strategy_state_from_positions = MagicMock()
    return bot


def _cfg(name="Normal_RSI"):
    return {
        "name": name,
        "enabled": True,
        "symbols": ["AAPL"],
        "params": {"rsi_period": 14},
    }


def _running_strategy(name="Normal_RSI"):
    return SimpleNamespace(
        name=name,
        config_name=name,
        symbols=["AAPL"],
        # Differs from the config, so reload builds a fresh instance.
        params={"rsi_period": 21},
        symbol_markets={"AAPL": "NASDAQ"},
        market_for=lambda s: "NASDAQ",
        history_window=100,
    )


def test_failed_init_keeps_the_running_strategy():
    old = _running_strategy()
    cfg = _cfg()
    bot = _bot_with_running_strategy(old, cfg)

    broken = _running_strategy()
    broken.initialize = MagicMock(side_effect=RuntimeError("loop mismatch"))

    with patch("src.bot.core.build_strategy", return_value=broken), \
         patch("src.bot.core.extract_symbols", return_value=["AAPL"]), \
         patch(
             "src.bot.core.extract_symbol_markets",
             return_value={"AAPL": "NASDAQ"},
         ):
        asyncio.run(bot.reload_strategies())

    # The half-built instance has no daily bars, so it could never produce a
    # signal. The engine must keep the one that still works.
    assert bot.strategy_engine._strategies == [old]
    assert broken not in bot.strategy_engine._strategies
    # The name stays listed exactly once — the dashboard still shows it active.
    assert bot.dashboard.strategy_names == ["Normal_RSI"]


def test_successful_init_swaps_in_the_new_strategy():
    old = _running_strategy()
    cfg = _cfg()
    bot = _bot_with_running_strategy(old, cfg)

    fresh = _running_strategy()
    fresh.initialize = MagicMock()

    with patch("src.bot.core.build_strategy", return_value=fresh), \
         patch("src.bot.core.extract_symbols", return_value=["AAPL"]), \
         patch(
             "src.bot.core.extract_symbol_markets",
             return_value={"AAPL": "NASDAQ"},
         ):
        asyncio.run(bot.reload_strategies())

    assert bot.strategy_engine._strategies == [fresh]


def test_reload_is_dispatched_to_the_bot_loop_not_the_web_loop():
    """The broker's session is bound to the bot loop; driving the reload from
    the web thread's loop is what silently emptied every RSI base."""
    ran_on = {}
    bot_loop = asyncio.new_event_loop()

    async def cb():
        ran_on["loop"] = asyncio.get_running_loop()

    original_cb = web_app.dashboard_state.reload_callback
    original_loop = web_app.dashboard_state.bot_loop
    web_app.dashboard_state.reload_callback = cb
    web_app.dashboard_state.bot_loop = bot_loop
    try:
        # Called from a thread that is NOT running bot_loop, exactly like the
        # web handler.
        assert web_app._trigger_bot_reload() is True
        bot_loop.call_later(0.05, bot_loop.stop)
        bot_loop.run_forever()
    finally:
        web_app.dashboard_state.reload_callback = original_cb
        web_app.dashboard_state.bot_loop = original_loop
        bot_loop.close()

    assert ran_on["loop"] is bot_loop


def test_trigger_reports_the_bot_unreachable_when_it_never_registered():
    original_cb = web_app.dashboard_state.reload_callback
    original_loop = web_app.dashboard_state.bot_loop
    web_app.dashboard_state.reload_callback = None
    web_app.dashboard_state.bot_loop = None
    try:
        assert web_app._trigger_bot_reload() is False
    finally:
        web_app.dashboard_state.reload_callback = original_cb
        web_app.dashboard_state.bot_loop = original_loop
