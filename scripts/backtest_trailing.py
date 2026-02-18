#!/usr/bin/env python3
"""RSI + Trailing Stop Strategy Backtest"""

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import List, Optional
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.config import Config


@dataclass
class Trade:
    """Trade record"""
    symbol: str
    buy_date: datetime
    buy_price: float
    shares: int
    sell_date: datetime = None
    sell_price: float = None
    sell_reason: str = None
    pnl_pct: float = None
    peak_price: float = None


def calculate_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    """Calculate RSI indicator"""
    delta = prices.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def get_yfinance_symbol(symbol: str, market: str) -> str:
    """Convert symbol to yfinance format"""
    if market == "krx":
        return f"{symbol}.KS"
    return symbol


def backtest_symbol(
    symbol: str,
    market: str,
    start_date: str,
    end_date: str,
    params: dict,
    initial_capital: float = 10_000_000,
) -> dict:
    """Backtest RSI + Trailing Stop strategy"""

    yf_symbol = get_yfinance_symbol(symbol, market)

    print(f"\n{'='*60}")
    print(f"Backtesting {symbol} ({market.upper()}) - TRAILING STOP")
    print(f"{'='*60}")

    df = yf.download(yf_symbol, start=start_date, end=end_date, progress=False)

    if df.empty:
        print(f"No data found for {symbol}")
        return None

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    rsi_period = params.get("rsi_period", 14)
    df['RSI'] = calculate_rsi(df['Close'], rsi_period)
    df = df.dropna()

    print(f"Period: {df.index[0].strftime('%Y-%m-%d')} ~ {df.index[-1].strftime('%Y-%m-%d')}")
    print(f"Trading days: {len(df)}")

    # Strategy parameters
    rsi_buy = params.get("rsi_buy", 30)
    trailing_stop_pct = params.get("trailing_stop", -10)  # -10% from peak
    stop_loss_pct = params.get("stop_loss", -10)  # -10% from entry

    # State tracking
    trades: List[Trade] = []
    capital = initial_capital
    position_shares = 0
    entry_price = 0.0
    peak_price = 0.0
    last_buy_date = None

    for date, row in df.iterrows():
        rsi = row['RSI']
        price = row['Close']

        if position_shares > 0:
            # Update peak price
            if price > peak_price:
                peak_price = price

            # Check stop loss (from entry)
            entry_pnl_pct = (price - entry_price) / entry_price * 100
            if entry_pnl_pct <= stop_loss_pct:
                sell_value = position_shares * price
                capital += sell_value

                trade = Trade(
                    symbol=symbol,
                    buy_date=last_buy_date,
                    buy_price=entry_price,
                    shares=position_shares,
                    sell_date=date,
                    sell_price=price,
                    sell_reason="stop_loss",
                    pnl_pct=entry_pnl_pct,
                    peak_price=peak_price,
                )
                trades.append(trade)
                print(f"[STOP] {date.strftime('%Y-%m-%d')} | {price:,.0f} | RSI:{rsi:.1f} | PnL:{entry_pnl_pct:+.1f}%")

                position_shares = 0
                entry_price = 0.0
                peak_price = 0.0
                continue

            # Check trailing stop (from peak)
            trailing_pnl_pct = (price - peak_price) / peak_price * 100
            if trailing_pnl_pct <= trailing_stop_pct:
                sell_value = position_shares * price
                capital += sell_value

                final_pnl_pct = (price - entry_price) / entry_price * 100

                trade = Trade(
                    symbol=symbol,
                    buy_date=last_buy_date,
                    buy_price=entry_price,
                    shares=position_shares,
                    sell_date=date,
                    sell_price=price,
                    sell_reason="trailing_stop",
                    pnl_pct=final_pnl_pct,
                    peak_price=peak_price,
                )
                trades.append(trade)
                print(f"[TRAIL] {date.strftime('%Y-%m-%d')} | {price:,.0f} | Peak:{peak_price:,.0f} | PnL:{final_pnl_pct:+.1f}%")

                position_shares = 0
                entry_price = 0.0
                peak_price = 0.0
                continue

        # Buy signal: RSI <= 30
        if position_shares == 0 and rsi <= rsi_buy:
            shares_to_buy = int(capital / price)
            if shares_to_buy > 0:
                cost = shares_to_buy * price
                capital -= cost
                position_shares = shares_to_buy
                entry_price = price
                peak_price = price
                last_buy_date = date

                print(f"[BUY] {date.strftime('%Y-%m-%d')} | {price:,.0f} | RSI:{rsi:.1f} | Shares:{shares_to_buy}")

    # Close open position at end
    if position_shares > 0:
        last_price = df.iloc[-1]['Close']
        sell_value = position_shares * last_price
        capital += sell_value

        final_pnl_pct = (last_price - entry_price) / entry_price * 100

        trade = Trade(
            symbol=symbol,
            buy_date=last_buy_date,
            buy_price=entry_price,
            shares=position_shares,
            sell_date=df.index[-1],
            sell_price=last_price,
            sell_reason="end_of_period",
            pnl_pct=final_pnl_pct,
            peak_price=peak_price,
        )
        trades.append(trade)
        print(f"[CLOSE] {df.index[-1].strftime('%Y-%m-%d')} | {last_price:,.0f} | PnL:{final_pnl_pct:+.1f}%")

    # Calculate metrics
    total_return = (capital - initial_capital) / initial_capital * 100
    buy_hold_return = (df.iloc[-1]['Close'] - df.iloc[0]['Close']) / df.iloc[0]['Close'] * 100

    winning_trades = [t for t in trades if t.pnl_pct and t.pnl_pct > 0]
    win_rate = len(winning_trades) / len(trades) * 100 if trades else 0

    print(f"\n--- Results for {symbol} ---")
    print(f"Total Return:      {total_return:+.2f}%")
    print(f"Buy & Hold:        {buy_hold_return:+.2f}%")
    print(f"Trades:            {len(trades)}")
    print(f"Win Rate:          {win_rate:.1f}%")

    return {
        "symbol": symbol,
        "market": market,
        "total_return_pct": total_return,
        "buy_hold_return_pct": buy_hold_return,
        "num_trades": len(trades),
        "win_rate_pct": win_rate,
        "final_capital": capital,
        "trades": trades,
    }


