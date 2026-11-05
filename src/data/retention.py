"""Data retention: keep the growing tables from growing forever.

- price_rsi (~1,000 ticks per symbol per day): ticks older than
  ``tick_days`` are folded into daily_close_rsi as one OHLC + RSI row per
  symbol-day, then deleted. The daily candle chart reads those rows back
  (Storage.get_daily_ohlc_rsi), so nothing older disappears from it.
- equity_snapshots (one every few minutes): days older than
  ``equity_full_days`` keep only their last snapshot — the day's close,
  which is what the daily equity curve and monthly returns use.

Deletes go in batches of BATCH ids, one commit per batch, so a first run
over months of backlog never holds a long lock on MySQL.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import delete, func, select

from .models import DailyCloseRSIModel, EquitySnapshot, PriceRSIModel

logger = logging.getLogger(__name__)

TICK_DAYS = 30
EQUITY_FULL_DAYS = 90
BATCH = 5000
# Daily run time, local (KST): inside the 07:00-09:00 gap between the US
# after-hours and day sessions, when the bot places no orders.
RUN_AT = time(7, 30)


def next_run(now: datetime, at: time = RUN_AT) -> datetime:
    """The next ``at`` strictly after ``now``."""
    run = datetime.combine(now.date(), at)
    return run if run > now else run + timedelta(days=1)


def _day_start(d: date) -> datetime:
    return datetime.combine(d, time.min)


async def _delete_ids(storage: Any, model: Any, ids: list[int]) -> None:
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        async with storage.async_session() as session:
            await session.execute(delete(model).where(model.id.in_(chunk)))
            await session.commit()


async def _fold_tick_day(storage: Any, day: date) -> int:
    """Fold one calendar day of ticks into daily_close_rsi, then delete them."""
    start, end = _day_start(day), _day_start(day + timedelta(days=1))
    async with storage.async_session() as session:
        result = await session.execute(
            select(
                PriceRSIModel.id, PriceRSIModel.symbol,
                PriceRSIModel.price, PriceRSIModel.rsi,
            )
            .where(PriceRSIModel.timestamp >= start, PriceRSIModel.timestamp < end)
            .order_by(PriceRSIModel.symbol, PriceRSIModel.timestamp, PriceRSIModel.id)
        )
        ticks = result.all()

    ids: list[int] = []
    folds: dict[str, dict] = {}
    for tick_id, symbol, price, rsi in ticks:
        ids.append(tick_id)
        f = folds.get(symbol)
        if f is None:
            folds[symbol] = f = {
                "open": price, "high": price, "low": price,
                "close": price, "rsi": None,
            }
        f["high"] = max(f["high"], price)
        f["low"] = min(f["low"], price)
        # Same rule as the tick path: the day closes on its last tick that
        # carries an RSI, so close and rsi stay aligned.
        if rsi is not None:
            f["close"], f["rsi"] = price, rsi
        elif f["rsi"] is None:
            f["close"] = price

    async with storage.async_session() as session:
        for symbol, f in folds.items():
            row = await session.get(DailyCloseRSIModel, (symbol, day))
            if row is None:
                session.add(DailyCloseRSIModel(
                    symbol=symbol, day=day, close=f["close"], rsi=f["rsi"],
                    open=f["open"], high=f["high"], low=f["low"],
                ))
            else:
                # Keep the tick path's close/rsi; only add the candle body
                row.open, row.high, row.low = f["open"], f["high"], f["low"]
        await session.commit()

    await _delete_ids(storage, PriceRSIModel, ids)
    return len(ids)


async def prune_ticks(
    storage: Any, keep_days: int = TICK_DAYS, now: datetime | None = None,
) -> int:
    """Fold and delete price_rsi ticks older than ``keep_days`` days."""
    cutoff = _day_start((now or datetime.now()).date() - timedelta(days=keep_days))
    async with storage.async_session() as session:
        oldest = await session.scalar(
            select(func.min(PriceRSIModel.timestamp))
            .where(PriceRSIModel.timestamp < cutoff)
        )
    if oldest is None:
        return 0

    deleted = 0
    day = oldest.date()
    while _day_start(day) < cutoff:
        deleted += await _fold_tick_day(storage, day)
        day += timedelta(days=1)
    return deleted


async def downsample_equity(
    storage: Any, full_days: int = EQUITY_FULL_DAYS, now: datetime | None = None,
) -> int:
    """Keep only each day's last equity snapshot before ``full_days`` days ago."""
    cutoff = _day_start((now or datetime.now()).date() - timedelta(days=full_days))
    async with storage.async_session() as session:
        result = await session.execute(
            select(EquitySnapshot.id, EquitySnapshot.timestamp)
            .where(EquitySnapshot.timestamp < cutoff)
            .order_by(EquitySnapshot.timestamp, EquitySnapshot.id)
        )
        rows = result.all()

    last_of_day: dict[date, int] = {}
    for snap_id, ts in rows:
        last_of_day[ts.date()] = snap_id
    keep = set(last_of_day.values())
    drop = [snap_id for snap_id, _ in rows if snap_id not in keep]
    await _delete_ids(storage, EquitySnapshot, drop)
    return len(drop)


async def run_retention(
    storage: Any,
    tick_days: int = TICK_DAYS,
    equity_full_days: int = EQUITY_FULL_DAYS,
    now: datetime | None = None,
) -> dict[str, int]:
    """One cleanup pass. A non-positive window turns that part off."""
    result = {"ticks_deleted": 0, "equity_deleted": 0}
    if tick_days > 0:
        result["ticks_deleted"] = await prune_ticks(storage, tick_days, now)
    if equity_full_days > 0:
        result["equity_deleted"] = await downsample_equity(
            storage, equity_full_days, now,
        )
    return result
