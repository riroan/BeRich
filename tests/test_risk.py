"""Tests for RiskManager"""

import pytest
from decimal import Decimal
from datetime import date
from unittest.mock import patch

from src.risk.manager import RiskManager
from src.risk.limits import RiskLimits
from src.core.types import Order, Position, Market, OrderSide, OrderType


class TestRiskManagerInit:
    """Test cases for RiskManager initialization"""

    def test_default_initialization(self):
        """Test initialization with default limits"""
        limits = RiskLimits()
        manager = RiskManager(limits=limits, account_value=Decimal("10000000"))

        assert manager.limits is limits
        assert manager.account_value == Decimal("10000000")
        assert manager.available_cash == Decimal("10000000")
        assert manager._daily_pnl == Decimal("0")
        assert manager._daily_trades == 0
        assert manager._positions == {}

    def test_custom_limits(self):
        """Test initialization with custom limits"""
        limits = RiskLimits(
            max_daily_loss=Decimal("500000"),
            max_daily_trades=20,
            max_position_value=Decimal("5000000"),
        )
        manager = RiskManager(limits=limits, account_value=Decimal("50000000"))

        assert manager.limits.max_daily_loss == Decimal("500000")
        assert manager.limits.max_daily_trades == 20
        assert manager.limits.max_position_value == Decimal("5000000")

    def test_zero_account_value(self):
        """Test initialization with zero account value"""
        limits = RiskLimits()
        manager = RiskManager(limits=limits, account_value=Decimal("0"))

        assert manager.account_value == Decimal("0")
        assert manager.available_cash == Decimal("0")


class TestValidateOrder:
    """Test cases for validate_order"""

    @pytest.fixture
    def manager(self):
        """Create a RiskManager with tight limits for testing"""
        limits = RiskLimits(
            max_daily_loss=Decimal("100000"),
            max_daily_trades=5,
            max_position_value=Decimal("1000000"),
            max_position_quantity=100,
            max_total_exposure=Decimal("5000000"),
        )
        return RiskManager(limits=limits, account_value=Decimal("10000000"))

    def _make_order(self, symbol="AAPL", side=OrderSide.BUY, quantity=10,
                    price=Decimal("100"), market=Market.NASDAQ):
        return Order(
            symbol=symbol,
            market=market,
            side=side,
            order_type=OrderType.LIMIT,
            quantity=quantity,
            price=price,
        )

    def test_valid_order(self, manager):
        """Test a valid order passes validation"""
        order = self._make_order(quantity=10, price=Decimal("100"))
        valid, reason = manager.validate_order(order)

        assert valid is True
        assert reason is None

    def test_max_position_value_exceeded(self, manager):
        """Test order rejected when position value exceeds limit"""
        # 10001 * 100 = 1000100 > max_position_value of 1000000
        order = self._make_order(quantity=10001, price=Decimal("100"))
        valid, reason = manager.validate_order(order)

        assert valid is False
        assert "Position value too large" in reason

    def test_max_position_value_at_boundary(self, manager):
        """Test order at exactly the position value limit"""
        # 10000 * 100 = 1000000 == max_position_value
        order = self._make_order(quantity=10000, price=Decimal("100"))
        valid, reason = manager.validate_order(order)

        assert valid is True

    def test_total_exposure_exceeded(self, manager):
        """Test order rejected when total exposure exceeds limit"""
        # Existing exposure = 45 * 100000 = 4,500,000
        positions = [
            Position(
                symbol="MSFT", market=Market.NASDAQ,
                quantity=45,
                avg_entry_price=Decimal("100"),
                current_price=Decimal("100000"),
                unrealized_pnl=Decimal("0"),
            ),
        ]
        manager.update_positions(positions)

        # Order value = 60 * 10000 = 600,000 (under position limit)
        # Total = 4,500,000 + 600,000 = 5,100,000 > 5,000,000
        order = self._make_order(
            symbol="AAPL", quantity=60, price=Decimal("10000"),
        )
        valid, reason = manager.validate_order(order)

        assert valid is False
        assert "Total exposure limit exceeded" in reason

    def test_position_quantity_limit_buy(self, manager):
        """Test buy order rejected when position quantity limit exceeded"""
        positions = [
            Position(
                symbol="AAPL", market=Market.NASDAQ, quantity=95,
                avg_entry_price=Decimal("100"), current_price=Decimal("100"),
                unrealized_pnl=Decimal("0"),
            ),
        ]
        manager.update_positions(positions)

        # 95 + 10 = 105 > max_position_quantity of 100
        order = self._make_order(quantity=10, price=Decimal("100"))
        valid, reason = manager.validate_order(order)

        assert valid is False
        assert "Single position quantity limit exceeded" in reason

    def test_position_quantity_limit_sell(self, manager):
        """Test sell order with large negative resulting position"""
        positions = [
            Position(
                symbol="AAPL", market=Market.NASDAQ, quantity=10,
                avg_entry_price=Decimal("100"), current_price=Decimal("100"),
                unrealized_pnl=Decimal("0"),
            ),
        ]
        manager.update_positions(positions)

        # 10 - 200 = -190, abs(-190) = 190 > 100
        order = self._make_order(
            side=OrderSide.SELL, quantity=200, price=Decimal("100"),
        )
        valid, reason = manager.validate_order(order)

        assert valid is False
        assert "Single position quantity limit exceeded" in reason

    def test_daily_trade_limit(self, manager):
        """Test order rejected when daily trade limit reached"""
        for _ in range(5):
            manager.record_trade(Decimal("0"))

        order = self._make_order()
        valid, reason = manager.validate_order(order)

        assert valid is False
        assert "Daily trade limit exceeded" in reason

    def test_market_order_no_price(self, manager):
        """Test market order with no price skips value checks"""
        order = Order(
            symbol="AAPL",
            market=Market.NASDAQ,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=99999,
            price=None,
        )
        valid, reason = manager.validate_order(order)

        assert valid is True

    def test_zero_quantity_order(self, manager):
        """Test order with zero quantity"""
        order = self._make_order(quantity=0, price=Decimal("100"))
        valid, reason = manager.validate_order(order)

        # 0 * 100 = 0, passes all value checks
        assert valid is True

    def test_zero_price_order(self, manager):
        """Test order with zero price"""
        order = self._make_order(quantity=10, price=Decimal("0"))
        valid, reason = manager.validate_order(order)

        # price is 0 which is falsy, so value checks are skipped
        assert valid is True


