"""Seed strategy config for the backtest CLI scripts.

Single source of truth that replaced the former ``config/strategies.yaml``
dependency. Backtests run the AMEX RSI mean-reversion strategy (SPY, GLD)
with the same params the live bot uses for that strategy.
"""

BACKTEST_STRATEGIES = [
    {
        "name": "AMEX_RSI_MeanReversion",
        "enabled": True,
        "market": "amex",
        "symbols": ["SPY", "GLD"],
        "params": {
            "rsi_period": 14,
            "stop_loss": -10,
            "cooldown_days": 3,
            "avg_down_levels": [[35, 0.3], [30, 0.35], [25, 0.35]],
            "sell_levels": [[70, 0.25], [75, 0.35], [80, 0.4]],
        },
    },
]
