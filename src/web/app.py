"""Web dashboard for trading bot monitoring"""

import asyncio
import hashlib
import secrets
import os
from datetime import datetime
from decimal import Decimal
from typing import Optional, Dict, Any, List
from pathlib import Path

from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import logging

logger = logging.getLogger(__name__)

# Base directory for templates and static files
BASE_DIR = Path(__file__).parent


class PositionInfo(BaseModel):
    symbol: str
    market: str
    quantity: int
    avg_price: float
    current_price: float
    pnl: float
    pnl_pct: float
    rsi: Optional[float] = None


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
        self.positions: Dict[str, PositionInfo] = {}
        self.rsi_values: Dict[str, float] = {}
        self.recent_signals: List[Dict[str, Any]] = []
        self.recent_orders: List[Dict[str, Any]] = []
        self.bot_status: Optional[BotStatus] = None
        self.daily_pnl: Decimal = Decimal("0")
        self.total_pnl: Decimal = Decimal("0")
        self.account_value: Decimal = Decimal("0")
        # Separate balances by currency
        self.balance_krw: Decimal = Decimal("0")  # 원화 (총 평가)
        self.balance_usd: Decimal = Decimal("0")  # 달러 (총 평가)
        self.cash_krw: Decimal = Decimal("0")     # 원화 예수금
        self.cash_usd: Decimal = Decimal("0")     # 달러 예수금
        self.last_update: Optional[datetime] = None
        # Price history for charts (symbol -> list of price points)
        self.price_history: Dict[str, List[PricePoint]] = {}
        self.rsi_history: Dict[str, List[Dict[str, Any]]] = {}

    def update_position(
        self,
        symbol: str,
        market: str,
        quantity: int,
        avg_price: float,
        current_price: float,
        rsi: Optional[float] = None,
    ):
        pnl = (current_price - avg_price) * quantity
        pnl_pct = ((current_price - avg_price) / avg_price * 100) if avg_price else 0

        self.positions[symbol] = PositionInfo(
            symbol=symbol,
            market=market,
            quantity=quantity,
            avg_price=avg_price,
            current_price=current_price,
            pnl=pnl,
            pnl_pct=pnl_pct,
            rsi=rsi,
        )
        self.last_update = datetime.now()

    def update_rsi(self, symbol: str, rsi: float):
        self.rsi_values[symbol] = rsi
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
            "timestamp": datetime.now().strftime("%H:%M:%S"),
        })
        # Keep only last 20 signals
        self.recent_signals = self.recent_signals[:20]

    def add_order(self, order_data: Dict[str, Any]):
        self.recent_orders.insert(0, {
            **order_data,
            "timestamp": datetime.now().strftime("%H:%M:%S"),
        })
        # Keep only last 20 orders
        self.recent_orders = self.recent_orders[:20]

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
    from datetime import timedelta
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
        """Main dashboard page"""
        context = {
            "request": request,
            "positions": list(dashboard_state.positions.values()),
            "rsi_values": dict(dashboard_state.rsi_values),
            "recent_signals": list(dashboard_state.recent_signals),
            "recent_orders": list(dashboard_state.recent_orders),
            "bot_status": dashboard_state.bot_status,
            "account_value": float(dashboard_state.account_value),
            "balance_krw": float(dashboard_state.balance_krw),
            "balance_usd": float(dashboard_state.balance_usd),
            "cash_krw": float(dashboard_state.cash_krw),
            "cash_usd": float(dashboard_state.cash_usd),
            "daily_pnl": float(dashboard_state.daily_pnl),
            "total_pnl": float(dashboard_state.total_pnl),
            "last_update": dashboard_state.last_update,
        }
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context=context,
        )

    @app.get("/api/status")
    async def get_status():
        """Get current bot status"""
        return {
            "bot_status": dashboard_state.bot_status,
            "account_value": float(dashboard_state.account_value),
            "balance_krw": float(dashboard_state.balance_krw),
            "balance_usd": float(dashboard_state.balance_usd),
            "cash_krw": float(dashboard_state.cash_krw),
            "cash_usd": float(dashboard_state.cash_usd),
            "daily_pnl": float(dashboard_state.daily_pnl),
            "total_pnl": float(dashboard_state.total_pnl),
            "last_update": dashboard_state.last_update.isoformat()
            if dashboard_state.last_update
            else None,
        }

    @app.get("/api/positions")
    async def get_positions():
        """Get current positions"""
        return list(dashboard_state.positions.values())

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

    @app.get("/symbol/{symbol}", response_class=HTMLResponse)
    async def symbol_detail(request: Request, symbol: str):
        """Symbol detail page with chart"""
        if not verify_session(request):
            return RedirectResponse(url="/login", status_code=302)

        position = dashboard_state.positions.get(symbol)
        rsi = dashboard_state.rsi_values.get(symbol)

        context = {
            "request": request,
            "symbol": symbol,
            "position": position,
            "rsi": rsi,
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

        return {
            "symbol": symbol,
            "prices": [p.model_dump() for p in prices[-limit:]],
            "rsi": rsi[-limit:],
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

    return app


def get_dashboard_state() -> DashboardState:
    """Get the global dashboard state"""
    return dashboard_state
