from __future__ import annotations

import json
from decimal import Decimal

import ccxt
import pytest

from altbot.broker import LiveBroker, PaperBroker, make_client_order_id
from altbot.config import ConfigError, load_config
from altbot.engine import Engine
from altbot.risk import RiskManager, Snapshot
from altbot.store import Store, now_ms
from conftest import CONFIG_TEMPLATE, FakeExchange, ScriptedStrategy, buy, sell


def build(cfg, broker_cls=None):
    ex = FakeExchange()
    store = Store(cfg.db_path)
    broker_cls = broker_cls or (PaperBroker if cfg.mode == "paper" else LiveBroker)
    broker = broker_cls(cfg, store, ex)
    risk = RiskManager(cfg, store)
    return ex, store, broker, risk, Engine(cfg, ex, store, broker, risk)


# ── idempotency ────────────────────────────────────────────────────────
def test_client_order_id_is_deterministic_and_exchange_safe():
    a = make_client_order_id("live", "binance", "grid", "SOL/USDT", "buy", "1700:lvl3")
    b = make_client_order_id("live", "binance", "grid", "SOL/USDT", "buy", "1700:lvl3")
    c = make_client_order_id("live", "binance", "grid", "SOL/USDT", "buy", "1700:lvl4")
    assert a == b != c
    assert len(a) == 36


def test_same_signal_on_every_tick_places_exactly_one_order(make_cfg):
    cfg = make_cfg("testnet")
    ex, store, broker, risk, engine = build(cfg)
    engine.boot()
    ScriptedStrategy.queued = [buy("1", key="candle-42:entry")]
    for _ in range(5):
        engine.tick()
    assert ex.create_calls == 1
    assert len(store.orders(("FILLED",))) == 1


def test_restart_does_not_duplicate(make_cfg):
    cfg = make_cfg("testnet")
    ex, store, broker, risk, engine = build(cfg)
    engine.boot()
    ScriptedStrategy.queued = [buy("0.5", key="k", type="limit", price="90")]
    engine.tick()
    store.close()
    # "power loss": brand-new objects on the same DB file and the same exchange state
    store2 = Store(cfg.db_path)
    broker2 = LiveBroker(cfg, store2, ex)
    engine2 = Engine(cfg, ex, store2, broker2, RiskManager(cfg, store2))
    engine2.boot()
    engine2.tick()
    assert ex.create_calls == 1
    assert [o["status"] for o in store2.orders(("OPEN",))] == ["OPEN"]


# ── ambiguous submits / recovery ───────────────────────────────────────
def test_timeout_on_submit_is_reconciled_not_resent(make_cfg):
    cfg = make_cfg("testnet")
    ex, store, broker, risk, engine = build(cfg)
    engine.boot()
    ex.fail_next_create = ccxt.RequestTimeout("read timed out")   # order DID land on exchange
    ScriptedStrategy.queued = [buy("0.5", key="k", type="limit", price="90")]
    engine.tick()   # submit -> UNKNOWN; the following reconcile in the same tick is on the next tick
    engine.tick()
    assert ex.create_calls == 1
    row = store.orders(("OPEN",))[0]
    assert row["exchange_order_id"] is not None


def test_unknown_order_that_never_landed_is_rejected_after_grace(make_cfg):
    cfg = make_cfg("testnet")
    ex, store, broker, risk, engine = build(cfg)
    ex.land_on_failure = False
    ex.fail_next_create = ccxt.ExchangeNotAvailable("502")
    broker.submit("SOL/USDT", buy("0.5", key="k", type="limit", price="90"), "scripted")
    cid = store.orders(("UNKNOWN",))[0]["client_order_id"]
    broker.reconcile()
    assert store.get_order(cid)["status"] == "UNKNOWN"      # still inside grace window
    store.conn.execute("UPDATE orders SET created_ms = ?", (now_ms() - 60_000,))
    broker.reconcile()
    assert store.get_order(cid)["status"] == "REJECTED"


