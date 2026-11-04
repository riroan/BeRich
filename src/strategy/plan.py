"""What a held position will do next, for display.

Reads the same ladders the RSI strategy trades on, so the dashboard can say
"RSI ≤ 25 buys stage 2, RSI ≥ 70 sells 30%, $83.20 stops out" instead of
showing bare stage counters.
"""

from __future__ import annotations

from src.strategy.rsi_rules import as_levels

# Mirrors the defaults in RSIMeanReversionStrategy.calculate_signal
_DEFAULT_BUY_LEVELS = [[30, 0.5], [25, 0.3], [20, 0.2]]
_DEFAULT_SELL_LEVELS = [[70, 0.3], [75, 0.4], [80, 0.5]]


def position_plan(
    params: dict,
    buy_stage: int,
    sell_stage: int,
    sl_stage: int,
    avg_price: float,
) -> dict:
    """Next rung of each ladder for a held position.

    A ladder that is spent reports None for its rung; it re-arms after the
    strategy's cooldown, which the stage-reset countdown already shows.
    """
    buy_levels = params.get("avg_down_levels") or _DEFAULT_BUY_LEVELS
    sell_levels = params.get("sell_levels") or _DEFAULT_SELL_LEVELS
    if "stop_loss_levels" in params:
        sl_levels = params["stop_loss_levels"] or []
    else:
        sl_levels = as_levels(params.get("stop_loss", -10)) or []

    plan: dict = {
        "next_buy_rsi": None,
        "next_buy_stage": None,
        "next_sell_rsi": None,
        "next_sell_portion": None,
        "stop_price": None,
        "stop_pct": None,
    }
    if 0 <= buy_stage < len(buy_levels):
        plan["next_buy_rsi"] = float(buy_levels[buy_stage][0])
        plan["next_buy_stage"] = buy_stage + 1
    if 0 <= sell_stage < len(sell_levels):
        plan["next_sell_rsi"] = float(sell_levels[sell_stage][0])
        plan["next_sell_portion"] = float(sell_levels[sell_stage][1])
    if avg_price > 0 and 0 <= sl_stage < len(sl_levels):
        pct = float(sl_levels[sl_stage][0])
        plan["stop_pct"] = pct
        plan["stop_price"] = avg_price * (1 + pct / 100)
    return plan
