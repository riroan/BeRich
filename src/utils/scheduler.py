"""Scheduler for periodic strategy execution"""

import asyncio
from datetime import datetime, time
from typing import Callable, Optional, List, Tuple
import logging

logger = logging.getLogger(__name__)


class TradingScheduler:
    """Scheduler for periodic trading tasks with multi-market support"""

    def __init__(
        self,
        interval_seconds: int = 60,  # Default: 1 minute
        market_hours: List[Tuple[time, time]] = None,  # List of (open, close) times
    ):
        self.interval = interval_seconds
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._callbacks: list[Callable] = []

        # Default: KRX (09:00-15:30) + US (23:30-06:00 next day)
        if market_hours is None:
            self.market_hours = [
                (time(9, 0), time(15, 30)),    # KRX: 09:00 - 15:30
                (time(23, 30), time(23, 59)),  # US: 23:30 - 23:59
                (time(0, 0), time(6, 0)),      # US: 00:00 - 06:00
            ]
        else:
            self.market_hours = market_hours

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

    def get_active_market(self) -> str:
        """Get which market is currently active"""
        now = datetime.now().time()

        if time(9, 0) <= now <= time(15, 30):
            return "KRX"
        elif time(23, 30) <= now or now <= time(6, 0):
            return "US"
        return "CLOSED"

    async def start(self) -> None:
        """Start the scheduler"""
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(f"Scheduler started (interval: {self.interval}s)")
        logger.info("Market hours: KRX 09:00-15:30, US 23:30-06:00")

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
                    logger.debug(f"Markets closed, skipping...")

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
