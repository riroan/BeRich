"""Web dashboard for trading bot monitoring"""

import asyncio
import hashlib
import secrets
import os
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, Dict, Any, List
from pathlib import Path
from enum import Enum

from fastapi import FastAPI, Request, Form, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import logging
import json

logger = logging.getLogger(__name__)

# Base directory for templates and static files
BASE_DIR = Path(__file__).parent


class ConnectionManager:
    """WebSocket connection manager"""

    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"WebSocket connected. Total: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        logger.info(f"WebSocket disconnected. Total: {len(self.active_connections)}")

    async def broadcast(self, message: dict):
        """Broadcast message to all connected clients"""
        if not self.active_connections:
            return

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
    rsi: Optional[float] = None
    # Strategy-specific info
    buy_stage: int = 0
    sell_stage: int = 0
    max_buy_stages: int = 3
    max_sell_stages: int = 3
    last_buy_date: Optional[str] = None
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
    rsi: Optional[float] = None
    trigger_rule: str  # what triggered this trade
    result: str  # success, failed, pending
    pnl: Optional[float] = None
    pnl_pct: Optional[float] = None


class SystemStatus(BaseModel):
    """System status info"""
    auto_trading_enabled: bool = True
    last_strategy_run: Optional[str] = None
    last_price_update: Optional[str] = None
    api_connected: bool = True
    account_tradable: bool = True
    data_collection_ok: bool = True
    error_message: Optional[str] = None


class PerformanceMetrics(BaseModel):
    """Performance analysis metrics"""
    total_return_pct: float = 0.0
    cagr: float = 0.0
    mdd: float = 0.0
    win_rate: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    avg_profit: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    sharpe_ratio: float = 0.0
    total_pnl: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0


class MarketStatus(BaseModel):
    """Market overview status"""
    market_rsi: Optional[float] = None
    oversold_count: int = 0
    overbought_count: int = 0
    total_symbols: int = 0
    market_state: str = "neutral"  # oversold, neutral, overbought


class BotStatus(BaseModel):
    running: bool
    paper_trading: bool
    warmup_remaining: Optional[str] = None
    strategies: List[str]
    uptime: str


class PricePoint(BaseModel):
    time: str  # ISO format or timestamp
    open: float
    high: float
    low: float
    close: float
    volume: int = 0


