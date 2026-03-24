#!/usr/bin/env python3
"""Main trading bot application"""

import asyncio
import signal
import sys
from pathlib import Path
from decimal import Decimal
from datetime import datetime, timedelta
import importlib

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.events import EventBus, Event, EventType
from src.core.types import Bar, Market
from src.broker.kis.client import KISBroker
from src.data.storage import Storage
from src.strategy.engine import StrategyEngine
from src.execution.order_manager import OrderManager
from src.risk.manager import RiskManager
from src.risk.limits import RiskLimits
from src.utils.config import Config
from src.utils.logger import setup_logger
from src.utils.scheduler import TradingScheduler
from src.utils.notifier import DiscordNotifier
from src.web.app import get_dashboard_state

logger = setup_logger("TradingBot")


class TradingBot:
    """Main trading bot application"""

    def __init__(self, config_dir: str = "config", warmup_hours: int = 0):
        self.config = Config(config_dir)
        self.event_bus = EventBus()

        self.storage = None
        self.broker = None
        self.strategy_engine = None
        self.order_manager = None
        self.risk_manager = None
        self.scheduler = None
        self.notifier = None
        self.dashboard = get_dashboard_state()

        self._running = False
        self._start_time: datetime = None
        self._warmup_hours = warmup_hours  # Hours to wait before trading

    async def initialize(self) -> None:
        """Initialize all components"""
        logger.info("Initializing Trading Bot...")

        # Load config
        self.config.load()

        # Load warmup hours from config if not set via CLI
        if self._warmup_hours == 0:
            self._warmup_hours = self.config.get("trading.warmup_hours", 0)

        # Initialize storage
        db_url = self.config.get("database.url", "sqlite+aiosqlite:///data/trading.db")

        # Ensure data directory exists
        Path("data").mkdir(exist_ok=True)

        self.storage = Storage(db_url)
        await self.storage.initialize()

        # Initialize risk manager
        risk_config = self.config.get_risk_config()
        limits = RiskLimits.from_config(risk_config)
        self.risk_manager = RiskManager(
            limits=limits,
            account_value=Decimal("100000000"),  # Initial account value
        )

        # Initialize broker
        kis_config = self.config.get_kis_config()
        self.broker = KISBroker(
            event_bus=self.event_bus,
            app_key=kis_config["app_key"],
            app_secret=kis_config["app_secret"],
            account_no=kis_config["account_no"],
            paper_trading=kis_config["paper_trading"],
        )
        await self.broker.connect()

        # Update account value from broker
        try:
            balance = await self.broker.get_account_balance()
            total_eval = balance.get("total_eval", Decimal("100000000"))
            self.risk_manager.update_account_value(total_eval)
        except Exception as e:
            logger.warning(f"Failed to get account balance: {e}")

        # Initialize Discord notifier
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

        # Initialize strategy engine
        self.strategy_engine = StrategyEngine(
            event_bus=self.event_bus,
            broker=self.broker,
        )
        await self._load_strategies()

        # Initialize order manager
        self.order_manager = OrderManager(
            event_bus=self.event_bus,
            broker=self.broker,
            risk_manager=self.risk_manager,
            storage=self.storage,
            is_trading_enabled=self.is_warmup_complete,
            notifier=self.notifier,
        )

        # Initialize scheduler (1 minute interval)
        self.scheduler = TradingScheduler(interval_seconds=60)
        self.scheduler.add_callback(self._on_tick)

        logger.info("Trading Bot initialized successfully")

    async def _load_strategies(self) -> None:
        """Load strategies from config"""
        for strategy_config in self.config.strategies:
            if not strategy_config.get("enabled"):
                continue

            try:
                # Dynamic import
                class_path = strategy_config["class"]
                module_path, class_name = class_path.rsplit(".", 1)
                module = importlib.import_module(module_path)
                strategy_class = getattr(module, class_name)

                # Get market
                from src.core.types import Market
                market_map = {
                    "krx": Market.KRX,
                    "nyse": Market.NYSE,
                    "nasdaq": Market.NASDAQ,
                    "amex": Market.AMEX,
                }
                market = market_map.get(strategy_config["market"].lower(), Market.KRX)

                # Create strategy instance
                strategy = strategy_class(
                    symbols=strategy_config["symbols"],
                    market=market,
                    params=strategy_config.get("params", {}),
                )

                self.strategy_engine.register_strategy(strategy)
                logger.info(f"Strategy loaded: {strategy_config['name']}")

            except Exception as e:
                logger.error(f"Failed to load strategy {strategy_config['name']}: {e}")

    async def _on_tick(self) -> None:
        """Called every minute - fetch prices and check strategies"""
        # Show warmup status
        if not self.is_warmup_complete():
            remaining = self.get_warmup_remaining()
            hours, remainder = divmod(int(remaining.total_seconds()), 3600)
            minutes = remainder // 60
            logger.info(f"[WARMUP] {hours}h {minutes}m remaining - data only")

        # Update dashboard status
        self._update_dashboard_status()

        # Update dashboard positions
        await self._update_dashboard_positions()

        logger.debug(f"Tick: {datetime.now().strftime('%H:%M:%S')}")

        for strategy in self.strategy_engine.get_strategies():
            for symbol in strategy.symbols:
                try:
                    # Rate limit: wait between API calls (KIS API limit: ~20 req/sec)
                    await asyncio.sleep(1)

                    # Get current price
                    price = await self.broker.get_current_price(symbol, strategy.market)

                    # Create a bar with current price (for RSI update)
                    bar = Bar(
                        symbol=symbol,
                        market=strategy.market,
                        open=price,
                        high=price,
                        low=price,
                        close=price,
                        volume=0,
                        timestamp=datetime.now(),
                        timeframe="1m",
                    )

                    # Update strategy data directly (for immediate RSI calculation)
                    strategy.update_bar(bar)

                    # Emit bar event for other handlers
                    await self.event_bus.publish(
                        Event(
                            event_type=EventType.BAR_UPDATE,
                            data=bar,
                            timestamp=datetime.now(),
                            source="Scheduler",
                        )
                    )

                    # Get RSI if available (now bar data is already updated)
                    rsi = None
                    rsi_str = ""
                    if hasattr(strategy, "get_current_rsi"):
                        rsi = strategy.get_current_rsi(symbol)
                        if rsi is not None:
                            rsi_str = f" | RSI: {rsi:.1f}"

                    # Save to database
                    await self.storage.save_price_rsi(
                        symbol=symbol,
                        market=strategy.market,
                        price=price,
                        rsi=rsi,
                    )

                    # Update dashboard
                    if rsi is not None:
                        self.dashboard.update_rsi(symbol, rsi)
                        self.dashboard.add_rsi_point(symbol, datetime.now(), rsi)

                    # Add price point to chart history
                    self.dashboard.add_price_point(
                        symbol=symbol,
                        time=datetime.now(),
                        open_=float(price),
                        high=float(price),
                        low=float(price),
                        close=float(price),
                    )

                    logger.info(f"[{symbol}] Price: {price:,}{rsi_str}")

                except Exception as e:
                    logger.error(f"Error fetching {symbol}: {e}")

    def is_warmup_complete(self) -> bool:
        """Check if warmup period is complete"""
        if self._warmup_hours <= 0:
            return True
        if self._start_time is None:
            return False
        elapsed = datetime.now() - self._start_time
        return elapsed >= timedelta(hours=self._warmup_hours)

    def get_warmup_remaining(self) -> timedelta:
        """Get remaining warmup time"""
        if self._start_time is None:
            return timedelta(hours=self._warmup_hours)
        elapsed = datetime.now() - self._start_time
        remaining = timedelta(hours=self._warmup_hours) - elapsed
        return max(remaining, timedelta(0))

    async def start(self) -> None:
        """Start the bot"""
        logger.info("Starting Trading Bot...")
        self._running = True
        self._start_time = datetime.now()

        # Start components
        await self.event_bus.start()
        await self.strategy_engine.start()
        await self.order_manager.start()

        # Initialize strategies with historical data
        await self.strategy_engine.initialize()

        # Sync existing positions from broker
        await self.strategy_engine.sync_positions()

        # Start scheduler (1 minute interval)
        await self.scheduler.start()

        logger.info("Trading Bot started")
        logger.info(f"Paper trading: {self.broker.paper_trading}")
        strategy_names = [s.name for s in self.strategy_engine.get_strategies()]
        logger.info(f"Strategies: {strategy_names}")
        if self._warmup_hours > 0:
            logger.info(
                f"Warmup period: {self._warmup_hours} hours (trading after warmup)"
            )
        logger.info(f"Check interval: {self.scheduler.interval} seconds")

        # Send startup notification
        if self.notifier:
            await self.notifier.notify_startup(
                strategies=strategy_names,
                paper_trading=self.broker.paper_trading,
            )

        # Update dashboard status
        self._update_dashboard_status()

    async def _update_dashboard_positions(self) -> None:
        """Update dashboard with current positions"""
        try:
            # Get positions for all markets
            for market in [Market.KRX, Market.NASDAQ, Market.NYSE, Market.AMEX]:
                try:
                    positions = await self.broker.get_positions(market)
                    for pos in positions:
                        # Get RSI if available
                        rsi = self.dashboard.rsi_values.get(pos.symbol)
                        self.dashboard.update_position(
                            symbol=pos.symbol,
                            market=market.value.upper(),
                            quantity=pos.quantity,
                            avg_price=float(pos.avg_entry_price),
                            current_price=float(pos.current_price),
                            rsi=rsi,
                        )
                except Exception:
                    pass  # Skip markets with no positions
        except Exception as e:
            logger.debug(f"Error updating dashboard positions: {e}")

    def _update_dashboard_status(self) -> None:
        """Update dashboard with current bot status"""
        strategy_names = [s.name for s in self.strategy_engine.get_strategies()]

        # Calculate uptime
        if self._start_time:
            uptime = datetime.now() - self._start_time
            hours, remainder = divmod(int(uptime.total_seconds()), 3600)
            minutes = remainder // 60
            uptime_str = f"{hours}h {minutes}m"
        else:
            uptime_str = "0m"

        # Calculate warmup remaining
        warmup_str = None
        if not self.is_warmup_complete():
            remaining = self.get_warmup_remaining()
            hours, remainder = divmod(int(remaining.total_seconds()), 3600)
            minutes = remainder // 60
            warmup_str = f"{hours}h {minutes}m"

        self.dashboard.set_bot_status(
            running=self._running,
            paper_trading=self.broker.paper_trading,
            strategies=strategy_names,
            uptime=uptime_str,
            warmup_remaining=warmup_str,
        )

        # Update account value
        self.dashboard.account_value = self.risk_manager.account_value

    async def stop(self) -> None:
        """Stop the bot"""
        logger.info("Stopping Trading Bot...")
        self._running = False

        # Send shutdown notification
        if self.notifier:
            await self.notifier.notify_shutdown()

        # Stop scheduler
        if self.scheduler:
            await self.scheduler.stop()

        # Stop order manager (cancels pending orders)
        if self.order_manager:
            await self.order_manager.stop()

        # Stop strategy engine
        if self.strategy_engine:
            await self.strategy_engine.stop()

        # Stop event bus
        await self.event_bus.stop()

        # Disconnect broker
        if self.broker:
            await self.broker.disconnect()

        # Close storage
        if self.storage:
            await self.storage.close()

        # Close notifier
        if self.notifier:
            await self.notifier.close()

        logger.info("Trading Bot stopped")

    async def run(self) -> None:
        """Main run loop"""
        await self.initialize()
        await self.start()

        # Keep running until stopped
        while self._running:
            await asyncio.sleep(1)


def run_web_server(host: str, port: int):
    """Run web server in a separate thread"""
    import uvicorn
    from src.web.app import create_app

    app = create_app()
    uvicorn.run(app, host=host, port=port, log_level="warning")


async def main():
    """Main entry point"""
    import argparse
    import threading

    parser = argparse.ArgumentParser(description="Trading Bot")
    parser.add_argument(
        "--warmup",
        type=int,
        default=0,
        help="Warmup period in hours before trading starts (default: 0)",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Enable web dashboard",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=8080,
        help="Web dashboard port (default: 8080)",
    )
    args = parser.parse_args()

    # Start web server in background thread
    if args.web:
        web_thread = threading.Thread(
            target=run_web_server,
            args=("0.0.0.0", args.web_port),
            daemon=True,
        )
        web_thread.start()
        logger.info(f"Web dashboard started at http://localhost:{args.web_port}")

    bot = TradingBot(warmup_hours=args.warmup)

    # Setup signal handlers
    loop = asyncio.get_event_loop()

    def signal_handler():
        logger.info("Received shutdown signal")
        asyncio.create_task(bot.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)

    try:
        await bot.run()
    except KeyboardInterrupt:
        pass
    finally:
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
