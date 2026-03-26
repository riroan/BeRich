"""Tests for WarmupManager"""

import pytest
from datetime import datetime, timedelta

from src.bot.warmup import WarmupManager


class TestWarmupManager:
    """Test cases for WarmupManager"""

    @pytest.fixture
    def temp_dir(self, tmp_path):
        """Create a temporary directory for warmup files"""
        return tmp_path

    @pytest.fixture
    def warmup_manager(self, temp_dir):
        """Create a WarmupManager instance"""
        return WarmupManager(warmup_hours=2, data_dir=temp_dir)

    def test_init(self, warmup_manager):
        """Test WarmupManager initialization"""
        assert warmup_manager.warmup_hours == 2
        assert warmup_manager._start_time is None

    def test_is_complete_no_warmup(self, temp_dir):
        """Test is_complete when warmup is disabled"""
        manager = WarmupManager(warmup_hours=0, data_dir=temp_dir)
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
        # Should be approximately 1 hour remaining
        assert timedelta(minutes=59) <= remaining <= timedelta(hours=1, seconds=1)

    def test_get_remaining_finished(self, warmup_manager):
        """Test get_remaining after warmup"""
        warmup_manager._start_time = datetime.now() - timedelta(hours=3)
        remaining = warmup_manager.get_remaining()
        assert remaining == timedelta(0)

    def test_get_remaining_str_complete(self, temp_dir):
        """Test get_remaining_str when complete"""
        manager = WarmupManager(warmup_hours=0, data_dir=temp_dir)
        assert manager.get_remaining_str() is None

    def test_get_remaining_str_in_progress(self, warmup_manager):
        """Test get_remaining_str during warmup"""
        warmup_manager._start_time = datetime.now() - timedelta(hours=1, minutes=30)
        result = warmup_manager.get_remaining_str()
        # Allow for timing variations (29-30 minutes)
        assert result in ("0h 29m", "0h 30m")

    def test_save_and_load(self, warmup_manager):
        """Test saving and loading warmup state"""
        warmup_manager._start_time = datetime.now() - timedelta(minutes=30)
        warmup_manager.save()

        # Create new manager and load
        new_manager = WarmupManager(
            warmup_hours=2,
            data_dir=warmup_manager._warmup_file.parent,
        )
        new_manager.load()

        assert new_manager._start_time is not None
        # Should be approximately the same time
        time_diff = abs(
            (warmup_manager._start_time - new_manager._start_time).total_seconds()
        )
        assert time_diff < 2

    def test_load_expired_warmup(self, warmup_manager):
        """Test loading expired warmup file"""
        # Save an old warmup start time
        warmup_manager._start_time = datetime.now() - timedelta(hours=5)
        warmup_manager.save()

        # Create new manager and load
        new_manager = WarmupManager(
            warmup_hours=2,
            data_dir=warmup_manager._warmup_file.parent,
        )
        new_manager.load()

        # Should not load expired warmup
        assert new_manager._start_time is None
        # File should be deleted
        assert not warmup_manager._warmup_file.exists()

    def test_start_new(self, warmup_manager):
        """Test starting new warmup"""
        warmup_manager.start()
        assert warmup_manager._start_time is not None
        assert warmup_manager._warmup_file.exists()

    def test_start_resume(self, warmup_manager):
        """Test resuming existing warmup"""
        # Save existing warmup
        original_time = datetime.now() - timedelta(minutes=30)
        warmup_manager._start_time = original_time
        warmup_manager.save()

        # Create new manager and start
        new_manager = WarmupManager(
            warmup_hours=2,
            data_dir=warmup_manager._warmup_file.parent,
        )
        new_manager.start()

        # Should resume from saved time
        time_diff = abs((original_time - new_manager._start_time).total_seconds())
        assert time_diff < 2

    def test_warmup_hours_setter(self, warmup_manager):
        """Test setting warmup_hours"""
        warmup_manager.warmup_hours = 5
        assert warmup_manager.warmup_hours == 5
