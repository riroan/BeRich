import pytest

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
    with pytest.raises(ValueError, match="BROKER"):
        create_broker(Config(), EventBus())
