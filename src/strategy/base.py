from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List
from decimal import Decimal
import pandas as pd

from src.core.types import Bar, Quote, Signal, SignalType, Market, Fill


class BaseStrategy(ABC):
    """Base class for trading strategies"""

    def __init__(
        self,
        symbols: List[str],
        market: Market,
        params: Dict[str, Any] = None,
    ):
        self.symbols = symbols
        self.market = market
        self.params = params or {}

        # Internal state
        self._bars: Dict[str, pd.DataFrame] = {}
        self._positions: Dict[str, int] = {}
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

    def initialize(self, historical_bars: Dict[str, List[Bar]]) -> None:
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
    async def calculate_signal(self, symbol: str) -> Optional[Signal]:
        """Calculate trading signal (implement in subclass)"""
        pass

    async def on_bar(self, bar: Bar) -> Optional[Signal]:
        """Called when new bar data received"""
        if bar.symbol not in self.symbols:
            return None

        self.update_bar(bar)
        return await self.calculate_signal(bar.symbol)

    async def on_quote(self, quote: Quote) -> Optional[Signal]:
        """Called when new quote data received"""
        return None

    async def on_fill(self, fill: Fill) -> None:
        """Called when order is filled"""
        symbol = fill.symbol
        qty = fill.quantity if fill.side.value == "buy" else -fill.quantity
        self._positions[symbol] = self._positions.get(symbol, 0) + qty
