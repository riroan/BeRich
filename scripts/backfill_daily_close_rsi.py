"""Fold existing price_rsi ticks into the daily_close_rsi summary table.

The tick path fills daily_close_rsi from the day it ships, so this is the
one-time catch-up for everything already in price_rsi. Idempotent: it
replaces whatever rows it computes, so re-running is safe and picks up
days a previous run missed.

Today's row is written too, then kept current by the tick path.

Usage:
  python -m scripts.backfill_daily_close_rsi            # dry-run
  python -m scripts.backfill_daily_close_rsi --apply
"""

import argparse
import asyncio
import os

from dotenv import load_dotenv
from sqlalchemy import text

from src.data.storage import Storage

# One row per symbol-day: the last tick that carried an RSI. Mirrors the
# rule get_daily_ohlc_rsi used, so backfilled days match what the pages
# rendered before.
FOLD_SQL = """
    SELECT p.symbol, date(p.timestamp) AS day, p.price AS close, p.rsi AS rsi
    FROM price_rsi p
    JOIN (
        SELECT symbol, date(timestamp) AS day, MAX(timestamp) AS last_tick
        FROM price_rsi
        WHERE rsi IS NOT NULL
        GROUP BY symbol, date(timestamp)
    ) last
      ON last.symbol = p.symbol AND last.last_tick = p.timestamp
    WHERE p.rsi IS NOT NULL
"""


async def main(apply: bool) -> None:
    load_dotenv()
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is not set")

    storage = Storage(database_url)
    await storage.initialize()  # creates daily_close_rsi if it is missing
    try:
        async with storage.async_session() as session:
            rows = (await session.execute(text(FOLD_SQL))).mappings().all()
            print(f"folded {len(rows)} symbol-days from price_rsi")
            if rows:
                days = sorted({str(r['day']) for r in rows})
                print(f"  range: {days[0]} .. {days[-1]}")
                print(f"  symbols: {len({r['symbol'] for r in rows})}")

            if not apply:
                print("dry-run — pass --apply to write")
                return

            for row in rows:
                await session.execute(
                    text(
                        "REPLACE INTO daily_close_rsi "
                        "(symbol, day, close, rsi, updated_at) "
                        "VALUES (:symbol, :day, :close, :rsi, NOW())"
                    ),
                    dict(row),
                )
            await session.commit()
            print(f"wrote {len(rows)} rows into daily_close_rsi")
    finally:
        await storage.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write the rows")
    asyncio.run(main(parser.parse_args().apply))
