"""The catalog database: Postgres in production (a schema per workspace), SQLite for local development.

Every model table is versioned SCD2-style: a row is valid from `since_version`
until `until_version` (exclusive; NULL = still current). That gives us:

* the model as of any version, for pinned queries and cache keys;
* exact, generic rollback of the head migration (delete rows it added, reopen
  rows it closed), so ops do not need hand-written "down" steps;
* a queryable history of every definition a member has ever had.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from typing import Any, Optional

from .db import Db, workspace_schema
from .model import Cube, Dimension, Join, Measure, Model, ModelError, PartitionSpec, RowPolicy, SchemaBinding, Segment

# The catalog schema itself lives in versioned migrations: catalog_migrations/<backend>/NNNN_*.sql.
# These are the SCD2 model tables and the key that identifies one row's current version.
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


def _loads(v: Any) -> Any:
    """JSON columns are TEXT on SQLite and JSONB (already decoded) on Postgres."""
    return json.loads(v) if isinstance(v, (str, bytes)) else v


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
    """One workspace's catalog.

    `url` is a SQLite path (":memory:" by default) or a `postgresql://` URL. On Postgres each
    workspace gets its own schema, `catalog_ws_<uuid hex>`. Pending catalog-schema migrations
    are applied on open unless `migrate=False` (production runs them in a deploy step:
    `shipcube-data catalog-migrate`). One Catalog holds one connection: use one per thread.
    """

    def __init__(self, url: str | Path = ":memory:", dialect: str = "duckdb", workspace_id: Optional[str] = None,
                 migrate: bool = True):
        self.url = str(url)
        self.db = Db(self.url, schema=workspace_schema(str(workspace_id) if workspace_id else None), create_schema=migrate)
        if migrate:
            self.db.migrate_schema()
        else:
            pending = self.db.pending_schema_migrations()
            if pending:
                self.db.close()
                raise ModelError(f"catalog schema is behind ({', '.join(pending)} pending); run `shipcube-data catalog-migrate`",
                                 "catalog_not_migrated")
        self.db.execute("INSERT INTO settings (key, value) VALUES ('dialect', ?) ON CONFLICT (key) DO NOTHING", (dialect,))
        if workspace_id:
            self.db.execute("INSERT INTO settings (key, value) VALUES ('workspace_id', ?) ON CONFLICT (key) DO NOTHING", (str(workspace_id),))
        self._cache: dict[int, Model] = {}

    def set_setting(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))

    def close(self) -> None:
        self.db.close()

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

        def rows(table: str):
            return self.db.execute(
                f"SELECT * FROM {table} WHERE since_version <= ? AND (until_version IS NULL OR until_version > ?)", (v, v)
            )

        model = Model()
        for r in rows("cubes"):
            model.cubes[r["name"]] = Cube(**_loads(r["spec"]))
        for r in rows("members"):
            spec = _loads(r["spec"])
            cube = model.cubes[r["cube"]]
            if r["kind"] == "measure":
                cube.measures[r["name"]] = Measure(**spec)
            else:
                cube.dimensions[r["name"]] = Dimension(**spec)
        for r in rows("member_aliases"):
            model.cubes[r["cube"]].aliases[r["alias"]] = r["target"]
        for r in rows("joins"):
            model.cubes[r["cube"]].joins[r["target"]] = Join(**_loads(r["spec"]))
        for r in rows("segments"):
            model.cubes[r["cube"]].segments[r["name"]] = Segment(**_loads(r["spec"]))
        for r in rows("partition_specs"):
            model.cubes[r["cube"]].partition = PartitionSpec(**_loads(r["spec"]))
        for r in rows("schema_bindings"):
            b = SchemaBinding(**_loads(r["spec"]))
            model.bindings[b.key] = b
        for r in rows("row_policies"):
            p = RowPolicy(**_loads(r["spec"]))
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
            self.db.execute("INSERT INTO data_role_assignments (principal_type, principal, role, granted_by, created_at) "
                            "VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
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
            self.db.execute("INSERT INTO data_attributes (principal_type, principal, key, value, set_by, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (principal_type, principal, key) "
                            "DO UPDATE SET value = excluded.value, set_by = excluded.set_by, created_at = excluded.created_at",
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
        for r in sorted(rows, key=lambda r: rank[r["principal_type"]]):
            attrs[r["key"]] = json.loads(r["value"])
        return roles, attrs

    def audit_log(self) -> list[Row]:
        return [dict(r) for r in self.db.execute("SELECT * FROM audit_log ORDER BY id")]

    def save_query(self, name: str, query: dict, app: Optional[str] = None, tenant: Optional[str] = None,
                   roles: list[str] | None = None, created_by: Optional[str] = None) -> None:
        self.db.execute(
            "INSERT INTO saved_queries (name, app, query, tenant, roles, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (name) DO UPDATE SET app = excluded.app, query = excluded.query, tenant = excluded.tenant, "
            "roles = excluded.roles, created_by = excluded.created_by, created_at = excluded.created_at",
            (name, app, _j(query), tenant, _j(roles or []), created_by, _now()),
        )

    def saved_queries(self) -> list[Row]:
        out = []
        for r in self.db.execute("SELECT * FROM saved_queries ORDER BY name"):
            d = dict(r)
            d["query"], d["roles"] = _loads(d["query"]), _loads(d["roles"])
            out.append(d)
        return out

    @contextmanager
    def transaction(self, lock: bool = True) -> Iterator["Catalog"]:
        """A write transaction holding this catalog's lock; nested calls join the outer one."""
        try:
            with self.db.transaction(lock=lock):
                yield self
        except BaseException:
            self._cache.clear()
            raise

    def history(self, cube: str, member: str) -> list[Row]:
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT name, kind, agg, sql, since_version, until_version FROM members WHERE cube = ? AND name = ? ORDER BY since_version",
                (cube, member),
            )
        ]
