"""Web dashboard for trading bot monitoring"""

import asyncio
import hmac
import secrets
import os
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from typing import Any
from pathlib import Path

from fastapi import Depends, FastAPI, Request, Form, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, model_validator
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
import secure
import logging
import json
import re

from src.analytics.tax import capital_gains_tax_by_year, usdkrw_rates

logger = logging.getLogger(__name__)

# Base directory for templates and static files
BASE_DIR = Path(__file__).parent


class ConnectionManager:
    """WebSocket connection manager"""

    def __init__(self):
        self.active_connections: list[WebSocket] = []
        self.loop: asyncio.AbstractEventLoop | None = None

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        if self.loop is None:
            self.loop = asyncio.get_running_loop()
        logger.info(f"WebSocket connected. Total: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        logger.info(f"WebSocket disconnected. Total: {len(self.active_connections)}")

    async def broadcast(self, message: dict):
        """Broadcast message to all connected clients.

        run_bot.py runs the web server in its own thread with its own
        event loop, separate from the bot's loop that calls this via
        broadcast_update(). WebSocket sends are bound to the loop that
        accepted the connection, so awaiting them directly from the bot's
        loop raises "Future attached to a different loop" — silently
        caught below, which prunes every connection as "disconnected"
        after the first tick and the dashboard never updates again. Hop
        the actual send over to the connections' own loop.
        """
        if not self.active_connections:
            return

        if self.loop is not None and self.loop is not asyncio.get_running_loop():
            fut = asyncio.run_coroutine_threadsafe(self._broadcast(message), self.loop)
            await asyncio.wrap_future(fut)
            return

        await self._broadcast(message)

    async def _broadcast(self, message: dict):
        message_json = json.dumps(message, default=str)
        disconnected = []

        for connection in self.active_connections:
            try:
                await connection.send_text(message_json)
            except Exception:
                disconnected.append(connection)

        # Clean up disconnected clients
        for conn in disconnected:
            self.disconnect(conn)


# Global WebSocket manager
ws_manager = ConnectionManager()


class SignalCandidate(BaseModel):
    """Signal candidate for upcoming trades"""
    symbol: str
    market: str
    signal_type: str  # buy_candidate, sell_candidate, stop_loss_alert
    rsi: float
    threshold: float
    distance: float  # how far from threshold
    current_price: float
    reason: str


class PositionInfo(BaseModel):
    symbol: str
    market: str
    quantity: int
    avg_price: float
    current_price: float
    pnl: float
    pnl_pct: float
    rsi: float | None = None
    # Strategy-specific info
    buy_stage: int = 0
    sell_stage: int = 0
    tp_stage: int = 0
    sl_stage: int = 0
    max_buy_stages: int = 3
    max_sell_stages: int = 3
    stage_cooldown_days: int = 0
    last_buy_date: str | None = None
    last_sell_date: str | None = None
    buy_stage_reset_remaining: str | None = None
    sell_stage_reset_remaining: str | None = None
    stop_loss_pct: float = -10.0
    stop_loss_distance: float = 0.0  # how far from stop loss


class TradeLog(BaseModel):
    """Trade/order log entry"""
    timestamp: str
    symbol: str
    market: str
    action: str  # buy, sell, partial_sell, stop_loss
    price: float
    quantity: int
    rsi: float | None = None
    trigger_rule: str  # what triggered this trade
    result: str  # success, failed, pending
    pnl: float | None = None
    pnl_pct: float | None = None


class SystemStatus(BaseModel):
    """System status info"""
    auto_trading_enabled: bool = True
    last_strategy_run: str | None = None
    last_price_update: str | None = None
    api_connected: bool = True
    account_tradable: bool = True
    data_collection_ok: bool = True
    error_message: str | None = None


class PerformanceMetrics(BaseModel):
    """Performance analysis metrics"""
    total_return_pct: float = 0.0
    total_return_usd: float = 0.0
    cagr: float = 0.0
    mdd: float = 0.0
    win_rate: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    avg_profit: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float | None = None
    sharpe_ratio: float = 0.0
    total_pnl: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0


class MarketStatus(BaseModel):
    """Market overview status"""
    market_rsi: float | None = None
    oversold_count: int = 0
    overbought_count: int = 0
    total_symbols: int = 0
    market_state: str = "neutral"  # oversold, neutral, overbought


class BotStatus(BaseModel):
    running: bool
    paper_trading: bool
    warmup_remaining: str | None = None
    strategies: list[str]
    uptime: str


class PricePoint(BaseModel):
    time: str  # ISO format or timestamp
    open: float
    high: float
    low: float
    close: float
    volume: int = 0


class BacktestRequest(BaseModel):
    """Backtest parameters. The RSI fields are ignored when strategy='ha',
    which takes none — the flip rule has nothing to tune."""
    strategy: str = Field("rsi", pattern="^(rsi|ha|rsi_ha)$")
    symbol: str = Field(..., min_length=1, max_length=20)
    market: str = "krx"
    start_date: str  # "YYYY-MM-DD"
    end_date: str    # "YYYY-MM-DD"
    # Range-checked below, only when strategy == "rsi" — a Field-level bound
    # would reject HA requests for values HA never reads.
    rsi_period: int = 14
    rsi_method: str = "wilder"
    stop_loss: float = -10.0
    take_profit: float | None = None
    cooldown_days: int = 1
    reset_requires_recovery: bool = False
    recovery_rsi: float = 50.0
    initial_capital: float = Field(10_000_000, gt=0, le=1e12)
    # [[rsi_threshold, portion], ...] — 3 stages each
    buy_levels: list[list[float]] = [[30, 0.5], [25, 0.3], [20, 0.2]]
    sell_levels: list[list[float]] = [[65, 0.3], [70, 0.3], [75, 0.4]]

    @model_validator(mode="after")
    def _validate(self):
        try:
            start = datetime.fromisoformat(self.start_date)
            end = datetime.fromisoformat(self.end_date)
        except ValueError as e:
            raise ValueError(f"date_range_invalid: {e}")
        if end <= start:
            raise ValueError("date_range_invalid: end_date must be after start_date")
        if (end - start).days > 365 * 5:
            raise ValueError("date_range_invalid: range exceeds 5 years")
        # Everything below is an RSI setting. A strategy that does not read
        # them must not be rejected for their shape — the HA flip rule has
        # nothing to tune. The flag is inherited, so a strategy specialising
        # the RSI one keeps the checks.
        from scripts.backtest_registry import backtest_class

        if not backtest_class(self.strategy).uses_rsi_params:
            return self
        if not (5 <= self.rsi_period <= 30):
            raise ValueError("rsi_period must be between 5 and 30")
        if self.rsi_method not in ("wilder", "cutler"):
            raise ValueError("rsi_method must be 'wilder' or 'cutler'")
        if not (-100.0 <= self.stop_loss <= -1.0):
            raise ValueError("stop_loss must be between -100 and -1")
        if self.take_profit is not None and not (1.0 <= self.take_profit <= 1000.0):
            raise ValueError("take_profit must be between 1 and 1000")
        if not (1 <= self.cooldown_days <= 30):
            raise ValueError("cooldown_days must be between 1 and 30")
        if not (0.0 <= self.recovery_rsi <= 100.0):
            raise ValueError("recovery_rsi must be between 0 and 100")
        for name, levels in (("buy_levels", self.buy_levels), ("sell_levels", self.sell_levels)):
            if not levels:
                raise ValueError(f"level_invalid: {name} cannot be empty")
            for lvl in levels:
                if not (0 <= lvl[1] <= 1.001):
                    raise ValueError(f"level_invalid: {name} portion {lvl[1]:.3f} must be in [0, 1]")
        return self


def _equity_usd_value(point: dict[str, Any]) -> float:
    from src.analytics.drawdown import equity_usd_value
    return equity_usd_value(point)


def _parse_flow_ts(value: Any) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return ts.replace(tzinfo=None)


def flow_adjusted_series(
    points: list[dict[str, Any]],
    flows: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], list[float]]:
    """Augment equity points with deposit-neutral values and a TWR index.

    Deposits/withdrawals recorded as 'adjustment' cash flows are stripped from
    the interval returns (time-weighted return), so external money movement
    never shows up as performance. 'initial' flows only register the starting
    principal, which is already baked into the equity values, so they never
    adjust returns. Each returned point gains ``deposit_adjusted_usd`` (USD
    value minus in-window net deposits) and ``twr_index`` (chained return
    index starting at 100). Also returns the list of interval returns.
    """
    series = []
    for point in points:
        value = _equity_usd_value(point)
        ts = _parse_flow_ts(point.get("timestamp"))
        if value > 0 and ts is not None:
            series.append((ts, value, point))

    events = []
    for flow in flows or []:
        if flow.get("flow_type") != "adjustment":
            continue
        ts = _parse_flow_ts(flow.get("timestamp"))
        amount = float(flow.get("amount_usd") or 0)
        if ts is not None and amount:
            events.append((ts, amount))
    events.sort(key=lambda e: e[0])

    augmented: list[dict[str, Any]] = []
    returns: list[float] = []
    index = 100.0
    cum_flow = 0.0
    prev_value: float | None = None
    ei = 0
    for ts, value, point in series:
        interval_flow = 0.0
        while ei < len(events) and events[ei][0] <= ts:
            interval_flow += events[ei][1]
            ei += 1
        if prev_value is not None:
            # Flows at/before the first point are already part of its value.
            r = (value - interval_flow - prev_value) / prev_value
            returns.append(r)
            index *= 1 + r
            cum_flow += interval_flow
        augmented.append({
            **point,
            "deposit_adjusted_usd": value - cum_flow,
            "twr_index": index,
        })
        prev_value = value

    return augmented, returns


