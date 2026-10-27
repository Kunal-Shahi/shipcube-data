"""The catalog database (SQLite in the prototype, Postgres in production).

Every model table is versioned SCD2-style: a row is valid from `since_version`
until `until_version` (exclusive; NULL = still current). That gives us:

* the model as of any version, for pinned queries and cache keys;
* exact, generic rollback of the head migration (delete rows it added, reopen
  rows it closed), so ops do not need hand-written "down" steps;
* a queryable history of every definition a member has ever had.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from .model import Cube, Dimension, Join, Measure, Model, ModelError, PartitionSpec, RowPolicy, SchemaBinding, Segment

DDL = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_versions (
    version        INTEGER PRIMARY KEY,
    migration_id   TEXT NOT NULL,
    fingerprint    TEXT NOT NULL,
    applied_at     TEXT NOT NULL,
    applied_by     TEXT,
    rolled_back_at TEXT
);

CREATE TABLE IF NOT EXISTS migrations (
    id           TEXT PRIMARY KEY,
    description  TEXT NOT NULL DEFAULT '',
    checksum     TEXT NOT NULL,
    ops          TEXT NOT NULL,              -- JSON list of ops
    touches      TEXT NOT NULL DEFAULT '[]', -- JSON list of touch keys
    kind         TEXT NOT NULL,              -- additive | change | breaking
    status       TEXT NOT NULL CHECK (status IN ('proposed', 'approved', 'applied', 'rolled_back', 'rejected')),
    base_version INTEGER,
    version      INTEGER,                    -- model version it produced
    proposed_by  TEXT,
    approved_by  TEXT,
    impact       TEXT,                       -- JSON dry-run report
    created_at   TEXT NOT NULL,
    applied_at   TEXT
);

CREATE TABLE IF NOT EXISTS cubes (
    name          TEXT NOT NULL,
    table_name    TEXT,
    sql           TEXT,
    title         TEXT,
    description   TEXT,
    grants        TEXT,
    spec          TEXT NOT NULL,
    since_version INTEGER NOT NULL,
    until_version INTEGER,
    PRIMARY KEY (name, since_version)
);

CREATE TABLE IF NOT EXISTS members (
    cube          TEXT NOT NULL,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('measure', 'dimension')),
    agg           TEXT,                      -- measures
    type          TEXT,                      -- dimensions
    sql           TEXT,
    deprecated    TEXT,
    grants        TEXT,
    spec          TEXT NOT NULL,
    since_version INTEGER NOT NULL,
    until_version INTEGER,
    PRIMARY KEY (cube, name, since_version)
);

CREATE TABLE IF NOT EXISTS member_aliases (
    cube          TEXT NOT NULL,
    alias         TEXT NOT NULL,
    target        TEXT NOT NULL,
    spec          TEXT NOT NULL,
    since_version INTEGER NOT NULL,
    until_version INTEGER,
    PRIMARY KEY (cube, alias, since_version)
);

CREATE TABLE IF NOT EXISTS joins (
    cube          TEXT NOT NULL,
    target        TEXT NOT NULL,
    relationship  TEXT NOT NULL,
    on_sql        TEXT NOT NULL,
    spec          TEXT NOT NULL,
    since_version INTEGER NOT NULL,
    until_version INTEGER,
    PRIMARY KEY (cube, target, since_version)
);

CREATE TABLE IF NOT EXISTS segments (
    cube          TEXT NOT NULL,
    name          TEXT NOT NULL,
    sql           TEXT NOT NULL,
    spec          TEXT NOT NULL,
    since_version INTEGER NOT NULL,
    until_version INTEGER,
    PRIMARY KEY (cube, name, since_version)
);

CREATE TABLE IF NOT EXISTS partition_specs (
    cube          TEXT NOT NULL,
    column_name   TEXT NOT NULL,
    kind          TEXT NOT NULL,
    strategy      TEXT NOT NULL,
    granularity   TEXT,
    table_pattern TEXT,
    spec          TEXT NOT NULL,
    since_version INTEGER NOT NULL,
    until_version INTEGER,
    PRIMARY KEY (cube, since_version)
);

CREATE TABLE IF NOT EXISTS schema_bindings (
    cube          TEXT NOT NULL,             -- '*' = every cube
    tenant        TEXT NOT NULL,             -- '*' = every tenant
    schema_name   TEXT NOT NULL,             -- may contain {tenant}
    tenant_column TEXT,
    spec          TEXT NOT NULL,
    since_version INTEGER NOT NULL,
    until_version INTEGER,
    PRIMARY KEY (cube, tenant, since_version)
);

CREATE TABLE IF NOT EXISTS row_policies (
    cube          TEXT NOT NULL,
    name          TEXT NOT NULL,
    filter        TEXT NOT NULL,
    roles         TEXT,
    spec          TEXT NOT NULL,
    since_version INTEGER NOT NULL,
    until_version INTEGER,
    PRIMARY KEY (cube, name, since_version)
);

CREATE TABLE IF NOT EXISTS saved_queries (
    name       TEXT PRIMARY KEY,
    app        TEXT,
    query      TEXT NOT NULL,
    tenant     TEXT,
    roles      TEXT NOT NULL DEFAULT '[]',
    created_by TEXT,
    created_at TEXT NOT NULL
);

-- Who holds which data role. Principals follow the security schema's conventions
-- (access_grants.principal_type): a user email, a Google group email, a domain, or a
-- workspace membership role (owner/admin/developer/viewer). Not versioned: people come
-- and go far more often than the model changes. Changes are audited.
CREATE TABLE IF NOT EXISTS data_role_assignments (
    principal_type TEXT NOT NULL CHECK (principal_type IN ('user', 'group', 'domain', 'workspace_role')),
    principal      TEXT NOT NULL,            -- lower-cased email, group, domain or role
    role           TEXT NOT NULL,            -- e.g. regional_manager, finance
    granted_by     TEXT,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (principal_type, principal, role)
);

-- Values row policies read as $ctx.<key>, e.g. region = 'South' for one user or a whole domain.
-- The most specific principal wins: user > group > domain > workspace_role.
CREATE TABLE IF NOT EXISTS data_attributes (
    principal_type TEXT NOT NULL CHECK (principal_type IN ('user', 'group', 'domain', 'workspace_role')),
    principal      TEXT NOT NULL,
    key            TEXT NOT NULL,
    value          TEXT NOT NULL,            -- JSON scalar or list
    set_by         TEXT,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (principal_type, principal, key)
);

-- Same shape as the security schema's audit_log, so production can write to that table directly.
CREATE TABLE IF NOT EXISTS audit_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id   TEXT,
    actor_user_id  TEXT,
    actor_token_id TEXT,
    actor_type     TEXT NOT NULL CHECK (actor_type IN ('user', 'token', 'system')),
    action         TEXT NOT NULL,            -- data.migration.apply, data.role.assign, ...
    target_type    TEXT NOT NULL,
    target_id      TEXT NOT NULL,
    detail         TEXT NOT NULL DEFAULT '{}',
    at             TEXT NOT NULL
);
"""

