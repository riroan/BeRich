"""Tests for StrategyConfig DB migration (unified strategy table)"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from decimal import Decimal

from src.core.types import Market
from src.data.storage import Storage
from src.strategy import available_strategies


# ==================== Market.from_string ====================


class TestMarketFromString:
    """Test Market.from_string() classmethod"""

    def test_valid_lowercase(self):
        assert Market.from_string("nasdaq") == Market.NASDAQ

    def test_valid_uppercase(self):
        assert Market.from_string("NYSE") == Market.NYSE

    def test_valid_mixed_case(self):
        assert Market.from_string("Amex") == Market.AMEX

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="Unknown market"):
            Market.from_string("invalid")

    def test_all_markets(self):
        for m in Market:
            assert Market.from_string(m.value) == m


# ==================== Strategy Registry ====================


class TestStrategyRegistry:
    """Test available_strategies() registry"""

    def test_returns_dict(self):
        result = available_strategies()
        assert isinstance(result, dict)
        assert len(result) >= 2  # RSI + Momentum

    def test_contains_rsi(self):
        result = available_strategies()
        rsi_paths = [
            k for k in result
            if "RSIMeanReversion" in k
        ]
        assert len(rsi_paths) == 1

    def test_contains_momentum(self):
        result = available_strategies()
        momentum_paths = [
            k for k in result
            if "Momentum" in k
        ]
        assert len(momentum_paths) == 1

    def test_class_path_format(self):
        result = available_strategies()
        for class_path, name in result.items():
            assert "." in class_path
            assert len(name) > 0


# ==================== StrategyConfig CRUD ====================


@pytest.fixture
async def storage():
    """Create an in-memory SQLite storage instance"""
    store = Storage("sqlite+aiosqlite://")
    await store.initialize()
    yield store
    await store.close()


class TestStrategyConfigCRUD:
    """Test StrategyConfig storage operations"""

    async def test_create_strategy_config(
        self, storage: Storage,
    ):
        result = await storage.create_strategy_config(
            name="TEST_RSI",
            class_path="src.strategy.builtin.rsi_mean_reversion.RSIMeanReversionStrategy",
            market="nasdaq",
            symbols=[
                {"symbol": "AAPL", "max_weight": 15.0},
            ],
            params={"rsi_period": 14},
        )
        assert result["name"] == "TEST_RSI"
        assert result["market"] == "nasdaq"
        assert result["enabled"] is True
        assert len(result["symbols"]) == 1
        assert result["params"]["rsi_period"] == 14

    async def test_get_all_strategy_configs(
        self, storage: Storage,
    ):
        await storage.create_strategy_config(
            name="A", class_path="a.B", market="nyse",
            symbols=[], params={},
        )
        await storage.create_strategy_config(
            name="B", class_path="a.B", market="nasdaq",
            symbols=[], params={},
        )
        configs = await storage.get_all_strategy_configs()
        assert len(configs) == 2
        names = [c["name"] for c in configs]
        assert "A" in names
        assert "B" in names

    async def test_get_all_empty(self, storage: Storage):
        configs = await storage.get_all_strategy_configs()
        assert configs == []

    async def test_get_strategy_config_found(
        self, storage: Storage,
    ):
        await storage.create_strategy_config(
            name="TEST", class_path="a.B", market="krx",
            symbols=[{"symbol": "005930"}],
            params={"x": 1},
        )
        result = await storage.get_strategy_config("TEST")
        assert result is not None
        assert result["name"] == "TEST"
        assert result["params"] == {"x": 1}

    async def test_get_strategy_config_not_found(
        self, storage: Storage,
    ):
        result = await storage.get_strategy_config(
            "NONEXISTENT",
        )
        assert result is None

    async def test_update_strategy_config(
        self, storage: Storage,
    ):
        await storage.create_strategy_config(
            name="UPD", class_path="a.B", market="nyse",
            symbols=[], params={"x": 1},
        )
        result = await storage.update_strategy_config(
            "UPD", params={"x": 2, "y": 3},
        )
        assert result is not None
        assert result["params"] == {"x": 2, "y": 3}

    async def test_update_enabled_toggle(
        self, storage: Storage,
    ):
        await storage.create_strategy_config(
            name="TOG", class_path="a.B", market="nyse",
            symbols=[], params={},
        )
        result = await storage.update_strategy_config(
            "TOG", enabled=False,
        )
        assert result["enabled"] is False

        result = await storage.update_strategy_config(
            "TOG", enabled=True,
        )
        assert result["enabled"] is True

    async def test_update_not_found(
        self, storage: Storage,
    ):
        result = await storage.update_strategy_config(
            "MISSING", params={"x": 1},
        )
        assert result is None

    async def test_delete_strategy_config(
        self, storage: Storage,
    ):
        await storage.create_strategy_config(
            name="DEL", class_path="a.B", market="nyse",
            symbols=[], params={},
        )
        deleted = await storage.delete_strategy_config(
            "DEL",
        )
        assert deleted is True

        configs = await storage.get_all_strategy_configs()
        assert len(configs) == 0

    async def test_delete_not_found(
        self, storage: Storage,
    ):
        deleted = await storage.delete_strategy_config(
            "MISSING",
        )
        assert deleted is False

    async def test_duplicate_name_raises(
        self, storage: Storage,
    ):
        await storage.create_strategy_config(
            name="DUP", class_path="a.B", market="nyse",
            symbols=[], params={},
        )
        with pytest.raises(Exception):
            await storage.create_strategy_config(
                name="DUP", class_path="a.B",
                market="nyse",
                symbols=[], params={},
            )


# ==================== Table Creation ====================


class TestStrategyConfigTable:
    """Test that strategy_configs table is created"""

    async def test_table_exists(self, storage: Storage):
        from sqlalchemy import inspect

        async with storage.engine.connect() as conn:
            tables = await conn.run_sync(
                lambda sc: inspect(sc).get_table_names()
            )
        assert "strategy_configs" in tables


# ==================== Load Strategies from DB ====================


class TestLoadStrategiesFromDB:
    """Test _load_strategies() reads from DB"""

    @pytest.fixture
    def bot(self, tmp_path):
        from src.bot.core import TradingBot
        from src.bot.warmup import WarmupManager
        with patch("src.bot.core.Config"):
            bot = TradingBot(config_dir=str(tmp_path))
            bot._data_dir = tmp_path
            bot._warmup = WarmupManager(warmup_hours=0)
            return bot

    @pytest.mark.asyncio
    async def test_empty_db_no_crash(self, bot):
        bot.storage = AsyncMock()
        bot.storage.get_all_strategy_configs = (
            AsyncMock(return_value=[])
        )
        bot.strategy_engine = MagicMock()

        await bot._load_strategies()

        assert bot.dashboard.strategy_names == []

    @pytest.mark.asyncio
    async def test_loads_enabled_only(self, bot):
        configs = [
            {
                "name": "ENABLED",
                "class_path": "src.strategy.builtin.rsi_mean_reversion.RSIMeanReversionStrategy",
                "market": "nasdaq",
                "enabled": True,
                "symbols": [
                    {"symbol": "AAPL", "max_weight": 15},
                ],
                "params": {"rsi_period": 14},
            },
            {
                "name": "DISABLED",
                "class_path": "src.strategy.builtin.rsi_mean_reversion.RSIMeanReversionStrategy",
                "market": "nyse",
                "enabled": False,
                "symbols": [{"symbol": "KO"}],
                "params": {"rsi_period": 14},
            },
        ]
        bot.storage = AsyncMock()
        bot.storage.get_all_strategy_configs = (
            AsyncMock(return_value=configs)
        )
        bot.strategy_engine = MagicMock()

        await bot._load_strategies()

        assert bot.dashboard.strategy_names == ["ENABLED"]
        bot.strategy_engine.register_strategy.assert_called_once()
