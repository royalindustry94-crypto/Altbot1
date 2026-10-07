"""
Risk gate. Every order intent passes here AFTER precision rounding and BEFORE submission.
Strategies can propose anything; only this module decides what may reach the exchange.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from . import dec
from .config import AppConfig
from .logging_setup import ev
from .store import ACTIVE_STATUSES, Store
from .strategies.base import OrderIntent

HALT_KEY = "halted_reason"


@dataclass(frozen=True)
class Decision:
    ok: bool
    reason: str = ""


@dataclass
class Snapshot:
    balances: dict[str, dict[str, Decimal]]
    prices: dict[str, Decimal]            # symbol -> last price, for every coin priced this tick
    markets: dict[str, dict[str, Any]]


class RiskManager:
    def __init__(self, cfg: AppConfig, store: Store) -> None:
        self.cfg = cfg
        self.store = store

    # ── halt / kill switch ──────────────────────────────────────────────
    def halted(self) -> str | None:
        if self.cfg.kill_switch_file.exists():
            return f"kill switch file present: {self.cfg.kill_switch_file}"
        return self.store.kv_get(HALT_KEY)

    def halt(self, reason: str) -> None:
        if not self.store.kv_get(HALT_KEY):
            self.store.kv_set(HALT_KEY, reason)
            ev("trading_halted", logging.CRITICAL, reason=reason,
               action="new orders blocked until `python -m altbot resume`")

    def resume(self) -> None:
        reason = self.store.kv_get(HALT_KEY)
        self.store.kv_delete(HALT_KEY)
        self.store.kv_delete(self._peak_key)
        ev("trading_resumed", logging.WARNING, previous_reason=reason, note="peak equity reset")

    # ── equity / drawdown ───────────────────────────────────────────────
    @property
    def _peak_key(self) -> str:
        return f"peak_equity:{self.cfg.mode}"

    def equity(self, snap: Snapshot) -> Decimal | None:
        """Quote + marked-to-market whitelisted coins. None if any held coin is unpriced."""
        total = snap.balances.get(self.cfg.quote, {}).get("total", Decimal(0))
        for coin in self.cfg.whitelist:
            held = snap.balances.get(coin, {}).get("total", Decimal(0))
            if held > 0:
                px = snap.prices.get(self.cfg.market_symbol(coin))
                if px is None:
                    return None   # never compute drawdown on partial data — false halts are bad too
                total += held * px
        return total

    def update_equity(self, snap: Snapshot) -> Decimal | None:
        eq = self.equity(snap)
        if eq is None:
            ev("equity_skipped_missing_price", logging.WARNING)
            return None
        peak = dec(self.store.kv_get(self._peak_key), Decimal(0))
        if eq > peak:
            self.store.kv_set(self._peak_key, str(eq))
            peak = eq
        if peak > 0:
            dd_pct = (peak - eq) / peak * 100
            if dd_pct >= self.cfg.risk.max_drawdown_pct:
                self.halt(f"drawdown {dd_pct:.2f}% >= limit {self.cfg.risk.max_drawdown_pct}% "
                          f"(peak={peak}, equity={eq})")
        return eq

    # ── per-order checks ────────────────────────────────────────────────
    def _open_buy_notional(self, symbol: str | None = None) -> Decimal:
        total = Decimal(0)
        for o in self.store.orders(ACTIVE_STATUSES, symbol=symbol, mode=self.cfg.mode):
            if o["side"] == "buy" and o["price"] is not None:
                total += (o["amount"] - o["filled"]) * o["price"]
        return total

    def check(self, symbol: str, intent: OrderIntent, snap: Snapshot) -> Decision:
        reason = self.halted()
        if reason:
            return Decision(False, f"halted: {reason}")

        base, quote = symbol.split("/")
        if base not in self.cfg.whitelist or quote != self.cfg.quote:
            return Decision(False, f"{symbol} is not whitelisted")
        if intent.side not in ("buy", "sell") or intent.type not in ("market", "limit"):
            return Decision(False, f"unsupported side/type {intent.side}/{intent.type}")
        if intent.amount <= 0:
            return Decision(False, "amount must be > 0")

        last = snap.prices.get(symbol)
        if last is None:
            return Decision(False, "no current price")
        px = intent.price if intent.type == "limit" else last
        notional = intent.amount * px

        limits = (snap.markets.get(symbol) or {}).get("limits") or {}
        min_amt = dec((limits.get("amount") or {}).get("min"))
        min_cost = dec((limits.get("cost") or {}).get("min"))
        if min_amt is not None and intent.amount < min_amt:
            return Decision(False, f"amount {intent.amount} < exchange min {min_amt}")
        if min_cost is not None and notional < min_cost:
            return Decision(False, f"notional {notional} < exchange min {min_cost}")

        if len(self.store.orders(ACTIVE_STATUSES, symbol=symbol, mode=self.cfg.mode)) \
                >= self.cfg.risk.max_open_orders_per_coin:
            return Decision(False, "max open orders for coin reached")

        if intent.side == "sell":
            free = snap.balances.get(base, {}).get("free", Decimal(0))
            if intent.amount > free:
                return Decision(False, f"sell {intent.amount} > free {base} {free} (spot: no shorting)")
            return Decision(True)

        # buys
        quote_free = snap.balances.get(quote, {}).get("free", Decimal(0))
        if notional > quote_free:
            return Decision(False, f"notional {notional} > free {quote} {quote_free}")
        coin_exposure = snap.balances.get(base, {}).get("total", Decimal(0)) * last \
            + self._open_buy_notional(symbol)
        if coin_exposure + notional > self.cfg.risk.max_per_coin_quote:
            return Decision(False, f"per-coin cap: {coin_exposure}+{notional} > {self.cfg.risk.max_per_coin_quote}")
        total_exposure = self._open_buy_notional()
        for coin in self.cfg.whitelist:
            sym = self.cfg.market_symbol(coin)
            held = snap.balances.get(coin, {}).get("total", Decimal(0))
            if held > 0:
                if sym not in snap.prices:
                    return Decision(False, f"cannot value {coin} exposure (no price)")
                total_exposure += held * snap.prices[sym]
        if total_exposure + notional > self.cfg.risk.max_total_exposure_quote:
            return Decision(False, f"total cap: {total_exposure}+{notional} > "
                                   f"{self.cfg.risk.max_total_exposure_quote}")
        return Decision(True)
