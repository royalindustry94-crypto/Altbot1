"""
Execution engine: one process, one thread, one deterministic loop.

    boot:  lock ─► reconcile DB vs exchange ─► report orphans ─► loop
    tick:  prices for all coins ─► reconcile ─► balances ─► equity/drawdown
           ─► per coin: strategy ─► dedupe ─► precision ─► risk ─► broker
"""

from __future__ import annotations

import fcntl
import logging
import os
import signal
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import ccxt

from . import dec
from .broker import Broker
from .config import AppConfig
from .exchange import RETRYABLE, call_api
from .logging_setup import ev
from .risk import RiskManager, Snapshot
from .store import ACTIVE_STATUSES, Store
from .strategies import REGISTRY, CancelIntent, OrderIntent, Strategy, StrategyContext


class SingletonLock:
    """OS-level exclusive lock: a second copy of the bot exits immediately. Released by the OS on crash."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            ev("singleton_lock_denied", logging.CRITICAL, lock_file=str(self.path))
            sys.exit(3)
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(str(os.getpid()))
        self._fh.flush()
        ev("singleton_lock_acquired", pid=os.getpid())


class Engine:
    def __init__(self, cfg: AppConfig, exchange: ccxt.Exchange, store: Store, broker: Broker,
                 risk: RiskManager) -> None:
        self.cfg = cfg
        self.ex = exchange
        self.store = store
        self.broker = broker
        self.risk = risk
        self.strategies: dict[str, Strategy] = {
            cfg.market_symbol(c.symbol): REGISTRY[c.strategy](cfg.market_symbol(c.symbol), c.params)
            for c in cfg.coins
        }
        self._stop = False
        self._last_heartbeat = 0.0
        self.tick_count = 0

    # ── lifecycle ────────────────────────────────────────────────────────
    def boot(self) -> None:
        ev("boot", mode=self.cfg.mode, exchange=self.cfg.exchange, quote=self.cfg.quote,
           config_sha256=self.cfg.sha256,
           coins={s: type(st).name for s, st in self.strategies.items()})
        if self.cfg.mode == "live":
            ev("live_mode_armed", logging.WARNING, note="real funds at risk")
        markets = call_api(self.ex.load_markets, op="load_markets")
        for symbol in list(self.strategies):
            m = markets.get(symbol)
            if not m or not m.get("spot") or m.get("active") is False:
                ev("coin_disabled_no_market", logging.ERROR, symbol=symbol,
                   note="not an active spot market on this exchange; skipped")
                del self.strategies[symbol]
        active = self.store.orders(ACTIVE_STATUSES, mode=self.cfg.mode)
        ev("recovery_start", active_orders_in_db=len(active))
        self.broker.reconcile()
        self.broker.scan_orphans(list(self.strategies))
        ev("recovery_done", active_orders=len(self.store.orders(ACTIVE_STATUSES, mode=self.cfg.mode)),
           halted=self.risk.halted())

    def stop(self, *_: Any) -> None:
        ev("shutdown_requested")
        self._stop = True

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        self.boot()
        while not self._stop:
            started = time.monotonic()
            try:
                self.tick()
            except Exception:
                ev("tick_crashed", logging.ERROR, _exc=True)
            remaining = self.cfg.loop_interval_s - (time.monotonic() - started)
            while remaining > 0 and not self._stop:
                time.sleep(min(1.0, remaining))
                remaining -= 1.0
        ev("shutdown_complete", note="resting orders stay on the exchange and are reconciled on next boot")

    # ── one iteration ────────────────────────────────────────────────────
    def _fetch_candles(self, symbol: str, strategy: Strategy) -> tuple[list, Decimal] | None:
        try:
            raw = call_api(self.ex.fetch_ohlcv, symbol, strategy.timeframe, None,
                           strategy.lookback + 1, op="fetch_ohlcv", retries=3)
        except (RETRYABLE + (ccxt.ExchangeError,)) as e:
            ev("market_data_unavailable", logging.WARNING, symbol=symbol, err=str(e)[:200])
            return None
        if not raw:
            return None
        return raw[:-1], dec(raw[-1][4])   # drop the still-forming candle; its close = last price

    def tick(self) -> None:
        self.tick_count += 1
        t0 = time.monotonic()

        market: dict[str, tuple[list, Decimal]] = {}
        for symbol, strat in self.strategies.items():
            data = self._fetch_candles(symbol, strat)
            if data:
                market[symbol] = data
                self.broker.on_price(symbol, data[1])

        try:
            self.broker.reconcile()
            balances = self.broker.balances()
        except (RETRYABLE + (ccxt.ExchangeError,)) as e:
            ev("tick_skipped_account_unavailable", logging.WARNING, err=str(e)[:200])
            return

        snap = Snapshot(balances=balances, prices={s: d[1] for s, d in market.items()},
                        markets=self.ex.markets or {})
        equity = self.risk.update_equity(snap)
        halted = self.risk.halted()

        for symbol, (candles, last) in market.items():
            strat = self.strategies[symbol]
            self._run_strategy(symbol, strat, candles, last, snap, halted)
            snap.balances = self.broker.balances()   # refresh after any fills

        now = time.time()
        if now - self._last_heartbeat >= self.cfg.heartbeat_every_s:
            self._last_heartbeat = now
            self.store.kv_set("last_heartbeat_ms", str(int(now * 1000)))
            ev("heartbeat", tick=self.tick_count, equity=equity, halted=halted,
               priced=len(market), coins=len(self.strategies),
               open_orders=len(self.store.orders(ACTIVE_STATUSES, mode=self.cfg.mode)),
               tick_ms=round((time.monotonic() - t0) * 1000))

    def _run_strategy(self, symbol: str, strat: Strategy, candles: list, last: Decimal,
                      snap: Snapshot, halted: str | None) -> None:
        base, quote = symbol.split("/")
        b = snap.balances.get(base, {})
        own_orders = [o for o in self.store.orders(ACTIVE_STATUSES, symbol=symbol, mode=self.cfg.mode)
                      if o["strategy"] == strat.name]
        ctx = StrategyContext(
            symbol=symbol, base=base, quote=quote, now_ms=int(time.time() * 1000),
            candles=candles, last_price=last,
            base_free=b.get("free", Decimal(0)), base_total=b.get("total", Decimal(0)),
            quote_free=snap.balances.get(quote, {}).get("free", Decimal(0)),
            open_orders=own_orders, market=(self.ex.markets or {}).get(symbol, {}),
        )
        try:
            intents = strat.on_tick(ctx) or []
        except Exception:
            ev("strategy_error", logging.ERROR, _exc=True, symbol=symbol, strategy=strat.name)
            return

        for intent in intents:
            if isinstance(intent, CancelIntent):
                self.broker.cancel(intent.client_order_id, intent.reason)
                continue
            if not isinstance(intent, OrderIntent):
                ev("strategy_bad_intent", logging.ERROR, symbol=symbol, intent=repr(intent))
                continue
            if self.broker.existing(symbol, intent, strat.name):
                continue   # this exact signal was already acted on — idempotent no-op
            if halted:
                ev("order_blocked_halted", logging.WARNING, symbol=symbol, key=intent.key, reason=halted)
                continue
            try:
                intent = self.broker.normalize(symbol, intent)
            except (ccxt.InvalidOrder, ccxt.BadSymbol, ccxt.ArgumentsRequired) as e:
                ev("order_blocked_precision", logging.WARNING, symbol=symbol, key=intent.key, err=str(e)[:200])
                continue
            decision = self.risk.check(symbol, intent, snap)
            if not decision.ok:
                ev("order_blocked_risk", logging.WARNING, symbol=symbol, side=intent.side,
                   amount=intent.amount, price=intent.price, key=intent.key, reason=decision.reason)
                continue
            self.broker.submit(symbol, intent, strat.name)
            snap.balances = self.broker.balances()
