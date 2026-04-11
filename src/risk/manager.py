from decimal import Decimal
from datetime import date
import logging

from src.core.types import Order, Position, OrderSide
from .limits import RiskLimits

logger = logging.getLogger(__name__)


class RiskManager:
    """Risk management system"""

    def __init__(self, limits: RiskLimits, account_value: Decimal):
        self.limits = limits
        self.account_value = account_value
        self.available_cash = account_value  # 현금 잔고 (초기값은 계좌 가치)

        # State tracking
        self._positions: dict[str, Position] = {}
        self._daily_pnl: Decimal = Decimal("0")
        self._daily_trades: int = 0
        self._last_reset_date: date = date.today()

    def update_account_value(self, value: Decimal) -> None:
        """Update account value"""
        self.account_value = value

    def update_available_cash(self, cash: Decimal) -> None:
        """Update available cash"""
        self.available_cash = cash

    def update_positions(self, positions: list[Position]) -> None:
        """Update position state"""
        self._positions = {p.symbol: p for p in positions}

    def validate_order(self, order: Order) -> tuple[bool, str | None]:
        """Validate order against risk limits"""
        self._check_daily_reset()

        # 1. Daily loss limit
        if self._daily_pnl < -self.limits.max_daily_loss:
            return False, f"Daily loss limit exceeded: {self._daily_pnl}"

        # 2. Daily trade count
        if self._daily_trades >= self.limits.max_daily_trades:
            return False, f"Daily trade limit exceeded: {self._daily_trades}"

        # 3. Position value limit
        if order.price:
            position_value = order.quantity * order.price
            if position_value > self.limits.max_position_value:
                return False, f"Position value too large: {position_value}"

            # 4. Total exposure limit
            total_exposure = sum(
                abs(p.quantity * p.current_price) for p in self._positions.values()
            )
            if total_exposure + position_value > self.limits.max_total_exposure:
                return False, "Total exposure limit exceeded"

        # 5. Position quantity limit
        current_position = self._positions.get(order.symbol)
        if current_position:
            if order.side == OrderSide.BUY:
                new_quantity = current_position.quantity + order.quantity
            else:
                new_quantity = current_position.quantity - order.quantity
            if abs(new_quantity) > self.limits.max_position_quantity:
                return False, "Single position quantity limit exceeded"

        return True, None

    def calculate_position_size(
        self,
        symbol: str,
        price: Decimal,
        signal_strength: float,
    ) -> int:
        """Calculate position size based on available cash

        매수금액 = 현금잔고 × signal_strength
        매수수량 = 매수금액 / 주가
        """
        if price <= 0:
            return 0

        # 현금 잔고 기준으로 매수 금액 계산
        buy_amount = self.available_cash * Decimal(str(signal_strength))

        # 매수 수량 계산
        quantity = int(buy_amount / price)

        # Apply limits
        quantity = min(quantity, self.limits.max_position_quantity)

        # Check position value limit
        position_value = quantity * price
        if position_value > self.limits.max_position_value:
            quantity = int(self.limits.max_position_value / price)

        return max(0, quantity)

    def record_trade(self, pnl: Decimal) -> None:
        """Record a completed trade"""
        self._check_daily_reset()
        self._daily_pnl += pnl
        self._daily_trades += 1

    def _check_daily_reset(self) -> None:
        """Reset daily counters if new day"""
        today = date.today()
        if today != self._last_reset_date:
            logger.info(f"Daily reset: PnL={self._daily_pnl}, Trades={self._daily_trades}")
            self._daily_pnl = Decimal("0")
            self._daily_trades = 0
            self._last_reset_date = today

    def get_daily_stats(self) -> dict:
        """Get daily statistics"""
        return {
            "daily_pnl": self._daily_pnl,
            "daily_trades": self._daily_trades,
            "remaining_trades": self.limits.max_daily_trades - self._daily_trades,
            "remaining_loss_budget": self.limits.max_daily_loss + self._daily_pnl,
        }
