"""Core TradingBot class"""

import asyncio
import importlib
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional
import logging

from src.core.events import EventBus
from src.core.types import Market
from src.broker.kis.client import KISBroker
from src.data.storage import Storage
from src.strategy.engine import StrategyEngine
from src.execution.order_manager import OrderManager
from src.risk.manager import RiskManager
from src.risk.limits import RiskLimits
from src.utils.config import Config
from src.utils.scheduler import TradingScheduler
from src.utils.notifier import DiscordNotifier
from src.web.app import get_dashboard_state

from src.bot.warmup import WarmupManager
from src.bot.tick_handler import TickHandlerMixin
from src.bot.dashboard_sync import DashboardSyncMixin
from src.bot.data_loader import DataLoaderMixin

logger = logging.getLogger(__name__)


class TradingBot(TickHandlerMixin, DashboardSyncMixin, DataLoaderMixin):
    """Main trading bot application"""

    def __init__(self, config_dir: str = "config", warmup_hours: int = 0):
        self.config = Config(config_dir)
        self.event_bus = EventBus()

        self.storage: Optional[Storage] = None
        self.broker: Optional[KISBroker] = None
        self.strategy_engine: Optional[StrategyEngine] = None
        self.order_manager: Optional[OrderManager] = None
        self.risk_manager: Optional[RiskManager] = None
        self.scheduler: Optional[TradingScheduler] = None
        self.notifier: Optional[DiscordNotifier] = None
        self.dashboard = get_dashboard_state()

        self._running = False
        self._stopped = False

        # Project paths
        self._project_root = Path(__file__).parent.parent.parent
        self._data_dir = self._project_root / "data"

        # Warmup management
        self._warmup = WarmupManager(warmup_hours, self._data_dir)

        # Equity snapshot settings
        self._equity_save_interval = 5  # Every 5 ticks (5 minutes)
        self._equity_save_counter = 0

    async def initialize(self) -> None:
        """Initialize all components"""
        logger.info("Initializing Trading Bot...")

        self.config.load()

        # Load warmup hours from config if not set via CLI
        if self._warmup.warmup_hours == 0:
            self._warmup.warmup_hours = self.config.get("trading.warmup_hours", 0)
            logger.info(f"Warmup hours loaded from config: {self._warmup.warmup_hours}")

        # Ensure data directory exists
        self._data_dir.mkdir(exist_ok=True)

        # Initialize storage
        db_url = self.config.get("database.url", "sqlite+aiosqlite:///data/trading.db")
        self.storage = Storage(db_url)
        await self.storage.initialize()

        # Wire storage and config to dashboard for web API access
        self.dashboard.storage = self.storage
        self.dashboard.db_url = db_url

        # Share KIS config and auth token for symbol validation
        kis_config = self.config.get_kis_config()
        self.dashboard.kis_config = {
            "app_key": kis_config["app_key"],
            "app_secret": kis_config["app_secret"],
            "paper_trading": kis_config["paper_trading"],
        }

        # Initialize risk manager
        risk_config = self.config.get_risk_config()
        limits = RiskLimits.from_config(risk_config)
        self.risk_manager = RiskManager(
            limits=limits,
            account_value=Decimal("100000000"),
        )

        # Initialize broker and share auth token
        await self._initialize_broker()
        if self.broker and self.broker._auth._access_token:
            self.dashboard.kis_auth_token = (
                self.broker._auth._access_token
            )

        # Initialize notifier
        self._initialize_notifier()

        # Initialize strategy engine
        self.strategy_engine = StrategyEngine(
            event_bus=self.event_bus,
            broker=self.broker,
            notifier=self.notifier,
        )
        await self._load_strategies()

        # Share strategy instances for live param updates
        self.dashboard.strategy_instances = (
            self.strategy_engine.get_strategies()
        )

        # Initialize order manager
        self.order_manager = OrderManager(
            event_bus=self.event_bus,
            broker=self.broker,
            risk_manager=self.risk_manager,
            storage=self.storage,
            is_trading_enabled=self._warmup.is_complete,
            notifier=self.notifier,
        )

        # Initialize scheduler
        self.scheduler = TradingScheduler(interval_seconds=60, us_only=True)
        self.scheduler.add_callback(self.on_tick)

        logger.info("Trading Bot initialized successfully")

    async def _initialize_broker(self) -> None:
        """Initialize broker connection"""
        kis_config = self.config.get_kis_config()
        self.broker = KISBroker(
            event_bus=self.event_bus,
            app_key=kis_config["app_key"],
            app_secret=kis_config["app_secret"],
            account_no=kis_config["account_no"],
            paper_trading=kis_config["paper_trading"],
        )
        await self.broker.connect()

        # Get account balances
        try:
            krw_balance = await self.broker.get_account_balance(Market.KRX)
            logger.debug(f"KRW balance response: {krw_balance}")
            self.dashboard.balance_krw = krw_balance.get("total_eval", Decimal("0"))
            self.dashboard.cash_krw = krw_balance.get("cash", Decimal("0"))
            self.dashboard.pnl_krw = krw_balance.get("profit_loss", Decimal("0"))

            try:
                usd_balance = await self.broker.get_account_balance(Market.NASDAQ)
                logger.debug(f"USD balance response: {usd_balance}")
                self.dashboard.balance_usd = usd_balance.get("total_eval", Decimal("0"))
                self.dashboard.cash_usd = usd_balance.get("cash", Decimal("0"))
                self.dashboard.pnl_usd = usd_balance.get("profit_loss", Decimal("0"))
            except Exception as e:
                logger.warning(f"Failed to get USD balance: {e}")

            total_eval = krw_balance.get("total_eval", Decimal("100000000"))
            self.risk_manager.update_account_value(total_eval)

            logger.info(
                f"Account balance - KRW: {self.dashboard.balance_krw:,.0f}, "
                f"USD: {self.dashboard.balance_usd:,.2f}"
            )
        except Exception as e:
            logger.warning(f"Failed to get account balance: {e}")

    def _initialize_notifier(self) -> None:
        """Initialize Discord notifier"""
        discord_config = self.config.get_discord_config()
        if discord_config.get("enabled") and discord_config.get("webhook_url"):
            self.notifier = DiscordNotifier(
                webhook_url=discord_config["webhook_url"],
                enabled=True,
            )
            logger.info("Discord notifications enabled")
        else:
            self.notifier = DiscordNotifier(enabled=False)
            logger.info("Discord notifications disabled")

    async def _load_strategies(self) -> None:
        """Load strategies from config, using DB symbols if available"""
        from collections import defaultdict

        # Check if DB has watched symbols
        db_symbols = await self.storage.get_watched_symbols(
            enabled_only=True,
        )

        if not db_symbols:
            # Seed DB from config on first run
            count = await self.storage.seed_watched_symbols(
                self.config.strategies,
            )
            logger.info(f"Seeded {count} symbols from config to DB")
            db_symbols = await self.storage.get_watched_symbols(
                enabled_only=True,
            )

        # Group DB symbols by strategy_name
        symbols_by_strategy = defaultdict(list)
        for s in db_symbols:
            symbols_by_strategy[s["strategy_name"]].append(
                s["symbol"]
            )

        # Store strategy names for web UI
        self.dashboard.strategy_names = [
            s["name"]
            for s in self.config.strategies
            if s.get("enabled")
        ]

        # Seed strategy params on first run
        all_db_params = await self.storage.get_all_strategy_params()
        if not all_db_params:
            count = await self.storage.seed_strategy_params(
                self.config.strategies,
            )
            logger.info(
                f"Seeded {count} strategy params to DB"
            )
            all_db_params = (
                await self.storage.get_all_strategy_params()
            )

        # Build params lookup by strategy name
        db_params_map = {
            p["strategy_name"]: p["params"]
            for p in all_db_params
        }

        for strategy_config in self.config.strategies:
            if not strategy_config.get("enabled"):
                continue

            try:
                class_path = strategy_config["class"]
                module_path, class_name = class_path.rsplit(".", 1)
                module = importlib.import_module(module_path)
                strategy_class = getattr(module, class_name)

                market_map = {
                    "krx": Market.KRX,
                    "nyse": Market.NYSE,
                    "nasdaq": Market.NASDAQ,
                    "amex": Market.AMEX,
                }
                market = market_map.get(
                    strategy_config["market"].lower(), Market.KRX,
                )

                # Use DB symbols if available, fallback to config
                name = strategy_config["name"]
                symbols = symbols_by_strategy.get(
                    name, strategy_config["symbols"],
                )

                if not symbols:
                    logger.warning(
                        f"No symbols for {name}, skipping"
                    )
                    continue

                # Use DB params if available, fallback to config
                params = db_params_map.get(
                    name,
                    strategy_config.get("params", {}),
                )

                strategy = strategy_class(
                    symbols=symbols,
                    market=market,
                    params=params,
                )

                self.strategy_engine.register_strategy(strategy)
                logger.info(
                    f"Strategy loaded: {name} "
                    f"({len(symbols)} symbols)"
                )

            except Exception as e:
                logger.error(
                    f"Failed to load strategy "
                    f"{strategy_config['name']}: {e}"
                )

    async def start(self) -> None:
        """Start the bot"""
        logger.info("Starting Trading Bot...")
        self._running = True

        self._warmup.start()

        # Start components
        await self.event_bus.start()
        await self.strategy_engine.start()
        await self.order_manager.start()

        # Initialize strategies with historical data
        await self.strategy_engine.initialize()

        # Sync existing positions from broker
        await self.strategy_engine.sync_positions()

        # Load historical data
        await self.load_chart_history()
        await self.load_equity_history()
        await self.load_fills()

        # Calculate initial RSI values
        await self.update_initial_rsi()

        # Start scheduler
        await self.scheduler.start()

        logger.info("Trading Bot started")
        logger.info(f"Paper trading: {self.broker.paper_trading}")
        strategy_names = [s.name for s in self.strategy_engine.get_strategies()]
        logger.info(f"Strategies: {strategy_names}")
        if self._warmup.warmup_hours > 0:
            logger.info(
                f"Warmup period: {self._warmup.warmup_hours} hours (trading after warmup)"
            )
        logger.info(f"Check interval: {self.scheduler.interval} seconds")

        # Send startup notification
        if self.notifier:
            await self.notifier.notify_startup(
                strategies=strategy_names,
                paper_trading=self.broker.paper_trading,
            )

        self.update_dashboard_status()

    async def stop(self) -> None:
        """Stop the bot"""
        if self._stopped:
            return
        self._stopped = True

        logger.info("Stopping Trading Bot...")
        self._running = False

        if self.notifier:
            await self.notifier.notify_shutdown()

        if self.scheduler:
            await self.scheduler.stop()

        if self.order_manager:
            await self.order_manager.stop()

        if self.strategy_engine:
            await self.strategy_engine.stop()

        await self.event_bus.stop()

        if self.broker:
            await self.broker.disconnect()

        if self.storage:
            await self.storage.close()

        if self.notifier:
            await self.notifier.close()

        logger.info("Trading Bot stopped")

    async def run(self) -> None:
        """Main run loop"""
        await self.initialize()
        await self.start()

        while self._running:
            await asyncio.sleep(1)
