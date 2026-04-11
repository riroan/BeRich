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
        self._status_task: Optional[asyncio.Task] = None
        self._bot_start_time: Optional[datetime] = None

        # Project paths
        self._project_root = Path(__file__).parent.parent.parent
        self._data_dir = self._project_root / "data"

        # Warmup management
        self._warmup = WarmupManager(warmup_hours)

        # Equity snapshot settings
        self._equity_save_interval = 5  # Every 5 ticks (5 minutes)
        self._equity_save_counter = 0

    async def initialize(self) -> None:
        """Initialize all components"""
        logger.info("Initializing Trading Bot...")

        self.config.load()

        # Load warmup hours from config if not set via CLI
        # Paper mode skips warmup
        kis_config = self.config.get_kis_config()
        if kis_config.get("paper_trading"):
            self._warmup.warmup_hours = 0
            logger.info("Paper mode: warmup disabled")
        elif self._warmup.warmup_hours == 0:
            self._warmup.warmup_hours = self.config.get("trading.warmup_hours", 0)
            logger.info(f"Warmup hours loaded from config: {self._warmup.warmup_hours}")

        # Ensure data directory exists
        self._data_dir.mkdir(exist_ok=True)

        # Initialize storage
        db_url = self.config.get("database.url", "sqlite+aiosqlite:///data/trading.db")
        self.storage = Storage(db_url)
        await self.storage.initialize()

        # Wire storage to warmup manager
        self._warmup.set_storage(self.storage)

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

        # Initialize broker first (needed for account balance)
        await self._initialize_broker()

        # FIX-005: Use actual account balance instead of hardcoded 100M
        risk_config = self.config.get_risk_config()
        limits = RiskLimits.from_config(risk_config)
        try:
            balances = await self.broker.get_account_balance()
            account_value = Decimal(str(
                balances.get("total_usd", 0) + balances.get("total_krw", 0)
            ))
            if account_value <= 0:
                account_value = Decimal("100000000")
                logger.warning("Account balance is 0, using fallback 100M for risk limits")
        except Exception as e:
            account_value = Decimal("100000000")
            logger.warning(f"Failed to get account balance, using fallback 100M: {e}")
        self.risk_manager = RiskManager(
            limits=limits,
            account_value=account_value,
        )
        logger.info(f"Risk manager initialized with account value: {account_value:,}")
        real_broker = getattr(
            self.broker, "_real_broker", self.broker,
        )
        if hasattr(real_broker, "_auth") and real_broker._auth._access_token:
            self.dashboard.kis_auth_token = (
                real_broker._auth._access_token
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

        # Wire reload callback for web API hot reload
        self.dashboard.reload_callback = (
            self.reload_strategies
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
        real_broker = KISBroker(
            event_bus=self.event_bus,
            app_key=kis_config["app_key"],
            app_secret=kis_config["app_secret"],
            account_no=kis_config["account_no"],
            paper_trading=kis_config["paper_trading"],
        )

        # Use PaperBroker if KIS_PAPER_TRADING=true
        if kis_config["paper_trading"]:
            from src.broker.paper import PaperBroker
            initial_cash_usd = Decimal(
                str(self.config.get("trading.paper_cash_usd", 10000))
            )
            initial_cash_krw = Decimal(
                str(self.config.get("trading.paper_cash_krw", 0))
            )
            self.broker = PaperBroker(
                event_bus=self.event_bus,
                real_broker=real_broker,
                initial_cash_usd=initial_cash_usd,
                initial_cash_krw=initial_cash_krw,
            )
            logger.info(
                f"Paper trading mode | "
                f"USD: ${initial_cash_usd:,.0f}, "
                f"KRW: {initial_cash_krw:,.0f}"
            )
        else:
            self.broker = real_broker

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
        """Load strategies from DB (strategy_configs table)"""
        configs = await self.storage.get_all_strategy_configs()

        if not configs:
            logger.info(
                "No strategies in DB. "
                "Create them via web UI."
            )
            self.dashboard.strategy_names = []
            return

        self.dashboard.strategy_names = [
            c["name"] for c in configs if c["enabled"]
        ]

        for cfg in configs:
            if not cfg["enabled"]:
                continue
            self._register_strategy_from_config(cfg)

    def _register_strategy_from_config(self, cfg: dict) -> None:
        """Create and register a strategy instance from DB config"""
        try:
            class_path = cfg["class_path"]
            module_path, class_name = class_path.rsplit(".", 1)
            module = importlib.import_module(module_path)
            strategy_class = getattr(module, class_name)

            market = Market.from_string(cfg["market"])

            # Extract symbol strings from objects
            symbols = [
                s["symbol"] if isinstance(s, dict) else s
                for s in cfg["symbols"]
            ]

            if not symbols:
                logger.warning(
                    f"No symbols for {cfg['name']}, skipping"
                )
                return

            strategy = strategy_class(
                symbols=symbols,
                market=market,
                params=cfg["params"],
            )

            self.strategy_engine.register_strategy(strategy)
            logger.info(
                f"Strategy loaded: {cfg['name']} "
                f"({len(symbols)} symbols)"
            )

        except Exception as e:
            logger.error(
                f"Failed to load strategy "
                f"{cfg['name']}: {e}"
            )

    async def reload_strategies(self) -> None:
        """Hot reload: rebuild strategies from DB (atomic swap)"""
        configs = await self.storage.get_all_strategy_configs()

        # Build comparison key for incremental reload
        old_strategies = {
            s.name_with_market: s
            for s in self.strategy_engine.get_strategies()
        }

        new_list = []
        new_names = []

        for cfg in configs:
            if not cfg["enabled"]:
                continue

            name = cfg["name"]
            new_names.append(name)

            # Check if unchanged — reuse existing instance
            old = old_strategies.get(name)
            if old and self._strategy_unchanged(old, cfg):
                new_list.append(old)
                continue

            # Changed or new — create fresh instance
            try:
                class_path = cfg["class_path"]
                module_path, class_name = class_path.rsplit(
                    ".", 1,
                )
                module = importlib.import_module(module_path)
                strategy_class = getattr(module, class_name)
                market = Market.from_string(cfg["market"])

                symbols = [
                    s["symbol"] if isinstance(s, dict) else s
                    for s in cfg["symbols"]
                ]

                strategy = strategy_class(
                    symbols=symbols,
                    market=market,
                    params=cfg["params"],
                )

                # Initialize new/changed strategy
                try:
                    historical_bars = {}
                    for symbol in symbols:
                        import asyncio
                        await asyncio.sleep(0.5)
                        bars = (
                            await self.broker.get_historical_bars(
                                symbol=symbol,
                                market=market,
                                days=strategy.required_history,
                            )
                        )
                        historical_bars[symbol] = bars
                    strategy.initialize(historical_bars)
                except Exception as e:
                    logger.warning(
                        f"[{name}] Init failed: {e}"
                    )

                new_list.append(strategy)
                logger.info(f"Strategy reloaded: {name}")

            except Exception as e:
                logger.error(
                    f"Failed to reload strategy {name}: {e}"
                )

        # Atomic swap
        self.strategy_engine._strategies = new_list

        # Update dashboard
        self.dashboard.strategy_names = new_names
        self.dashboard.strategy_instances = new_list

        logger.info(
            f"Strategies reloaded: {len(new_list)} active"
        )

    def _strategy_unchanged(
        self, strategy, cfg: dict,
    ) -> bool:
        """Check if a strategy config matches the running instance"""
        import json
        symbols = [
            s["symbol"] if isinstance(s, dict) else s
            for s in cfg["symbols"]
        ]
        return (
            sorted(strategy.symbols) == sorted(symbols)
            and strategy.params == cfg["params"]
            and strategy.market == Market.from_string(
                cfg["market"],
            )
        )

    async def start(self) -> None:
        """Start the bot"""
        logger.info("Starting Trading Bot...")
        self._running = True
        self._bot_start_time = datetime.now()

        await self._warmup.start()

        # Start components
        await self.event_bus.start()
        await self.strategy_engine.start()
        await self.order_manager.start()

        # FIX-004: Sync positions BEFORE initialize so strategies know existing positions
        await self.strategy_engine.sync_positions()

        # Initialize strategies with historical data
        await self.strategy_engine.initialize()

        # Load historical data
        await self.load_chart_history()
        await self.load_equity_history()
        await self.load_fills()

        # Calculate initial RSI values
        await self.update_initial_rsi()

        # Start scheduler
        await self.scheduler.start()

        # Dashboard status updater runs regardless of market hours (for warmup countdown)
        self._status_task = asyncio.create_task(self._status_loop())

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

        await self.update_dashboard_status()

    async def stop(self) -> None:
        """Stop the bot"""
        if self._stopped:
            return
        self._stopped = True

        logger.info("Stopping Trading Bot...")
        self._running = False

        if self.notifier:
            await self.notifier.notify_shutdown()

        if self._status_task:
            self._status_task.cancel()
            try:
                await self._status_task
            except asyncio.CancelledError:
                pass

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

    async def _status_loop(self) -> None:
        """Update dashboard status every 60s regardless of market hours (keeps warmup countdown live).
        Exits automatically once warmup is complete — market-hours tick handler takes over."""
        while self._running:
            try:
                await asyncio.sleep(60)
                if not self._running:
                    break
                await self.update_dashboard_status()
                await self.broadcast_tick_update()
                # Stop looping once warmup is done; on_tick() handles updates during market hours
                if await self._warmup.is_complete():
                    break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Status loop error: {e}")

    async def run(self) -> None:
        """Main run loop"""
        await self.initialize()
        await self.start()

        while self._running:
            await asyncio.sleep(1)
