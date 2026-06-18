"""Core TradingBot class"""

import asyncio
from datetime import datetime
from decimal import Decimal
from pathlib import Path
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
from src.utils.scheduler import TradingScheduler, Session, get_current_session
from src.utils.notifier import DiscordNotifier
from src.web.app import get_dashboard_state

from src.bot._utils import build_strategy, extract_symbols
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

        self.storage: Storage | None = None
        self.broker: KISBroker | None = None
        self.strategy_engine: StrategyEngine | None = None
        self.order_manager: OrderManager | None = None
        self.risk_manager: RiskManager | None = None
        self.scheduler: TradingScheduler | None = None
        self.notifier: DiscordNotifier | None = None
        self.dashboard = get_dashboard_state()

        self._running = False
        self._stopped = False
        self._status_task: asyncio.Task | None = None
        self._bot_start_time: datetime | None = None

        # Project paths
        self._project_root = Path(__file__).parent.parent.parent
        self._data_dir = self._project_root / "data"

        # Warmup management
        self._warmup = WarmupManager(warmup_hours)

        # Equity snapshot settings
        self._equity_save_interval = 5  # Every 5 ticks (5 minutes)
        self._equity_save_counter = 0

        # Daily-bar confirmation poll (RSI base slide on regular close)
        self._last_session: Session | None = None
        self._confirm_poll_task: asyncio.Task | None = None
        self._confirm_poll_date = None

        # Config-sync loop: reconcile strategy symbols from the DB
        # regardless of market hours (the web UI runs in a separate
        # thread/loop and reaches the bot only through the shared DB).
        self._config_sync_task: asyncio.Task | None = None
        self._config_sync_interval = 15  # seconds

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

        # US-only bot: risk equity = USD overseas total eval, already
        # fetched into the dashboard by _initialize_broker above. If it's
        # 0 we FAIL SAFE — the equity-pct risk gates then reject every
        # order — rather than fall back to a phantom KRW figure that
        # silently disables all risk limits.
        risk_config = self.config.get_risk_config()
        limits = RiskLimits.from_config(risk_config)
        account_value = self.dashboard.balance_usd or Decimal("0")
        if account_value <= 0:
            logger.critical(
                "Account value is 0 — risk limits will reject ALL orders "
                "until a valid USD balance is available",
            )
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
            hts_id=kis_config.get("hts_id", ""),
            slippage_buffer=float(
                self.config.get("trading.slippage_buffer_pct", 0.01)
            ),
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

        # Get account balances (populates dashboard.balance_usd/krw;
        # risk_manager equity is derived from balance_usd after this).
        try:
            await self._fetch_and_apply_balance(Market.KRX)

            # USD balance drives risk equity — a zero here means every
            # order is rejected (fail-safe), so retry before giving up.
            for attempt in range(3):
                try:
                    await self._fetch_and_apply_balance(Market.NASDAQ)
                except Exception as e:
                    logger.warning(
                        f"USD balance fetch failed "
                        f"(attempt {attempt + 1}/3): {e}"
                    )
                if self.dashboard.balance_usd > 0:
                    break
                await asyncio.sleep(2)

            if self.dashboard.balance_usd <= 0:
                logger.critical(
                    "USD balance still unavailable after retries — risk "
                    "equity will be 0 and ALL orders rejected until a "
                    "balance is obtained on a later tick",
                )

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
            if not extract_symbols(cfg["symbols"]):
                logger.warning(
                    f"No symbols for {cfg['name']}, skipping"
                )
                return

            strategy = build_strategy(cfg)
            self.strategy_engine.register_strategy(strategy)
            logger.info(
                f"Strategy loaded: {cfg['name']} "
                f"({len(strategy.symbols)} symbols)"
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
            if (old := old_strategies.get(name)) and self._strategy_unchanged(old, cfg):
                new_list.append(old)
                continue

            # Changed or new — create fresh instance
            try:
                strategy = build_strategy(cfg)

                # Initialize new/changed strategy
                try:
                    historical_bars = {}
                    for symbol in strategy.symbols:
                        await asyncio.sleep(0.5)
                        bars = (
                            await self.broker.get_historical_bars(
                                symbol=symbol,
                                market=strategy.market,
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
        return (
            sorted(strategy.symbols) == sorted(extract_symbols(cfg["symbols"]))
            and strategy.params == cfg["params"]
            and strategy.market == Market.from_string(cfg["market"])
        )

    async def _reconcile_open_orders(self) -> None:
        """Reconcile orders left SUBMITTED/PARTIAL by a previous process.

        KISBroker._orders is memory-only, so without this an order that
        filled while the bot was down would stay SUBMITTED in the DB
        forever. Terminal-state changes are persisted here; the strategy
        position itself is restored separately by sync_positions().
        """
        try:
            open_orders = await self.storage.get_open_orders()
            if not open_orders:
                return
            logger.info(
                f"Reconciling {len(open_orders)} open order(s) from DB"
            )
            changed = await self.broker.reconcile_open_orders(open_orders)
            for order in changed:
                await self.storage.save_order(order)
        except Exception as e:
            logger.warning(f"Open-order reconciliation failed: {e!r}")

    async def _handle_session_transition(self) -> None:
        """React to US trading-session changes on each tick.

        - REGULAR→AFTER: kick the daily-bar confirmation poll once/day
          (RSI base slides only on KIS bar confirmation, not the clock).
        - Any change into a tradable session: cancel stale, unfilled
          stop-loss orders so the strategy re-emits them at the new
          session's price next tick (Phase 4 #7). Safe because the
          position reset is fill-driven — an unfilled stop-loss never
          dropped the position.
        """
        current = get_current_session(datetime.now())
        prev = self._last_session
        self._last_session = current

        if prev is None or prev == current:
            return

        if prev == Session.REGULAR and current == Session.AFTER:
            today = datetime.now().date()
            poll_running = (
                self._confirm_poll_task
                and not self._confirm_poll_task.done()
            )
            if self._confirm_poll_date != today and not poll_running:
                self._confirm_poll_date = today
                self._confirm_poll_task = asyncio.create_task(
                    self._run_daily_confirm_poll()
                )

        if current != Session.CLOSED and self.order_manager:
            try:
                await self.order_manager.cancel_unfilled_stop_losses()
            except Exception as e:
                logger.warning(f"Stale stop-loss cancel failed: {e}")

    async def _run_daily_confirm_poll(self) -> None:
        """After the regular session closes, poll KIS daily bars and slide
        each symbol's RSI base when its new confirmed bar appears.

        5-min interval, up to 30 min. A symbol whose new bar never appears
        keeps its previous-session base (no silent clock-based slide).
        """
        strategies = [
            s for s in self.strategy_engine.get_strategies()
            if hasattr(s, "confirm_daily_bar")
        ]
        pending = {
            (s, sym) for s in strategies for sym in s.symbols
        }
        if not pending:
            return
        logger.info(
            f"Daily-bar confirmation poll started ({len(pending)} symbols)"
        )

        for attempt in range(6):  # 6 × 5 min = 30 min
            done = set()
            for strategy, symbol in pending:
                try:
                    await asyncio.sleep(1)  # rate limit
                    bars = await self.broker.get_historical_bars(
                        symbol=symbol, market=strategy.market, days=5,
                    )
                    if not bars:
                        continue
                    if strategy.confirm_daily_bar(symbol, bars[-1]) == "appended":
                        done.add((strategy, symbol))
                except Exception as e:
                    logger.warning(f"[{symbol}] confirm poll error: {e}")
            pending -= done
            if not pending:
                break
            if attempt < 5:
                await asyncio.sleep(300)  # retry in 5 min

        if pending:
            names = sorted(sym for _, sym in pending)
            logger.warning(
                f"Daily-bar confirmation incomplete after 30min: {names} "
                f"— RSI base kept at previous session (no silent slide)"
            )
        else:
            logger.info("Daily-bar confirmation poll complete (all slid)")

    async def _config_sync_loop(self) -> None:
        """Reconcile strategy symbols from the DB regardless of market hours.

        The web UI runs in a separate thread with its own event loop and can
        reach the bot only through the shared DB. on_tick's reconciliation
        is gated behind is_market_open() (scheduler._run_loop), so symbol
        add/remove edits made while the market is closed never applied until
        the next session — only a restart (which reloads from the DB
        unconditionally) showed them. This loop polls every
        _config_sync_interval seconds so edits reflect within seconds,
        market open or not.
        """
        while self._running:
            try:
                await asyncio.sleep(self._config_sync_interval)
                if not self._running:
                    break
                await self._sync_enabled_symbols()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Config sync loop error: {e}")

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

        # Reconcile DB orders left open by a previous process before
        # sync_positions restores strategy state from the broker.
        await self._reconcile_open_orders()

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

        # Symbol reconciliation runs regardless of market hours so web-UI
        # symbol edits apply without a restart.
        self._config_sync_task = asyncio.create_task(self._config_sync_loop())

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

        if self._confirm_poll_task:
            self._confirm_poll_task.cancel()
            try:
                await self._confirm_poll_task
            except asyncio.CancelledError:
                pass

        if self._config_sync_task:
            self._config_sync_task.cancel()
            try:
                await self._config_sync_task
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
