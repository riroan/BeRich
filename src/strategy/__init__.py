from .base import BaseStrategy
from .engine import StrategyEngine

# Import all builtin strategies so __subclasses__() discovers them
from .builtin import *  # noqa: F401,F403


def available_strategies() -> dict[str, str]:
    """Return {class_path: display_name} for all registered strategies.

    This is the allowlist for class_path validation.
    """
    result = {}
    for cls in BaseStrategy.__subclasses__():
        module = cls.__module__
        class_path = f"{module}.{cls.__name__}"
        result[class_path] = cls.__name__
    return result


__all__ = ["BaseStrategy", "StrategyEngine", "available_strategies"]
