#!/usr/bin/env python3
"""RSI Mean Reversion Strategy Backtest

Used by both the CLI (`python -m scripts.backtest_rsi`) and the web dashboard
(`/api/backtest`). The simulation loop lives in `_run_simulation()` so CLI
and web produce identical results from identical input data.
"""

import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.types import Market
from src.strategy.rsi_rules import (
    calculate_rsi,
    resolve_buy_stage,
    resolve_sell_stage,
)
from scripts._backtest_seed import BACKTEST_STRATEGIES

if TYPE_CHECKING:
    from src.data.storage import Storage


@dataclass
class Trade:
    """Trade record"""
    symbol: str
    buy_date: datetime
    buy_price: float
    buy_stage: int
    portion: float
    shares: int
    sell_date: datetime = None
    sell_price: float = None
    sell_reason: str = None
    pnl_pct: float = None


def get_yfinance_symbol(symbol: str, market: str) -> str:
    """Convert symbol to yfinance format"""
    if market == "krx":
        return f"{symbol}.KS"
    return symbol


def _run_simulation(
    df: pd.DataFrame,
    symbol: str,
    params: dict,
    initial_capital: float = 10_000_000,
    verbose: bool = False,
) -> dict:
    """Run RSI Mean Reversion simulation on a price DataFrame.

    df must have a `Close` column and a DatetimeIndex.
    Returns the full result dict including timeseries for chart rendering.
    Pure function — no I/O, no prints unless verbose=True.
    """
    rsi_period = params.get("rsi_period", 14)
    stop_loss_pct = params.get("stop_loss", -10)
    cooldown_days = params.get("cooldown_days", 1)
    reset_requires_recovery = params.get("reset_requires_recovery", False)
    recovery_rsi = params.get("recovery_rsi", 50)
    avg_down_levels = params.get("avg_down_levels", [[30, 0.5], [25, 0.3], [20, 0.2]])
    sell_levels = params.get("sell_levels", [[65, 0.3], [70, 0.3], [75, 0.4]])

    df = df.copy()
    df["RSI"] = calculate_rsi(df["Close"], rsi_period)
    df = df.dropna()

    if df.empty:
        return _empty_result(symbol, initial_capital)

    trades: list[Trade] = []
    buy_events: list[dict] = []  # {date, price, stage} — every BUY at the moment it fires
    capital = initial_capital
    position_shares = 0
    position_cost = 0.0
    buy_stage = 0
    sell_stage = 0
    last_buy_date = None
    last_sell_date = None
    rsi_recovered = False

    for date, row in df.iterrows():
        rsi = row["RSI"]
        price = row["Close"]

        # Optional legacy gate: require RSI to recover once after a buy
        # before cooldown can repeat the current buy stage. Default matches
        # the live bot: cooldown_days alone unlocks stage repetition.
        if reset_requires_recovery and last_buy_date is not None and rsi >= recovery_rsi:
            rsi_recovered = True

        if position_shares > 0:
            avg_price = position_cost / position_shares
            pnl_pct = (price - avg_price) / avg_price * 100

            if pnl_pct <= stop_loss_pct:
                sell_value = position_shares * price
                capital += sell_value
                trade = Trade(
                    symbol=symbol, buy_date=last_buy_date, buy_price=avg_price,
                    buy_stage=buy_stage, portion=1.0, shares=position_shares,
                    sell_date=date, sell_price=price, sell_reason="stop_loss",
                    pnl_pct=pnl_pct,
                )
                trades.append(trade)
                if verbose:
                    print(f"[STOP] {date.strftime('%Y-%m-%d')} | {price:,.0f} | RSI:{rsi:.1f} | PnL:{pnl_pct:+.1f}%")
                position_shares = 0
                position_cost = 0.0
                buy_stage = 0
                sell_stage = 0
                last_sell_date = None
                continue

        if position_shares > 0:
            sell_repeat_ready = (
                last_sell_date is not None
                and (date - last_sell_date).days >= cooldown_days
            )
            sell_stage_idx, _ = resolve_sell_stage(
                rsi, sell_stage, sell_levels, sell_repeat_ready,
            )

            if sell_stage_idx is not None:
                rsi_threshold, portion = sell_levels[sell_stage_idx]
                shares_to_sell = int(position_shares * portion)
                if shares_to_sell == 0 and position_shares > 0 and portion > 0:
                    shares_to_sell = 1
                if shares_to_sell > 0:
                    sell_value = shares_to_sell * price
                    capital += sell_value
                    avg_price = position_cost / position_shares
                    pnl_pct = (price - avg_price) / avg_price * 100
                    trade = Trade(
                        symbol=symbol, buy_date=last_buy_date, buy_price=avg_price,
                        buy_stage=buy_stage, portion=portion, shares=shares_to_sell,
                        sell_date=date, sell_price=price,
                        sell_reason=f"sell_stage_{sell_stage_idx+1}", pnl_pct=pnl_pct,
                    )
                    trades.append(trade)
                    if verbose:
                        print(f"[SELL{sell_stage_idx+1}] {date.strftime('%Y-%m-%d')} | {price:,.0f} | RSI:{rsi:.1f} | {portion*100:.0f}% | PnL:{pnl_pct:+.1f}")
                    position_cost -= avg_price * shares_to_sell
                    position_shares -= shares_to_sell
                    sell_stage = sell_stage_idx + 1
                    last_sell_date = date
                    if position_shares == 0:
                        buy_stage = 0
                        sell_stage = 0
                        position_cost = 0.0
                        last_sell_date = None

        buy_repeat_ready = (
            last_buy_date is not None
            and (date - last_buy_date).days >= cooldown_days
            and (not reset_requires_recovery or rsi_recovered)
        )
        buy_stage_idx, _ = resolve_buy_stage(
            rsi, buy_stage, avg_down_levels, buy_repeat_ready,
        )

        if buy_stage_idx is not None:
            rsi_threshold, portion = avg_down_levels[buy_stage_idx]
            # Match live bot: buy_amount = (max_symbol_value − current_value) × portion
            # Single-symbol backtest → max_symbol_value = initial_capital, mark-to-market.
            current_value = position_shares * price
            remaining_room = max(initial_capital - current_value, 0)
            buy_amount = min(remaining_room * portion, capital)
            shares_to_buy = int(buy_amount / price)
            if shares_to_buy > 0 and capital >= shares_to_buy * price:
                cost = shares_to_buy * price
                capital -= cost
                had_position = position_shares > 0
                position_shares += shares_to_buy
                position_cost += cost
                buy_stage = buy_stage_idx + 1
                if not had_position:
                    sell_stage = 0
                last_buy_date = date
                rsi_recovered = False
                buy_events.append({
                    "date": date.strftime("%Y-%m-%d"),
                    "price": float(price),
                    "stage": buy_stage_idx + 1,
                })
                if verbose:
                    print(f"[BUY{buy_stage_idx+1}] {date.strftime('%Y-%m-%d')} | {price:,.0f} | RSI:{rsi:.1f} | {portion*100:.0f}% | Shares:{shares_to_buy}")

    if position_shares > 0:
        last_price = df.iloc[-1]["Close"]
        sell_value = position_shares * last_price
        capital += sell_value
        avg_price = position_cost / position_shares
        pnl_pct = (last_price - avg_price) / avg_price * 100
        trade = Trade(
            symbol=symbol, buy_date=last_buy_date, buy_price=avg_price,
            buy_stage=buy_stage, portion=1.0, shares=position_shares,
            sell_date=df.index[-1], sell_price=last_price, sell_reason="end_of_period",
            pnl_pct=pnl_pct,
        )
        trades.append(trade)
        if verbose:
            print(f"[CLOSE] {df.index[-1].strftime('%Y-%m-%d')} | {last_price:,.0f} | PnL:{pnl_pct:+.1f}%")

    total_return = (capital - initial_capital) / initial_capital * 100
    buy_hold_return = (df.iloc[-1]["Close"] - df.iloc[0]["Close"]) / df.iloc[0]["Close"] * 100
    winning = [t for t in trades if t.pnl_pct and t.pnl_pct > 0]
    win_rate = len(winning) / len(trades) * 100 if trades else 0

    def _stage_from_reason(reason: str) -> int:
        if reason and reason.startswith("sell_stage_"):
            return int(reason.split("_")[-1])
        return 0  # stop_loss / end_of_period — frontend reads `reason` not `stage`

    sell_trades_payload = [
        {
            "date": t.sell_date.strftime("%Y-%m-%d"),
            "price": float(t.sell_price),
            "reason": t.sell_reason,
            "stage": _stage_from_reason(t.sell_reason),
        }
        for t in trades if t.sell_date is not None
    ]

    return {
        "symbol": symbol,
        "total_return_pct": round(total_return, 4),
        "buy_hold_return_pct": round(buy_hold_return, 4),
        "num_trades": len(trades),
        "num_buys": len(buy_events),
        "num_sells": len(sell_trades_payload),
        "win_rate_pct": round(win_rate, 2),
        "final_capital": capital,
        "trades": trades,
        "prices": [round(float(p), 4) for p in df["Close"].tolist()],
        "dates": df.index.strftime("%Y-%m-%d").tolist(),
        "rsi_values": [round(float(r), 2) for r in df["RSI"].tolist()],
        "buy_trades": buy_events,
        "sell_trades": sell_trades_payload,
    }


