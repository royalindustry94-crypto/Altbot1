"""Load and strictly validate config.yaml. Invalid config never reaches the engine."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import ccxt
import yaml

VALID_MODES = ("paper", "testnet", "live")
LIVE_CONFIRM_ENV = "ALTBOT_LIVE_CONFIRM"
LIVE_CONFIRM_VALUE = "I_ACCEPT_REAL_MONEY_RISK"
_COIN_RE = re.compile(r"^[A-Z0-9]{1,20}$")


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class CoinConfig:
    symbol: str                      # base asset, e.g. "SOL"
    strategy: str                    # registered strategy name
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RiskConfig:
    max_total_exposure_quote: Decimal
    max_per_coin_quote: Decimal
    max_open_orders_per_coin: int
    max_drawdown_pct: Decimal
    paper_starting_quote: Decimal
    paper_fee_bps: Decimal
    paper_slippage_bps: Decimal


@dataclass(frozen=True)
class AppConfig:
    mode: str
    exchange: str
    quote: str
    loop_interval_s: int
    heartbeat_every_s: int
    unknown_order_grace_s: int
    db_path: Path
    log_dir: Path
    kill_switch_file: Path
    env_file: Path
    risk: RiskConfig
    coins: tuple[CoinConfig, ...]
    sha256: str

    def market_symbol(self, base: str) -> str:
        return f"{base}/{self.quote}"

    @property
    def symbols(self) -> list[str]:
        return [self.market_symbol(c.symbol) for c in self.coins]

    @property
    def whitelist(self) -> frozenset[str]:
        return frozenset(c.symbol for c in self.coins)


def _dec(raw: dict, key: str, minimum: Decimal | None = None, maximum: Decimal | None = None) -> Decimal:
    if key not in raw:
        raise ConfigError(f"risk.{key} is required")
    try:
        value = Decimal(str(raw[key]))
    except (InvalidOperation, ValueError):
        raise ConfigError(f"risk.{key} must be a number, got {raw[key]!r}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"risk.{key} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"risk.{key} must be <= {maximum}")
    return value


def _int(raw: dict, key: str, default: int, minimum: int) -> int:
    value = raw.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ConfigError(f"{key} must be an integer >= {minimum}")
    return value


def load_config(path: Path, check_live_gate: bool = True) -> AppConfig:
    path = Path(path).resolve()
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    text = path.read_bytes()
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"config is not valid YAML: {e}")
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")

    base_dir = path.parent

    mode = raw.get("mode")
    if mode not in VALID_MODES:
        raise ConfigError(f"mode must be one of {VALID_MODES}, got {mode!r}")
    if mode == "live" and check_live_gate and os.getenv(LIVE_CONFIRM_ENV) != LIVE_CONFIRM_VALUE:
        raise ConfigError(f"mode=live also requires env {LIVE_CONFIRM_ENV}={LIVE_CONFIRM_VALUE}")

    exchange = str(raw.get("exchange", "")).lower()
    if exchange not in ccxt.exchanges:
        raise ConfigError(f"unknown exchange id {exchange!r}")

    quote = str(raw.get("quote", "")).upper()
    if not _COIN_RE.match(quote):
        raise ConfigError(f"invalid quote currency {quote!r}")

    risk_raw = raw.get("risk") or {}
    if not isinstance(risk_raw, dict):
        raise ConfigError("risk must be a mapping")
    risk = RiskConfig(
        max_total_exposure_quote=_dec(risk_raw, "max_total_exposure_quote", Decimal("0.01")),
        max_per_coin_quote=_dec(risk_raw, "max_per_coin_quote", Decimal("0.01")),
        max_open_orders_per_coin=_int(risk_raw, "max_open_orders_per_coin", 10, 1),
        max_drawdown_pct=_dec(risk_raw, "max_drawdown_pct", Decimal("0.1"), Decimal("99")),
        paper_starting_quote=_dec(risk_raw, "paper_starting_quote", Decimal("0")),
        paper_fee_bps=_dec(risk_raw, "paper_fee_bps", Decimal("0"), Decimal("1000")),
        paper_slippage_bps=_dec(risk_raw, "paper_slippage_bps", Decimal("0"), Decimal("1000")),
    )
    if risk.max_per_coin_quote > risk.max_total_exposure_quote:
        raise ConfigError("risk.max_per_coin_quote cannot exceed risk.max_total_exposure_quote")

    coins_raw = raw.get("coins") or []
    if not isinstance(coins_raw, list):
        raise ConfigError("coins must be a list")

    from .strategies import REGISTRY, load_builtin_strategies
    load_builtin_strategies()

    coins: list[CoinConfig] = []
    seen: set[str] = set()
    for i, entry in enumerate(coins_raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"coins[{i}] must be a mapping")
        if entry.get("enabled", True) is False:
            continue
        sym = str(entry.get("symbol", "")).upper()
        if not _COIN_RE.match(sym):
            raise ConfigError(f"coins[{i}].symbol invalid: {entry.get('symbol')!r}")
        if sym == quote:
            raise ConfigError(f"coins[{i}].symbol cannot equal the quote currency")
        if sym in seen:
            raise ConfigError(f"coin {sym} listed twice")
        seen.add(sym)
        strategy = str(entry.get("strategy", ""))
        if strategy not in REGISTRY:
            raise ConfigError(f"coins[{i}] ({sym}): unknown strategy {strategy!r}; "
                              f"available: {sorted(REGISTRY)}")
        params = entry.get("params") or {}
        if not isinstance(params, dict):
            raise ConfigError(f"coins[{i}] ({sym}): params must be a mapping")
        try:
            REGISTRY[strategy](f"{sym}/{quote}", params)  # params are validated by the strategy itself
        except (ValueError, TypeError) as e:
            raise ConfigError(f"coins[{i}] ({sym}) strategy {strategy}: {e}")
        coins.append(CoinConfig(symbol=sym, strategy=strategy, params=dict(params)))

    def _path(key: str, default: str) -> Path:
        p = Path(str(raw.get(key, default)))
        return p if p.is_absolute() else base_dir / p

    return AppConfig(
        mode=mode,
        exchange=exchange,
        quote=quote,
        loop_interval_s=_int(raw, "loop_interval_s", 30, 5),
        heartbeat_every_s=_int(raw, "heartbeat_every_s", 300, 10),
        unknown_order_grace_s=_int(raw, "unknown_order_grace_s", 120, 10),
        db_path=_path("db_path", "data/altbot.sqlite3"),
        log_dir=_path("log_dir", "logs"),
        kill_switch_file=_path("kill_switch_file", "KILL"),
        env_file=_path("env_file", ".env"),
        risk=risk,
        coins=tuple(coins),
        sha256=hashlib.sha256(text).hexdigest(),
    )
