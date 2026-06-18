# yfinance Paper Mode Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Add a KIS-free paper trading mode that uses yfinance for market data and the existing BeRich paper execution/dashboard flow.

**Architecture:** Introduce an explicit broker/data-source boundary instead of hard-coding `KISBroker` in `TradingBot`. Add a `YFinanceBroker` that implements the same async methods BeRich already calls (`connect`, `get_current_price`, `get_historical_bars`, balances, positions, order submission/cancel). In `BROKER=yfinance` + `TRADING_MODE=paper`, use yfinance for price/history and existing-style simulated fills, without requiring KIS credentials.

**Tech Stack:** Python 3.13, FastAPI dashboard, SQLAlchemy storage, pytest, uv, yfinance.

---

## Current Context

The repo is at `/home/hojinjang/dev/BeRich`.

Observed current behavior:

- `uv sync --extra dev` succeeds.
- `uv run pytest tests/ -v` currently passes: `194 passed`.
- Docker build succeeds.
- Existing `KIS_PAPER_TRADING=true` is not fully offline: `PaperBroker` delegates price/history/auth to `KISBroker`.
- Bot fails with example credentials because KIS auth is still required:
  - `Authentication failed: {"error_code":"EGW00103","error_description":"유효하지 않은 AppKey입니다."}`

Key files:

- `src/bot/core.py`
  - imports `KISBroker` directly.
  - `_initialize_broker()` hard-codes KIS.
- `src/broker/paper.py`
  - simulates order fills.
  - delegates `connect`, `get_current_price`, `get_historical_bars` to a real KIS broker.
- `src/broker/kis/client.py`
  - implicit broker API surface used by the bot/order manager.
- `src/execution/order_manager.py`
  - calls `broker.get_current_price()`, `broker.get_positions()`, `broker.submit_order()`.
- `src/core/types.py`
  - common `Market`, `Order`, `Position`, `Bar`, etc.
- `.env.example`, `pyproject.toml`, tests under `tests/`.

## Assumptions

- Initial goal is **not live trading**.
- Initial yfinance mode must support both US equities/ETFs and Korean equities/ETFs.
- KRX support should include `.KS`/`.KQ` handling. Numeric 6-digit symbols may default to `.KS`, but the implementation should allow explicit `.KQ` symbols/config where needed.
- Paper fills can be immediate at current yfinance price, same as current `PaperBroker` behavior.
- Existing dashboard and strategy code should remain mostly unchanged.
- Avoid broad refactor. Keep compatibility with current KIS behavior.

## Proposed Design

### New config knobs

Add env vars:

```env
BROKER=yfinance        # kis | yfinance
TRADING_MODE=paper     # paper only for yfinance initially
YFINANCE_PRICE_DELAY=0 # optional future knob, not required for first implementation
```

Keep current KIS vars working:

```env
KIS_APP_KEY=...
KIS_APP_SECRET=...
KIS_ACCOUNT_NO=...
KIS_PAPER_TRADING=true
```

Compatibility rule:

- If `BROKER` is missing, default to `kis` so existing behavior is unchanged.
- If `BROKER=yfinance`, force paper-style behavior and never instantiate `KISBroker`.

### Broker API

Create a lightweight typing Protocol to document the implicit interface:

```python
# src/broker/base.py
from typing import Protocol
from decimal import Decimal

from src.core.types import Market, Order, Position, Bar


class Broker(Protocol):
    paper_trading: bool

    @property
    def is_connected(self) -> bool: ...

    @property
    def account_no(self) -> str: ...

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def get_current_price(self, symbol: str, market: Market = Market.KRX) -> Decimal: ...
    async def get_historical_bars(self, symbol: str, market: Market = Market.KRX, days: int = 100) -> list[Bar]: ...
    async def get_positions(self, market: Market = Market.KRX) -> list[Position]: ...
    async def get_account_balance(self, market: Market = Market.KRX) -> dict: ...
    async def submit_order(self, order: Order) -> str: ...
    async def cancel_order(self, order_id: str) -> bool: ...
```

No runtime inheritance needed. This is primarily for clarity/tests/type checking.

### YFinanceBroker behavior

Create `src/broker/yfinance.py`.

Responsibilities:

- Connect/disconnect as no-op state transitions.
- Map BeRich `Market` + symbol to yfinance ticker:
  - `Market.NASDAQ`, `Market.NYSE`, `Market.AMEX`: symbol unchanged, e.g. `AAPL`, `SPY`.
  - `Market.KRX`: initially support explicit suffix if user provides it; otherwise map numeric 6-digit symbol to `{symbol}.KS` as best-effort.
- `get_current_price()`:
  - use `yf.Ticker(ticker).history(period="5d", interval="1d")` or `fast_info` fallback.
  - return latest close/last price as `Decimal`.