def _empty_result(symbol: str, initial_capital: float) -> dict:
    return {
        "symbol": symbol,
        "total_return_pct": 0.0,
        "buy_hold_return_pct": 0.0,
        "num_trades": 0,
        "num_buys": 0,
        "num_sells": 0,
        "win_rate_pct": 0.0,
        "final_capital": initial_capital,
        "trades": [],
        "prices": [],
        "dates": [],
        "rsi_values": [],
        "buy_trades": [],
        "sell_trades": [],
    }


async def load_price_history(
    symbol: str,
    market: str,
    start_date: str,
    end_date: str,
    storage: "Storage",
) -> tuple[pd.DataFrame, str]:
    """Load OHLC daily history. KIS DB first, yfinance fallback (10s timeout).

    Returns (df, data_source) where data_source ∈ {"kis_db", "yfinance", "none", "timeout"}.
    """
    start = datetime.fromisoformat(start_date)
    end = datetime.fromisoformat(end_date)
    mkt = Market.from_string(market)

    db_bars = await storage.get_bars(
        symbol=symbol, timeframe="1d", start=start, end=end, market=mkt,
    )

    expected_days = max((end - start).days * 5 // 7, 1)
    if db_bars and len(db_bars) >= int(expected_days * 0.8):
        df = pd.DataFrame(
            [
                {
                    "Open": float(b.open),
                    "High": float(b.high),
                    "Low": float(b.low),
                    "Close": float(b.close),
                    "Volume": int(b.volume),
                }
                for b in db_bars
            ],
            index=pd.DatetimeIndex([b.timestamp for b in db_bars]),
        )
        return df, "kis_db"

    yf_symbol = get_yfinance_symbol(symbol, market)
    try:
        df = await asyncio.wait_for(
            asyncio.to_thread(
                yf.download, yf_symbol, start=start_date, end=end_date, progress=False,
            ),
            timeout=10.0,
        )
    except asyncio.TimeoutError:
        return pd.DataFrame(), "timeout"

    if df.empty:
        return df, "none"
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df, "yfinance"


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
    """Web async entry. KIS DB first, yfinance fallback. Quiet (no prints).

    Returns (result, error_code) where exactly one is non-None.
    error_code ∈ {"ticker_not_found", "data_source_timeout"} on failure.
    """
    df, source = await load_price_history(symbol, market, start_date, end_date, storage)
    if source == "none":
        return None, "ticker_not_found"
    if source == "timeout":
        return None, "data_source_timeout"
    result = _run_simulation(df, symbol, params, initial_capital, verbose=False)
    result["market"] = market
    result["data_source"] = source
    return result, None


def main():
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
