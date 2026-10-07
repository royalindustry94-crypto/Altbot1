"""
SQLite state store (WAL, synchronous=FULL). The single source of truth for:
  * every order intent, written BEFORE it is sent to the exchange (write-ahead),
  * paper-trading balances,
  * durable flags (halted, peak equity, last heartbeat).
Amounts are stored as TEXT and round-trip through Decimal — never float.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from . import dec

ACTIVE_STATUSES = ("PENDING", "OPEN", "UNKNOWN")
TERMINAL_STATUSES = ("FILLED", "CANCELED", "REJECTED")
_DECIMAL_COLS = ("amount", "price", "filled", "avg_price", "fee_quote")

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    client_order_id   TEXT PRIMARY KEY,
    mode              TEXT NOT NULL,
    exchange          TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    side              TEXT NOT NULL CHECK (side IN ('buy','sell')),
    type              TEXT NOT NULL CHECK (type IN ('market','limit')),
    amount            TEXT NOT NULL,
    price             TEXT,
    strategy          TEXT NOT NULL,
    intent_key        TEXT NOT NULL,
    reason            TEXT,
    status            TEXT NOT NULL,
    exchange_order_id TEXT,
    filled            TEXT NOT NULL DEFAULT '0',
    avg_price         TEXT,
    fee_quote         TEXT NOT NULL DEFAULT '0',
    last_error        TEXT,
    created_ms        INTEGER NOT NULL,
    updated_ms        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status, symbol);
CREATE TABLE IF NOT EXISTS paper_balances (
    asset TEXT PRIMARY KEY,
    free  TEXT NOT NULL,
    used  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_ms INTEGER NOT NULL
);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


class Store:
    def __init__(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(str(path), isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(SCHEMA)
        self._depth = 0

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Atomic, re-entrant transaction. Either every write inside lands, or none do."""
        if self._depth:
            self._depth += 1
            try:
                yield self.conn
            finally:
                self._depth -= 1
            return
        self.conn.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        finally:
            self._depth = 0

    # ── orders ──────────────────────────────────────────────────────────
    @staticmethod
    def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
        if r is None:
            return None
        d = dict(r)
        for col in _DECIMAL_COLS:
            d[col] = dec(d[col])
        return d

    def insert_order(self, row: dict[str, Any]) -> bool:
        """Insert a new intent. Returns False if this client_order_id already exists."""
        ts = now_ms()
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO orders
               (client_order_id, mode, exchange, symbol, side, type, amount, price, strategy,
                intent_key, reason, status, created_ms, updated_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row["client_order_id"], row["mode"], row["exchange"], row["symbol"], row["side"],
             row["type"], str(row["amount"]), None if row.get("price") is None else str(row["price"]),
             row["strategy"], row["intent_key"], row.get("reason", ""), row["status"], ts, ts),
        )
        return cur.rowcount == 1

    def update_order(self, client_order_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_ms"] = now_ms()
        cols = ", ".join(f"{k} = ?" for k in fields)
        vals = [str(v) if isinstance(v, Decimal) else v for v in fields.values()]
        self.conn.execute(f"UPDATE orders SET {cols} WHERE client_order_id = ?",
                          (*vals, client_order_id))

    def get_order(self, client_order_id: str) -> dict[str, Any] | None:
        return self._row(self.conn.execute(
            "SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,)).fetchone())

    def orders(self, statuses: tuple[str, ...] = ACTIVE_STATUSES, symbol: str | None = None,
               mode: str | None = None) -> list[dict[str, Any]]:
        q = f"SELECT * FROM orders WHERE status IN ({','.join('?' * len(statuses))})"
        args: list[Any] = list(statuses)
        if symbol:
            q += " AND symbol = ?"
            args.append(symbol)
        if mode:
            q += " AND mode = ?"
            args.append(mode)
        q += " ORDER BY created_ms"
        return [self._row(r) for r in self.conn.execute(q, args).fetchall()]

    def known_client_ids(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT client_order_id FROM orders")}

    # ── paper balances ──────────────────────────────────────────────────
    def paper_balances(self) -> dict[str, dict[str, Decimal]]:
        out = {}
        for r in self.conn.execute("SELECT asset, free, used FROM paper_balances"):
            free, used = Decimal(r["free"]), Decimal(r["used"])
            out[r["asset"]] = {"free": free, "used": used, "total": free + used}
        return out

    def set_paper_balance(self, asset: str, free: Decimal, used: Decimal) -> None:
        if free < 0 or used < 0:
            raise ValueError(f"negative paper balance for {asset}: free={free} used={used}")
        self.conn.execute(
            "INSERT INTO paper_balances(asset, free, used) VALUES (?,?,?) "
            "ON CONFLICT(asset) DO UPDATE SET free=excluded.free, used=excluded.used",
            (asset, str(free), str(used)))

    # ── key/value flags ─────────────────────────────────────────────────
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        r = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return r["value"] if r else default

    def kv_set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO kv(key, value, updated_ms) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ms=excluded.updated_ms",
            (key, value, now_ms()))

    def kv_delete(self, key: str) -> None:
        self.conn.execute("DELETE FROM kv WHERE key = ?", (key,))
