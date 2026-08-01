"""Internal helpers shared across bot modules"""

import importlib
from typing import Any

from src.core.types import Market


def extract_symbols(cfg_symbols: list) -> list[str]:
    """Extract the *enabled* symbol strings from a strategy_configs `symbols`
    field.

    Symbols may be stored as plain strings or as `{"symbol": "..."}` dicts. A
    dict may carry `"enabled": False` (the dashboard's per-symbol toggle) —
    those are excluded from trading. Plain strings and dicts without the flag
    are treated as enabled.
    """
    result = []
    for s in cfg_symbols:
        if isinstance(s, dict):
            if not s.get("enabled", True):
                continue
            result.append(s["symbol"])
        else:
            result.append(s)
    return result


def extract_symbol_markets(cfg: dict) -> dict[str, Market]:
    """Map each enabled symbol to its market.

    Market lives on the symbol entry so one strategy can hold several
    markets. Entries written before that (plain strings, or dicts without
    `market`) fall back to the config's market.
    """
    default = Market.from_string(cfg["market"]) if cfg.get("market") else None
    markets: dict[str, Market] = {}
    for s in cfg["symbols"]:
        if isinstance(s, dict):
            if not s.get("enabled", True):
                continue
            symbol = s["symbol"]
            raw = s.get("market")
            markets[symbol] = Market.from_string(raw) if raw else default
        else:
            markets[s] = default
    return markets


def build_strategy(cfg: dict) -> Any:
    """Instantiate a strategy from a strategy_configs row."""
    module_path, class_name = cfg["class_path"].rsplit(".", 1)
    strategy_class = getattr(importlib.import_module(module_path), class_name)
    return strategy_class(
        symbols=extract_symbols(cfg["symbols"]),
        symbol_markets=extract_symbol_markets(cfg),
        params=cfg["params"],
        config_name=cfg["name"],
    )