class TestRecordTrade:
    """Test cases for record_trade"""

    @pytest.fixture
    def manager(self):
        limits = RiskLimits(max_daily_loss=Decimal("100000"), max_daily_trades=10)
        return RiskManager(limits=limits, account_value=Decimal("10000000"))

    def test_record_positive_pnl(self, manager):
        """Test recording a trade with positive PnL"""
        manager.record_trade(Decimal("5000"))

        assert manager._daily_pnl == Decimal("5000")
        assert manager._daily_trades == 1

    def test_record_negative_pnl(self, manager):
        """Test recording a trade with negative PnL"""
        manager.record_trade(Decimal("-3000"))

        assert manager._daily_pnl == Decimal("-3000")
        assert manager._daily_trades == 1

    def test_record_zero_pnl(self, manager):
        """Test recording a trade with zero PnL"""
        manager.record_trade(Decimal("0"))

        assert manager._daily_pnl == Decimal("0")
        assert manager._daily_trades == 1

    def test_cumulative_trades(self, manager):
        """Test PnL accumulates across multiple trades"""
        manager.record_trade(Decimal("1000"))
        manager.record_trade(Decimal("-500"))
        manager.record_trade(Decimal("200"))

        assert manager._daily_pnl == Decimal("700")
        assert manager._daily_trades == 3

    def test_large_negative_pnl(self, manager):
        """Test recording large negative PnL"""
        manager.record_trade(Decimal("-99999"))

        assert manager._daily_pnl == Decimal("-99999")


class TestDailyLossLimit:
    """Test cases for daily loss limit enforcement"""

    @pytest.fixture
    def manager(self):
        limits = RiskLimits(max_daily_loss=Decimal("50000"), max_daily_trades=100)
        return RiskManager(limits=limits, account_value=Decimal("10000000"))

    def test_order_rejected_after_loss_limit(self, manager):
        """Test order is rejected after daily loss exceeds limit"""
        # Record loss exceeding the limit
        manager.record_trade(Decimal("-60000"))

        order = Order(
            symbol="AAPL", market=Market.NASDAQ, side=OrderSide.BUY,
            order_type=OrderType.LIMIT, quantity=1, price=Decimal("100"),
        )
        valid, reason = manager.validate_order(order)

        assert valid is False
        assert "Daily loss limit exceeded" in reason

    def test_order_allowed_at_exact_loss_limit(self, manager):
        """Test order allowed when loss equals exactly the limit"""
        # -50000 is NOT < -50000, so this should pass
        manager.record_trade(Decimal("-50000"))

        order = Order(
            symbol="AAPL", market=Market.NASDAQ, side=OrderSide.BUY,
            order_type=OrderType.LIMIT, quantity=1, price=Decimal("100"),
        )
        valid, reason = manager.validate_order(order)

        assert valid is True

    def test_order_allowed_under_loss_limit(self, manager):
        """Test order allowed when loss is under the limit"""
        manager.record_trade(Decimal("-49999"))

        order = Order(
            symbol="AAPL", market=Market.NASDAQ, side=OrderSide.BUY,
            order_type=OrderType.LIMIT, quantity=1, price=Decimal("100"),
        )
        valid, reason = manager.validate_order(order)

        assert valid is True

    def test_incremental_losses_trigger_limit(self, manager):
        """Test that many small losses can trigger the limit"""
        for _ in range(6):
            manager.record_trade(Decimal("-10000"))

        # Total PnL = -60000, exceeds -50000 limit
        order = Order(
            symbol="AAPL", market=Market.NASDAQ, side=OrderSide.BUY,
            order_type=OrderType.LIMIT, quantity=1, price=Decimal("100"),
        )
        valid, reason = manager.validate_order(order)

        assert valid is False

    def test_daily_reset_clears_loss(self, manager):
        """Test that daily reset clears loss tracking"""
        manager.record_trade(Decimal("-60000"))

        # Simulate a new day
        with patch("src.risk.manager.date") as mock_date:
            mock_date.today.return_value = date(2099, 1, 1)
            mock_date.side_effect = lambda *args, **kw: date(*args, **kw)

            order = Order(
                symbol="AAPL", market=Market.NASDAQ, side=OrderSide.BUY,
                order_type=OrderType.LIMIT, quantity=1, price=Decimal("100"),
            )
            valid, reason = manager.validate_order(order)

            assert valid is True
            assert manager._daily_pnl == Decimal("0")
            assert manager._daily_trades == 0