- `get_historical_bars()`:
  - use daily history for at least `days` rows, preferably `period=f"{max(days * 2, 120)}d"` to account for weekends/holidays.
  - convert rows to `Bar` with `timeframe="1d"`.
- Paper account state:
  - initial USD and KRW from existing config keys `trading.paper_cash_usd`, `trading.paper_cash_krw`.
  - persist paper cash, positions, orders, and fills across restarts by default using the existing database/storage layer or a small broker-owned JSON state file under `data/` if DB integration is too invasive for v1.
  - keep an explicit reset path for development, but do not reset on ordinary process restart.
- `submit_order()`:
  - immediate fill at `get_current_price()` or order price fallback.
  - update cash/positions.
  - emit `ORDER_FILLED` event like current `PaperBroker`.
- `get_positions()`:
  - return in-memory positions with current yfinance price.
- `get_account_balance()`:
  - return dict with `total_eval`, `cash`, `stocks_eval`, `profit_loss`.
- `cancel_order()`:
  - if pending/submitted order exists, mark cancelled; otherwise return false.

### Broker factory

Create `src/broker/factory.py`:

```python
from decimal import Decimal
from src.core.events import EventBus
from src.utils.config import Config
from src.broker.kis.client import KISBroker
from src.broker.paper import PaperBroker
from src.broker.yfinance import YFinanceBroker


def create_broker(config: Config, event_bus: EventBus):
    broker_name = config.get_broker_name()
    trading_mode = config.get_trading_mode()

    if broker_name == "yfinance":
        if trading_mode != "paper":
            raise ValueError("BROKER=yfinance only supports TRADING_MODE=paper")
        return YFinanceBroker(
            event_bus=event_bus,
            initial_cash_usd=Decimal(str(config.get("trading.paper_cash_usd", 10000))),
            initial_cash_krw=Decimal(str(config.get("trading.paper_cash_krw", 0))),
        )

    if broker_name == "kis":
        kis_config = config.get_kis_config()
        real_broker = KISBroker(...)
        if kis_config["paper_trading"]:
            return PaperBroker(...)
        return real_broker

    raise ValueError(f"Unsupported BROKER: {broker_name}")
```

Then simplify `TradingBot._initialize_broker()` to call factory.

---

## Step-by-Step Plan

### Task 1: Add yfinance dependency

**Objective:** Make yfinance available in both uv/dev and Docker installs.

**Files:**

- Modify: `pyproject.toml`
- Modify: `uv.lock` via `uv lock` or `uv sync`

**Change:**

Add to `[project].dependencies`:

```toml
"yfinance>=0.2.50",
```

**Validation:**

Run:

```bash
uv sync --extra dev
uv run python - <<'PY'
import yfinance as yf
print(yf.__version__)
PY
```

Expected:

- `uv sync` succeeds.
- Python prints a yfinance version.

---

### Task 2: Document the broker Protocol

**Objective:** Make the currently implicit broker interface explicit without changing runtime behavior.

**Files:**

- Create: `src/broker/base.py`
- Modify: `tests/test_broker_base.py` or create if absent

**Implementation:**

Create:

```python
from typing import Protocol
from decimal import Decimal

from src.core.types import Market, Order, Position, Bar


class Broker(Protocol):
    """Async broker interface used by TradingBot and OrderManager."""

    paper_trading: bool

    @property
    def is_connected(self) -> bool:
        """Whether the broker is connected."""
        ...

    @property
    def account_no(self) -> str:
        """Account identifier for logs/dashboard."""
        ...

    async def connect(self) -> None:
        ...

    async def disconnect(self) -> None:
        ...

    async def get_current_price(
        self,
        symbol: str,
        market: Market = Market.KRX,
    ) -> Decimal:
        ...

    async def get_historical_bars(
        self,
        symbol: str,
        market: Market = Market.KRX,
        days: int = 100,
    ) -> list[Bar]:
        ...

    async def get_positions(
        self,
        market: Market = Market.KRX,
    ) -> list[Position]:
        ...

    async def get_account_balance(
        self,
        market: Market = Market.KRX,
    ) -> dict:
        ...

    async def submit_order(self, order: Order) -> str:
        ...

    async def cancel_order(self, order_id: str) -> bool:
        ...
```

**Test:**

Basic import test:

```python
from src.broker.base import Broker


def test_broker_protocol_importable():
    assert Broker is not None
```

**Validation:**

Run:

```bash
uv run pytest tests/test_broker_base.py -v
```

Expected: pass.

---

### Task 3: Add config helpers for broker selection

**Objective:** Centralize broker/mode env parsing while preserving current defaults.

**Files:**

- Modify: `src/utils/config.py`
- Modify: `tests/test_core.py` or create `tests/test_config.py`
- Modify: `.env.example`
- Modify: `README.md` setup section later in Task 10; avoid doing README here if keeping tasks tiny.