class DashboardState:
    """Shared state for dashboard data"""

    def __init__(self):
        # Core data
        self.positions: dict[str, PositionInfo] = {}
        self.rsi_values: dict[str, float] = {}
        self.rsi_prices: dict[str, dict[str, Any]] = {}  # symbol -> {price, market}
        self.recent_signals: list[dict[str, Any]] = []
        self.recent_orders: list[dict[str, Any]] = []
        self.bot_status: BotStatus | None = None

        # Balance info - separate by currency
        self.account_value: Decimal = Decimal("0")
        self.balance_krw: Decimal = Decimal("0")
        self.balance_usd: Decimal = Decimal("0")
        self.cash_krw: Decimal = Decimal("0")
        self.cash_usd: Decimal = Decimal("0")
        self.pnl_krw: Decimal = Decimal("0")
        self.pnl_usd: Decimal = Decimal("0")
        self.daily_pnl: Decimal = Decimal("0")
        self.total_pnl: Decimal = Decimal("0")

        # Timestamps
        self.last_update: datetime | None = None
        self.last_strategy_run: datetime | None = None
        self.last_price_update: datetime | None = None

        # Price/RSI history for charts
        self.price_history: dict[str, list[PricePoint]] = {}
        self.rsi_history: dict[str, list[dict[str, Any]]] = {}

        # Trade logs (extended from recent_orders)
        self.trade_logs: list[TradeLog] = []

        # Signal candidates
        self.signal_candidates: list[SignalCandidate] = []
        # symbol -> {"buy_1", "buy_2", "sell_1"}: the live RSI strategy's
        # ladder levels, pushed by the bot. Empty until the bot syncs, so
        # update_signal_candidates() falls back to the code defaults.
        self.rsi_thresholds: dict[str, dict[str, float]] = {}

        # System status
        self.system_status: SystemStatus = SystemStatus()

        # Performance metrics
        self.performance: PerformanceMetrics = PerformanceMetrics()

        # Market status
        self.market_status_krx: MarketStatus = MarketStatus()
        self.market_status_us: MarketStatus = MarketStatus()

        # Risk alerts
        self.risk_alerts: list[dict[str, Any]] = []

        # Strategy internal state (synced from strategy)
        self.strategy_state: dict[str, dict[str, Any]] = {}

        # Trade points for chart markers
        self.trade_points: dict[str, list[dict[str, Any]]] = {}

        # Equity history for equity curve chart
        self.equity_history: list[dict[str, Any]] = []

        # Fills for performance calculation
        self.fills: list[dict[str, Any]] = []

        # External cash flows (principal ledger) for deposit-neutral metrics
        self.cash_flows: list[dict[str, Any]] = []

        # Storage reference (set by bot on init - NOT usable from web thread)
        self.storage = None

        # Database URL for web-local storage
        self.db_url: str | None = None

        # Strategy names from config
        self.strategy_names: list[str] = []

        # KIS API config for symbol validation
        self.kis_config: dict[str, Any] | None = None

        # KIS auth object (shared from bot's broker). A live reference, NOT a
        # token snapshot — KIS tokens expire in 24h and the broker refreshes
        # its own; a copied string goes stale and never recovers.
        self.kis_auth: Any | None = None

        # Live strategy instances (set by bot)
        self.strategy_instances: list[Any] | None = None

        # Hot reload callback, and the bot loop it has to run on (set by bot)
        self.reload_callback: Any | None = None
        self.bot_loop: asyncio.AbstractEventLoop | None = None

        # Trading pause flag (data collection continues)
        self.trading_paused: bool = False
        self.debug_freeze: bool = False

    def update_position(
        self,
        symbol: str,
        market: str,
        quantity: int,
        avg_price: float,
        current_price: float,
        rsi: float | None = None,
        buy_stage: int = 0,
        sell_stage: int = 0,
        tp_stage: int = 0,
        sl_stage: int = 0,
        max_buy_stages: int = 3,
        max_sell_stages: int = 3,
        stage_cooldown_days: int = 0,
        last_buy_date: str | None = None,
        last_sell_date: str | None = None,
        stop_loss_pct: float = -10.0,
    ):
        pnl = (current_price - avg_price) * quantity
        pnl_pct = ((current_price - avg_price) / avg_price * 100) if avg_price else 0
        stop_loss_distance = pnl_pct - stop_loss_pct  # how far from stop loss

        self.positions[symbol] = PositionInfo(
            symbol=symbol,
            market=market,
            quantity=quantity,
            avg_price=avg_price,
            current_price=current_price,
            pnl=pnl,
            pnl_pct=pnl_pct,
            rsi=rsi,
            buy_stage=buy_stage,
            sell_stage=sell_stage,
            tp_stage=tp_stage,
            sl_stage=sl_stage,
            max_buy_stages=max_buy_stages,
            max_sell_stages=max_sell_stages,
            stage_cooldown_days=stage_cooldown_days,
            last_buy_date=last_buy_date,
            last_sell_date=last_sell_date,
            buy_stage_reset_remaining=self._stage_reset_remaining(
                buy_stage, last_buy_date, stage_cooldown_days,
            ),
            sell_stage_reset_remaining=self._stage_reset_remaining(
                sell_stage, last_sell_date, stage_cooldown_days,
            ),
            stop_loss_pct=stop_loss_pct,
            stop_loss_distance=stop_loss_distance,
        )
        self.last_update = datetime.now()

    @staticmethod
    def _stage_reset_remaining(
        stage: int,
        last_date: str | None,
        cooldown_days: int,
    ) -> str | None:
        if stage <= 0:
            return None
        if not last_date or cooldown_days <= 0:
            return "Ready"
        try:
            started = datetime.fromisoformat(last_date.replace("Z", "+00:00"))
        except ValueError:
            return None

        now = datetime.now(started.tzinfo) if started.tzinfo else datetime.now()
        remaining = started + timedelta(days=cooldown_days) - now
        if remaining.total_seconds() <= 0:
            return "Ready"

        total_minutes = int((remaining.total_seconds() + 59) // 60)
        days, minutes = divmod(total_minutes, 24 * 60)
        hours, minutes = divmod(minutes, 60)
        if days > 0:
            return f"{days}d {hours}h"
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"

    @staticmethod
    def _position_from_record(record: dict[str, Any] | PositionInfo) -> PositionInfo:
        if isinstance(record, PositionInfo):
            return record

        avg_price = float(record["avg_price"])
        current_price = float(record.get("current_price", avg_price))
        pnl = float(
            record.get(
                "pnl",
                (current_price - avg_price) * int(record["quantity"]),
            )
        )
        pnl_pct = float(
            record.get(
                "pnl_pct",
                ((current_price - avg_price) / avg_price * 100)
                if avg_price else 0,
            )
        )
        stop_loss_pct = float(record.get("stop_loss_pct", -10.0))
        buy_stage = int(record.get("buy_stage", 0))
        sell_stage = int(record.get("sell_stage", 0))
        stage_cooldown_days = int(record.get("stage_cooldown_days", 0))
        last_buy_date = record.get("last_buy_date")
        last_sell_date = record.get("last_sell_date")

        return PositionInfo(
            symbol=str(record["symbol"]).upper(),
            market=str(record["market"]).upper(),
            quantity=int(record["quantity"]),
            avg_price=avg_price,
            current_price=current_price,
            pnl=pnl,
            pnl_pct=pnl_pct,
            rsi=(
                float(record["rsi"])
                if record.get("rsi") is not None else None
            ),
            buy_stage=buy_stage,
            sell_stage=sell_stage,
            tp_stage=int(record.get("tp_stage", 0)),
            sl_stage=int(record.get("sl_stage", 0)),
            max_buy_stages=int(record.get("max_buy_stages", 3)),
            max_sell_stages=int(record.get("max_sell_stages", 3)),
            stage_cooldown_days=stage_cooldown_days,
            last_buy_date=last_buy_date,
            last_sell_date=last_sell_date,
            buy_stage_reset_remaining=DashboardState._stage_reset_remaining(
                buy_stage, last_buy_date, stage_cooldown_days,
            ),
            sell_stage_reset_remaining=DashboardState._stage_reset_remaining(
                sell_stage, last_sell_date, stage_cooldown_days,
            ),
            stop_loss_pct=stop_loss_pct,
            stop_loss_distance=float(
                record.get("stop_loss_distance", pnl_pct - stop_loss_pct)
            ),
        )

    def replace_positions_from_records(
        self,
        records: list[dict[str, Any] | PositionInfo],
        market: str | None = None,
    ) -> None:
        positions = {
            position.symbol: position
            for position in (self._position_from_record(record) for record in records)
        }

        # `current_positions` is a holding-only table — it has no rsi column,
        # so records read back from it always carry rsi=None. Without this,
        # every /api/positions request replaced the tick's in-memory RSI with
        # nothing and the Positions table showed "-" while the RSI Monitor,
        # reading the same rsi_values, showed the number.
        for symbol, position in positions.items():
            if position.rsi is None and symbol in self.rsi_values:
                positions[symbol] = position.model_copy(
                    update={"rsi": self.rsi_values[symbol]},
                )

        # Same story for price, and for the same reason: the tick path is the
        # single source of truth for it (see the note in
        # dashboard_sync._update_market_positions about not mixing the balance
        # API's evaluation price into price_rsi). The page render rebuilds
        # price/PnL from the latest price_rsi row, while the bot's position
        # sweep carries the KIS balance API's evaluation price — which lags.
        # Both fed this dict, so the WebSocket pushed the lagging number over
        # the freshly rendered one and the P&L-sorted table reshuffled ~60s
        # after every refresh.
        for symbol, position in positions.items():
            tick = self.rsi_prices.get(symbol, {}).get("price")
            if tick is None or position.avg_price <= 0:
                continue
            tick = float(tick)
            if tick == position.current_price:
                continue
            pnl_pct = (tick - position.avg_price) / position.avg_price * 100
            positions[symbol] = position.model_copy(update={
                "current_price": tick,
                "pnl": (tick - position.avg_price) * position.quantity,
                "pnl_pct": pnl_pct,
                # Derived from pnl_pct, so it has to move with it.
                "stop_loss_distance": pnl_pct - position.stop_loss_pct,
            })

        if market is None:
            self.positions = positions
        else:
            market_upper = market.upper()
            self.positions = {
                symbol: position
                for symbol, position in self.positions.items()
                if position.market.upper() != market_upper
            }
            self.positions.update(positions)

        for position in positions.values():
            if position.rsi is not None:
                self.rsi_values[position.symbol] = position.rsi
            self.rsi_prices[position.symbol] = {
                "price": position.current_price,
                "market": position.market,
            }

        self.last_update = datetime.now()

    def update_rsi(self, symbol: str, rsi: float, price: float = None, market: str = None):
        self.rsi_values[symbol] = rsi
        if price is not None:
            self.rsi_prices[symbol] = {"price": price, "market": market}
        if symbol in self.positions:
            self.positions[symbol].rsi = rsi
        self.last_update = datetime.now()

    def set_rsi_thresholds(self, strategy_states: dict):
        """Cache each symbol's RSI ladder levels from the bot's strategy sync.

        Covers every symbol a strategy watches, not just held ones — buy
        candidates are mostly symbols with no position yet.
        """
        self.rsi_thresholds = {
            symbol: {
                k: state[k]
                for k in ("buy_1", "buy_2", "sell_1", "sell_2")
                if state.get(k) is not None
            }
            for symbol, state in strategy_states.items()
        }

    def rsi_levels(self, symbol: str) -> tuple[float, float, float, float]:
        """(buy_1, buy_2, sell_1, sell_2) for a symbol.

        Single source for both the candidate lists and the RSI colouring, so
        a symbol cannot be listed as a candidate without being coloured. The
        defaults match the bands that were hardcoded before the ladder became
        configurable — they apply until the bot syncs.
        """
        levels = self.rsi_thresholds.get(symbol, {})
        return (
            levels.get("buy_1", 30.0),
            levels.get("buy_2", 25.0),
            levels.get("sell_1", 70.0),
            levels.get("sell_2", 75.0),
        )

    def add_price_point(
        self,
        symbol: str,
        time: datetime,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: int = 0,
    ):
        """Add a price point to history"""
        if symbol not in self.price_history:
            self.price_history[symbol] = []

        point = PricePoint(
            time=f"{time:%Y-%m-%d %H:%M}",
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=volume,
        )
        self.price_history[symbol].append(point)

        # Keep only last 500 points
        if len(self.price_history[symbol]) > 500:
            self.price_history[symbol] = self.price_history[symbol][-500:]

    def add_rsi_point(self, symbol: str, time: datetime, rsi: float):
        """Add RSI point to history"""
        if symbol not in self.rsi_history:
            self.rsi_history[symbol] = []

        self.rsi_history[symbol].append({
            "time": f"{time:%Y-%m-%d %H:%M}",
            "value": rsi,
        })

        # Keep only last 500 points
        if len(self.rsi_history[symbol]) > 500:
            self.rsi_history[symbol] = self.rsi_history[symbol][-500:]

    def add_signal(self, signal_data: dict[str, Any]):
        self.recent_signals.insert(0, {
            **signal_data,
            "timestamp": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
        })
        # Keep only last 50 signals
        self.recent_signals = self.recent_signals[:50]

    def add_order(self, order_data: dict[str, Any]):
        self.recent_orders.insert(0, {
            **order_data,
            "timestamp": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
        })
        # Keep only last 50 orders
        self.recent_orders = self.recent_orders[:50]

    def add_trade_log(
        self,
        symbol: str,
        market: str,
        action: str,
        price: float,
        quantity: int,
        trigger_rule: str,
        result: str = "success",
        rsi: float | None = None,
        pnl: float | None = None,
        pnl_pct: float | None = None,
        timestamp: datetime | str | None = None,
    ):
        """Add detailed trade log"""
        timestamp_value = timestamp or datetime.now()
        log_time = self._format_trade_timestamp(timestamp_value, "%Y-%m-%d %H:%M:%S")
        marker_time = self._format_trade_timestamp(timestamp_value, "%Y-%m-%d %H:%M")
        log = TradeLog(
            timestamp=log_time,
            symbol=symbol,
            market=market,
            action=action,
            price=price,
            quantity=quantity,
            rsi=rsi,
            trigger_rule=trigger_rule,
            result=result,
            pnl=pnl,
            pnl_pct=pnl_pct,
        )
        self.trade_logs.insert(0, log)
        # Keep only last 100 logs
        self.trade_logs = self.trade_logs[:100]

        # Also add to trade points for chart markers
        if symbol not in self.trade_points:
            self.trade_points[symbol] = []
        self.trade_points[symbol].append({
            "time": marker_time,
            "action": action,
            "price": price,
            "rsi": rsi,
        })
        # Keep only last 50 points per symbol
        if len(self.trade_points[symbol]) > 50:
            self.trade_points[symbol] = self.trade_points[symbol][-50:]

    @staticmethod
    def _format_trade_timestamp(timestamp: datetime | str, fmt: str) -> str:
        if isinstance(timestamp, datetime):
            return timestamp.strftime(fmt)

        value = timestamp.strip()
        if not value:
            return datetime.now().strftime(fmt)

        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime(fmt)
        except ValueError:
            if fmt == "%Y-%m-%d %H:%M" and len(value) >= 16:
                return value[:16]
            return value

    def update_signal_candidates(self):
        """Update list of signal candidates based on RSI values"""
        candidates = []

        for symbol, rsi in self.rsi_values.items():
            position = self.positions.get(symbol)
            market = position.market if position else "Unknown"
            current_price = position.current_price if position else 0

            buy_1, buy_2, sell_1, _ = self.rsi_levels(symbol)

            # Buy candidates by oversold severity. Bands are mutually
            # exclusive so a symbol appears at most once: RSI <= buy_1 used to
            # match BOTH the <=buy_1+5 and <=buy_1 branches, and the render
            # filter (`"buy" in signal_type`) caught both → duplicate rows in
            # the Buy Candidate list. Deep oversold takes precedence.
            if rsi <= buy_1:
                threshold = buy_2
                distance = rsi - threshold
                candidates.append(SignalCandidate(
                    symbol=symbol,
                    market=market,
                    signal_type="buy_candidate_2",
                    rsi=rsi,
                    threshold=threshold,
                    distance=distance,
                    current_price=current_price,
                    reason=f"RSI {rsi:.1f} deep oversold candidate",
                ))
            elif rsi <= buy_1 + 5:
                threshold = buy_1
                distance = rsi - threshold
                candidates.append(SignalCandidate(
                    symbol=symbol,
                    market=market,
                    signal_type="buy_candidate",
                    rsi=rsi,
                    threshold=threshold,
                    distance=distance,
                    current_price=current_price,
                    reason=f"RSI {rsi:.1f} approaching oversold",
                ))

            # Sell candidates: RSI approaching the first sell rung
            if rsi >= sell_1 - 5 and position and position.quantity > 0:
                threshold = sell_1
                distance = rsi - threshold
                candidates.append(SignalCandidate(
                    symbol=symbol,
                    market=market,
                    signal_type="sell_candidate",
                    rsi=rsi,
                    threshold=threshold,
                    distance=distance,
                    current_price=current_price,
                    reason=f"RSI {rsi:.1f} approaching overbought",
                ))

            # Stop loss alert
            if position and position.quantity > 0:
                if position.stop_loss_distance <= 2:  # within 2% of stop loss
                    candidates.append(SignalCandidate(
                        symbol=symbol,
                        market=market,
                        signal_type="stop_loss_alert",
                        rsi=rsi,
                        threshold=position.stop_loss_pct,
                        distance=position.stop_loss_distance,
                        current_price=current_price,
                        reason=f"PnL {position.pnl_pct:.1f}% near stop loss {position.stop_loss_pct}%",
                    ))

        self.signal_candidates = sorted(candidates, key=lambda x: abs(x.distance))

    def update_market_status(self):
        """Update market status overview"""
        krx_rsis = []
        us_rsis = []

        for symbol, rsi in self.rsi_values.items():
            if (position := self.positions.get(symbol)):
                if position.market == "KRX":
                    krx_rsis.append(rsi)
                else:
                    us_rsis.append(rsi)

        # KRX market
        if krx_rsis:
            avg_rsi = sum(krx_rsis) / len(krx_rsis)
            oversold = sum(1 for r in krx_rsis if r <= 30)
            overbought = sum(1 for r in krx_rsis if r >= 70)
            state = "oversold" if avg_rsi < 40 else ("overbought" if avg_rsi > 60 else "neutral")
            self.market_status_krx = MarketStatus(
                market_rsi=avg_rsi,
                oversold_count=oversold,
                overbought_count=overbought,
                total_symbols=len(krx_rsis),
                market_state=state,
            )

        # US market
        if us_rsis:
            avg_rsi = sum(us_rsis) / len(us_rsis)
            oversold = sum(1 for r in us_rsis if r <= 30)
            overbought = sum(1 for r in us_rsis if r >= 70)
            state = "oversold" if avg_rsi < 40 else ("overbought" if avg_rsi > 60 else "neutral")
            self.market_status_us = MarketStatus(
                market_rsi=avg_rsi,
                oversold_count=oversold,
                overbought_count=overbought,
                total_symbols=len(us_rsis),
                market_state=state,
            )

    def update_risk_alerts(self):
        """Update risk alerts"""
        alerts = []

        for symbol, position in self.positions.items():
            # Stop loss imminent
            if position.stop_loss_distance <= 2:
                alerts.append({
                    "type": "stop_loss_imminent",
                    "symbol": symbol,
                    "message": f"{symbol}: {position.pnl_pct:.1f}% (stop loss at {position.stop_loss_pct}%)",
                    "severity": "high",
                })

            # Large position warning (if position value > 20% of total)
            # This would need total portfolio value calculation

            # Consecutive losses would need trade history analysis

        self.risk_alerts = alerts

    def set_bot_status(
        self,
        running: bool,
        paper_trading: bool,
        strategies: list[str],
        uptime: str,
        warmup_remaining: str | None = None,
    ):
        self.bot_status = BotStatus(
            running=running,
            paper_trading=paper_trading,
            strategies=strategies,
            uptime=uptime,
            warmup_remaining=warmup_remaining,
        )
        self.strategy_names = strategies

    def update_system_status(
        self,
        auto_trading: bool = True,
        api_connected: bool = True,
        account_tradable: bool = True,
        data_ok: bool = True,
        error: str | None = None,
    ):
        self.system_status = SystemStatus(
            auto_trading_enabled=auto_trading,
            last_strategy_run=f"{self.last_strategy_run:%Y-%m-%d %H:%M:%S}" if self.last_strategy_run else None,
            last_price_update=f"{self.last_price_update:%Y-%m-%d %H:%M:%S}" if self.last_price_update else None,
            api_connected=api_connected,
            account_tradable=account_tradable,
            data_collection_ok=data_ok,
            error_message=error,
        )

    def calculate_performance(self):
        """Calculate performance metrics from equity history and fills"""
        import math

        self.performance = PerformanceMetrics()

        # Calculate from equity history (deposit-neutral TWR series)
        series, returns = flow_adjusted_series(
            self.equity_history, self.cash_flows
        )
        if len(series) >= 2:
            initial = series[0]
            current = series[-1]
            final_index = current["twr_index"]

            # Total return is measured against principal, not the TWR index.
            # TWR only strips a deposit out if the deposit's date is known,
            # and deposits are registered by editing 원금 — which records the
            # new total, not when the money arrived. Against principal only
            # the total matters, so this stays correct with undated deposits.
            principal = sum(f["amount_usd"] for f in self.cash_flows)
            if principal > 0:
                gain = _equity_usd_value(current) - principal
                self.performance.total_return_usd = gain
                self.performance.total_return_pct = gain / principal * 100.0

            # Calculate MDD (Maximum Drawdown) on the TWR index
            peak = 0
            max_drawdown = 0
            for point in series:
                value = point["twr_index"]
                if value > peak:
                    peak = value
                if peak > 0:
                    drawdown = (peak - value) / peak * 100
                    if drawdown > max_drawdown:
                        max_drawdown = drawdown
            self.performance.mdd = max_drawdown

            # Calculate CAGR
            first_time = datetime.fromisoformat(initial.get("timestamp", ""))
            last_time = datetime.fromisoformat(current.get("timestamp", ""))
            days = (last_time - first_time).days
            if days > 0:
                years = days / 365.0
                if years >= 0.01 and final_index > 0:
                    cagr = (
                        (pow(final_index / 100.0, 1 / years) - 1) * 100
                    )
                    self.performance.cagr = max(min(cagr, 9999.99), -9999.99)

            # Calculate Sharpe Ratio (simplified - daily returns)
            if len(series) > 2:
                if returns:
                    avg_return = sum(returns) / len(returns)
                    variance = sum((r - avg_return) ** 2 for r in returns) / len(returns)
                    std_dev = math.sqrt(variance) if variance > 0 else 0
                    if std_dev > 0:
                        # Annualized Sharpe (assuming ~252 trading days)
                        sharpe = avg_return / std_dev * math.sqrt(252)
                        self.performance.sharpe_ratio = max(min(sharpe, 99.99), -99.99)

        # Calculate from fills/trades
        if self.fills:
            sell_trades = [
                f for f in self.fills
                if str(f.get("side") or "").lower() == "sell"
            ]
            pnls = [f.get("pnl", 0) or 0 for f in sell_trades if f.get("pnl") is not None]

            if pnls:
                self.performance.total_trades = len(pnls)
                self.performance.total_pnl = sum(pnls)

                winning = [p for p in pnls if p > 0]
                losing = [p for p in pnls if p < 0]

                self.performance.winning_trades = len(winning)
                self.performance.losing_trades = len(losing)

                if self.performance.total_trades > 0:
                    self.performance.win_rate = (
                        len(winning) / self.performance.total_trades * 100
                    )

                if winning:
                    self.performance.avg_profit = sum(winning) / len(winning)
                    self.performance.best_trade = max(winning)

                if losing:
                    self.performance.avg_loss = sum(losing) / len(losing)
                    self.performance.worst_trade = min(losing)

                # Profit Factor
                gross_profit = sum(winning) if winning else 0
                gross_loss = abs(sum(losing)) if losing else 0
                if gross_loss > 0:
                    self.performance.profit_factor = gross_profit / gross_loss


# Global dashboard state
dashboard_state = DashboardState()


def _trigger_bot_reload() -> bool:
    """Hot-reload the bot's strategies. Returns whether the bot was reachable.

    The reload has to run on the BOT's event loop. This web app lives in its
    own thread with its own loop, and reloading refetches daily bars through
    the broker, whose aiohttp session is bound to the bot's loop — driving it
    from here fails mid-fetch ("attached to a different loop"), leaving every
    strategy with an empty RSI base and the bot silently unable to trade.
    """
    cb = dashboard_state.reload_callback
    loop = dashboard_state.bot_loop
    if cb is None or loop is None:
        return False

    async def _reload():
        try:
            await cb()
        except Exception as e:
            logger.error(f"Strategy reload failed: {e}")

    asyncio.run_coroutine_threadsafe(_reload(), loop)
    return True


# Global templates (created once)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _usd_signed(
    value: float | None, decimals: int = 2, truncate: bool = False,
) -> str:
    """Signed USD with the sign outside the symbol: +$1,234.56 / -$1,234.56.

    Writing this inline as ``${{ "{:+,.2f}".format(x) }}`` puts the sign
    INSIDE, rendering "$-1,234.56". Six templates had drifted into that,
    each disagreeing with the live WebSocket update that overwrote it a
    second later. Registered as a filter so there is one copy to be wrong.

    truncate=True cuts the magnitude at `decimals` places instead of
    rounding (main-page P&L wants the raw digits, not a rounded one).
    Goes through Decimal(str(...)) rather than float math so a value
    that's conceptually exact at the cut point (e.g. 743.50) can't drop
    a cent from float representation error (743.499999999...).
    """
    if value is None:
        return "-"
    sign = "+" if value > 0 else ("-" if value < 0 else "")
    magnitude = abs(value)
    if truncate:
        quantum = Decimal(1).scaleb(-decimals)
        magnitude = float(
            Decimal(str(magnitude)).quantize(quantum, rounding=ROUND_DOWN)
        )
    return f"{sign}${magnitude:,.{decimals}f}"