def main():
    config = Config("config")
    config.load()

    # Backtest period: 2020-2023 (COVID crash + 2022 bear market)
    start_date = datetime(2020, 1, 1)
    end_date = datetime(2023, 1, 1)

    print("="*60)
    print("RSI + TRAILING STOP STRATEGY BACKTEST")
    print(f"Period: {start_date.strftime('%Y-%m-%d')} ~ {end_date.strftime('%Y-%m-%d')}")
    print("Buy: RSI <= 30 | Sell: Trailing Stop -10% from peak")
    print("="*60)

    all_results = []

    for strategy_config in config.strategies:
        if not strategy_config.get("enabled"):
            continue

        market = strategy_config.get("market", "krx")
        symbols = strategy_config.get("symbols", [])
        params = strategy_config.get("params", {})
        params["trailing_stop"] = -10  # Add trailing stop param

        for symbol in symbols:
            result = backtest_symbol(
                symbol=symbol,
                market=market,
                start_date=start_date.strftime("%Y-%m-%d"),
                end_date=end_date.strftime("%Y-%m-%d"),
                params=params,
                initial_capital=10_000_000,
            )
            if result:
                all_results.append(result)

    # Summary
    print("\n" + "="*60)
    print("SUMMARY - TRAILING STOP STRATEGY")
    print("="*60)
    print(f"{'Symbol':<10} {'Market':<8} {'Return':>10} {'B&H':>10} {'Trades':>8} {'WinRate':>8}")
    print("-"*60)

    total_return_sum = 0
    buy_hold_sum = 0

    for r in all_results:
        print(f"{r['symbol']:<10} {r['market'].upper():<8} {r['total_return_pct']:>+9.2f}% {r['buy_hold_return_pct']:>+9.2f}% {r['num_trades']:>8} {r['win_rate_pct']:>7.1f}%")
        total_return_sum += r['total_return_pct']
        buy_hold_sum += r['buy_hold_return_pct']

    if all_results:
        avg_return = total_return_sum / len(all_results)
        avg_bh = buy_hold_sum / len(all_results)
        print("-"*60)
        print(f"{'AVERAGE':<10} {'':<8} {avg_return:>+9.2f}% {avg_bh:>+9.2f}%")

        print("\n" + "="*60)
        if avg_return > avg_bh:
            print(f"Strategy OUTPERFORMED Buy & Hold by {avg_return - avg_bh:.2f}%p")
        else:
            print(f"Strategy UNDERPERFORMED Buy & Hold by {avg_bh - avg_return:.2f}%p")


if __name__ == "__main__":
    main()
