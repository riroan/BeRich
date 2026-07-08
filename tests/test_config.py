from src.utils.config import Config


def test_broker_defaults_to_kis(monkeypatch):
    monkeypatch.setattr("src.utils.config.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.delenv("BROKER", raising=False)
    assert Config().get_broker_name() == "kis"


def test_broker_reads_env_lowercase(monkeypatch):
    monkeypatch.setenv("BROKER", "YFINANCE")
    assert Config().get_broker_name() == "yfinance"


def test_trading_mode_defaults_from_kis_paper_flag(monkeypatch):
    monkeypatch.setattr("src.utils.config.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.delenv("TRADING_MODE", raising=False)
    monkeypatch.setenv("KIS_PAPER_TRADING", "false")
    assert Config().get_trading_mode() == "live"


def test_trading_mode_env_overrides_kis_paper_flag(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "PAPER")
    monkeypatch.setenv("KIS_PAPER_TRADING", "false")
    assert Config().get_trading_mode() == "paper"


def test_get_returns_default_for_empty_string():
    config = Config()
    config._settings = {"database": {"url": ""}}
    assert config.get("database.url", "sqlite+aiosqlite:///data/trading.db") == "sqlite+aiosqlite:///data/trading.db"