class DashboardState:
    """Shared state for dashboard data"""

    def __init__(self):
        # Core data
        self.positions: Dict[str, PositionInfo] = {}
        self.rsi_values: Dict[str, float] = {}
        self.rsi_prices: Dict[str, Dict[str, Any]] = {}  # symbol -> {price, market}
        self.recent_signals: List[Dict[str, Any]] = []
        self.recent_orders: List[Dict[str, Any]] = []
        self.bot_status: Optional[BotStatus] = None

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
        self.last_update: Optional[datetime] = None
        self.last_strategy_run: Optional[datetime] = None
        self.last_price_update: Optional[datetime] = None

        # Price/RSI history for charts
        self.price_history: Dict[str, List[PricePoint]] = {}
        self.rsi_history: Dict[str, List[Dict[str, Any]]] = {}

        # Trade logs (extended from recent_orders)
        self.trade_logs: List[TradeLog] = []

        # Signal candidates
        self.signal_candidates: List[SignalCandidate] = []

        # System status
        self.system_status: SystemStatus = SystemStatus()

        # Performance metrics
        self.performance: PerformanceMetrics = PerformanceMetrics()

        # Market status
        self.market_status_krx: MarketStatus = MarketStatus()
        self.market_status_us: MarketStatus = MarketStatus()

        # Risk alerts
        self.risk_alerts: List[Dict[str, Any]] = []

        # Strategy internal state (synced from strategy)
        self.strategy_state: Dict[str, Dict[str, Any]] = {}

        # Trade points for chart markers
        self.trade_points: Dict[str, List[Dict[str, Any]]] = {}

        # Equity history for equity curve chart
        self.equity_history: List[Dict[str, Any]] = []

        # Fills for performance calculation
        self.fills: List[Dict[str, Any]] = []

        # Storage reference (set by bot on init - NOT usable from web thread)
        self.storage = None

        # Database URL for web-local storage
        self.db_url: Optional[str] = None

        # Strategy names from config
        self.strategy_names: List[str] = []

        # KIS API config for symbol validation
        self.kis_config: Optional[Dict[str, Any]] = None

        # KIS auth token (shared from bot's broker)
        self.kis_auth_token: Optional[str] = None

        # Live strategy instances (set by bot)
        self.strategy_instances: Optional[List[Any]] = None

    def update_position(
        self,
        symbol: str,
        market: str,
        quantity: int,
        avg_price: float,
        current_price: float,
        rsi: Optional[float] = None,
        buy_stage: int = 0,
        sell_stage: int = 0,
        max_buy_stages: int = 3,
        max_sell_stages: int = 3,
        last_buy_date: Optional[str] = None,
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
            max_buy_stages=max_buy_stages,
            max_sell_stages=max_sell_stages,
            last_buy_date=last_buy_date,
            stop_loss_pct=stop_loss_pct,
            stop_loss_distance=stop_loss_distance,
        )
        self.last_update = datetime.now()

    def update_rsi(self, symbol: str, rsi: float, price: float = None, market: str = None):
        self.rsi_values[symbol] = rsi
        if price is not None:
            self.rsi_prices[symbol] = {"price": price, "market": market}
        if symbol in self.positions:
            self.positions[symbol].rsi = rsi
        self.last_update = datetime.now()

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
            time=time.strftime("%Y-%m-%d %H:%M"),
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
            "time": time.strftime("%Y-%m-%d %H:%M"),
            "value": rsi,
        })

        # Keep only last 500 points
        if len(self.rsi_history[symbol]) > 500:
            self.rsi_history[symbol] = self.rsi_history[symbol][-500:]

    def add_signal(self, signal_data: Dict[str, Any]):
        self.recent_signals.insert(0, {
            **signal_data,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        # Keep only last 50 signals
        self.recent_signals = self.recent_signals[:50]

    def add_order(self, order_data: Dict[str, Any]):
        self.recent_orders.insert(0, {
            **order_data,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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
        rsi: Optional[float] = None,
        pnl: Optional[float] = None,
        pnl_pct: Optional[float] = None,
    ):
        """Add detailed trade log"""
        log = TradeLog(
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "action": action,
            "price": price,
            "rsi": rsi,
        })
        # Keep only last 50 points per symbol
        if len(self.trade_points[symbol]) > 50:
            self.trade_points[symbol] = self.trade_points[symbol][-50:]

    def update_signal_candidates(self):
        """Update list of signal candidates based on RSI values"""
        candidates = []

        for symbol, rsi in self.rsi_values.items():
            position = self.positions.get(symbol)
            market = position.market if position else "Unknown"
            current_price = position.current_price if position else 0

            # Buy candidates: RSI approaching 30
            if rsi <= 35:
                threshold = 30
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

            # Additional buy candidates: RSI approaching 25
            if rsi <= 30:
                threshold = 25
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

            # Sell candidates: RSI approaching 70
            if rsi >= 65 and position and position.quantity > 0:
                threshold = 70
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
            position = self.positions.get(symbol)
            if position:
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
        strategies: List[str],
        uptime: str,
        warmup_remaining: Optional[str] = None,
    ):
        self.bot_status = BotStatus(
            running=running,
            paper_trading=paper_trading,
            strategies=strategies,
            uptime=uptime,
            warmup_remaining=warmup_remaining,
        )

    def update_system_status(
        self,
        auto_trading: bool = True,
        api_connected: bool = True,
        account_tradable: bool = True,
        data_ok: bool = True,
        error: Optional[str] = None,
    ):
        self.system_status = SystemStatus(
            auto_trading_enabled=auto_trading,
            last_strategy_run=self.last_strategy_run.strftime("%Y-%m-%d %H:%M:%S") if self.last_strategy_run else None,
            last_price_update=self.last_price_update.strftime("%Y-%m-%d %H:%M:%S") if self.last_price_update else None,
            api_connected=api_connected,
            account_tradable=account_tradable,
            data_collection_ok=data_ok,
            error_message=error,
        )

    def calculate_performance(self):
        """Calculate performance metrics from equity history and fills"""
        import math

        # Calculate from equity history
        if len(self.equity_history) >= 2:
            # Get initial and current values (use USD for now, or combine)
            initial = self.equity_history[0]
            current = self.equity_history[-1]

            # Total return (using USD as primary)
            initial_value = initial.get("total_usd", 0) or 0
            current_value = current.get("total_usd", 0) or 0

            if initial_value > 0:
                self.performance.total_return_pct = (
                    (current_value - initial_value) / initial_value * 100
                )

            # Calculate MDD (Maximum Drawdown)
            peak = 0
            max_drawdown = 0
            for point in self.equity_history:
                value = point.get("total_usd", 0) or 0
                if value > peak:
                    peak = value
                if peak > 0:
                    drawdown = (peak - value) / peak * 100
                    if drawdown > max_drawdown:
                        max_drawdown = drawdown
            self.performance.mdd = max_drawdown

            # Calculate CAGR
            if len(self.equity_history) > 1 and initial_value > 0:
                first_time = datetime.fromisoformat(initial.get("timestamp", ""))
                last_time = datetime.fromisoformat(current.get("timestamp", ""))
                days = (last_time - first_time).days
                if days > 0:
                    years = days / 365.0
                    if years > 0 and current_value > 0:
                        self.performance.cagr = (
                            (pow(current_value / initial_value, 1 / years) - 1) * 100
                        )

            # Calculate Sharpe Ratio (simplified - daily returns)
            if len(self.equity_history) > 2:
                returns = []
                for i in range(1, len(self.equity_history)):
                    prev_val = self.equity_history[i - 1].get("total_usd", 0) or 1
                    curr_val = self.equity_history[i].get("total_usd", 0) or 1
                    if prev_val > 0:
                        daily_return = (curr_val - prev_val) / prev_val
                        returns.append(daily_return)

                if returns:
                    avg_return = sum(returns) / len(returns)
                    variance = sum((r - avg_return) ** 2 for r in returns) / len(returns)
                    std_dev = math.sqrt(variance) if variance > 0 else 0
                    if std_dev > 0:
                        # Annualized Sharpe (assuming ~252 trading days)
                        self.performance.sharpe_ratio = (
                            avg_return / std_dev * math.sqrt(252)
                        )

        # Calculate from fills/trades
        if self.fills:
            sell_trades = [f for f in self.fills if f.get("side") == "sell"]
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

# Global templates (created once)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Session storage (in-memory)
valid_sessions: Dict[str, datetime] = {}

# Auth config from environment
AUTH_USERNAME = os.getenv("DASHBOARD_USERNAME", "admin")
AUTH_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
SESSION_COOKIE_NAME = "berich_session"
SESSION_EXPIRE_HOURS = 24


def generate_session_token() -> str:
    """Generate a secure session token"""
    return secrets.token_hex(32)


def hash_password(password: str) -> str:
    """Hash password for comparison"""
    return hashlib.sha256(password.encode()).hexdigest()


def verify_session(request: Request) -> bool:
    """Check if request has valid session"""
    # If no password set, allow access
    if not AUTH_PASSWORD:
        return True

    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_token:
        return False

    if session_token not in valid_sessions:
        return False

    # Check expiry
    created = valid_sessions[session_token]
    if datetime.now() - created > timedelta(hours=SESSION_EXPIRE_HOURS):
        del valid_sessions[session_token]
        return False

    return True


def require_auth(request: Request):
    """Dependency to require authentication"""
    if not verify_session(request):
        raise HTTPException(status_code=401, detail="Not authenticated")


def create_app() -> FastAPI:
    """Create FastAPI application"""
    app = FastAPI(title="BeRich Dashboard", version="1.0.0")

    # Mount static files
    static_dir = BASE_DIR / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

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
    async def login(request: Request, username: str = Form(...), password: str = Form(...)):
        """Handle login"""
        if username == AUTH_USERNAME and password == AUTH_PASSWORD:
            # Create session
            token = generate_session_token()
            valid_sessions[token] = datetime.now()

            response = RedirectResponse(url="/", status_code=302)
            response.set_cookie(
                key=SESSION_COOKIE_NAME,
                value=token,
                httponly=True,
                max_age=SESSION_EXPIRE_HOURS * 3600,
            )
            return response
        else:
            return RedirectResponse(url="/login?error=Invalid credentials", status_code=302)

    @app.get("/logout")
    async def logout(request: Request):
        """Handle logout"""
        session_token = request.cookies.get(SESSION_COOKIE_NAME)
        if session_token and session_token in valid_sessions:
            del valid_sessions[session_token]

        response = RedirectResponse(url="/login", status_code=302)
        response.delete_cookie(SESSION_COOKIE_NAME)
        return response

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        """Main dashboard page"""
        if not verify_session(request):
            return RedirectResponse(url="/login", status_code=302)

        # Update derived states
        dashboard_state.update_signal_candidates()
        dashboard_state.update_market_status()
        dashboard_state.update_risk_alerts()

        # Calculate portfolio summary
        krw_positions = [p for p in dashboard_state.positions.values() if p.market == "KRX"]
        us_positions = [p for p in dashboard_state.positions.values() if p.market != "KRX"]

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
            # Portfolio summary
            "positions": list(dashboard_state.positions.values()),
            "krw_positions": krw_positions,
            "us_positions": us_positions,
            "position_count": len(dashboard_state.positions),
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
                for symbol, rsi in dashboard_state.rsi_values.items()
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

        context = {
            "request": request,
            "trade_logs": [log.model_dump() for log in dashboard_state.trade_logs],
            "bot_status": dashboard_state.bot_status,
            "last_update": dashboard_state.last_update,
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

        # Recalculate performance metrics
        dashboard_state.calculate_performance()

        context = {
            "request": request,
            "performance": dashboard_state.performance.model_dump(),
            "trade_logs": [log.model_dump() for log in dashboard_state.trade_logs],
            "fills": dashboard_state.fills,
            "bot_status": dashboard_state.bot_status,
            "last_update": dashboard_state.last_update,
        }
        return templates.TemplateResponse(
            request=request,
            name="performance.html",
            context=context,
        )

    @app.get("/api/status")
    async def get_status():
        """Get current bot status"""
        return {
            "bot_status": dashboard_state.bot_status,
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
        return [p.model_dump() for p in dashboard_state.positions.values()]

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

        position = dashboard_state.positions.get(symbol)
        rsi = dashboard_state.rsi_values.get(symbol)
        trade_points = dashboard_state.trade_points.get(symbol, [])

        # Get symbol-specific trade logs
        symbol_trades = [
            log.model_dump() for log in dashboard_state.trade_logs
            if log.symbol == symbol
        ][:20]

        context = {
            "request": request,
            "symbol": symbol,
            "position": position,
            "rsi": rsi,
            "trade_points": trade_points,
            "symbol_trades": symbol_trades,
            "last_update": dashboard_state.last_update,
        }
        return templates.TemplateResponse(
            request=request,
            name="symbol.html",
            context=context,
        )

    @app.get("/api/symbol/{symbol}/history")
    async def get_symbol_history(symbol: str, limit: int = 100):
        """Get price history for a symbol"""
        prices = dashboard_state.price_history.get(symbol, [])
        rsi = dashboard_state.rsi_history.get(symbol, [])
        trade_points = dashboard_state.trade_points.get(symbol, [])

        return {
            "symbol": symbol,
            "prices": [p.model_dump() for p in prices[-limit:]],
            "rsi": rsi[-limit:],
            "trade_points": trade_points[-limit:],
        }

    @app.get("/api/symbol/{symbol}")
    async def get_symbol_info(symbol: str):
        """Get symbol info"""
        position = dashboard_state.positions.get(symbol)
        rsi = dashboard_state.rsi_values.get(symbol)

        return {
            "symbol": symbol,
            "position": position.model_dump() if position else None,
            "rsi": rsi,
        }

    @app.get("/api/equity-history")
    async def get_equity_history():
        """Get equity curve data"""
        return {"data": dashboard_state.equity_history}

    # ==================== Symbol Management Routes ====================

    class WatchedSymbolCreate(BaseModel):
        symbol: str
        market: str
        strategy_name: str

    async def _get_web_storage():
        """Get a storage instance for web requests (own event loop)"""
        if not dashboard_state.db_url:
            return None
        from src.data.storage import Storage
        storage = Storage(dashboard_state.db_url)
        await storage.initialize()
        return storage

    @app.get("/symbols", response_class=HTMLResponse)
    async def symbols_page(request: Request):
        """Symbol management page"""
        if not verify_session(request):
            return RedirectResponse(url="/login", status_code=302)

        symbols = []
        storage = await _get_web_storage()
        if storage:
            try:
                symbols = await storage.get_watched_symbols(
                    enabled_only=False,
                )
            finally:
                await storage.close()

        context = {
            "request": request,
            "symbols": symbols,
            "bot_status": dashboard_state.bot_status,
            "last_update": dashboard_state.last_update,
            "markets": ["krx", "nasdaq", "nyse", "amex"],
            "strategy_names": dashboard_state.strategy_names,
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
        """Get watched symbols"""
        storage = await _get_web_storage()
        if not storage:
            return {"error": "Storage not available", "symbols": []}
        try:
            symbols = await storage.get_watched_symbols(
                strategy_name=strategy_name,
                enabled_only=enabled_only,
            )
            return {"symbols": symbols}
        finally:
            await storage.close()

    async def _validate_symbol_kis(
        symbol: str, market_code: str, kis_config: dict,
        auth_token: str,
    ) -> dict:
        """Validate symbol via KIS API using shared token"""
        import aiohttp

        base_url = (
            "https://openapivts.koreainvestment.com:29443"
            if kis_config.get("paper_trading")
            else "https://openapi.koreainvestment.com:9443"
        )

        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {auth_token}",
            "appkey": kis_config["app_key"],
            "appsecret": kis_config["app_secret"],
            "custtype": "P",
        }

        if market_code == "krx":
            headers["tr_id"] = "FHKST01010100"
            endpoint = (
                "/uapi/domestic-stock/v1"
                "/quotations/inquire-price"
            )
            params = {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
            }
        else:
            headers["tr_id"] = "HHDFS00000300"
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
            async with session.get(
                f"{base_url}{endpoint}",
                headers=headers,
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
        """Add a watched symbol"""
        storage = await _get_web_storage()
        if not storage:
            raise HTTPException(
                status_code=503, detail="Storage not available",
            )

        from src.core.types import Market
        market_map = {
            "krx": Market.KRX,
            "nyse": Market.NYSE,
            "nasdaq": Market.NASDAQ,
            "amex": Market.AMEX,
        }
        market = market_map.get(body.market.lower())
        if not market:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid market: {body.market}",
            )

        # Validate symbol via KIS API
        if dashboard_state.kis_config and dashboard_state.kis_auth_token:
            validation = await _validate_symbol_kis(
                symbol=body.symbol.upper(),
                market_code=body.market.lower(),
                kis_config=dashboard_state.kis_config,
                auth_token=dashboard_state.kis_auth_token,
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
            result = await storage.add_watched_symbol(
                symbol=body.symbol.upper(),
                market=market,
                strategy_name=body.strategy_name,
            )
            return result
        finally:
            await storage.close()

    @app.delete("/api/symbols/{symbol_id}")
    async def delete_symbol(symbol_id: int):
        """Remove a watched symbol"""
        storage = await _get_web_storage()
        if not storage:
            raise HTTPException(
                status_code=503, detail="Storage not available",
            )

        try:
            deleted = await storage.remove_watched_symbol(symbol_id)
            if not deleted:
                raise HTTPException(
                    status_code=404, detail="Symbol not found",
                )
            return {"success": True}
        finally:
            await storage.close()

    @app.post("/api/symbols/{symbol_id}/toggle")
    async def toggle_symbol(symbol_id: int):
        """Toggle symbol enabled/disabled"""
        storage = await _get_web_storage()
        if not storage:
            raise HTTPException(
                status_code=503, detail="Storage not available",
            )

        try:
            result = await storage.toggle_watched_symbol(symbol_id)
            if not result:
                raise HTTPException(
                    status_code=404, detail="Symbol not found",
                )
            return result
        finally:
            await storage.close()

    # ==================== Strategy Settings Routes ====================

    class StrategyParamsUpdate(BaseModel):
        strategy_name: str
        params: Dict[str, Any]

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        """Strategy settings page"""
        if not verify_session(request):
            return RedirectResponse(url="/login", status_code=302)

        # Get params from DB
        all_params = []
        storage = await _get_web_storage()
        if storage:
            try:
                all_params = (
                    await storage.get_all_strategy_params()
                )
            finally:
                await storage.close()

        # Get current live params from strategies
        live_params = {}
        for strategy in (
            dashboard_state.strategy_instances or []
        ):
            live_params[strategy.name_with_market] = (
                dict(strategy.params)
            )

        context = {
            "request": request,
            "all_params": all_params,
            "live_params": live_params,
            "strategy_names": dashboard_state.strategy_names,
            "bot_status": dashboard_state.bot_status,
            "last_update": dashboard_state.last_update,
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

    # ==================== Analytics Routes ====================

    @app.get("/analytics", response_class=HTMLResponse)
    async def analytics_page(request: Request):
        """Analytics page with reports, drawdown, and statistics"""
        if not verify_session(request):
            return RedirectResponse(url="/login", status_code=302)

        from src.analytics import ReportGenerator, DrawdownAnalyzer, TradeStatistics

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
            "daily_report": daily_report,
            "weekly_report": weekly_report,
            "monthly_report": monthly_report,
            "drawdown": drawdown,
            "statistics": statistics,
            "bot_status": dashboard_state.bot_status,
            "last_update": dashboard_state.last_update,
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
