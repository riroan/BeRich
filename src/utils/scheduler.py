"""Scheduler for periodic strategy execution"""

import asyncio
from datetime import datetime, time
from typing import Callable, Optional, List, Tuple
from zoneinfo import ZoneInfo
import logging

logger = logging.getLogger(__name__)


def is_us_dst() -> bool:
    """Check if US is currently in Daylight Saving Time"""
    # US DST: 2nd Sunday of March to 1st Sunday of November
    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et)
    # Check if UTC offset is -4 (EDT) or -5 (EST)
    return now_et.utcoffset().total_seconds() == -4 * 3600


def get_us_market_hours_kst() -> List[Tuple[time, time]]:
    """Get US market hours in KST, accounting for DST"""
    # US Regular Market: 9:30 AM - 4:00 PM ET
    # EST (winter): 23:30 - 06:00 KST (next day)
    # EDT (summer): 22:30 - 05:00 KST (next day)
    if is_us_dst():
        return [
            (time(22, 30), time(23, 59)),  # EDT: 22:30 - 23:59
            (time(0, 0), time(5, 0)),      # EDT: 00:00 - 05:00
        ]
    else:
        return [
            (time(23, 30), time(23, 59)),  # EST: 23:30 - 23:59
            (time(0, 0), time(6, 0)),      # EST: 00:00 - 06:00
        ]


class TradingScheduler:
    """Scheduler for periodic trading tasks with multi-market support"""

    def __init__(
        self,
        interval_seconds: int = 60,  # Default: 1 minute
        market_hours: List[Tuple[time, time]] = None,  # List of (open, close) times
        us_only: bool = False,  # Only trade during US market hours
    ):
        self.interval = interval_seconds
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._callbacks: list[Callable] = []
        self.us_only = us_only

        if market_hours is not None:
            self.market_hours = market_hours
        elif us_only:
            # US market hours only (DST-aware)
            self.market_hours = get_us_market_hours_kst()
        else:
            # Default: KRX + US
            self.market_hours = [
                (time(9, 0), time(15, 30)),    # KRX: 09:00 - 15:30
            ] + get_us_market_hours_kst()

    def add_callback(self, callback: Callable) -> None:
        """Add a callback to be executed on each tick"""
        self._callbacks.append(callback)

    def is_market_open(self) -> bool:
        """Check if any market is currently open"""
        now = datetime.now().time()
        weekday = datetime.now().weekday()

        # Weekend check (Saturday=5, Sunday=6)
        # Note: US market closes Saturday morning KST (Friday night US)
        # So we allow early Saturday morning (00:00-06:00)
        if weekday == 6:  # Sunday - all markets closed
            return False
        if weekday == 5 and now > time(6, 0):  # Saturday after 06:00
            return False

        # Check if current time falls within any market hours
        for market_open, market_close in self.market_hours:
            if market_open <= now <= market_close:
                return True

        return False

    async def start(self) -> None:
        """Start the scheduler"""
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(f"Scheduler started (interval: {self.interval}s)")
        if self.us_only:
            if is_us_dst():
                logger.info("Market hours: US only (EDT: 22:30-05:00 KST)")
            else:
                logger.info("Market hours: US only (EST: 23:30-06:00 KST)")
        else:
            logger.info("Market hours: KRX 09:00-15:30, US 23:30-06:00 KST")

    async def stop(self) -> None:
        """Stop the scheduler"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Scheduler stopped")

    async def _run_loop(self) -> None:
        """Main scheduler loop"""
        while self._running:
            try:
                if self.is_market_open():
                    await self._execute_callbacks()
                else:
                    logger.debug("Markets closed, skipping...")

                await asyncio.sleep(self.interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
                await asyncio.sleep(self.interval)

    async def _execute_callbacks(self) -> None:
        """Execute all registered callbacks"""
        for callback in self._callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback()
                else:
                    callback()
            except Exception as e:
                logger.error(f"Callback error: {e}")
