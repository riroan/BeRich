"""Data retention: old ticks fold into daily rows, old equity thins to daily."""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from src.core.types import Market
from src.data import retention
from src.data.models import DailyCloseRSIModel, EquitySnapshot, PriceRSIModel
from src.data.storage import Storage

NOW = datetime(2026, 11, 5, 7, 30)


@pytest.fixture
def storage(tmp_path):
    s = Storage(f"sqlite+aiosqlite:///{tmp_path / 'r.db'}")
    asyncio.run(s.initialize())
    yield s
    asyncio.run(s.close())


def _run(coro):
    return asyncio.run(coro)


async def _seed_ticks(storage, days=40, per_day=5):
    """Ticks for two symbols, plus the daily row the tick path keeps."""
    async with storage.async_session() as session:
        for d in range(days, 0, -1):
            day = (NOW - timedelta(days=d)).date()
            for sym, base in (("AAA", 100), ("BBB", 50)):
                for i in range(per_day):
                    price = Decimal(base + d + (i * 3 % 7))
                    rsi = Decimal(30 + i)
                    session.add(PriceRSIModel(
                        symbol=sym, market=Market.NASDAQ, price=price, rsi=rsi,
                        timestamp=datetime.combine(day, time(10, i)),
                    ))
                await session.merge(DailyCloseRSIModel(
                    symbol=sym, day=day, close=price, rsi=rsi,
                ))
        await session.commit()


async def _count(storage, model, where=None):
    async with storage.async_session() as session:
        q = select(func.count()).select_from(model)
        if where is not None:
            q = q.where(where)
        return await session.scalar(q)


def test_old_ticks_fold_into_daily_rows_and_are_deleted(storage):
    cutoff = datetime.combine(NOW.date() - timedelta(days=30), time.min)

    async def run():
        await _seed_ticks(storage)
        deleted = await retention.prune_ticks(storage, 30, now=NOW)
        old = await _count(storage, PriceRSIModel, PriceRSIModel.timestamp < cutoff)
        recent = await _count(storage, PriceRSIModel)
        async with storage.async_session() as session:
            row = await session.get(
                DailyCloseRSIModel, ("AAA", (NOW - timedelta(days=35)).date()),
            )
        return deleted, old, recent, row

    deleted, old, recent, row = _run(run())
    assert deleted == 10 * 2 * 5  # days 31..40, two symbols, five ticks
    assert old == 0
    assert recent == 30 * 2 * 5
    # base 100 + 35 days + (i*3 % 7) for i in 0..4 → 0,3,6,2,5
    assert (row.open, row.high, row.low) == (Decimal(135), Decimal(141), Decimal(135))
    assert row.close == Decimal(140) and row.rsi == Decimal(34)


def test_daily_candles_are_unchanged_by_retention(storage):
    async def run():
        await _seed_ticks(storage)
        before = {s: await storage.get_daily_ohlc_rsi(s) for s in ("AAA", "BBB")}
        await retention.prune_ticks(storage, 30, now=NOW)
        after = {s: await storage.get_daily_ohlc_rsi(s) for s in ("AAA", "BBB")}
        return before, after

    before, after = _run(run())
    assert len(before["AAA"]) == 40
    assert after == before


def test_second_run_is_a_no_op(storage):
    async def run():
        await _seed_ticks(storage)
        await retention.prune_ticks(storage, 30, now=NOW)
        return await retention.prune_ticks(storage, 30, now=NOW)

    assert _run(run()) == 0


def test_old_equity_keeps_the_last_snapshot_of_each_day(storage):
    async def run():
        async with storage.async_session() as session:
            for d in range(100, 0, -1):
                day = (NOW - timedelta(days=d)).date()
                for h in (10, 14, 22):
                    v = Decimal(1000 + d * 10 + h)
                    session.add(EquitySnapshot(
                        timestamp=datetime.combine(day, time(h)),
                        total_krw=0, total_usd=v, cash_krw=0, cash_usd=0,
                        position_value_krw=0, position_value_usd=v,
                    ))
            await session.commit()
        deleted = await retention.downsample_equity(storage, 90, now=NOW)
        async with storage.async_session() as session:
            kept = (await session.execute(
                select(EquitySnapshot.timestamp).order_by(EquitySnapshot.timestamp)
            )).scalars().all()
        return deleted, kept

    deleted, kept = _run(run())
    assert deleted == 10 * 2  # days 91..100 lose two of three
    old = [t for t in kept if t.date() < NOW.date() - timedelta(days=90)]
    assert len(old) == 10 and all(t.hour == 22 for t in old)
    assert len(kept) == 10 + 90 * 3


def test_run_retention_honours_disabled_windows(storage):
    async def run():
        await _seed_ticks(storage)
        return await retention.run_retention(
            storage, tick_days=0, equity_full_days=0, now=NOW,
        )

    assert _run(run()) == {"ticks_deleted": 0, "equity_deleted": 0}


def test_next_run_is_the_next_0730():
    assert retention.next_run(datetime(2026, 11, 5, 7, 0)) == datetime(2026, 11, 5, 7, 30)
    assert retention.next_run(datetime(2026, 11, 5, 7, 30)) == datetime(2026, 11, 6, 7, 30)
    assert retention.next_run(datetime(2026, 11, 5, 23, 0)) == datetime(2026, 11, 6, 7, 30)


def test_migration_adds_ohlc_columns_to_existing_table(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'old.db'}"

    async def run():
        old = Storage(url)
        async with old.engine.begin() as conn:
            await conn.execute(text(
                "CREATE TABLE daily_close_rsi (symbol VARCHAR(20), day DATE, "
                "close NUMERIC NOT NULL, rsi NUMERIC, updated_at DATETIME, "
                "PRIMARY KEY (symbol, day))"
            ))
        await old.close()
        s = Storage(url)
        await s.initialize()
        async with s.engine.connect() as conn:
            cols = (await conn.execute(text("PRAGMA table_info(daily_close_rsi)"))).all()
        await s.close()
        return {c[1] for c in cols}

    assert {"open", "high", "low"} <= _run(run())

