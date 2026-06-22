"""Tests for US 24h session classification (Phase 2)."""

from datetime import datetime, date

import pytest

from src.utils.scheduler import (
    Session, get_current_session, is_us_market_holiday, is_us_early_close,
    daytime_tag, DAYTIME_TAG,
)


# Anchor dates — a clean (no-holiday) week so session logic is tested
# independently of the holiday gate. 2026-06-22 Mon ... 2026-06-28 Sun.
# (2026-06-19 Fri is Juneteenth, so the prior week is avoided here.)
WED = (2026, 6, 24)   # Wednesday, trading day
SAT = (2026, 6, 27)   # Saturday; preceding Fri 06-26 is a normal trading day
SUN = (2026, 6, 28)   # Sunday
MON = (2026, 6, 22)   # Monday, trading day


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


class TestHolidayGate:
    """US market holidays / early closes via XNYS calendar close the bot."""

    def test_juneteenth_closed(self):
        pytest.importorskip("exchange_calendars")
        # 2026-06-19 Fri = Juneteenth (NYSE closed). Every KST instant that
        # maps to that US trading date must be CLOSED.
        FRI = (2026, 6, 19)
        assert _at(FRI, 11, 0) == Session.CLOSED   # day_market window
        assert _at(FRI, 18, 0) == Session.CLOSED   # pre window
        assert _at(FRI, 23, 0) == Session.CLOSED   # regular window
        # next KST morning still maps to Fri 06-19 (carryover) → CLOSED
        assert _at((2026, 6, 20), 3, 0) == Session.CLOSED   # regular tail
        assert _at((2026, 6, 20), 6, 0) == Session.CLOSED   # after tail

    def test_normal_trading_day_open(self):
        pytest.importorskip("exchange_calendars")
        # 2026-06-24 Wed (trading) — holiday gate must NOT close it.
        assert _at((2026, 6, 24), 11, 0) == Session.DAY_MARKET
        assert _at((2026, 6, 24), 23, 0) == Session.REGULAR

    def test_is_us_market_holiday(self):
        pytest.importorskip("exchange_calendars")
        assert is_us_market_holiday(date(2026, 6, 19)) is True    # Juneteenth
        assert is_us_market_holiday(date(2026, 6, 18)) is False   # Thu trading
        assert is_us_market_holiday(date(2026, 12, 25)) is True   # Christmas
        assert is_us_market_holiday(date(2026, 6, 21)) is True    # Sunday

    def test_calendar_range_extends_years_ahead(self):
        pytest.importorskip("exchange_calendars")
        # Regression (#1): default XNYS range ended only ~1yr out, so the
        # cached calendar later raised DateOutOfBounds and the holiday gate
        # silently treated every day as a trading day. Built far-end now.
        import pandas as pd
        from src.utils.scheduler import _get_xnys_calendar
        last = _get_xnys_calendar().last_session
        assert last >= pd.Timestamp(date.today()) + pd.DateOffset(years=4)


class TestEarlyClose:
    """단축장: NYSE early close (13:00 ET) shortens the regular session by 3h;
    the after-window is unchanged. 2026-11-27 (Fri, EST) is an early close."""

    def test_is_us_early_close(self):
        pytest.importorskip("exchange_calendars")
        assert is_us_early_close(date(2026, 11, 27)) is True   # day after Thxgiving
        assert is_us_early_close(date(2026, 12, 24)) is True   # Christmas Eve
        assert is_us_early_close(date(2026, 11, 26)) is False  # Thxgiving (full holiday)
        assert is_us_early_close(date(2026, 6, 24)) is False   # normal day

    def test_early_close_shortens_regular(self):
        pytest.importorskip("exchange_calendars")
        # KST Sat 11-28 early morning = carryover of US Fri 11-27 (early close).
        # EST (winter), so dst=False. reg normally ends 06:00 KST; early → 03:00.
        SAT = (2026, 11, 28)
        assert _at(SAT, 1, 0, dst=False) == Session.REGULAR   # before early close
        assert _at(SAT, 3, 0, dst=False) == Session.CLOSED    # early close hit
        assert _at(SAT, 5, 0, dst=False) == Session.CLOSED    # was regular, now closed
        assert _at(SAT, 6, 30, dst=False) == Session.AFTER    # after-window kept

    def test_normal_day_keeps_full_regular_and_after(self):
        pytest.importorskip("exchange_calendars")
        # Regression: a normal day's carryover keeps regular→06:00 then AFTER.
        # KST Sat 06-27 = carryover of Fri 06-26 (normal). dst=True (summer).
        assert _at((2026, 6, 27), 4, 0, dst=True) == Session.REGULAR  # still regular
        assert _at((2026, 6, 27), 6, 0, dst=True) == Session.AFTER


class TestDaytimeTag:
    """주간거래(DAY_MARKET)에만 마커가 붙는다 — 로그/디스코드 공용."""

    def test_marks_only_day_market(self):
        assert daytime_tag(Session.DAY_MARKET) == DAYTIME_TAG
        for s in (Session.REGULAR, Session.PRE, Session.AFTER, Session.CLOSED):
            assert daytime_tag(s) == ""
