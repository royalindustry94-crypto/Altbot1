"""Strategy registry. Every module in this package not starting with '_' is auto-loaded."""

from __future__ import annotations

import importlib
import pkgutil

from .base import (REGISTRY, CancelIntent, Intent, OrderIntent, Strategy,
                   StrategyContext, register)

__all__ = ["REGISTRY", "CancelIntent", "Intent", "OrderIntent", "Strategy",
           "StrategyContext", "register", "load_builtin_strategies"]


def load_builtin_strategies() -> dict[str, type[Strategy]]:
    for mod in pkgutil.iter_modules(__path__):
        if not mod.name.startswith("_") and mod.name != "base":
            importlib.import_module(f"{__name__}.{mod.name}")
    return REGISTRY