class TestGetDailyStats:
    """Test cases for get_daily_stats"""

    def test_initial_stats(self):
        limits = RiskLimits(max_daily_loss=Decimal("100000"), max_daily_trades=50)
        manager = RiskManager(limits=limits, account_value=Decimal("10000000"))

        stats = manager.get_daily_stats()

        assert stats["daily_pnl"] == Decimal("0")
        assert stats["daily_trades"] == 0
        assert stats["remaining_trades"] == 50
        assert stats["remaining_loss_budget"] == Decimal("100000")

    def test_stats_after_trades(self):
        limits = RiskLimits(max_daily_loss=Decimal("100000"), max_daily_trades=50)
        manager = RiskManager(limits=limits, account_value=Decimal("10000000"))

        manager.record_trade(Decimal("-30000"))
        manager.record_trade(Decimal("10000"))

        stats = manager.get_daily_stats()

        assert stats["daily_pnl"] == Decimal("-20000")
        assert stats["daily_trades"] == 2
        assert stats["remaining_trades"] == 48
        assert stats["remaining_loss_budget"] == Decimal("80000")


class TestCalculatePositionSize:
    """Test cases for calculate_position_size"""

    @pytest.fixture
    def manager(self):
        limits = RiskLimits(
            max_position_value=Decimal("1000000"),
            max_position_quantity=500,
        )
        mgr = RiskManager(limits=limits, account_value=Decimal("10000000"))
        mgr.available_cash = Decimal("5000000")
        return mgr

    def test_basic_position_size(self, manager):
        """Test basic position sizing"""
        qty = manager.calculate_position_size("AAPL", Decimal("100"), 0.1)

        # buy_amount = 5000000 * 0.1 = 500000
        # quantity = 500000 / 100 = 5000, but capped at max_position_quantity=500
        assert qty == 500

    def test_zero_price(self, manager):
        """Test position size with zero price"""
        qty = manager.calculate_position_size("AAPL", Decimal("0"), 0.5)
        assert qty == 0

    def test_negative_price(self, manager):
        """Test position size with negative price"""
        qty = manager.calculate_position_size("AAPL", Decimal("-10"), 0.5)
        assert qty == 0

    def test_zero_signal_strength(self, manager):
        """Test position size with zero signal"""
        qty = manager.calculate_position_size("AAPL", Decimal("100"), 0.0)
        assert qty == 0

    def test_position_value_cap(self, manager):
        """Test position value cap is applied"""
        # available_cash=5000000, signal=1.0, price=50000
        # buy_amount = 5000000, qty = 100
        # position_value = 100 * 50000 = 5000000 > 1000000 limit
        # capped qty = 1000000 / 50000 = 20
        qty = manager.calculate_position_size("AAPL", Decimal("50000"), 1.0)
        assert qty == 20


class TestUpdateMethods:
    """Test update_account_value, update_available_cash, update_positions"""

    def test_update_account_value(self):
        limits = RiskLimits()
        manager = RiskManager(limits=limits, account_value=Decimal("1000000"))

        manager.update_account_value(Decimal("2000000"))
        assert manager.account_value == Decimal("2000000")

    def test_update_available_cash(self):
        limits = RiskLimits()
        manager = RiskManager(limits=limits, account_value=Decimal("1000000"))

        manager.update_available_cash(Decimal("500000"))
        assert manager.available_cash == Decimal("500000")

    def test_update_positions(self):
        limits = RiskLimits()
        manager = RiskManager(limits=limits, account_value=Decimal("1000000"))

        positions = [
            Position(
                symbol="AAPL", market=Market.NASDAQ, quantity=100,
                avg_entry_price=Decimal("150"), current_price=Decimal("155"),
                unrealized_pnl=Decimal("500"),
            ),
            Position(
                symbol="MSFT", market=Market.NASDAQ, quantity=50,
                avg_entry_price=Decimal("300"), current_price=Decimal("310"),
                unrealized_pnl=Decimal("500"),
            ),
        ]
        manager.update_positions(positions)

        assert len(manager._positions) == 2
        assert manager._positions["AAPL"].quantity == 100
        assert manager._positions["MSFT"].quantity == 50
