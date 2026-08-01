from abc import ABC, abstractmethod
from typing import Any
from decimal import Decimal
import pandas as pd

from src.core.types import Bar, Quote, Signal, Market, Fill


class BaseStrategy(ABC):
    """Base class for trading strategies"""

    def __init__(
        self,
        symbols: list[str],
        market: Market | None = None,
        params: dict[str, Any] = None,
        symbol_markets: dict[str, Market] | None = None,
        config_name: str | None = None,
    ):
        self.symbols = symbols
        self.params = params or {}
        self.config_name = config_name
        # Market belongs to the symbol, not the strategy: one strategy can
        # hold NASDAQ and KRX names at once. `market=` stays as a shorthand
        # that applies one market to every symbol.
        if symbol_markets:
            self.symbol_markets = dict(symbol_markets)
        elif market is not None:
            self.symbol_markets = {s: market for s in symbols}
        else:
            raise ValueError(
                "BaseStrategy needs symbol_markets or a market",
            )

        # Internal state
        self._bars: dict[str, pd.DataFrame] = {}
        self._positions: dict[str, int] = {}
        self._is_initialized = False

    @property
    @abstractmethod
    def name(self) -> str:
        """Strategy name"""
        pass

    @property
    def required_history(self) -> int:
        """Minimum number of bars required"""
        return 100

    def market_for(self, symbol: str) -> Market | None:
        """The market this symbol trades on."""
        return self.symbol_markets.get(symbol)

    @property
    def markets(self) -> set[Market]:
        """Every market this strategy touches."""
        return set(self.symbol_markets.values())

    @property
    def name_with_market(self) -> str:
        """Identity used to match a running instance to its DB config.

        The config name is the unique key; it used to be reconstructed from
        market + name, which broke the moment a strategy could be renamed or
        hold more than one market.
        """
        return self.config_name or self.name

    def initialize(self, historical_bars: dict[str, list[Bar]]) -> None:
        """Initialize strategy with historical data"""
        for symbol, bars in historical_bars.items():
            if not bars:
                continue

            df = pd.DataFrame(
                [
                    {
                        "timestamp": b.timestamp,
                        "open": float(b.open),
                        "high": float(b.high),
                        "low": float(b.low),
                        "close": float(b.close),
                        "volume": b.volume,
                    }
                    for b in bars
                ]
            )
            df.set_index("timestamp", inplace=True)
            self._bars[symbol] = df

        self._is_initialized = True

    def update_bar(self, bar: Bar) -> None:
        """Add new bar data"""
        symbol = bar.symbol
        new_row = pd.DataFrame(
            [
                {
                    "timestamp": bar.timestamp,
                    "open": float(bar.open),
                    "high": float(bar.high),
                    "low": float(bar.low),
                    "close": float(bar.close),
                    "volume": bar.volume,
                }
            ]
        ).set_index("timestamp")

        if symbol in self._bars:
            self._bars[symbol] = pd.concat([self._bars[symbol], new_row]).tail(
                self.required_history * 2
            )
        else:
            self._bars[symbol] = new_row

    def get_dataframe(self, symbol: str) -> pd.DataFrame:
        """Get DataFrame for a symbol"""
        return self._bars.get(symbol, pd.DataFrame())

    def get_position(self, symbol: str) -> int:
        """Get current position for a symbol"""
        return self._positions.get(symbol, 0)

    def sync_position(
        self,
        symbol: str,
        quantity: int,
        avg_price: Decimal,
    ) -> None:
        """Sync position from broker (called on startup)

        Override in subclass to restore strategy-specific state.
        """
        if symbol not in self.symbols:
            return

        self._positions[symbol] = quantity

    @abstractmethod
    async def calculate_signal(self, symbol: str) -> Signal | None:
        """Calculate trading signal (implement in subclass)"""
        pass

    async def on_bar(self, bar: Bar) -> Signal | None:
        """Called when new bar data received"""
        if bar.symbol not in self.symbols:
            return None

        self.update_bar(bar)
        return await self.calculate_signal(bar.symbol)

    async def on_quote(self, quote: Quote) -> Signal | None:
        """Called when new quote data received"""
        return None

    async def on_fill(self, fill: Fill) -> None:
        """Called when order is filled"""
        symbol = fill.symbol
        qty = fill.quantity if fill.side.value == "buy" else -fill.quantity
        self._positions[symbol] = self._positions.get(symbol, 0) + qty
