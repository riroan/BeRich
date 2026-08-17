#!/usr/bin/env python3
"""RSI Mean Reversion Strategy Backtest

The decision logic lives in `RSIMeanReversionBacktest`; the money-side
accounting is `scripts.backtest_engine.run_simulation()`, shared with every
other backtest strategy. `_run_simulation()` stays as the RSI-flavoured
entry both the CLI (`python -m scripts.backtest_rsi`) and the parity tests
call, so CLI and web produce identical results from identical input data.
"""

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Optional

import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.strategy.rsi_rules import (
    calculate_rsi,
    as_levels,
    resolve_buy_stage,
    resolve_sell_stage,
    resolve_stop_loss_stage,
    resolve_take_profit_stage,
)
from scripts._backtest_seed import BACKTEST_STRATEGIES
from scripts.backtest_engine import (  # noqa: F401  (re-exported for callers)
    BacktestSignal,
    BacktestStrategy,
    Trade,
    get_yfinance_symbol,
    load_price_history,
    run_simulation,
    run_symbol_async,
)

if TYPE_CHECKING:
    from src.data.storage import Storage


class RSIMeanReversionBacktest(BacktestStrategy):
    """Averaging-down buy ladder + staged RSI sells + PnL ladders.

    Mirrors src/strategy/builtin/rsi_mean_reversion.py bar for bar — see
    tests/test_live_backtest_parity.py, which asserts the two agree.
    """

    key = "rsi"

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        p = self.params
        self.rsi_period = p.get("rsi_period", 14)
        self.rsi_method = p.get("rsi_method", "wilder")
        # Explicit empty ladder = no exit on that side; missing key falls back
        # to the scalar. Must match the live strategy exactly.
        if "stop_loss_levels" in p:
            self.stop_loss_levels = p["stop_loss_levels"]
        else:
            self.stop_loss_levels = as_levels(p.get("stop_loss", -10))
        if "take_profit_levels" in p:
            self.take_profit_levels = p["take_profit_levels"]
        else:
            self.take_profit_levels = as_levels(p.get("take_profit"))
        self.cooldown_days = p.get("cooldown_days", 1)
        # Must mirror rsi_mean_reversion exactly: 0 = re-enter immediately
        # after a full exit, which is what happens when a symbol's state is
        # wiped.
        self.reentry_cooldown_days = p.get("reentry_cooldown_days", 0)
        self.reset_requires_recovery = p.get("reset_requires_recovery", False)
        self.recovery_rsi = p.get("recovery_rsi", 50)
        self.avg_down_levels = p.get(
            "avg_down_levels", [[30, 0.5], [25, 0.3], [20, 0.2]],
        )
        self.sell_levels = p.get(
            "sell_levels", [[65, 0.3], [70, 0.3], [75, 0.4]],
        )

        # Ladder state
        self.buy_stage = 0
        self.sell_stage = 0
        self.tp_stage = 0
        self.sl_stage = 0
        self.last_buy_date = None
        self.last_sell_date = None
        self.last_exit_date = None
        self.rsi_recovered = False

    @classmethod
    def params_from_request(cls, body) -> dict:
        return {
            "rsi_period": body.rsi_period,
            "rsi_method": body.rsi_method,
            "stop_loss": body.stop_loss,
            "take_profit": body.take_profit,
            "cooldown_days": body.cooldown_days,
            "reset_requires_recovery": body.reset_requires_recovery,
            "recovery_rsi": body.recovery_rsi,
            "avg_down_levels": body.buy_levels,
            "sell_levels": body.sell_levels,
        }

    @property
    def name(self) -> str:
        return "RSI Mean Reversion"

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        df["RSI"] = calculate_rsi(df["Close"], self.rsi_period, self.rsi_method)
        return df.dropna()

    def indicator_series(self, df: pd.DataFrame) -> list[float]:
        return [round(float(r), 2) for r in df["RSI"].tolist()]

    def decide(self, date, row) -> Iterator[BacktestSignal]:
        rsi = row["RSI"]

        # Optional legacy gate: require RSI to recover once after a buy
        # before cooldown can repeat the current buy stage. Default matches
        # the live bot: cooldown_days alone unlocks stage repetition.
        if (
            self.reset_requires_recovery
            and self.last_buy_date is not None
            and rsi >= self.recovery_rsi
        ):
            self.rsi_recovered = True

        # 1) PnL ladders. A position sits on one side of its entry, so the
        # two are mutually exclusive; losses are checked first to keep the
        # ordering explicit. Either one ends the bar.
        if self.pos.shares > 0:
            pnl_pct = self.pos.pnl_pct(row["Close"])
            for levels, stage, reason, resolve in (
                (self.stop_loss_levels, self.sl_stage, "stop_loss",
                 resolve_stop_loss_stage),
                (self.take_profit_levels, self.tp_stage, "take_profit",
                 resolve_take_profit_stage),
            ):
                if not levels:
                    continue
                stage_idx, _ = resolve(pnl_pct, stage, levels)
                if stage_idx is not None:
                    yield BacktestSignal(
                        side="sell",
                        portion=levels[stage_idx][1],
                        reason=reason,
                        stage=stage_idx + 1,
                        record_empty_fill=True,
                    )
                    return

        # 2) Staged RSI sells.
        if self.pos.shares > 0:
            sell_repeat_ready = (
                self.last_sell_date is not None
                and (date - self.last_sell_date).days >= self.cooldown_days
            )
            sell_stage_idx, _ = resolve_sell_stage(
                rsi, self.sell_stage, self.sell_levels, sell_repeat_ready,
            )
            if sell_stage_idx is not None:
                yield BacktestSignal(
                    side="sell",
                    portion=self.sell_levels[sell_stage_idx][1],
                    reason=f"sell_stage_{sell_stage_idx + 1}",
                    stage=sell_stage_idx + 1,
                )

        # 3) Averaging-down buys.
        if (
            self.reentry_cooldown_days
            and self.pos.shares == 0
            and self.last_exit_date is not None
            and (date - self.last_exit_date).days < self.reentry_cooldown_days
        ):
            return

        buy_repeat_ready = (
            self.last_buy_date is not None
            and (date - self.last_buy_date).days >= self.cooldown_days
            and (not self.reset_requires_recovery or self.rsi_recovered)
        )
        buy_stage_idx, _ = resolve_buy_stage(
            rsi, self.buy_stage, self.avg_down_levels, buy_repeat_ready,
        )
        if buy_stage_idx is not None:
            yield BacktestSignal(
                side="buy",
                portion=self.avg_down_levels[buy_stage_idx][1],
                reason=f"avg_down_stage_{buy_stage_idx + 1}",
                stage=buy_stage_idx + 1,
            )

    def on_fill(self, signal, date, price, filled, shares_before) -> None:
        if signal.side == "buy":
            self.buy_stage = signal.stage
            if shares_before == 0:
                self.sell_stage = 0
                self.last_exit_date = None
            self.last_buy_date = date
            self.rsi_recovered = False
            return

        if signal.reason == "stop_loss":
            self.sl_stage = signal.stage
        elif signal.reason == "take_profit":
            self.tp_stage = signal.stage
        else:
            self.sell_stage = signal.stage
            self.last_sell_date = date

        if self.pos.shares == 0:
            self.buy_stage = 0
            self.sell_stage = 0
            self.tp_stage = 0
            self.sl_stage = 0
            self.last_sell_date = None
            self.last_exit_date = date