def test_crash_after_write_ahead_before_send_is_recovered(make_cfg):
    cfg = make_cfg("testnet")
    ex, store, broker, risk, engine = build(cfg)
    # Simulate: intent row committed, process died before create_order ran.
    store.insert_order({"client_order_id": "cid-x", "mode": "testnet", "exchange": "binance",
                        "symbol": "SOL/USDT", "side": "buy", "type": "limit", "amount": Decimal("1"),
                        "price": Decimal("90"), "strategy": "scripted", "intent_key": "k",
                        "status": "PENDING"})
    store.conn.execute("UPDATE orders SET created_ms = ?", (now_ms() - 60_000,))
    broker.reconcile()
    assert store.get_order("cid-x")["status"] == "REJECTED"
    assert ex.create_calls == 0


def test_orphan_orders_are_reported_not_touched(make_cfg, tmp_path):
    cfg = make_cfg("testnet")
    ex, store, broker, risk, engine = build(cfg)
    ex.orders["999"] = {"id": "999", "clientOrderId": "web_manual", "symbol": "SOL/USDT", "side": "buy",
                        "type": "limit", "amount": 1, "price": 50, "filled": 0, "status": "open"}
    engine.boot()
    log_text = (cfg.log_dir / "altbot.jsonl").read_text()
    assert "orphan_order_on_exchange" in log_text
    assert ex.orders["999"]["status"] == "open"


# ── risk gate ──────────────────────────────────────────────────────────
def snap(balances=None, prices=None, markets=None):
    return Snapshot(balances=balances or {"USDT": {"free": Decimal(1000), "used": Decimal(0), "total": Decimal(1000)}},
                    prices=prices or {"SOL/USDT": Decimal(100), "LINK/USDT": Decimal(10)},
                    markets=markets or FakeExchange().markets)


def test_risk_rejects_non_whitelisted(make_cfg):
    cfg = make_cfg()
    risk = RiskManager(cfg, Store(cfg.db_path))
    d = risk.check("DOGE/USDT", buy("10"), snap(prices={"DOGE/USDT": Decimal("0.1")}))
    assert not d.ok and "whitelisted" in d.reason


def test_risk_enforces_per_coin_cap_min_notional_and_no_shorting(make_cfg):
    cfg = make_cfg()
    risk = RiskManager(cfg, Store(cfg.db_path))
    assert not risk.check("SOL/USDT", buy("3"), snap()).ok           # $300 > $250 cap
    assert risk.check("SOL/USDT", buy("2"), snap()).ok                # $200 ok
    assert not risk.check("SOL/USDT", buy("0.01"), snap()).ok        # $1 < $5 exchange min
    assert not risk.check("SOL/USDT", sell("1"), snap()).ok          # holds no SOL


def test_drawdown_halts_persistently_and_blocks_orders(make_cfg):
    cfg = make_cfg()
    store = Store(cfg.db_path)
    risk = RiskManager(cfg, store)
    risk.update_equity(snap())   # peak 1000
    risk.update_equity(snap(balances={"USDT": {"free": Decimal(800), "used": Decimal(0), "total": Decimal(800)}}))
    assert risk.halted()
    assert RiskManager(cfg, Store(cfg.db_path)).halted()   # survives restart
    assert not risk.check("SOL/USDT", buy("1"), snap()).ok
    risk.resume()
    assert not risk.halted()


def test_missing_price_never_triggers_false_drawdown(make_cfg):
    cfg = make_cfg()
    store = Store(cfg.db_path)
    risk = RiskManager(cfg, store)
    bal = {"USDT": {"free": Decimal(500), "used": Decimal(0), "total": Decimal(500)},
           "SOL": {"free": Decimal(5), "used": Decimal(0), "total": Decimal(5)}}
    risk.update_equity(snap(balances=bal))                       # 500 + 5*100 = 1000
    assert risk.update_equity(snap(balances=bal, prices={"LINK/USDT": Decimal(10)})) is None
    assert not risk.halted()


