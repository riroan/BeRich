from typing import List, Optional
from datetime import datetime, timedelta
from decimal import Decimal
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select
import logging

from src.core.types import Bar, Order, Fill, Market
from .models import Base, BarModel, OrderModel, FillModel, PriceRSIModel, EquitySnapshot

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

    async def get_all_fills(self) -> List[Fill]:
        """Get all fills for performance calculation"""
        async with self.async_session() as session:
            query = select(FillModel).order_by(FillModel.timestamp)
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

    async def get_price_rsi_history(
        self,
        symbol: str,
        limit: int = 200,
    ) -> List[dict]:
        """Get recent price/RSI history for a symbol"""
        async with self.async_session() as session:
            query = (
                select(PriceRSIModel)
                .where(PriceRSIModel.symbol == symbol)
                .order_by(PriceRSIModel.timestamp.desc())
                .limit(limit)
            )

            result = await session.execute(query)
            rows = result.scalars().all()

            # Return in chronological order
            return [
                {
                    "symbol": row.symbol,
                    "market": row.market.value.upper() if row.market else None,
                    "price": float(row.price),
                    "rsi": float(row.rsi) if row.rsi else None,
                    "timestamp": row.timestamp,
                }
                for row in reversed(rows)
            ]

    async def get_all_symbols_with_history(self) -> List[str]:
        """Get all symbols that have price/RSI history"""
        async with self.async_session() as session:
            from sqlalchemy import distinct
            query = select(distinct(PriceRSIModel.symbol))
            result = await session.execute(query)
            return [row[0] for row in result.all()]

    # ==================== Equity Snapshots ====================

    async def save_equity_snapshot(
        self,
        total_krw: Decimal,
        total_usd: Decimal,
        cash_krw: Decimal,
        cash_usd: Decimal,
        position_value_krw: Decimal,
        position_value_usd: Decimal,
    ) -> None:
        """Save equity snapshot"""
        async with self.async_session() as session:
            snapshot = EquitySnapshot(
                timestamp=datetime.now(),
                total_krw=total_krw,
                total_usd=total_usd,
                cash_krw=cash_krw,
                cash_usd=cash_usd,
                position_value_krw=position_value_krw,
                position_value_usd=position_value_usd,
            )
            session.add(snapshot)
            await session.commit()

    async def get_equity_history(self, days: int = 90) -> List[dict]:
        """Get equity history for the last N days"""
        async with self.async_session() as session:
            from_date = datetime.now() - timedelta(days=days)
            query = (
                select(EquitySnapshot)
                .where(EquitySnapshot.timestamp >= from_date)
                .order_by(EquitySnapshot.timestamp)
            )

            result = await session.execute(query)
            rows = result.scalars().all()

            return [
                {
                    "timestamp": row.timestamp.isoformat(),
                    "total_krw": float(row.total_krw),
                    "total_usd": float(row.total_usd),
                    "cash_krw": float(row.cash_krw),
                    "cash_usd": float(row.cash_usd),
                    "position_value_krw": float(row.position_value_krw),
                    "position_value_usd": float(row.position_value_usd),
                }
                for row in rows
            ]
