"""Scheduler for periodic strategy execution"""

import asyncio
from datetime import datetime, time, timedelta, date
from enum import Enum
from typing import Callable
from zoneinfo import ZoneInfo
import logging

logger = logging.getLogger(__name__)

# Lazily-built NYSE (XNYS) calendar — expensive to construct, so cache it.
_XNYS_CAL = None


def _get_xnys_calendar():
    """XNYS calendar, built once with a far-future end.

    get_calendar()'s default session range ends only ~1 year out, so the
    cached calendar would later raise DateOutOfBounds and the holiday gate
    would silently treat every day as a trading day. Build with end =
    today + 5 years instead — comfortably covers any run between restarts
    (and each restart re-rolls the window forward).
    """
    global _XNYS_CAL
    if _XNYS_CAL is None:
        import exchange_calendars as xcals
        import pandas as pd
        end = pd.Timestamp(datetime.now().date()) + pd.DateOffset(years=5)
        _XNYS_CAL = xcals.get_calendar("XNYS", end=end)
    return _XNYS_CAL


def is_us_market_holiday(d: date) -> bool:
    """True if ``d`` is NOT a US (NYSE/XNYS) trading day — a market holiday
    or weekend. Degrades to False (treat as a trading day) if
    exchange_calendars is unavailable, so a missing dep never silently
    halts trading.
    """
    try:
        import pandas as pd
        return not _get_xnys_calendar().is_session(pd.Timestamp(d))
    except Exception as e:  # pragma: no cover - defensive fallback
        logger.debug(f"holiday check unavailable ({e!r}); treating as trading day")
        return False


def is_us_early_close(d: date) -> bool:
    """True if ``d`` is a US (XNYS) early-close (단축장) session — regular
    trading ends 13:00 ET instead of 16:00. Degrades to False if the
    calendar is unavailable.
    """
    try:
        import pandas as pd
        return pd.Timestamp(d) in _get_xnys_calendar().early_closes
    except Exception:  # pragma: no cover - defensive fallback
        return False


class Session(Enum):
    """US trading session (24h coverage), classified in KST."""
    DAY_MARKET = "day_market"  # KIS 주간거래 (US overnight)
    PRE = "pre"                # pre-market
    REGULAR = "regular"        # regular session
    AFTER = "after"            # after-hours
    CLOSED = "closed"          # weekend / no session


def is_us_dst() -> bool:
    """Check if US is currently in Daylight Saving Time"""
    # US DST: 2nd Sunday of March to 1st Sunday of November
    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et)
    # Check if UTC offset is -4 (EDT) or -5 (EST)
    return now_et.utcoffset().total_seconds() == -4 * 3600


# US session boundaries as minutes-from-midnight KST. Verified against the
# KIS "해외주식 주문" API doc (2026-06). EST (winter) shifts boundaries
# +60 min EXCEPT the after-market close, which the doc fixes at 07:00 KST
# in BOTH seasons. KIS routes pre/regular/after through the regular order
# endpoint; 주간거래(daytime) is a separate endpoint. There is a CLOSED gap
# between after-market close (07:00) and 주간거래 open (~09:00/10:00) — the
# regular endpoint errors there and 주간거래 hasn't started, so it is NOT 24h.
#   [0, REG_AM_END)        REGULAR  (prev evening, carried over midnight)
#   [REG_AM_END, AFTER_END) AFTER
#   [AFTER_END, DAY_START)  CLOSED   (gap)
#   [DAY_START, DAY_END)    DAY_MARKET (주간거래)
#   [DAY_END, PRE_END)      PRE
#   [PRE_END, 1440)         REGULAR  (evening)
# KIS hours (KST), summer / winter:
#   PRE 17:00-22:30 / 18:00-23:30 · REGULAR 22:30-05:00 / 23:30-06:00
#   AFTER 05:00-07:00 / 06:00-07:00 · DAY_MARKET 09:00-17:00 / 10:00-18:00
_REG_AM_END_EDT = 5 * 60       # 05:00 (regular session close, summer)
_AFTER_END_KST = 7 * 60        # 07:00 (after-market close — FIXED both seasons)
_DAY_START_EDT = 9 * 60        # 09:00 (주간거래 open, summer)
_DAY_END_EDT = 17 * 60         # 17:00 (주간거래 close = PRE open, summer)
_PRE_END_EDT = 22 * 60 + 30    # 22:30 (PRE close = REGULAR evening open, summer)


