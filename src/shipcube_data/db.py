"""One small connection wrapper over SQLite (local dev, tests) and Postgres (production).

Catalog code writes portable SQL with `?` placeholders and `ON CONFLICT` upserts; this
module translates placeholders, normalises rows, runs versioned schema migrations and
provides transactions with a per-catalog lock:

* Postgres: each workspace's catalog lives in its own schema (`catalog_ws_<hex>`), the
  connection's search_path points at it, and write transactions take
  `pg_advisory_xact_lock` on the schema name, so concurrent appliers serialise.
* SQLite: `BEGIN IMMEDIATE` takes the database write lock.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import zlib
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib import resources
from typing import Any, Iterator, Optional, Sequence

from .model import ModelError

_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class Row(dict):
    """A dict row that also supports positional access, like sqlite3.Row."""

    def __init__(self, names: Sequence[str], values: Sequence[Any]):
        super().__init__(zip(names, values))
        self._values = tuple(values)

    def __getitem__(self, key):  # type: ignore[override]
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


class Result:
    def __init__(self, rows: list[Row], rowcount: int):
        self.rows, self.rowcount = rows, rowcount

    def fetchone(self) -> Optional[Row]:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[Row]:
        return self.rows

    def __iter__(self) -> Iterator[Row]:
        return iter(self.rows)


class Db:
    """A single connection. Not thread-safe: use one Db (one Catalog) per thread or request."""

    kind: str

    def __init__(self, url: str, schema: Optional[str] = None):
        self.url = url
        self._depth = 0
        self._lock = threading.RLock()
        if url.startswith(("postgres://", "postgresql://")):
            import psycopg

            self.kind = "postgres"
            self.schema = schema or "catalog_default"
            if not _SCHEMA_RE.match(self.schema):
                raise ModelError(f"invalid catalog schema name {self.schema!r}")
            self.conn = psycopg.connect(url, autocommit=True, connect_timeout=10)
            self.conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
            self.conn.execute(f'SET search_path TO "{self.schema}"')
        else:
            self.kind = "sqlite"
            self.schema = None
            path = url.removeprefix("sqlite:///").removeprefix("sqlite://") or ":memory:"
            self.conn = sqlite3.connect(path, isolation_level=None, timeout=30, check_same_thread=False)
            self.conn.execute("PRAGMA foreign_keys = ON")
            if path != ":memory:":
                self.conn.execute("PRAGMA journal_mode = WAL")

    # ------------------------------------------------------------ statements

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Result:
        with self._lock:
            if self.kind == "postgres":
                cur = self.conn.execute(sql.replace("?", "%s"), tuple(params))
            else:
                cur = self.conn.execute(sql, tuple(params))
            if cur.description is None:
                return Result([], cur.rowcount)
            names = [d[0] for d in cur.description]
            return Result([Row(names, r) for r in cur.fetchall()], cur.rowcount)

    def script(self, sql: str) -> None:
        with self._lock:
            if self.kind == "postgres":
                self.conn.execute(sql)
                return
            # executescript() would COMMIT an open transaction, so run statements one by one.
            buf = ""
            for line in sql.splitlines(keepends=True):
                buf += line
                if sqlite3.complete_statement(buf):
                    self.conn.execute(buf)
                    buf = ""
            if buf.strip() and not all(l.strip().startswith("--") or not l.strip() for l in buf.splitlines()):
                self.conn.execute(buf)

    # ------------------------------------------------------------ transactions

    @contextmanager
    def transaction(self, lock: bool = True) -> Iterator["Db"]:
        """Nested calls join the outer transaction. `lock` serialises writers of this catalog."""
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield self
                finally:
                    self._depth -= 1
                return
            self.execute("BEGIN IMMEDIATE" if self.kind == "sqlite" else "BEGIN")
            self._depth = 1
            try:
                if lock and self.kind == "postgres":
                    self.execute("SELECT pg_advisory_xact_lock(?)", (lock_key(self.schema or ""),))
                yield self
            except BaseException:
                self._depth = 0
                self.execute("ROLLBACK")
                raise
            else:
                self._depth = 0
                self.execute("COMMIT")

    @property
    def in_transaction(self) -> bool:
        return self._depth > 0

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------ schema migrations

    def _schema_files(self) -> list:
        return sorted(
            (f for f in resources.files("shipcube_data.catalog_migrations").joinpath(self.kind).iterdir() if f.name.endswith(".sql")),
            key=lambda f: f.name,
        )

    def pending_schema_migrations(self) -> list[str]:
        if self.kind == "postgres":
            exists = self.execute("SELECT to_regclass('catalog_schema_migrations') IS NOT NULL AS ok").fetchone()["ok"]
        else:
            exists = bool(self.execute("SELECT 1 FROM sqlite_master WHERE name = 'catalog_schema_migrations'").fetchone())
        done = {r["version"] for r in self.execute("SELECT version FROM catalog_schema_migrations")} if exists else set()
        return [f.name for f in self._schema_files() if int(f.name.split("_", 1)[0]) not in done]

    def migrate_schema(self) -> list[str]:
        """Apply pending catalog-schema migrations for this backend, in order, under a lock.

        Applied files are checksummed; editing one after it ran is refused.
        """
        applied: list[str] = []
        files = self._schema_files()
        with self._session_lock():
            self.script(
                "CREATE TABLE IF NOT EXISTS catalog_schema_migrations ("
                " version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            done = {r["version"]: r for r in self.execute("SELECT version, name, checksum FROM catalog_schema_migrations")}
            for f in files:
                version = int(f.name.split("_", 1)[0])
                body = f.read_text()
                checksum = hashlib.sha256(body.encode()).hexdigest()[:16]
                if version in done:
                    if done[version]["checksum"] != checksum:
                        raise ModelError(f"catalog schema migration {f.name} changed after it was applied; add a new one instead")
                    continue
                with self.transaction(lock=False):
                    self.script(body)
                    self.execute(
                        "INSERT INTO catalog_schema_migrations (version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                        (version, f.name, checksum, datetime.now(timezone.utc).isoformat(timespec="seconds")),
                    )
                applied.append(f.name)
        return applied

    @contextmanager
    def _session_lock(self) -> Iterator[None]:
        if self.kind != "postgres":
            yield
            return
        key = lock_key("schema:" + (self.schema or ""))
        self.execute("SELECT pg_advisory_lock(?)", (key,))
        try:
            yield
        finally:
            self.execute("SELECT pg_advisory_unlock(?)", (key,))


def lock_key(name: str) -> int:
    """A stable signed 64-bit advisory-lock key."""
    v = zlib.crc32(name.encode()) | (zlib.adler32(name.encode()) << 32)
    return v - (1 << 64) if v >= (1 << 63) else v


def workspace_schema(workspace_id: Optional[str]) -> str:
    return "catalog_ws_" + workspace_id.replace("-", "").lower() if workspace_id else "catalog_default"
