"""
Order execution. Two interchangeable brokers share ONE idempotent submit path:

    intent ──► deterministic clientOrderId ──► already in DB? ──yes──► suppressed (no-op)
                                                     │no
                                                     ▼
                                      INSERT status=PENDING (write-ahead, durable)
                                                     ▼
                                      send to exchange / paper simulator
                                                     ▼
                         OPEN/FILLED  |  REJECTED  |  UNKNOWN (timeout: reconcile, never resend)

Because the clientOrderId is a hash of (mode, exchange, strategy, symbol, side, key),
re-running the bot, a crash mid-submit, or a strategy emitting the same signal twice
can never produce a second order for the same signal.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import uuid
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any

import ccxt

from . import dec
from .config import AppConfig
from .exchange import RETRYABLE, call_api
from .logging_setup import ev
from .store import ACTIVE_STATUSES, Store, now_ms
from .strategies.base import OrderIntent

_STATUS_MAP = {"open": "OPEN", "closed": "FILLED", "canceled": "CANCELED",
               "cancelled": "CANCELED", "expired": "CANCELED", "rejected": "REJECTED"}


def make_client_order_id(mode: str, exchange: str, strategy: str, symbol: str, side: str, key: str) -> str:
    """Deterministic UUID-format id (36 chars) — accepted by Binance, Bybit, OKX, Kraken, Coinbase."""
    digest = hashlib.sha256("|".join((mode, exchange, strategy, symbol, side, key)).encode()).digest()
    return str(uuid.UUID(bytes=digest[:16], version=4))


class Broker(ABC):
    def __init__(self, cfg: AppConfig, store: Store, exchange: ccxt.Exchange) -> None:
        self.cfg = cfg
        self.store = store
        self.ex = exchange

    # ── shared idempotent path ─────────────────────────────────────────
    def client_id_for(self, symbol: str, intent: OrderIntent, strategy: str) -> str:
        return make_client_order_id(self.cfg.mode, self.cfg.exchange, strategy, symbol, intent.side, intent.key)

    def existing(self, symbol: str, intent: OrderIntent, strategy: str) -> dict[str, Any] | None:
        return self.store.get_order(self.client_id_for(symbol, intent, strategy))

    def normalize(self, symbol: str, intent: OrderIntent) -> OrderIntent:
        """Round amount/price to the exchange's precision. Raises ccxt.InvalidOrder if it rounds to nothing."""
        amount = dec(self.ex.amount_to_precision(symbol, float(intent.amount)))
        price = intent.price
        if intent.type == "limit":
            if price is None or price <= 0:
                raise ccxt.InvalidOrder("limit order requires a positive price")
            price = dec(self.ex.price_to_precision(symbol, float(price)))
        if amount is None or amount <= 0:
            raise ccxt.InvalidOrder(f"amount {intent.amount} rounds to zero for {symbol}")
        return dataclasses.replace(intent, amount=amount, price=price)

    def submit(self, symbol: str, intent: OrderIntent, strategy: str) -> dict[str, Any]:
        cid = self.client_id_for(symbol, intent, strategy)
        row = {"client_order_id": cid, "mode": self.cfg.mode, "exchange": self.cfg.exchange,
               "symbol": symbol, "side": intent.side, "type": intent.type, "amount": intent.amount,
               "price": intent.price, "strategy": strategy, "intent_key": intent.key,
               "reason": intent.reason, "status": "PENDING"}
        if not self.store.insert_order(row):
            existing = self.store.get_order(cid)
            ev("duplicate_intent_suppressed", client_order_id=cid, symbol=symbol,
               status=existing["status"] if existing else None)
            return existing
        ev("order_intent_recorded", client_order_id=cid, symbol=symbol, side=intent.side,
           type=intent.type, amount=intent.amount, price=intent.price, strategy=strategy,
           key=intent.key, reason=intent.reason)
        self._submit(self.store.get_order(cid))
        return self.store.get_order(cid)

    def _transition(self, cid: str, status: str, **fields: Any) -> None:
        before = self.store.get_order(cid)
        self.store.update_order(cid, status=status, **fields)
        if before and before["status"] != status:
            after = self.store.get_order(cid)
            ev("order_status_changed", client_order_id=cid, symbol=after["symbol"],
               side=after["side"], frm=before["status"], to=status,
               exchange_order_id=after["exchange_order_id"], amount=after["amount"],
               filled=after["filled"], avg_price=after["avg_price"],
               fee_quote=after["fee_quote"], err=after["last_error"])

    # ── per-implementation hooks ───────────────────────────────────────
    @abstractmethod
    def _submit(self, row: dict[str, Any]) -> None: ...

    @abstractmethod
    def cancel(self, client_order_id: str, reason: str = "") -> None: ...

    @abstractmethod
    def reconcile(self) -> None: ...

    @abstractmethod
    def balances(self) -> dict[str, dict[str, Decimal]]: ...

    def on_price(self, symbol: str, last: Decimal) -> None:
        """Paper broker matches resting limit orders here; live broker ignores it."""

    def scan_orphans(self, symbols: list[str]) -> None:
        """Live only: report exchange orders this bot did not create."""


