"""Tests for RiskManager — limits are fractions of account equity."""

import pytest
from decimal import Decimal
from datetime import date
from unittest.mock import patch

from src.risk.manager import RiskManager
from src.risk.limits import RiskLimits
from src.core.types import Order, Position, Market, OrderSide, OrderType


def _order(symbol="AAPL", side=OrderSide.BUY, quantity=10,
           price=Decimal("100"), market=Market.NASDAQ):
    return Order(
        symbol=symbol, market=market, side=side,
        order_type=OrderType.LIMIT, quantity=quantity, price=price,
    )


class TestRiskLimitsConfig:
    def test_defaults_are_fractions(self):
        lim = RiskLimits()
        assert lim.max_daily_loss_pct == 0.03
        assert lim.max_position_pct == 0.25
        assert lim.max_total_exposure_pct == 1.0

    def test_from_config_reads_pct_keys(self):
        lim = RiskLimits.from_config({
            "max_daily_loss_pct": 0.05,
            "max_position_pct": 0.30,
            "max_total_exposure_pct": 0.8,
            "max_daily_trades": 20,
            "position_sizing": {"risk_per_trade": 0.01},
        })
        assert lim.max_daily_loss_pct == 0.05
        assert lim.max_position_pct == 0.30
        assert lim.max_total_exposure_pct == 0.8
        assert lim.max_daily_trades == 20
        assert lim.risk_per_trade == 0.01


class TestPctHelper:
    def test_pct_scales_with_equity(self):
        m = RiskManager(RiskLimits(), account_value=Decimal("10000"))
        assert m._pct(0.25) == Decimal("2500.00")

    def test_zero_equity_yields_zero_limit(self):
        m = RiskManager(RiskLimits(), account_value=Decimal("0"))
        assert m._pct(0.25) == Decimal("0")

    def test_negative_equity_treated_as_zero(self):
        m = RiskManager(RiskLimits(), account_value=Decimal("-5"))
        assert m._pct(0.25) == Decimal("0")


class TestValidateOrder:
    @pytest.fixture
    def manager(self):
        # equity $10,000 → per-position cap 25% = $2,500,
        # daily-loss cap 3% = $300, total-exposure cap 100% = $10,000
        return RiskManager(
            RiskLimits(
                max_daily_loss_pct=0.03,
                max_position_pct=0.25,
                max_total_exposure_pct=1.0,
                max_daily_trades=5,
                max_position_quantity=100,
            ),
            account_value=Decimal("10000"),
        )

    def test_valid_usd_order_passes(self, manager):
        # 10 × $100 = $1,000 ≤ $2,500
        ok, reason = manager.validate_order(_order(quantity=10))
        assert ok is True and reason is None

    def test_position_pct_rejects_oversized_usd_order(self, manager):
        # 30 × $100 = $3,000 > $2,500 (25% of $10k). The exact bug class
        # the KRW-limit audit flagged: a realistic USD order now trips.
        ok, reason = manager.validate_order(_order(quantity=30))
        assert ok is False
        assert "Position value too large" in reason

    def test_position_pct_at_boundary_passes(self, manager):
        # 25 × $100 = $2,500 == cap (not strictly greater)
        ok, _ = manager.validate_order(_order(quantity=25))
        assert ok is True

    def test_total_exposure_pct_rejects(self, manager):
        manager.update_positions([Position(
            symbol="MSFT", market=Market.NASDAQ, quantity=90,
            avg_entry_price=Decimal("100"), current_price=Decimal("100"),
            unrealized_pnl=Decimal("0"),
        )])  # existing exposure $9,000
        # new $2,000 → $11,000 > $10,000 cap
        ok, reason = manager.validate_order(
            _order(quantity=20, price=Decimal("100"))
        )
        assert ok is False
        assert "Total exposure limit exceeded" in reason

    def test_daily_loss_pct_blocks_further_orders(self, manager):
        manager.record_trade(Decimal("-301"))  # beyond -3% ($300)
        ok, reason = manager.validate_order(_order(quantity=1))
        assert ok is False
        assert "Daily loss limit exceeded" in reason

    def test_daily_loss_exactly_at_limit_allowed(self, manager):
        manager.record_trade(Decimal("-300"))  # not < -300
        ok, _ = manager.validate_order(_order(quantity=1))
        assert ok is True

    def test_zero_equity_is_fail_safe(self):
        # Unknown balance ⇒ every priced order rejected (never trade blind)
        m = RiskManager(RiskLimits(), account_value=Decimal("0"))
        ok, reason = m.validate_order(_order(quantity=1, price=Decimal("1")))
        assert ok is False
        assert "Position value too large" in reason

    def test_market_order_without_price_skips_value_checks(self, manager):
        o = Order(symbol="AAPL", market=Market.NASDAQ, side=OrderSide.BUY,
                  order_type=OrderType.MARKET, quantity=99999, price=None)
        ok, _ = manager.validate_order(o)
        assert ok is True

    def test_daily_trade_limit(self, manager):
        for _ in range(5):
            manager.record_trade(Decimal("0"))
        ok, reason = manager.validate_order(_order())
        assert ok is False
        assert "Daily trade limit exceeded" in reason

    def test_position_quantity_limit(self, manager):
        manager.update_positions([Position(
            symbol="AAPL", market=Market.NASDAQ, quantity=95,
            avg_entry_price=Decimal("1"), current_price=Decimal("1"),
            unrealized_pnl=Decimal("0"),
        )])
        ok, reason = manager.validate_order(
            _order(quantity=10, price=Decimal("1"))
        )
        assert ok is False
        assert "Single position quantity limit exceeded" in reason


