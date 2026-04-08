"""Tests for WarmupManager"""

import pytest
import pytest_asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from src.bot.warmup import WarmupManager, WARMUP_KEY


class TestWarmupManager:
    """Test cases for WarmupManager"""

    @pytest.fixture
    def mock_storage(self):
        """Create a mock storage"""
        storage = MagicMock()
        storage.get_bot_state = AsyncMock(return_value=None)
        storage.set_bot_state = AsyncMock()
        storage.delete_bot_state = AsyncMock()
        return storage

    @pytest.fixture
    def warmup_manager(self, mock_storage):
        """Create a WarmupManager instance"""
        manager = WarmupManager(warmup_hours=2)
        manager.set_storage(mock_storage)
        return manager

    def test_init(self, warmup_manager):
        """Test WarmupManager initialization"""
        assert warmup_manager.warmup_hours == 2
        assert warmup_manager._start_time is None

    def test_is_complete_no_warmup(self):
        """Test is_complete when warmup is disabled"""
        manager = WarmupManager(warmup_hours=0)
        assert manager.is_complete() is True

    def test_is_complete_not_started(self, warmup_manager):
        """Test is_complete when warmup not started"""
        assert warmup_manager.is_complete() is False

    def test_is_complete_in_progress(self, warmup_manager):
        """Test is_complete during warmup period"""
        warmup_manager._start_time = datetime.now() - timedelta(hours=1)
        assert warmup_manager.is_complete() is False

    def test_is_complete_finished(self, warmup_manager):
        """Test is_complete after warmup period"""
        warmup_manager._start_time = datetime.now() - timedelta(hours=3)
        assert warmup_manager.is_complete() is True

    def test_get_remaining_not_started(self, warmup_manager):
        """Test get_remaining when not started"""
        remaining = warmup_manager.get_remaining()
        assert remaining == timedelta(hours=2)

    def test_get_remaining_in_progress(self, warmup_manager):
        """Test get_remaining during warmup"""
        warmup_manager._start_time = datetime.now() - timedelta(hours=1)
        remaining = warmup_manager.get_remaining()
        assert timedelta(minutes=59) <= remaining <= timedelta(hours=1, seconds=1)

    def test_get_remaining_finished(self, warmup_manager):
        """Test get_remaining after warmup"""
        warmup_manager._start_time = datetime.now() - timedelta(hours=3)
        remaining = warmup_manager.get_remaining()
        assert remaining == timedelta(0)

    def test_get_remaining_str_complete(self):
        """Test get_remaining_str when complete"""
        manager = WarmupManager(warmup_hours=0)
        assert manager.get_remaining_str() is None

    def test_get_remaining_str_in_progress(self, warmup_manager):
        """Test get_remaining_str during warmup"""
        warmup_manager._start_time = datetime.now() - timedelta(hours=1, minutes=30)
        result = warmup_manager.get_remaining_str()
        assert result in ("0h 29m", "0h 30m")

    @pytest.mark.asyncio
    async def test_save(self, warmup_manager, mock_storage):
        """Test saving warmup state to DB"""
        warmup_manager._start_time = datetime.now() - timedelta(minutes=30)
        await warmup_manager.save()

        mock_storage.set_bot_state.assert_called_once_with(
            WARMUP_KEY, warmup_manager._start_time.isoformat()
        )

    @pytest.mark.asyncio
    async def test_load_existing(self, warmup_manager, mock_storage):
        """Test loading existing warmup from DB"""
        saved_time = datetime.now() - timedelta(minutes=30)
        mock_storage.get_bot_state.return_value = saved_time.isoformat()

        await warmup_manager.load()

        assert warmup_manager._start_time is not None
        time_diff = abs((saved_time - warmup_manager._start_time).total_seconds())
        assert time_diff < 2

    @pytest.mark.asyncio
    async def test_load_expired(self, warmup_manager, mock_storage):
        """Test loading expired warmup from DB"""
        saved_time = datetime.now() - timedelta(hours=5)
        mock_storage.get_bot_state.return_value = saved_time.isoformat()

        await warmup_manager.load()

        assert warmup_manager._start_time is None
        mock_storage.delete_bot_state.assert_called_once_with(WARMUP_KEY)

    @pytest.mark.asyncio
    async def test_start_new(self, warmup_manager, mock_storage):
        """Test starting new warmup"""
        await warmup_manager.start()
        assert warmup_manager._start_time is not None
        mock_storage.set_bot_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_resume(self, warmup_manager, mock_storage):
        """Test resuming existing warmup"""
        original_time = datetime.now() - timedelta(minutes=30)
        mock_storage.get_bot_state.return_value = original_time.isoformat()

        await warmup_manager.start()

        time_diff = abs((original_time - warmup_manager._start_time).total_seconds())
        assert time_diff < 2
        # Should not save again since it loaded existing
        mock_storage.set_bot_state.assert_not_called()

    def test_warmup_hours_setter(self, warmup_manager):
        """Test setting warmup_hours"""
        warmup_manager.warmup_hours = 5
        assert warmup_manager.warmup_hours == 5
