"""Warmup period management for trading bot"""

from datetime import datetime, timedelta
from typing import Optional
import logging

from src.data.storage import Storage

logger = logging.getLogger(__name__)

WARMUP_KEY = "warmup_start_time"


class WarmupManager:
    """Manages warmup period before trading starts"""

    def __init__(self, warmup_hours: int, storage: Optional[Storage] = None):
        self._warmup_hours = warmup_hours
        self._storage = storage
        self._start_time: datetime | None = None

    @property
    def warmup_hours(self) -> int:
        return self._warmup_hours

    @warmup_hours.setter
    def warmup_hours(self, value: int) -> None:
        self._warmup_hours = value

    def set_storage(self, storage: Storage) -> None:
        """Set storage after initialization"""
        self._storage = storage

    def is_complete(self) -> bool:
        """Check if warmup period is complete"""
        if self._warmup_hours <= 0:
            return True
        if self._start_time is None:
            return False

        elapsed = datetime.now() - self._start_time
        return elapsed >= timedelta(hours=self._warmup_hours)

    def get_remaining(self) -> timedelta:
        """Get remaining warmup time"""
        if self._start_time is None:
            return timedelta(hours=self._warmup_hours)
        elapsed = datetime.now() - self._start_time
        remaining = timedelta(hours=self._warmup_hours) - elapsed
        return max(remaining, timedelta(0))

    def get_remaining_str(self) -> str | None:
        """Get remaining warmup time as string"""
        if self.is_complete():
            return None
        remaining = self.get_remaining()
        hours, remainder = divmod(int(remaining.total_seconds()), 3600)
        minutes = remainder // 60
        return f"{hours}h {minutes}m"

    async def save(self) -> None:
        """Save warmup start time to DB"""
        if self._start_time and self._warmup_hours > 0 and self._storage:
            await self._storage.set_bot_state(
                WARMUP_KEY, self._start_time.isoformat()
            )
            logger.info(f"Warmup start time saved: {self._start_time}")

    async def load(self) -> None:
        """Load warmup start time from DB if exists"""
        if not self._storage:
            logger.debug("No storage available for warmup load")
            return

        if self._warmup_hours <= 0:
            logger.debug("Warmup disabled (warmup_hours <= 0)")
            return

        try:
            saved = await self._storage.get_bot_state(WARMUP_KEY)
            if saved:
                saved_time = datetime.fromisoformat(saved)
                elapsed = datetime.now() - saved_time
                if elapsed < timedelta(hours=self._warmup_hours):
                    self._start_time = saved_time
                    remaining = timedelta(hours=self._warmup_hours) - elapsed
                    hours, remainder = divmod(int(remaining.total_seconds()), 3600)
                    minutes = remainder // 60
                    logger.info(
                        f"Warmup resumed from {saved_time.strftime('%Y-%m-%d %H:%M:%S')} "
                        f"({hours}h {minutes}m remaining)"
                    )
                else:
                    await self._storage.delete_bot_state(WARMUP_KEY)
                    logger.info("Previous warmup already completed")
            else:
                logger.debug("No warmup state found in DB")
        except Exception as e:
            logger.warning(f"Failed to load warmup start time: {e}")

    async def start(self) -> None:
        """Start warmup period (load existing or create new)"""
        await self.load()
        if self._start_time is None:
            self._start_time = datetime.now()
            await self.save()

    async def cleanup(self) -> None:
        """Clean up warmup state from DB when complete"""
        if self._storage and self.is_complete():
            await self._storage.delete_bot_state(WARMUP_KEY)
            logger.info("Warmup complete - auto trading enabled")

    def log_status(self) -> None:
        """Log warmup status"""
        if not self.is_complete():
            remaining = self.get_remaining()
            hours, remainder = divmod(int(remaining.total_seconds()), 3600)
            minutes = remainder // 60
            logger.info(f"[WARMUP] {hours}h {minutes}m remaining - data only")
