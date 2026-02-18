from dataclasses import dataclass
from decimal import Decimal


@dataclass
class RiskLimits:
    """Risk limit configuration"""

    # Daily limits
    max_daily_loss: Decimal = Decimal("1000000")
    max_daily_trades: int = 50

    # Position limits
    max_position_value: Decimal = Decimal("10000000")
    max_position_quantity: int = 10000
    max_total_exposure: Decimal = Decimal("100000000")

    # Concentration
    max_concentration: float = 0.25

    # Drawdown
    max_drawdown: Decimal = Decimal("5000000")

    # Position sizing
    risk_per_trade: float = 0.05  # 5%

    @classmethod
    def from_config(cls, config: dict) -> "RiskLimits":
        """Create from config dict"""
        return cls(
            max_daily_loss=Decimal(str(config.get("max_daily_loss", 1000000))),
            max_daily_trades=config.get("max_daily_trades", 50),
            max_position_value=Decimal(str(config.get("max_position_value", 10000000))),
            max_position_quantity=config.get("max_position_quantity", 10000),
            max_total_exposure=Decimal(str(config.get("max_total_exposure", 100000000))),
            max_concentration=config.get("max_concentration", 0.25),
            max_drawdown=Decimal(str(config.get("max_drawdown", 5000000))),
            risk_per_trade=config.get("position_sizing", {}).get("risk_per_trade", 0.05),
        )
