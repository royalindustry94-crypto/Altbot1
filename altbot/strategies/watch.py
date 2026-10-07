"""'watch' — observe-only. Lets a coin be whitelisted and tracked before it gets a real strategy."""

from __future__ import annotations

from .base import Intent, Strategy, StrategyContext, register


@register
class WatchOnly(Strategy):
    name = "watch"
    description = "Observe only: tracks price/balances for the coin, never places orders."
    timeframe = "15m"
    lookback = 2

    def on_tick(self, ctx: StrategyContext) -> list[Intent]:
        return []
