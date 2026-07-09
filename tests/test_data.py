"""Tests for data module (models + storage)"""

import pytest
from decimal import Decimal
from datetime import datetime

from src.core.types import Market, OrderSide, OrderStatus, OrderType, Order
from src.data.storage import Storage


@pytest.fixture
async def storage():
    """Create an in-memory SQLite storage instance"""
    store = Storage("sqlite+aiosqlite://")
    await store.initialize()
    yield store
    await store.close()


class TestStorageInitialization:
    """Test database initialization and table creation"""

    async def test_initialize_creates_tables(self, storage: Storage):
        """Tables should exist after initialize()"""
        from sqlalchemy import inspect

        async with storage.engine.connect() as conn:
            tables = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).get_table_names()
            )
            current_position_columns = await conn.run_sync(
                lambda sync_conn: {
                    col["name"]
                    for col in inspect(sync_conn).get_columns("current_positions")
                }
            )
            equity_columns = await conn.run_sync(
                lambda sync_conn: {
                    col["name"]
                    for col in inspect(sync_conn).get_columns("equity_snapshots")
                }
            )

        expected = [
            "bars",
            "orders",
            "fills",
            "position_snapshots",
            "current_positions",
            "price_rsi",
            "equity_snapshots",
            "strategy_params",
        ]
        for table in expected:
            assert table in tables, f"Table '{table}' not found"
        assert "current_price" not in current_position_columns
        assert "pnl" not in current_position_columns
        assert "pnl_pct" not in current_position_columns
        assert "rsi" not in current_position_columns
        assert "adjusted_total_usd" in equity_columns
        assert "settlement_adjustment_usd" in equity_columns

    async def test_initialize_idempotent(self, storage: Storage):
        """Calling initialize() twice should not raise"""
        await storage.initialize()

    async def test_sqlite_url_conversion(self):
        """sqlite:// URLs should be converted to sqlite+aiosqlite://"""
        store = Storage("sqlite:///test.db")
        assert "aiosqlite" in str(store.engine.url)
        await store.close()

    async def test_mysql_url_conversion(self):
        """mysql:// URLs should be converted to mysql+aiomysql://"""
        store = Storage("mysql://user:pass@localhost:3306/berich")
        assert "aiomysql" in str(store.engine.url)
        await store.close()

    async def test_mysql_engine_uses_stale_connection_guards(self):
        """MySQL engine should recycle and pre-ping pooled connections."""
        store = Storage("mysql+aiomysql://user:pass@localhost:3306/berich")
        pool = store.engine.sync_engine.pool

        assert pool.size() == 5
        assert pool._max_overflow == 10
        assert pool._recycle == 3600
        assert pool._pre_ping is True

        await store.close()



class TestSaveOrder:
    """Test order CRUD operations"""

    async def test_save_and_get_order(self, storage: Storage):
        """Save an order then retrieve it by order_id"""
        order = Order(
            symbol="005930",
            market=Market.KRX,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=10,
            price=Decimal("70000"),
            order_id="ORD001",
            status=OrderStatus.SUBMITTED,
        )
        await storage.save_order(order)

        result = await storage.get_order("ORD001")
        assert result is not None
        assert result.order_id == "ORD001"
        assert result.symbol == "005930"
        assert result.market == Market.KRX
        assert result.side == OrderSide.BUY
        assert result.quantity == 10
        assert result.status == OrderStatus.SUBMITTED

    async def test_save_order_updates_existing(self, storage: Storage):
        """Saving an order with the same order_id should update it"""
        order = Order(
            symbol="005930",
            market=Market.KRX,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=10,
            price=Decimal("70000"),
            order_id="ORD002",
            status=OrderStatus.SUBMITTED,
        )
        await storage.save_order(order)

        # Update status
        order.status = OrderStatus.FILLED
        order.filled_quantity = 10
        order.filled_avg_price = Decimal("70000")
        await storage.save_order(order)

        result = await storage.get_order("ORD002")
        assert result is not None
        assert result.status == OrderStatus.FILLED
        assert result.filled_quantity == 10
        assert result.filled_avg_price == Decimal("70000")

    async def test_get_order_not_found(self, storage: Storage):
        """Getting a non-existent order should return None"""
        result = await storage.get_order("NONEXISTENT")
        assert result is None

    async def test_get_open_orders_only_returns_open(
        self, storage: Storage,
    ):
        """get_open_orders returns SUBMITTED/PARTIAL_FILLED, not terminal"""
        statuses = {
            "OPEN-S": OrderStatus.SUBMITTED,
            "OPEN-P": OrderStatus.PARTIAL_FILLED,
            "DONE-F": OrderStatus.FILLED,
            "DONE-C": OrderStatus.CANCELLED,
        }
        for oid, status in statuses.items():
            await storage.save_order(Order(
                symbol="AAPL",
                market=Market.NASDAQ,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=5,
                price=Decimal("100"),
                order_id=oid,
                status=status,
            ))

        open_orders = await storage.get_open_orders()
        ids = {o.order_id for o in open_orders}
        assert ids == {"OPEN-S", "OPEN-P"}



