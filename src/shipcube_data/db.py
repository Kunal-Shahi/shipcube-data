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
import uuid
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


SHARED_SCHEMA = "catalog"


class Db:
    """A single connection. Not thread-safe: use one Db (one Catalog) per thread or request.

    Layouts (`self.layout`, also the catalog_migrations/ directory):
    * "sqlite": one file per catalog (local development, tests).
    * "postgres": one schema per workspace (`catalog_ws_<hex>`); standalone deployments.
    * "postgres_shared": ShipCube's own database, schema `catalog`, every table keyed by
      workspace_id under FORCE ROW LEVEL SECURITY (workspace_id = app_workspace()). This is the
      production layout. Requests run on the host's transaction (`Db.wrap`), which has already
      set app.workspace_id for the verified caller; nothing is created at request time.
    """

    kind: str
    layout: str
    wrapped: bool = False

    def __init__(self, url: str, schema: Optional[str] = None, create_schema: bool = True,
                 shared: bool = False, workspace_id: Optional[str] = None):
        self.url = url
        self._depth = 0
        self._lock = threading.RLock()
        self.workspace_id = workspace_id
        if url.startswith(("postgres://", "postgresql://")):
            import psycopg

            self.kind = "postgres"
            self.layout = "postgres_shared" if shared else "postgres"
            self.schema = SHARED_SCHEMA if shared else (schema or "catalog_default")
            if not _SCHEMA_RE.match(self.schema):
                raise ModelError(f"invalid catalog schema name {self.schema!r}")
            self.conn = psycopg.connect(url, autocommit=True, connect_timeout=10)
            # Check first: CREATE SCHEMA IF NOT EXISTS still needs CREATE on the database, which the
            # migration role doesn't have when the schema is provisioned for it (ShipCube's bootstrap.sql).
            exists = self.conn.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (self.schema,)).fetchone()
            if not exists and create_schema:
                try:
                    self.conn.execute(f'CREATE SCHEMA "{self.schema}"')
                except psycopg.errors.InsufficientPrivilege:
                    self.conn.close()
                    raise ModelError(f"cannot create schema {self.schema}: this role has no CREATE on the database",
                                     "catalog_not_migrated",
                                     f"create it once as an administrator, e.g. ShipCube's bootstrap.sql: "
                                     f"CREATE SCHEMA {self.schema}; GRANT USAGE, CREATE ON SCHEMA {self.schema} TO <migration role>") from None
            elif not exists:
                self.conn.close()
                raise ModelError(f"no catalog for this workspace (schema {self.schema} does not exist)", "catalog_not_migrated")
            if shared:
                # app_workspace() and friends live in public; RLS reads app.workspace_id for every statement.
                self.conn.execute(f'SET search_path TO "{self.schema}", public')
                if workspace_id:
                    self.conn.execute("SELECT set_config('app.workspace_id', %s, false)", (str(workspace_id),))
            else:
                self.conn.execute(f'SET search_path TO "{self.schema}"')
        else:
            if shared:
                raise ModelError("the shared catalog layout needs Postgres", "invalid")
            self.kind = self.layout = "sqlite"
            self.schema = None
            path = url.removeprefix("sqlite:///").removeprefix("sqlite://") or ":memory:"
            self.conn = sqlite3.connect(path, isolation_level=None, timeout=30, check_same_thread=False)
            self.conn.execute("PRAGMA foreign_keys = ON")
            if path != ":memory:":
                self.conn.execute("PRAGMA journal_mode = WAL")

    @classmethod
    def wrap(cls, conn: Any, workspace_id: str) -> "Db":
        """Use the host's open transaction (ShipCube API: `db.tx(caller)`, RLS context already set).

        The host owns the transaction and commits it; our transactions become savepoints, and
        writers serialise on pg_advisory_xact_lock(hashtext('catalog:' || workspace_id))."""
        db = cls.__new__(cls)
        db.url, db._depth, db._lock = "<host transaction>", 0, threading.RLock()
        db.kind, db.layout, db.schema, db.wrapped, db.workspace_id = "postgres", "postgres_shared", SHARED_SCHEMA, True, workspace_id
        db.conn = conn
        conn.execute("SELECT set_config('search_path', %s, true)", (f'"{SHARED_SCHEMA}", public',))
        return db

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
            rows = cur.fetchall()  # tuples, or dicts on a host connection with dict_row
            out = []
            for r in rows:
                values = [r[n] for n in names] if isinstance(r, dict) else r
                # UUID columns (the shared layout's workspace_id) come back as text, as on every other layout
                out.append(Row(names, [str(v) if isinstance(v, uuid.UUID) else v for v in values]))
            return Result(out, cur.rowcount)

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

    def _lock_sql(self) -> tuple[str, tuple]:
        if self.layout == "postgres_shared":
            return "SELECT pg_advisory_xact_lock(hashtext(?))", (f"catalog:{self.workspace_id}",)
        return "SELECT pg_advisory_xact_lock(?)", (lock_key(self.schema or ""),)

    @contextmanager
    def transaction(self, lock: bool = True) -> Iterator["Db"]:
        """Nested calls join the outer transaction. `lock` serialises writers of this catalog."""
        if self.wrapped:  # the host's transaction: a savepoint, so a failed write undoes only itself
            with self._lock, self.conn.transaction():
                outer = self._depth == 0
                self._depth += 1
                try:
                    if lock and outer:
                        self.execute(*self._lock_sql())
                    yield self
                finally:
                    self._depth -= 1
            return
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
                    self.execute(*self._lock_sql())
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
        if not self.wrapped:  # the host owns its connection
            self.conn.close()

    # ------------------------------------------------------------ schema migrations

    def _schema_files(self) -> list:
        return sorted(
            (f for f in resources.files("shipcube_data.catalog_migrations").joinpath(self.layout).iterdir() if f.name.endswith(".sql")),
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
        if self.wrapped:
            raise ModelError("catalog DDL runs as the migration role (shipcube-data catalog-migrate), never on a request", "forbidden")
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


def migrate_shared_catalog(url: str) -> list[str]:
    """Create or update the shared catalog (schema `catalog`) in ShipCube's database.

    Run as the migration role after the API's own migrations (the policies use app_workspace()),
    e.g. from `shipcube_api.admin migrate`. Idempotent; checksummed; advisory-locked."""
    db = Db(url, shared=True)
    try:
        return db.migrate_schema()
    finally:
        db.close()