**Implementation in `src/utils/config.py`:**

Add methods to `Config`:

```python
    def get_broker_name(self) -> str:
        """Get selected broker implementation."""
        return os.getenv("BROKER", "kis").strip().lower()

    def get_trading_mode(self) -> str:
        """Get trading mode: paper or live."""
        mode = os.getenv("TRADING_MODE")
        if mode:
            return mode.strip().lower()
        return "paper" if os.getenv("KIS_PAPER_TRADING", "true").lower() == "true" else "live"
```

Update `.env.example` near the top:

```env
# Broker selection: kis or yfinance
BROKER=kis

# Trading mode: paper or live. yfinance supports paper only.
TRADING_MODE=paper
```

**Tests:**

Use monkeypatch:

```python
from src.utils.config import Config


def test_broker_defaults_to_kis(monkeypatch):
    monkeypatch.delenv("BROKER", raising=False)
    assert Config().get_broker_name() == "kis"


def test_broker_reads_env_lowercase(monkeypatch):
    monkeypatch.setenv("BROKER", "YFINANCE")
    assert Config().get_broker_name() == "yfinance"


def test_trading_mode_defaults_from_kis_paper_flag(monkeypatch):
    monkeypatch.delenv("TRADING_MODE", raising=False)
    monkeypatch.setenv("KIS_PAPER_TRADING", "false")
    assert Config().get_trading_mode() == "live"
```

**Validation:**

Run:

```bash
uv run pytest tests/test_config.py -v
```

Expected: pass.

---

### Task 4: Implement yfinance symbol mapping and history conversion helpers

**Objective:** Add deterministic helper functions before wiring the full broker.

**Files:**

- Create: `src/broker/yfinance.py`
- Create: `tests/test_yfinance_broker.py`

**Implementation skeleton:**

```python
from datetime import datetime
from decimal import Decimal
from uuid import uuid4
import logging

import pandas as pd
import yfinance as yf

from src.core.events import EventBus, Event, EventType
from src.core.types import Bar, Fill, Market, Order, OrderSide, OrderStatus, Position

logger = logging.getLogger("TradingBot")


def to_yfinance_symbol(symbol: str, market: Market) -> str:
    symbol = symbol.strip().upper()
    if market in {Market.NASDAQ, Market.NYSE, Market.AMEX}:
        return symbol
    if market == Market.KRX:
        if symbol.endswith((".KS", ".KQ")):
            return symbol
        if symbol.isdigit() and len(symbol) == 6:
            return f"{symbol}.KS"
    return symbol


def row_to_bar(symbol: str, market: Market, timestamp, row) -> Bar:
    return Bar(
        symbol=symbol,
        market=market,
        open=Decimal(str(row["Open"])),
        high=Decimal(str(row["High"])),
        low=Decimal(str(row["Low"])),
        close=Decimal(str(row["Close"])),
        volume=int(row.get("Volume", 0) or 0),
        timestamp=timestamp.to_pydatetime() if hasattr(timestamp, "to_pydatetime") else timestamp,
        timeframe="1d",
    )
```

**Tests:**

```python
from decimal import Decimal
import pandas as pd

from src.broker.yfinance import to_yfinance_symbol, row_to_bar
from src.core.types import Market


def test_to_yfinance_symbol_us_markets_unchanged():
    assert to_yfinance_symbol("aapl", Market.NASDAQ) == "AAPL"
    assert to_yfinance_symbol("spy", Market.AMEX) == "SPY"


def test_to_yfinance_symbol_krx_numeric_defaults_to_ks():
    assert to_yfinance_symbol("005930", Market.KRX) == "005930.KS"


def test_to_yfinance_symbol_krx_suffix_preserved():
    assert to_yfinance_symbol("091990.KQ", Market.KRX) == "091990.KQ"


def test_row_to_bar_converts_ohlcv():
    row = pd.Series({"Open": 1.1, "High": 2.2, "Low": 1.0, "Close": 2.0, "Volume": 123})
    ts = pd.Timestamp("2026-01-02")
    bar = row_to_bar("AAPL", Market.NASDAQ, ts, row)
    assert bar.symbol == "AAPL"
    assert bar.close == Decimal("2.0")
    assert bar.volume == 123
    assert bar.timeframe == "1d"
```

**Validation:**

Run:

```bash
uv run pytest tests/test_yfinance_broker.py -v
```

Expected: pass.

---

### Task 5: Implement `YFinanceBroker` read methods

**Objective:** Support connection state, current price, historical bars, and paper account balance reads.

**Files:**

- Modify: `src/broker/yfinance.py`
- Modify: `tests/test_yfinance_broker.py`

**Implementation notes:**

Add class:

