"""
Strategy plug-in contract.

A strategy is PURE DECISION LOGIC: it receives a read-only snapshot and returns
intents. It never talks to the exchange, never touches balances, never sleeps.
Risk checks, idempotency, precision and submission are the engine's job.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, ClassVar, Union

REGISTRY: dict[str, type["Strategy"]] = {}


def register(cls: type["Strategy"]) -> type["Strategy"]:
    if not getattr(cls, "name", None):
        raise TypeError(f"{cls.__name__} must define a class attribute 'name'")
    if cls.name in REGISTRY and REGISTRY[cls.name] is not cls:
        raise TypeError(f"strategy name {cls.name!r} already registered")
    REGISTRY[cls.name] = cls
    return cls


@dataclass(frozen=True)
class OrderIntent:
    side: str                       # "buy" | "sell"
    type: str                       # "market" | "limit"
    amount: Decimal                 # base-asset units (e.g. 0.5 SOL)
    key: str                        # DETERMINISTIC idempotency key, e.g. f"{candle_ts}:entry"
    price: Decimal | None = None    # required for limit orders
    reason: str = ""


@dataclass(frozen=True)
class CancelIntent:
    client_order_id: str
    reason: str = ""


Intent = Union[OrderIntent, CancelIntent]


@dataclass(frozen=True)
class StrategyContext:
    symbol: str                       # "SOL/USDT"
    base: str
    quote: str
    now_ms: int
    candles: list[list[float]]        # CLOSED candles only: [ts_ms, open, high, low, close, volume]
    last_price: Decimal
    base_free: Decimal
    base_total: Decimal
    quote_free: Decimal
    open_orders: list[dict[str, Any]]  # this strategy's own active orders for this symbol
    market: dict[str, Any] = field(default_factory=dict)  # ccxt market (limits, precision)


class Strategy(ABC):
    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    timeframe: ClassVar[str] = "15m"
    lookback: ClassVar[int] = 200
    default_params: ClassVar[dict[str, Any]] = {}

    def __init__(self, symbol: str, params: dict[str, Any] | None = None) -> None:
        params = params or {}
        unknown = set(params) - set(self.default_params)
        if unknown:
            raise ValueError(f"unknown params {sorted(unknown)}; allowed: {sorted(self.default_params)}")
        self.symbol = symbol
        self.params: dict[str, Any] = {**self.default_params, **params}
        self.validate_params()

    def validate_params(self) -> None:
        """Override to raise ValueError on bad parameter values."""

    @abstractmethod
    def on_tick(self, ctx: StrategyContext) -> list[Intent]:
        ...
