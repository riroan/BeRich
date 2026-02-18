from typing import List, Optional
from datetime import datetime
from decimal import Decimal
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select
import logging

from src.core.types import Bar, Order, Fill, Market
from .models import Base, BarModel, OrderModel, FillModel, PriceRSIModel

logger = logging.getLogger(__name__)


class Storage:
    """Async database storage"""

    def __init__(self, database_url: str):
        # Ensure async driver
        if database_url.startswith("sqlite://"):
            database_url = database_url.replace("sqlite://", "sqlite+aiosqlite://")
        elif database_url.startswith("mysql://"):
            database_url = database_url.replace("mysql://", "mysql+aiomysql://")

        self.engine = create_async_engine(database_url, echo=False)
        self.async_session = sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

    async def initialize(self) -> None:
        """Create tables"""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database initialized")

    async def close(self) -> None:
        """Close database connection"""
        await self.engine.dispose()

    # ==================== Bars ====================

    async def save_bar(self, bar: Bar) -> None:
        """Save a bar to database"""
        async with self.async_session() as session:
            bar_model = BarModel(
                symbol=bar.symbol,
                market=bar.market,
                timeframe=bar.timeframe,
                timestamp=bar.timestamp,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
            )
            session.add(bar_model)
            await session.commit()

    async def save_bars(self, bars: List[Bar]) -> None:
        """Save multiple bars to database"""
        async with self.async_session() as session:
            for bar in bars:
                bar_model = BarModel(
                    symbol=bar.symbol,
                    market=bar.market,
                    timeframe=bar.timeframe,
                    timestamp=bar.timestamp,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                )
                session.add(bar_model)
            await session.commit()

    async def get_bars(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        market: Market = Market.KRX,
    ) -> List[Bar]:
        """Get bars from database"""
        async with self.async_session() as session:
            query = (
                select(BarModel)
                .where(
                    BarModel.symbol == symbol,
                    BarModel.market == market,
                    BarModel.timeframe == timeframe,
                    BarModel.timestamp >= start,
                    BarModel.timestamp <= end,
                )
                .order_by(BarModel.timestamp)
            )

            result = await session.execute(query)
            rows = result.scalars().all()

            return [
                Bar(
                    symbol=row.symbol,
                    market=row.market,
                    open=Decimal(str(row.open)),
                    high=Decimal(str(row.high)),
                    low=Decimal(str(row.low)),
                    close=Decimal(str(row.close)),
                    volume=row.volume,
                    timestamp=row.timestamp,
                    timeframe=row.timeframe,
                )
                for row in rows
            ]

    # ==================== Orders ====================

    async def save_order(self, order: Order) -> None:
        """Save or update an order"""
        async with self.async_session() as session:
            # Check if order exists
            query = select(OrderModel).where(OrderModel.order_id == order.order_id)
            result = await session.execute(query)
            existing = result.scalar_one_or_none()

            if existing:
                existing.status = order.status
                existing.filled_quantity = order.filled_quantity
                existing.filled_avg_price = order.filled_avg_price
                existing.updated_at = datetime.now()
            else:
                order_model = OrderModel(
                    order_id=order.order_id,
                    symbol=order.symbol,
                    market=order.market,
                    side=order.side,
                    quantity=order.quantity,
                    price=order.price,
                    status=order.status,
                    filled_quantity=order.filled_quantity,
                    filled_avg_price=order.filled_avg_price,
                    created_at=order.created_at,
                )
                session.add(order_model)

            await session.commit()

    async def get_order(self, order_id: str) -> Optional[Order]:
        """Get order by ID"""
        async with self.async_session() as session:
            query = select(OrderModel).where(OrderModel.order_id == order_id)
            result = await session.execute(query)
            row = result.scalar_one_or_none()

            if not row:
                return None

            return Order(
                symbol=row.symbol,
                market=row.market,
                side=row.side,
                order_type=row.order_type if hasattr(row, "order_type") else None,
                quantity=row.quantity,
                price=Decimal(str(row.price)) if row.price else None,
                order_id=row.order_id,
                status=row.status,
                created_at=row.created_at,
                filled_quantity=row.filled_quantity,
                filled_avg_price=(
                    Decimal(str(row.filled_avg_price)) if row.filled_avg_price else None
                ),
            )

    # ==================== Fills ====================

    async def save_fill(self, fill: Fill) -> None:
        """Save a fill record"""
        async with self.async_session() as session:
            fill_model = FillModel(
                order_id=fill.order_id,
                symbol=fill.symbol,
                market=fill.market,
                side=fill.side,
                quantity=fill.quantity,
                price=fill.price,
                commission=fill.commission,
                pnl=fill.pnl,
                timestamp=fill.timestamp,
            )
            session.add(fill_model)
            await session.commit()

    async def get_fills(
        self,
        start: datetime,
        end: datetime,
        symbol: Optional[str] = None,
    ) -> List[Fill]:
        """Get fills within date range"""
        async with self.async_session() as session:
            query = select(FillModel).where(
                FillModel.timestamp >= start,
                FillModel.timestamp <= end,
            )

            if symbol:
                query = query.where(FillModel.symbol == symbol)

            query = query.order_by(FillModel.timestamp)

            result = await session.execute(query)
            rows = result.scalars().all()

            return [
                Fill(
                    order_id=row.order_id,
                    symbol=row.symbol,
                    market=row.market,
                    side=row.side,
                    quantity=row.quantity,
                    price=Decimal(str(row.price)),
                    commission=Decimal(str(row.commission)),
                    pnl=Decimal(str(row.pnl)) if row.pnl else None,
                    timestamp=row.timestamp,
                )
                for row in rows
            ]

    # ==================== Price/RSI ====================

    async def save_price_rsi(
        self,
        symbol: str,
        market: Market,
        price: Decimal,
        rsi: Optional[float] = None,
    ) -> None:
        """Save price and RSI data"""
        async with self.async_session() as session:
            record = PriceRSIModel(
                symbol=symbol,
                market=market,
                price=price,
                rsi=Decimal(str(rsi)) if rsi is not None else None,
                timestamp=datetime.now(),
            )
            session.add(record)
            await session.commit()
