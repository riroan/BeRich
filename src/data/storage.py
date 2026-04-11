from datetime import datetime, timedelta
from decimal import Decimal
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select
import logging

from src.core.types import Bar, Order, Fill, Market
from .models import (
    Base, BarModel, OrderModel, FillModel,
    PriceRSIModel, EquitySnapshot,
    WatchedSymbol, StrategyParams,
    StrategyConfigModel, BotStateModel,
)

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
        """Create tables and run migrations"""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await self._migrate(conn)
        logger.info("Database initialized")

    async def _migrate(self, conn) -> None:
        """Run schema migrations for existing tables"""
        from sqlalchemy import text, inspect

        def _check_and_migrate(sync_conn):
            insp = inspect(sync_conn)
            # Add max_weight to watched_symbols if missing
            if "watched_symbols" in insp.get_table_names():
                cols = [c["name"] for c in insp.get_columns("watched_symbols")]
                if "max_weight" not in cols:
                    sync_conn.execute(text(
                        "ALTER TABLE watched_symbols "
                        "ADD COLUMN max_weight DECIMAL(5,2) "
                        "DEFAULT 20.0"
                    ))
                    logger.info("Migrated: added max_weight column")

            # Add rsi to fills if missing
            if "fills" in insp.get_table_names():
                cols = [c["name"] for c in insp.get_columns("fills")]
                if "rsi" not in cols:
                    sync_conn.execute(text(
                        "ALTER TABLE fills "
                        "ADD COLUMN rsi DECIMAL(10,4)"
                    ))
                    logger.info("Migrated: added rsi column to fills")

        await conn.run_sync(_check_and_migrate)

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

    async def save_bars(self, bars: list[Bar]) -> None:
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
    ) -> list[Bar]:
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

    async def get_order(self, order_id: str) -> Order | None:
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
                rsi=fill.rsi,
                timestamp=fill.timestamp,
            )
            session.add(fill_model)
            await session.commit()

    async def get_all_fills(self) -> list[Fill]:
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
                    rsi=float(row.rsi) if row.rsi else None,
                    timestamp=row.timestamp,
                )
                for row in rows
            ]

    async def get_fills(
        self,
        start: datetime,
        end: datetime,
        symbol: str | None = None,
    ) -> list[Fill]:
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
                    rsi=float(row.rsi) if row.rsi else None,
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
        rsi: float | None = None,
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
    ) -> list[dict]:
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

    async def get_all_symbols_with_history(self) -> list[str]:
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

    async def get_equity_history(self, days: int = 90) -> list[dict]:
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

    # ==================== Watched Symbols ====================

    async def get_watched_symbols(
        self,
        strategy_name: str | None = None,
        enabled_only: bool = True,
    ) -> list[dict]:
        """Get all watched symbols"""
        async with self.async_session() as session:
            query = select(WatchedSymbol)

            if strategy_name:
                query = query.where(WatchedSymbol.strategy_name == strategy_name)
            if enabled_only:
                query = query.where(WatchedSymbol.enabled == 1)

            query = query.order_by(WatchedSymbol.strategy_name, WatchedSymbol.symbol)
            result = await session.execute(query)
            rows = result.scalars().all()

            return [
                {
                    "id": row.id,
                    "symbol": row.symbol,
                    "market": row.market.value if row.market else None,
                    "strategy_name": row.strategy_name,
                    "enabled": bool(row.enabled),
                    "max_weight": float(row.max_weight) if row.max_weight else 20.0,
                    "created_at": row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else None,
                    "updated_at": row.updated_at.strftime("%Y-%m-%d %H:%M") if row.updated_at else None,
                }
                for row in rows
            ]

    async def add_watched_symbol(
        self,
        symbol: str,
        market: Market,
        strategy_name: str,
    ) -> dict:
        """Add a new watched symbol"""
        async with self.async_session() as session:
            # Check for duplicate
            query = select(WatchedSymbol).where(
                WatchedSymbol.symbol == symbol,
                WatchedSymbol.strategy_name == strategy_name,
            )
            result = await session.execute(query)
            existing = result.scalar_one_or_none()

            if existing:
                return {
                    "id": existing.id,
                    "symbol": existing.symbol,
                    "market": existing.market.value,
                    "strategy_name": existing.strategy_name,
                    "enabled": bool(existing.enabled),
                    "duplicate": True,
                }

            record = WatchedSymbol(
                symbol=symbol.upper(),
                market=market,
                strategy_name=strategy_name,
                enabled=1,
            )
            session.add(record)
            await session.commit()
            await session.refresh(record)

            return {
                "id": record.id,
                "symbol": record.symbol,
                "market": record.market.value,
                "strategy_name": record.strategy_name,
                "enabled": True,
                "duplicate": False,
            }

    async def remove_watched_symbol(self, symbol_id: int) -> bool:
        """Remove a watched symbol by ID"""
        async with self.async_session() as session:
            query = select(WatchedSymbol).where(WatchedSymbol.id == symbol_id)
            result = await session.execute(query)
            record = result.scalar_one_or_none()

            if not record:
                return False

            await session.delete(record)
            await session.commit()
            return True

    async def toggle_watched_symbol(self, symbol_id: int) -> dict | None:
        """Toggle enabled/disabled for a watched symbol"""
        async with self.async_session() as session:
            query = select(WatchedSymbol).where(WatchedSymbol.id == symbol_id)
            result = await session.execute(query)
            record = result.scalar_one_or_none()

            if not record:
                return None

            record.enabled = 0 if record.enabled else 1
            record.updated_at = datetime.now()
            await session.commit()

            return {
                "id": record.id,
                "symbol": record.symbol,
                "market": record.market.value,
                "strategy_name": record.strategy_name,
                "enabled": bool(record.enabled),
            }

    async def update_watched_symbol_weight(
        self, symbol_id: int, max_weight: float,
    ) -> dict | None:
        """Update max portfolio weight for a symbol"""
        async with self.async_session() as session:
            query = select(WatchedSymbol).where(
                WatchedSymbol.id == symbol_id,
            )
            result = await session.execute(query)
            record = result.scalar_one_or_none()

            if not record:
                return None

            record.max_weight = max_weight
            record.updated_at = datetime.now()
            await session.commit()

            return {
                "id": record.id,
                "symbol": record.symbol,
                "max_weight": float(record.max_weight),
            }

    async def seed_watched_symbols(self, strategies_config: list) -> int:
        """Seed watched symbols from YAML config (only if not already in DB)"""
        count = 0
        market_map = {
            "krx": Market.KRX,
            "nyse": Market.NYSE,
            "nasdaq": Market.NASDAQ,
            "amex": Market.AMEX,
        }

        for strategy in strategies_config:
            strategy_name = strategy["name"]
            market = market_map.get(strategy["market"].lower(), Market.KRX)

            for symbol in strategy.get("symbols", []):
                async with self.async_session() as session:
                    # Check if already exists
                    query = select(WatchedSymbol).where(
                        WatchedSymbol.symbol == symbol,
                        WatchedSymbol.strategy_name == strategy_name,
                    )
                    result = await session.execute(query)
                    if result.scalar_one_or_none():
                        continue

                    record = WatchedSymbol(
                        symbol=symbol,
                        market=market,
                        strategy_name=strategy_name,
                        enabled=1 if strategy.get("enabled", True) else 0,
                    )
                    session.add(record)
                    await session.commit()
                    count += 1

        return count

    # ==================== Strategy Params ====================

    async def get_strategy_params(
        self, strategy_name: str,
    ) -> dict | None:
        """Get strategy params from DB (JSON parsed)"""
        import json
        async with self.async_session() as session:
            query = select(StrategyParams).where(
                StrategyParams.strategy_name == strategy_name,
            )
            result = await session.execute(query)
            row = result.scalar_one_or_none()
            if not row:
                return None
            return json.loads(row.params_json)

    async def get_all_strategy_params(self) -> list[dict]:
        """Get all strategy params"""
        import json
        async with self.async_session() as session:
            query = select(StrategyParams).order_by(
                StrategyParams.strategy_name,
            )
            result = await session.execute(query)
            rows = result.scalars().all()
            return [
                {
                    "strategy_name": row.strategy_name,
                    "params": json.loads(row.params_json),
                    "updated_at": (
                        row.updated_at.strftime("%Y-%m-%d %H:%M")
                        if row.updated_at else None
                    ),
                }
                for row in rows
            ]

    async def save_strategy_params(
        self, strategy_name: str, params: dict,
    ) -> None:
        """Save or update strategy params"""
        import json
        async with self.async_session() as session:
            query = select(StrategyParams).where(
                StrategyParams.strategy_name == strategy_name,
            )
            result = await session.execute(query)
            existing = result.scalar_one_or_none()

            params_json = json.dumps(params)

            if existing:
                existing.params_json = params_json
                existing.updated_at = datetime.now()
            else:
                record = StrategyParams(
                    strategy_name=strategy_name,
                    params_json=params_json,
                )
                session.add(record)

            await session.commit()

    async def seed_strategy_params(
        self, strategies_config: list,
    ) -> int:
        """Seed strategy params from YAML config"""
        count = 0
        for strategy in strategies_config:
            if not strategy.get("enabled"):
                continue
            name = strategy["name"]
            params = strategy.get("params", {})
            if not params:
                continue

            existing = await self.get_strategy_params(name)
            if existing is None:
                await self.save_strategy_params(name, params)
                count += 1
        return count

    # ==================== Bot State ====================

    async def get_bot_state(self, key: str) -> str | None:
        """Get a bot state value by key"""
        async with self.async_session() as session:
            query = select(BotStateModel).where(BotStateModel.key == key)
            result = await session.execute(query)
            row = result.scalar_one_or_none()
            return row.value if row else None

    async def set_bot_state(self, key: str, value: str) -> None:
        """Set a bot state value"""
        async with self.async_session() as session:
            query = select(BotStateModel).where(BotStateModel.key == key)
            result = await session.execute(query)
            existing = result.scalar_one_or_none()

            if existing:
                existing.value = value
                existing.updated_at = datetime.now()
            else:
                record = BotStateModel(key=key, value=value)
                session.add(record)

            await session.commit()

    async def delete_bot_state(self, key: str) -> None:
        """Delete a bot state value"""
        async with self.async_session() as session:
            query = select(BotStateModel).where(BotStateModel.key == key)
            result = await session.execute(query)
            record = result.scalar_one_or_none()
            if record:
                await session.delete(record)
                await session.commit()

    # ==================== Strategy Config ====================

    async def get_all_strategy_configs(self) -> list[dict]:
        """Get all strategy configurations"""
        import json
        async with self.async_session() as session:
            query = select(StrategyConfigModel).order_by(
                StrategyConfigModel.name,
            )
            result = await session.execute(query)
            rows = result.scalars().all()
            return [
                {
                    "id": row.id,
                    "name": row.name,
                    "class_path": row.class_path,
                    "market": row.market,
                    "enabled": bool(row.enabled),
                    "symbols": json.loads(row.symbols_json),
                    "params": json.loads(row.params_json),
                    "created_at": (
                        row.created_at.strftime("%Y-%m-%d %H:%M")
                        if row.created_at else None
                    ),
                    "updated_at": (
                        row.updated_at.strftime("%Y-%m-%d %H:%M")
                        if row.updated_at else None
                    ),
                }
                for row in rows
            ]

    async def get_strategy_config(
        self, name: str,
    ) -> dict | None:
        """Get a single strategy config by name"""
        import json
        async with self.async_session() as session:
            query = select(StrategyConfigModel).where(
                StrategyConfigModel.name == name,
            )
            result = await session.execute(query)
            row = result.scalar_one_or_none()
            if not row:
                return None
            return {
                "id": row.id,
                "name": row.name,
                "class_path": row.class_path,
                "market": row.market,
                "enabled": bool(row.enabled),
                "symbols": json.loads(row.symbols_json),
                "params": json.loads(row.params_json),
            }

    async def create_strategy_config(
        self,
        name: str,
        class_path: str,
        market: str,
        symbols: list,
        params: dict,
        enabled: bool = True,
    ) -> dict:
        """Create a new strategy config"""
        import json
        async with self.async_session() as session:
            record = StrategyConfigModel(
                name=name,
                class_path=class_path,
                market=market.lower(),
                enabled=1 if enabled else 0,
                symbols_json=json.dumps(symbols),
                params_json=json.dumps(params),
            )
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return {
                "id": record.id,
                "name": record.name,
                "class_path": record.class_path,
                "market": record.market,
                "enabled": bool(record.enabled),
                "symbols": symbols,
                "params": params,
            }

    async def update_strategy_config(
        self,
        name: str,
        **kwargs,
    ) -> dict | None:
        """Update a strategy config. Pass only fields to update."""
        import json
        async with self.async_session() as session:
            query = select(StrategyConfigModel).where(
                StrategyConfigModel.name == name,
            )
            result = await session.execute(query)
            record = result.scalar_one_or_none()
            if not record:
                return None

            if "class_path" in kwargs:
                record.class_path = kwargs["class_path"]
            if "market" in kwargs:
                record.market = kwargs["market"].lower()
            if "enabled" in kwargs:
                record.enabled = (
                    1 if kwargs["enabled"] else 0
                )
            if "symbols" in kwargs:
                record.symbols_json = json.dumps(
                    kwargs["symbols"],
                )
            if "params" in kwargs:
                record.params_json = json.dumps(
                    kwargs["params"],
                )

            record.updated_at = datetime.now()
            await session.commit()
            return {
                "id": record.id,
                "name": record.name,
                "class_path": record.class_path,
                "market": record.market,
                "enabled": bool(record.enabled),
                "symbols": json.loads(record.symbols_json),
                "params": json.loads(record.params_json),
            }

    async def delete_strategy_config(
        self, name: str,
    ) -> bool:
        """Delete a strategy config. Returns True if deleted."""
        async with self.async_session() as session:
            query = select(StrategyConfigModel).where(
                StrategyConfigModel.name == name,
            )
            result = await session.execute(query)
            record = result.scalar_one_or_none()
            if not record:
                return False
            await session.delete(record)
            await session.commit()
            return True
