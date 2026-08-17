from .base import BaseStrategy
from .engine import StrategyEngine

# Import all builtin strategies so __subclasses__() discovers them
from .builtin import *  # noqa: F401,F403


def available_strategies() -> dict[str, str]:
    """Return {class_path: display_name} for all registered strategies.

    This is the allowlist for class_path validation.

    Walks the whole subclass tree, not just the direct children: a strategy
    that specialises another one (RSI_HeikinAshi extends RSI_MeanReversion)
    is still a strategy, and __subclasses__() alone would leave it out of
    the dropdown and out of the allowlist.
    """
    result = {}
    stack = list(BaseStrategy.__subclasses__())
    while stack:
        cls = stack.pop()
        class_path = f"{cls.__module__}.{cls.__name__}"
        if class_path in result:
            continue
        result[class_path] = cls.__name__
        stack.extend(cls.__subclasses__())
    return result


__all__ = ["BaseStrategy", "StrategyEngine", "available_strategies"]