```python
class YFinanceBroker:
    """KIS-free paper broker using yfinance market data."""

    def __init__(
        self,
        event_bus: EventBus,
        initial_cash_krw: Decimal = Decimal("0"),
        initial_cash_usd: Decimal = Decimal("10000"),
    ):
        self.event_bus = event_bus
        self.paper_trading = True
        self._connected = False
        self._cash = {"krw": initial_cash_krw, "usd": initial_cash_usd}
        self._positions: dict[str, dict] = {}
        self._orders: dict[str, Order] = {}
        self._fills: list[Fill] = []

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def account_no(self) -> str:
        return "YFINANCE-PAPER"

    async def connect(self) -> None:
        self._connected = True
        logger.info("YFinance paper broker connected")
        await self.event_bus.publish(Event(
            event_type=EventType.BROKER_CONNECTED,
            data={"broker": "yfinance", "paper_trading": True},
            timestamp=datetime.now(),
            source="YFinanceBroker",
        ))

    async def disconnect(self) -> None:
        self._connected = False
        logger.info("YFinance paper broker disconnected")

    def _history(self, symbol: str, market: Market, period: str, interval: str) -> pd.DataFrame:
        ticker = to_yfinance_symbol(symbol, market)
        return yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=False)

    async def get_current_price(self, symbol: str, market: Market = Market.KRX) -> Decimal:
        df = self._history(symbol, market, period="5d", interval="1d")
        if df.empty:
            raise ValueError(f"No yfinance price data for {symbol} ({market.value})")
        close = df["Close"].dropna().iloc[-1]
        return Decimal(str(close))

    async def get_historical_bars(self, symbol: str, market: Market = Market.KRX, days: int = 100) -> list[Bar]:
        period_days = max(days * 2, 120)
        df = self._history(symbol, market, period=f"{period_days}d", interval="1d")
        if df.empty:
            raise ValueError(f"No yfinance historical data for {symbol} ({market.value})")
        bars = [row_to_bar(symbol, market, idx, row) for idx, row in df.tail(days).iterrows()]
        return bars
```

Add balance and positions read methods:

```python
    async def get_positions(self, market: Market = Market.KRX) -> list[Position]:
        positions = []
        for symbol, pos in self._positions.items():
            if pos["market"] != market or pos["quantity"] <= 0:
                continue
            current_price = await self.get_current_price(symbol, market)
            positions.append(Position(
                symbol=symbol,
                market=market,
                quantity=pos["quantity"],
                avg_entry_price=pos["avg_price"],
                current_price=current_price,
                unrealized_pnl=(current_price - pos["avg_price"]) * pos["quantity"],
            ))
        return positions

    async def get_account_balance(self, market: Market = Market.KRX) -> dict:
        cash_key = "krw" if market == Market.KRX else "usd"
        positions = await self.get_positions(market)
        stocks_eval = sum((p.current_price * p.quantity for p in positions), Decimal("0"))
        profit_loss = sum((p.unrealized_pnl for p in positions), Decimal("0"))
        cash = self._cash[cash_key]
        return {
            "total_eval": cash + stocks_eval,
            "cash": cash,
            "stocks_eval": stocks_eval,
            "profit_loss": profit_loss,
        }
```

**Tests:**

Patch `_history` to avoid network:

```python
from decimal import Decimal
import pandas as pd
import pytest

from src.broker.yfinance import YFinanceBroker
from src.core.events import EventBus
from src.core.types import Market


@pytest.mark.asyncio
async def test_yfinance_broker_connects():
    broker = YFinanceBroker(EventBus())
    await broker.connect()
    assert broker.is_connected is True
    assert broker.account_no == "YFINANCE-PAPER"


@pytest.mark.asyncio
async def test_get_current_price_uses_latest_close(monkeypatch):
    broker = YFinanceBroker(EventBus())
    df = pd.DataFrame({"Close": [10.0, 12.5]}, index=pd.date_range("2026-01-01", periods=2))
    monkeypatch.setattr(broker, "_history", lambda *args, **kwargs: df)
    assert await broker.get_current_price("AAPL", Market.NASDAQ) == Decimal("12.5")


@pytest.mark.asyncio
async def test_get_historical_bars_returns_tail(monkeypatch):
    broker = YFinanceBroker(EventBus())
    df = pd.DataFrame(
        {"Open": [1, 2, 3], "High": [2, 3, 4], "Low": [0, 1, 2], "Close": [1.5, 2.5, 3.5], "Volume": [10, 20, 30]},
        index=pd.date_range("2026-01-01", periods=3),
    )
    monkeypatch.setattr(broker, "_history", lambda *args, **kwargs: df)
    bars = await broker.get_historical_bars("AAPL", Market.NASDAQ, days=2)
    assert len(bars) == 2
    assert bars[-1].close == Decimal("3.5")
```

**Validation:**

Run:

```bash
uv run pytest tests/test_yfinance_broker.py -v
```

Expected: pass.

---

### Task 6: Implement `YFinanceBroker` paper order execution