class TestStrategyParams:
    """Test strategy parameter CRUD operations"""

    async def test_save_and_get_strategy_params(self, storage: Storage):
        """Save params then retrieve them"""
        params = {"rsi_period": 14, "stop_loss": -10, "levels": [30, 25, 20]}
        await storage.save_strategy_params("RSI_MeanReversion", params)

        result = await storage.get_strategy_params("RSI_MeanReversion")
        assert result is not None
        assert result["rsi_period"] == 14
        assert result["stop_loss"] == -10
        assert result["levels"] == [30, 25, 20]

    async def test_save_strategy_params_updates_existing(self, storage: Storage):
        """Saving params for the same strategy should update them"""
        await storage.save_strategy_params("strat_a", {"x": 1})
        await storage.save_strategy_params("strat_a", {"x": 2, "y": 3})

        result = await storage.get_strategy_params("strat_a")
        assert result == {"x": 2, "y": 3}

    async def test_get_strategy_params_not_found(self, storage: Storage):
        """Getting params for non-existent strategy returns None"""
        result = await storage.get_strategy_params("nonexistent")
        assert result is None

    async def test_get_all_strategy_params(self, storage: Storage):
        """Get all strategy params"""
        await storage.save_strategy_params("strat_a", {"x": 1})
        await storage.save_strategy_params("strat_b", {"y": 2})

        all_params = await storage.get_all_strategy_params()
        assert len(all_params) == 2
        names = [p["strategy_name"] for p in all_params]
        assert "strat_a" in names
        assert "strat_b" in names

        # Each entry should have params and updated_at
        for p in all_params:
            assert "params" in p
            assert "updated_at" in p

    async def test_seed_strategy_params(self, storage: Storage):
        """Seed params from config, only if not already in DB"""
        config = [
            {"name": "strat_a", "enabled": True, "params": {"x": 1}},
            {"name": "strat_b", "enabled": True, "params": {"y": 2}},
            {"name": "strat_c", "enabled": False, "params": {"z": 3}},  # disabled
        ]
        count = await storage.seed_strategy_params(config)
        assert count == 2  # strat_c is disabled

        # Seeding again should not duplicate
        count = await storage.seed_strategy_params(config)
        assert count == 0