templates.env.filters["usd_signed"] = _usd_signed


def _asset_version(name: str) -> int:
    """Cache-buster derived from the file's mtime.

    Hand-bumping ?v= in base.html was forgotten on style.css edits, so the
    service worker (cache-first on /static/) kept serving stale CSS until a
    manual reload. Deriving it from mtime makes that impossible to forget.
    """
    try:
        return int((BASE_DIR / "static" / name).stat().st_mtime)
    except OSError:
        return 0


templates.env.globals["asset_v"] = _asset_version


def _avg_rsi() -> float | None:
    """Simple average of the live RSI values, for the header badge.

    Registered as a Jinja global (not passed per-route) so it shows up in
    _header.html on every page without every route wiring it through.
    """
    values = dashboard_state.rsi_values.values()
    return sum(values) / len(values) if values else None


templates.env.globals["avg_rsi"] = _avg_rsi


def _rsi_class(symbol: str, rsi: float | None) -> str:
    """CSS class for an RSI readout, on the same bands as the candidate lists.

    warning = the symbol is a buy/sell candidate; danger = it is already past
    the first rung and heading for the second. Registered as a Jinja global
    for the same reason as avg_rsi().
    """
    if rsi is None:
        return ""
    buy_1, buy_2, sell_1, sell_2 = dashboard_state.rsi_levels(symbol)
    if rsi <= buy_2 or rsi >= sell_2:
        return "rsi-danger"
    if rsi <= buy_1 + 5 or rsi >= sell_1 - 5:
        return "rsi-warning"
    return "rsi-neutral"


templates.env.globals["rsi_class"] = _rsi_class

# Session storage (in-memory)
valid_sessions: dict[str, datetime] = {}

# Mock mode: skip authentication entirely
MOCK_MODE: bool = False

# Rate limiter
limiter = Limiter(key_func=get_remote_address)

# Security headers
_csp = (
    secure.ContentSecurityPolicy()
    .default_src("'self'")
    .script_src("'self'", "'unsafe-inline'")
    .style_src("'self'", "'unsafe-inline'", "https://fonts.googleapis.com")
    .font_src("'self'", "https://fonts.gstatic.com")
    .img_src("'self'", "data:")
    .connect_src("'self'", "ws:", "wss:")
    .object_src("'none'")
)
secure_headers = secure.Secure(csp=_csp)


def _rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(status_code=429, content={"detail": "Too many requests"})


# Auth config from environment
AUTH_USERNAME = os.getenv("DASHBOARD_USERNAME", "admin")
AUTH_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
SESSION_COOKIE_NAME = "berich_session"
SESSION_EXPIRE_DAYS = 30


def _is_secure_request(request: Request) -> bool:
    """Return True when the original client request is HTTPS."""
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    return request.url.scheme == "https" or forwarded_proto.split(",", 1)[0].strip() == "https"


def _set_session_cookie(response: Response, request: Request, token: str) -> None:
    """Set the dashboard session cookie with HTTPS-only Secure semantics."""
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        max_age=SESSION_EXPIRE_DAYS * 86400,
        secure=_is_secure_request(request),
        samesite="lax",
    )


def generate_session_token() -> str:
    """Generate a secure session token"""
    return secrets.token_hex(32)


def verify_session(request: Request) -> bool:
    """Check if request has valid session"""
    if MOCK_MODE:
        return True

    if not AUTH_PASSWORD:
        return False

    if not (session_token := request.cookies.get(SESSION_COOKIE_NAME)):
        return False

    if session_token not in valid_sessions:
        return False

    last_seen = valid_sessions[session_token]
    if datetime.now() - last_seen > timedelta(days=SESSION_EXPIRE_DAYS):
        del valid_sessions[session_token]
        return False

    # Sliding expiry: refresh last-seen timestamp on activity
    valid_sessions[session_token] = datetime.now()
    return True


def require_auth(request: Request):
    """Dependency to require authentication"""
    if not verify_session(request):
        raise HTTPException(status_code=401, detail="Not authenticated")


