from sqlalchemy import Column, Integer, String, DateTime, Numeric, Enum as SQLEnum, Index
from sqlalchemy.orm import declarative_base
from datetime import datetime

from src.core.types import Market, OrderSide, OrderStatus

Base = declarative_base()


class BarModel(Base):
    """OHLCV bar data table"""

    __tablename__ = "bars"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False)
    market = Column(SQLEnum(Market), nullable=False)
    timeframe = Column(String(10), nullable=False)
    timestamp = Column(DateTime, nullable=False)
    open = Column(Numeric(20, 8), nullable=False)
    high = Column(Numeric(20, 8), nullable=False)
    low = Column(Numeric(20, 8), nullable=False)
    close = Column(Numeric(20, 8), nullable=False)
    volume = Column(Integer, nullable=False)

    __table_args__ = (
        Index("idx_bars_symbol_timeframe_timestamp", "symbol", "timeframe", "timestamp"),
    )


class OrderModel(Base):
    """Order history table"""

    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    order_id = Column(String(50), unique=True, nullable=False)
    symbol = Column(String(20), nullable=False)
    market = Column(SQLEnum(Market), nullable=False)
    side = Column(SQLEnum(OrderSide), nullable=False)
    quantity = Column(Integer, nullable=False)
    price = Column(Numeric(20, 8))
    status = Column(SQLEnum(OrderStatus), nullable=False)
    filled_quantity = Column(Integer, default=0)
    filled_avg_price = Column(Numeric(20, 8))
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class FillModel(Base):
    """Fill history table"""

    __tablename__ = "fills"

    id = Column(Integer, primary_key=True)
    order_id = Column(String(50), nullable=False)
    symbol = Column(String(20), nullable=False)
    market = Column(SQLEnum(Market), nullable=False)
    side = Column(SQLEnum(OrderSide), nullable=False)
    quantity = Column(Integer, nullable=False)
    price = Column(Numeric(20, 8), nullable=False)
    commission = Column(Numeric(20, 8), nullable=False)
    pnl = Column(Numeric(20, 8))
    timestamp = Column(DateTime, nullable=False)


class PositionSnapshot(Base):
    """Daily position snapshot table"""

    __tablename__ = "position_snapshots"

    id = Column(Integer, primary_key=True)
    date = Column(DateTime, nullable=False)
    symbol = Column(String(20), nullable=False)
    market = Column(SQLEnum(Market), nullable=False)
    quantity = Column(Integer, nullable=False)
    avg_price = Column(Numeric(20, 8), nullable=False)
    market_value = Column(Numeric(20, 8), nullable=False)
    unrealized_pnl = Column(Numeric(20, 8), nullable=False)


class PriceRSIModel(Base):
    """Price and RSI history table"""

    __tablename__ = "price_rsi"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False)
    market = Column(SQLEnum(Market), nullable=False)
    price = Column(Numeric(20, 8), nullable=False)
    rsi = Column(Numeric(10, 4))
    timestamp = Column(DateTime, nullable=False)

    __table_args__ = (
        Index("idx_price_rsi_symbol_timestamp", "symbol", "timestamp"),
    )