**Objective:** Make yfinance broker usable by `OrderManager` for simulated fills.

**Files:**

- Modify: `src/broker/yfinance.py`
- Modify: `tests/test_yfinance_broker.py`

**Implementation:**

Port the minimal order execution behavior from `PaperBroker`, but keep it self-contained.

Important behavior:

- BUY reduces cash and creates/averages position.
- SELL requires existing quantity and increases cash.
- Filled orders emit `ORDER_FILLED` with the `Order` as data, same as current `PaperBroker`.
- Rejected orders return an order id with `OrderStatus.REJECTED`.

**Core code shape:**

```python
    async def submit_order(self, order: Order) -> str:
        order_id = f"YF-PAPER-{uuid4().hex[:8].upper()}"
        order.order_id = order_id
        order.status = OrderStatus.SUBMITTED
        fill_price = order.price or await self.get_current_price(order.symbol, order.market)
        cash_key = "krw" if order.market == Market.KRX else "usd"
        pnl = None

        if order.side == OrderSide.BUY:
            cost = fill_price * order.quantity
            if cost > self._cash[cash_key]:
                order.status = OrderStatus.REJECTED
                self._orders[order_id] = order
                return order_id
            self._cash[cash_key] -= cost
            pos = self._positions.get(order.symbol)
            if pos:
                old_qty = pos["quantity"]
                new_qty = old_qty + order.quantity
                pos["avg_price"] = ((pos["avg_price"] * old_qty) + (fill_price * order.quantity)) / new_qty
                pos["quantity"] = new_qty
            else:
                self._positions[order.symbol] = {
                    "quantity": order.quantity,
                    "avg_price": fill_price,
                    "market": order.market,
                }

        elif order.side == OrderSide.SELL:
            pos = self._positions.get(order.symbol)
            if not pos or pos["quantity"] < order.quantity:
                order.status = OrderStatus.REJECTED
                self._orders[order_id] = order
                return order_id
            proceeds = fill_price * order.quantity
            self._cash[cash_key] += proceeds
            pnl = (fill_price - pos["avg_price"]) * order.quantity
            pos["quantity"] -= order.quantity
            if pos["quantity"] <= 0:
                del self._positions[order.symbol]

        order.status = OrderStatus.FILLED
        order.filled_quantity = order.quantity
        order.filled_avg_price = fill_price
        self._orders[order_id] = order
        self._fills.append(Fill(
            order_id=order_id,
            symbol=order.symbol,
            market=order.market,
            side=order.side,
            quantity=order.quantity,
            price=fill_price,
            commission=Decimal("0"),
            timestamp=datetime.now(),
            pnl=pnl,
        ))
        await self.event_bus.publish(Event(
            event_type=EventType.ORDER_FILLED,
            data=order,
            timestamp=datetime.now(),
            source="YFinanceBroker",
        ))
        return order_id

    async def cancel_order(self, order_id: str) -> bool:
        order = self._orders.get(order_id)
        if not order or order.status == OrderStatus.FILLED:
            return False
        order.status = OrderStatus.CANCELLED
        return True
```

**Tests:**

```python
from src.core.types import Order, OrderSide, OrderType, OrderStatus


@pytest.mark.asyncio
async def test_submit_buy_order_updates_cash_and_position(monkeypatch):
    broker = YFinanceBroker(EventBus(), initial_cash_usd=Decimal("1000"))
    monkeypatch.setattr(broker, "get_current_price", AsyncMock(return_value=Decimal("10")))
    order = Order("AAPL", Market.NASDAQ, OrderSide.BUY, OrderType.MARKET, quantity=5)
    order_id = await broker.submit_order(order)
    assert order_id.startswith("YF-PAPER-")
    assert order.status == OrderStatus.FILLED
    assert broker._cash["usd"] == Decimal("950")
    positions = await broker.get_positions(Market.NASDAQ)
    assert positions[0].quantity == 5


@pytest.mark.asyncio
async def test_submit_buy_rejects_insufficient_cash(monkeypatch):
    broker = YFinanceBroker(EventBus(), initial_cash_usd=Decimal("10"))
    monkeypatch.setattr(broker, "get_current_price", AsyncMock(return_value=Decimal("20")))
    order = Order("AAPL", Market.NASDAQ, OrderSide.BUY, OrderType.MARKET, quantity=1)
    await broker.submit_order(order)
    assert order.status == OrderStatus.REJECTED


@pytest.mark.asyncio
async def test_submit_sell_order_updates_cash_and_position(monkeypatch):
    broker = YFinanceBroker(EventBus(), initial_cash_usd=Decimal("1000"))
    monkeypatch.setattr(broker, "get_current_price", AsyncMock(return_value=Decimal("10")))
    buy = Order("AAPL", Market.NASDAQ, OrderSide.BUY, OrderType.MARKET, quantity=5)
    await broker.submit_order(buy)
    monkeypatch.setattr(broker, "get_current_price", AsyncMock(return_value=Decimal("12")))
    sell = Order("AAPL", Market.NASDAQ, OrderSide.SELL, OrderType.MARKET, quantity=2)
    await broker.submit_order(sell)
    assert sell.status == OrderStatus.FILLED
    assert broker._cash["usd"] == Decimal("974")
    positions = await broker.get_positions(Market.NASDAQ)
    assert positions[0].quantity == 3
```

