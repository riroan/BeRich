"""Tests for US 24h session classification (Phase 2)."""

from datetime import datetime

from src.utils.scheduler import Session, get_current_session


# Anchor dates (verified weekdays):
#   2026-06-17 Wed, 2026-06-20 Sat, 2026-06-21 Sun, 2026-06-22 Mon
WED = (2026, 6, 17)
SAT = (2026, 6, 20)
SUN = (2026, 6, 21)
MON = (2026, 6, 22)


def _at(date_tuple, h, m, dst=True):
    return get_current_session(datetime(*date_tuple, h, m), dst=dst)


class TestSessionsEDT:
    """Summer (EDT) KST: DAY 09-17, PRE 17-22:30, REGULAR 22:30-05,
    AFTER 05-07, then CLOSED gap 07-09 (KIS regular endpoint hours)."""

    def test_weekday_full_cycle(self):
        assert _at(WED, 0, 0) == Session.REGULAR      # midnight carryover
        assert _at(WED, 4, 59) == Session.REGULAR
        assert _at(WED, 5, 0) == Session.AFTER
        assert _at(WED, 6, 59) == Session.AFTER
        assert _at(WED, 7, 0) == Session.CLOSED       # after-market closed
        assert _at(WED, 8, 59) == Session.CLOSED      # gap before 주간거래
        assert _at(WED, 9, 0) == Session.DAY_MARKET
        assert _at(WED, 16, 59) == Session.DAY_MARKET
        assert _at(WED, 17, 0) == Session.PRE
        assert _at(WED, 22, 29) == Session.PRE
        assert _at(WED, 22, 30) == Session.REGULAR
        assert _at(WED, 23, 59) == Session.REGULAR


class TestSessionsEST:
    """Winter (EST) shifts +1h, EXCEPT after-market close fixed at 07:00."""

    def test_weekday_boundaries(self):
        assert _at(WED, 5, 0, dst=False) == Session.REGULAR   # still carryover
        assert _at(WED, 6, 0, dst=False) == Session.AFTER
        assert _at(WED, 6, 59, dst=False) == Session.AFTER
        assert _at(WED, 7, 0, dst=False) == Session.CLOSED    # after close 07:00
        assert _at(WED, 9, 59, dst=False) == Session.CLOSED   # gap before 주간거래
        assert _at(WED, 10, 0, dst=False) == Session.DAY_MARKET
        assert _at(WED, 18, 0, dst=False) == Session.PRE
        assert _at(WED, 23, 30, dst=False) == Session.REGULAR


class TestWeekendBoundaries:
    """Sunday closed; Saturday only the Friday-night carryover; Monday
    closed until DAY_MARKET open."""

    def test_sunday_closed_all_day(self):
        for h in (0, 5, 9, 12, 17, 22, 23):
            assert _at(SUN, h, 0) == Session.CLOSED

    def test_saturday_morning_carryover_then_closed(self):
        assert _at(SAT, 0, 0) == Session.REGULAR   # Fri regular tail
        assert _at(SAT, 4, 59) == Session.REGULAR
        assert _at(SAT, 5, 0) == Session.AFTER     # Fri after-hours
        assert _at(SAT, 6, 59) == Session.AFTER
        assert _at(SAT, 7, 0) == Session.CLOSED    # after-market closed
        assert _at(SAT, 9, 0) == Session.CLOSED    # no fresh Sat session
        assert _at(SAT, 12, 0) == Session.CLOSED

    def test_monday_closed_until_day_market(self):
        assert _at(MON, 0, 0) == Session.CLOSED    # Sun US night
        assert _at(MON, 8, 59) == Session.CLOSED
        assert _at(MON, 9, 0) == Session.DAY_MARKET
        assert _at(MON, 22, 30) == Session.REGULAR
