"""Back-adjust price_rsi history for a stock split / merge.

A split (e.g. 20:1) drops the live price to 1/20 while the historical
price_rsi rows keep the pre-split scale, leaving a cliff that corrupts RSI.
This rescales the pre-split rows onto the post-split scale so the series is
continuous again.

Dry-run by default. The split point is detected automatically (the largest
single-step price drop) unless --split-before is given; the ratio is inferred
from that step unless --ratio is given. Affected rows are backed up to CSV
before any write.

Usage:
  python -m scripts.adjust_split --symbol KORU                 # dry-run, auto-detect
  python -m scripts.adjust_split --symbol KORU --apply         # write (after backup)
  python -m scripts.adjust_split --symbol KORU --ratio 20 \\
      --split-before 2026-07-14T00:00 --apply
"""

import argparse
import asyncio
import csv
import os
from datetime import datetime
from decimal import Decimal

from dotenv import load_dotenv
from sqlalchemy import text

from src.data.storage import Storage


async def _fetch_rows(storage: Storage, symbol: str) -> list[dict]:
    async with storage.async_session() as session:
        result = await session.execute(
            text(
                "SELECT id, price, rsi, timestamp FROM price_rsi "
                "WHERE symbol = :sym ORDER BY timestamp"
            ),
            {"sym": symbol},
        )
        return [
            {"id": r[0], "price": Decimal(str(r[1])), "rsi": r[2], "ts": r[3]}
            for r in result
        ]


def _detect_split(rows: list[dict]) -> tuple[datetime | None, float | None]:
    """Return (timestamp of the post-split row, inferred ratio) for the
    largest single-step drop, or (None, None) if nothing looks like a split.
    """
    worst_ratio = 1.0
    at_ts = None
    for prev, cur in zip(rows, rows[1:]):
        if prev["price"] <= 0:
            continue
        ratio = float(cur["price"] / prev["price"])
        if ratio < worst_ratio:
            worst_ratio = ratio
            at_ts = cur["ts"]
    # A real split shows up as a >40% drop; ignore ordinary moves.
    if at_ts is None or worst_ratio > 0.6:
        return None, None
    return at_ts, round(1 / worst_ratio)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Back-adjust a split in price_rsi")
    parser.add_argument("--symbol", required=True)
    parser.add_argument(
        "--ratio", type=float, default=None,
        help="Split ratio N (pre-split price is divided by N). Auto if omitted.",
    )
    parser.add_argument(
        "--split-before", default=None,
        help="ISO timestamp; rows strictly before this are adjusted. Auto if omitted.",
    )
    parser.add_argument("--apply", action="store_true", help="Write changes (default dry-run)")
    parser.add_argument(
        "--clear-corrupt-rsi", action="store_true",
        help="Also NULL the rsi of rows AT/AFTER the split (computed on the "
             "stale mixed-scale base; price stays untouched there).",
    )
    parser.add_argument(
        "--database-url", default=os.getenv("DATABASE_URL"),
        help="Defaults to $DATABASE_URL (same DB the app uses).",
    )
    args = parser.parse_args()

    if not args.database_url:
        raise SystemExit("DATABASE_URL not set and --database-url not given")

    storage = Storage(args.database_url)
    try:
        rows = await _fetch_rows(storage, args.symbol)
        if not rows:
            print(f"No price_rsi rows for {args.symbol}")
            return

        det_ts, det_ratio = _detect_split(rows)
        split_before = (
            datetime.fromisoformat(args.split_before)
            if args.split_before else det_ts
        )
        ratio = args.ratio if args.ratio is not None else det_ratio

        print(f"Symbol           : {args.symbol}")
        print(f"Rows total       : {len(rows)} "
              f"({rows[0]['ts']} → {rows[-1]['ts']})")
        if det_ts is not None:
            print(f"Auto-detected    : {det_ratio}:1 split at {det_ts}")
        else:
            print("Auto-detected    : no split-scale drop found")

        if ratio is None or split_before is None:
            raise SystemExit(
                "Could not determine ratio/split date automatically — "
                "pass --ratio and --split-before explicitly."
            )

        affected = [r for r in rows if r["ts"] < split_before]
        if not affected:
            print("No rows before the split date — nothing to adjust.")
            return

        pre_prices = [float(r["price"]) for r in affected]
        print(f"Split ratio      : divide pre-split price by {ratio}")
        print(f"Split-before     : {split_before}")
        print(f"Affected rows    : {len(affected)}")
        print(f"Pre-split price  : min {min(pre_prices):,.4f}  "
              f"max {max(pre_prices):,.4f}")
        print(f"After adjust     : min {min(pre_prices)/ratio:,.4f}  "
              f"max {max(pre_prices)/ratio:,.4f}")

        # Post-split rows carry correct (post-split) prices but a corrupted
        # rsi (computed while the RSI base still held pre-split rows).
        post_split_rsi = [
            r for r in rows if r["ts"] >= split_before and r["rsi"] is not None
        ]
        if args.clear_corrupt_rsi:
            print(f"RSI to clear     : {len(post_split_rsi)} rows "
                  f"at/after {split_before}")

        if not args.apply:
            print("\n[DRY-RUN] No changes written. Re-run with --apply to commit.")
            return

        # Back up affected rows before writing (data/ is gitignored).
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
        )
        os.makedirs(backup_dir, exist_ok=True)
        backup = os.path.join(
            backup_dir, f"price_rsi_backup_{args.symbol}_{stamp}.csv"
        )
        with open(backup, "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["id", "old_price", "rsi", "timestamp"])
            for r in affected:
                writer.writerow([r["id"], r["price"], r["rsi"], r["ts"]])
        print(f"\nBacked up {len(affected)} rows → {backup}")

        if args.clear_corrupt_rsi and post_split_rsi:
            rsi_backup = os.path.join(
                backup_dir, f"rsi_corrupt_backup_{args.symbol}_{stamp}.csv"
            )
            with open(rsi_backup, "w", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(["id", "price", "old_rsi", "timestamp"])
                for r in post_split_rsi:
                    writer.writerow([r["id"], r["price"], r["rsi"], r["ts"]])
            print(f"Backed up {len(post_split_rsi)} rsi rows → {rsi_backup}")

        async with storage.async_session() as session:
            await session.execute(
                text(
                    "UPDATE price_rsi SET price = price / :ratio "
                    "WHERE symbol = :sym AND timestamp < :split_before"
                ),
                {"ratio": ratio, "sym": args.symbol, "split_before": split_before},
            )
            if args.clear_corrupt_rsi:
                await session.execute(
                    text(
                        "UPDATE price_rsi SET rsi = NULL "
                        "WHERE symbol = :sym AND timestamp >= :split_before"
                    ),
                    {"sym": args.symbol, "split_before": split_before},
                )
            await session.commit()
        print(f"Applied: {len(affected)} rows rescaled by 1/{ratio}.")
        if args.clear_corrupt_rsi:
            print(f"Cleared rsi on {len(post_split_rsi)} post-split rows.")
    finally:
        await storage.close()


if __name__ == "__main__":
    load_dotenv()
    asyncio.run(main())