def _run_simulation(
    df: pd.DataFrame,
    symbol: str,
    params: dict,
    initial_capital: float = 10_000_000,
    verbose: bool = False,
) -> dict:
    """Run the RSI Mean Reversion simulation on a price DataFrame."""
    return run_simulation(
        df, symbol, RSIMeanReversionBacktest(params), initial_capital, verbose,
    )


def backtest_symbol(
    symbol: str,
    market: str,
    start_date: str,
    end_date: str,
    params: dict,
    initial_capital: float = 10_000_000,
) -> Optional[dict]:
    """CLI sync entry. yfinance only. Prints per-trade events."""
    yf_symbol = get_yfinance_symbol(symbol, market)
    print(f"\n{'='*60}")
    print(f"Backtesting {symbol} ({market.upper()})")
    print(f"{'='*60}")

    df = yf.download(yf_symbol, start=start_date, end=end_date, progress=False)
    if df.empty:
        print(f"No data found for {symbol}")
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    print(f"Period: {df.index[0].strftime('%Y-%m-%d')} ~ {df.index[-1].strftime('%Y-%m-%d')}")
    print(f"Trading days: {len(df)}")

    result = _run_simulation(df, symbol, params, initial_capital, verbose=True)
    result["market"] = market
    result["data_source"] = "yfinance"

    print(f"\n--- Results for {symbol} ---")
    print(f"Total Return:      {result['total_return_pct']:+.2f}%")
    print(f"Buy & Hold:        {result['buy_hold_return_pct']:+.2f}%")
    print(f"Trades:            {result['num_trades']}")
    print(f"Win Rate:          {result['win_rate_pct']:.1f}%")
    return result


