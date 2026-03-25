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
from src.web.app import get_dashboard_state, broadcast_update

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
        self._stopped = False  # Prevent duplicate stop calls
        self._start_time: datetime = None
        self._warmup_hours = warmup_hours  # Hours to wait before trading
        # Use absolute path based on script location
        self._project_root = Path(__file__).parent.parent
        self._warmup_file = self._project_root / "data" / "warmup_start.txt"
        # Equity snapshot save interval (every N ticks, 1 tick = 60 seconds)
        self._equity_save_interval = 5  # 5분마다 저장
        self._equity_save_counter = 0

    async def initialize(self) -> None:
        """Initialize all components"""
        logger.info("Initializing Trading Bot...")

        # Load config
        self.config.load()

        # Load warmup hours from config if not set via CLI
        if self._warmup_hours == 0:
            self._warmup_hours = self.config.get("trading.warmup_hours", 0)
            logger.info(f"Warmup hours loaded from config: {self._warmup_hours}")

        # Initialize storage
        db_url = self.config.get("database.url", "sqlite+aiosqlite:///data/trading.db")

        # Ensure data directory exists
        (self._project_root / "data").mkdir(exist_ok=True)

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

        # Update account value from broker (both KRW and USD)
        try:
            # Get KRW balance
            krw_balance = await self.broker.get_account_balance(Market.KRX)
            logger.debug(f"KRW balance response: {krw_balance}")
            self.dashboard.balance_krw = krw_balance.get("total_eval", Decimal("0"))
            self.dashboard.cash_krw = krw_balance.get("cash", Decimal("0"))
            self.dashboard.pnl_krw = krw_balance.get("profit_loss", Decimal("0"))

            # Get USD balance
            try:
                usd_balance = await self.broker.get_account_balance(Market.NASDAQ)
                logger.debug(f"USD balance response: {usd_balance}")
                self.dashboard.balance_usd = usd_balance.get("total_eval", Decimal("0"))
                self.dashboard.cash_usd = usd_balance.get("cash", Decimal("0"))
                self.dashboard.pnl_usd = usd_balance.get("profit_loss", Decimal("0"))
            except Exception as e:
                logger.warning(f"Failed to get USD balance: {e}")

            # Total for risk manager (KRW only for now)
            total_eval = krw_balance.get("total_eval", Decimal("100000000"))
            self.risk_manager.update_account_value(total_eval)

            logger.info(
                f"Account balance - KRW: {self.dashboard.balance_krw:,.0f}, "
                f"USD: {self.dashboard.balance_usd:,.2f}"
            )
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
            notifier=self.notifier,
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
                        self.dashboard.update_rsi(symbol, rsi, price=float(price), market=strategy.market.value.upper())
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

                    # Update last price update time
                    self.dashboard.last_price_update = datetime.now()

                    logger.info(f"[{symbol}] Price: {price:,}{rsi_str}")

                except Exception as e:
                    logger.error(f"Error fetching {symbol}: {e}")
                    # Send data fetch error notification
                    if self.notifier:
                        await self.notifier.notify_data_fetch_failed(
                            symbol=symbol,
                            error=str(e),
                        )

        # Update system status with latest price update time
        self.dashboard.update_system_status(
            auto_trading=self.is_warmup_complete(),
            api_connected=True,
            account_tradable=True,
            data_ok=True,
        )

        # Broadcast update to WebSocket clients
        try:
            await broadcast_update("tick")
        except Exception as e:
            logger.debug(f"WebSocket broadcast failed: {e}")

    def is_warmup_complete(self) -> bool:
        """Check if warmup period is complete"""
        if self._warmup_hours <= 0:
            return True
        if self._start_time is None:
            return False
        elapsed = datetime.now() - self._start_time
        complete = elapsed >= timedelta(hours=self._warmup_hours)

        # Clean up warmup file when complete
        if complete and self._warmup_file.exists():
            self._warmup_file.unlink()
            logger.info("Warmup complete - auto trading enabled")

        return complete

    def get_warmup_remaining(self) -> timedelta:
        """Get remaining warmup time"""
        if self._start_time is None:
            return timedelta(hours=self._warmup_hours)
        elapsed = datetime.now() - self._start_time
        remaining = timedelta(hours=self._warmup_hours) - elapsed
        return max(remaining, timedelta(0))

    def _save_warmup_start(self) -> None:
        """Save warmup start time to file for persistence across restarts"""
        if self._start_time and self._warmup_hours > 0:
            self._warmup_file.write_text(self._start_time.isoformat())
            logger.info(f"Warmup start time saved: {self._start_time}")

    def _load_warmup_start(self) -> None:
        """Load warmup start time from file if exists"""
        logger.debug(f"Warmup check: hours={self._warmup_hours}, file={self._warmup_file}, exists={self._warmup_file.exists()}")

        if self._warmup_hours > 0 and self._warmup_file.exists():
            try:
                saved_time = datetime.fromisoformat(self._warmup_file.read_text().strip())
                # Check if warmup is still relevant (not completed yet)
                elapsed = datetime.now() - saved_time
                if elapsed < timedelta(hours=self._warmup_hours):
                    self._start_time = saved_time
                    remaining = timedelta(hours=self._warmup_hours) - elapsed
                    hours, remainder = divmod(int(remaining.total_seconds()), 3600)
                    minutes = remainder // 60
                    logger.info(
                        f"Warmup resumed from {saved_time.strftime('%Y-%m-%d %H:%M:%S')} "
                        f"({hours}h {minutes}m remaining)"
                    )
                else:
                    # Warmup already completed, delete the file
                    self._warmup_file.unlink()
                    logger.info("Previous warmup already completed")
            except Exception as e:
                logger.warning(f"Failed to load warmup start time: {e}")
        elif self._warmup_hours <= 0:
            logger.debug("Warmup disabled (warmup_hours <= 0)")
        elif not self._warmup_file.exists():
            logger.debug(f"Warmup file not found: {self._warmup_file}")

    async def start(self) -> None:
        """Start the bot"""
        logger.info("Starting Trading Bot...")
        self._running = True

        # Try to load warmup start time from previous run
        self._load_warmup_start()

        # If no saved warmup time, use current time
        if self._start_time is None:
            self._start_time = datetime.now()
            self._save_warmup_start()

        # Start components
        await self.event_bus.start()
        await self.strategy_engine.start()
        await self.order_manager.start()

        # Initialize strategies with historical data
        await self.strategy_engine.initialize()

        # Sync existing positions from broker
        await self.strategy_engine.sync_positions()

        # Load chart history from database (for charts only)
        await self._load_chart_history()

        # Load equity history for equity curve
        await self._load_equity_history()

        # Load fills for performance calculation
        await self._load_fills()

        # Calculate initial RSI values from loaded historical data (overrides DB values)
        await self._update_initial_rsi()

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
            # Get strategy state for additional info
            strategy_states = {}
            for strategy in self.strategy_engine.get_strategies():
                if hasattr(strategy, '_buy_stages'):
                    for symbol in strategy.symbols:
                        strategy_states[symbol] = {
                            'buy_stage': strategy._buy_stages.get(symbol, 0),
                            'sell_stage': strategy._sell_stages.get(symbol, 0),
                            'max_buy_stages': len(strategy.params.get('avg_down_levels', [(30, 0.5), (25, 0.3), (20, 0.2)])),
                            'max_sell_stages': len(strategy.params.get('sell_levels', [(70, 0.3), (75, 0.4), (80, 0.5)])),
                            'last_buy_time': strategy._last_buy_time.get(symbol),
                            'stop_loss_pct': strategy.params.get('stop_loss', -10),
                        }

            # Get positions for all markets
            for market in [Market.KRX, Market.NASDAQ, Market.NYSE, Market.AMEX]:
                try:
                    positions = await self.broker.get_positions(market)
                    for pos in positions:
                        # Get RSI if available
                        rsi = self.dashboard.rsi_values.get(pos.symbol)

                        # Get strategy state
                        state = strategy_states.get(pos.symbol, {})
                        last_buy_date = None
                        if state.get('last_buy_time'):
                            last_buy_date = state['last_buy_time'].strftime('%m-%d %H:%M')

                        # Calculate PnL percentage
                        pnl_pct = 0
                        if pos.avg_entry_price > 0:
                            pnl_pct = float(
                                (pos.current_price - pos.avg_entry_price)
                                / pos.avg_entry_price * 100
                            )

                        stop_loss_pct = state.get('stop_loss_pct', -10.0)

                        self.dashboard.update_position(
                            symbol=pos.symbol,
                            market=market.value.upper(),
                            quantity=pos.quantity,
                            avg_price=float(pos.avg_entry_price),
                            current_price=float(pos.current_price),
                            rsi=rsi,
                            buy_stage=state.get('buy_stage', 0),
                            sell_stage=state.get('sell_stage', 0),
                            max_buy_stages=state.get('max_buy_stages', 3),
                            max_sell_stages=state.get('max_sell_stages', 3),
                            last_buy_date=last_buy_date,
                            stop_loss_pct=stop_loss_pct,
                        )

                        # Check stop loss imminent (within 2%)
                        distance_to_stop = pnl_pct - stop_loss_pct
                        if distance_to_stop <= 2.0 and distance_to_stop > 0:
                            if self.notifier and not hasattr(self, f'_stop_loss_alert_{pos.symbol}'):
                                await self.notifier.notify_stop_loss_imminent(
                                    symbol=pos.symbol,
                                    current_pnl_pct=pnl_pct,
                                    stop_loss_pct=stop_loss_pct,
                                    distance_pct=distance_to_stop,
                                )
                                # Prevent duplicate alerts
                                setattr(self, f'_stop_loss_alert_{pos.symbol}', True)
                        else:
                            # Reset alert flag
                            if hasattr(self, f'_stop_loss_alert_{pos.symbol}'):
                                delattr(self, f'_stop_loss_alert_{pos.symbol}')

                except Exception:
                    pass  # Skip markets with no positions

            # Update balances
            try:
                krw_balance = await self.broker.get_account_balance(Market.KRX)
                self.dashboard.balance_krw = krw_balance.get("total_eval", Decimal("0"))
                self.dashboard.cash_krw = krw_balance.get("cash", Decimal("0"))
                self.dashboard.pnl_krw = krw_balance.get("profit_loss", Decimal("0"))

                usd_balance = await self.broker.get_account_balance(Market.NASDAQ)
                self.dashboard.balance_usd = usd_balance.get("total_eval", Decimal("0"))
                self.dashboard.cash_usd = usd_balance.get("cash", Decimal("0"))
                self.dashboard.pnl_usd = usd_balance.get("profit_loss", Decimal("0"))

                # Save equity snapshot for chart (every N ticks to reduce data)
                self._equity_save_counter += 1
                if self._equity_save_counter >= self._equity_save_interval:
                    self._equity_save_counter = 0

                    position_value_krw = self.dashboard.balance_krw - self.dashboard.cash_krw
                    position_value_usd = self.dashboard.balance_usd - self.dashboard.cash_usd
                    await self.storage.save_equity_snapshot(
                        total_krw=self.dashboard.balance_krw,
                        total_usd=self.dashboard.balance_usd,
                        cash_krw=self.dashboard.cash_krw,
                        cash_usd=self.dashboard.cash_usd,
                        position_value_krw=position_value_krw,
                        position_value_usd=position_value_usd,
                    )

                    # Also update dashboard equity history for live chart
                    self.dashboard.equity_history.append({
                        "timestamp": datetime.now().isoformat(),
                        "total_krw": float(self.dashboard.balance_krw),
                        "total_usd": float(self.dashboard.balance_usd),
                        "cash_krw": float(self.dashboard.cash_krw),
                        "cash_usd": float(self.dashboard.cash_usd),
                        "position_value_krw": float(position_value_krw),
                        "position_value_usd": float(position_value_usd),
                    })
                    # Keep only last 1000 points in memory (5분 간격 = 약 3.5일)
                    if len(self.dashboard.equity_history) > 1000:
                        self.dashboard.equity_history = self.dashboard.equity_history[-1000:]

                # Check low cash ratio (USD)
                if self.dashboard.balance_usd > 0:
                    cash_ratio = float(self.dashboard.cash_usd / self.dashboard.balance_usd * 100)
                    min_cash_ratio = 10.0  # 10% minimum
                    if cash_ratio < min_cash_ratio:
                        if self.notifier and not hasattr(self, '_low_cash_alert'):
                            await self.notifier.notify_low_cash_ratio(
                                cash_ratio=cash_ratio,
                                min_ratio=min_cash_ratio,
                            )
                            self._low_cash_alert = True
                    else:
                        if hasattr(self, '_low_cash_alert'):
                            delattr(self, '_low_cash_alert')

            except Exception as e:
                logger.error(f"Failed to update balances: {e}")
                if self.notifier:
                    await self.notifier.notify_account_error(error=str(e))

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

        # Update system status
        self.dashboard.last_strategy_run = datetime.now()
        self.dashboard.update_system_status(
            auto_trading=self.is_warmup_complete(),
            api_connected=True,
            account_tradable=True,
            data_ok=True,
        )

    async def _update_initial_rsi(self) -> None:
        """Calculate and update initial RSI values from loaded historical data"""
        logger.info("Calculating initial RSI values...")

        # Clear old RSI values and only keep active strategy symbols
        self.dashboard.rsi_values.clear()
        self.dashboard.rsi_prices.clear()

        for strategy in self.strategy_engine.get_strategies():
            for symbol in strategy.symbols:
                try:
                    if hasattr(strategy, "get_current_rsi"):
                        rsi = strategy.get_current_rsi(symbol)
                        if rsi is not None:
                            # Get latest price from strategy data
                            df = strategy.get_dataframe(symbol)
                            price = float(df["close"].iloc[-1]) if len(df) > 0 else None
                            market = strategy.market.value.upper()

                            self.dashboard.update_rsi(
                                symbol, rsi, price=price, market=market
                            )
                            logger.info(f"  [{symbol}] RSI: {rsi:.1f}")
                except Exception as e:
                    logger.error(f"Failed to calculate RSI for {symbol}: {e}")

    async def _load_chart_history(self) -> None:
        """Load price/RSI history from database on startup"""
        try:
            symbols = await self.storage.get_all_symbols_with_history()
            logger.info(f"Loading chart history for {len(symbols)} symbols...")

            for symbol in symbols:
                history = await self.storage.get_price_rsi_history(symbol, limit=200)

                for record in history:
                    # Add price point
                    self.dashboard.add_price_point(
                        symbol=record["symbol"],
                        time=record["timestamp"],
                        open_=record["price"],
                        high=record["price"],
                        low=record["price"],
                        close=record["price"],
                    )

                    # Add RSI point
                    if record["rsi"] is not None:
                        self.dashboard.add_rsi_point(
                            symbol=record["symbol"],
                            time=record["timestamp"],
                            rsi=record["rsi"],
                        )

                if history:
                    latest = history[-1]
                    if latest["rsi"] is not None:
                        self.dashboard.update_rsi(
                            symbol,
                            latest["rsi"],
                            price=latest.get("price"),
                            market=latest.get("market"),
                        )
                    logger.info(
                        f"  [{symbol}] Loaded {len(history)} history points"
                    )

            logger.info("Chart history loaded successfully")
        except Exception as e:
            logger.warning(f"Failed to load chart history: {e}")

    async def _load_equity_history(self) -> None:
        """Load equity history from database for equity curve chart"""
        try:
            history = await self.storage.get_equity_history(days=90)
            self.dashboard.equity_history = history
            logger.info(f"Loaded {len(history)} equity history points")
        except Exception as e:
            logger.warning(f"Failed to load equity history: {e}")

    async def _load_fills(self) -> None:
        """Load fills from database for performance calculation"""
        try:
            fills = await self.storage.get_all_fills()
            self.dashboard.fills = [
                {
                    "order_id": f.order_id,
                    "symbol": f.symbol,
                    "market": f.market.value if f.market else None,
                    "side": f.side.value if f.side else None,
                    "quantity": f.quantity,
                    "price": float(f.price),
                    "commission": float(f.commission),
                    "pnl": float(f.pnl) if f.pnl else None,
                    "timestamp": f.timestamp.isoformat() if f.timestamp else None,
                }
                for f in fills
            ]
            self.dashboard.calculate_performance()
            logger.info(f"Loaded {len(fills)} fills, performance calculated")
        except Exception as e:
            logger.warning(f"Failed to load fills: {e}")

    async def stop(self) -> None:
        """Stop the bot"""
        if self._stopped:
            return  # Already stopped
        self._stopped = True

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