def create_app() -> FastAPI:
    """Create FastAPI application"""
    if not AUTH_PASSWORD:
        raise RuntimeError(
            "DASHBOARD_PASSWORD must be set in .env. Refusing to start server."
        )

    _debug = os.getenv("DEBUG") == "true"
    app = FastAPI(
        title="BeRich Dashboard",
        version="1.0.0",
        docs_url="/docs" if _debug else None,
        redoc_url="/redoc" if _debug else None,
        openapi_url="/openapi.json" if _debug else None,
    )

    # Rate limiter + security headers
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)
    app.add_middleware(SlowAPIMiddleware)

    # Mount static files
    static_dir = BASE_DIR / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # PWA: serve service worker from root scope so it can control the whole site.
    # StaticFiles mounts at /static would scope the SW to /static/ only.
    @app.get("/sw.js", include_in_schema=False)
    async def service_worker():
        sw_path = static_dir / "sw.js"
        if not sw_path.exists():
            raise HTTPException(status_code=404)
        return FileResponse(
            sw_path,
            media_type="application/javascript",
            headers={
                "Service-Worker-Allowed": "/",
                "Cache-Control": "no-cache",
            },
        )

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest():
        m_path = static_dir / "manifest.webmanifest"
        if not m_path.exists():
            raise HTTPException(status_code=404)
        return FileResponse(m_path, media_type="application/manifest+json")

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        """Auth check for /api/ routes + security headers + sliding cookie."""
        if request.url.path.startswith("/api/"):
            if not verify_session(request):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Not authenticated"},
                )
        response = await call_next(request)
        secure_headers.set_headers(response)

        # Sliding cookie: re-issue max_age on authenticated requests so active
        # users never expire. Skip /login and /logout which manage the cookie
        # themselves.
        path = request.url.path
        if path not in ("/login", "/logout"):
            token = request.cookies.get(SESSION_COOKIE_NAME)
            if token and token in valid_sessions:
                _set_session_cookie(response, request, token)
        return response

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, error: str = ""):
        """Login page"""
        # If already logged in, redirect to home
        if verify_session(request):
            return RedirectResponse(url="/", status_code=302)

        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"request": request, "error": error},
        )

    @app.post("/login")
    @limiter.limit("5/minute")
    async def login(request: Request, username: str = Form(...), password: str = Form(...)):
        """Handle login"""
        if username == AUTH_USERNAME and hmac.compare_digest(password, AUTH_PASSWORD):
            # Create session
            token = generate_session_token()
            valid_sessions[token] = datetime.now()

            response = RedirectResponse(url="/", status_code=302)
            _set_session_cookie(response, request, token)
            return response
        else:
            return RedirectResponse(url="/login?error=Invalid credentials", status_code=302)

    @app.get("/logout")
    async def logout(request: Request):
        """Handle logout"""
        if (
            (session_token := request.cookies.get(SESSION_COOKIE_NAME))
            and session_token in valid_sessions
        ):
            del valid_sessions[session_token]

        response = RedirectResponse(url="/login", status_code=302)
        response.delete_cookie(SESSION_COOKIE_NAME)
        return response

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        """Main dashboard page"""
        if not verify_session(request):
            return RedirectResponse(url="/login", status_code=302)

        await _load_fills_for_web()
        positions = await _get_current_positions_for_web()
        # Default order: P&L ascending (client can re-sort by rsi/price/symbol)
        positions.sort(key=lambda p: p.pnl_pct)

        # Update derived states
        dashboard_state.update_signal_candidates()
        dashboard_state.update_market_status()
        dashboard_state.update_risk_alerts()

        # Calculate portfolio summary
        krw_positions = [p for p in positions if p.market == "KRX"]
        us_positions = [p for p in positions if p.market != "KRX"]

        total_krw_value = float(dashboard_state.balance_krw)
        total_usd_value = float(dashboard_state.balance_usd)
        cash_ratio_krw = (float(dashboard_state.cash_krw) / total_krw_value * 100) if total_krw_value > 0 else 0
        cash_ratio_usd = (float(dashboard_state.cash_usd) / total_usd_value * 100) if total_usd_value > 0 else 0

        # Separate buy/sell candidates
        buy_candidates = [c for c in dashboard_state.signal_candidates if "buy" in c.signal_type]
        sell_candidates = [c for c in dashboard_state.signal_candidates if c.signal_type == "sell_candidate"]
        stop_loss_alerts = [c for c in dashboard_state.signal_candidates if c.signal_type == "stop_loss_alert"]

        context = {
            "request": request,
            "active_page": "dashboard",
            # Portfolio summary
            "positions": positions,
            "krw_positions": krw_positions,
            "us_positions": us_positions,
            "position_count": len(positions),
            # Balance
            "balance_krw": float(dashboard_state.balance_krw),
            "balance_usd": float(dashboard_state.balance_usd),
            "cash_krw": float(dashboard_state.cash_krw),
            "cash_usd": float(dashboard_state.cash_usd),
            "cash_ratio_krw": cash_ratio_krw,
            "cash_ratio_usd": cash_ratio_usd,
            "pnl_krw": float(dashboard_state.pnl_krw),
            "pnl_usd": float(dashboard_state.pnl_usd),
            "daily_pnl": float(dashboard_state.daily_pnl),
            "total_pnl": float(dashboard_state.total_pnl),
            # RSI with price info
            "rsi_values": dict(dashboard_state.rsi_values),
            "rsi_with_prices": {
                symbol: {
                    "rsi": rsi,
                    "price": dashboard_state.rsi_prices.get(symbol, {}).get("price"),
                    "market": dashboard_state.rsi_prices.get(symbol, {}).get("market"),
                }
                # Default order: RSI ascending (client can re-sort by name/price)
                for symbol, rsi in sorted(
                    dashboard_state.rsi_values.items(), key=lambda kv: kv[1]
                )
            },
            # Signals and orders
            "recent_signals": list(dashboard_state.recent_signals[:20]),
            "recent_orders": list(dashboard_state.recent_orders[:20]),
            "trade_logs": [log.model_dump() for log in dashboard_state.trade_logs[:20]],
            # Signal candidates
            "buy_candidates": [c.model_dump() for c in buy_candidates[:10]],
            "sell_candidates": [c.model_dump() for c in sell_candidates[:10]],
            "stop_loss_alerts": [c.model_dump() for c in stop_loss_alerts],
            # Status
            "bot_status": dashboard_state.bot_status,
            "trading_paused": dashboard_state.trading_paused,
            "system_status": dashboard_state.system_status.model_dump(),
            "last_update": dashboard_state.last_update,
            # Market status
            "market_status_krx": dashboard_state.market_status_krx.model_dump(),
            "market_status_us": dashboard_state.market_status_us.model_dump(),
            # Risk
            "risk_alerts": dashboard_state.risk_alerts,
            # Performance
            "performance": dashboard_state.performance.model_dump(),
        }
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context=context,
        )

    @app.get("/trades", response_class=HTMLResponse)
    async def trades_page(request: Request):
        """Trade log page"""
        if not verify_session(request):
            return RedirectResponse(url="/login", status_code=302)

        await _load_fills_for_web()
        context = {
            "request": request,
            "active_page": "trades",
            "trade_logs": [log.model_dump() for log in dashboard_state.trade_logs],
            "bot_status": dashboard_state.bot_status,
            "trading_paused": dashboard_state.trading_paused,
            "last_update": dashboard_state.last_update,
            "pnl_usd": float(dashboard_state.pnl_usd),
        }
        return templates.TemplateResponse(
            request=request,
            name="trades.html",
            context=context,
        )

    @app.get("/performance", response_class=HTMLResponse)
    async def performance_page(request: Request):
        """Performance analysis page"""
        if not verify_session(request):
            return RedirectResponse(url="/login", status_code=302)

        await _load_fills_for_web()
        await _load_equity_history_for_web()
        # Recalculate performance metrics
        dashboard_state.calculate_performance()

        fx = await usdkrw_rates(dashboard_state.fills)

        context = {
            "request": request,
            "active_page": "performance",
            "performance": dashboard_state.performance.model_dump(),
            "principal_usd": sum(
                f["amount_usd"] for f in dashboard_state.cash_flows
            ),
            "trade_logs": [log.model_dump() for log in dashboard_state.trade_logs],
            "fills": dashboard_state.fills,
            "tax_years": capital_gains_tax_by_year(dashboard_state.fills, fx),
            "balance_usd": float(dashboard_state.balance_usd),
            "pnl_usd": float(dashboard_state.pnl_usd),
            "bot_status": dashboard_state.bot_status,
            "trading_paused": dashboard_state.trading_paused,
            "last_update": dashboard_state.last_update,
        }
        return templates.TemplateResponse(
            request=request,
            name="performance.html",
            context=context,
        )

    @app.get("/menu", response_class=HTMLResponse)
    async def menu_page(request: Request):
        """Mobile menu page"""
        if not verify_session(request):
            return RedirectResponse(url="/login", status_code=302)

        context = {
            "request": request,
            "active_page": "menu",
            "bot_status": dashboard_state.bot_status,
            "trading_paused": dashboard_state.trading_paused,
            "last_update": dashboard_state.last_update,
            "pnl_usd": float(dashboard_state.pnl_usd),
        }
        return templates.TemplateResponse(
            request=request,
            name="menu.html",
            context=context,
        )

    @app.get("/api/status")
    async def get_status():
        """Get current bot status"""
        return {
            "bot_status": dashboard_state.bot_status,
            "trading_paused": dashboard_state.trading_paused,
            "system_status": dashboard_state.system_status.model_dump(),
            "balance_krw": float(dashboard_state.balance_krw),
            "balance_usd": float(dashboard_state.balance_usd),
            "cash_krw": float(dashboard_state.cash_krw),
            "cash_usd": float(dashboard_state.cash_usd),
            "pnl_krw": float(dashboard_state.pnl_krw),
            "pnl_usd": float(dashboard_state.pnl_usd),
            "daily_pnl": float(dashboard_state.daily_pnl),
            "total_pnl": float(dashboard_state.total_pnl),
            "last_update": dashboard_state.last_update.isoformat()
            if dashboard_state.last_update
            else None,
        }

    @app.get("/api/positions")
    async def get_positions():
        """Get current positions"""
        positions = await _get_current_positions_for_web()
        return [p.model_dump() for p in positions]

    @app.get("/api/rsi")
    async def get_rsi():
        """Get RSI values"""
        return dashboard_state.rsi_values

    @app.get("/api/signals")
    async def get_signals():
        """Get recent signals"""
        return dashboard_state.recent_signals

    @app.get("/api/orders")
    async def get_orders():
        """Get recent orders"""
        return dashboard_state.recent_orders

    @app.get("/api/trade-logs")
    async def get_trade_logs(limit: int = 50):
        """Get trade logs"""
        await _load_fills_for_web()
        return [log.model_dump() for log in dashboard_state.trade_logs[:limit]]

    @app.get("/api/signal-candidates")
    async def get_signal_candidates():
        """Get signal candidates"""
        dashboard_state.update_signal_candidates()
        return [c.model_dump() for c in dashboard_state.signal_candidates]

    @app.get("/api/market-status")
    async def get_market_status():
        """Get market status"""
        dashboard_state.update_market_status()
        return {
            "krx": dashboard_state.market_status_krx.model_dump(),
            "us": dashboard_state.market_status_us.model_dump(),
        }

    @app.get("/api/risk-alerts")
    async def get_risk_alerts():
        """Get risk alerts"""
        dashboard_state.update_risk_alerts()
        return dashboard_state.risk_alerts

    @app.get("/symbol/{symbol}", response_class=HTMLResponse)
    async def symbol_detail(request: Request, symbol: str):
        """Symbol detail page with chart"""
        if not verify_session(request):
            return RedirectResponse(url="/login", status_code=302)

        symbol_upper = symbol.upper()
        await _load_fills_for_web()
        positions = await _get_current_positions_for_web()
        position = {p.symbol: p for p in positions}.get(symbol_upper)
        rsi = position.rsi if position and position.rsi is not None else (
            dashboard_state.rsi_values.get(symbol_upper)
        )
        trade_points = dashboard_state.trade_points.get(symbol_upper, [])

        # Get current price from rsi_prices (available even without position)
        price_info = dashboard_state.rsi_prices.get(symbol_upper, {})
        current_price = position.current_price if position else (
            price_info.get("price") if price_info else None
        )
        market = position.market if position else (
            price_info.get("market", "nasdaq") if price_info else None
        )

        # Get symbol-specific trade logs
        symbol_trades = [
            log.model_dump()
            for log in dashboard_state.trade_logs
            if log.symbol == symbol_upper
        ][:20]

        context = {
            "request": request,
            "active_page": "symbols",
            "symbol": symbol_upper,
            "position": position,
            "rsi": rsi,
            "current_price": current_price,
            "symbol_market": market,
            "trade_points": trade_points,
            "symbol_trades": symbol_trades,
            "bot_status": dashboard_state.bot_status,
            "trading_paused": dashboard_state.trading_paused,
            "last_update": dashboard_state.last_update,
            "pnl_usd": float(dashboard_state.pnl_usd),
        }
        return templates.TemplateResponse(
            request=request,
            name="symbol.html",
            context=context,
        )

    @app.get("/api/symbol/{symbol}/history")
    async def get_symbol_history(
        symbol: str,
        limit: int = 100,
        before: str | None = None,
    ):
        """Get price history for a symbol (from DB, falls back to memory).

        ``before`` is a "YYYY-MM-DD HH:MM" cursor (same format as response
        ``time`` fields). Records strictly older than the cursor are returned,
        enabling lazy-loading when the user scrolls back on the chart.
        """
        await _load_fills_for_web()
        trade_points = dashboard_state.trade_points.get(symbol, [])

        before_dt: datetime | None = None
        if before:
            try:
                before_dt = datetime.fromisoformat(before)
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid 'before' timestamp format",
                )

        storage = await _get_web_storage()
        if storage:
            try:
                history = await storage.get_price_rsi_history(
                    symbol, limit=limit, before=before_dt,
                )
                prices = []
                rsi_points = []
                for record in history:
                    # Skip records without RSI so price and RSI series stay
                    # index-aligned — chart sync uses logical bar indices.
                    if record["rsi"] is None:
                        continue
                    ts = f"{record['timestamp']:%Y-%m-%d %H:%M}"
                    price = record["price"]
                    prices.append({
                        "time": ts,
                        "open": price,
                        "high": price,
                        "low": price,
                        "close": price,
                        "volume": 0,
                    })
                    rsi_points.append({"time": ts, "value": record["rsi"]})
                return {
                    "symbol": symbol,
                    "prices": prices,
                    "rsi": rsi_points,
                    "trade_points": trade_points[-limit:],
                }
            finally:
                await storage.close()

        prices = dashboard_state.price_history.get(symbol, [])
        rsi = dashboard_state.rsi_history.get(symbol, [])
        if before:
            prices = [p for p in prices if p.time < before]
            rsi = [r for r in rsi if r["time"] < before]
        return {
            "symbol": symbol,
            "prices": [p.model_dump() for p in prices[-limit:]],
            "rsi": rsi[-limit:],
            "trade_points": trade_points[-limit:],
        }

    @app.get("/api/symbol/{symbol}/daily")
    async def get_symbol_daily(symbol: str, limit: int = 250):
        """Daily OHLC candles for a symbol, aggregated from per-tick history.

        Each day's last recorded tick is the close (and carries the day's
        RSI); open/high/low come from that day's intraday ticks. Same
        response shape as the history endpoint so the chart can swap
        series in place. ``time`` fields are "YYYY-MM-DD" business days.
        """
        await _load_fills_for_web()
        trade_points = dashboard_state.trade_points.get(symbol, [])

        storage = await _get_web_storage()
        if not storage:
            return {
                "symbol": symbol,
                "prices": [],
                "rsi": [],
                "trade_points": [],
            }
        try:
            rows = await storage.get_daily_ohlc_rsi(symbol, limit=limit)
        finally:
            await storage.close()

        prices = [
            {
                "time": r["day"],
                "open": r["open"],
                "high": r["high"],
                "low": r["low"],
                "close": r["close"],
            }
            for r in rows
        ]
        rsi_points = [{"time": r["day"], "value": r["rsi"]} for r in rows]
        return {
            "symbol": symbol,
            "prices": prices,
            "rsi": rsi_points,
            "trade_points": trade_points,
        }

    @app.get("/api/symbol/{symbol}")
    async def get_symbol_info(symbol: str):
        """Get symbol info"""
        symbol_upper = symbol.upper()
        positions = await _get_current_positions_for_web()
        position = {p.symbol: p for p in positions}.get(symbol_upper)
        rsi = position.rsi if position and position.rsi is not None else (
            dashboard_state.rsi_values.get(symbol_upper)
        )

        return {
            "symbol": symbol_upper,
            "position": position.model_dump() if position else None,
            "rsi": rsi,
        }

    @app.get("/api/equity-history")
    async def get_equity_history(limit: int | None = None, before: str | None = None):
        """Get equity curve data from the DB.

        Reads from storage (full 90-day window) instead of the in-memory
        ``equity_history``, which is capped at the last 1000 snapshots and so
        truncated the curve to only the most recent ~3-4 weeks. Falls back to
        the in-memory cache if storage is unavailable.

        ``limit``+``before`` switch to cursor pagination (same convention as
        ``/api/symbol/{symbol}/history``) so the 분봉 chart can lazy-load
        instead of pulling the full window upfront. Called with no params,
        returns the full window — used by Monthly Returns, which needs the
        complete flow-adjusted chain to compute correctly.
        """
        before_dt: datetime | None = None
        if before:
            try:
                before_dt = datetime.fromisoformat(before)
            except ValueError:
                raise HTTPException(
                    status_code=400, detail="Invalid 'before' timestamp format",
                )

        storage = await _get_web_storage()
        if storage:
            try:
                points = await storage.get_equity_history(limit=limit, before=before_dt)
                flows = await storage.get_cash_flows()
                series, _ = flow_adjusted_series(points, flows)
                return {"data": series}
            except Exception as e:
                logger.warning(f"Equity history DB read failed: {e}")
            finally:
                await storage.close()
        series, _ = flow_adjusted_series(
            dashboard_state.equity_history, dashboard_state.cash_flows
        )
        return {"data": series}

    class PrincipalUpdate(BaseModel):
        principal_usd: float
        # When the money actually moved. Without it the flow is stamped with
        # the moment of the edit, so a deposit entered late reads as return
        # for the whole stretch between arrival and entry.
        occurred_on: date | None = None

    @app.get("/api/principal")
    async def get_principal():
        """Current principal (sum of the recorded cash flow ledger)"""
        flows = dashboard_state.cash_flows
        storage = await _get_web_storage()
        if storage:
            try:
                flows = await storage.get_cash_flows()
                dashboard_state.cash_flows = flows
            except Exception as e:
                logger.warning(f"Cash flow DB read failed: {e}")
            finally:
                await storage.close()
        return {
            "principal_usd": sum(f["amount_usd"] for f in flows),
            "flows": flows,
        }

    @app.post("/api/principal")
    async def update_principal(body: PrincipalUpdate):
        """Set current principal; the delta is recorded as a deposit/withdrawal.

        The first ever update registers the starting principal ('initial',
        excluded from return adjustment since it is already reflected in the
        equity history). Later updates store only the difference as an
        'adjustment' flow, which the TWR metrics strip out.

        Pass occurred_on to backfill a deposit that already happened; the
        flow is dated then instead of now, which is what the time-weighted
        metrics need to strip it out of the right interval. Backfilling
        several deposits means one call each, oldest first.
        """
        if body.principal_usd < 0:
            raise HTTPException(
                status_code=400, detail="Principal must be >= 0",
            )
        if body.occurred_on and body.occurred_on > date.today():
            raise HTTPException(
                status_code=400, detail="Date must not be in the future",
            )
        storage = await _get_web_storage()
        if not storage:
            raise HTTPException(
                status_code=503, detail="Storage not available",
            )
        occurred_at = (
            datetime.combine(body.occurred_on, datetime.min.time())
            if body.occurred_on else None
        )
        try:
            flows = await storage.get_cash_flows()
            current = sum(f["amount_usd"] for f in flows)
            delta = body.principal_usd - current
            if not flows:
                await storage.save_cash_flow(
                    Decimal(str(body.principal_usd)), flow_type="initial",
                    timestamp=occurred_at,
                )
            elif abs(delta) >= 0.01:
                await storage.save_cash_flow(
                    Decimal(str(round(delta, 2))), flow_type="adjustment",
                    timestamp=occurred_at,
                )
            flows = await storage.get_cash_flows()
        finally:
            await storage.close()

        dashboard_state.cash_flows = flows
        dashboard_state.calculate_performance()
        return {
            "success": True,
            "principal_usd": sum(f["amount_usd"] for f in flows),
        }

    # ==================== Backtest ====================

    @app.get("/backtest", response_class=HTMLResponse)
    async def backtest_page(request: Request):
        """Backtest UI — slider + price chart with buy/sell markers."""
        if not verify_session(request):
            return RedirectResponse(url="/login", status_code=302)
        return templates.TemplateResponse(
            request=request,
            name="backtest.html",
            context={"active_page": "backtest"},
        )

    @app.post("/api/backtest")
    @limiter.limit("10/minute")
    async def run_backtest(
        request: Request,
        body: BacktestRequest,
        _: None = Depends(require_auth),
    ):
        """Run a backtest. KIS DB first, yfinance fallback."""
        from scripts.backtest_engine import run_symbol_async
        from scripts.backtest_registry import build_from_request

        storage = await _get_web_storage()
        if storage is None:
            return JSONResponse({"error": "internal_error"}, status_code=500)

        try:
            # Each strategy pulls the fields it actually reads.
            strategy = build_from_request(body)
            result, err = await run_symbol_async(
                symbol=body.symbol,
                market=body.market,
                start_date=body.start_date,
                end_date=body.end_date,
                strategy=strategy,
                storage=storage,
                initial_capital=body.initial_capital,
            )
            if err is not None:
                # ticker_not_found / data_source_timeout → 422
                return JSONResponse({"error": err}, status_code=422)
            result["alpha_pct"] = round(
                result["total_return_pct"] - result["buy_hold_return_pct"], 4
            )
            # Strip trades (Trade dataclass) — frontend reads buy_trades/sell_trades payloads
            result.pop("trades", None)
            return result
        except ValueError as e:
            # Market.from_string raises ValueError for unknown markets
            logger.warning(f"Backtest validation error: {e}")
            return JSONResponse({"error": "market_invalid", "detail": str(e)}, status_code=422)
        except Exception as e:
            logger.error(f"Backtest error: {e}", exc_info=True)
            return JSONResponse({"error": "internal_error"}, status_code=500)
        finally:
            await storage.close()

    # ==================== Trading Control ====================

    @app.post("/api/trading/pause")
    async def pause_trading():
        """Pause trading (data collection continues)"""
        dashboard_state.trading_paused = True
        logger.info("Trading PAUSED by user")
        return {"paused": True}

    @app.post("/api/trading/resume")
    async def resume_trading():
        """Resume trading"""
        dashboard_state.trading_paused = False
        logger.info("Trading RESUMED by user")
        return {"paused": False}

    @app.get("/api/trading/status")
    async def trading_status():
        """Get trading pause status"""
        return {"paused": dashboard_state.trading_paused}

    # ==================== Debug: Seed Test Positions ====================

    @app.post("/api/debug/seed-positions")
    async def seed_test_positions():
        """Inject test positions into dashboard (dev only)"""
        if os.getenv("DEBUG") != "true":
            raise HTTPException(status_code=404, detail="Not found")
        import random
        dashboard_state.debug_freeze = True
        test_positions = [
            ("AAPL", "NASDAQ", 15, 245.30, 258.90),
            ("GOOG", "NASDAQ", 8, 158.20, 165.80),
            ("NVDA", "NASDAQ", 10, 170.50, 182.08),
            ("QQQ", "NASDAQ", 5, 460.00, 478.20),
            ("KO", "NYSE", 25, 74.50, 77.29),
            ("VZ", "NYSE", 30, 43.20, 41.80),
            ("XLE", "AMEX", 20, 84.00, 80.10),
            ("SOXX", "NASDAQ", 12, 355.00, 370.40),
            ("IAU", "AMEX", 40, 44.50, 47.85),
            ("SPY", "AMEX", 5, 505.00, 520.10),
        ]
        for symbol, market, qty, avg, curr in test_positions:
            rsi = random.uniform(25, 75)
            stage = random.randint(0, 2)
            dashboard_state.update_position(
                symbol=symbol, market=market, quantity=qty,
                avg_price=avg, current_price=curr, rsi=round(rsi, 1),
                buy_stage=stage, max_buy_stages=3,
            )
        # Seed RSI values near buy/sell thresholds to trigger signal candidates
        signal_rsi = [
            ("WMT", "NASDAQ", 127.26, 28.5),   # buy candidate
            ("JNJ", "NYSE", 152.30, 32.1),      # buy candidate
            ("O", "NYSE", 55.40, 33.2),          # buy candidate
            ("SOXX", "NASDAQ", 370.40, 74.5),    # sell candidate
            ("IAU", "AMEX", 47.85, 71.2),        # sell candidate
            ("SPY", "AMEX", 520.10, 76.8),       # sell candidate
        ]
        for symbol, market, price, rsi in signal_rsi:
            dashboard_state.update_rsi(symbol, rsi, price=price, market=market)

        dashboard_state.update_signal_candidates()
        dashboard_state.set_bot_status(
            running=True,
            paper_trading=True,
            strategies=["RSI Mean Reversion"],
            uptime="0d 1h 23m",
        )
        return {"seeded": len(test_positions), "signals": len(signal_rsi)}

    @app.post("/api/debug/seed-trades")
    async def seed_test_trades():
        """Inject sample trade logs into dashboard (dev only)."""
        if os.getenv("DEBUG") != "true":
            raise HTTPException(status_code=404, detail="Not found")

        dashboard_state.trade_logs = []
        dashboard_state.trade_points = {}
        now = datetime.now().replace(microsecond=0)
        sample_trades = [
            {
                "symbol": "IAU",
                "market": "AMEX",
                "action": "buy",
                "price": 82.90,
                "quantity": 2,
                "trigger_rule": "RSI <= 35",
                "rsi": 32.8,
                "timestamp": now - timedelta(minutes=58),
            },
            {
                "symbol": "AMZN",
                "market": "NASDAQ",
                "action": "buy",
                "price": 245.29,
                "quantity": 1,
                "trigger_rule": "RSI <= 30",
                "rsi": 29.7,
                "timestamp": now - timedelta(minutes=45),
            },
            {
                "symbol": "AXP",
                "market": "NYSE",
                "action": "partial_sell",
                "price": 337.91,
                "quantity": 1,
                "trigger_rule": "RSI >= 70",
                "rsi": 70.2,
                "pnl": 35.86,
                "pnl_pct": 11.9,
                "timestamp": now - timedelta(minutes=31),
            },
            {
                "symbol": "CVX",
                "market": "NYSE",
                "action": "buy",
                "price": 175.64,
                "quantity": 3,
                "trigger_rule": "Average down stage 1",
                "rsi": 34.1,
                "timestamp": now - timedelta(minutes=22),
            },
            {
                "symbol": "XLE",
                "market": "AMEX",
                "action": "stop_loss",
                "price": 51.25,
                "quantity": 4,
                "trigger_rule": "Stop loss -10%",
                "rsi": 41.5,
                "pnl": -13.80,
                "pnl_pct": -6.3,
                "timestamp": now - timedelta(minutes=11),
            },
            {
                "symbol": "GOOG",
                "market": "NASDAQ",
                "action": "sell",
                "price": 365.80,
                "quantity": 1,
                "trigger_rule": "RSI >= 65",
                "rsi": 66.4,
                "pnl": 10.27,
                "pnl_pct": 2.9,
                "timestamp": now - timedelta(minutes=3),
            },
        ]
        for trade in sample_trades:
            dashboard_state.add_trade_log(result="success", **trade)

        return {"seeded": len(sample_trades)}

    @app.post("/api/debug/seed-tax-fills")
    async def seed_tax_fills():
        """Write sample sell fills so the Capital Gains Tax table has rows.

        Unlike seed-trades these go to the DB, because /performance reloads
        fills from storage on every request and would drop in-memory ones.
        They carry a SEED- order_id so they can be taken out again:
        DELETE FROM fills WHERE order_id LIKE 'SEED-TAX-%';
        """
        if os.getenv("DEBUG") != "true":
            raise HTTPException(status_code=404, detail="Not found")

        storage = await _get_web_storage()
        if not storage:
            raise HTTPException(status_code=503, detail="Storage unavailable")

        from src.core.types import Fill, Market, OrderSide

        year = datetime.now().year
        # (symbol, market, years back, pnl) — one year under the deduction,
        # one over it, one a net loss, plus a KRX row the tax calc drops.
        samples = [
            ("AAPL", Market.NASDAQ, 0, "1500.00"),
            ("TSLA", Market.NASDAQ, 0, "-300.00"),
            ("NVDA", Market.NASDAQ, 1, "5000.00"),
            ("MSFT", Market.NASDAQ, 1, "-500.00"),
            ("005930", Market.KRX, 1, "9999.00"),
            ("GOOG", Market.NASDAQ, 2, "-800.00"),
        ]
        try:
            for i, (symbol, market, years_back, pnl) in enumerate(samples):
                await storage.save_fill(Fill(
                    order_id=f"SEED-TAX-{i}",
                    symbol=symbol,
                    market=market,
                    side=OrderSide.SELL,
                    quantity=1,
                    price=Decimal("100"),
                    commission=Decimal("0"),
                    pnl=Decimal(pnl),
                    timestamp=datetime(year - years_back, 6, 1, 12, 0),
                ))
        finally:
            await storage.close()

        return {"seeded": len(samples)}

    # ==================== Symbol Management Routes ====================

    class WatchedSymbolCreate(BaseModel):
        symbol: str
        market: str
        strategy_name: str
        max_weight: float = 20.0

    async def _get_web_storage():
        """Get a storage instance for web requests (own event loop)"""
        if not dashboard_state.db_url:
            return None
        from src.data.storage import Storage
        storage = Storage(dashboard_state.db_url)
        await storage.initialize()
        return storage

    async def _get_current_positions_for_web() -> list[PositionInfo]:
        """Read current positions from DB, falling back to in-memory state."""
        storage = await _get_web_storage()
        if not storage:
            return list(dashboard_state.positions.values())

        try:
            records = await storage.get_current_positions()
            dashboard_state.replace_positions_from_records(records)
            return list(dashboard_state.positions.values())
        except Exception as e:
            logger.warning(f"Failed to load current positions for web: {e}")
            return list(dashboard_state.positions.values())
        finally:
            await storage.close()

    async def _load_equity_history_for_web() -> None:
        """Read persisted equity history so performance metrics use the same
        90-day DB window as the equity curve (in-memory history is empty in
        standalone web mode and capped at 1000 points under the bot)."""
        storage = await _get_web_storage()
        if not storage:
            return
        try:
            dashboard_state.equity_history = await storage.get_equity_history()
        except Exception as e:
            logger.warning(f"Failed to load equity history for web: {e}")
        finally:
            await storage.close()

    async def _load_fills_for_web() -> None:
        """Read persisted fills into dashboard state for standalone web views."""
        storage = await _get_web_storage()
        if not storage:
            return

        try:
            try:
                dashboard_state.cash_flows = await storage.get_cash_flows()
            except Exception as e:
                logger.warning(f"Failed to load cash flows for web: {e}")
            fills = await storage.get_all_fills()
        except Exception as e:
            logger.warning(f"Failed to load fills for web: {e}")
            return
        finally:
            await storage.close()

        from src.core.types import trade_action

        def _enum_value(value) -> str:
            if hasattr(value, "value"):
                return str(value.value)
            return str(value)

        dashboard_state.fills = [
            {
                "order_id": fill.order_id,
                "symbol": fill.symbol,
                "market": _enum_value(fill.market).upper(),
                "side": _enum_value(fill.side),
                "quantity": fill.quantity,
                "price": float(fill.price),
                "commission": float(fill.commission),
                "pnl": float(fill.pnl) if fill.pnl is not None else None,
                "rsi": float(fill.rsi) if fill.rsi is not None else None,
                "reason": fill.reason,
                "timestamp": (
                    fill.timestamp.isoformat() if fill.timestamp else None
                ),
            }
            for fill in fills
        ]

        dashboard_state.trade_logs = []
        dashboard_state.trade_points = {}
        for fill in fills:
            side = _enum_value(fill.side)
            pnl = float(fill.pnl) if fill.pnl is not None else None
            cost = float(fill.price) * fill.quantity
            pnl_pct = (
                pnl / cost * 100
                if pnl is not None and cost > 0 else None
            )
            dashboard_state.add_trade_log(
                symbol=fill.symbol,
                market=_enum_value(fill.market).upper(),
                action=trade_action(side, fill.reason),
                price=float(fill.price),
                quantity=fill.quantity,
                rsi=float(fill.rsi) if fill.rsi is not None else None,
                trigger_rule=fill.reason or "historical",
                result="success",
                pnl=pnl,
                pnl_pct=pnl_pct,
                timestamp=fill.timestamp,
            )

        dashboard_state.calculate_performance()

    @app.get("/symbols", response_class=HTMLResponse)
    async def symbols_page(request: Request):
        """Symbol management page"""
        if not verify_session(request):
            return RedirectResponse(url="/login", status_code=302)

        # Build flat symbol list from strategy_configs
        symbols = []
        strategies: list[dict[str, str]] = []
        storage = await _get_web_storage()
        if storage:
            try:
                configs = (
                    await storage.get_all_strategy_configs()
                )
                # market comes with the name so a row can only offer the
                # strategies its symbol can actually move to
                strategies = [
                    {"name": cfg["name"], "market": cfg["market"]}
                    for cfg in configs
                ]
                for cfg in configs:
                    for s in cfg.get("symbols", []):
                        sym = (
                            s["symbol"]
                            if isinstance(s, dict) else s
                        )
                        mw = (
                            s.get("max_weight", 20.0)
                            if isinstance(s, dict)
                            else 20.0
                        )
                        sym_enabled = (
                            s.get("enabled", True)
                            if isinstance(s, dict)
                            else True
                        )
                        sym_market = (
                            s.get("market") if isinstance(s, dict) else None
                        ) or cfg["market"]
                        symbols.append({
                            "id": cfg["id"],
                            "symbol": sym,
                            "market": sym_market,
                            "strategy_name": cfg["name"],
                            "enabled": cfg["enabled"] and sym_enabled,
                            "max_weight": mw,
                            "created_at": cfg.get(
                                "created_at",
                            ),
                            "updated_at": cfg.get(
                                "updated_at",
                            ),
                        })
            finally:
                await storage.close()

        context = {
            "request": request,
            "active_page": "symbols",
            "symbols": symbols,
            "bot_status": dashboard_state.bot_status,
            "trading_paused": dashboard_state.trading_paused,
            "last_update": dashboard_state.last_update,
            "markets": ["krx", "nasdaq", "nyse", "amex"],
            "strategies": strategies,
            "pnl_usd": float(dashboard_state.pnl_usd),
        }
        return templates.TemplateResponse(
            request=request,
            name="symbols.html",
            context=context,
        )

    @app.get("/api/symbols")
    async def get_symbols(
        strategy_name: str = None,
        enabled_only: bool = False,
    ):
        """Get symbols from strategy_configs"""
        storage = await _get_web_storage()
        if not storage:
            return {"symbols": []}
        try:
            configs = (
                await storage.get_all_strategy_configs()
            )
            symbols = []
            for cfg in configs:
                if strategy_name and (
                    cfg["name"] != strategy_name
                ):
                    continue
                if enabled_only and not cfg["enabled"]:
                    continue
                for s in cfg.get("symbols", []):
                    sym = (
                        s["symbol"]
                        if isinstance(s, dict) else s
                    )
                    sym_market = (
                        s.get("market") if isinstance(s, dict) else None
                    ) or cfg["market"]
                    symbols.append({
                        "symbol": sym,
                        "market": sym_market,
                        "strategy_name": cfg["name"],
                        "enabled": cfg["enabled"],
                    })
            return {"symbols": symbols}
        finally:
            await storage.close()

    async def _validate_symbol_kis(
        symbol: str, market_code: str, kis_config: dict,
        auth: Any,
    ) -> dict:
        """Validate symbol via KIS API using the bot's shared KISAuth"""
        import aiohttp

        base_url = (
            "https://openapivts.koreainvestment.com:29443"
            if kis_config.get("paper_trading")
            else "https://openapi.koreainvestment.com:9443"
        )

        if market_code == "krx":
            tr_id = "FHKST01010100"
            endpoint = (
                "/uapi/domestic-stock/v1"
                "/quotations/inquire-price"
            )
            params = {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
            }
        else:
            tr_id = "HHDFS00000300"
            endpoint = (
                "/uapi/overseas-price/v1"
                "/quotations/price"
            )
            excd_map = {
                "nyse": "NYS",
                "nasdaq": "NAS",
                "amex": "AMS",
            }
            params = {
                "AUTH": "",
                "EXCD": excd_map.get(market_code, "NAS"),
                "SYMB": symbol,
            }

        async with aiohttp.ClientSession() as session:
            # Refreshes if the shared token has expired (24h TTL)
            await auth.ensure_authenticated(session)
            async with session.get(
                f"{base_url}{endpoint}",
                headers=auth.get_headers(tr_id),
                params=params,
            ) as resp:
                data = await resp.json()

        if data.get("rt_cd") != "0":
            return {
                "valid": False,
                "error": data.get("msg1", "Unknown error"),
            }

        # Check price exists
        output = data.get("output", {})
        if market_code == "krx":
            price = output.get("stck_prpr", "0")
        else:
            price = output.get("last", "0")

        if not price or price == "0":
            return {
                "valid": False,
                "error": f"No price data for {symbol}",
            }

        return {"valid": True, "price": price}

    @app.post("/api/symbols")
    async def add_symbol(body: WatchedSymbolCreate):
        """Add a symbol to a strategy config's symbols list"""
        if body.max_weight < 1 or body.max_weight > 100:
            raise HTTPException(
                status_code=400,
                detail="Weight must be between 1 and 100",
            )

        storage = await _get_web_storage()
        if not storage:
            raise HTTPException(
                status_code=503, detail="Storage not available",
            )

        # Validate symbol via KIS API
        if dashboard_state.kis_config and dashboard_state.kis_auth:
            validation = await _validate_symbol_kis(
                symbol=body.symbol.upper(),
                market_code=body.market.lower(),
                kis_config=dashboard_state.kis_config,
                auth=dashboard_state.kis_auth,
            )
            if not validation["valid"]:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Invalid symbol '{body.symbol}': "
                        f"{validation['error']}"
                    ),
                )

        try:
            config = await storage.get_strategy_config(body.strategy_name)
            if not config:
                raise HTTPException(
                    status_code=404,
                    detail=f"Strategy '{body.strategy_name}' not found",
                )

            symbol_upper = body.symbol.upper()
            symbols_list = config["symbols"]

            # Check for duplicate
            existing = [
                s["symbol"] if isinstance(s, dict) else s
                for s in symbols_list
            ]
            if symbol_upper in existing:
                return {"symbol": symbol_upper, "duplicate": True}

            symbols_list.append({
                "symbol": symbol_upper,
                "market": body.market.lower(),
                "max_weight": body.max_weight,
            })
            await storage.update_strategy_config(
                body.strategy_name, symbols=symbols_list,
            )
            return {
                "symbol": symbol_upper,
                "strategy_name": body.strategy_name,
                "duplicate": False,
            }
        finally:
            await storage.close()

    @app.delete("/api/symbols/{config_id}")
    async def delete_symbol(config_id: int, symbol: str):
        """Remove a symbol from a strategy config's symbols list"""
        storage = await _get_web_storage()
        if not storage:
            raise HTTPException(
                status_code=503, detail="Storage not available",
            )

        try:
            config = await storage.get_strategy_config_by_id(config_id)
            if not config:
                raise HTTPException(
                    status_code=404, detail="Strategy config not found",
                )

            symbol_upper = symbol.upper()
            new_symbols = [
                s for s in config["symbols"]
                if (s["symbol"] if isinstance(s, dict) else s) != symbol_upper
            ]
            if len(new_symbols) == len(config["symbols"]):
                raise HTTPException(
                    status_code=404, detail="Symbol not found",
                )

            await storage.update_strategy_config(
                config["name"], symbols=new_symbols,
            )
            return {"success": True}
        finally:
            await storage.close()

    @app.post("/api/symbols/{config_id}/toggle")
    async def toggle_symbol(config_id: int, symbol: str):
        """Toggle a symbol's enabled flag within a strategy config"""
        storage = await _get_web_storage()
        if not storage:
            raise HTTPException(
                status_code=503, detail="Storage not available",
            )

        try:
            config = await storage.get_strategy_config_by_id(config_id)
            if not config:
                raise HTTPException(
                    status_code=404, detail="Strategy config not found",
                )

            symbol_upper = symbol.upper()
            new_symbols = []
            found = False
            for s in config["symbols"]:
                sym = s["symbol"] if isinstance(s, dict) else s
                if sym == symbol_upper:
                    found = True
                    entry = s if isinstance(s, dict) else {"symbol": sym}
                    entry["enabled"] = not entry.get("enabled", True)
                    new_symbols.append(entry)
                else:
                    new_symbols.append(s)

            if not found:
                raise HTTPException(
                    status_code=404, detail="Symbol not found",
                )

            await storage.update_strategy_config(
                config["name"], symbols=new_symbols,
            )
            toggled = next(
                s for s in new_symbols
                if (s["symbol"] if isinstance(s, dict) else s) == symbol_upper
            )
            return {"symbol": symbol_upper, "enabled": toggled.get("enabled", True)}
        finally:
            await storage.close()

    class SymbolStrategyUpdate(BaseModel):
        strategy_name: str

    @app.post("/api/symbols/{config_id}/strategy")
    async def move_symbol_strategy(
        config_id: int, body: SymbolStrategyUpdate, symbol: str,
    ):
        """Move a symbol to another strategy, keeping weight and enabled"""
        storage = await _get_web_storage()
        if not storage:
            raise HTTPException(
                status_code=503, detail="Storage not available",
            )

        try:
            source = await storage.get_strategy_config_by_id(config_id)
            if not source:
                raise HTTPException(
                    status_code=404, detail="Strategy config not found",
                )

            target = await storage.get_strategy_config(body.strategy_name)
            if not target:
                raise HTTPException(
                    status_code=404,
                    detail=f"Strategy '{body.strategy_name}' not found",
                )

            symbol_upper = symbol.upper()
            if target["id"] == source["id"]:
                return {
                    "symbol": symbol_upper, "strategy_name": source["name"],
                }

            entry = next(
                (
                    s for s in source["symbols"]
                    if (s["symbol"] if isinstance(s, dict) else s)
                    == symbol_upper
                ),
                None,
            )
            if entry is None:
                raise HTTPException(
                    status_code=404, detail="Symbol not found",
                )

            target_symbols = target["symbols"]
            if any(
                (s["symbol"] if isinstance(s, dict) else s) == symbol_upper
                for s in target_symbols
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"{symbol_upper} is already in '{target['name']}'"
                    ),
                )

            if not isinstance(entry, dict):
                entry = {"symbol": symbol_upper, "max_weight": 20.0}
            # The symbol carries its market with it. Legacy entries that
            # inherited it from the source config get it pinned here, so a
            # move into a strategy with a different default cannot silently
            # retarget the symbol at another exchange.
            entry.setdefault("market", source["market"])

            # ponytail: two writes, not one transaction. Add to the target
            # first so a failure between them leaves a visible duplicate
            # rather than silently dropping the symbol. Wrap in a storage
            # transaction if moves ever get frequent enough to collide.
            await storage.update_strategy_config(
                target["name"], symbols=[*target_symbols, entry],
            )
            await storage.update_strategy_config(
                source["name"],
                symbols=[
                    s for s in source["symbols"]
                    if (s["symbol"] if isinstance(s, dict) else s)
                    != symbol_upper
                ],
            )
            return {"symbol": symbol_upper, "strategy_name": target["name"]}
        finally:
            await storage.close()

    class WeightUpdate(BaseModel):
        max_weight: float

    @app.post("/api/symbols/{config_id}/weight")
    async def update_symbol_weight(
        config_id: int, body: WeightUpdate, symbol: str,
    ):
        """Update max_weight for a symbol within a strategy config"""
        storage = await _get_web_storage()
        if not storage:
            raise HTTPException(
                status_code=503, detail="Storage not available",
            )

        if body.max_weight < 1 or body.max_weight > 100:
            raise HTTPException(
                status_code=400,
                detail="Weight must be between 1 and 100",
            )

        try:
            config = await storage.get_strategy_config_by_id(config_id)
            if not config:
                raise HTTPException(
                    status_code=404, detail="Strategy config not found",
                )

            symbol_upper = symbol.upper()
            new_symbols = []
            found = False
            for s in config["symbols"]:
                sym = s["symbol"] if isinstance(s, dict) else s
                if sym == symbol_upper:
                    found = True
                    entry = s if isinstance(s, dict) else {"symbol": sym}
                    entry["max_weight"] = body.max_weight
                    new_symbols.append(entry)
                else:
                    new_symbols.append(s)

            if not found:
                raise HTTPException(
                    status_code=404, detail="Symbol not found",
                )

            await storage.update_strategy_config(
                config["name"], symbols=new_symbols,
            )
            return {"symbol": symbol_upper, "max_weight": body.max_weight}
        finally:
            await storage.close()

    # ==================== Portfolio Routes ====================

    @app.get("/portfolio", response_class=HTMLResponse)
    async def portfolio_page(request: Request):
        """Portfolio overview page"""
        if not verify_session(request):
            return RedirectResponse(url="/login", status_code=302)

        # Get symbol weights from strategy_configs
        symbol_weights = {}
        storage = await _get_web_storage()
        if storage:
            try:
                configs = (
                    await storage.get_all_strategy_configs()
                )
                for cfg in configs:
                    for s in cfg.get("symbols", []):
                        if isinstance(s, dict):
                            symbol_weights[s["symbol"]] = (
                                s.get("max_weight", 20.0)
                            )
            finally:
                await storage.close()

        # Build portfolio data from positions
        positions = await _get_current_positions_for_web()
        total_value = float(dashboard_state.balance_usd)

        portfolio = []
        for pos in positions:
            value = pos.current_price * pos.quantity
            weight = (value / total_value * 100) if total_value > 0 else 0
            max_weight = symbol_weights.get(pos.symbol, 20.0)
            portfolio.append({
                "symbol": pos.symbol,
                "market": pos.market,
                "quantity": pos.quantity,
                "avg_price": pos.avg_price,
                "current_price": pos.current_price,
                "value": value,
                "weight": weight,
                "max_weight": max_weight,
                "over_limit": weight > max_weight,
                "pnl": pos.pnl,
                "pnl_pct": pos.pnl_pct,
            })

        # Cash weight
        cash_total = float(dashboard_state.cash_usd)
        cash_weight = (
            (cash_total / total_value * 100)
            if total_value > 0 else 100
        )

        context = {
            "request": request,
            "active_page": "portfolio",
            "portfolio": portfolio,
            "total_value": total_value,
            "cash_total": cash_total,
            "cash_weight": cash_weight,
            "bot_status": dashboard_state.bot_status,
            "trading_paused": dashboard_state.trading_paused,
            "last_update": dashboard_state.last_update,
            "pnl_usd": float(dashboard_state.pnl_usd),
        }
        return templates.TemplateResponse(
            request=request,
            name="portfolio.html",
            context=context,
        )

    @app.get("/api/portfolio")
    async def get_portfolio():
        """Get portfolio data"""
        symbol_weights = {}
        storage = await _get_web_storage()
        if storage:
            try:
                configs = (
                    await storage.get_all_strategy_configs()
                )
                for cfg in configs:
                    for s in cfg.get("symbols", []):
                        if isinstance(s, dict):
                            symbol_weights[s["symbol"]] = (
                                s.get("max_weight", 20.0)
                            )
            finally:
                await storage.close()

        positions = await _get_current_positions_for_web()
        total_value = float(dashboard_state.balance_usd)

        portfolio = []
        for pos in positions:
            value = pos.current_price * pos.quantity
            weight = (value / total_value * 100) if total_value > 0 else 0
            max_weight = symbol_weights.get(pos.symbol, 20.0)
            portfolio.append({
                "symbol": pos.symbol,
                "market": pos.market,
                "value": value,
                "weight": round(weight, 2),
                "max_weight": max_weight,
                "over_limit": weight > max_weight,
                "pnl_pct": round(pos.pnl_pct, 2),
            })

        cash_total = float(dashboard_state.cash_usd)
        cash_weight = (
            (cash_total / total_value * 100)
            if total_value > 0 else 100
        )

        return {
            "total_value": round(total_value, 2),
            "cash": round(cash_total, 2),
            "cash_weight": round(cash_weight, 2),
            "positions": portfolio,
        }

    @app.get("/portfolio/correlation", response_class=HTMLResponse)
    async def portfolio_correlation_page(request: Request):
        """Preview: how correlated is each holding with its single most
        correlated other holding? One number per symbol (not a full N×N
        matrix, which stops being readable past a handful of holdings, and
        not an average, which dilutes a genuinely dangerous pair once the
        rest of the portfolio is diversified)."""
        if not verify_session(request):
            return RedirectResponse(url="/login", status_code=302)

        def corr_color(value: float) -> str:
            if value < 0.3:
                return "var(--positive)"
            if value < 0.6:
                return "var(--warning)"
            return "var(--negative)"

        positions = await _get_current_positions_for_web()
        symbols = [p.symbol for p in positions]
        correlations: list[dict[str, Any]] = []
        matrix_symbols: list[str] = []
        matrix_rows: list[dict[str, Any]] = []

        if len(symbols) >= 3:
            import pandas as pd

            storage = await _get_web_storage()
            closes = {}
            if storage:
                try:
                    for sym in symbols:
                        rows = await storage.get_daily_ohlc_rsi(sym, limit=60)
                        if rows:
                            closes[sym] = pd.Series(
                                {r["day"]: r["close"] for r in rows}
                            )
                finally:
                    await storage.close()

            if len(closes) >= 3:
                df = pd.DataFrame(closes).sort_index().pct_change().dropna(how="all")
                corr = df.corr().abs()
                seen_pairs: set[frozenset[str]] = set()
                for sym in corr.columns:
                    others = corr[sym].drop(sym).dropna()
                    if others.empty:
                        continue
                    max_corr = float(others.max())
                    with_symbol = others.idxmax()
                    pair = frozenset({sym, with_symbol})
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    correlations.append({
                        "symbol": sym,
                        "max_corr": round(max_corr, 2),
                        "with_symbol": with_symbol,
                        "color": corr_color(max_corr),
                    })
                correlations.sort(key=lambda c: c["max_corr"], reverse=True)

                # Full N×N grid — kept for side-by-side comparison against the
                # per-symbol list above. Same corr() call, just rendered in
                # full instead of collapsed to one number per symbol; gets
                # unreadable past a handful of holdings (that's the whole
                # reason the list exists), so .table-wrap scrolls it instead
                # of breaking the page layout.
                matrix_symbols = list(corr.columns)
                for sym in matrix_symbols:
                    cells = []
                    for other in matrix_symbols:
                        value = float(corr.loc[sym, other])
                        cells.append({
                            "other": other,
                            "value": round(value, 2),
                            "color": "var(--card)" if sym == other else corr_color(value),
                            "dim": sym == other,
                        })
                    matrix_rows.append({"symbol": sym, "cells": cells})

        context = {
            "request": request,
            "active_page": "portfolio",
            "correlations": correlations,
            "symbol_count": len(symbols),
            "matrix_symbols": matrix_symbols,
            "matrix_rows": matrix_rows,
        }
        return templates.TemplateResponse(
            request=request,
            name="portfolio_correlation.html",
            context=context,
        )

    @app.get("/portfolio/rsi-trend", response_class=HTMLResponse)
    async def portfolio_rsi_trend_page(request: Request):
        """Preview: one blended RSI line across every enabled registered
        symbol (not just current holdings). View-only — no strategy reads
        this; it's just a way to eyeball whether the whole book is
        oversold/overbought as a group, day by day."""
        if not verify_session(request):
            return RedirectResponse(url="/login", status_code=302)

        storage = await _get_web_storage()
        symbols: list[str] = []
        daily: dict[str, list[float]] = {}
        if storage:
            try:
                configs = await storage.get_all_strategy_configs()
                seen = set()
                for cfg in configs:
                    for s in cfg.get("symbols", []):
                        sym = s["symbol"] if isinstance(s, dict) else s
                        sym_enabled = (
                            s.get("enabled", True)
                            if isinstance(s, dict) else True
                        )
                        if cfg["enabled"] and sym_enabled and sym not in seen:
                            seen.add(sym)
                            symbols.append(sym)

                for sym in symbols:
                    # Same 60-day window the correlation preview uses.
                    rows = await storage.get_daily_ohlc_rsi(sym, limit=60)
                    for row in rows:
                        daily.setdefault(row["day"], []).append(row["rsi"])
            finally:
                await storage.close()

        # Simple equal-weighted average — every registered symbol counts the
        # same regardless of position size, since this is a viewing aid, not
        # a trading signal.
        trend = [
            {"day": day, "rsi": round(sum(vals) / len(vals), 2)}
            for day, vals in sorted(daily.items())
        ]

        context = {
            "request": request,
            "active_page": "symbols",
            "symbol_count": len(symbols),
            "trend": trend,
            "latest_rsi": trend[-1]["rsi"] if trend else None,
        }
        return templates.TemplateResponse(
            request=request,
            name="portfolio_rsi_trend.html",
            context=context,
        )

    # ==================== Strategy Settings Routes ====================

    class StrategyParamsUpdate(BaseModel):
        strategy_name: str
        params: dict[str, Any]

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        """Strategy settings page"""
        if not verify_session(request):
            return RedirectResponse(url="/login", status_code=302)

        # Get strategy configs from DB
        strategy_configs = []
        storage = await _get_web_storage()
        if storage:
            try:
                strategy_configs = (
                    await storage.get_all_strategy_configs()
                )
            finally:
                await storage.close()

        # A strategy can hold several markets, so show the ones its symbols
        # actually trade rather than the config's default.
        for cfg in strategy_configs:
            markets = []
            for s in cfg.get("symbols", []):
                mkt = (
                    s.get("market") if isinstance(s, dict) else None
                ) or cfg["market"]
                if mkt and mkt not in markets:
                    markets.append(mkt)
            cfg["markets"] = markets

        # Get available strategy classes
        from src.strategy import available_strategies
        strategy_classes = [
            {"class_path": k, "name": v}
            for k, v in available_strategies().items()
        ]

        context = {
            "request": request,
            "active_page": "settings",
            "strategy_configs": strategy_configs,
            "strategy_classes": strategy_classes,
            "strategy_names": dashboard_state.strategy_names,
            "bot_status": dashboard_state.bot_status,
            "trading_paused": dashboard_state.trading_paused,
            "last_update": dashboard_state.last_update,
            "pnl_usd": float(dashboard_state.pnl_usd),
        }
        return templates.TemplateResponse(
            request=request,
            name="settings.html",
            context=context,
        )

    @app.get("/api/settings")
    async def get_settings():
        """Get all strategy params"""
        storage = await _get_web_storage()
        if not storage:
            return {"params": []}
        try:
            params = await storage.get_all_strategy_params()
            return {"params": params}
        finally:
            await storage.close()

    @app.get("/api/settings/{strategy_name}")
    async def get_strategy_settings(strategy_name: str):
        """Get params for a strategy"""
        storage = await _get_web_storage()
        if not storage:
            raise HTTPException(
                status_code=503,
                detail="Storage not available",
            )
        try:
            params = await storage.get_strategy_params(
                strategy_name,
            )
            if params is None:
                raise HTTPException(
                    status_code=404,
                    detail="Strategy not found",
                )
            return {"strategy_name": strategy_name, "params": params}
        finally:
            await storage.close()

    @app.post("/api/settings")
    async def update_settings(body: StrategyParamsUpdate):
        """Update strategy params (saves to DB + live update)"""
        storage = await _get_web_storage()
        if not storage:
            raise HTTPException(
                status_code=503,
                detail="Storage not available",
            )

        try:
            await storage.save_strategy_params(
                body.strategy_name, body.params,
            )
        finally:
            await storage.close()

        # Live update: apply to running strategy
        applied = False
        for strategy in (
            dashboard_state.strategy_instances or []
        ):
            if strategy.name_with_market == body.strategy_name:
                strategy.params.update(body.params)
                applied = True
                logger.info(
                    f"Live params updated: {body.strategy_name}"
                )
                break

        return {
            "success": True,
            "applied_live": applied,
            "strategy_name": body.strategy_name,
            "params": body.params,
        }

    # ==================== Strategy Config CRUD ====================

    class StrategyConfigCreate(BaseModel):
        name: str
        class_path: str
        market: str
        symbols: list
        params: dict
        enabled: bool = True

    class StrategyConfigUpdate(BaseModel):
        name: str | None = None
        class_path: str | None = None
        market: str | None = None
        symbols: list | None = None
        params: dict | None = None
        enabled: bool | None = None

    def _validate_strategy_name(name: str) -> str:
        """Strategy names become DOM ids and inline JS string literals on the
        settings page, so anything outside this set can break that markup."""
        name = name.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", name):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Strategy name must be 1-100 characters of letters, "
                    "digits, underscore or hyphen"
                ),
            )
        return name

    @app.get("/api/strategies")
    async def get_strategies():
        """Get all strategy configurations"""
        storage = await _get_web_storage()
        if not storage:
            return {"strategies": []}
        try:
            configs = (
                await storage.get_all_strategy_configs()
            )
            return {"strategies": configs}
        finally:
            await storage.close()

    @app.get("/api/strategy-classes")
    async def get_strategy_classes():
        """Get available strategy classes for dropdown"""
        from src.strategy import available_strategies
        classes = available_strategies()
        return {
            "classes": [
                {"class_path": k, "name": v}
                for k, v in classes.items()
            ]
        }

    @app.post("/api/strategies")
    async def create_strategy(body: StrategyConfigCreate):
        """Create a new strategy configuration"""
        from src.strategy import available_strategies

        new_name = _validate_strategy_name(body.name)

        # Validate class_path against allowlist
        allowed = available_strategies()
        if body.class_path not in allowed:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Invalid class_path. "
                    f"Allowed: {list(allowed.keys())}"
                ),
            )

        # Validate market
        try:
            from src.core.types import Market
            Market.from_string(body.market)
        except ValueError as e:
            raise HTTPException(
                status_code=400, detail=str(e),
            )

        storage = await _get_web_storage()
        if not storage:
            raise HTTPException(
                status_code=503,
                detail="Storage not available",
            )

        try:
            result = (
                await storage.create_strategy_config(
                    name=new_name,
                    class_path=body.class_path,
                    market=body.market,
                    symbols=body.symbols,
                    params=body.params,
                    enabled=body.enabled,
                )
            )
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(
                    status_code=400,
                    detail=f"Strategy '{body.name}' "
                    f"already exists",
                )
            raise
        finally:
            await storage.close()

        # Trigger hot reload
        bot_running = _trigger_bot_reload()

        return {
            "success": True,
            "strategy": result,
            "bot_reloaded": bot_running,
        }

    @app.put("/api/strategies/{name}")
    async def update_strategy(
        name: str, body: StrategyConfigUpdate,
    ):
        """Update a strategy configuration"""
        renamed_to = None
        if body.name is not None:
            renamed_to = _validate_strategy_name(body.name)

        # Validate class_path if provided
        if body.class_path is not None:
            from src.strategy import available_strategies
            allowed = available_strategies()
            if body.class_path not in allowed:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid class_path",
                )

        if body.market is not None:
            try:
                from src.core.types import Market
                Market.from_string(body.market)
            except ValueError as e:
                raise HTTPException(
                    status_code=400, detail=str(e),
                )

        storage = await _get_web_storage()
        if not storage:
            raise HTTPException(
                status_code=503,
                detail="Storage not available",
            )

        try:
            kwargs = {
                k: v
                for k, v in body.model_dump().items()
                if v is not None and k != "name"
            }
            # the row is looked up by its current name, so a rename has to
            # travel under a different key than the path parameter
            if renamed_to is not None and renamed_to != name:
                if await storage.get_strategy_config(renamed_to):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Strategy '{renamed_to}' already exists",
                    )
                kwargs["new_name"] = renamed_to
            result = (
                await storage.update_strategy_config(
                    name, **kwargs,
                )
            )
        finally:
            await storage.close()

        if result is None:
            raise HTTPException(
                status_code=404,
                detail=f"Strategy '{name}' not found",
            )

        # Trigger hot reload
        bot_running = _trigger_bot_reload()

        return {
            "success": True,
            "strategy": result,
            "bot_reloaded": bot_running,
        }

    @app.delete("/api/strategies/{name}")
    async def delete_strategy(name: str):
        """Delete a strategy configuration"""
        storage = await _get_web_storage()
        if not storage:
            raise HTTPException(
                status_code=503,
                detail="Storage not available",
            )

        try:
            deleted = (
                await storage.delete_strategy_config(name)
            )
        finally:
            await storage.close()

        if not deleted:
            raise HTTPException(
                status_code=404,
                detail=f"Strategy '{name}' not found",
            )

        # Trigger hot reload
        bot_running = _trigger_bot_reload()

        return {
            "success": True,
            "deleted": name,
            "bot_reloaded": bot_running,
        }

    # ==================== Analytics Routes ====================

    @app.get("/analytics", response_class=HTMLResponse)
    async def analytics_page(request: Request):
        """Analytics page with reports, drawdown, and statistics"""
        if not verify_session(request):
            return RedirectResponse(url="/login", status_code=302)

        from src.analytics import ReportGenerator, DrawdownAnalyzer, TradeStatistics

        await _load_fills_for_web()
        # Generate reports
        report_gen = ReportGenerator(
            fills=dashboard_state.fills,
            equity_history=dashboard_state.equity_history,
        )
        daily_report = report_gen.generate_daily_report()
        weekly_report = report_gen.generate_weekly_report()
        monthly_report = report_gen.generate_monthly_report()

        # Drawdown analysis
        dd_analyzer = DrawdownAnalyzer(dashboard_state.equity_history)
        drawdown = dd_analyzer.analyze("usd")

        # Trade statistics
        stats_calc = TradeStatistics(dashboard_state.fills)
        statistics = stats_calc.calculate()

        context = {
            "request": request,
            "active_page": "analytics",
            "daily_report": daily_report,
            "weekly_report": weekly_report,
            "monthly_report": monthly_report,
            "drawdown": drawdown,
            "statistics": statistics,
            "bot_status": dashboard_state.bot_status,
            "trading_paused": dashboard_state.trading_paused,
            "last_update": dashboard_state.last_update,
            "pnl_usd": float(dashboard_state.pnl_usd),
            "timedelta": timedelta,
        }
        return templates.TemplateResponse(
            request=request,
            name="analytics.html",
            context=context,
        )

    @app.get("/api/analytics/reports")
    async def get_analytics_reports(period: str = "daily"):
        """Get trade reports"""
        from src.analytics import ReportGenerator

        await _load_fills_for_web()
        report_gen = ReportGenerator(
            fills=dashboard_state.fills,
            equity_history=dashboard_state.equity_history,
        )

        if period == "daily":
            report = report_gen.generate_daily_report()
        elif period == "weekly":
            report = report_gen.generate_weekly_report()
        elif period == "monthly":
            report = report_gen.generate_monthly_report()
        else:
            report = report_gen.generate_daily_report()

        return {
            "period_type": report.period_type,
            "start_date": report.start_date.isoformat(),
            "end_date": report.end_date.isoformat(),
            "total_trades": report.total_trades,
            "winning_trades": report.winning_trades,
            "losing_trades": report.losing_trades,
            "win_rate": report.win_rate,
            "total_pnl": float(report.total_pnl),
            "avg_win": float(report.avg_win),
            "avg_loss": float(report.avg_loss),
            "profit_factor": report.profit_factor,
            "best_trade": float(report.best_trade),
            "worst_trade": float(report.worst_trade),
            "return_pct": report.return_pct,
            "by_symbol": {k: {
                "trades": v["trades"],
                "wins": v["wins"],
                "losses": v["losses"],
                "pnl": float(v["pnl"]),
            } for k, v in report.by_symbol.items()},
        }

    @app.get("/api/analytics/drawdown")
    async def get_analytics_drawdown(currency: str = "usd"):
        """Get drawdown analysis"""
        from src.analytics import DrawdownAnalyzer

        analyzer = DrawdownAnalyzer(dashboard_state.equity_history)
        analysis = analyzer.analyze(currency)

        return {
            "current_equity": float(analysis.current_equity),
            "peak_equity": float(analysis.peak_equity),
            "current_drawdown": float(analysis.current_drawdown),
            "current_drawdown_pct": analysis.current_drawdown_pct,
            "mdd": float(analysis.mdd),
            "mdd_pct": analysis.mdd_pct,
            "mdd_start": analysis.mdd_start.isoformat() if analysis.mdd_start else None,
            "mdd_bottom": analysis.mdd_bottom.isoformat() if analysis.mdd_bottom else None,
            "avg_drawdown_pct": analysis.avg_drawdown_pct,
            "max_drawdown_duration_days": analysis.max_drawdown_duration_days,
            "current_drawdown_duration_days": analysis.current_drawdown_duration_days,
            "alert_triggered": analysis.alert_triggered,
            "alert_level": analysis.alert_level,
            "history": analysis.drawdown_history[-100:],  # Last 100 points
        }

    @app.get("/api/analytics/statistics")
    async def get_analytics_statistics():
        """Get trade statistics"""
        from src.analytics import TradeStatistics

        await _load_fills_for_web()
        calc = TradeStatistics(dashboard_state.fills)
        stats = calc.calculate()

        return {
            "total_trades": stats.total_trades,
            "winning_trades": stats.winning_trades,
            "losing_trades": stats.losing_trades,
            "win_rate": stats.win_rate,
            "total_pnl": float(stats.total_pnl),
            "avg_pnl": float(stats.avg_pnl),
            "avg_win": float(stats.avg_win),
            "avg_loss": float(stats.avg_loss),
            "best_trade": float(stats.best_trade),
            "worst_trade": float(stats.worst_trade),
            "profit_factor": stats.profit_factor,
            "current_streak": stats.current_streak,
            "max_win_streak": stats.max_win_streak,
            "max_loss_streak": stats.max_loss_streak,
            "last_7_days_win_rate": stats.last_7_days_win_rate,
            "last_30_days_win_rate": stats.last_30_days_win_rate,
            "by_symbol": [{
                "symbol": s.symbol,
                "total_trades": s.total_trades,
                "win_rate": s.win_rate,
                "total_pnl": float(s.total_pnl),
                "profit_factor": s.profit_factor,
            } for s in stats.by_symbol],
            "by_hour": [{
                "label": t.label,
                "total_trades": t.total_trades,
                "win_rate": t.win_rate,
                "total_pnl": float(t.total_pnl),
            } for t in stats.by_hour],
            "by_day_of_week": [{
                "label": t.label,
                "total_trades": t.total_trades,
                "win_rate": t.win_rate,
                "total_pnl": float(t.total_pnl),
            } for t in stats.by_day_of_week],
        }

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        """WebSocket endpoint for real-time updates"""
        if not verify_session(websocket):
            await websocket.close(code=1008)  # Policy Violation
            return
        await ws_manager.connect(websocket)
        try:
            # Send initial data on connect
            await websocket.send_text(json.dumps({
                "type": "init",
                "data": get_dashboard_snapshot()
            }, default=str))

            # Keep connection alive and handle incoming messages
            while True:
                try:
                    # Wait for messages (ping/pong or commands)
                    data = await websocket.receive_text()
                    if data == "ping":
                        await websocket.send_text("pong")
                except WebSocketDisconnect:
                    break
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
        finally:
            ws_manager.disconnect(websocket)

    return app