**Validation:**

Run:

```bash
uv run pytest tests/test_yfinance_broker.py -v
```

Expected: pass.

---

### Task 7: Persist yfinance paper state across restarts

**Objective:** Make paper cash, positions, orders, and fills survive ordinary process restarts.

**Files:**

- Modify: `src/broker/yfinance.py`
- Modify: `tests/test_yfinance_broker.py`
- Possibly create: `data/yfinance_paper_state.json` at runtime only; do not commit runtime state.

**Implementation approach:**

Use a small JSON state file as the v1 persistence layer because it is broker-local and avoids a larger DB schema migration. Default path:

```text
data/yfinance_paper_state.json
```

Add optional constructor arg:

```python
state_path: str | Path = "data/yfinance_paper_state.json"
```

Behavior:

- On `connect()`, load the state file if it exists.
- If it does not exist, initialize from configured paper cash.
- After every accepted/rejected order state change, save state atomically.
- Persist Decimals as strings.
- Persist markets/order statuses by enum value.
- Do not persist transient connection state.
- Add a helper method `reset_state()` for tests/dev, but do not call it during normal startup.

**Tests:**

- Create broker with temp `state_path`.
- Buy a position.
- Instantiate a new broker with the same `state_path`.
- Connect/load and assert cash and position are restored.

Example assertion shape:

```python
@pytest.mark.asyncio
async def test_yfinance_paper_state_persists_across_restart(tmp_path, monkeypatch):
    state_path = tmp_path / "paper_state.json"
    broker1 = YFinanceBroker(EventBus(), initial_cash_usd=Decimal("1000"), state_path=state_path)
    monkeypatch.setattr(broker1, "get_current_price", AsyncMock(return_value=Decimal("10")))
    await broker1.connect()
    await broker1.submit_order(Order("AAPL", Market.NASDAQ, OrderSide.BUY, OrderType.MARKET, quantity=5))

    broker2 = YFinanceBroker(EventBus(), initial_cash_usd=Decimal("1000"), state_path=state_path)
    monkeypatch.setattr(broker2, "get_current_price", AsyncMock(return_value=Decimal("10")))
    await broker2.connect()
    positions = await broker2.get_positions(Market.NASDAQ)

    assert broker2._cash["usd"] == Decimal("950")
    assert positions[0].symbol == "AAPL"
    assert positions[0].quantity == 5
```

**Validation:**

Run:

```bash
uv run pytest tests/test_yfinance_broker.py -v
```

Expected: pass.

---

### Task 8: Add broker factory and preserve KIS behavior

**Objective:** Select KIS or yfinance from config without changing the rest of the bot.

**Files:**

- Create: `src/broker/factory.py`
- Modify: `tests/test_broker_factory.py`

**Implementation:**

Implement `create_broker(config, event_bus)`.

Key requirements:

- `BROKER=yfinance` returns `YFinanceBroker` and does not construct `KISBroker`.
- `BROKER=kis` preserves existing KIS + `PaperBroker` wrapping behavior.
- Unknown broker raises `ValueError`.
- `BROKER=yfinance` with `TRADING_MODE=live` raises `ValueError`.

**Tests:**

Use monkeypatch env and avoid calling network.

```python
from src.broker.factory import create_broker
from src.broker.yfinance import YFinanceBroker
from src.core.events import EventBus
from src.utils.config import Config


def test_factory_returns_yfinance_broker(monkeypatch):
    monkeypatch.setenv("BROKER", "yfinance")
    monkeypatch.setenv("TRADING_MODE", "paper")
    broker = create_broker(Config(), EventBus())
    assert isinstance(broker, YFinanceBroker)


def test_factory_rejects_yfinance_live(monkeypatch):
    monkeypatch.setenv("BROKER", "yfinance")
    monkeypatch.setenv("TRADING_MODE", "live")
    with pytest.raises(ValueError, match="paper"):
        create_broker(Config(), EventBus())


def test_factory_rejects_unknown_broker(monkeypatch):
    monkeypatch.setenv("BROKER", "unknown")
    with pytest.raises(ValueError, match="Unsupported BROKER"):
        create_broker(Config(), EventBus())
```

**Validation:**

Run:

```bash
uv run pytest tests/test_broker_factory.py -v
```

Expected: pass.

---

### Task 9: Wire `TradingBot` to broker factory

