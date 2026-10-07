"""Structured JSON-lines audit logging with rotation and secret redaction."""

from __future__ import annotations

import json
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

log = logging.getLogger("altbot")
_SECRETS: list[str] = []


def register_secret(value: str | None) -> None:
    """Any string registered here is scrubbed from every log line before it is written."""
    if value and len(value) >= 8 and value not in _SECRETS:
        _SECRETS.append(value)


class JsonFormatter(logging.Formatter):
    converter = time.gmtime

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S") + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "event": record.getMessage(),
        }
        payload.update(getattr(record, "ctx", None) or {})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        out = json.dumps(payload, default=str)
        for secret in _SECRETS:
            out = out.replace(secret, "***REDACTED***")
        return out


def setup_logging(log_dir: Path, console: bool = True, level: int = logging.INFO) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log.setLevel(level)
    log.propagate = False
    for h in list(log.handlers):
        log.removeHandler(h)
        h.close()
    file_h = RotatingFileHandler(log_dir / "altbot.jsonl", maxBytes=5 * 1024 * 1024,
                                 backupCount=10, encoding="utf-8")
    file_h.setFormatter(JsonFormatter())
    log.addHandler(file_h)
    if console:
        console_h = logging.StreamHandler(sys.stdout)
        console_h.setFormatter(JsonFormatter())
        log.addHandler(console_h)


def ev(event: str, level: int = logging.INFO, _exc: bool = False, **ctx: Any) -> None:
    """Emit one structured audit event."""
    log.log(level, event, extra={"ctx": ctx}, exc_info=_exc)
