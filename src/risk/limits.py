from dataclasses import dataclass


@dataclass
class RiskLimits:
    """Risk limit configuration.

    Money limits are expressed as a FRACTION of current account equity
    (account_value, in the account's currency — USD for this US-only
    bot), not absolute amounts. This keeps the limits currency-correct
    and auto-scaling; an absolute KRW figure compared against a USD
    order value silently disables the limit.
    """

    # Daily limits
    max_daily_loss_pct: float = 0.03      # 3% of equity
    max_daily_trades: int = 50

    # Position limits
    max_position_pct: float = 0.25        # 25% of equity per symbol
    max_position_quantity: int = 10000
    max_total_exposure_pct: float = 1.0   # 100% of equity

    # Drawdown (not enforced in validate_order; kept for reporting)
    max_drawdown_pct: float = 0.15        # 15% of equity

    # Position sizing
    risk_per_trade: float = 0.02          # 2%

    @classmethod
    def from_config(cls, config: dict) -> "RiskLimits":
        """Create from config dict (fraction-based keys)."""
        return cls(
            max_daily_loss_pct=float(config.get("max_daily_loss_pct", 0.03)),
            max_daily_trades=config.get("max_daily_trades", 50),
            max_position_pct=float(config.get("max_position_pct", 0.25)),
            max_position_quantity=config.get("max_position_quantity", 10000),
            max_total_exposure_pct=float(
                config.get("max_total_exposure_pct", 1.0)
            ),
            max_drawdown_pct=float(config.get("max_drawdown_pct", 0.15)),
            risk_per_trade=config.get("position_sizing", {}).get(
                "risk_per_trade", 0.02
            ),
        )
