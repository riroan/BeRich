"""Warmup period management for trading bot"""

from datetime import datetime, timedelta
from pathlib import Path
import logging

logger = logging.getLogger(__name__)


class WarmupManager:
    """Manages warmup period before trading starts"""

    def __init__(self, warmup_hours: int, data_dir: Path):
        self._warmup_hours = warmup_hours
        self._warmup_file = data_dir / "warmup_start.txt"
        self._start_time: datetime | None = None

    @property
    def warmup_hours(self) -> int:
        return self._warmup_hours

    @warmup_hours.setter
    def warmup_hours(self, value: int) -> None:
        self._warmup_hours = value

    def is_complete(self) -> bool:
        """Check if warmup period is complete"""
        if self._warmup_hours <= 0:
            return True
        if self._start_time is None:
            return False

        elapsed = datetime.now() - self._start_time
        complete = elapsed >= timedelta(hours=self._warmup_hours)

        if complete and self._warmup_file.exists():
            self._warmup_file.unlink()
            logger.info("Warmup complete - auto trading enabled")

        return complete

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

    def save(self) -> None:
        """Save warmup start time to file for persistence"""
        if self._start_time and self._warmup_hours > 0:
            self._warmup_file.write_text(self._start_time.isoformat())
            logger.info(f"Warmup start time saved: {self._start_time}")

    def load(self) -> None:
        """Load warmup start time from file if exists"""
        logger.debug(
            f"Warmup check: hours={self._warmup_hours}, "
            f"file={self._warmup_file}, exists={self._warmup_file.exists()}"
        )

        if self._warmup_hours > 0 and self._warmup_file.exists():
            try:
                saved_time = datetime.fromisoformat(
                    self._warmup_file.read_text().strip()
                )
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
                    self._warmup_file.unlink()
                    logger.info("Previous warmup already completed")
            except Exception as e:
                logger.warning(f"Failed to load warmup start time: {e}")
        elif self._warmup_hours <= 0:
            logger.debug("Warmup disabled (warmup_hours <= 0)")
        elif not self._warmup_file.exists():
            logger.debug(f"Warmup file not found: {self._warmup_file}")

    def start(self) -> None:
        """Start warmup period (load existing or create new)"""
        self.load()
        if self._start_time is None:
            self._start_time = datetime.now()
            self.save()

    def log_status(self) -> None:
        """Log warmup status"""
        if not self.is_complete():
            remaining = self.get_remaining()
            hours, remainder = divmod(int(remaining.total_seconds()), 3600)
            minutes = remainder // 60
            logger.info(f"[WARMUP] {hours}h {minutes}m remaining - data only")
