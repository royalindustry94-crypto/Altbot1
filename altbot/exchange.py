"""Exchange connection: credentials, CCXT client factory, key-scope check, retrying reads."""

from __future__ import annotations

import logging
import os
import random
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

import ccxt

from .config import AppConfig, ConfigError
from .logging_setup import ev, register_secret

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

T = TypeVar("T")

MAX_RETRIES = 5
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 30.0
REQUEST_TIMEOUT_MS = 15_000

# NetworkError covers RequestTimeout, ExchangeNotAvailable (5xx/maintenance),
# DDoSProtection and RateLimitExceeded (429) — all safe to retry for READS.
RETRYABLE = (ccxt.NetworkError,)
FATAL = (ccxt.AuthenticationError, ccxt.PermissionDenied, ccxt.AccountSuspended,
         ccxt.BadSymbol, ccxt.NotSupported)

_sleep = time.sleep  # patched in tests


def call_api(fn: Callable[..., T], *args: Any, op: str, retries: int = MAX_RETRIES, **kwargs: Any) -> T:
    """
    Run one READ (or otherwise idempotent) API call with capped, jittered backoff.
    Never use this for create_order: an order POST that times out has an UNKNOWN
    outcome and must be reconciled by clientOrderId, not blindly re-sent.
    """
    for attempt in range(1, retries + 1):
        t0 = time.monotonic()
        try:
            result = fn(*args, **kwargs)
            ev("api_ok", logging.DEBUG, op=op, attempt=attempt,
               ms=round((time.monotonic() - t0) * 1000))
            return result
        except ccxt.InvalidNonce as e:
            ev("api_invalid_nonce_resync", logging.WARNING, op=op, attempt=attempt, err=str(e)[:300])
            owner = getattr(fn, "__self__", None)
            if owner is not None and hasattr(owner, "load_time_difference"):
                try:
                    owner.load_time_difference()
                except Exception:
                    pass
            if attempt == retries:
                raise
        except FATAL as e:
            ev("api_fatal", logging.CRITICAL, op=op, err_type=type(e).__name__, err=str(e)[:300])
            raise
        except RETRYABLE as e:
            if attempt == retries:
                ev("api_retries_exhausted", logging.ERROR, op=op, attempts=attempt,
                   err_type=type(e).__name__, err=str(e)[:300])
                raise
            delay = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** (attempt - 1)) * random.uniform(0.5, 1.0)
            ev("api_retryable", logging.WARNING, op=op, attempt=attempt, err_type=type(e).__name__,
               err=str(e)[:300], retry_in_s=round(delay, 2))
            _sleep(delay)
        except ccxt.ExchangeError as e:
            ev("api_exchange_error", logging.ERROR, op=op, err_type=type(e).__name__, err=str(e)[:300])
            raise
    raise RuntimeError("unreachable")


def load_credentials(cfg: AppConfig) -> dict[str, str] | None:
    """Paper mode needs no keys and loads none. Testnet and live keys use DIFFERENT env names."""
    if cfg.mode == "paper":
        return None
    if cfg.env_file.exists():
        perms = cfg.env_file.stat().st_mode & 0o777
        if perms & 0o077:
            raise ConfigError(f"{cfg.env_file} is {oct(perms)}; run: chmod 600 {cfg.env_file}")
        if load_dotenv:
            load_dotenv(cfg.env_file, override=False)

    prefix = cfg.exchange.upper() + ("_TESTNET" if cfg.mode == "testnet" else "")
    key = os.getenv(f"{prefix}_API_KEY")
    secret = os.getenv(f"{prefix}_API_SECRET")
    password = os.getenv(f"{prefix}_API_PASSPHRASE")
    if not key or not secret:
        raise ConfigError(f"mode={cfg.mode} requires {prefix}_API_KEY and {prefix}_API_SECRET")

    for s in (key, secret, password):
        register_secret(s)
    creds = {"apiKey": key, "secret": secret}
    if password:
        creds["password"] = password
    ev("credentials_loaded", exchange=cfg.exchange, mode=cfg.mode,
       key_fingerprint=f"{key[:4]}…{key[-4:]}")
    return creds


def build_exchange(cfg: AppConfig, creds: dict[str, str] | None) -> ccxt.Exchange:
    params: dict[str, Any] = {
        "enableRateLimit": True,                  # non-negotiable
        "timeout": REQUEST_TIMEOUT_MS,
        "options": {"adjustForTimeDifference": True, "defaultType": "spot"},
    }
    if creds:
        params.update(creds)
    exchange: ccxt.Exchange = getattr(ccxt, cfg.exchange)(params)
    if exchange.id == "binance":
        # Spot-only bot: skip futures market discovery (fewer calls, no futures endpoints).
        exchange.options["fetchMarkets"] = {**exchange.options.get("fetchMarkets", {}), "types": ["spot"]}
        if cfg.mode == "paper":
            # Binance's official public-data mirror: same prices, no account, fewer region blocks.
            exchange.urls["api"]["public"] = "https://data-api.binance.vision/api/v3"
    if cfg.mode == "testnet":
        try:
            exchange.set_sandbox_mode(True)
        except ccxt.NotSupported:
            raise ConfigError(f"{cfg.exchange} has no testnet in ccxt; use mode=paper instead")
    ev("exchange_built", exchange=cfg.exchange, mode=cfg.mode,
       rate_limit_ms=exchange.rateLimit, authenticated=bool(creds))
    return exchange


def assert_key_is_trade_only(exchange: ccxt.Exchange, cfg: AppConfig) -> None:
    """In live mode, refuse to run on a key that can withdraw funds."""
    if cfg.mode != "live":
        return
    if exchange.id == "binance":
        r = call_api(exchange.sapiGetAccountApiRestrictions, op="api_restrictions")
        withdrawals = str(r.get("enableWithdrawals")).lower() == "true"
        ip_locked = str(r.get("ipRestrict")).lower() == "true"
        ev("key_scope_checked", enable_withdrawals=withdrawals, ip_restricted=ip_locked)
        if withdrawals:
            raise ConfigError("API key has WITHDRAWALS ENABLED. Delete it and issue a trade-only key.")
        if not ip_locked:
            ev("key_not_ip_restricted", logging.WARNING, advice="bind the key to your server's static IP")
    else:
        ev("key_scope_unverifiable", logging.WARNING, exchange=exchange.id,
           advice="confirm withdrawals are disabled for this key in the exchange UI")
