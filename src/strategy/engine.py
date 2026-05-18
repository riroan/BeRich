import asyncio
from datetime import datetime
from decimal import Decimal
import logging

from src.core.types import Bar, Quote, Signal, Market, Position, Fill
from src.core.events import EventBus, Event, EventType
from src.broker.kis.client import KISBroker
from src.utils.notifier import DiscordNotifier
from .base import BaseStrategy

logger = logging.getLogger("TradingBot")


class StrategyEngine:
    """Strategy execution engine"""

    def __init__(
        self,
        event_bus: EventBus,
        broker: KISBroker,
        notifier: DiscordNotifier | None = None,
    ):
        self.event_bus = event_bus
        self.broker = broker
        self.notifier = notifier
        self._strategies: list[BaseStrategy] = []
        self._running = False
        # order_id → cumulative qty / cumulative avg price already
        # applied to strategies. Makes fills idempotent, lets partials
        # apply as deltas, and lets us hand the strategy the delta's
        # MARGINAL price so its incremental weighted-average cost is
        # correct on both the WS and REST-poller paths.
        self._applied_fills: dict[str, int] = {}
        self._applied_avg: dict[str, Decimal] = {}

    def register_strategy(self, strategy: BaseStrategy) -> None:
        """Register a strategy"""
        self._strategies.append(strategy)
        logger.info(f"Strategy registered: {strategy.name}")

    async def initialize(self) -> None:
        """Initialize all strategies with historical data"""
        for strategy in self._strategies:
            historical_bars = {}

            for symbol in strategy.symbols:
                try:
                    await asyncio.sleep(1)
                    bars = await self.broker.get_historical_bars(
                        symbol=symbol,
                        market=strategy.market,
                        days=strategy.required_history,
                    )
                    historical_bars[symbol] = bars
                    logger.info(f"Loaded {len(bars)} bars for {symbol}")
                except Exception as e:
                    logger.error(f"Failed to load bars for {symbol}: {e}")
                    historical_bars[symbol] = []

            strategy.initialize(historical_bars)
            logger.info(f"Strategy initialized: {strategy.name}")

    async def start(self) -> None:
        """Start the engine"""
        self._running = True

        # Subscribe to events
        self.event_bus.subscribe(EventType.BAR_UPDATE, self._on_bar)
        self.event_bus.subscribe(EventType.QUOTE_UPDATE, self._on_quote)
        self.event_bus.subscribe(EventType.ORDER_FILLED, self._on_fill)
        self.event_bus.subscribe(EventType.ORDER_PARTIAL_FILLED, self._on_fill)

        logger.info("Strategy engine started")

    async def stop(self) -> None:
        """Stop the engine"""
        self._running = False
        logger.info("Strategy engine stopped")

    async def _on_bar(self, event: Event) -> None:
        """Handle bar data"""
        bar: Bar = event.data

        for strategy in self._strategies:
            if bar.symbol in strategy.symbols and bar.market == strategy.market:
                try:
                    signal = await strategy.on_bar(bar)
                    if signal:
                        await self._emit_signal(signal, strategy.name)
                except Exception as e:
                    logger.error(f"Error in strategy {strategy.name}: {e}")
                    if self.notifier:
                        await self.notifier.notify_strategy_error(
                            strategy_name=strategy.name,
                            error=str(e),
                        )

    async def _on_quote(self, event: Event) -> None:
        """Handle quote data"""
        quote: Quote = event.data

        for strategy in self._strategies:
            if quote.symbol in strategy.symbols and quote.market == strategy.market:
                try:
                    signal = await strategy.on_quote(quote)
                    if signal:
                        await self._emit_signal(signal, strategy.name)
                except Exception as e:
                    logger.error(f"Error in strategy {strategy.name}: {e}")
                    if self.notifier:
                        await self.notifier.notify_strategy_error(
                            strategy_name=strategy.name,
                            error=str(e),
                        )

    async def _on_fill(self, event: Event) -> None:
        """Apply fills to strategies exactly once, as incremental deltas.

        Routes both ORDER_FILLED and ORDER_PARTIAL_FILLED here.
        ``order.filled_quantity`` is CUMULATIVE; we apply only the delta
        versus what was already applied for this order_id. This makes a
        duplicate/redelivered event a no-op (no position double-count)
        and accounts partial fills as they happen, so a
        partial-then-cancelled order still tracks the real shares.
        """
        order = event.data
        if not hasattr(order, "symbol"):
            return

        order_id = order.order_id or ""
        cum_q = order.filled_quantity or 0
        cum_avg = order.filled_avg_price or Decimal("0")
        prev_q = self._applied_fills.get(order_id, 0)
        prev_avg = self._applied_avg.get(order_id, Decimal("0"))
        delta = cum_q - prev_q
        if delta <= 0:
            return  # duplicate event or no new shares

        # filled_avg_price is now the CUMULATIVE average on both the WS
        # and poller paths. Hand the strategy the delta's MARGINAL price
        # so its incremental weighted-average entry cost reconstructs the
        # true cumulative average exactly (and equals cum_avg on the
        # common single-fill case).
        if cum_q > 0:
            marginal = (cum_avg * cum_q - prev_avg * prev_q) / delta
        else:
            marginal = cum_avg
        self._applied_fills[order_id] = cum_q
        self._applied_avg[order_id] = cum_avg

        for strategy in self._strategies:
            if order.symbol in strategy.symbols:
                fill = Fill(
                    order_id=order_id,
                    symbol=order.symbol,
                    market=order.market,
                    side=order.side,
                    quantity=delta,
                    price=marginal,
                    commission=Decimal("0"),
                    timestamp=datetime.now(),
                )
                await strategy.on_fill(fill)

    async def _emit_signal(self, signal: Signal, strategy_name: str) -> None:
        """Emit signal event"""
        await self.event_bus.publish(
            Event(
                event_type=EventType.SIGNAL_GENERATED,
                data={"signal": signal, "strategy": strategy_name},
                timestamp=datetime.now(),
                source="StrategyEngine",
            )
        )
        logger.info(
            f"Signal generated: {strategy_name} - {signal.signal_type.name} "
            f"{signal.symbol} (strength={signal.strength:.2f})"
        )

    def get_strategies(self) -> list[BaseStrategy]:
        """Get all registered strategies"""
        return self._strategies

    async def sync_positions(self) -> None:
        """Sync positions from broker to all strategies"""
        logger.info("Syncing positions from broker...")

        # Collect all unique markets from strategies
        markets: set[Market] = set()
        for strategy in self._strategies:
            markets.add(strategy.market)

        # Fetch positions for each market
        all_positions: dict[str, Position] = {}
        for market in markets:
            try:
                await asyncio.sleep(0.5)
                positions = await self.broker.get_positions(market)
                for pos in positions:
                    all_positions[pos.symbol] = pos
                logger.info(f"Fetched {len(positions)} positions from {market.value}")
            except Exception as e:
                logger.error(f"Failed to fetch positions for {market.value}: {e}")

        if not all_positions:
            logger.info("No existing positions found")
            return

        # Sync to each strategy
        for strategy in self._strategies:
            for symbol in strategy.symbols:
                if symbol in all_positions:
                    pos = all_positions[symbol]
                    strategy.sync_position(
                        symbol=symbol,
                        quantity=pos.quantity,
                        avg_price=pos.avg_entry_price,
                    )

        logger.info(f"Position sync complete: {len(all_positions)} positions")
