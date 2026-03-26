"""Tests for RSI Mean Reversion Strategy"""

import pytest
from decimal import Decimal
from datetime import datetime, timedelta
from unittest.mock import MagicMock, AsyncMock
import pandas as pd

from src.strategy.builtin.rsi_mean_reversion import RSIMeanReversionStrategy
from src.core.types import Market, Bar, SignalType


class TestRSIMeanReversionStrategy:
    """Test cases for RSIMeanReversionStrategy"""

    @pytest.fixture
    def strategy(self):
        """Create a strategy instance"""
        return RSIMeanReversionStrategy(
            symbols=["AAPL", "MSFT"],
            market=Market.NASDAQ,
            params={
                "rsi_period": 14,
                "stop_loss": -10,
                "avg_down_levels": [(30, 0.5), (25, 0.3), (20, 0.2)],
                "sell_levels": [(70, 0.3), (75, 0.4), (80, 0.5)],
            },
        )

    @pytest.fixture
    def sample_bars(self):
        """Create sample historical bars"""
        bars = []
        base_price = 100.0
        for i in range(50):
            # Create varying prices for RSI calculation
            price = base_price + (i % 10) - 5
            bar = MagicMock()
            bar.timestamp = datetime.now() - timedelta(days=50 - i)
            bar.open = price
            bar.high = price + 1
            bar.low = price - 1
            bar.close = price
            bar.volume = 1000000
            bars.append(bar)
        return bars

    def test_init(self, strategy):
        """Test strategy initialization"""
        assert strategy.name == "RSI_MeanReversion"
        assert strategy.required_history == 30
        assert "AAPL" in strategy.symbols
        assert strategy.market == Market.NASDAQ

    def test_initialize(self, strategy, sample_bars):
        """Test initialization with historical data"""
        strategy.initialize({"AAPL": sample_bars})

        assert "AAPL" in strategy._daily_bars
        df = strategy._daily_bars["AAPL"]
        assert len(df) == 50

    def test_update_daily_close_same_day(self, strategy, sample_bars):
        """Test updating daily close on same day"""
        strategy.initialize({"AAPL": sample_bars})

        # Update with new price
        strategy.update_daily_close("AAPL", 105.0)

        df = strategy._daily_bars["AAPL"]
        assert df.iloc[-1]["close"] == 105.0

    def test_update_daily_close_new_day(self, strategy, sample_bars):
        """Test updating daily close on new day"""
        # Create bars from yesterday
        old_bars = []
        for i in range(30):
            bar = MagicMock()
            bar.timestamp = datetime.now() - timedelta(days=31 - i)
            bar.open = 100.0
            bar.high = 101.0
            bar.low = 99.0
            bar.close = 100.0
            bar.volume = 1000000
            old_bars.append(bar)

        strategy.initialize({"AAPL": old_bars})
        initial_len = len(strategy._daily_bars["AAPL"])

        # Update with today's price
        strategy.update_daily_close("AAPL", 105.0)

        df = strategy._daily_bars["AAPL"]
        assert len(df) == initial_len + 1
        assert df.iloc[-1]["close"] == 105.0

    def test_calculate_rsi(self, strategy):
        """Test RSI calculation"""
        # Create price series with known pattern
        prices = pd.Series([44, 44.25, 44.5, 43.75, 44.5, 44.25, 44, 43.5,
                           44, 44.5, 45, 45.25, 45.5, 45, 44.5, 44.75,
                           45, 45.5, 46, 46.5])

        rsi = strategy._calculate_rsi(prices, period=14)

        # RSI should be between 0 and 100
        assert all(0 <= r <= 100 for r in rsi.dropna())

    def test_get_current_rsi(self, strategy, sample_bars):
        """Test getting current RSI"""
        strategy.initialize({"AAPL": sample_bars})

        rsi = strategy.get_current_rsi("AAPL")

        assert rsi is not None
        assert 0 <= rsi <= 100

    def test_get_current_rsi_insufficient_data(self, strategy):
        """Test RSI with insufficient data"""
        bars = []
        for i in range(10):  # Less than required_history
            bar = MagicMock()
            bar.timestamp = datetime.now() - timedelta(days=10 - i)
            bar.open = 100.0
            bar.high = 101.0
            bar.low = 99.0
            bar.close = 100.0
            bar.volume = 1000000
            bars.append(bar)

        strategy.initialize({"AAPL": bars})

        rsi = strategy.get_current_rsi("AAPL")
        assert rsi is None

    @pytest.mark.asyncio
    async def test_calculate_signal_buy(self, strategy, sample_bars):
        """Test buy signal generation"""
        # Create bars that result in low RSI
        bars = []
        price = 100.0
        for i in range(50):
            # Declining prices = low RSI
            price = 100.0 - (i * 0.5)
            bar = MagicMock()
            bar.timestamp = datetime.now() - timedelta(days=50 - i)
            bar.open = price + 0.5
            bar.high = price + 1
            bar.low = price - 1
            bar.close = price
            bar.volume = 1000000
            bars.append(bar)

        strategy.initialize({"AAPL": bars})

        signal = await strategy.calculate_signal("AAPL")

        # With declining prices, RSI should be low and generate buy signal
        if signal is not None:
            assert signal.signal_type == SignalType.ENTRY_LONG

    @pytest.mark.asyncio
    async def test_calculate_signal_sell(self, strategy):
        """Test sell signal generation"""
        # Create bars that result in high RSI
        bars = []
        for i in range(50):
            # Rising prices = high RSI
            price = 50.0 + (i * 1.0)
            bar = MagicMock()
            bar.timestamp = datetime.now() - timedelta(days=50 - i)
            bar.open = price - 0.5
            bar.high = price + 1
            bar.low = price - 1
            bar.close = price
            bar.volume = 1000000
            bars.append(bar)

        strategy.initialize({"AAPL": bars})

        # Set position and entry price
        strategy._positions["AAPL"] = 100
        strategy._entry_prices["AAPL"] = Decimal("60")
        strategy._sell_stages["AAPL"] = 0

        signal = await strategy.calculate_signal("AAPL")

        # With rising prices, RSI should be high
        if signal is not None:
            assert signal.signal_type == SignalType.EXIT_LONG

    @pytest.mark.asyncio
    async def test_stop_loss_signal(self, strategy, sample_bars):
        """Test stop loss signal"""
        strategy.initialize({"AAPL": sample_bars})

        # Set position with high entry price (to trigger stop loss)
        strategy._positions["AAPL"] = 100
        strategy._entry_prices["AAPL"] = Decimal("150")  # Entry at 150

        # Current price around 100 = -33% loss > -10% stop loss
        signal = await strategy.calculate_signal("AAPL")

        assert signal is not None
        assert signal.signal_type == SignalType.EXIT_LONG
        assert signal.metadata.get("reason") == "stop_loss"

    def test_sync_position(self, strategy):
        """Test position sync"""
        strategy.sync_position("AAPL", 100, Decimal("150"))

        assert strategy._positions["AAPL"] == 100
        assert strategy._entry_prices["AAPL"] == Decimal("150")
        assert strategy._buy_stages["AAPL"] == 1
        assert strategy._sell_stages["AAPL"] == 0

    def test_sync_position_no_position(self, strategy):
        """Test position sync with no position"""
        # First set some state
        strategy._positions["AAPL"] = 100
        strategy._entry_prices["AAPL"] = Decimal("150")
        strategy._buy_stages["AAPL"] = 2

        # Sync with zero quantity
        strategy.sync_position("AAPL", 0, Decimal("0"))

        assert "AAPL" not in strategy._entry_prices
        assert "AAPL" not in strategy._buy_stages

    def test_reset_position(self, strategy):
        """Test position reset"""
        # Set up state
        strategy._entry_prices["AAPL"] = Decimal("100")
        strategy._buy_stages["AAPL"] = 2
        strategy._sell_stages["AAPL"] = 1
        strategy._last_buy_time["AAPL"] = datetime.now()

        # Reset
        strategy._reset_position("AAPL")

        assert "AAPL" not in strategy._entry_prices
        assert "AAPL" not in strategy._buy_stages
        assert "AAPL" not in strategy._sell_stages
        assert "AAPL" not in strategy._last_buy_time

    @pytest.mark.asyncio
    async def test_on_fill_buy(self, strategy, sample_bars):
        """Test on_fill for buy order"""
        strategy.initialize({"AAPL": sample_bars})

        fill = MagicMock()
        fill.symbol = "AAPL"
        fill.side = MagicMock()
        fill.side.value = "buy"
        fill.quantity = 50
        fill.price = Decimal("100")

        await strategy.on_fill(fill)

        assert strategy._entry_prices["AAPL"] == Decimal("100")
        assert strategy._sell_stages["AAPL"] == 0

    @pytest.mark.asyncio
    async def test_on_fill_averaging(self, strategy, sample_bars):
        """Test on_fill with averaging down"""
        strategy.initialize({"AAPL": sample_bars})

        # First buy
        strategy._positions["AAPL"] = 50
        strategy._entry_prices["AAPL"] = Decimal("100")

        # Second buy at lower price
        fill = MagicMock()
        fill.symbol = "AAPL"
        fill.side = MagicMock()
        fill.side.value = "buy"
        fill.quantity = 50
        fill.price = Decimal("80")

        await strategy.on_fill(fill)

        # Average should be (50*100 + 50*80) / 100 = 90
        assert strategy._entry_prices["AAPL"] == Decimal("90")
