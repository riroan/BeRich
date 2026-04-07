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
            "watched_symbols",
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


class TestMigration:
    """Test schema migration logic"""

    async def test_migrate_adds_max_weight_column(self):
        """Migration should add max_weight if missing"""
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy import text

        engine = create_async_engine("sqlite+aiosqlite://", echo=False)

        # Create watched_symbols table WITHOUT max_weight
        async with engine.begin() as conn:
            await conn.execute(text(
                "CREATE TABLE watched_symbols ("
                "  id INTEGER PRIMARY KEY,"
                "  symbol VARCHAR(20) NOT NULL,"
                "  market VARCHAR(10) NOT NULL,"
                "  strategy_name VARCHAR(100) NOT NULL,"
                "  enabled INTEGER DEFAULT 1,"
                "  created_at DATETIME,"
                "  updated_at DATETIME"
                ")"
            ))

        # Now run Storage.initialize which calls _migrate
        store = Storage("sqlite+aiosqlite://")
        # Replace the engine with our pre-seeded one
        store.engine = engine
        from sqlalchemy.ext.asyncio import AsyncSession
        from sqlalchemy.orm import sessionmaker
        store.async_session = sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False,
        )

        async with engine.begin() as conn:
            await store._migrate(conn)

        # Verify max_weight column exists now
        async with engine.connect() as conn:
            from sqlalchemy import inspect
            cols = await conn.run_sync(
                lambda sc: [c["name"] for c in inspect(sc).get_columns("watched_symbols")]
            )
        assert "max_weight" in cols

        await engine.dispose()


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


class TestWatchedSymbols:
    """Test watched symbol CRUD operations"""

    async def test_add_watched_symbol(self, storage: Storage):
        """Add a watched symbol and verify it appears in the list"""
        result = await storage.add_watched_symbol(
            symbol="aapl",
            market=Market.NASDAQ,
            strategy_name="RSI_MeanReversion",
        )

        assert result["duplicate"] is False
        assert result["symbol"] == "AAPL"  # should be uppercased
        assert result["enabled"] is True

    async def test_add_duplicate_watched_symbol(self, storage: Storage):
        """Adding the same symbol+strategy should return duplicate flag"""
        await storage.add_watched_symbol(
            symbol="MSFT",
            market=Market.NASDAQ,
            strategy_name="RSI_MeanReversion",
        )
        result = await storage.add_watched_symbol(
            symbol="MSFT",
            market=Market.NASDAQ,
            strategy_name="RSI_MeanReversion",
        )
        assert result["duplicate"] is True

    async def test_get_watched_symbols_all(self, storage: Storage):
        """Get all watched symbols"""
        await storage.add_watched_symbol("AAPL", Market.NASDAQ, "strat_a")
        await storage.add_watched_symbol("MSFT", Market.NASDAQ, "strat_a")

        symbols = await storage.get_watched_symbols()
        assert len(symbols) == 2
        names = [s["symbol"] for s in symbols]
        assert "AAPL" in names
        assert "MSFT" in names

    async def test_get_watched_symbols_by_strategy(self, storage: Storage):
        """Filter watched symbols by strategy name"""
        await storage.add_watched_symbol("AAPL", Market.NASDAQ, "strat_a")
        await storage.add_watched_symbol("005930", Market.KRX, "strat_b")

        symbols = await storage.get_watched_symbols(strategy_name="strat_a")
        assert len(symbols) == 1
        assert symbols[0]["symbol"] == "AAPL"

    async def test_get_watched_symbols_enabled_only(self, storage: Storage):
        """enabled_only filter should exclude disabled symbols"""
        result = await storage.add_watched_symbol("AAPL", Market.NASDAQ, "strat_a")
        symbol_id = result["id"]

        # Disable the symbol
        await storage.toggle_watched_symbol(symbol_id)

        # enabled_only=True (default) should return empty
        symbols = await storage.get_watched_symbols()
        assert len(symbols) == 0

        # enabled_only=False should return it
        symbols = await storage.get_watched_symbols(enabled_only=False)
        assert len(symbols) == 1

    async def test_remove_watched_symbol(self, storage: Storage):
        """Remove a watched symbol by ID"""
        result = await storage.add_watched_symbol("AAPL", Market.NASDAQ, "strat_a")
        symbol_id = result["id"]

        removed = await storage.remove_watched_symbol(symbol_id)
        assert removed is True

        symbols = await storage.get_watched_symbols(enabled_only=False)
        assert len(symbols) == 0

    async def test_remove_watched_symbol_not_found(self, storage: Storage):
        """Removing a non-existent symbol should return False"""
        removed = await storage.remove_watched_symbol(99999)
        assert removed is False

    async def test_toggle_watched_symbol(self, storage: Storage):
        """Toggle a watched symbol between enabled and disabled"""
        result = await storage.add_watched_symbol("AAPL", Market.NASDAQ, "strat_a")
        symbol_id = result["id"]

        # Toggle off
        toggled = await storage.toggle_watched_symbol(symbol_id)
        assert toggled is not None
        assert toggled["enabled"] is False

        # Toggle on
        toggled = await storage.toggle_watched_symbol(symbol_id)
        assert toggled is not None
        assert toggled["enabled"] is True

    async def test_toggle_watched_symbol_not_found(self, storage: Storage):
        """Toggling a non-existent symbol should return None"""
        result = await storage.toggle_watched_symbol(99999)
        assert result is None

    async def test_update_symbol_weight(self, storage: Storage):
        """Update the max_weight of a watched symbol"""
        result = await storage.add_watched_symbol("AAPL", Market.NASDAQ, "strat_a")
        symbol_id = result["id"]

        updated = await storage.update_watched_symbol_weight(symbol_id, 15.5)
        assert updated is not None
        assert updated["max_weight"] == 15.5
        assert updated["symbol"] == "AAPL"

    async def test_update_symbol_weight_not_found(self, storage: Storage):
        """Updating weight of a non-existent symbol should return None"""
        result = await storage.update_watched_symbol_weight(99999, 10.0)
        assert result is None

    async def test_watched_symbol_dict_fields(self, storage: Storage):
        """Returned dicts should contain expected keys"""
        await storage.add_watched_symbol("AAPL", Market.NASDAQ, "strat_a")
        symbols = await storage.get_watched_symbols()
        assert len(symbols) == 1

        s = symbols[0]
        assert "id" in s
        assert "symbol" in s
        assert "market" in s
        assert "strategy_name" in s
        assert "enabled" in s
        assert "max_weight" in s
        assert "created_at" in s
        assert "updated_at" in s
        assert s["market"] == "nasdaq"


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


class TestSeedWatchedSymbols:
    """Test seeding watched symbols from config"""

    async def test_seed_watched_symbols(self, storage: Storage):
        """Seed symbols from config"""
        config = [
            {
                "name": "strat_a",
                "market": "nasdaq",
                "symbols": ["AAPL", "MSFT"],
                "enabled": True,
            },
            {
                "name": "strat_b",
                "market": "krx",
                "symbols": ["005930"],
                "enabled": False,
            },
        ]
        count = await storage.seed_watched_symbols(config)
        assert count == 3

        # Seeding again should not duplicate
        count = await storage.seed_watched_symbols(config)
        assert count == 0
