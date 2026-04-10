from enum import Enum, auto
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional


class Market(Enum):
    """Trading market"""
    KRX = "krx"
    NYSE = "nyse"
    NASDAQ = "nasdaq"
    AMEX = "amex"

    @classmethod
    def from_string(cls, value: str) -> "Market":
        """Convert string to Market enum (case-insensitive)"""
        try:
            return cls(value.lower())
        except ValueError:
            raise ValueError(f"Unknown market: {value}. Valid: {[m.value for m in cls]}")


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIAL_FILLED = "partial_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class SignalType(Enum):
    ENTRY_LONG = auto()
    EXIT_LONG = auto()
    HOLD = auto()


@dataclass
class Quote:
    """Real-time quote data"""
    symbol: str
    market: Market
    bid_price: Decimal
    ask_price: Decimal
    bid_size: int
    ask_size: int
    last_price: Decimal
    last_size: int
    timestamp: datetime


@dataclass
class Bar:
    """OHLCV bar data"""
    symbol: str
    market: Market
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    timestamp: datetime
    timeframe: str  # "1m", "5m", "1h", "1d"


@dataclass
class Order:
    """Order information"""
    symbol: str
    market: Market
    side: OrderSide
    order_type: OrderType
    quantity: int
    price: Optional[Decimal] = None
    order_id: Optional[str] = None
    status: OrderStatus = OrderStatus.PENDING
    created_at: datetime = field(default_factory=datetime.now)
    filled_quantity: int = 0
    filled_avg_price: Optional[Decimal] = None


@dataclass
class Position:
    """Position information"""
    symbol: str
    market: Market
    quantity: int
    avg_entry_price: Decimal
    current_price: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal = field(default_factory=lambda: Decimal("0"))


@dataclass
class Signal:
    """Trading signal"""
    signal_type: SignalType
    symbol: str
    market: Market
    strength: float  # 0.0 ~ 1.0
    target_price: Optional[Decimal] = None
    stop_loss: Optional[Decimal] = None
    metadata: dict = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class Fill:
    """Fill information"""
    order_id: str
    symbol: str
    market: Market
    side: OrderSide
    quantity: int
    price: Decimal
    commission: Decimal
    timestamp: datetime
    pnl: Optional[Decimal] = None
    rsi: Optional[float] = None