CURRENT_UNIQUE = {
    "cubes": "name",
    "members": "cube, name",
    "member_aliases": "cube, alias",
    "joins": "cube, target",
    "segments": "cube, name",
    "partition_specs": "cube",
    "schema_bindings": "cube, tenant",
    "row_policies": "cube, name",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _j(v: Any) -> str:
    return json.dumps(v, sort_keys=True)


Row = dict[str, Any]


def _entities(model: Model) -> dict[tuple[str, tuple], Row]:
    """Flatten a model into catalog rows keyed by (table, key)."""
    out: dict[tuple[str, tuple], Row] = {}
    for cube in model.cubes.values():
        header = cube.header()
        out[("cubes", (cube.name,))] = {
            "name": cube.name, "table_name": cube.table, "sql": cube.sql, "title": cube.title,
            "description": cube.description, "grants": ",".join(cube.grants) or None, "spec": _j(header),
        }
        for m in cube.members():
            out[("members", (cube.name, m.name))] = {
                "cube": cube.name, "name": m.name, "kind": m.kind,
                "agg": getattr(m, "agg", None), "type": getattr(m, "type", None),
                "sql": m.sql, "deprecated": m.deprecated, "grants": ",".join(m.grants) or None,
                "spec": _j(m.model_dump(mode="json")),
            }
        for alias, target in cube.aliases.items():
            out[("member_aliases", (cube.name, alias))] = {"cube": cube.name, "alias": alias, "target": target, "spec": _j(target)}
        for j in cube.joins.values():
            out[("joins", (cube.name, j.to))] = {
                "cube": cube.name, "target": j.to, "relationship": j.relationship, "on_sql": j.on,
                "spec": _j(j.model_dump(mode="json")),
            }
        for s in cube.segments.values():
            out[("segments", (cube.name, s.name))] = {"cube": cube.name, "name": s.name, "sql": s.sql, "spec": _j(s.model_dump(mode="json"))}
        if cube.partition:
            p = cube.partition
            out[("partition_specs", (cube.name,))] = {
                "cube": cube.name, "column_name": p.column, "kind": p.kind, "strategy": p.strategy,
                "granularity": p.granularity, "table_pattern": p.table_pattern, "spec": _j(p.model_dump(mode="json")),
            }
    for b in model.bindings.values():
        out[("schema_bindings", (b.cube, b.tenant))] = {
            "cube": b.cube, "tenant": b.tenant, "schema_name": b.schema_, "tenant_column": b.tenant_column,
            "spec": _j(b.model_dump(mode="json", by_alias=True)),
        }
    for p in model.policies.values():
        out[("row_policies", (p.cube, p.name))] = {
            "cube": p.cube, "name": p.name, "filter": _j(p.filter), "roles": ",".join(p.roles) or None,
            "spec": _j(p.model_dump(mode="json")),
        }
    return out


KEY_COLS = {t: [c.strip() for c in cols.split(",")] for t, cols in CURRENT_UNIQUE.items()}


class Catalog:
    def __init__(self, path: str | Path = ":memory:", dialect: str = "duckdb", workspace_id: Optional[str] = None):
        self.path = str(path)
        self.db = sqlite3.connect(self.path, isolation_level=None)  # explicit transactions only
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(DDL)
        for table, cols in CURRENT_UNIQUE.items():
            self.db.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {table}_current ON {table} ({cols}) WHERE until_version IS NULL")
        self.db.execute("INSERT OR IGNORE INTO settings VALUES ('dialect', ?)", (dialect,))
        if workspace_id:
            self.db.execute("INSERT OR IGNORE INTO settings VALUES ('workspace_id', ?)", (str(workspace_id),))
        self._cache: dict[int, Model] = {}

    # ------------------------------------------------------------ settings & versions

    @property
    def dialect(self) -> str:
        return self.db.execute("SELECT value FROM settings WHERE key = 'dialect'").fetchone()[0]

    def head(self) -> int:
        row = self.db.execute("SELECT MAX(version) FROM model_versions WHERE rolled_back_at IS NULL").fetchone()
        return row[0] or 0

    def _next_version(self) -> int:
        return (self.db.execute("SELECT MAX(version) FROM model_versions").fetchone()[0] or 0) + 1

    def versions(self) -> list[Row]:
        return [dict(r) for r in self.db.execute("SELECT * FROM model_versions ORDER BY version")]

    # ------------------------------------------------------------ model load / store

    def load(self, version: Optional[int] = None) -> Model:
        head = self.head()
        v = head if version is None else version
        if v > head or v < 0:
            raise ModelError(f"model version {v} does not exist (head is {head})")
        if v and self.db.execute("SELECT rolled_back_at FROM model_versions WHERE version = ?", (v,)).fetchone()[0]:
            raise ModelError(f"model version {v} was rolled back")
        if v in self._cache:
            return self._cache[v].model_copy(deep=True)

        def rows(table: str) -> Iterator[sqlite3.Row]:
            return self.db.execute(
                f"SELECT * FROM {table} WHERE since_version <= ? AND (until_version IS NULL OR until_version > ?)", (v, v)
            )

        model = Model()
        for r in rows("cubes"):
            model.cubes[r["name"]] = Cube(**json.loads(r["spec"]))
        for r in rows("members"):
            spec = json.loads(r["spec"])
            cube = model.cubes[r["cube"]]
            if r["kind"] == "measure":
                cube.measures[r["name"]] = Measure(**spec)
            else:
                cube.dimensions[r["name"]] = Dimension(**spec)
        for r in rows("member_aliases"):
            model.cubes[r["cube"]].aliases[r["alias"]] = r["target"]
        for r in rows("joins"):
            model.cubes[r["cube"]].joins[r["target"]] = Join(**json.loads(r["spec"]))
        for r in rows("segments"):
            model.cubes[r["cube"]].segments[r["name"]] = Segment(**json.loads(r["spec"]))
        for r in rows("partition_specs"):
            model.cubes[r["cube"]].partition = PartitionSpec(**json.loads(r["spec"]))
        for r in rows("schema_bindings"):
            b = SchemaBinding(**json.loads(r["spec"]))
            model.bindings[b.key] = b
        for r in rows("row_policies"):
            p = RowPolicy(**json.loads(r["spec"]))
            model.policies[p.key] = p
        # Keep member order stable regardless of row order.
        for cube in model.cubes.values():
            cube.measures = dict(sorted(cube.measures.items()))
            cube.dimensions = dict(sorted(cube.dimensions.items()))
        self._cache[v] = model
        return model.model_copy(deep=True)

    def _write_diff(self, old: Model, new: Model, version: int) -> dict[str, list[str]]:
        before, after = _entities(old), _entities(new)
        diff: dict[str, list[str]] = {"added": [], "changed": [], "removed": []}
        for key in sorted(before.keys() | after.keys()):
            table, ident = key
            label = f"{table}:{'.'.join(ident)}"
            if key in before and key in after and before[key] == after[key]:
                continue
            if key in before:
                where = " AND ".join(f"{c} = ?" for c in KEY_COLS[table])
                self.db.execute(f"UPDATE {table} SET until_version = ? WHERE {where} AND until_version IS NULL", (version, *ident))
            if key in after:
                row = {**after[key], "since_version": version}
                cols = ", ".join(row)
                self.db.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
            diff["changed" if key in before and key in after else "added" if key in after else "removed"].append(label)
        return diff

    def commit_version(self, migration_id: str, old: Model, new: Model, actor: Optional[str]) -> tuple[int, dict]:
        """Persist `new` as the next model version. Caller holds the transaction."""
        version = self._next_version()
        diff = self._write_diff(old, new, version)
        self.db.execute(
            "INSERT INTO model_versions (version, migration_id, fingerprint, applied_at, applied_by) VALUES (?, ?, ?, ?, ?)",
            (version, migration_id, new.fingerprint(), _now(), actor),
        )
        return version, diff

    def pop_version(self, version: int) -> None:
        """Undo exactly what `version` wrote. Only valid for the head version."""
        if version != self.head():
            raise ModelError(f"only the head version ({self.head()}) can be rolled back")
        for table in CURRENT_UNIQUE:
            self.db.execute(f"DELETE FROM {table} WHERE since_version = ?", (version,))
            self.db.execute(f"UPDATE {table} SET until_version = NULL WHERE until_version = ?", (version,))
        self.db.execute("UPDATE model_versions SET rolled_back_at = ? WHERE version = ?", (_now(), version))
        self._cache.pop(version, None)

    # ------------------------------------------------------------ migrations, audit, saved queries

    def migration(self, mid: str) -> Optional[Row]:
        r = self.db.execute("SELECT * FROM migrations WHERE id = ?", (mid,)).fetchone()
        return dict(r) if r else None

    def migrations(self, status: Optional[str] = None) -> list[Row]:
        sql = "SELECT * FROM migrations" + (" WHERE status = ?" if status else "") + " ORDER BY COALESCE(version, 1e9), id"
        return [dict(r) for r in self.db.execute(sql, (status,) if status else ())]

    def upsert_migration(self, **fields: Any) -> None:
        fields.setdefault("created_at", _now())
        existing = self.migration(fields["id"])
        if existing:
            fields.pop("created_at")
            sets = ", ".join(f"{k} = ?" for k in fields if k != "id")
            self.db.execute(f"UPDATE migrations SET {sets} WHERE id = ?", (*[v for k, v in fields.items() if k != "id"], fields["id"]))
        else:
            cols = ", ".join(fields)
            self.db.execute(f"INSERT INTO migrations ({cols}) VALUES ({', '.join('?' * len(fields))})", tuple(fields.values()))

    def applied_since(self, version: int) -> list[Row]:
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM migrations WHERE status = 'applied' AND version > ? ORDER BY version", (version,)
            )
        ]

    @property
    def workspace_id(self) -> Optional[str]:
        row = self.db.execute("SELECT value FROM settings WHERE key = 'workspace_id'").fetchone()
        return row[0] if row else None

    def audit(self, action: str, actor: Optional[str] = None, migration_id: Optional[str] = None,
              version: Optional[int] = None, *, target_type: str = "migration", target_id: Optional[str] = None,
              **details: Any) -> None:
        """Record an event. `actor` is a principal string: 'user:<id>', 'token:<id>' or a free-form system name."""
        kind, _, ident = (actor or "system").partition(":")
        if kind not in {"user", "token"} or not ident:
            kind, ident = "system", None
            if actor:
                details["actor"] = actor
        if version is not None:
            details["version"] = version
        self.db.execute(
            "INSERT INTO audit_log (workspace_id, actor_user_id, actor_token_id, actor_type, action, target_type, target_id, detail, at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.workspace_id, ident if kind == "user" else None, ident if kind == "token" else None, kind,
             action if "." in action else f"data.migration.{action}", target_type, target_id or migration_id or "-",
             _j(details), _now()),
        )

    # ------------------------------------------------------------ data roles and attributes

    def assign_role(self, principal_type: str, principal: str, role: str, by: Optional[str] = None) -> None:
        with self.transaction():
            self.db.execute("INSERT OR IGNORE INTO data_role_assignments VALUES (?, ?, ?, ?, ?)",
                            (principal_type, principal.lower(), role, by, _now()))
            self.audit("data.role.assign", by, target_type="data_role", target_id=role,
                       principal=f"{principal_type}:{principal.lower()}")

    def unassign_role(self, principal_type: str, principal: str, role: str, by: Optional[str] = None) -> None:
        with self.transaction():
            self.db.execute("DELETE FROM data_role_assignments WHERE principal_type = ? AND principal = ? AND role = ?",
                            (principal_type, principal.lower(), role))
            self.audit("data.role.unassign", by, target_type="data_role", target_id=role,
                       principal=f"{principal_type}:{principal.lower()}")

    def set_attribute(self, principal_type: str, principal: str, key: str, value: Any, by: Optional[str] = None) -> None:
        with self.transaction():
            self.db.execute("INSERT OR REPLACE INTO data_attributes VALUES (?, ?, ?, ?, ?, ?)",
                            (principal_type, principal.lower(), key, json.dumps(value), by, _now()))
            self.audit("data.attribute.set", by, target_type="data_attribute", target_id=key,
                       principal=f"{principal_type}:{principal.lower()}")

    def principal_grants(self, principals: list[tuple[str, str]]) -> tuple[set[str], dict[str, Any]]:
        """Roles and attributes for a set of (principal_type, principal), most specific attribute wins."""
        if not principals:
            return set(), {}
        where = " OR ".join("(principal_type = ? AND principal = ?)" for _ in principals)
        args = [x for p in principals for x in (p[0], p[1].lower())]
        roles = {r[0] for r in self.db.execute(f"SELECT role FROM data_role_assignments WHERE {where}", args)}
        rank = {"workspace_role": 0, "domain": 1, "group": 2, "user": 3}
        attrs: dict[str, Any] = {}
        rows = self.db.execute(f"SELECT principal_type, key, value FROM data_attributes WHERE {where}", args).fetchall()
        for ptype, key, value in sorted(rows, key=lambda r: rank[r[0]]):
            attrs[key] = json.loads(value)
        return roles, attrs

    def audit_log(self) -> list[Row]:
        return [dict(r) for r in self.db.execute("SELECT * FROM audit_log ORDER BY id")]

    def save_query(self, name: str, query: dict, app: Optional[str] = None, tenant: Optional[str] = None,
                   roles: list[str] | None = None, created_by: Optional[str] = None) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO saved_queries (name, app, query, tenant, roles, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, app, _j(query), tenant, _j(roles or []), created_by, _now()),
        )

    def saved_queries(self) -> list[Row]:
        out = []
        for r in self.db.execute("SELECT * FROM saved_queries ORDER BY name"):
            d = dict(r)
            d["query"], d["roles"] = json.loads(d["query"]), json.loads(d["roles"])
            out.append(d)
        return out

    def transaction(self) -> "_Tx":
        return _Tx(self)

    def history(self, cube: str, member: str) -> list[Row]:
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT name, kind, agg, sql, since_version, until_version FROM members WHERE cube = ? AND name = ? ORDER BY since_version",
                (cube, member),
            )
        ]


class _Tx:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog

    def __enter__(self) -> Catalog:
        self.catalog.db.execute("BEGIN IMMEDIATE")
        return self.catalog

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type:
            self.catalog.db.execute("ROLLBACK")
            self.catalog._cache.clear()
        else:
            self.catalog.db.execute("COMMIT")

