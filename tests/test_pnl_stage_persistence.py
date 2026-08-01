"""tp_stage / sl_stage survive a restart.

Without persistence a restart resets the PnL ladders to 0 and the same rung
fires again at the same PnL, selling another slice on every deploy.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import text

from src.core.types import Market, SignalType
from src.data.storage import Storage

OLD_SCHEMA = """
CREATE TABLE current_positions (
    id INTEGER PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL,
    market VARCHAR(20) NOT NULL,
    quantity INTEGER NOT NULL,
    avg_price NUMERIC(20, 8) NOT NULL,
    buy_stage INTEGER NOT NULL DEFAULT 0,
    sell_stage INTEGER NOT NULL DEFAULT 0,
    max_buy_stages INTEGER NOT NULL DEFAULT 3,
    max_sell_stages INTEGER NOT NULL DEFAULT 3,
    stage_cooldown_days INTEGER NOT NULL DEFAULT 0,
    last_buy_date VARCHAR(20),
    last_sell_date VARCHAR(20),
    stop_loss_pct NUMERIC(10, 4) NOT NULL DEFAULT -10.0,
    updated_at DATETIME
)
"""


async def _columns(storage: Storage) -> set[str]:
    async with storage.engine.begin() as conn:
        rows = await conn.execute(text("PRAGMA table_info(current_positions)"))
        return {r[1] for r in rows}


def test_migration_adds_the_columns_to_an_existing_table(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'migrate.db'}"

    async def run():
        storage = Storage(db_url)
        # Pre-create the table as it looked before the PnL ladders existed,
        # with a row in it, so create_all cannot silently do the work.
        async with storage.engine.begin() as conn:
            await conn.execute(text(OLD_SCHEMA))
            await conn.execute(text(
                "INSERT INTO current_positions "
                "(symbol, market, quantity, avg_price, buy_stage) "
                "VALUES ('AAPL', 'NASDAQ', 5, 100.0, 2)"
            ))

        await storage.initialize()
        cols = await _columns(storage)

        async with storage.engine.begin() as conn:
            row = (await conn.execute(text(
                "SELECT buy_stage, tp_stage, sl_stage FROM current_positions"
            ))).first()

        # Idempotent: a second start must not fail on the existing columns.
        await storage.initialize()
        await storage.close()
        return cols, row

    cols, row = asyncio.run(run())

    assert {"tp_stage", "sl_stage"} <= cols
    # The pre-existing row keeps its data and defaults the new columns.
    assert tuple(row) == (2, 0, 0)


def test_pnl_stages_round_trip_through_storage(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'roundtrip.db'}"

    async def run():
        storage = Storage(db_url)
        await storage.initialize()
        await storage.replace_current_positions_for_market(
            Market.NASDAQ,
            [{
                "symbol": "AAPL", "quantity": 7, "avg_price": Decimal("100"),
                "buy_stage": 2, "sell_stage": 1, "tp_stage": 2, "sl_stage": 1,
            }],
        )
        records = await storage.get_current_positions()
        await storage.close()
        return records

    records = asyncio.run(run())

    assert records[0]["tp_stage"] == 2
    assert records[0]["sl_stage"] == 1


def test_changed_pnl_stage_is_written_not_skipped_as_unchanged(tmp_path):
    # The write path short-circuits when nothing changed. A ladder advance
    # with everything else identical must still be persisted.
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'dirty.db'}"

    def _position(tp_stage):
        return [{
            "symbol": "AAPL", "quantity": 7, "avg_price": Decimal("100"),
            "tp_stage": tp_stage,
        }]

    async def run():
        storage = Storage(db_url)
        await storage.initialize()
        await storage.replace_current_positions_for_market(
            Market.NASDAQ, _position(0),
        )
        unchanged = await storage.replace_current_positions_for_market(
            Market.NASDAQ, _position(0),
        )
        changed = await storage.replace_current_positions_for_market(
            Market.NASDAQ, _position(1),
        )
        records = await storage.get_current_positions()
        await storage.close()
        return unchanged, changed, records

    unchanged, changed, records = asyncio.run(run())

    assert unchanged is False
    assert changed is True
    assert records[0]["tp_stage"] == 1


@pytest.mark.asyncio
async def test_restore_puts_the_stages_back_on_the_strategy():
    from src.bot.data_loader import DataLoaderMixin
    from src.strategy.builtin.rsi_mean_reversion import (
        RSIMeanReversionStrategy,
    )
    from src.web.app import DashboardState

    strategy = RSIMeanReversionStrategy(
        symbols=["AAPL"], market=Market.NASDAQ, params={},
    )

    class _Bot(DataLoaderMixin):
        pass

    bot = _Bot()
    bot.strategy_engine = type(
        "E", (), {"get_strategies": lambda self: [strategy]},
    )()
    state = DashboardState()
    state.update_position(
        symbol="AAPL", market="NASDAQ", quantity=5,
        avg_price=100.0, current_price=131.0,
        buy_stage=2, sell_stage=1, tp_stage=2, sl_stage=1,
    )
    bot.dashboard = state

    bot.restore_strategy_state_from_positions()

    assert strategy._tp_stages["AAPL"] == 2
    assert strategy._sl_stages["AAPL"] == 1


@pytest.mark.asyncio
async def test_restore_rearms_exits_after_a_hot_reload():
    """Hot reload builds a fresh instance; without the broker mirrors it can
    buy but never sell.

    Every exit path is gated on `current_position > 0`; the buy path is not.
    So a strategy that loses `_positions` ends up with exits closed and the
    entrance open, and the first buy fill then overwrites the real cost basis
    with that fill price.
    """
    import pandas as pd
    from src.bot.data_loader import DataLoaderMixin
    from src.strategy.builtin.rsi_mean_reversion import (
        RSIMeanReversionStrategy,
    )
    from src.web.app import DashboardState

    # Deep loss, so a restored strategy must want out.
    closes = [100 + (0.4 if i % 2 else -0.4) for i in range(30)] + [
        100 * (1 - 0.02 * i) for i in range(1, 21)
    ]
    strategy = RSIMeanReversionStrategy(
        symbols=["AAPL"], market=Market.NASDAQ,
        params={"stop_loss": -10, "sell_levels": [[99, 1.0]]},
    )
    strategy._daily_bars["AAPL"] = pd.DataFrame(
        {"close": closes},
        index=pd.bdate_range(start="2022-01-03", periods=len(closes)),
    )

    class _Bot(DataLoaderMixin):
        pass

    bot = _Bot()
    bot.strategy_engine = type(
        "E", (), {"get_strategies": lambda self: [strategy]},
    )()
    state = DashboardState()
    state.update_position(
        symbol="AAPL", market="NASDAQ", quantity=10,
        avg_price=100.0, current_price=float(closes[-1]),
        buy_stage=1, sell_stage=0,
    )
    bot.dashboard = state

    # A freshly built instance does not merely go quiet: the exit paths are
    # gated on the position it lost, the buy path is not, so it averages down
    # into a deep loss it can no longer sell out of.
    assert strategy.get_position("AAPL") == 0
    before = await strategy.calculate_signal("AAPL")
    assert before is not None and before.signal_type is SignalType.ENTRY_LONG

    bot.restore_strategy_state_from_positions()

    assert strategy.get_position("AAPL") == 10
    assert strategy._entry_prices["AAPL"] == Decimal("100.0")

    signal = await strategy.calculate_signal("AAPL")
    assert signal is not None
    assert signal.metadata["reason"] == "stop_loss"
