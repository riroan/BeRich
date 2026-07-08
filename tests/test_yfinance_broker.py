from decimal import Decimal
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from src.broker.yfinance import YFinanceBroker, row_to_bar, to_yfinance_symbol
from src.core.events import EventBus
from src.core.types import Market, Order, OrderSide, OrderStatus, OrderType


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


@pytest.mark.asyncio
async def test_yfinance_broker_connects(tmp_path):
    broker = YFinanceBroker(EventBus(), state_path=tmp_path / "state.json")
    await broker.connect()
    assert broker.is_connected is True
    assert broker.account_no == "YFINANCE-PAPER"


@pytest.mark.asyncio
async def test_get_current_price_uses_latest_close(monkeypatch, tmp_path):
    broker = YFinanceBroker(EventBus(), state_path=tmp_path / "state.json")
    df = pd.DataFrame({"Close": [10.0, 12.5]}, index=pd.date_range("2026-01-01", periods=2))
    monkeypatch.setattr(broker, "_history", lambda *args, **kwargs: df)
    assert await broker.get_current_price("AAPL", Market.NASDAQ) == Decimal("12.5")


@pytest.mark.asyncio
async def test_get_historical_bars_returns_tail(monkeypatch, tmp_path):
    broker = YFinanceBroker(EventBus(), state_path=tmp_path / "state.json")
    df = pd.DataFrame(
        {
            "Open": [1, 2, 3],
            "High": [2, 3, 4],
            "Low": [0, 1, 2],
            "Close": [1.5, 2.5, 3.5],
            "Volume": [10, 20, 30],
        },
        index=pd.date_range("2026-01-01", periods=3),
    )
    monkeypatch.setattr(broker, "_history", lambda *args, **kwargs: df)
    bars = await broker.get_historical_bars("AAPL", Market.NASDAQ, days=2)
    assert len(bars) == 2
    assert bars[-1].close == Decimal("3.5")


@pytest.mark.asyncio
async def test_submit_buy_order_updates_cash_and_position(monkeypatch, tmp_path):
    broker = YFinanceBroker(
        EventBus(), initial_cash_usd=Decimal("1000"), state_path=tmp_path / "state.json"
    )
    monkeypatch.setattr(broker, "get_current_price", AsyncMock(return_value=Decimal("10")))
    order = Order("AAPL", Market.NASDAQ, OrderSide.BUY, OrderType.MARKET, quantity=5)
    order_id = await broker.submit_order(order)
    assert order_id.startswith("YF-PAPER-")
    assert order.status == OrderStatus.FILLED
    assert broker._cash["usd"] == Decimal("950")
    positions = await broker.get_positions(Market.NASDAQ)
    assert positions[0].quantity == 5


@pytest.mark.asyncio
async def test_submit_buy_rejects_insufficient_cash(monkeypatch, tmp_path):
    broker = YFinanceBroker(
        EventBus(), initial_cash_usd=Decimal("10"), state_path=tmp_path / "state.json"
    )
    monkeypatch.setattr(broker, "get_current_price", AsyncMock(return_value=Decimal("20")))
    order = Order("AAPL", Market.NASDAQ, OrderSide.BUY, OrderType.MARKET, quantity=1)
    await broker.submit_order(order)
    assert order.status == OrderStatus.REJECTED


@pytest.mark.asyncio
async def test_submit_sell_order_updates_cash_and_position(monkeypatch, tmp_path):
    broker = YFinanceBroker(
        EventBus(), initial_cash_usd=Decimal("1000"), state_path=tmp_path / "state.json"
    )
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
