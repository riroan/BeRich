#!/usr/bin/env python3
"""Backtest engine — the strategy-agnostic half of a simulation.

Mirrors the live architecture (src/strategy/base.py + the order manager):

    live                          backtest
    ─────────────────────────     ────────────────────────────────
    BaseStrategy                  BacktestStrategy
      .initialize(bars)             .prepare(df)
      .calculate_signal()           .decide(date, row)
      .on_fill(fill)                .on_fill(signal, ...)
      Signal(strength=portion)      BacktestSignal(portion=...)
    OrderManager                  run_simulation()
      sizing / fills / logs         sizing / fills / trade records

A strategy decides *what* to do with the position; the engine owns the
money. `decide()` is a generator so a bar can produce several actions in
order — the engine applies each one before resuming it, which is why the
strategy sees the position its previous action produced (the live loop
gets the same thing from get_position()).
"""

import asyncio
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Optional

import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.types import Market

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


@dataclass
class Position:
    """The engine's live position. A strategy reads it, never writes it."""
    shares: int = 0
    cost: float = 0.0
    # When the current position was last added to, and at what stage. Trade
    # records carry these as the entry side, so they are engine facts rather
    # than strategy state.
    entry_date: datetime = None
    entry_stage: int = 0

    @property
    def avg_price(self) -> float:
        return self.cost / self.shares if self.shares else 0.0

    def pnl_pct(self, price: float) -> float:
        avg = self.avg_price
        return (price - avg) / avg * 100 if avg else 0.0


@dataclass
class BacktestSignal:
    """One action for the current bar — the backtest twin of Signal."""
    side: str                       # "buy" | "sell"
    portion: float                  # of remaining weight room / of holdings
    reason: str
    stage: int = 0
    # A PnL-ladder exit spends its rung even when the portion rounds down to
    # zero shares, so the trade is still recorded; a staged sell is not.
    # Preserved from the original loop rather than normalized.
    record_empty_fill: bool = False


class BacktestStrategy(ABC):
    """Bar-by-bar decision maker. Subclass per strategy, register in
    scripts/backtest_registry.py."""

    #: stable id used by the API and the registry ("rsi", "ha", ...)
    key: str = ""

    #: whether the request's RSI ladder fields apply. The API validates them
    #: only for strategies that read them, so a strategy with nothing to tune
    #: is never rejected over settings it ignores. Inherited, so specialising
    #: an RSI strategy keeps the validation.
    uses_rsi_params: bool = False

    def __init__(self, params: dict | None = None):
        self.params = params or {}
        self.pos = Position()

    def bind(self, position: Position) -> None:
        """Engine hands over the position it will keep updating."""
        self.pos = position

    @classmethod
    def params_from_request(cls, body) -> dict:
        """Pull this strategy's params off an API request body.

        Each strategy owns which request fields it reads, so a strategy with
        nothing to tune (the default) never sees — or gets validated
        against — another one's settings.
        """
        return {}

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable strategy name"""

    @abstractmethod
    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add indicator columns and drop warm-up rows. Called once."""

    @abstractmethod
    def decide(self, date, row) -> Iterator[BacktestSignal]:
        """Yield this bar's actions in order.

        The engine applies each yielded signal (and calls on_fill) before
        resuming the generator, so `self.pos` is current after every yield.
        """

    def on_fill(
        self,
        signal: BacktestSignal,
        date,
        price: float,
        filled: int,
        shares_before: int,
    ) -> None:
        """Advance strategy state on a fill (mirrors the live on_fill
        contract). Not called for a signal the engine dropped — a buy that
        could not afford one share, or a sell that rounded to zero shares
        without record_empty_fill."""

    def indicator_series(self, df: pd.DataFrame) -> list[float]:
        """Values for the chart's indicator panel. Empty = no panel."""
        return []


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
        "ohlc": [],
        "rsi_values": [],
        "buy_trades": [],
        "sell_trades": [],
    }


def _ohlc_payload(df: pd.DataFrame) -> list[list[float]]:
    """[open, high, low, close] per bar, or [] if the frame has no OHLC.

    Both data sources (KIS DB, yfinance) carry OHLC, but a caller can pass a
    close-only frame — the chart falls back to the line in that case.
    """
    cols = ("Open", "High", "Low", "Close")
    if not all(c in df.columns for c in cols):
        return []
    return [
        [round(float(v), 4) for v in row]
        for row in df[list(cols)].itertuples(index=False, name=None)
    ]


def _stage_from_reason(reason: str) -> int:
    if reason and reason.startswith("sell_stage_"):
        return int(reason.split("_")[-1])
    return 0  # stop_loss / take_profit / end_of_period — frontend reads `reason`