class TestCurrentPositions:
    """Test DB-backed current position state."""

    @pytest.mark.asyncio
    async def test_replace_and_get_current_positions(self, storage: Storage):
        changed = await storage.replace_current_positions_for_market(
            Market.NASDAQ,
            [
                {
                    "symbol": "aapl",
                    "quantity": 2,
                    "avg_price": 100,
                    "buy_stage": 1,
                    "sell_stage": 2,
                    "stage_cooldown_days": 7,
                    "last_buy_date": "2026-06-20T09:30:00",
                    "last_sell_date": "2026-06-21T10:45:00",
                    "stop_loss_pct": -8,
                },
            ],
        )
        await storage.save_price_rsi(
            symbol="AAPL",
            market=Market.NASDAQ,
            price=Decimal("110"),
            rsi=42.5,
        )

        positions = await storage.get_current_positions()

        assert changed is True
        assert len(positions) == 1
        assert positions[0]["symbol"] == "AAPL"
        assert positions[0]["market"] == "NASDAQ"
        assert positions[0]["current_price"] == 110.0
        assert positions[0]["pnl"] == 20.0
        assert positions[0]["pnl_pct"] == 10.0
        assert positions[0]["stop_loss_distance"] == 18.0
        assert positions[0]["rsi"] == 42.5
        assert positions[0]["buy_stage"] == 1
        assert positions[0]["sell_stage"] == 2
        assert positions[0]["stage_cooldown_days"] == 7
        assert positions[0]["last_buy_date"] == "2026-06-20T09:30:00"
        assert positions[0]["last_sell_date"] == "2026-06-21T10:45:00"

    @pytest.mark.asyncio
    async def test_replace_current_positions_skips_unchanged_state(
        self,
        storage: Storage,
    ):
        position = {
            "symbol": "AAPL",
            "quantity": 1,
            "avg_price": 100,
            "buy_stage": 0,
        }

        first = await storage.replace_current_positions_for_market(
            Market.NASDAQ,
            [position],
        )
        second = await storage.replace_current_positions_for_market(
            Market.NASDAQ,
            [position],
        )

        assert first is True
        assert second is False

    @pytest.mark.asyncio
    async def test_replace_current_positions_only_clears_that_market(
        self,
        storage: Storage,
    ):
        await storage.replace_current_positions_for_market(
            Market.NASDAQ,
            [{"symbol": "AAPL", "quantity": 1, "avg_price": 100}],
        )
        await storage.replace_current_positions_for_market(
            Market.NYSE,
            [{"symbol": "KO", "quantity": 3, "avg_price": 70}],
        )

        await storage.replace_current_positions_for_market(Market.NASDAQ, [])

        positions = await storage.get_current_positions()
        assert [p["symbol"] for p in positions] == ["KO"]
        assert positions[0]["market"] == "NYSE"




class TestFillReasonPersistence:
    """Option 2: a fill's reason persists so partial_sell/stop_loss labels
    survive a restart."""

    @pytest.mark.asyncio
    async def test_save_and_get_fill_reason(self, storage: Storage):
        from src.core.types import Fill

        await storage.save_fill(Fill(
            order_id="O1", symbol="BAC", market=Market.NYSE,
            side=OrderSide.SELL, quantity=1, price=Decimal("55.77"),
            commission=Decimal("0"), timestamp=datetime.now(),
            pnl=Decimal("6.27"), rsi=74.1, reason="staged_sell_1",
        ))

        fills = await storage.get_all_fills()
        assert len(fills) == 1
        assert fills[0].reason == "staged_sell_1"
        assert fills[0].pnl == Decimal("6.27")

    @pytest.mark.asyncio
    async def test_fill_reason_defaults_none(self, storage: Storage):
        from src.core.types import Fill

        await storage.save_fill(Fill(
            order_id="O2", symbol="AAPL", market=Market.NASDAQ,
            side=OrderSide.BUY, quantity=1, price=Decimal("100"),
            commission=Decimal("0"), timestamp=datetime.now(),
        ))
        fills = await storage.get_all_fills()
        assert fills[0].reason is None


class TestEquitySnapshots:
    """Equity snapshot persistence."""

    @pytest.mark.asyncio
    async def test_save_equity_snapshot_defaults_adjusted_total(self, storage: Storage):
        await storage.save_equity_snapshot(
            total_krw=Decimal("0"),
            total_usd=Decimal("1000"),
            cash_krw=Decimal("0"),
            cash_usd=Decimal("400"),
            position_value_krw=Decimal("0"),
            position_value_usd=Decimal("600"),
        )

        history = await storage.get_equity_history(days=1)

        assert history[0]["total_usd"] == 1000.0
        assert history[0]["adjusted_total_usd"] == 1000.0
        assert history[0]["settlement_adjustment_usd"] == 0.0

    @pytest.mark.asyncio
    async def test_save_equity_snapshot_with_settlement_adjustment(
        self,
        storage: Storage,
    ):
        await storage.save_equity_snapshot(
            total_krw=Decimal("0"),
            total_usd=Decimal("1000"),
            cash_krw=Decimal("0"),
            cash_usd=Decimal("400"),
            position_value_krw=Decimal("0"),
            position_value_usd=Decimal("600"),
            adjusted_total_usd=Decimal("1050"),
            settlement_adjustment_usd=Decimal("50"),
        )

        history = await storage.get_equity_history(days=1)

        assert history[0]["adjusted_total_usd"] == 1050.0
        assert history[0]["settlement_adjustment_usd"] == 50.0