**Objective:** Remove direct KIS construction from bot initialization path.

**Files:**

- Modify: `src/bot/core.py`
- Modify: tests that assert bot initialization behavior if present, likely `tests/test_bot.py`

**Implementation:**

Change imports:

```python
# remove or stop using this for type annotation only
from src.broker.kis.client import KISBroker

# add
from src.broker.base import Broker
from src.broker.factory import create_broker
```

Change property annotation:

```python
self.broker: Broker | None = None
```

Replace `_initialize_broker()` body construction section with:

```python
    async def _initialize_broker(self) -> None:
        """Initialize broker connection"""
        self.broker = create_broker(self.config, self.event_bus)
        await self.broker.connect()

        try:
            krw_balance = await self.broker.get_account_balance(Market.KRX)
            ... existing balance/dashboard code unchanged ...
```

Keep existing dashboard balance update code as-is.

**Tests:**

Patch `create_broker` in `src.bot.core` and assert it is called by `_initialize_broker()`.

```python
@pytest.mark.asyncio
async def test_initialize_broker_uses_factory(monkeypatch):
    bot = TradingBot()
    broker = AsyncMock()
    broker.get_account_balance = AsyncMock(return_value={"total_eval": Decimal("1000"), "cash": Decimal("1000"), "profit_loss": Decimal("0")})
    monkeypatch.setattr("src.bot.core.create_broker", lambda config, event_bus: broker)
    await bot._initialize_broker()
    broker.connect.assert_awaited_once()
    assert bot.broker is broker
```

Adjust for existing test fixtures/imports.

**Validation:**

Run:

```bash
uv run pytest tests/test_bot.py -v
uv run pytest tests/test_broker_factory.py tests/test_yfinance_broker.py -v
```

Expected: pass.

---

### Task 10: Integration smoke test for yfinance mode without KIS env

**Objective:** Prove BeRich can initialize with yfinance paper mode without KIS credentials.

**Files:**

- Create or modify: `tests/test_yfinance_integration.py`

**Approach:**

Avoid real network by monkeypatching `YFinanceBroker._history` or `get_current_price/get_historical_bars`.

Test that:

- `BROKER=yfinance`
- `TRADING_MODE=paper`
- KIS env vars removed
- `TradingBot._initialize_broker()` succeeds
- selected broker account is `YFINANCE-PAPER`

**Test shape:**

```python
@pytest.mark.asyncio
async def test_yfinance_mode_does_not_require_kis_credentials(monkeypatch):
    monkeypatch.setenv("BROKER", "yfinance")
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.delenv("KIS_APP_KEY", raising=False)
    monkeypatch.delenv("KIS_APP_SECRET", raising=False)
    monkeypatch.delenv("KIS_ACCOUNT_NO", raising=False)

    async def fake_get_account_balance(self, market=Market.KRX):
        return {"total_eval": Decimal("10000"), "cash": Decimal("10000"), "stocks_eval": Decimal("0"), "profit_loss": Decimal("0")}

    monkeypatch.setattr(YFinanceBroker, "get_account_balance", fake_get_account_balance)

    bot = TradingBot()
    await bot._initialize_broker()
    assert bot.broker.account_no == "YFINANCE-PAPER"
    await bot.broker.disconnect()
```

**Validation:**

Run:

```bash
uv run pytest tests/test_yfinance_integration.py -v
```

Expected: pass.

---

### Task 11: Update documentation and examples

**Objective:** Make yfinance paper mode discoverable and easy to run.

**Files:**

- Modify: `.env.example`
- Modify: `README.md`

**README additions:**

Add a section before KIS setup or inside install/run:

```markdown
### KIS 없이 yfinance paper mode로 실행

KIS API 키 없이 UI와 RSI 전략을 paper trading으로 테스트하려면 `.env`에서:

```env
BROKER=yfinance
TRADING_MODE=paper
KIS_PAPER_TRADING=true
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=change-me
```

이 모드는 yfinance에서 시세/일봉 데이터를 가져오고, 주문은 메모리 상에서 즉시 체결 처리합니다. 실제 주문은 발생하지 않습니다.

실행:

```bash
uv run python scripts/run_bot.py --web --web-port 9095
```

또는 Docker:

```bash
docker compose up -d --build
```

주의:

- yfinance 데이터는 지연/누락될 수 있어 실거래 판단용으로 쓰면 안 됩니다.
- KRX 종목은 yfinance suffix가 필요할 수 있습니다. 예: `005930.KS`, `091990.KQ`.
- yfinance broker는 현재 live trading을 지원하지 않습니다.
```

**Validation:**

Run:

```bash
uv run pytest tests/ -v
```

Expected: all tests pass.

---

### Task 12: Local runtime verification with real yfinance network

**Objective:** Actually run the app in yfinance paper mode and verify dashboard responds.

**Files:**

