import pytest

from src.utils.config import Config


def test_broker_defaults_to_kis(monkeypatch):
    monkeypatch.setattr("src.utils.config.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.delenv("BROKER", raising=False)
    assert Config().get_broker_name() == "kis"


def test_broker_reads_env_lowercase(monkeypatch):
    monkeypatch.setenv("BROKER", "YFINANCE")
    assert Config().get_broker_name() == "yfinance"


def test_broker_rejects_invalid_value(monkeypatch):
    monkeypatch.setenv("BROKER", "unknown")
    with pytest.raises(ValueError, match="BROKER"):
        Config().get_broker_name()


def test_trading_mode_defaults_to_paper(monkeypatch):
    monkeypatch.setattr("src.utils.config.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.delenv("TRADING_MODE", raising=False)
    assert Config().get_trading_mode() == "paper"


def test_trading_mode_reads_env_lowercase(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "LIVE")
    assert Config().get_trading_mode() == "live"


def test_trading_mode_rejects_invalid_value(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "sandbox")
    with pytest.raises(ValueError, match="TRADING_MODE"):
        Config().get_trading_mode()


def test_load_ignores_legacy_strategies_yaml(tmp_path):
    (tmp_path / "settings.yaml").write_text("risk:\n  max_positions: 5\n")
    (tmp_path / "strategies.yaml").write_text("strategies:\n  - legacy\n")

    config = Config(config_dir=str(tmp_path))
    config.load()

    assert config.get("risk.max_positions") == 5
    assert not hasattr(config, "strategies")


def test_get_returns_default_for_empty_string():
    config = Config()
    config._settings = {"database": {"url": ""}}
    assert (
        config.get("database.url", "sqlite+aiosqlite:///data/trading.db")
        == "sqlite+aiosqlite:///data/trading.db"
    )
