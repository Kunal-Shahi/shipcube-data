"""Observability: structured JSON logs and a metrics hook.

Every event is one JSON line on the `shipcube_data` logger, e.g.

    {"ts": "...", "level": "INFO", "event": "query.ok", "query_id": "q_...", "request_id": "...",
     "trace": "...", "workspace_id": "...", "tenant": "acme", "elapsed_ms": 41, ...}

Logs never contain row data, secret values or SQL literals: SQL is logged as a hash.
Call `configure_logging()` once at process start (the CLI does) or attach your own handler;
in Cloud Run, stdout JSON lines become structured Cloud Logging entries (`severity`, `message`).
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

logger = logging.getLogger("shipcube_data")

# Request-scoped ids, set by the caller (MCP server / API) and picked up by every event.
request_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("shipcube_request_id", default=None)
trace_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("shipcube_trace", default=None)


def new_query_id() -> str:
    return "q_" + uuid.uuid4().hex[:20]


def sql_hash(sql: str) -> str:
    return hashlib.sha256(sql.encode()).hexdigest()[:16]


def log_event(event: str, level: int = logging.INFO, **fields: Any) -> None:
    payload = {"event": event, **{k: v for k, v in fields.items() if v is not None}}
    if request_id.get():
        payload.setdefault("request_id", request_id.get())
    if trace_id.get():
        payload.setdefault("trace", trace_id.get())
    logger.log(level, event, extra={"structured": payload})


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        body = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **getattr(record, "structured", {}),
        }
        if record.exc_info:
            body["exception"] = self.formatException(record.exc_info)
        return json.dumps(body, default=str)


def configure_logging(level: str | int = "INFO", stream=None) -> None:
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.handlers[:] = [handler]
    logger.setLevel(level)
    logger.propagate = False


class Metrics(Protocol):
    def incr(self, name: str, value: float = 1, **labels: str) -> None: ...
    def observe(self, name: str, value: float, **labels: str) -> None: ...


class NoMetrics:
    def incr(self, name: str, value: float = 1, **labels: str) -> None:
        pass

    def observe(self, name: str, value: float, **labels: str) -> None:
        pass


class InMemoryMetrics:
    """Counters and observations kept in memory, for tests and for a /metrics endpoint."""

    def __init__(self) -> None:
        self.counters: dict[tuple, float] = {}
        self.observations: dict[tuple, list[float]] = {}

    def incr(self, name: str, value: float = 1, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        self.counters[key] = self.counters.get(key, 0) + value

    def observe(self, name: str, value: float, **labels: str) -> None:
        self.observations.setdefault((name, tuple(sorted(labels.items()))), []).append(value)


class Timer:
    def __enter__(self) -> "Timer":
        self.start = time.perf_counter()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.ms = round((time.perf_counter() - self.start) * 1000, 1)

    @property
    def elapsed_ms(self) -> float:
        return round((time.perf_counter() - self.start) * 1000, 1)
