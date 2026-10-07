from __future__ import annotations

import itertools
import sys
from decimal import Decimal
from pathlib import Path

import ccxt
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from altbot import exchange as exchange_mod  # noqa: E402
from altbot.config import load_config  # noqa: E402
from altbot.logging_setup import setup_logging  # noqa: E402
from altbot.strategies import OrderIntent, Strategy, register  # noqa: E402

exchange_mod._sleep = lambda s: None   # no real backoff sleeps in tests


@register
class ScriptedStrategy(Strategy):
    """Test-only: emits whatever intents the test queued, every tick, until cleared."""
    name = "scripted"
    timeframe = "1m"
    lookback = 3
    queued: list = []

    def on_tick(self, ctx):
        return list(type(self).queued)


class FakeExchange:
    """Minimal ccxt-shaped exchange. Orders 'land' even when we simulate a timeout."""

    id = "binance"
    rateLimit = 50

    def __init__(self):
        self.markets = {
            "SOL/USDT": {"symbol": "SOL/USDT", "spot": True, "active": True,
                         "limits": {"amount": {"min": 0.001}, "cost": {"min": 5}}},
            "LINK/USDT": {"symbol": "LINK/USDT", "spot": True, "active": True,
                          "limits": {"amount": {"min": 0.01}, "cost": {"min": 5}}},
        }
        self.has = {"fetchOrder": True, "fetchClosedOrders": True}
        self.price = {"SOL/USDT": 100.0, "LINK/USDT": 10.0}
        self.orders: dict[str, dict] = {}
        self.create_calls = 0
        self.fail_next_create: Exception | None = None
        self.land_on_failure = True
        self._ids = itertools.count(1)
        self.balance = {"USDT": {"free": 1000.0, "used": 0.0, "total": 1000.0}}

    def load_markets(self):
        return self.markets

    def amount_to_precision(self, symbol, amount):
        return f"{float(amount):.3f}"

    def price_to_precision(self, symbol, price):
        return f"{float(price):.2f}"

    def fetch_ohlcv(self, symbol, timeframe, since, limit):
        p = self.price[symbol]
        return [[i * 60_000, p, p, p, p, 1.0] for i in range(limit)]

    def create_order(self, symbol, type, side, amount, price, params):
        self.create_calls += 1
        cid = params["clientOrderId"]
        for o in self.orders.values():
            if o["clientOrderId"] == cid:
                raise ccxt.DuplicateOrderId("duplicate clientOrderId")
        order = {"id": str(next(self._ids)), "clientOrderId": cid, "symbol": symbol, "side": side,
                 "type": type, "amount": amount, "price": price, "filled": 0.0,
                 "status": "open" if type == "limit" else "closed", "average": None, "fee": None}
        if type == "market":
            order["filled"], order["average"] = amount, self.price[symbol]
        if self.fail_next_create:
            err, self.fail_next_create = self.fail_next_create, None
            if self.land_on_failure:
                self.orders[order["id"]] = order
            raise err
        self.orders[order["id"]] = order
        return dict(order)

    def fetch_order(self, id, symbol):
        if id not in self.orders:
            raise ccxt.OrderNotFound(id)
        return dict(self.orders[id])

    def fetch_open_orders(self, symbol):
        return [dict(o) for o in self.orders.values() if o["symbol"] == symbol and o["status"] == "open"]

    def fetch_closed_orders(self, symbol, since):
        return [dict(o) for o in self.orders.values() if o["symbol"] == symbol and o["status"] != "open"]

    def cancel_order(self, id, symbol):
        if id not in self.orders:
            raise ccxt.OrderNotFound(id)
        self.orders[id]["status"] = "canceled"
        return dict(self.orders[id])

    def fetch_balance(self):
        out = {"total": {k: v["total"] for k, v in self.balance.items()}}
        out.update({k: dict(v) for k, v in self.balance.items()})
        return out


CONFIG_TEMPLATE = """
mode: {mode}
exchange: binance
quote: USDT
loop_interval_s: 5
heartbeat_every_s: 10
unknown_order_grace_s: 10
risk:
  max_total_exposure_quote: 800
  max_per_coin_quote: 250
  max_open_orders_per_coin: 3
  max_drawdown_pct: 15
  paper_starting_quote: 1000
  paper_fee_bps: 10
  paper_slippage_bps: 0
coins:
  - symbol: SOL
    strategy: scripted
  - symbol: LINK
    strategy: watch
"""


@pytest.fixture
def make_cfg(tmp_path, monkeypatch):
    def _make(mode="paper", body=None):
        p = tmp_path / "config.yaml"
        p.write_text(body or CONFIG_TEMPLATE.format(mode=mode))
        if mode == "live":
            monkeypatch.setenv("ALTBOT_LIVE_CONFIRM", "I_ACCEPT_REAL_MONEY_RISK")
        cfg = load_config(p)
        setup_logging(cfg.log_dir, console=False)
        return cfg
    return _make


@pytest.fixture(autouse=True)
def reset_script():
    ScriptedStrategy.queued = []
    yield
    ScriptedStrategy.queued = []


def buy(amount="1", key="k1", type="market", price=None):
    return OrderIntent(side="buy", type=type, amount=Decimal(amount), key=key,
                       price=None if price is None else Decimal(price))


def sell(amount="1", key="s1", type="market", price=None):
    return OrderIntent(side="sell", type=type, amount=Decimal(amount), key=key,
                       price=None if price is None else Decimal(price))
