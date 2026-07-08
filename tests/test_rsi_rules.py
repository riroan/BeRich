"""Shared RSI rules — single source of truth for live strategy + backtest."""

from __future__ import annotations

import os

import numpy as np
import pandas as pd

os.environ.setdefault("DASHBOARD_PASSWORD", "test")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import scripts.backtest_rsi as backtest_rsi  # noqa: E402
from src.core.types import Market  # noqa: E402
from src.strategy.builtin.rsi_mean_reversion import (  # noqa: E402
    RSIMeanReversionStrategy,
)
from src.strategy.rsi_rules import (  # noqa: E402
    calculate_rsi,
    resolve_buy_stage,
    resolve_sell_stage,
)

BUY_LEVELS = [(35, 0.3), (30, 0.35), (25, 0.35)]
SELL_LEVELS = [(65, 0.3), (70, 0.4), (75, 0.5)]


class TestSingleSourceOfTruth:
    def test_backtest_imports_shared_rsi(self):
        # Re-defining calculate_rsi in the backtest module reintroduces
        # the live-vs-backtest formula drift this module exists to kill.
        assert backtest_rsi.calculate_rsi is calculate_rsi

    def test_live_strategy_delegates_to_shared_rsi(self):
        strategy = RSIMeanReversionStrategy(
            symbols=["X"], market=Market.NASDAQ, params={},
        )
        rng = np.random.default_rng(42)
        prices = pd.Series(100 + rng.normal(0, 2, 60).cumsum())
        pd.testing.assert_series_equal(
            strategy._calculate_rsi(prices, period=14),
            calculate_rsi(prices, period=14),
            check_names=False,
        )


class TestResolveBuyStage:
    def test_stage1_fires_at_threshold(self):
        idx, thr = resolve_buy_stage(34.0, 0, BUY_LEVELS, False)
        assert idx == 0
        assert thr == 35

    def test_no_fire_above_threshold(self):
        idx, thr = resolve_buy_stage(36.0, 0, BUY_LEVELS, False)
        assert idx is None
        assert thr == 35

    def test_progresses_to_next_stage_immediately(self):
        idx, _ = resolve_buy_stage(29.0, 1, BUY_LEVELS, False)
        assert idx == 1

    def test_stage1_threshold_consumed_mid_ladder(self):
        # NASA case: stage 2/3 with RSI 32.4 — stage-1's 35 must NOT
        # re-fire; the actionable threshold is the stage-2 repeat (30).
        idx, thr = resolve_buy_stage(32.4, 2, BUY_LEVELS, True)
        assert idx is None
        assert thr == 30

    def test_cooldown_repeats_current_stage(self):
        idx, thr = resolve_buy_stage(29.0, 2, BUY_LEVELS, True)
        assert idx == 1
        assert thr == 30

    def test_repeat_requires_cooldown(self):
        idx, _ = resolve_buy_stage(29.0, 2, BUY_LEVELS, False)
        assert idx is None

    def test_full_ladder_restarts_at_stage1_after_cooldown(self):
        idx, thr = resolve_buy_stage(34.0, 3, BUY_LEVELS, True)
        assert idx == 0
        assert thr == 35

    def test_full_ladder_waits_for_cooldown(self):
        idx, thr = resolve_buy_stage(20.0, 3, BUY_LEVELS, False)
        assert idx is None
        assert thr is None


class TestResolveSellStage:
    def test_stage1_fires_at_threshold(self):
        idx, thr = resolve_sell_stage(66.0, 0, SELL_LEVELS, False)
        assert idx == 0
        assert thr == 65

    def test_no_fire_below_threshold(self):
        idx, _ = resolve_sell_stage(64.0, 0, SELL_LEVELS, False)
        assert idx is None

    def test_progresses_to_next_stage_immediately(self):
        idx, _ = resolve_sell_stage(71.0, 1, SELL_LEVELS, False)
        assert idx == 1

    def test_cooldown_repeats_current_stage(self):
        idx, thr = resolve_sell_stage(71.0, 2, SELL_LEVELS, True)
        assert idx == 1
        assert thr == 70

    def test_repeat_requires_reaching_repeat_threshold(self):
        idx, thr = resolve_sell_stage(66.0, 2, SELL_LEVELS, True)
        assert idx is None
        assert thr == 70

    def test_full_ladder_restarts_at_stage1_after_cooldown(self):
        idx, thr = resolve_sell_stage(66.0, 3, SELL_LEVELS, True)
        assert idx == 0
        assert thr == 65
