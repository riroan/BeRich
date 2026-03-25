from typing import Dict, List, Optional, Set
import asyncio
from datetime import datetime
import logging

from src.core.types import Bar, Quote, Signal, Market, Position
from src.core.events import EventBus, Event, EventType
from src.broker.kis.client import KISBroker
from .base import BaseStrategy

logger = logging.getLogger("TradingBot")


class StrategyEngine:
    """Strategy execution engine"""

    def __init__(
        self,
        event_bus: EventBus,
        broker: KISBroker,
    ):
        self.event_bus = event_bus
        self.broker = broker
        self._strategies: List[BaseStrategy] = []
        self._running = False

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

    async def _on_fill(self, event: Event) -> None:
        """Handle fill events"""
        order = event.data

        for strategy in self._strategies:
            if hasattr(order, "symbol") and order.symbol in strategy.symbols:
                # Create a simple fill-like object for the strategy
                from src.core.types import Fill, OrderSide
                from decimal import Decimal

                fill = Fill(
                    order_id=order.order_id or "",
                    symbol=order.symbol,
                    market=order.market,
                    side=order.side,
                    quantity=order.filled_quantity,
                    price=order.filled_avg_price or Decimal("0"),
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

    def get_strategies(self) -> List[BaseStrategy]:
        """Get all registered strategies"""
        return self._strategies

    async def sync_positions(self) -> None:
        """Sync positions from broker to all strategies"""
        logger.info("Syncing positions from broker...")

        # Collect all unique markets from strategies
        markets: Set[Market] = set()
        for strategy in self._strategies:
            markets.add(strategy.market)

        # Fetch positions for each market
        all_positions: Dict[str, Position] = {}
        for market in markets:
            try:
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