async def backtest_symbol_async(
    symbol: str,
    market: str,
    start_date: str,
    end_date: str,
    params: dict,
    storage: "Storage",
    initial_capital: float = 10_000_000,
) -> tuple[Optional[dict], Optional[str]]:
    """Web async entry for the RSI strategy (thin wrapper over the engine)."""
    return await run_symbol_async(
        symbol, market, start_date, end_date,
        RSIMeanReversionBacktest(params), storage, initial_capital,
    )


def main():
    from datetime import datetime

    start_date = datetime(2020, 1, 1)
    end_date = datetime(2023, 1, 1)

    print("=" * 60)
    print("RSI MEAN REVERSION STRATEGY BACKTEST")
    print(f"Period: {start_date.strftime('%Y-%m-%d')} ~ {end_date.strftime('%Y-%m-%d')}")
    print("=" * 60)

    all_results = []
    for strategy_config in BACKTEST_STRATEGIES:
        if not strategy_config.get("enabled"):
            continue
        market = strategy_config.get("market", "krx")
        symbols = strategy_config.get("symbols", [])
        params = strategy_config.get("params", {})
        for symbol in symbols:
            result = backtest_symbol(
                symbol=symbol, market=market,
                start_date=start_date.strftime("%Y-%m-%d"),
                end_date=end_date.strftime("%Y-%m-%d"),
                params=params, initial_capital=10_000_000,
            )
            if result:
                all_results.append(result)

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"{'Symbol':<10} {'Market':<8} {'Return':>10} {'B&H':>10} {'Trades':>8} {'WinRate':>8}")
    print("-" * 60)

    total_return_sum = 0
    buy_hold_sum = 0
    for r in all_results:
        print(f"{r['symbol']:<10} {r['market'].upper():<8} {r['total_return_pct']:>+9.2f}% {r['buy_hold_return_pct']:>+9.2f}% {r['num_trades']:>8} {r['win_rate_pct']:>7.1f}%")
        total_return_sum += r["total_return_pct"]
        buy_hold_sum += r["buy_hold_return_pct"]

    if all_results:
        avg_return = total_return_sum / len(all_results)
        avg_bh = buy_hold_sum / len(all_results)
        print("-" * 60)
        print(f"{'AVERAGE':<10} {'':<8} {avg_return:>+9.2f}% {avg_bh:>+9.2f}%")
        print("\n" + "=" * 60)
        if avg_return > avg_bh:
            print(f"Strategy OUTPERFORMED Buy & Hold by {avg_return - avg_bh:.2f}%p")
        else:
            print(f"Strategy UNDERPERFORMED Buy & Hold by {avg_bh - avg_return:.2f}%p")


if __name__ == "__main__":
    main()
