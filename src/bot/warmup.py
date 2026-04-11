"""Warmup period management for trading bot"""

from datetime import datetime, timedelta
import logging

from src.data.storage import Storage

logger = logging.getLogger(__name__)

WARMUP_KEY = "warmup_start_time"


class WarmupManager:
    """Manages warmup period before trading starts"""

    def __init__(self, warmup_hours: int, storage: Storage | None = None):
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

    async def _sync_from_db(self) -> None:
        """Sync warmup start time from DB"""
        if not self._storage:
            return
        try:
            saved = await self._storage.get_bot_state(WARMUP_KEY)
            if saved:
                self._start_time = datetime.fromisoformat(saved)
            else:
                self._start_time = None
        except Exception as e:
            logger.warning(f"Failed to sync warmup from DB: {e}")

    async def is_complete(self) -> bool:
        """Check if warmup period is complete (reads from DB every call)"""
        if self._warmup_hours <= 0:
            return True

        await self._sync_from_db()

        if self._start_time is None:
            return False

        elapsed = datetime.now() - self._start_time
        complete = elapsed >= timedelta(hours=self._warmup_hours)

        if complete:
            await self._storage.delete_bot_state(WARMUP_KEY)

        return complete

    def get_remaining(self) -> timedelta:
        """Get remaining warmup time"""
        if self._start_time is None:
            return timedelta(hours=self._warmup_hours)
        elapsed = datetime.now() - self._start_time
        remaining = timedelta(hours=self._warmup_hours) - elapsed
        return max(remaining, timedelta(0))

    async def get_remaining_str(self) -> str | None:
        """Get remaining warmup time as string"""
        if await self.is_complete():
            return None
        remaining = self.get_remaining()
        hours, remainder = divmod(int(remaining.total_seconds()), 3600)
        minutes = remainder // 60
        return f"{hours}h {minutes}m"

    async def start(self) -> None:
        """Start warmup period (insert only if not exists in DB)"""
        if self._warmup_hours <= 0:
            logger.debug("Warmup disabled (warmup_hours <= 0)")
            return

        if not self._storage:
            logger.debug("No storage available for warmup")
            return

        await self._sync_from_db()

        if self._start_time is not None:
            remaining = self.get_remaining()
            hours, remainder = divmod(int(remaining.total_seconds()), 3600)
            minutes = remainder // 60
            logger.info(
                f"Warmup resumed from {self._start_time:%Y-%m-%d %H:%M:%S} "
                f"({hours}h {minutes}m remaining)"
            )
        else:
            self._start_time = datetime.now()
            await self._storage.set_bot_state(
                WARMUP_KEY, self._start_time.isoformat()
            )
            logger.info(f"Warmup started: {self._start_time}")

    async def log_status(self) -> None:
        """Log warmup status"""
        if not await self.is_complete():
            remaining = self.get_remaining()
            hours, remainder = divmod(int(remaining.total_seconds()), 3600)
            minutes = remainder // 60
            logger.info(f"[WARMUP] {hours}h {minutes}m remaining - data only")
