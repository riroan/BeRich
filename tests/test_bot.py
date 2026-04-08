"""Tests for TradingBot core"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from decimal import Decimal
from pathlib import Path

from src.bot.core import TradingBot
from src.bot.warmup import WarmupManager


class TestTradingBot:
    """Test cases for TradingBot"""

    @pytest.fixture
    def mock_config(self, tmp_path):
        """Create mock config"""
        config = MagicMock()
        config.load = MagicMock()
        config.get = MagicMock(return_value=0)
        config.get_risk_config = MagicMock(return_value={
            "max_position_size": 0.1,
            "max_daily_loss": 0.05,
            "max_drawdown": 0.15,
        })
        config.get_kis_config = MagicMock(return_value={
            "app_key": "test_key",
            "app_secret": "test_secret",
            "account_no": "test_account",
            "paper_trading": True,
        })
        config.get_discord_config = MagicMock(return_value={
            "enabled": False,
            "webhook_url": None,
        })
        config.strategies = []
        return config

    @pytest.fixture
    def bot(self, tmp_path):
        """Create a TradingBot instance"""
        with patch("src.bot.core.Config") as MockConfig:
            MockConfig.return_value = MagicMock()
            bot = TradingBot(config_dir=str(tmp_path))
            bot._data_dir = tmp_path
            bot._warmup = WarmupManager(warmup_hours=0)
            return bot

    def test_init(self, bot):
        """Test TradingBot initialization"""
        assert bot._running is False
        assert bot._stopped is False
        assert bot.storage is None
        assert bot.broker is None

    def test_init_with_warmup(self, tmp_path):
        """Test TradingBot initialization with warmup"""
        with patch("src.bot.core.Config"):
            bot = TradingBot(config_dir=str(tmp_path), warmup_hours=2)
            bot._data_dir = tmp_path
            bot._warmup = WarmupManager(warmup_hours=2)

            assert bot._warmup.warmup_hours == 2

    @pytest.mark.asyncio
    async def test_initialize(self, bot, mock_config):
        """Test bot initialization"""
        bot.config = mock_config

        with patch("src.bot.core.Storage") as MockStorage, \
             patch("src.bot.core.KISBroker") as MockBroker, \
             patch("src.bot.core.StrategyEngine") as MockEngine, \
             patch("src.bot.core.OrderManager") as MockOrderManager, \
             patch("src.bot.core.TradingScheduler") as MockScheduler, \
             patch("src.bot.core.RiskManager") as MockRiskManager, \
             patch("src.bot.core.RiskLimits") as MockRiskLimits:

            # Setup mocks
            mock_storage = AsyncMock()
            MockStorage.return_value = mock_storage

            mock_broker = AsyncMock()
            mock_broker.connect = AsyncMock()
            mock_broker.get_account_balance = AsyncMock(return_value={
                "total_eval": Decimal("1000000"),
                "cash": Decimal("500000"),
                "profit_loss": Decimal("10000"),
            })
            MockBroker.return_value = mock_broker

            mock_engine = MagicMock()
            MockEngine.return_value = mock_engine

            mock_order_manager = MagicMock()
            MockOrderManager.return_value = mock_order_manager

            mock_scheduler = MagicMock()
            MockScheduler.return_value = mock_scheduler

            mock_risk_manager = MagicMock()
            MockRiskManager.return_value = mock_risk_manager

            MockRiskLimits.from_config = MagicMock()

            await bot.initialize()

            assert bot.storage is not None
            assert bot.broker is not None
            assert bot.strategy_engine is not None

    @pytest.mark.asyncio
    async def test_stop_idempotent(self, bot):
        """Test that stop can be called multiple times"""
        bot._stopped = False
        bot._running = True
        bot.scheduler = AsyncMock()
        bot.order_manager = AsyncMock()
        bot.strategy_engine = AsyncMock()
        bot.event_bus = AsyncMock()
        bot.broker = AsyncMock()
        bot.storage = AsyncMock()
        bot.notifier = AsyncMock()

        await bot.stop()
        assert bot._stopped is True

        # Second call should do nothing
        await bot.stop()
        assert bot._stopped is True

    def test_equity_save_interval(self, bot):
        """Test equity save interval configuration"""
        assert bot._equity_save_interval == 5
        assert bot._equity_save_counter == 0


class TestDashboardSyncMixin:
    """Test cases for DashboardSyncMixin"""

    @pytest.fixture
    def bot_with_dashboard(self, tmp_path):
        """Create bot with mocked dashboard"""
        with patch("src.bot.core.Config"), \
             patch("src.bot.core.get_dashboard_state") as mock_dashboard:

            dashboard = MagicMock()
            dashboard.rsi_values = {}
            dashboard.balance_krw = Decimal("0")
            dashboard.balance_usd = Decimal("0")
            dashboard.cash_krw = Decimal("0")
            dashboard.cash_usd = Decimal("0")
            dashboard.equity_history = []
            mock_dashboard.return_value = dashboard

            bot = TradingBot(config_dir=str(tmp_path))
            bot._data_dir = tmp_path
            bot._warmup = WarmupManager(warmup_hours=0)
            bot.dashboard = dashboard
            return bot

    def test_get_strategy_states(self, bot_with_dashboard):
        """Test getting strategy states"""
        bot = bot_with_dashboard

        # Mock strategy engine
        mock_strategy = MagicMock()
        mock_strategy.symbols = ["AAPL"]
        mock_strategy._buy_stages = {"AAPL": 1}
        mock_strategy._sell_stages = {"AAPL": 0}
        mock_strategy._last_buy_time = {}
        mock_strategy.params = {}

        mock_engine = MagicMock()
        mock_engine.get_strategies.return_value = [mock_strategy]
        bot.strategy_engine = mock_engine

        states = bot._get_strategy_states()

        assert "AAPL" in states
        assert states["AAPL"]["buy_stage"] == 1

    def test_update_dashboard_status(self, bot_with_dashboard):
        """Test dashboard status update"""
        bot = bot_with_dashboard

        # Mock dependencies
        mock_strategy = MagicMock()
        mock_strategy.name = "TestStrategy"

        mock_engine = MagicMock()
        mock_engine.get_strategies.return_value = [mock_strategy]
        bot.strategy_engine = mock_engine

        bot.broker = MagicMock()
        bot.broker.paper_trading = True

        bot.risk_manager = MagicMock()
        bot.risk_manager.account_value = Decimal("1000000")

        bot._warmup._start_time = None

        bot.update_dashboard_status()

        bot.dashboard.set_bot_status.assert_called_once()


class TestTickHandlerMixin:
    """Test cases for TickHandlerMixin"""

    @pytest.fixture
    def bot_with_tick(self, tmp_path):
        """Create bot for tick handler testing"""
        with patch("src.bot.core.Config"), \
             patch("src.bot.core.get_dashboard_state") as mock_dashboard:

            dashboard = MagicMock()
            dashboard.rsi_values = {}
            dashboard.last_price_update = None
            mock_dashboard.return_value = dashboard

            bot = TradingBot(config_dir=str(tmp_path))
            bot._data_dir = tmp_path
            bot._warmup = WarmupManager(warmup_hours=0)
            bot.dashboard = dashboard
            return bot

    @pytest.mark.asyncio
    async def test_process_symbol_tick(self, bot_with_tick):
        """Test processing tick for a symbol"""
        bot = bot_with_tick

        # Mock dependencies
        bot.broker = AsyncMock()
        bot.broker.get_current_price = AsyncMock(return_value=Decimal("150"))

        mock_strategy = MagicMock()
        mock_strategy.market = MagicMock()
        mock_strategy.market.value = "NASDAQ"
        mock_strategy.update_daily_close = MagicMock()
        mock_strategy.get_current_rsi = MagicMock(return_value=45.0)

        bot.storage = AsyncMock()
        bot.event_bus = AsyncMock()
        bot.notifier = None

        await bot._process_symbol_tick(mock_strategy, "AAPL")

        bot.broker.get_current_price.assert_called_once()
        mock_strategy.update_daily_close.assert_called_once_with("AAPL", 150.0)


class TestDataLoaderMixin:
    """Test cases for DataLoaderMixin"""

    @pytest.fixture
    def bot_with_loader(self, tmp_path):
        """Create bot for data loader testing"""
        with patch("src.bot.core.Config"), \
             patch("src.bot.core.get_dashboard_state") as mock_dashboard:

            dashboard = MagicMock()
            dashboard.rsi_values = {}
            dashboard.rsi_prices = {}
            dashboard.equity_history = []
            dashboard.fills = []
            mock_dashboard.return_value = dashboard

            bot = TradingBot(config_dir=str(tmp_path))
            bot._data_dir = tmp_path
            bot._warmup = WarmupManager(warmup_hours=0)
            bot.dashboard = dashboard
            return bot

    @pytest.mark.asyncio
    async def test_update_initial_rsi(self, bot_with_loader):
        """Test initial RSI calculation"""
        bot = bot_with_loader

        mock_strategy = MagicMock()
        mock_strategy.symbols = ["AAPL"]
        mock_strategy.get_current_rsi = MagicMock(return_value=35.5)
        mock_strategy.get_dataframe = MagicMock(return_value=MagicMock(
            __len__=MagicMock(return_value=50)
        ))
        mock_strategy.market = MagicMock()
        mock_strategy.market.value = "NASDAQ"

        mock_engine = MagicMock()
        mock_engine.get_strategies.return_value = [mock_strategy]
        bot.strategy_engine = mock_engine

        await bot.update_initial_rsi()

        bot.dashboard.update_rsi.assert_called()

    @pytest.mark.asyncio
    async def test_load_equity_history(self, bot_with_loader):
        """Test loading equity history"""
        bot = bot_with_loader

        mock_history = [
            {"timestamp": "2024-01-01", "total_krw": 1000000},
            {"timestamp": "2024-01-02", "total_krw": 1010000},
        ]

        bot.storage = AsyncMock()
        bot.storage.get_equity_history = AsyncMock(return_value=mock_history)

        await bot.load_equity_history()

        assert bot.dashboard.equity_history == mock_history

    @pytest.mark.asyncio
    async def test_load_fills(self, bot_with_loader):
        """Test loading fills"""
        bot = bot_with_loader

        mock_fill = MagicMock()
        mock_fill.order_id = "123"
        mock_fill.symbol = "AAPL"
        mock_fill.market = MagicMock()
        mock_fill.market.value = "NASDAQ"
        mock_fill.side = MagicMock()
        mock_fill.side.value = "buy"
        mock_fill.quantity = 10
        mock_fill.price = Decimal("150")
        mock_fill.commission = Decimal("1")
        mock_fill.pnl = None
        mock_fill.timestamp = None

        bot.storage = AsyncMock()
        bot.storage.get_all_fills = AsyncMock(return_value=[mock_fill])

        await bot.load_fills()

        assert len(bot.dashboard.fills) == 1
        bot.dashboard.calculate_performance.assert_called_once()
