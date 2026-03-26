#!/usr/bin/env python3
"""Stop Loss Strategy Comparison Backtest: Fixed vs Trailing"""

import yfinance as yf
import pandas as pd
from datetime import datetime
from dataclasses import dataclass
from typing import List
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


@dataclass
class Trade:
    """Trade record"""
    symbol: str
    buy_date: datetime
    buy_price: float
    sell_date: datetime = None
    sell_price: float = None
    sell_reason: str = None
    pnl_pct: float = None
    max_profit_pct: float = None  # For trailing stop analysis


def calculate_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    """Calculate RSI indicator"""
    delta = prices.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss.replace(0, 1e-10)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def backtest_with_stop_loss(
    df: pd.DataFrame,
    symbol: str,
    initial_capital: float,
    stop_loss_pct: float,  # Fixed stop loss (e.g., -10)
    trailing_stop_pct: float = None,  # Trailing stop (e.g., -5 from peak)
    params: dict = None,
) -> dict:
    """Backtest with specified stop loss strategy"""

    params = params or {}
    avg_down_levels = params.get("avg_down_levels", [[35, 0.3], [30, 0.35], [25, 0.35]])
    sell_levels = params.get("sell_levels", [[70, 0.25], [75, 0.35], [80, 0.4]])
    cooldown_days = params.get("cooldown_days", 3)

    trades: List[Trade] = []
    capital = initial_capital
    position_shares = 0
    position_cost = 0.0
    buy_stage = 0
    sell_stage = 0
    last_buy_date = None
    max_price_since_buy = 0.0  # For trailing stop

    for date, row in df.iterrows():
        rsi = row['RSI']
        price = row['Close']

        # Check cooldown reset
        if last_buy_date is not None:
            days_since_buy = (date - last_buy_date).days
            if days_since_buy >= cooldown_days:
                buy_stage = 0

        # Update max price for trailing stop
        if position_shares > 0:
            if price > max_price_since_buy:
                max_price_since_buy = price

        # Check stop loss
        if position_shares > 0:
            avg_price = position_cost / position_shares
            pnl_pct = (price - avg_price) / avg_price * 100
            max_profit_pct = (max_price_since_buy - avg_price) / avg_price * 100

            triggered = False
            reason = ""

            # Fixed stop loss check
            if pnl_pct <= stop_loss_pct:
                triggered = True
                reason = "fixed_stop"

            # Trailing stop check (only if in profit and trailing is enabled)
            if trailing_stop_pct is not None and max_profit_pct > 0:
                drawdown_from_peak = (price - max_price_since_buy) / max_price_since_buy * 100
                if drawdown_from_peak <= trailing_stop_pct:
                    triggered = True
                    reason = "trailing_stop"

            if triggered:
                sell_value = position_shares * price
                capital += sell_value

                trade = Trade(
                    symbol=symbol,
                    buy_date=last_buy_date,
                    buy_price=avg_price,
                    sell_date=date,
                    sell_price=price,
                    sell_reason=reason,
                    pnl_pct=pnl_pct,
                    max_profit_pct=max_profit_pct,
                )
                trades.append(trade)

                position_shares = 0
                position_cost = 0.0
                buy_stage = 0
                sell_stage = 0
                max_price_since_buy = 0.0
                continue

        # Check staged selling (RSI based)
        if position_shares > 0:
            for stage_idx, (rsi_threshold, portion) in enumerate(sell_levels):
                if sell_stage > stage_idx:
                    continue

                if rsi >= rsi_threshold:
                    shares_to_sell = int(position_shares * portion)
                    if shares_to_sell == 0 and stage_idx == len(sell_levels) - 1:
                        shares_to_sell = position_shares

                    if shares_to_sell > 0:
                        sell_value = shares_to_sell * price
                        capital += sell_value

                        avg_price = position_cost / position_shares
                        pnl_pct = (price - avg_price) / avg_price * 100
                        max_profit_pct = (max_price_since_buy - avg_price) / avg_price * 100 if max_price_since_buy > 0 else 0

                        trade = Trade(
                            symbol=symbol,
                            buy_date=last_buy_date,
                            buy_price=avg_price,
                            sell_date=date,
                            sell_price=price,
                            sell_reason=f"rsi_sell_{stage_idx+1}",
                            pnl_pct=pnl_pct,
                            max_profit_pct=max_profit_pct,
                        )
                        trades.append(trade)

                        position_cost -= avg_price * shares_to_sell
                        position_shares -= shares_to_sell
                        sell_stage = stage_idx + 1

                        if position_shares == 0:
                            buy_stage = 0
                            sell_stage = 0
                            position_cost = 0.0
                            max_price_since_buy = 0.0
                    break

        # Check buy signals
        for stage_idx, (rsi_threshold, portion) in enumerate(avg_down_levels):
            if buy_stage > stage_idx:
                continue

            if rsi <= rsi_threshold:
                buy_amount = capital * portion
                shares_to_buy = int(buy_amount / price)

                if shares_to_buy > 0 and capital >= shares_to_buy * price:
                    cost = shares_to_buy * price
                    capital -= cost
                    position_shares += shares_to_buy
                    position_cost += cost
                    buy_stage = stage_idx + 1
                    sell_stage = 0
                    last_buy_date = date
                    max_price_since_buy = price
                break

    # Close open position at end
    if position_shares > 0:
        last_price = df.iloc[-1]['Close']
        sell_value = position_shares * last_price
        capital += sell_value

        avg_price = position_cost / position_shares
        pnl_pct = (last_price - avg_price) / avg_price * 100
        max_profit_pct = (max_price_since_buy - avg_price) / avg_price * 100 if max_price_since_buy > 0 else 0

        trade = Trade(
            symbol=symbol,
            buy_date=last_buy_date,
            buy_price=avg_price,
            sell_date=df.index[-1],
            sell_price=last_price,
            sell_reason="end_of_period",
            pnl_pct=pnl_pct,
            max_profit_pct=max_profit_pct,
        )
        trades.append(trade)

    # Calculate metrics
    total_return = (capital - initial_capital) / initial_capital * 100
    winning_trades = [t for t in trades if t.pnl_pct and t.pnl_pct > 0]
    stop_loss_trades = [t for t in trades if t.sell_reason in ("fixed_stop", "trailing_stop")]

    return {
        "total_return_pct": total_return,
        "final_capital": capital,
        "num_trades": len(trades),
        "win_rate": len(winning_trades) / len(trades) * 100 if trades else 0,
        "stop_loss_count": len(stop_loss_trades),
        "trades": trades,
    }