def get_dashboard_snapshot() -> dict:
    """Get current dashboard state as a snapshot for WebSocket"""
    dashboard_state.update_signal_candidates()
    dashboard_state.update_market_status()

    return {
        "balance_krw": float(dashboard_state.balance_krw),
        "balance_usd": float(dashboard_state.balance_usd),
        "cash_krw": float(dashboard_state.cash_krw),
        "cash_usd": float(dashboard_state.cash_usd),
        "pnl_krw": float(dashboard_state.pnl_krw),
        "pnl_usd": float(dashboard_state.pnl_usd),
        "positions": [p.model_dump() for p in dashboard_state.positions.values()],
        "rsi_values": dict(dashboard_state.rsi_values),
        "rsi_prices": dict(dashboard_state.rsi_prices),
        "recent_signals": dashboard_state.recent_signals[:10],
        "recent_orders": dashboard_state.recent_orders[:10],
        "bot_status": dashboard_state.bot_status.model_dump() if dashboard_state.bot_status else None,
        "system_status": dashboard_state.system_status.model_dump(),
        "last_update": dashboard_state.last_update.isoformat() if dashboard_state.last_update else None,
    }


async def broadcast_update(update_type: str = "update"):
    """Broadcast dashboard update to all connected clients"""
    await ws_manager.broadcast({
        "type": update_type,
        "data": get_dashboard_snapshot()
    })


def get_dashboard_state() -> DashboardState:
    """Get the global dashboard state"""
    return dashboard_state


def get_ws_manager() -> ConnectionManager:
    """Get the global WebSocket manager"""
    return ws_manager