# ════════════════════════════════════════════════════════════════════════
class LiveBroker(Broker):
    """Real orders (testnet or mainnet)."""

    def _apply_exchange_order(self, cid: str, o: dict[str, Any]) -> None:
        status = _STATUS_MAP.get(str(o.get("status") or "").lower(), "OPEN")
        fee = o.get("fee") or {}
        fee_quote = dec(fee.get("cost"), Decimal(0)) if fee.get("currency") == self.cfg.quote else None
        fields: dict[str, Any] = {"exchange_order_id": o.get("id"),
                                  "filled": dec(o.get("filled"), Decimal(0)),
                                  "avg_price": dec(o.get("average")), "last_error": None}
        if fee_quote is not None:
            fields["fee_quote"] = fee_quote
        self._transition(cid, status, **fields)

    def _submit(self, row: dict[str, Any]) -> None:
        cid = row["client_order_id"]
        price = float(row["price"]) if row["price"] is not None else None
        try:
            o = self.ex.create_order(row["symbol"], row["type"], row["side"], float(row["amount"]),
                                     price, {"clientOrderId": cid})
            ev("order_submitted", client_order_id=cid, exchange_order_id=o.get("id"),
               symbol=row["symbol"], side=row["side"], amount=row["amount"], price=row["price"])
            self._apply_exchange_order(cid, o)
        except ccxt.DuplicateOrderId as e:
            # The exchange already has this clientOrderId: an earlier attempt landed.
            self._transition(cid, "UNKNOWN", last_error=f"duplicate_on_exchange: {str(e)[:200]}")
        except (ccxt.InsufficientFunds, ccxt.InvalidOrder, ccxt.BadSymbol) as e:
            self._transition(cid, "REJECTED", last_error=f"{type(e).__name__}: {str(e)[:250]}")
        except ccxt.NetworkError as e:
            # Timeout / 5xx / 429 on a POST: outcome is UNKNOWN. Do NOT resend; reconcile.
            self._transition(cid, "UNKNOWN", last_error=f"{type(e).__name__}: {str(e)[:250]}")
        except ccxt.ExchangeError as e:
            self._transition(cid, "UNKNOWN", last_error=f"{type(e).__name__}: {str(e)[:250]}")

    def _find_on_exchange(self, row: dict[str, Any], open_cache: dict[str, list]) -> dict | None:
        symbol, cid = row["symbol"], row["client_order_id"]
        if row["exchange_order_id"] and self.ex.has.get("fetchOrder"):
            try:
                return call_api(self.ex.fetch_order, row["exchange_order_id"], symbol, op="fetch_order")
            except ccxt.OrderNotFound:
                pass
        if symbol not in open_cache:
            open_cache[symbol] = call_api(self.ex.fetch_open_orders, symbol, op="fetch_open_orders")
        for o in open_cache[symbol]:
            if o.get("clientOrderId") == cid:
                return o
        since = row["created_ms"] - 60_000
        for method in ("fetchClosedOrders", "fetchOrders"):
            if self.ex.has.get(method):
                fn = self.ex.fetch_closed_orders if method == "fetchClosedOrders" else self.ex.fetch_orders
                for o in call_api(fn, symbol, since, op=method):
                    if o.get("clientOrderId") == cid:
                        return o
                break
        return None

    def reconcile(self) -> None:
        open_cache: dict[str, list] = {}
        for row in self.store.orders(ACTIVE_STATUSES, mode=self.cfg.mode):
            cid = row["client_order_id"]
            try:
                found = self._find_on_exchange(row, open_cache)
            except (RETRYABLE + (ccxt.ExchangeError,)) as e:
                ev("reconcile_lookup_failed", logging.WARNING, client_order_id=cid, err=str(e)[:200])
                continue
            if found:
                self._apply_exchange_order(cid, found)
            elif row["status"] in ("PENDING", "UNKNOWN"):
                age_s = (now_ms() - row["created_ms"]) / 1000
                if age_s >= self.cfg.unknown_order_grace_s:
                    self._transition(cid, "REJECTED", last_error="not_found_on_exchange_after_grace")
            else:
                ev("open_order_missing_on_exchange", logging.WARNING, client_order_id=cid,
                   exchange_order_id=row["exchange_order_id"], symbol=row["symbol"])

    def scan_orphans(self, symbols: list[str]) -> None:
        known = self.store.known_client_ids()
        for symbol in symbols:
            try:
                for o in call_api(self.ex.fetch_open_orders, symbol, op="fetch_open_orders"):
                    if o.get("clientOrderId") not in known:
                        ev("orphan_order_on_exchange", logging.WARNING, symbol=symbol,
                           exchange_order_id=o.get("id"), client_order_id=o.get("clientOrderId"),
                           side=o.get("side"), amount=o.get("amount"), price=o.get("price"),
                           note="not created by altbot; left untouched")
            except (RETRYABLE + (ccxt.ExchangeError,)) as e:
                ev("orphan_scan_failed", logging.WARNING, symbol=symbol, err=str(e)[:200])

    def cancel(self, client_order_id: str, reason: str = "") -> None:
        row = self.store.get_order(client_order_id)
        if not row or row["status"] not in ACTIVE_STATUSES:
            return
        if not row["exchange_order_id"]:
            self.reconcile()
            row = self.store.get_order(client_order_id)
            if not row["exchange_order_id"] or row["status"] not in ACTIVE_STATUSES:
                return
        try:
            call_api(self.ex.cancel_order, row["exchange_order_id"], row["symbol"], op="cancel_order")
            ev("order_cancel_sent", client_order_id=client_order_id,
               exchange_order_id=row["exchange_order_id"], reason=reason)
        except ccxt.OrderNotFound:
            ev("order_cancel_not_found", logging.WARNING, client_order_id=client_order_id)
        self.reconcile()

    def balances(self) -> dict[str, dict[str, Decimal]]:
        raw = call_api(self.ex.fetch_balance, op="fetch_balance")
        out = {}
        for asset, total in (raw.get("total") or {}).items():
            bucket = raw.get(asset) or {}
            out[asset] = {"free": dec(bucket.get("free"), Decimal(0)),
                          "used": dec(bucket.get("used"), Decimal(0)),
                          "total": dec(total, Decimal(0))}
        return out


