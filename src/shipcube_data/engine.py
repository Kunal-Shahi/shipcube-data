"""The data layer facade: what ShipCube's MCP tools and apps call."""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from typing import Any, Callable, Optional

from . import rawsql
from .catalog import Catalog
from .compiler import Compiler, SecurityContext
from .migrate import Runner
from .model import Model, ModelError, humanize
from .obs import Metrics, NoMetrics, Timer, log_event, new_query_id, sql_hash
from .query import Condition, Order, Query
from .warehouses import DuckDBWarehouse, Executor, QueryLimits


def duckdb_executor(con) -> Executor:
    """A guarded executor over a DuckDB connection (local development and tests)."""
    return DuckDBWarehouse(con)


class ResultCache:
    """LRU result cache. Keys include the fingerprint of the model parts a query uses,
    so a migration invalidates exactly the queries it can affect."""

    def __init__(self, size: int = 512, ttl_seconds: float = 300):
        self.size, self.ttl = size, ttl_seconds
        self.data: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self.hits = self.misses = 0

    @staticmethod
    def key(fingerprint: str, query: Query, ctx: SecurityContext, dialect: str, freshness: str = "") -> str:
        body = {
            "fp": fingerprint,
            "q": query.model_dump(mode="json", by_alias=True, exclude={"model_version"}, exclude_defaults=True),
            "tenant": ctx.tenant,
            "roles": sorted(ctx.roles),
            "attrs": ctx.attrs,
            "dialect": dialect,
            "fresh": freshness,
        }
        return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()

    def get(self, key: str) -> Optional[Any]:
        hit = self.data.get(key)
        if hit and time.monotonic() - hit[0] < self.ttl:
            self.data.move_to_end(key)
            self.hits += 1
            return hit[1]
        self.misses += 1
        return None

    def put(self, key: str, value: Any) -> None:
        self.data[key] = (time.monotonic(), value)
        self.data.move_to_end(key)
        while len(self.data) > self.size:
            self.data.popitem(last=False)


def pin_definitions(head: Model, pinned: Model, keep_head: frozenset[tuple[str, str]] = frozenset()) -> Model:
    """The model a pinned query runs against: head, with member *definitions* taken from the pin.

    Everything that decides who sees what stays at head: grants, row policies, schema bindings,
    joins, partitions, segments and the cube sources. A pin can only bring back how a member
    that still exists was computed (its SQL, aggregation and filter). Members that no longer
    exist at head are not resurrected. (Red-team issue #2: pinning an old version must never
    bring back old access rules.)

    `keep_head` lists (cube, member) whose definition is an access rule since the pin: a masking
    `change_sql`, or a member dropped and re-created under the same name. Those keep head's definition.
    """
    out = head.model_copy(deep=True)
    for cube in out.cubes.values():
        old_cube = pinned.cubes.get(cube.name)
        if old_cube is None:
            continue
        # names under which each current member was known at the pin: itself, plus renamed-from aliases
        names_for: dict[str, list[str]] = {m.name: [m.name] for m in cube.members()}
        for alias, target in cube.aliases.items():
            names_for.setdefault(target, []).append(alias)
        for member in cube.members():
            if (cube.name, member.name) in keep_head:
                continue
            old = next((old_cube.member(n) for n in names_for[member.name] if old_cube.member(n)), None)
            if old is None or old.kind != member.kind:
                continue
            member.sql = old.sql
            if hasattr(member, "agg"):
                member.agg, member.filter = old.agg, old.filter  # type: ignore[attr-defined]
            else:
                member.type = old.type  # type: ignore[attr-defined]
    return out


