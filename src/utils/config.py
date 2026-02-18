import os
import re
from pathlib import Path
from typing import Any
import yaml
from dotenv import load_dotenv


class Config:
    """Configuration manager with YAML and environment variable support"""

    def __init__(self, config_dir: str = "config"):
        self.config_dir = Path(config_dir)
        self._settings: dict[str, Any] = {}
        self._strategies: list = []
        load_dotenv()

    def load(self) -> None:
        """Load configuration files"""
        settings_path = self.config_dir / "settings.yaml"
        if settings_path.exists():
            self._settings = self._load_yaml(settings_path)

        strategies_path = self.config_dir / "strategies.yaml"
        if strategies_path.exists():
            strategies_config = self._load_yaml(strategies_path)
            self._strategies = strategies_config.get("strategies", [])

    def _load_yaml(self, path: Path) -> dict:
        """Load YAML file with environment variable substitution"""
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        # Replace ${VAR_NAME} with environment variables
        pattern = r"\$\{([^}]+)\}"

        def replace_env(match):
            var_name = match.group(1)
            return os.getenv(var_name, "")

        content = re.sub(pattern, replace_env, content)
        return yaml.safe_load(content) or {}

    def get(self, key: str, default: Any = None) -> Any:
        """Get config value using dot notation (e.g., 'broker.kis.app_key')"""
        keys = key.split(".")
        value = self._settings

        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
            else:
                return default
            if value is None:
                return default

        return value

    @property
    def settings(self) -> dict:
        return self._settings

    @property
    def strategies(self) -> list:
        return self._strategies

    def get_kis_config(self) -> dict:
        """Get KIS broker configuration"""
        return {
            "app_key": os.getenv("KIS_APP_KEY", ""),
            "app_secret": os.getenv("KIS_APP_SECRET", ""),
            "account_no": os.getenv("KIS_ACCOUNT_NO", ""),
            "paper_trading": os.getenv("KIS_PAPER_TRADING", "true").lower() == "true",
        }

    def get_risk_config(self) -> dict:
        """Get risk management configuration"""
        return self._settings.get("risk", {})
