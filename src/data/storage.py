from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import delete, select
from sqlalchemy.exc import DBAPIError
import logging

from src.core.types import Bar, Order, Fill, Market, OrderStatus
from .models import (
    Base, BarModel, OrderModel, FillModel,
    CurrentPositionModel, PriceRSIModel, EquitySnapshot,
    StrategyParams,
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

        engine_kwargs: dict[str, Any] = {"echo": False}
        if database_url.startswith("mysql+aiomysql://"):
            # MySQL closes idle connections (wait_timeout) and Dockerized DB
            # restarts can leave stale sockets in SQLAlchemy's pool.  Validate
            # pooled connections before use and recycle them periodically so
            # scheduled account/equity snapshot writes do not fail with
            # "Lost connection to MySQL server during query".
            engine_kwargs.update(
                pool_size=5,
                max_overflow=10,
                pool_recycle=3600,
                pool_pre_ping=True,
            )

        self.engine = create_async_engine(database_url, **engine_kwargs)
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

            def _column_names(table_name: str) -> set[str]:
                if hasattr(insp, "clear_cache"):
                    insp.clear_cache()
                return {c["name"] for c in insp.get_columns(table_name)}

            def _is_duplicate_column_error(exc: DBAPIError) -> bool:
                orig = getattr(exc, "orig", exc)
                args = getattr(orig, "args", ())
                if args and args[0] == 1060:
                    return True
                return "duplicate column" in str(orig).lower()

            def _add_column_if_missing(
                table_name: str,
                column_name: str,
                ddl: str,
                log_message: str,
            ) -> set[str]:
                cols = _column_names(table_name)
                if column_name in cols:
                    return cols
                try:
                    sync_conn.execute(text(ddl))
                    logger.info(log_message)
                except DBAPIError as exc:
                    if not _is_duplicate_column_error(exc):
                        raise
                    logger.info(
                        "Migration skipped: %s.%s already exists",
                        table_name,
                        column_name,
                    )
                return _column_names(table_name)

            # Add rsi to fills if missing
            if "fills" in insp.get_table_names():
                cols = [c["name"] for c in insp.get_columns("fills")]
                if "rsi" not in cols:
                    sync_conn.execute(text(
                        "ALTER TABLE fills "
                        "ADD COLUMN rsi DECIMAL(10,4)"
                    ))
                    logger.info("Migrated: added rsi column to fills")
                # Add reason so partial_sell / stop_loss survive restart
                if "reason" not in cols:
                    sync_conn.execute(text(
                        "ALTER TABLE fills "
                        "ADD COLUMN reason VARCHAR(40)"
                    ))
                    logger.info("Migrated: added reason column to fills")

            if "current_positions" in insp.get_table_names():
                cols = _column_names("current_positions")
                obsolete_cols = {
                    "current_price",
                    "pnl",
                    "pnl_pct",
                    "rsi",
                    "stop_loss_distance",
                }
                if cols & obsolete_cols:
                    CurrentPositionModel.__table__.drop(sync_conn, checkfirst=True)
                    CurrentPositionModel.__table__.create(sync_conn, checkfirst=True)
                    logger.info(
                        "Migrated: recreated current_positions with holding-only schema"
                    )
                    cols = _column_names("current_positions")
                elif "last_sell_date" not in cols:
                    cols = _add_column_if_missing(
                        "current_positions",
                        "last_sell_date",
                        "ALTER TABLE current_positions "
                        "ADD COLUMN last_sell_date VARCHAR(20)",
                        "Migrated: added last_sell_date column "
                        "to current_positions",
                    )
                if "stage_cooldown_days" not in cols:
                    _add_column_if_missing(
                        "current_positions",
                        "stage_cooldown_days",
                        "ALTER TABLE current_positions "
                        "ADD COLUMN stage_cooldown_days INTEGER NOT NULL DEFAULT 0",
                        "Migrated: added stage_cooldown_days column "
                        "to current_positions",
                    )

            if "equity_snapshots" in insp.get_table_names():
                cols = {c["name"] for c in insp.get_columns("equity_snapshots")}
                if "adjusted_total_usd" not in cols:
                    sync_conn.execute(text(
                        "ALTER TABLE equity_snapshots "
                        "ADD COLUMN adjusted_total_usd DECIMAL(20,2)"
                    ))
                    sync_conn.execute(text(
                        "UPDATE equity_snapshots "
                        "SET adjusted_total_usd = total_usd "
                        "WHERE adjusted_total_usd IS NULL"
                    ))
                    logger.info(
                        "Migrated: added adjusted_total_usd column "
                        "to equity_snapshots"
                    )
                if "settlement_adjustment_usd" not in cols:
                    sync_conn.execute(text(
                        "ALTER TABLE equity_snapshots "
                        "ADD COLUMN settlement_adjustment_usd "
                        "DECIMAL(20,2) NOT NULL DEFAULT 0"
                    ))
                    logger.info(
                        "Migrated: added settlement_adjustment_usd column "
                        "to equity_snapshots"
                    )

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

    async def get_open_orders(self) -> list[Order]:
        """Orders still SUBMITTED/PARTIAL_FILLED.

        Used at startup to re-reconcile orders that were open when a
        previous process exited (otherwise they stay SUBMITTED forever
        since KISBroker._orders is memory-only).
        """
        async with self.async_session() as session:
            query = select(OrderModel).where(
                OrderModel.status.in_(
                    [OrderStatus.SUBMITTED, OrderStatus.PARTIAL_FILLED]
                )
            )
            result = await session.execute(query)
            rows = result.scalars().all()

            return [
                Order(
                    symbol=row.symbol,
                    market=row.market,
                    side=row.side,
                    order_type=(
                        row.order_type
                        if hasattr(row, "order_type") else None
                    ),
                    quantity=row.quantity,
                    price=Decimal(str(row.price)) if row.price else None,
                    order_id=row.order_id,
                    status=row.status,
                    created_at=row.created_at,
                    filled_quantity=row.filled_quantity,
                    filled_avg_price=(
                        Decimal(str(row.filled_avg_price))
                        if row.filled_avg_price else None
                    ),
                )
                for row in rows
            ]

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
                reason=fill.reason,
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
                    reason=row.reason,
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
                    reason=row.reason,
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
        before: datetime | None = None,
    ) -> list[dict]:
        """Get recent price/RSI history for a symbol.

        If ``before`` is given, returns records strictly older than that
        timestamp (used for cursor-based pagination when scrolling back).
        """
        async with self.async_session() as session:
            query = (
                select(PriceRSIModel)
                .where(PriceRSIModel.symbol == symbol)
            )
            if before is not None:
                query = query.where(PriceRSIModel.timestamp < before)
            query = (
                query.order_by(PriceRSIModel.timestamp.desc())
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

    async def get_daily_ohlc_rsi(
        self, symbol: str, limit: int = 250,
    ) -> list[dict]:
        """Aggregate per-tick price/RSI history into daily OHLC candles.

        Each stored-local calendar day becomes one candle: open = first
        tick of the day, high/low = intraday extremes, close = the last
        tick, rsi = that last tick's RSI. Days without a closing RSI are
        skipped so price and RSI stay index-aligned (same rule as
        get_price_rsi_history). Returns the most recent ``limit`` days in
        chronological order.
        """
        from sqlalchemy import text

        sql = text(
            """
            WITH daily AS (
                SELECT date(timestamp) AS d,
                       MIN(timestamp)  AS mn,
                       MAX(timestamp)  AS mx,
                       MAX(price)      AS hi,
                       MIN(price)      AS lo
                FROM price_rsi
                WHERE symbol = :symbol
                GROUP BY date(timestamp)
            )
            SELECT daily.d AS bar_day,
                   o.price AS open,
                   daily.hi AS high,
                   daily.lo AS low,
                   c.price AS close,
                   c.rsi   AS rsi
            FROM daily
            JOIN price_rsi o
              ON o.symbol = :symbol AND o.timestamp = daily.mn
            JOIN price_rsi c
              ON c.symbol = :symbol AND c.timestamp = daily.mx
            WHERE c.rsi IS NOT NULL
            ORDER BY daily.d DESC
            LIMIT :limit
            """
        )

        async with self.async_session() as session:
            result = await session.execute(
                sql, {"symbol": symbol, "limit": limit},
            )
            rows = result.mappings().all()

        out = []
        for row in reversed(rows):  # chronological order
            day = row["bar_day"]
            day_str = (
                day.isoformat() if hasattr(day, "isoformat") else str(day)[:10]
            )
            out.append({
                "day": day_str,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "rsi": float(row["rsi"]),
            })
        return out

    async def get_all_symbols_with_history(self) -> list[str]:
        """Get all symbols that have price/RSI history"""
        async with self.async_session() as session:
            from sqlalchemy import distinct
            query = select(distinct(PriceRSIModel.symbol))
            result = await session.execute(query)
            return [row[0] for row in result.all()]

    # ==================== Current Positions ====================

    async def replace_current_positions_for_market(
        self,
        market: Market | str,
        positions: list[dict],
    ) -> bool:
        """Replace current-position rows for a market when holding state changed."""
        market_enum = (
            market if isinstance(market, Market)
            else Market.from_string(str(market))
        )

        async with self.async_session() as session:
            incoming = [
                {
                    "symbol": str(position["symbol"]).upper(),
                    "quantity": int(position["quantity"]),
                    "avg_price": Decimal(str(position["avg_price"])),
                    "buy_stage": int(position.get("buy_stage", 0)),
                    "sell_stage": int(position.get("sell_stage", 0)),
                    "max_buy_stages": int(position.get("max_buy_stages", 3)),
                    "max_sell_stages": int(position.get("max_sell_stages", 3)),
                    "stage_cooldown_days": int(
                        position.get("stage_cooldown_days", 0)
                    ),
                    "last_buy_date": position.get("last_buy_date"),
                    "last_sell_date": position.get("last_sell_date"),
                    "stop_loss_pct": Decimal(str(
                        position.get("stop_loss_pct", -10.0),
                    )),
                }
                for position in positions
            ]
            incoming.sort(key=lambda item: item["symbol"])

            existing_result = await session.execute(
                select(CurrentPositionModel)
                .where(CurrentPositionModel.market == market_enum)
                .order_by(CurrentPositionModel.symbol)
            )
            existing = existing_result.scalars().all()

            def _state_tuple(item) -> tuple:
                if isinstance(item, dict):
                    return (
                        item["symbol"],
                        item["quantity"],
                        item["avg_price"],
                        item["buy_stage"],
                        item["sell_stage"],
                        item["max_buy_stages"],
                        item["max_sell_stages"],
                        item["stage_cooldown_days"],
                        item["last_buy_date"],
                        item["last_sell_date"],
                        item["stop_loss_pct"],
                    )
                return (
                    item.symbol,
                    item.quantity,
                    Decimal(str(item.avg_price)),
                    item.buy_stage,
                    item.sell_stage,
                    item.max_buy_stages,
                    item.max_sell_stages,
                    item.stage_cooldown_days,
                    item.last_buy_date,
                    item.last_sell_date,
                    Decimal(str(item.stop_loss_pct)),
                )

            if [_state_tuple(row) for row in existing] == [
                _state_tuple(row) for row in incoming
            ]:
                return False

            await session.execute(
                delete(CurrentPositionModel).where(
                    CurrentPositionModel.market == market_enum,
                )
            )

            now = datetime.now()
            for position in incoming:
                session.add(CurrentPositionModel(
                    symbol=position["symbol"],
                    market=market_enum,
                    quantity=position["quantity"],
                    avg_price=position["avg_price"],
                    buy_stage=position["buy_stage"],
                    sell_stage=position["sell_stage"],
                    max_buy_stages=position["max_buy_stages"],
                    max_sell_stages=position["max_sell_stages"],
                    stage_cooldown_days=position["stage_cooldown_days"],
                    last_buy_date=position["last_buy_date"],
                    last_sell_date=position["last_sell_date"],
                    stop_loss_pct=position["stop_loss_pct"],
                    updated_at=now,
                ))

            await session.commit()
            return True

    async def get_current_positions(
        self,
        market: Market | str | None = None,
    ) -> list[dict]:
        """Get current-position rows for dashboard rendering."""
        async with self.async_session() as session:
            query = select(CurrentPositionModel)
            if market is not None:
                market_enum = (
                    market if isinstance(market, Market)
                    else Market.from_string(str(market))
                )
                query = query.where(CurrentPositionModel.market == market_enum)
            query = query.order_by(
                CurrentPositionModel.market,
                CurrentPositionModel.symbol,
            )

            result = await session.execute(query)
            rows = result.scalars().all()

            positions = []
            for row in rows:
                latest_result = await session.execute(
                    select(PriceRSIModel)
                    .where(
                        PriceRSIModel.symbol == row.symbol,
                        PriceRSIModel.market == row.market,
                    )
                    .order_by(PriceRSIModel.timestamp.desc())
                    .limit(1)
                )
                latest = latest_result.scalar_one_or_none()

                avg_price = Decimal(str(row.avg_price))
                current_price = Decimal(str(latest.price)) if latest else avg_price
                pnl = (current_price - avg_price) * row.quantity
                pnl_pct = (
                    (current_price - avg_price) / avg_price * Decimal("100")
                    if avg_price else Decimal("0")
                )
                stop_loss_pct = Decimal(str(row.stop_loss_pct))
                stop_loss_distance = pnl_pct - stop_loss_pct

                positions.append({
                    "symbol": row.symbol,
                    "market": (
                        row.market.value.upper()
                        if isinstance(row.market, Market)
                        else str(row.market).upper()
                    ),
                    "quantity": row.quantity,
                    "avg_price": float(avg_price),
                    "current_price": float(current_price),
                    "pnl": float(pnl),
                    "pnl_pct": float(pnl_pct),
                    "rsi": (
                        float(latest.rsi)
                        if latest and latest.rsi is not None else None
                    ),
                    "buy_stage": row.buy_stage,
                    "sell_stage": row.sell_stage,
                    "max_buy_stages": row.max_buy_stages,
                    "max_sell_stages": row.max_sell_stages,
                    "stage_cooldown_days": row.stage_cooldown_days,
                    "last_buy_date": row.last_buy_date,
                    "last_sell_date": row.last_sell_date,
                    "stop_loss_pct": float(stop_loss_pct),
                    "stop_loss_distance": float(stop_loss_distance),
                    "updated_at": (
                        row.updated_at.isoformat()
                        if row.updated_at else None
                    ),
                    "price_updated_at": (
                        latest.timestamp.isoformat()
                        if latest and latest.timestamp else None
                    ),
                })
            return positions

    # ==================== Equity Snapshots ====================

    async def save_equity_snapshot(
        self,
        total_krw: Decimal,
        total_usd: Decimal,
        cash_krw: Decimal,
        cash_usd: Decimal,
        position_value_krw: Decimal,
        position_value_usd: Decimal,
        adjusted_total_usd: Decimal | None = None,
        settlement_adjustment_usd: Decimal = Decimal("0"),
    ) -> None:
        """Save equity snapshot"""
        async with self.async_session() as session:
            adjusted_total = (
                adjusted_total_usd if adjusted_total_usd is not None else total_usd
            )
            snapshot = EquitySnapshot(
                timestamp=datetime.now(),
                total_krw=total_krw,
                total_usd=total_usd,
                cash_krw=cash_krw,
                cash_usd=cash_usd,
                position_value_krw=position_value_krw,
                position_value_usd=position_value_usd,
                adjusted_total_usd=adjusted_total,
                settlement_adjustment_usd=settlement_adjustment_usd,
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
                    "adjusted_total_usd": float(
                        row.adjusted_total_usd
                        if row.adjusted_total_usd is not None else row.total_usd
                    ),
                    "settlement_adjustment_usd": float(
                        row.settlement_adjustment_usd or 0
                    ),
                }
                for row in rows
            ]

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
                    "updated_at": f"{row.updated_at:%Y-%m-%d %H:%M}" if row.updated_at else None,
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
            if not (params := strategy.get("params", {})):
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
                    "created_at": f"{row.created_at:%Y-%m-%d %H:%M}" if row.created_at else None,
                    "updated_at": f"{row.updated_at:%Y-%m-%d %H:%M}" if row.updated_at else None,
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

    async def get_strategy_config_by_id(
        self, config_id: int,
    ) -> dict | None:
        """Get a single strategy config by id"""
        import json
        async with self.async_session() as session:
            query = select(StrategyConfigModel).where(
                StrategyConfigModel.id == config_id,
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