def run_simulation(
    df: pd.DataFrame,
    symbol: str,
    strategy: BacktestStrategy,
    initial_capital: float = 10_000_000,
    verbose: bool = False,
) -> dict:
    """Run `strategy` over a price DataFrame and account for the money.

    df must have a `Close` column and a DatetimeIndex.
    Returns the full result dict including timeseries for chart rendering.
    Pure function — no I/O, no prints unless verbose=True.
    """
    df = strategy.prepare(df.copy())
    if df.empty:
        return _empty_result(symbol, initial_capital)

    pos = Position()
    strategy.bind(pos)

    capital = initial_capital
    trades: list[Trade] = []
    buy_events: list[dict] = []   # every BUY at the moment it fires

    for date, row in df.iterrows():
        price = row["Close"]

        for signal in strategy.decide(date, row):
            if signal.side == "buy":
                # Match the live sizing: buy_amount =
                # (max_symbol_value − current_value) × portion. Single-symbol
                # backtest → max_symbol_value = initial_capital, marked to market.
                current_value = pos.shares * price
                remaining_room = max(initial_capital - current_value, 0)
                buy_amount = min(remaining_room * signal.portion, capital)
                shares_to_buy = int(buy_amount / price)
                if shares_to_buy <= 0 or capital < shares_to_buy * price:
                    continue

                cost = shares_to_buy * price
                shares_before = pos.shares
                capital -= cost
                pos.shares += shares_to_buy
                pos.cost += cost
                pos.entry_date = date
                pos.entry_stage = signal.stage
                buy_events.append({
                    "date": date.strftime("%Y-%m-%d"),
                    "price": float(price),
                    "stage": signal.stage,
                })
                if verbose:
                    print(
                        f"[BUY{signal.stage}] {date.strftime('%Y-%m-%d')} | "
                        f"{price:,.0f} | {signal.portion * 100:.0f}% | "
                        f"Shares:{shares_to_buy}"
                    )
                strategy.on_fill(signal, date, price, shares_to_buy, shares_before)
                continue

            # sell
            if pos.shares <= 0:
                continue
            avg_price = pos.avg_price
            pnl_pct = pos.pnl_pct(price)
            shares_to_sell = int(pos.shares * signal.portion)
            if shares_to_sell == 0 and pos.shares > 0 and signal.portion > 0:
                shares_to_sell = 1
            if shares_to_sell <= 0 and not signal.record_empty_fill:
                continue

            shares_before = pos.shares
            capital += shares_to_sell * price
            pos.cost -= avg_price * shares_to_sell
            pos.shares -= shares_to_sell
            if pos.shares == 0:
                pos.cost = 0.0
            trades.append(Trade(
                symbol=symbol,
                buy_date=pos.entry_date,
                buy_price=avg_price,
                buy_stage=pos.entry_stage,
                portion=signal.portion,
                shares=shares_to_sell,
                sell_date=date,
                sell_price=price,
                sell_reason=signal.reason,
                pnl_pct=pnl_pct,
            ))
            if verbose:
                print(
                    f"[SELL {signal.reason}] {date.strftime('%Y-%m-%d')} | "
                    f"{price:,.0f} | {signal.portion * 100:.0f}% | "
                    f"PnL:{pnl_pct:+.1f}%"
                )
            strategy.on_fill(signal, date, price, shares_to_sell, shares_before)

    # Close whatever is still open at the last bar.
    if pos.shares > 0:
        last_price = df.iloc[-1]["Close"]
        avg_price = pos.avg_price
        pnl_pct = pos.pnl_pct(last_price)
        capital += pos.shares * last_price
        trades.append(Trade(
            symbol=symbol,
            buy_date=pos.entry_date,
            buy_price=avg_price,
            buy_stage=pos.entry_stage,
            portion=1.0,
            shares=pos.shares,
            sell_date=df.index[-1],
            sell_price=last_price,
            sell_reason="end_of_period",
            pnl_pct=pnl_pct,
        ))
        if verbose:
            print(
                f"[CLOSE] {df.index[-1].strftime('%Y-%m-%d')} | "
                f"{last_price:,.0f} | PnL:{pnl_pct:+.1f}%"
            )

    total_return = (capital - initial_capital) / initial_capital * 100
    first_close, last_close = df.iloc[0]["Close"], df.iloc[-1]["Close"]
    buy_hold_return = (last_close - first_close) / first_close * 100
    winning = [t for t in trades if t.pnl_pct and t.pnl_pct > 0]
    win_rate = len(winning) / len(trades) * 100 if trades else 0

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
        # [open, high, low, close] per bar, aligned with `dates`. Lets the
        # chart draw candles (and Heikin-Ashi) instead of just the close line.
        "ohlc": _ohlc_payload(df),
        # Indicator panel: RSI for the RSI strategy, empty for one that has
        # no oscillator (the frontend hides the panel then).
        "rsi_values": strategy.indicator_series(df),
        "buy_trades": buy_events,
        "sell_trades": sell_trades_payload,
    }


def get_yfinance_symbol(symbol: str, market: str) -> str:
    """Convert symbol to yfinance format"""
    if market == "krx":
        return f"{symbol}.KS"
    return symbol


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


async def run_symbol_async(
    symbol: str,
    market: str,
    start_date: str,
    end_date: str,
    strategy: BacktestStrategy,
    storage: "Storage",
    initial_capital: float = 10_000_000,
) -> tuple[Optional[dict], Optional[str]]:
    """Web async entry. KIS DB first, yfinance fallback. Quiet (no prints).

    Returns (result, error_code) where exactly one is non-None.
    error_code ∈ {"ticker_not_found", "data_source_timeout"} on failure.
    """
    df, source = await load_price_history(
        symbol, market, start_date, end_date, storage,
    )
    if source == "none":
        return None, "ticker_not_found"
    if source == "timeout":
        return None, "data_source_timeout"
    result = run_simulation(df, symbol, strategy, initial_capital, verbose=False)
    result["market"] = market
    result["data_source"] = source
    result["strategy"] = strategy.key
    return result, None