def test_kill_switch_file_blocks_new_orders(make_cfg):
    cfg = make_cfg("testnet")
    ex, store, broker, risk, engine = build(cfg)
    engine.boot()
    cfg.kill_switch_file.touch()
    ScriptedStrategy.queued = [buy("1", key="k")]
    engine.tick()
    assert ex.create_calls == 0


# ── paper broker ───────────────────────────────────────────────────────
def test_paper_market_buy_then_sell_accounts_fees(make_cfg):
    cfg = make_cfg("paper")
    ex, store, broker, risk, engine = build(cfg)
    engine.boot()
    ScriptedStrategy.queued = [buy("2", key="b")]
    engine.tick()
    bal = store.paper_balances()
    assert bal["SOL"]["free"] == Decimal("2.000")
    assert bal["USDT"]["free"] == Decimal("1000") - Decimal("200") - Decimal("0.2")
    ScriptedStrategy.queued = [sell("2", key="s")]
    engine.tick()
    assert store.paper_balances()["SOL"]["total"] == 0
    assert ex.create_calls == 0        # paper mode never touches order endpoints


def test_paper_limit_order_reserves_then_fills_on_cross(make_cfg):
    cfg = make_cfg("paper")
    ex, store, broker, risk, engine = build(cfg)
    engine.boot()
    ScriptedStrategy.queued = [buy("1", key="dip", type="limit", price="95")]
    engine.tick()
    assert store.orders(("OPEN",))
    assert store.paper_balances()["USDT"]["used"] > 0
    ex.price["SOL/USDT"] = 94.0
    engine.tick()
    assert not store.orders(("OPEN",))
    b = store.paper_balances()
    assert b["SOL"]["total"] == Decimal("1.000") and b["USDT"]["used"] == 0


def test_paper_cancel_releases_reservation(make_cfg):
    cfg = make_cfg("paper")
    ex, store, broker, risk, engine = build(cfg)
    engine.boot()
    ScriptedStrategy.queued = [buy("1", key="dip", type="limit", price="95")]
    engine.tick()
    cid = store.orders(("OPEN",))[0]["client_order_id"]
    broker.cancel(cid, "test")
    b = store.paper_balances()["USDT"]
    assert b["used"] == 0 and b["free"] == Decimal("1000")


# ── config ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("mutate,msg", [
    (lambda t: t.replace("strategy: watch", "strategy: nope"), "unknown strategy"),
    (lambda t: t.replace("symbol: LINK", "symbol: SOL"), "listed twice"),
    (lambda t: t.replace("mode: paper", "mode: yolo"), "mode must be"),
    (lambda t: t.replace("max_per_coin_quote: 250", "max_per_coin_quote: 900"), "cannot exceed"),
    (lambda t: t.replace("strategy: watch", "strategy: watch\n    params: {bogus: 1}"), "unknown params"),
])
def test_config_rejects_bad_input(tmp_path, mutate, msg):
    p = tmp_path / "config.yaml"
    p.write_text(mutate(CONFIG_TEMPLATE.format(mode="paper")))
    with pytest.raises(ConfigError, match=msg):
        load_config(p)


def test_live_mode_requires_second_confirmation(tmp_path, monkeypatch):
    monkeypatch.delenv("ALTBOT_LIVE_CONFIRM", raising=False)
    p = tmp_path / "config.yaml"
    p.write_text(CONFIG_TEMPLATE.format(mode="live"))
    with pytest.raises(ConfigError, match="ALTBOT_LIVE_CONFIRM"):
        load_config(p)


def test_secrets_never_reach_the_log(make_cfg, monkeypatch):
    cfg = make_cfg("testnet")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "KEY_abcdefghijklmnop")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "SECRET_zyxwvutsrqponm")
    from altbot.exchange import load_credentials
    from altbot.logging_setup import ev
    load_credentials(cfg)
    ev("oops", leaked="SECRET_zyxwvutsrqponm and KEY_abcdefghijklmnop")
    text = (cfg.log_dir / "altbot.jsonl").read_text()
    assert "SECRET_zyxwvutsrqponm" not in text and "KEY_abcdefghijklmnop" not in text
    for line in text.splitlines():
        json.loads(line)   # every line is valid JSON