# ════════════════════════════════════════════════════════════════════════
class PaperBroker(Broker):
    """Simulated fills against live public prices. Never calls a private endpoint."""

    def __init__(self, cfg: AppConfig, store: Store, exchange: ccxt.Exchange) -> None:
        super().__init__(cfg, store, exchange)
        self.last_prices: dict[str, Decimal] = {}
        with store.tx():
            if not store.paper_balances():
                store.set_paper_balance(cfg.quote, cfg.risk.paper_starting_quote, Decimal(0))
                ev("paper_wallet_seeded", asset=cfg.quote, amount=cfg.risk.paper_starting_quote)

    def _bal(self, asset: str) -> tuple[Decimal, Decimal]:
        b = self.store.paper_balances().get(asset)
        return (b["free"], b["used"]) if b else (Decimal(0), Decimal(0))

    def _fee(self, notional: Decimal) -> Decimal:
        return notional * self.cfg.risk.paper_fee_bps / Decimal(10_000)

    def _settle(self, row: dict[str, Any], fill_price: Decimal, from_reserved: bool) -> None:
        """Move balances for a full fill. Caller must hold a transaction."""
        base, quote = row["symbol"].split("/")
        amount = row["amount"]
        notional = amount * fill_price
        fee = self._fee(notional)
        bf, bu = self._bal(base)
        qf, qu = self._bal(quote)
        if row["side"] == "buy":
            if from_reserved:
                reserved = row["amount"] * row["price"] * (1 + self.cfg.risk.paper_fee_bps / Decimal(10_000))
                qu -= reserved
                qf += reserved - notional - fee
            else:
                qf -= notional + fee
            bf += amount
        else:
            if from_reserved:
                bu -= amount
            else:
                bf -= amount
            qf += notional - fee
        self.store.set_paper_balance(base, bf, bu)
        self.store.set_paper_balance(quote, qf, qu)
        self._transition(row["client_order_id"], "FILLED", filled=amount, avg_price=fill_price,
                         fee_quote=fee, exchange_order_id=f"paper-{row['client_order_id'][:8]}")

    def _submit(self, row: dict[str, Any]) -> None:
        cid, symbol = row["client_order_id"], row["symbol"]
        last = self.last_prices.get(symbol)
        if last is None:
            self._transition(cid, "REJECTED", last_error="no_price_yet")
            return
        base, quote = symbol.split("/")
        slip = self.cfg.risk.paper_slippage_bps / Decimal(10_000)
        try:
            with self.store.tx():
                if row["type"] == "market":
                    px = last * (1 + slip) if row["side"] == "buy" else last * (1 - slip)
                    qf, _ = self._bal(quote)
                    bf, _ = self._bal(base)
                    if row["side"] == "buy" and qf < row["amount"] * px + self._fee(row["amount"] * px):
                        self._transition(cid, "REJECTED", last_error="InsufficientFunds (paper)")
                        return
                    if row["side"] == "sell" and bf < row["amount"]:
                        self._transition(cid, "REJECTED", last_error="InsufficientFunds (paper)")
                        return
                    self._settle(row, px, from_reserved=False)
                else:
                    if row["side"] == "buy":
                        reserve = row["amount"] * row["price"] * (1 + self.cfg.risk.paper_fee_bps / Decimal(10_000))
                        qf, qu = self._bal(quote)
                        if qf < reserve:
                            self._transition(cid, "REJECTED", last_error="InsufficientFunds (paper)")
                            return
                        self.store.set_paper_balance(quote, qf - reserve, qu + reserve)
                    else:
                        bf, bu = self._bal(base)
                        if bf < row["amount"]:
                            self._transition(cid, "REJECTED", last_error="InsufficientFunds (paper)")
                            return
                        self.store.set_paper_balance(base, bf - row["amount"], bu + row["amount"])
                    self._transition(cid, "OPEN", exchange_order_id=f"paper-{cid[:8]}")
        except Exception as e:
            ev("paper_submit_failed", logging.ERROR, _exc=True, client_order_id=cid)
            self._transition(cid, "REJECTED", last_error=f"paper_error: {str(e)[:200]}")
            return
        if row["type"] == "limit":
            self._match(symbol)

    def _match(self, symbol: str) -> None:
        last = self.last_prices.get(symbol)
        if last is None:
            return
        for row in self.store.orders(("OPEN",), symbol=symbol, mode="paper"):
            crosses = last <= row["price"] if row["side"] == "buy" else last >= row["price"]
            if crosses:
                with self.store.tx():
                    self._settle(row, row["price"], from_reserved=True)

    def on_price(self, symbol: str, last: Decimal) -> None:
        self.last_prices[symbol] = last
        self._match(symbol)

    def cancel(self, client_order_id: str, reason: str = "") -> None:
        row = self.store.get_order(client_order_id)
        if not row or row["status"] != "OPEN":
            return
        base, quote = row["symbol"].split("/")
        with self.store.tx():
            if row["side"] == "buy":
                reserved = row["amount"] * row["price"] * (1 + self.cfg.risk.paper_fee_bps / Decimal(10_000))
                qf, qu = self._bal(quote)
                self.store.set_paper_balance(quote, qf + reserved, qu - reserved)
            else:
                bf, bu = self._bal(base)
                self.store.set_paper_balance(base, bf + row["amount"], bu - row["amount"])
            self._transition(client_order_id, "CANCELED", last_error=f"canceled: {reason}"[:250])

    def reconcile(self) -> None:
        # A PENDING paper row means we crashed between write-ahead and simulation.
        # Nothing ever reached an exchange, so it is safe to close it out.
        for row in self.store.orders(("PENDING", "UNKNOWN"), mode="paper"):
            self._transition(row["client_order_id"], "REJECTED", last_error="crash_before_paper_fill")

    def balances(self) -> dict[str, dict[str, Decimal]]:
        return self.store.paper_balances()


def make_broker(cfg: AppConfig, store: Store, exchange: ccxt.Exchange) -> Broker:
    return PaperBroker(cfg, store, exchange) if cfg.mode == "paper" else LiveBroker(cfg, store, exchange)