class TestRecordTradeAndStats:
    @pytest.fixture
    def manager(self):
        return RiskManager(
            RiskLimits(max_daily_loss_pct=0.03),
            account_value=Decimal("10000"),
        )

    def test_record_accumulates(self, manager):
        manager.record_trade(Decimal("100"))
        manager.record_trade(Decimal("-40"))
        assert manager._daily_pnl == Decimal("60")
        assert manager._daily_trades == 2

    def test_daily_stats_budget_is_equity_based(self, manager):
        manager.record_trade(Decimal("-50"))
        stats = manager.get_daily_stats()
        # cap = 3% of $10,000 = $300; remaining = 300 + (-50) = 250
        assert stats["remaining_loss_budget"] == Decimal("250.00")
        assert stats["daily_pnl"] == Decimal("-50")

    def test_daily_reset_clears(self, manager):
        manager.record_trade(Decimal("-999"))
        with patch("src.risk.manager.date") as md:
            md.today.return_value = date(2099, 1, 1)
            ok, _ = manager.validate_order(
                _order(quantity=1, price=Decimal("1"))
            )
        assert ok is True
        assert manager._daily_pnl == Decimal("0")


class TestCalculatePositionSize:
    @pytest.fixture
    def manager(self):
        m = RiskManager(
            RiskLimits(max_position_pct=0.25, max_position_quantity=500),
            account_value=Decimal("10000"),
        )
        m.available_cash = Decimal("5000")
        return m

    def test_capped_by_quantity(self, manager):
        assert manager.calculate_position_size("AAPL", Decimal("100"), 0.1) == 5

    def test_capped_by_position_pct(self, manager):
        # signal 1.0 → buy_amount 5000, qty 100 @ $50;
        # value 100×50=5000 > 25% of 10000 ($2500) → 2500/50 = 50
        assert manager.calculate_position_size("AAPL", Decimal("50"), 1.0) == 50

    def test_zero_or_negative_price(self, manager):
        assert manager.calculate_position_size("AAPL", Decimal("0"), 0.5) == 0
        assert manager.calculate_position_size("AAPL", Decimal("-1"), 0.5) == 0


class TestUpdateMethods:
    def test_update_account_value_rescales_limits(self):
        m = RiskManager(RiskLimits(max_position_pct=0.25),
                         account_value=Decimal("1000"))
        assert m._pct(0.25) == Decimal("250.00")
        m.update_account_value(Decimal("4000"))
        assert m._pct(0.25) == Decimal("1000.00")
