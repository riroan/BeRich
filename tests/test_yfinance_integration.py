from decimal import Decimal

import pytest

from src.bot.core import TradingBot
from src.broker.yfinance import YFinanceBroker
from src.core.types import Market


@pytest.mark.asyncio
async def test_yfinance_mode_does_not_require_kis_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("BROKER", "yfinance")
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.delenv("KIS_APP_KEY", raising=False)
    monkeypatch.delenv("KIS_APP_SECRET", raising=False)
    monkeypatch.delenv("KIS_ACCOUNT_NO", raising=False)

    async def fake_get_account_balance(self, market=Market.KRX):
        return {
            "total_eval": Decimal("10000"),
            "cash": Decimal("10000"),
            "stocks_eval": Decimal("0"),
            "profit_loss": Decimal("0"),
        }

    monkeypatch.setattr(YFinanceBroker, "get_account_balance", fake_get_account_balance)

    bot = TradingBot(config_dir=str(tmp_path))
    bot.config.load()
    bot._data_dir = tmp_path
    await bot._initialize_broker()
    assert bot.broker is not None
    assert bot.broker.account_no == "YFINANCE-PAPER"
    await bot.broker.disconnect()
