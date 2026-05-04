"""Internal helpers shared across bot modules"""

import importlib
from typing import Any

from src.core.types import Market


def extract_symbols(cfg_symbols: list) -> list[str]:
    """Extract symbol strings from a strategy_configs `symbols` field.

    Symbols may be stored as plain strings or as `{"symbol": "..."}` dicts.
    """
    return [s["symbol"] if isinstance(s, dict) else s for s in cfg_symbols]


def build_strategy(cfg: dict) -> Any:
    """Instantiate a strategy from a strategy_configs row."""
    module_path, class_name = cfg["class_path"].rsplit(".", 1)
    strategy_class = getattr(importlib.import_module(module_path), class_name)
    return strategy_class(
        symbols=extract_symbols(cfg["symbols"]),
        market=Market.from_string(cfg["market"]),
        params=cfg["params"],
    )
