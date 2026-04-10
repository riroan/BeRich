"""
Migration: WatchedSymbol + StrategyParams → StrategyConfig

Run this BEFORE removing strategies.yaml and old models.
Usage: python -m scripts.migrate_strategy_configs
"""

import asyncio
import json
import logging
from src.data.storage import Storage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Hardcoded from current strategies.yaml (DB doesn't have class_path/market)
STRATEGY_METADATA = {
    "KRX_RSI_MeanReversion": {
        "class_path": "src.strategy.builtin.rsi_mean_reversion.RSIMeanReversionStrategy",
        "market": "krx",
    },
    "NASDAQ_RSI_MeanReversion": {
        "class_path": "src.strategy.builtin.rsi_mean_reversion.RSIMeanReversionStrategy",
        "market": "nasdaq",
    },
    "NYSE_RSI_MeanReversion": {
        "class_path": "src.strategy.builtin.rsi_mean_reversion.RSIMeanReversionStrategy",
        "market": "nyse",
    },
    "AMEX_RSI_MeanReversion": {
        "class_path": "src.strategy.builtin.rsi_mean_reversion.RSIMeanReversionStrategy",
        "market": "amex",
    },
    "KRX_Momentum": {
        "class_path": "src.strategy.builtin.momentum.MomentumStrategy",
        "market": "krx",
    },
}


async def migrate():
    storage = Storage("sqlite+aiosqlite:///data/trading.db")
    await storage.initialize()

    try:
        # Check if strategy_configs already has data
        existing = await storage.get_all_strategy_configs()
        if existing:
            logger.info(
                f"strategy_configs already has "
                f"{len(existing)} entries. Skipping."
            )
            return

        # Read old tables
        from collections import defaultdict
        from sqlalchemy import select, text

        # Get watched symbols grouped by strategy
        symbols_by_strategy = defaultdict(list)
        async with storage.async_session() as session:
            try:
                result = await session.execute(
                    text(
                        "SELECT symbol, market, "
                        "strategy_name, enabled, "
                        "max_weight "
                        "FROM watched_symbols"
                    )
                )
                for row in result:
                    symbols_by_strategy[row[2]].append({
                        "symbol": row[0],
                        "max_weight": float(
                            row[4]
                        ) if row[4] else 20.0,
                    })
            except Exception as e:
                logger.warning(
                    f"No watched_symbols table: {e}"
                )

        # Get strategy params
        params_by_strategy = {}
        async with storage.async_session() as session:
            try:
                result = await session.execute(
                    text(
                        "SELECT strategy_name, params_json "
                        "FROM strategy_params"
                    )
                )
                for row in result:
                    params_by_strategy[row[0]] = (
                        json.loads(row[1])
                    )
            except Exception as e:
                logger.warning(
                    f"No strategy_params table: {e}"
                )

        # Merge into strategy_configs
        migrated = 0
        for name, meta in STRATEGY_METADATA.items():
            symbols = symbols_by_strategy.get(name, [])
            params = params_by_strategy.get(name, {})

            # If no symbols in DB, skip disabled strategies
            if not symbols and name.startswith("KRX"):
                logger.info(f"Skipping disabled: {name}")
                continue

            # Default params if not in DB
            if not params:
                params = {
                    "rsi_period": 14,
                    "stop_loss": -10,
                    "cooldown_days": 3,
                    "avg_down_levels": [
                        [35, 0.3], [30, 0.35], [25, 0.35],
                    ],
                    "sell_levels": [
                        [70, 0.25], [75, 0.35], [80, 0.4],
                    ],
                }

            enabled = len(symbols) > 0

            await storage.create_strategy_config(
                name=name,
                class_path=meta["class_path"],
                market=meta["market"],
                symbols=symbols if symbols else [],
                params=params,
                enabled=enabled,
            )
            migrated += 1
            logger.info(
                f"Migrated: {name} "
                f"({len(symbols)} symbols)"
            )

        logger.info(
            f"Migration complete: "
            f"{migrated} strategies migrated"
        )

        # Rename old tables as backup
        async with storage.engine.begin() as conn:
            for table in [
                "watched_symbols", "strategy_params",
            ]:
                try:
                    await conn.execute(text(
                        f"ALTER TABLE {table} "
                        f"RENAME TO {table}_backup"
                    ))
                    logger.info(
                        f"Renamed {table} → "
                        f"{table}_backup"
                    )
                except Exception as e:
                    logger.warning(
                        f"Could not rename {table}: {e}"
                    )

    finally:
        await storage.close()


if __name__ == "__main__":
    asyncio.run(migrate())
