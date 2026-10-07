"""
TEMPLATE — copy to e.g. `rsi_momentum.py` (no leading underscore) to make it loadable.

Rules every strategy must follow:
  1. Pure function of `ctx` + params. No network calls, no sleeps, no global state.
  2. `ctx.candles` holds CLOSED candles only — never decide on a still-forming candle.
  3. Every OrderIntent.key must be deterministic for the signal that produced it,
     typically f"{closed_candle_ts}:{purpose}". The engine turns that key into a
     clientOrderId; the same key can only ever produce ONE order, across restarts.
  4. Amounts are base-asset units as Decimal. The risk layer may still reject.
"""

from __future__ import annotations

from decimal import Decimal

from .base import Intent, OrderIntent, Strategy, StrategyContext, register


# @register   # <- uncomment in your copy
class ExampleStrategy(Strategy):
    name = "example"
    description = "Describe the edge in one line."
    timeframe = "1h"
    lookback = 100
    default_params = {"order_quote": "20"}

    def validate_params(self) -> None:
        if Decimal(str(self.params["order_quote"])) <= 0:
            raise ValueError("order_quote must be > 0")

    def on_tick(self, ctx: StrategyContext) -> list[Intent]:
        if len(ctx.candles) < self.lookback:
            return []
        last_closed_ts = int(ctx.candles[-1][0])
        signal = False  # <- your logic here
        if not signal:
            return []
        amount = Decimal(str(self.params["order_quote"])) / ctx.last_price
        return [OrderIntent(side="buy", type="market", amount=amount,
                            key=f"{last_closed_ts}:entry", reason="example signal")]
