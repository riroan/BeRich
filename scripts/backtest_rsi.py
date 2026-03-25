#!/usr/bin/env python3
"""RSI Mean Reversion Strategy Backtest"""

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import List, Dict, Optional
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
    buy_stage: int
    portion: float
    shares: int
    sell_date: datetime = None
    sell_price: float = None
    sell_reason: str = None
    pnl_pct: float = None


def calculate_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    """Calculate RSI indicator"""
    delta = prices.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss.replace(0, 1e-10)  # Avoid division by zero
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
    """Backtest RSI Mean Reversion strategy for a single symbol"""

    yf_symbol = get_yfinance_symbol(symbol, market)

    # Download data
    print(f"\n{'='*60}")
    print(f"Backtesting {symbol} ({market.upper()})")
    print(f"{'='*60}")

    df = yf.download(yf_symbol, start=start_date, end=end_date, progress=False)

    if df.empty:
        print(f"No data found for {symbol}")
        return None

    # Handle MultiIndex columns
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    # Calculate RSI
    rsi_period = params.get("rsi_period", 14)
    df['RSI'] = calculate_rsi(df['Close'], rsi_period)
    df = df.dropna()

    print(f"Period: {df.index[0].strftime('%Y-%m-%d')} ~ {df.index[-1].strftime('%Y-%m-%d')}")
    print(f"Trading days: {len(df)}")

    # Strategy parameters
    stop_loss_pct = params.get("stop_loss", -10)
    cooldown_days = params.get("cooldown_days", 1)
    avg_down_levels = params.get("avg_down_levels", [[30, 0.5], [25, 0.3], [20, 0.2]])
    sell_levels = params.get("sell_levels", [[65, 0.3], [70, 0.3], [75, 0.4]])

    # State tracking
    trades: List[Trade] = []
    capital = initial_capital
    position_shares = 0
    position_cost = 0.0
    buy_stage = 0
    sell_stage = 0
    last_buy_date = None

    for date, row in df.iterrows():
        rsi = row['RSI']
        price = row['Close']

        # Check cooldown reset
        if last_buy_date is not None:
            days_since_buy = (date - last_buy_date).days
            if days_since_buy >= cooldown_days:
                buy_stage = 0

        # Check stop loss
        if position_shares > 0:
            avg_price = position_cost / position_shares
            pnl_pct = (price - avg_price) / avg_price * 100

            if pnl_pct <= stop_loss_pct:
                # Stop loss triggered - sell all
                sell_value = position_shares * price
                capital += sell_value

                trade = Trade(
                    symbol=symbol,
                    buy_date=last_buy_date,
                    buy_price=avg_price,
                    buy_stage=buy_stage,
                    portion=1.0,
                    shares=position_shares,
                    sell_date=date,
                    sell_price=price,
                    sell_reason="stop_loss",
                    pnl_pct=pnl_pct,
                )
                trades.append(trade)
                print(f"[STOP] {date.strftime('%Y-%m-%d')} | {price:,.0f} | RSI:{rsi:.1f} | PnL:{pnl_pct:+.1f}%")

                position_shares = 0
                position_cost = 0.0
                buy_stage = 0
                sell_stage = 0
                continue

        # Check staged selling
        if position_shares > 0:
            for stage_idx, (rsi_threshold, portion) in enumerate(sell_levels):
                if sell_stage > stage_idx:
                    continue

                if rsi >= rsi_threshold:
                    shares_to_sell = int(position_shares * portion)
                    if shares_to_sell == 0 and stage_idx == len(sell_levels) - 1:
                        shares_to_sell = position_shares  # Sell remaining

                    if shares_to_sell > 0:
                        sell_value = shares_to_sell * price
                        capital += sell_value

                        avg_price = position_cost / position_shares
                        pnl_pct = (price - avg_price) / avg_price * 100

                        trade = Trade(
                            symbol=symbol,
                            buy_date=last_buy_date,
                            buy_price=avg_price,
                            buy_stage=buy_stage,
                            portion=portion,
                            shares=shares_to_sell,
                            sell_date=date,
                            sell_price=price,
                            sell_reason=f"sell_stage_{stage_idx+1}",
                            pnl_pct=pnl_pct,
                        )
                        trades.append(trade)
                        print(f"[SELL{stage_idx+1}] {date.strftime('%Y-%m-%d')} | {price:,.0f} | RSI:{rsi:.1f} | {portion*100:.0f}% | PnL:{pnl_pct:+.1f}%")

                        position_cost -= avg_price * shares_to_sell
                        position_shares -= shares_to_sell
                        sell_stage = stage_idx + 1

                        if position_shares == 0:
                            buy_stage = 0
                            sell_stage = 0
                            position_cost = 0.0
                    break

        # Check averaging down buy signals
        for stage_idx, (rsi_threshold, portion) in enumerate(avg_down_levels):
            if buy_stage > stage_idx:
                continue

            if rsi <= rsi_threshold:
                buy_amount = capital * portion  # 남은 현금 기준으로 변경
                shares_to_buy = int(buy_amount / price)

                if shares_to_buy > 0 and capital >= shares_to_buy * price:
                    cost = shares_to_buy * price
                    capital -= cost
                    position_shares += shares_to_buy
                    position_cost += cost
                    buy_stage = stage_idx + 1
                    sell_stage = 0  # Reset sell stage on buy
                    last_buy_date = date

                    print(f"[BUY{stage_idx+1}] {date.strftime('%Y-%m-%d')} | {price:,.0f} | RSI:{rsi:.1f} | {portion*100:.0f}% | Shares:{shares_to_buy}")
                break

    # Close open position at end
    if position_shares > 0:
        last_price = df.iloc[-1]['Close']
        sell_value = position_shares * last_price
        capital += sell_value

        avg_price = position_cost / position_shares
        pnl_pct = (last_price - avg_price) / avg_price * 100

        trade = Trade(
            symbol=symbol,
            buy_date=last_buy_date,
            buy_price=avg_price,
            buy_stage=buy_stage,
            portion=1.0,
            shares=position_shares,
            sell_date=df.index[-1],
            sell_price=last_price,
            sell_reason="end_of_period",
            pnl_pct=pnl_pct,
        )
        trades.append(trade)
        print(f"[CLOSE] {df.index[-1].strftime('%Y-%m-%d')} | {last_price:,.0f} | PnL:{pnl_pct:+.1f}%")

    # Calculate metrics
    total_return = (capital - initial_capital) / initial_capital * 100
    buy_hold_return = (df.iloc[-1]['Close'] - df.iloc[0]['Close']) / df.iloc[0]['Close'] * 100

    winning_trades = [t for t in trades if t.pnl_pct and t.pnl_pct > 0]
    losing_trades = [t for t in trades if t.pnl_pct and t.pnl_pct <= 0]
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
    # Load config
    config = Config("config")
    config.load()

    # Backtest period: 2020-2023 (COVID crash + 2022 bear market)
    start_date = datetime(2020, 1, 1)
    end_date = datetime(2023, 1, 1)

    print("="*60)
    print("RSI MEAN REVERSION STRATEGY BACKTEST")
    print(f"Period: {start_date.strftime('%Y-%m-%d')} ~ {end_date.strftime('%Y-%m-%d')}")
    print("="*60)

    all_results = []

    # Run backtest for each enabled strategy
    for strategy_config in config.strategies:
        if not strategy_config.get("enabled"):
            continue

        market = strategy_config.get("market", "krx")
        symbols = strategy_config.get("symbols", [])
        params = strategy_config.get("params", {})

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
    print("SUMMARY")
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
