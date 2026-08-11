"""Shared RSI trading rules — single source of truth for live + backtest.

The live strategy (RSIMeanReversionStrategy) and the backtest simulator
(scripts/backtest_rsi.py) must trade by identical rules. The RSI formula and
the stage-ladder decisions live ONLY here; both callers import from this
module so the two paths cannot drift apart again.

Execution-environment concerns stay with the callers: how cooldown readiness
is measured (wall clock vs bar dates), fills, sizing, and state tracking.
"""

import operator
from typing import Callable

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


def _resolve_stage(
    value: float,
    current_stage: int,
    levels: list,
    repeat_ready: bool,
    fires: Callable[[float, float], bool],
) -> tuple[int | None, float | None]:
    """Shared ladder logic behind resolve_buy_stage and resolve_sell_stage.

    Progress to the next stage immediately when fires(value, threshold) is
    true for the next level. Cooldown (repeat_ready) resets the ladder:
    whatever stage it reached, the next fire starts over at stage 1.

    levels: [(threshold, portion), ...]. fires: operator.le for a
    descending ladder (buy: fires on value <= threshold), operator.ge for
    an ascending ladder (sell: fires on value >= threshold).
    Returns (stage_idx or None, next actionable threshold or None).
    """
    stage_idx = None
    next_threshold = (
        levels[current_stage][0] if current_stage < len(levels) else None
    )

    if current_stage < len(levels) and fires(value, levels[current_stage][0]):
        stage_idx = current_stage
    elif current_stage > 0 and repeat_ready:
        # Cooldown resets the ladder back to stage 1.
        next_threshold = levels[0][0]
        if fires(value, next_threshold):
            stage_idx = 0

    return stage_idx, next_threshold


def resolve_buy_stage(
    current_rsi: float,
    current_stage: int,
    levels: list,
    repeat_ready: bool,
) -> tuple[int | None, float | None]:
    """Decide which buy stage fires, if any.

    Progress to the next stage immediately when its threshold is hit.
    Cooldown (repeat_ready) resets the ladder: whatever stage it reached,
    the next buy starts over at stage 1.

    levels: [(rsi_threshold, portion), ...] — buys fire on RSI <= threshold.
    Returns (stage_idx or None, next actionable threshold or None).
    """
    return _resolve_stage(
        current_rsi, current_stage, levels, repeat_ready, operator.le,
    )


def resolve_take_profit_stage(
    pnl_pct: float,
    current_stage: int,
    levels: list,
) -> tuple[int | None, float | None]:
    """Profit ladder on PnL% instead of RSI: fires on pnl >= threshold.

    Same ascending shape as the RSI sell ladder, so it reuses it. Repetition
    is off on purpose: PnL does not oscillate the way RSI does, so a price
    parked above a threshold would otherwise drain the position one cooldown
    at a time. Stages advance only by reaching a deeper level.
    """
    return resolve_sell_stage(pnl_pct, current_stage, levels, False)


def resolve_stop_loss_stage(
    pnl_pct: float,
    current_stage: int,
    levels: list,
) -> tuple[int | None, float | None]:
    """Loss ladder on PnL%: fires on pnl <= threshold, descending.

    Same shape as the buy ladder (thresholds decrease as it deepens), no
    repetition, for the same reason as resolve_take_profit_stage.
    """
    return resolve_buy_stage(pnl_pct, current_stage, levels, False)


def as_levels(scalar, portion: float = 1.0) -> list | None:
    """Normalise a plain `stop_loss: -10` / `take_profit: 20` into a ladder.

    Keeps single-threshold configs working unchanged: one stage that exits
    the whole position.
    """
    if scalar is None:
        return None
    return [[float(scalar), portion]]


def resolve_sell_stage(
    current_rsi: float,
    current_stage: int,
    levels: list,
    repeat_ready: bool,
) -> tuple[int | None, float | None]:
    """Mirror of resolve_buy_stage for the sell ladder (RSI >= threshold)."""
    return _resolve_stage(
        current_rsi, current_stage, levels, repeat_ready, operator.ge,
    )