def get_current_session(ts: datetime, dst: bool | None = None) -> Session:
    """Classify a KST datetime into its US trading session.

    Weekend boundaries (KST): Sunday fully closed; Saturday open only for
    the regular-tail + after-hours carryover (Friday US night); Monday
    closed before 주간거래 open (Sunday US daytime).
    ``dst`` defaults to the live DST state; pass explicitly for testing.

    NOTE: weekend cutoffs assume KIS's session calendar matches the derived
    KST boundaries — verify against the KIS session calendar.
    """
    if dst is None:
        dst = is_us_dst()
    shift = 0 if dst else 60

    reg_am_end = _REG_AM_END_EDT + shift
    after_end = _AFTER_END_KST            # fixed 07:00, no DST shift
    day_start = _DAY_START_EDT + shift
    day_end = _DAY_END_EDT + shift
    pre_end = _PRE_END_EDT + shift

    weekday = ts.weekday()  # Mon=0 .. Sun=6
    if weekday == 6:  # Sunday — closed all day
        return Session.CLOSED

    m = ts.hour * 60 + ts.minute

    # Holiday gate: map this KST instant to the US trading DATE it belongs to
    # and close if that date isn't an NYSE session. The early-morning
    # carryover (before after_end) belongs to the PREVIOUS US date; from
    # day_market open onward it's today's US date. The 07:00-day_start gap is
    # CLOSED regardless, so its date is irrelevant.
    if m < after_end:
        us_date = (ts - timedelta(days=1)).date()
    elif m < day_start:
        us_date = None
    else:
        us_date = ts.date()
    if us_date is not None and is_us_market_holiday(us_date):
        return Session.CLOSED

    # Early-morning carryover (REGULAR tail + AFTER) belongs to the
    # PREVIOUS US trading night (us_date computed above).
    if m < after_end:
        # Monday early morning = Sunday US night → still closed.
        if weekday == 0:
            return Session.CLOSED
        # Early close (단축장): NYSE closes 13:00 ET vs the normal 16:00, i.e.
        # exactly 3h early, so the regular session ends reg_am_end-180. The
        # freed 3h is CLOSED (regular done, KIS after-window not open yet);
        # the after-window itself is unchanged.
        reg_end = reg_am_end - 180 if is_us_early_close(us_date) else reg_am_end
        if m < reg_end:
            return Session.REGULAR
        if m < reg_am_end:
            return Session.CLOSED
        return Session.AFTER

    # Gap between after-market close and 주간거래 open: regular endpoint is
    # outside operating hours and 주간거래 hasn't started yet.
    if m < day_start:
        return Session.CLOSED

    # Saturday has no fresh 주간/정규 session (Friday US night already ended).
    if weekday == 5:
        return Session.CLOSED

    if m < day_end:
        return Session.DAY_MARKET
    if m < pre_end:
        return Session.PRE
    return Session.REGULAR


DAYTIME_TAG = "[DAYTIME]"


def daytime_tag(session: "Session | None" = None) -> str:
    """``'[DAYTIME]'`` during the 주간거래 session, else ``''``.

    Shared marker for tagging logs and Discord messages so 주간거래 activity
    is distinguishable from regular/pre/after. Pass a known session to avoid
    recomputing; defaults to the live session.
    """
    if session is None:
        session = get_current_session(datetime.now())
    return DAYTIME_TAG if session == Session.DAY_MARKET else ""


def get_us_session_windows_kst() -> list[tuple[time, time]]:
    """US session windows in KST (DST-aware), merged for introspection.

    Weekday sessions are contiguous, so this is effectively the full day
    split at the midnight wrap. Used for the us_only scheduler default and
    startup logging; gating is done by get_current_session().
    """
    shift = 0 if is_us_dst() else 60

    def _t(minutes: int) -> time:
        minutes %= 24 * 60
        return time(minutes // 60, minutes % 60)

    return [
        (time(0, 0), _t(_AFTER_END_KST - 1)),          # carryover REGULAR + AFTER
        (_t(_DAY_START_EDT + shift), time(23, 59)),    # DAY_MARKET..PRE..evening REGULAR
    ]


def get_us_market_hours_kst() -> list[tuple[time, time]]:
    """US REGULAR market hours in KST (legacy KRX+US default path).

    EST (winter): 23:30 - 06:00 KST (next day)
    EDT (summer): 22:30 - 05:00 KST (next day)
    """
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
        market_hours: list[tuple[time, time]] = None,  # List of (open, close) times
        us_only: bool = False,  # Only trade during US market hours
    ):
        self.interval = interval_seconds
        self._running = False
        self._task: asyncio.Task | None = None
        self._callbacks: list[Callable] = []
        self.us_only = us_only

        if market_hours is not None:
            self.market_hours = market_hours
        elif us_only:
            # Full US 24h session windows (DST-aware). Gating is driven by
            # get_current_session(); this is kept for introspection/logging.
            self.market_hours = get_us_session_windows_kst()
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
        # US 24h: a single source of truth handles all session + weekend
        # boundaries.
        if self.us_only:
            return get_current_session(datetime.now()) != Session.CLOSED

        now = datetime.now().time()
        weekday = datetime.now().weekday()

        # Weekend check (Saturday=5, Sunday=6)
        # Note: US market closes Saturday morning KST (Friday night US)
        # So we allow early Saturday morning (00:00-06:00)
        if weekday == 6:  # Sunday - all markets closed
            return False
        if weekday == 5 and now > time(6, 0):  # Saturday after 06:00
            return False
        # Monday early morning KST corresponds to Sunday ET — US market still closed.
        # The first real US session of the week lands on Monday night KST (22:30+).
        if weekday == 0 and now < time(6, 0):
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
                logger.info(
                    "Market hours: US (EDT KST) — DAY 09:00-17:00 / "
                    "PRE 17:00-22:30 / REGULAR 22:30-05:00 / "
                    "AFTER 05:00-07:00 (07:00-09:00 closed)"
                )
            else:
                logger.info(
                    "Market hours: US (EST KST) — DAY 10:00-18:00 / "
                    "PRE 18:00-23:30 / REGULAR 23:30-06:00 / "
                    "AFTER 06:00-07:00 (07:00-10:00 closed)"
                )
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