- Modify local `.env` only after confirmation because it is environment/config write.

**Manual `.env` change:**

Set:

```env
BROKER=yfinance
TRADING_MODE=paper
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=<local-password>
```

Leave KIS values unused.

**Commands:**

Stop old mock dashboard if still running:

```bash
# Identify Hermes background proc if needed, or stop process using port 9095 after confirming scope.
ss -ltnp | grep ':9095'
```

Run direct dev server:

```bash
uv run python scripts/run_bot.py --web --web-port 9095
```

Expected logs:

```text
YFinance paper broker connected
Trading Bot initialized successfully
Web dashboard started at http://localhost:9095
```

Verify HTTP:

```bash
curl -sS -L -o /tmp/berich_yfinance_home.html -w '%{http_code} %{content_type}\n' http://127.0.0.1:9095/
grep -o '<title>[^<]*' /tmp/berich_yfinance_home.html | head -1
```

Expected:

```text
200 text/html; charset=utf-8
<title>BeRich Dashboard
```

**Note:** If app redirects to login because auth is enabled, `200` login page is still acceptable; verify the process logs have yfinance broker connected.

---

### Task 13: Docker verification

**Objective:** Ensure container image still builds and runs with yfinance mode.

**Commands:**

```bash
docker compose build
docker compose up -d
sleep 10
docker compose ps
docker logs --tail 100 quant-bot
```

Expected:

- build succeeds.
- MySQL is healthy.
- `quant-bot` stays running.
- logs show `YFinance paper broker connected`, not KIS auth failure.

If `logs/` or `data/` permissions recur:

```bash
mkdir -p logs data
sudo chown -R 999:999 logs data
```

Then:

```bash
docker compose restart trading-bot
```

---

## Files Likely to Change

- `pyproject.toml`
- `uv.lock`
- `.env.example`
- `README.md`
- `src/utils/config.py`
- `src/broker/base.py` new
- `src/broker/yfinance.py` new
- `src/broker/factory.py` new
- `src/bot/core.py`
- tests:
  - `tests/test_config.py`
  - `tests/test_broker_base.py`
  - `tests/test_yfinance_broker.py`
  - `tests/test_broker_factory.py`
  - `tests/test_yfinance_integration.py`
  - possibly `tests/test_bot.py`

## Test / Validation Matrix

Run incrementally:

```bash
uv run pytest tests/test_config.py -v
uv run pytest tests/test_broker_base.py -v
uv run pytest tests/test_yfinance_broker.py -v
uv run pytest tests/test_broker_factory.py -v
uv run pytest tests/test_yfinance_integration.py -v
uv run pytest tests/test_bot.py -v
```

Final full suite:

```bash
uv run pytest tests/ -v
```

Runtime smoke:

```bash
BROKER=yfinance TRADING_MODE=paper uv run python scripts/run_bot.py --web --web-port 9095
curl -sS -L -o /tmp/berich_home.html -w '%{http_code} %{content_type}\n' http://127.0.0.1:9095/
```

Docker:

```bash
docker compose build
docker compose up -d
docker compose ps
docker logs --tail 100 quant-bot
```

## Risks / Tradeoffs

1. **yfinance is not a broker.** It is data only. This mode must remain paper-only.
2. **Data quality/latency.** yfinance data can be delayed, adjusted, missing, or rate-limited. Good for prototyping, not production trading.
3. **KRX symbol mapping.** `.KS` vs `.KQ` cannot be inferred perfectly from a 6-digit code. Default `.KS` is best-effort only, so explicit `.KQ` symbols should be supported for KOSDAQ.
4. **Persistent paper state.** Persisting paper cash/positions/orders/fills in JSON is simpler than a DB migration, but it requires atomic writes and clear reset tooling to avoid corrupted or stale simulation state.
5. **Existing `PaperBroker` duplication.** `YFinanceBroker` will duplicate some paper execution logic. This is acceptable for minimal change, but later refactor could extract `PaperExecutionMixin` or make `PaperBroker` accept any data broker without requiring auth.
6. **Scheduler is currently `TradingScheduler(interval_seconds=60, us_only=True)`.** Supporting Korea as well as US may require revisiting market-hours logic so KRX strategies are not skipped unintentionally.

## Confirmed Decisions

1. Support both US and Korean symbols in yfinance paper mode.
2. Persist paper positions/state across ordinary restarts by default.
3. Update local `.env` to use `BROKER=yfinance` during implementation while leaving KIS values unused.

## Recommended v1 Scope

- Implement `BROKER=yfinance`, `TRADING_MODE=paper`.
- Support US symbols robustly.
- Support Korean symbols with explicit `.KS`/`.KQ` and numeric-code best-effort mapping.
- Persist paper cash/positions/orders/fills across restarts by default.
- Do not touch live trading.
- Verify direct dev run and Docker run.
