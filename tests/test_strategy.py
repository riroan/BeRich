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
        assert strategy.required_history == 20
        assert "AAPL" in strategy.symbols
        assert strategy.market == Market.NASDAQ

    def test_initialize(self, strategy, sample_bars):
        """Test initialization with historical data"""
        strategy.initialize({"AAPL": sample_bars})

        assert "AAPL" in strategy._daily_bars
        df = strategy._daily_bars["AAPL"]
        assert len(df) == 50

    def test_update_daily_close_layers_live_not_base(self, strategy, sample_bars):
        """C': live price layers into get_daily_dataframe as the most
        recent point and never mutates the confirmed base."""
        strategy.initialize({"AAPL": sample_bars})
        base_len = len(strategy._daily_bars["AAPL"])
        base_last_close = strategy._daily_bars["AAPL"].iloc[-1]["close"]

        strategy.update_daily_close("AAPL", 105.0)

        # Confirmed base is untouched (no clock-based slide).
        base = strategy._daily_bars["AAPL"]
        assert len(base) == base_len
        assert base.iloc[-1]["close"] == base_last_close

        # The live forming row shows up as the latest point in the view.
        view = strategy.get_daily_dataframe("AAPL")
        assert len(view) == base_len + 1
        assert view.iloc[-1]["close"] == 105.0

    def test_update_daily_close_never_slides_on_clock(self, strategy):
        """C': successive ticks on different days do NOT append base rows —
        only confirm_daily_bar slides the window."""
        old_bars = []
        for i in range(30):
            bar = MagicMock()
            bar.timestamp = datetime.now() - timedelta(days=31 - i)
            bar.open = bar.high = bar.low = bar.close = 100.0
            bar.volume = 1000000
            old_bars.append(bar)

        strategy.initialize({"AAPL": old_bars})
        initial_len = len(strategy._daily_bars["AAPL"])

        strategy.update_daily_close("AAPL", 105.0)
        strategy.update_daily_close("AAPL", 106.0)

        # Base length unchanged; only the live slot moves.
        assert len(strategy._daily_bars["AAPL"]) == initial_len
        view = strategy.get_daily_dataframe("AAPL")
        assert len(view) == initial_len + 1
        assert view.iloc[-1]["close"] == 106.0

    def test_confirm_daily_bar_appends_on_newer_date(self, strategy, sample_bars):
        """A confirmed bar with a newer date slides the base."""
        strategy.initialize({"AAPL": sample_bars})
        base_len = len(strategy._daily_bars["AAPL"])
        last = strategy.last_confirmed_date("AAPL")

        bar = Bar(
            symbol="AAPL", market=Market.NASDAQ,
            open=Decimal("110"), high=Decimal("111"), low=Decimal("109"),
            close=Decimal("110"), volume=0,
            timestamp=datetime.now() + timedelta(days=1), timeframe="1d",
        )
        assert strategy.confirm_daily_bar("AAPL", bar) == "appended"
        # Rolling window: base is capped at required_history, so a slide keeps
        # it at the cap rather than growing unbounded.
        assert len(strategy._daily_bars["AAPL"]) == min(
            base_len + 1, strategy.required_history
        )
        assert strategy._daily_bars["AAPL"].iloc[-1]["close"] == 110.0
        assert strategy.last_confirmed_date("AAPL") > last

    def test_confirm_daily_bar_refreshes_same_date(self, strategy, sample_bars):
        """A confirmed bar with the same date refreshes (final close)
        without growing the base."""
        strategy.initialize({"AAPL": sample_bars})
        base_len = len(strategy._daily_bars["AAPL"])
        same_ts = strategy._daily_bars["AAPL"].index[-1]

        bar = Bar(
            symbol="AAPL", market=Market.NASDAQ,
            open=Decimal("99"), high=Decimal("120"), low=Decimal("98"),
            close=Decimal("118"), volume=0,
            timestamp=same_ts, timeframe="1d",
        )
        assert strategy.confirm_daily_bar("AAPL", bar) == "refreshed"
        assert len(strategy._daily_bars["AAPL"]) == base_len
        assert strategy._daily_bars["AAPL"].iloc[-1]["close"] == 118.0

    def test_confirm_daily_bar_skips_stale_date(self, strategy, sample_bars):
        """An older-dated bar is ignored."""
        strategy.initialize({"AAPL": sample_bars})
        base_len = len(strategy._daily_bars["AAPL"])

        bar = Bar(
            symbol="AAPL", market=Market.NASDAQ,
            open=Decimal("50"), high=Decimal("50"), low=Decimal("50"),
            close=Decimal("50"), volume=0,
            timestamp=datetime.now() - timedelta(days=5), timeframe="1d",
        )
        assert strategy.confirm_daily_bar("AAPL", bar) == "skipped"
        assert len(strategy._daily_bars["AAPL"]) == base_len

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
    async def test_next_sell_stage_ignores_cooldown(self, strategy):
        """SELL2 can fire before the sell cooldown if its RSI threshold is hit."""
        bars = []
        for i in range(50):
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
        strategy._positions["AAPL"] = 100
        strategy._entry_prices["AAPL"] = Decimal("60")
        strategy._buy_stages["AAPL"] = 2
        strategy._sell_stages["AAPL"] = 1
        strategy._last_buy_time["AAPL"] = datetime.now() - timedelta(days=10)
        strategy._last_sell_time["AAPL"] = datetime.now()

        signal = await strategy.calculate_signal("AAPL")

        assert strategy._buy_stages["AAPL"] == 2
        assert strategy._sell_stages["AAPL"] == 1
        assert signal is not None
        assert signal.metadata["reason"] == "staged_sell_2"

    @pytest.mark.asyncio
    async def test_sell_cooldown_allows_same_stage_again(self, strategy):
        """After sell cooldown, the sell ladder can start again at SELL1."""
        bars = []
        for i in range(50):
            price = 90.0 + (i * 0.1)
            bar = MagicMock()
            bar.timestamp = datetime.now() - timedelta(days=50 - i)
            bar.open = price - 0.5
            bar.high = price + 1
            bar.low = price - 1
            bar.close = price
            bar.volume = 1000000
            bars.append(bar)

        strategy.initialize({"AAPL": bars})
        df = strategy.get_daily_dataframe("AAPL")
        strategy._calculate_rsi = MagicMock(
            return_value=pd.Series([72.0] * len(df), index=df.index)
        )
        strategy._positions["AAPL"] = 100
        strategy._entry_prices["AAPL"] = Decimal("60")
        strategy._sell_stages["AAPL"] = 1
        strategy._last_sell_time["AAPL"] = datetime.now() - timedelta(days=10)

        signal = await strategy.calculate_signal("AAPL")

        assert strategy._sell_stages["AAPL"] == 1
        assert signal is not None
        assert signal.metadata["reason"] == "staged_sell_1"

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
        strategy._last_sell_time["AAPL"] = datetime.now()

        # Reset
        strategy._reset_position("AAPL")

        assert "AAPL" not in strategy._entry_prices
        assert "AAPL" not in strategy._buy_stages
        assert "AAPL" not in strategy._sell_stages
        assert "AAPL" not in strategy._last_buy_time
        assert "AAPL" not in strategy._last_sell_time

    def _fill(self, side, qty, price, metadata=None):
        fill = MagicMock()
        fill.symbol = "AAPL"
        fill.side = MagicMock()
        fill.side.value = side
        fill.quantity = qty
        fill.price = Decimal(str(price))
        fill.metadata = metadata or {}
        return fill

    @pytest.mark.asyncio
    async def test_on_fill_buy(self, strategy, sample_bars):
        """Test on_fill for buy order"""
        strategy.initialize({"AAPL": sample_bars})

        await strategy.on_fill(
            self._fill("buy", 50, 100, {"stage": 1, "reason": "avg_down_stage_1"})
        )

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
        await strategy.on_fill(self._fill("buy", 50, 80, {"stage": 2}))

        # Average should be (50*100 + 50*80) / 100 = 90
        assert strategy._entry_prices["AAPL"] == Decimal("90")

    @pytest.mark.asyncio
    async def test_on_fill_buy_advances_stage_on_fill(self, strategy, sample_bars):
        """C' #8: buy stage counter advances on the actual fill, idempotent
        via target stage (set, not increment)."""
        strategy.initialize({"AAPL": sample_bars})

        # Partial fills of the same stage-1 order → counter stays at 1.
        await strategy.on_fill(self._fill("buy", 30, 100, {"stage": 1}))
        await strategy.on_fill(self._fill("buy", 20, 100, {"stage": 1}))

        assert strategy._buy_stages["AAPL"] == 1
        assert "AAPL" in strategy._last_buy_time
        assert strategy._sell_stages["AAPL"] == 0

    @pytest.mark.asyncio
    async def test_on_fill_buy_keeps_existing_sell_stage(self, strategy, sample_bars):
        """Buy fills advance buy stage without resetting the sell counter."""
        strategy.initialize({"AAPL": sample_bars})
        strategy._positions["AAPL"] = 70
        strategy._entry_prices["AAPL"] = Decimal("100")
        strategy._sell_stages["AAPL"] = 1

        await strategy.on_fill(
            self._fill("buy", 30, 80, {"stage": 1, "reason": "avg_down_stage_1"})
        )

        assert strategy._buy_stages["AAPL"] == 1
        assert strategy._sell_stages["AAPL"] == 1

    @pytest.mark.asyncio
    async def test_on_fill_sell_advances_sell_stage(self, strategy, sample_bars):
        """Staged-sell counter advances on the fill."""
        strategy.initialize({"AAPL": sample_bars})
        strategy._positions["AAPL"] = 100
        strategy._entry_prices["AAPL"] = Decimal("100")

        await strategy.on_fill(
            self._fill("sell", 30, 110, {"stage": 1, "reason": "staged_sell_1"})
        )

        assert strategy._sell_stages["AAPL"] == 1
        assert "AAPL" in strategy._last_sell_time
        # Non-final sell: position reduced but state kept.
        assert strategy._positions["AAPL"] == 70
        assert "AAPL" in strategy._entry_prices

    @pytest.mark.asyncio
    async def test_on_fill_stop_loss_resets_only_when_flat(self, strategy, sample_bars):
        """A partial stop-loss fill must NOT wipe state; reset happens only
        once the position is fully flat."""
        strategy.initialize({"AAPL": sample_bars})
        strategy._positions["AAPL"] = 100
        strategy._entry_prices["AAPL"] = Decimal("100")
        strategy._buy_stages["AAPL"] = 2

        # Partial stop-loss fill → still holding 40 → no reset.
        await strategy.on_fill(self._fill("sell", 60, 90, {"reason": "stop_loss"}))
        assert strategy._positions["AAPL"] == 40
        assert "AAPL" in strategy._entry_prices

        # Remaining fills → flat → reset everything.
        await strategy.on_fill(self._fill("sell", 40, 90, {"reason": "stop_loss"}))
        assert strategy.get_position("AAPL") <= 0
        assert "AAPL" not in strategy._entry_prices
        assert "AAPL" not in strategy._buy_stages

    @pytest.mark.asyncio
    async def test_on_fill_final_sell_keeps_residual_state(self, strategy, sample_bars):
        """Final staged sell keeps state until sell cooldown or full exit."""
        strategy.initialize({"AAPL": sample_bars})
        strategy._positions["AAPL"] = 100
        strategy._entry_prices["AAPL"] = Decimal("100")
        strategy._buy_stages["AAPL"] = 2

        await strategy.on_fill(self._fill(
            "sell", 50, 120,
            {"stage": 3, "reason": "staged_sell_3", "is_final": True},
        ))

        assert strategy._positions["AAPL"] == 50
        assert strategy._entry_prices["AAPL"] == Decimal("100")
        assert strategy._buy_stages["AAPL"] == 2
        assert strategy._sell_stages["AAPL"] == 3
        assert "AAPL" in strategy._last_sell_time

    @pytest.mark.asyncio
    async def test_on_fill_staged_sell_resets_when_flat(self, strategy, sample_bars):
        """Any staged sell that fully exits clears symbol tracking."""
        strategy.initialize({"AAPL": sample_bars})
        strategy._positions["AAPL"] = 50
        strategy._entry_prices["AAPL"] = Decimal("100")
        strategy._buy_stages["AAPL"] = 2

        await strategy.on_fill(self._fill(
            "sell", 50, 120,
            {"stage": 3, "reason": "staged_sell_3", "is_final": True},
        ))

        assert strategy.get_position("AAPL") <= 0
        assert "AAPL" not in strategy._entry_prices
        assert "AAPL" not in strategy._buy_stages
        assert "AAPL" not in strategy._sell_stages