def main():
    # Test symbols (value stocks that work well with RSI strategy)
    symbols = ["KO", "PEP", "JNJ", "VZ", "O", "SPY", "AAPL", "GOOG"]

    # Backtest period
    start_date = "2020-01-01"
    end_date = "2024-01-01"

    initial_capital = 5_000_000  # 500만원

    print("=" * 80)
    print("STOP LOSS STRATEGY COMPARISON")
    print(f"Period: {start_date} ~ {end_date}")
    print(f"Initial Capital: {initial_capital:,} KRW per symbol")
    print("=" * 80)

    # Test configurations
    configs = [
        {"name": "Fixed -10%", "stop_loss": -10, "trailing": None},
        {"name": "Fixed -15%", "stop_loss": -15, "trailing": None},
        {"name": "Fixed -10% + Trail -5%", "stop_loss": -10, "trailing": -5},
        {"name": "Fixed -10% + Trail -7%", "stop_loss": -10, "trailing": -7},
        {"name": "Fixed -10% + Trail -10%", "stop_loss": -10, "trailing": -10},
        {"name": "Fixed -15% + Trail -5%", "stop_loss": -15, "trailing": -5},
    ]

    results_by_config = {c["name"]: [] for c in configs}

    params = {
        "avg_down_levels": [[35, 0.3], [30, 0.35], [25, 0.35]],
        "sell_levels": [[70, 0.25], [75, 0.35], [80, 0.4]],
        "cooldown_days": 3,
    }

    for symbol in symbols:
        print(f"\n{'='*60}")
        print(f"Testing {symbol}")
        print("=" * 60)

        # Download data
        df = yf.download(symbol, start=start_date, end=end_date, progress=False)
        if df.empty:
            print(f"No data for {symbol}")
            continue

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df['RSI'] = calculate_rsi(df['Close'], 14)
        df = df.dropna()

        buy_hold_return = (df.iloc[-1]['Close'] - df.iloc[0]['Close']) / df.iloc[0]['Close'] * 100

        print(f"Buy & Hold Return: {buy_hold_return:+.2f}%")
        print()

        for config in configs:
            result = backtest_with_stop_loss(
                df=df,
                symbol=symbol,
                initial_capital=initial_capital,
                stop_loss_pct=config["stop_loss"],
                trailing_stop_pct=config["trailing"],
                params=params,
            )
            result["symbol"] = symbol
            result["buy_hold"] = buy_hold_return
            results_by_config[config["name"]].append(result)

            print(f"  {config['name']:<25} | Return: {result['total_return_pct']:>+7.2f}% | "
                  f"Trades: {result['num_trades']:>3} | Win: {result['win_rate']:>5.1f}% | "
                  f"StopLoss: {result['stop_loss_count']:>2}")

    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY BY STRATEGY")
    print("=" * 80)
    print(f"{'Strategy':<30} {'Avg Return':>12} {'vs B&H':>10} {'Avg Trades':>12} {'Avg Win%':>10} {'StopLoss':>10}")
    print("-" * 80)

    best_strategy = None
    best_return = -999

    for config_name, results in results_by_config.items():
        if not results:
            continue

        avg_return = sum(r["total_return_pct"] for r in results) / len(results)
        avg_bh = sum(r["buy_hold"] for r in results) / len(results)
        avg_trades = sum(r["num_trades"] for r in results) / len(results)
        avg_win = sum(r["win_rate"] for r in results) / len(results)
        avg_stop = sum(r["stop_loss_count"] for r in results) / len(results)

        vs_bh = avg_return - avg_bh

        print(f"{config_name:<30} {avg_return:>+11.2f}% {vs_bh:>+9.2f}%p {avg_trades:>11.1f} {avg_win:>9.1f}% {avg_stop:>9.1f}")

        if avg_return > best_return:
            best_return = avg_return
            best_strategy = config_name

    print("-" * 80)
    print(f"\nBEST STRATEGY: {best_strategy} (Avg Return: {best_return:+.2f}%)")

    # Detailed stop loss analysis
    print("\n" + "=" * 80)
    print("STOP LOSS TRIGGER ANALYSIS")
    print("=" * 80)

    for config_name, results in results_by_config.items():
        all_trades = []
        for r in results:
            all_trades.extend(r["trades"])

        stop_trades = [t for t in all_trades if t.sell_reason in ("fixed_stop", "trailing_stop")]
        if stop_trades:
            avg_loss = sum(t.pnl_pct for t in stop_trades) / len(stop_trades)
            avg_max_profit = sum(t.max_profit_pct for t in stop_trades if t.max_profit_pct) / len(stop_trades) if stop_trades else 0

            fixed_count = len([t for t in stop_trades if t.sell_reason == "fixed_stop"])
            trail_count = len([t for t in stop_trades if t.sell_reason == "trailing_stop"])

            print(f"{config_name:<30} | Total: {len(stop_trades):>3} | Fixed: {fixed_count:>3} | Trail: {trail_count:>3} | "
                  f"Avg Loss: {avg_loss:>+6.1f}% | Avg Peak Profit: {avg_max_profit:>+6.1f}%")


if __name__ == "__main__":
    main()
