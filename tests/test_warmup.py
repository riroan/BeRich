"""Tests for WarmupManager"""

import pytest
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

    @pytest.mark.asyncio
    async def test_is_complete_no_warmup(self, mock_storage):
        """Test is_complete when warmup is disabled"""
        manager = WarmupManager(warmup_hours=0)
        manager.set_storage(mock_storage)
        assert await manager.is_complete() is True

    @pytest.mark.asyncio
    async def test_is_complete_not_started(self, warmup_manager, mock_storage):
        """Test is_complete when warmup not started"""
        mock_storage.get_bot_state.return_value = None
        assert await warmup_manager.is_complete() is False

    @pytest.mark.asyncio
    async def test_is_complete_in_progress(self, warmup_manager, mock_storage):
        """Test is_complete during warmup period"""
        start_time = datetime.now() - timedelta(hours=1)
        mock_storage.get_bot_state.return_value = start_time.isoformat()
        assert await warmup_manager.is_complete() is False

    @pytest.mark.asyncio
    async def test_is_complete_finished(self, warmup_manager, mock_storage):
        """Test is_complete after warmup period"""
        start_time = datetime.now() - timedelta(hours=3)
        mock_storage.get_bot_state.return_value = start_time.isoformat()
        assert await warmup_manager.is_complete() is True
        mock_storage.delete_bot_state.assert_called_once_with(WARMUP_KEY)

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

    @pytest.mark.asyncio
    async def test_get_remaining_str_complete(self, mock_storage):
        """Test get_remaining_str when complete"""
        manager = WarmupManager(warmup_hours=0)
        manager.set_storage(mock_storage)
        assert await manager.get_remaining_str() is None

    @pytest.mark.asyncio
    async def test_get_remaining_str_in_progress(
        self, warmup_manager, mock_storage,
    ):
        """Test get_remaining_str during warmup"""
        start_time = datetime.now() - timedelta(hours=1, minutes=30)
        mock_storage.get_bot_state.return_value = start_time.isoformat()
        result = await warmup_manager.get_remaining_str()
        assert result in ("0h 29m", "0h 30m")

    @pytest.mark.asyncio
    async def test_start_new(self, warmup_manager, mock_storage):
        """Test starting new warmup (no existing state in DB)"""
        mock_storage.get_bot_state.return_value = None
        await warmup_manager.start()
        assert warmup_manager._start_time is not None
        mock_storage.set_bot_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_existing(self, warmup_manager, mock_storage):
        """Test starting with existing state in DB (should not overwrite)"""
        original_time = datetime.now() - timedelta(minutes=30)
        mock_storage.get_bot_state.return_value = original_time.isoformat()

        await warmup_manager.start()

        time_diff = abs(
            (original_time - warmup_manager._start_time).total_seconds()
        )
        assert time_diff < 2
        mock_storage.set_bot_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_is_complete_syncs_from_db(
        self, warmup_manager, mock_storage,
    ):
        """Test that is_complete reads from DB every time"""
        mock_storage.get_bot_state.return_value = None
        assert await warmup_manager.is_complete() is False

        # Simulate external DB update
        start_time = datetime.now() - timedelta(hours=3)
        mock_storage.get_bot_state.return_value = start_time.isoformat()
        assert await warmup_manager.is_complete() is True

    def test_warmup_hours_setter(self, warmup_manager):
        """Test setting warmup_hours"""
        warmup_manager.warmup_hours = 5
        assert warmup_manager.warmup_hours == 5
