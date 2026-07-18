"""Broker factory for selecting runtime broker implementations."""

from decimal import Decimal

from src.broker.base import Broker
from src.broker.kis.client import KISBroker
from src.broker.paper import PaperBroker
from src.broker.yfinance import YFinanceBroker
from src.core.events import EventBus
from src.utils.config import Config


def create_broker(config: Config, event_bus: EventBus) -> Broker:
    """Create the configured broker implementation."""
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
        real_broker = KISBroker(
            event_bus=event_bus,
            app_key=kis_config["app_key"],
            app_secret=kis_config["app_secret"],
            account_no=kis_config["account_no"],
            paper_trading=kis_config["paper_trading"],
            hts_id=kis_config.get("hts_id", ""),
            slippage_buffer=float(config.get("trading.slippage_buffer_pct", 0.01)),
        )
        if kis_config["paper_trading"]:
            return PaperBroker(
                event_bus=event_bus,
                real_broker=real_broker,
                initial_cash_usd=Decimal(str(config.get("trading.paper_cash_usd", 10000))),
                initial_cash_krw=Decimal(str(config.get("trading.paper_cash_krw", 0))),
            )
        return real_broker

    raise ValueError(f"Unsupported BROKER: {broker_name}")
