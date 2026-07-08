"""Shared RSI trading rules — single source of truth for live + backtest.

The live strategy (RSIMeanReversionStrategy) and the backtest simulator
(scripts/backtest_rsi.py) must trade by identical rules. The RSI formula and
the stage-ladder decisions live ONLY here; both callers import from this
module so the two paths cannot drift apart again.

Execution-environment concerns stay with the callers: how cooldown readiness
is measured (wall clock vs bar dates), fills, sizing, and state tracking.
"""

import pandas as pd


def calculate_rsi(
    prices: pd.Series,
    period: int = 14,
    method: str = "wilder",
) -> pd.Series:
    """Calculate RSI.

    method: "wilder" (exponential smoothing, the industry standard) or
    "cutler" (simple moving average of gains/losses — jumpier, fires
    thresholds more often). Unknown values fall back to "wilder" so a
    typo in strategy params can never silently change live trading.
    """
    delta = prices.diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)

    if method == "cutler":
        # Cutler's RSI: simple moving average over the period window
        avg_gain = gain.rolling(window=period).mean()
        avg_loss = loss.rolling(window=period).mean()
    else:
        # Wilder's smoothing (EMA with alpha = 1/period)
        avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, 1e-10)  # Avoid division by zero
    rsi = 100 - (100 / (1 + rs))
    return rsi


def resolve_buy_stage(
    current_rsi: float,
    current_stage: int,
    levels: list,
    repeat_ready: bool,
) -> tuple[int | None, float | None]:
    """Decide which buy stage fires, if any.

    Progress to the next stage immediately when its threshold is hit.
    Cooldown (repeat_ready) repeats the current stage, except after the
    final stage where it restarts the ladder at stage 1.

    levels: [(rsi_threshold, portion), ...] — buys fire on RSI <= threshold.
    Returns (stage_idx or None, next actionable threshold or None).
    """
    stage_idx = None
    next_threshold = (
        levels[current_stage][0] if current_stage < len(levels) else None
    )

    if (
        current_stage < len(levels)
        and current_rsi <= levels[current_stage][0]
    ):
        stage_idx = current_stage
    elif current_stage > 0 and repeat_ready:
        repeat_idx = (
            0 if current_stage >= len(levels) else current_stage - 1
        )
        next_threshold = levels[repeat_idx][0]
        if current_rsi <= next_threshold:
            stage_idx = repeat_idx

    return stage_idx, next_threshold


def resolve_sell_stage(
    current_rsi: float,
    current_stage: int,
    levels: list,
    repeat_ready: bool,
) -> tuple[int | None, float | None]:
    """Mirror of resolve_buy_stage for the sell ladder (RSI >= threshold)."""
    stage_idx = None
    next_threshold = (
        levels[current_stage][0] if current_stage < len(levels) else None
    )

    if (
        current_stage < len(levels)
        and current_rsi >= levels[current_stage][0]
    ):
        stage_idx = current_stage
    elif current_stage > 0 and repeat_ready:
        repeat_idx = (
            0 if current_stage >= len(levels) else current_stage - 1
        )
        next_threshold = levels[repeat_idx][0]
        if current_rsi >= next_threshold:
            stage_idx = repeat_idx

    return stage_idx, next_threshold