class DataLayer:
    def __init__(self, catalog: Catalog, execute: Executor, cache: Optional[ResultCache] = None,
                 limits: Optional[QueryLimits] = None, metrics: Optional[Metrics] = None):
        self.catalog, self.execute = catalog, execute
        self.cache = cache or ResultCache()
        self.limits = limits or QueryLimits.from_env()
        self.metrics = metrics or NoMetrics()
        self.runner = Runner(catalog)

    @classmethod
    def from_catalog(cls, catalog: Catalog, resolve_secret=None, connectors=None, datasource: str = "primary", **kwargs) -> "DataLayer":
        """Build a DataLayer that queries the catalog's registered datasource (see datasources.py).

        The warehouse is connected on the first query, not here: browsing the model, reviewing
        migrations or replacing a broken warehouse must not depend on the current one being reachable."""
        from .datasources import open_executor

        state: dict[str, Executor] = {}

        def lazy(sql: str, limits: QueryLimits, query_id: str):
            if "ex" not in state:
                state["ex"] = open_executor(catalog, resolve_secret, datasource, connectors)
            return state["ex"](sql, limits, query_id)

        return cls(catalog, lazy, **kwargs)

    @property
    def dialect(self) -> str:
        return self.catalog.dialect

    def _model(self, version: Optional[int] = None) -> Model:
        return self.catalog.load(version)

    # ------------------------------------------------------------ discovery

    def list_cubes(self, ctx: SecurityContext) -> list[dict[str, Any]]:
        model, comp = self._model(), None
        comp = Compiler(model, ctx, self.dialect, self.dialect)
        out = []
        for cube in model.cubes.values():
            members = [m for m in cube.members() if comp.visible(cube, m)]
            if not members:
                continue
            out.append({
                "name": cube.name,
                "title": cube.title or humanize(cube.name),
                "description": cube.description,
                "measures": [m.name for m in members if m.kind == "measure"],
                "dimensions": [m.name for m in members if m.kind == "dimension"],
                "joins": sorted(set(cube.joins) | {c.name for c in model.cubes.values() if cube.name in c.joins}),
            })
        return out

    def describe_cube(self, name: str, ctx: SecurityContext) -> dict[str, Any]:
        model = self._model()
        cube = model.cube(name)
        comp = Compiler(model, ctx, self.dialect, self.dialect)

        def member(m) -> dict[str, Any]:
            d = {
                "path": f"{cube.name}.{m.name}",
                "title": m.title or humanize(m.name),
                "description": m.description,
                "format": m.format,
            }
            if m.kind == "measure":
                d |= {"agg": m.agg, "additive": m.agg in {"sum", "count"}}
            else:
                d |= {"type": m.type, "granularities": ["day", "month", "year"] if m.type == "time" else None}
            if m.deprecated:
                d |= {"deprecated": m.deprecated, "replaced_by": m.replaced_by}
            return {k: v for k, v in d.items() if v is not None}

        visible = [m for m in cube.members() if comp.visible(cube, m)]
        if not visible:
            raise ModelError(f"unknown cube {name!r}")
        joins = [{"to": e.dst, "relationship": e.relationship} for e in comp._edges(cube.name)]
        part = cube.partition
        return {
            "name": cube.name,
            "title": cube.title or humanize(cube.name),
            "description": cube.description,
            "measures": [member(m) for m in visible if m.kind == "measure"],
            "dimensions": [member(m) for m in visible if m.kind == "dimension"],
            "segments": [{"path": f"{cube.name}.{s.name}", "title": s.title or humanize(s.name), "description": s.description} for s in cube.segments.values()],
            "joins": joins,
            "aliases": {f"{cube.name}.{a}": f"{cube.name}.{t}" for a, t in cube.aliases.items()},
            "partitioned_by": {"column": part.column, "kind": part.kind, "granularity": part.granularity,
                               "filter_required": part.require_filter} if part else None,
            "model_version": self.catalog.head(),
        }

    # ------------------------------------------------------------ querying

    def compile(self, query: Query | dict, ctx: SecurityContext, dialect: Optional[str] = None):
        q = query if isinstance(query, Query) else Query.model_validate(query)
        model = self._model()
        kept: frozenset[tuple[str, str]] = frozenset()
        if q.model_version is not None and q.model_version != self.catalog.head():
            kept = self.access_changes_since(model, q.model_version)
            model = pin_definitions(model, self._model(q.model_version), kept)
        compiled = Compiler(model, ctx, dialect or self.dialect, self.dialect).compile(q)
        compiled.warnings += [f"{c}.{m} uses the current definition: it was masked or re-created after "
                              f"model_version {q.model_version}" for c, m in sorted(kept)]
        return compiled, q

    def access_changes_since(self, head: Model, version: int) -> frozenset[tuple[str, str]]:
        """Members (by their current names) whose definition became an access rule after `version`:
        a `change_sql` with `masking`, or a `drop_member` (a member re-created later is a new member,
        so the pin must not hand back the old one's SQL)."""
        out: set[tuple[str, str]] = set()
        for rec in self.catalog.applied_since(version):
            ops = json.loads(rec["ops"]) if isinstance(rec["ops"], str) else rec["ops"]
            for op in ops:
                if (op.get("op") == "change_sql" and op.get("masking")) or op.get("op") == "drop_member":
                    cube_name, _, name = op["member"].partition(".")
                    cube = head.cubes.get(cube_name)
                    if cube is not None:
                        out.add((cube_name, cube.aliases.get(name, name)))
        return frozenset(out)

    def query(self, query: Query | dict, ctx: SecurityContext, *, use_cache: bool = True) -> dict[str, Any]:
        """Compile, run under the guardrails, and log one `query.ok` / `query.error` event."""
        query_id = new_query_id()
        timer = Timer().__enter__()
        fields: dict[str, Any] = {"query_id": query_id, "tenant": ctx.tenant, "workspace_id": ctx.attrs.get("workspace_id"),
                                  "user": ctx.user, "dialect": self.dialect}
        try:
            q = query if isinstance(query, Query) else Query.model_validate(query)
            if q.limit and q.limit > self.limits.max_rows:
                raise ModelError(f"limit {q.limit} is over this workspace's row limit of {self.limits.max_rows}", "invalid")
            if not q.limit:
                q = q.model_copy(update={"limit": self.limits.max_rows})
            compiled, q = self.compile(q, ctx)
            version = q.model_version or self.catalog.head()
            fields |= {"cubes": compiled.cubes, "fingerprint": compiled.fingerprint, "model_version": version,
                       "sql_hash": sql_hash(compiled.sql),
                       "partitions_scanned": {c: p.get("scanned") for c, p in compiled.partitions.items()}}
            key = ResultCache.key(compiled.fingerprint, q, ctx, self.dialect)
            cached = self.cache.get(key) if use_cache else None
            stats: dict[str, Any] = {}
            truncated = False
            if cached is None:
                try:
                    res = self.execute(compiled.sql, self.limits, query_id)
                except ModelError as e:
                    if e.code != "missing_relation":
                        raise
                    raise ModelError(
                        f"no data is provisioned for tenant {ctx.tenant!r} in this workspace ({e})", "tenant_not_provisioned",
                        "an admin maps your Google domain or email to a provisioned tenant (data attribute `tenant`), "
                        "or loads data into that tenant's schema",
                    ) from None
                rows, truncated, stats = [dict(zip(res.columns, r)) for r in res.rows], res.truncated, res.stats
                if use_cache and not truncated:
                    self.cache.put(key, rows)
                hit = False
            else:
                rows, hit = cached, True
        except ModelError as e:
            log_event("query.error", level=30, **fields, code=e.code, error=str(e)[:300], elapsed_ms=timer.elapsed_ms)
            self.metrics.incr("data_query_errors_total", code=e.code)
            raise
        warnings = list(compiled.warnings)
        if truncated:
            warnings.append(f"result truncated at {self.limits.max_rows} rows; add filters or a smaller limit")
        elapsed = timer.elapsed_ms
        log_event("query.ok", **fields, rows=len(rows), truncated=truncated, cache="hit" if hit else "miss",
                  elapsed_ms=elapsed, **{f"wh_{k}": v for k, v in stats.items()})
        self.metrics.observe("data_query_duration_ms", elapsed, cache="hit" if hit else "miss")
        self.metrics.incr("data_queries_total", cache="hit" if hit else "miss")
        return {
            "query_id": query_id,
            "columns": [c.__dict__ for c in compiled.columns],
            "rows": rows,
            "truncated": truncated,
            "sql": compiled.sql,
            "model_version": version,
            "fingerprint": compiled.fingerprint,
            "partitions": compiled.partitions,
            "warnings": warnings,
            "verified": True,
            "cache": "hit" if hit else "miss",
            "stats": {"elapsed_ms": elapsed, **stats},
        }

    def search_values(self, dimension: str, text: str = "", ctx: Optional[SecurityContext] = None, limit: int = 20) -> list[Any]:
        assert ctx is not None
        q = Query(
            dimensions=[dimension],
            filters=[Condition(member=dimension, op="contains", values=[text])] if text else [],
            order=[Order(member=dimension)],
            limit=limit,
        )
        res = self.query(q, ctx)
        col = res["columns"][0]["name"]
        return [r[col] for r in res["rows"]]

    def raw_sql(self, sql: str, ctx: SecurityContext, limit: int = 1000) -> dict[str, Any]:
        query_id = new_query_id()
        limit = min(limit, self.limits.max_rows)
        try:
            safe = rawsql.validate(sql, self._model(), ctx, self.dialect, limit)
            res = self.execute(safe, self.limits, query_id)
        except ModelError as e:
            log_event("raw_sql.error", level=30, query_id=query_id, tenant=ctx.tenant, code=e.code, error=str(e)[:300])
            raise
        log_event("raw_sql.ok", query_id=query_id, tenant=ctx.tenant, user=ctx.user, sql_hash=sql_hash(safe), rows=len(res.rows))
        return {"query_id": query_id, "columns": [{"name": n} for n in res.columns],
                "rows": [dict(zip(res.columns, r)) for r in res.rows], "sql": safe, "truncated": res.truncated,
                "verified": False, "warnings": ["raw SQL: not checked against the semantic model; numbers may not match governed metrics"]}
