"""Tests for TradingBot core"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime
from decimal import Decimal

from src.bot.core import TradingBot
from src.bot.warmup import WarmupManager
from src.core.types import Market, Position


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

    @pytest.mark.asyncio
    async def test_confirm_poll_triggers_on_regular_to_after(self, bot):
        """REGULAR→AFTER transition spawns the daily-bar confirm poll once."""
        from src.utils.scheduler import Session

        bot._last_session = Session.REGULAR
        bot._run_daily_confirm_poll = AsyncMock()

        with patch("src.bot.core.get_current_session", return_value=Session.AFTER):
            await bot._handle_session_transition()
            assert bot._confirm_poll_task is not None
            await bot._confirm_poll_task  # let the spawned task run

        bot._run_daily_confirm_poll.assert_awaited_once()
        assert bot._last_session == Session.AFTER

    @pytest.mark.asyncio
    async def test_confirm_poll_no_trigger_without_transition(self, bot):
        """No transition (AFTER→AFTER) → no poll."""
        from src.utils.scheduler import Session

        bot._last_session = Session.AFTER
        bot._run_daily_confirm_poll = AsyncMock()

        with patch("src.bot.core.get_current_session", return_value=Session.AFTER):
            await bot._handle_session_transition()

        bot._run_daily_confirm_poll.assert_not_called()

    @pytest.mark.asyncio
    async def test_session_transition_cancels_stale_stop_losses(self, bot):
        """Entering a new tradable session re-prices stale stop-losses (#7)."""
        from src.utils.scheduler import Session

        bot._last_session = Session.PRE
        bot._run_daily_confirm_poll = AsyncMock()
        bot.order_manager = AsyncMock()

        with patch("src.bot.core.get_current_session", return_value=Session.REGULAR):
            await bot._handle_session_transition()

        bot.order_manager.cancel_unfilled_stop_losses.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_stop_loss_cancel_without_transition(self, bot):
        """No session change → no stop-loss cancel."""
        from src.utils.scheduler import Session

        bot._last_session = Session.REGULAR
        bot.order_manager = AsyncMock()

        with patch("src.bot.core.get_current_session", return_value=Session.REGULAR):
            await bot._handle_session_transition()

        bot.order_manager.cancel_unfilled_stop_losses.assert_not_called()

    @pytest.mark.asyncio
    async def test_confirm_poll_slides_base(self, bot):
        """The poll folds the latest confirmed bar into each symbol."""
        latest_bar = MagicMock()
        strategy = MagicMock()
        strategy.symbols = ["AAPL"]
        strategy.market = MagicMock()
        strategy.confirm_daily_bar = MagicMock(return_value="appended")

        bot.strategy_engine = MagicMock()
        bot.strategy_engine.get_strategies.return_value = [strategy]
        bot.broker = AsyncMock()
        bot.broker.get_historical_bars = AsyncMock(return_value=[latest_bar])

        with patch("src.bot.core.asyncio.sleep", new=AsyncMock()):
            await bot._run_daily_confirm_poll()

        strategy.confirm_daily_bar.assert_called_once_with("AAPL", latest_bar)

    @pytest.mark.asyncio
    async def test_sync_enabled_symbols_adds_symbol(self, bot):
        """A symbol added to the DB config is reconciled into the running
        strategy (the mechanism behind market-independent sync)."""
        strategy = MagicMock()
        strategy.name_with_market = "NASDAQ_RSI_MeanReversion"
        strategy.symbols = ["AAPL"]
        strategy.market = MagicMock()
        strategy.initialize = MagicMock()

        bot.strategy_engine = MagicMock()
        bot.strategy_engine.get_strategies.return_value = [strategy]
        bot.storage = AsyncMock()
        bot.storage.get_all_strategy_configs = AsyncMock(return_value=[{
            "name": "NASDAQ_RSI_MeanReversion", "enabled": True,
            "symbols": [{"symbol": "AAPL"}, {"symbol": "MSFT"}],
            "market": "NASDAQ",
        }])
        bot.broker = AsyncMock()
        bot.broker.get_historical_bars = AsyncMock(return_value=["bar"])
        bot.dashboard = MagicMock()

        with patch("src.bot.tick_handler.asyncio.sleep", new=AsyncMock()):
            await bot._sync_enabled_symbols()

        assert set(strategy.symbols) == {"AAPL", "MSFT"}
        strategy.initialize.assert_called_once()  # bars loaded for MSFT

    @pytest.mark.asyncio
    async def test_sync_enabled_symbols_removes_symbol(self, bot):
        """A symbol removed from the DB config is dropped from the strategy."""
        strategy = MagicMock()
        strategy.name_with_market = "NASDAQ_RSI_MeanReversion"
        strategy.symbols = ["AAPL", "MSFT"]
        strategy.market = MagicMock()

        bot.strategy_engine = MagicMock()
        bot.strategy_engine.get_strategies.return_value = [strategy]
        bot.storage = AsyncMock()
        bot.storage.get_all_strategy_configs = AsyncMock(return_value=[{
            "name": "NASDAQ_RSI_MeanReversion", "enabled": True,
            "symbols": [{"symbol": "AAPL"}], "market": "NASDAQ",
        }])
        bot.broker = AsyncMock()
        bot.dashboard = MagicMock()
        bot.dashboard.rsi_values = {"MSFT": 50}
        bot.dashboard.rsi_prices = {"MSFT": 100}

        with patch("src.bot.tick_handler.asyncio.sleep", new=AsyncMock()):
            await bot._sync_enabled_symbols()

        assert set(strategy.symbols) == {"AAPL"}

    @pytest.mark.asyncio
    async def test_config_sync_loop_runs_regardless_of_market(self, bot):
        """Regression: symbol reconciliation runs via an always-on loop that
        never consults is_market_open() — so edits apply even when the market
        is closed (previously they waited for the next session or a restart)."""
        bot._running = True

        async def _sync_then_stop():
            bot._running = False  # break the loop after one iteration

        bot._sync_enabled_symbols = AsyncMock(side_effect=_sync_then_stop)

        with patch("src.bot.core.asyncio.sleep", new=AsyncMock()):
            await bot._config_sync_loop()

        bot._sync_enabled_symbols.assert_awaited_once()


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

    def test_restore_strategy_stage_state_from_positions(self, bot_with_dashboard):
        """DB-backed current positions restore only strategy stage counters."""
        bot = bot_with_dashboard

        db_position = MagicMock()
        db_position.market = "NASDAQ"
        db_position.quantity = 1
        db_position.buy_stage = 2
        db_position.sell_stage = 1
        db_position.last_buy_date = "2026-06-20T09:30:00"
        db_position.last_sell_date = "2026-06-21T10:45:00"
        bot.dashboard.positions = {"AAPL": db_position}

        mock_strategy = MagicMock()
        mock_strategy.market = Market.NASDAQ
        mock_strategy.symbols = ["AAPL"]
        mock_strategy._positions = {"AAPL": 2}
        mock_strategy._entry_prices = {"AAPL": Decimal("120")}
        mock_strategy._buy_stages = {"AAPL": 1}
        mock_strategy._sell_stages = {"AAPL": 0}
        mock_strategy._last_buy_time = {}
        mock_strategy._last_sell_time = {}

        mock_engine = MagicMock()
        mock_engine.get_strategies.return_value = [mock_strategy]
        bot.strategy_engine = mock_engine

        bot.restore_strategy_stage_state_from_positions()

        assert mock_strategy._buy_stages["AAPL"] == 2
        assert mock_strategy._sell_stages["AAPL"] == 1
        assert (
            mock_strategy._last_buy_time["AAPL"].isoformat()
            == "2026-06-20T09:30:00"
        )
        assert (
            mock_strategy._last_sell_time["AAPL"].isoformat()
            == "2026-06-21T10:45:00"
        )
        assert mock_strategy._positions["AAPL"] == 2
        assert mock_strategy._entry_prices["AAPL"] == Decimal("120")

    @pytest.mark.asyncio
    async def test_update_dashboard_status(self, bot_with_dashboard):
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

        bot._warmup.is_complete = AsyncMock(return_value=True)
        bot._warmup.get_remaining_str = AsyncMock(return_value=None)
        bot._warmup._start_time = datetime.now()

        await bot.update_dashboard_status()

        bot.dashboard.set_bot_status.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_market_positions_returns_dashboard_records(
        self,
        bot_with_dashboard,
    ):
        """Broker positions should be normalized for DB-backed dashboard state."""
        bot = bot_with_dashboard
        bot.dashboard.rsi_values = {"AAPL": 42.5}
        bot.broker = AsyncMock()
        bot.broker.get_positions = AsyncMock(return_value=[
            Position(
                symbol="AAPL",
                market=Market.NASDAQ,
                quantity=2,
                avg_entry_price=Decimal("100"),
                current_price=Decimal("110"),
                unrealized_pnl=Decimal("20"),
            ),
        ])

        records = await bot._update_market_positions(
            Market.NASDAQ,
            {
                "AAPL": {
                    "buy_stage": 1,
                    "sell_stage": 0,
                    "max_buy_stages": 3,
                    "max_sell_stages": 3,
                    "last_buy_time": datetime(2024, 1, 2, 3, 4),
                    "stop_loss_pct": -8.0,
                },
            },
        )

        assert records is not None
        assert records[0]["symbol"] == "AAPL"
        assert records[0]["market"] == "NASDAQ"
        assert records[0]["pnl"] == 20.0
        assert records[0]["pnl_pct"] == 10.0
        assert records[0]["stop_loss_distance"] == 18.0
        assert records[0]["rsi"] == 42.5


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
    async def test_load_current_positions(self, bot_with_loader):
        """Test loading current positions."""
        bot = bot_with_loader
        mock_positions = [
            {
                "symbol": "AAPL",
                "market": "NASDAQ",
                "quantity": 2,
                "avg_price": 100,
                "current_price": 110,
                "pnl": 20,
                "pnl_pct": 10,
            },
        ]

        bot.storage = AsyncMock()
        bot.storage.get_current_positions = AsyncMock(return_value=mock_positions)

        await bot.load_current_positions()

        bot.dashboard.replace_positions_from_records.assert_called_once_with(
            mock_positions,
        )

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
        fill_time = datetime(2024, 1, 2, 3, 4, 5)
        mock_fill.timestamp = fill_time
        mock_fill.reason = None
        mock_fill.rsi = 28.4

        bot.storage = AsyncMock()
        bot.storage.get_all_fills = AsyncMock(return_value=[mock_fill])

        await bot.load_fills()

        assert len(bot.dashboard.fills) == 1
        assert bot.dashboard.fills[0]["timestamp"] == "2024-01-02T03:04:05"
        kwargs = bot.dashboard.add_trade_log.call_args.kwargs
        assert kwargs["timestamp"] == fill_time
        bot.dashboard.calculate_performance.assert_called_once()

    @pytest.mark.asyncio
    async def test_load_fills_restores_partial_sell_label(self, bot_with_loader):
        """Option 2: persisted reason restores partial_sell label + pnl on
        reload (previously a reloaded partial sell became plain 'sell')."""
        bot = bot_with_loader

        f = MagicMock()
        f.order_id = "O1"
        f.symbol = "BAC"
        f.market = MagicMock()
        f.market.value = "NYSE"
        f.side = MagicMock()
        f.side.value = "sell"
        f.quantity = 1
        f.price = Decimal("55.77")
        f.commission = Decimal("0")
        f.pnl = Decimal("6.27")
        f.rsi = 74.1
        f.reason = "staged_sell_1"
        f.timestamp = None

        bot.storage = AsyncMock()
        bot.storage.get_all_fills = AsyncMock(return_value=[f])

        await bot.load_fills()

        kwargs = bot.dashboard.add_trade_log.call_args.kwargs
        assert kwargs["action"] == "partial_sell"
        assert kwargs["pnl"] == 6.27

    @pytest.mark.asyncio
    async def test_load_fills_preserves_zero_pnl(self, bot_with_loader):
        """Break-even fills are real trades and must not become NULL."""
        bot = bot_with_loader

        f = MagicMock()
        f.order_id = "O2"
        f.symbol = "BAC"
        f.market = MagicMock()
        f.market.value = "NYSE"
        f.side = MagicMock()
        f.side.value = "sell"
        f.quantity = 1
        f.price = Decimal("55.00")
        f.commission = Decimal("0")
        f.pnl = Decimal("0")
        f.rsi = 65.0
        f.reason = None
        f.timestamp = datetime(2024, 1, 2, 3, 4, 5)

        bot.storage = AsyncMock()
        bot.storage.get_all_fills = AsyncMock(return_value=[f])

        await bot.load_fills()

        assert bot.dashboard.fills[0]["pnl"] == 0.0
        kwargs = bot.dashboard.add_trade_log.call_args.kwargs
        assert kwargs["pnl"] == 0.0
        assert kwargs["pnl_pct"] == 0.0
