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

        expected = [
            "bars",
            "orders",
            "fills",
            "position_snapshots",
            "price_rsi",
            "equity_snapshots",
            "strategy_params",
        ]
        for table in expected:
            assert table in tables, f"Table '{table}' not found"

    async def test_initialize_idempotent(self, storage: Storage):
        """Calling initialize() twice should not raise"""
        await storage.initialize()

    async def test_sqlite_url_conversion(self):
        """sqlite:// URLs should be converted to sqlite+aiosqlite://"""
        store = Storage("sqlite:///test.db")
        assert "aiosqlite" in str(store.engine.url)
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


