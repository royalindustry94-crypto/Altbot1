"""
CLI:
    python -m altbot validate     check config + that every coin is a tradable market
    python -m altbot run          start the 24/7 engine
    python -m altbot status       DB view: mode, halt flag, open orders, paper balances
    python -m altbot strategies   list available strategies and their params
    python -m altbot resume       clear a drawdown halt (deliberate human action)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import ccxt

from .broker import make_broker
from .config import ConfigError, load_config
from .engine import Engine, SingletonLock
from .exchange import (RETRYABLE, assert_key_is_trade_only, build_exchange, call_api,
                       load_credentials)
from .logging_setup import ev, setup_logging
from .risk import HALT_KEY, RiskManager
from .store import ACTIVE_STATUSES, Store
from .strategies import load_builtin_strategies


def cmd_strategies(_cfg_path: Path) -> int:
    for name, cls in sorted(load_builtin_strategies().items()):
        print(f"{name:20s} tf={cls.timeframe:4s} {cls.description}")
        for k, v in cls.default_params.items():
            print(f"{'':22s}{k} = {v!r}")
    return 0


def cmd_validate(cfg_path: Path) -> int:
    cfg = load_config(cfg_path, check_live_gate=False)
    print(f"config OK  mode={cfg.mode} exchange={cfg.exchange} quote={cfg.quote} sha256={cfg.sha256[:12]}")
    if not cfg.coins:
        print("no coins configured yet — add entries under `coins:` in config.yaml")
        return 0
    ex = getattr(ccxt, cfg.exchange)({"enableRateLimit": True})
    try:
        markets = call_api(ex.load_markets, op="load_markets", retries=3)
    except RETRYABLE as e:
        print(f"could not reach {cfg.exchange} to verify markets: {type(e).__name__}")
        return 5
    bad = 0
    for c in cfg.coins:
        sym = cfg.market_symbol(c.symbol)
        m = markets.get(sym)
        if not m or not m.get("spot") or m.get("active") is False:
            print(f"  ✗ {sym:14s} not an active spot market on {cfg.exchange}")
            bad += 1
            continue
        lim = m.get("limits") or {}
        print(f"  ✓ {sym:14s} strategy={c.strategy:12s} min_amount={(lim.get('amount') or {}).get('min')} "
              f"min_cost={(lim.get('cost') or {}).get('min')}")
    return 1 if bad else 0


def cmd_status(cfg_path: Path) -> int:
    cfg = load_config(cfg_path, check_live_gate=False)
    store = Store(cfg.db_path)
    print(json.dumps({
        "mode": cfg.mode,
        "halted": store.kv_get(HALT_KEY),
        "kill_switch_file": cfg.kill_switch_file.exists(),
        "last_heartbeat_ms": store.kv_get("last_heartbeat_ms"),
        "peak_equity": store.kv_get(f"peak_equity:{cfg.mode}"),
        "active_orders": store.orders(ACTIVE_STATUSES, mode=cfg.mode),
        "paper_balances": store.paper_balances() if cfg.mode == "paper" else None,
    }, indent=2, default=str))
    return 0


def cmd_resume(cfg_path: Path) -> int:
    cfg = load_config(cfg_path, check_live_gate=False)
    setup_logging(cfg.log_dir, console=False)
    RiskManager(cfg, Store(cfg.db_path)).resume()
    print("halt cleared; peak equity reset. Remove the KILL file too if present.")
    return 0


def cmd_run(cfg_path: Path) -> int:
    cfg = load_config(cfg_path)
    setup_logging(cfg.log_dir)
    SingletonLock(cfg.db_path.with_suffix(".lock")).acquire()
    try:
        creds = load_credentials(cfg)
        exchange = build_exchange(cfg, creds)
        assert_key_is_trade_only(exchange, cfg)
        store = Store(cfg.db_path)
        engine = Engine(cfg, exchange, store, make_broker(cfg, store, exchange), RiskManager(cfg, store))
        engine.run_forever()
        return 0
    except ConfigError as e:
        ev("config_error", 50, err=str(e))
        return 2
    except (ccxt.AuthenticationError, ccxt.PermissionDenied, ccxt.AccountSuspended) as e:
        ev("fatal_exchange_error", 50, err_type=type(e).__name__, err=str(e)[:300])
        return 4
    except RETRYABLE as e:
        ev("exchange_unreachable_at_boot", 50, err_type=type(e).__name__, err=str(e)[:300])
        return 5   # systemd restarts us; boot is fully idempotent


COMMANDS = {"run": cmd_run, "validate": cmd_validate, "status": cmd_status,
            "strategies": cmd_strategies, "resume": cmd_resume}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="altbot")
    p.add_argument("command", choices=sorted(COMMANDS))
    p.add_argument("--config", default="config.yaml", type=Path)
    args = p.parse_args(argv)
    try:
        return COMMANDS[args.command](args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
