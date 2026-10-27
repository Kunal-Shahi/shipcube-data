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
from .query import Condition, Order, Query

Executor = Callable[[str], tuple[list[str], list[tuple]]]


def duckdb_executor(con) -> Executor:
    def run(sql: str) -> tuple[list[str], list[tuple]]:
        cur = con.execute(sql)
        return [d[0] for d in cur.description], cur.fetchall()

    return run


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


class DataLayer:
    def __init__(self, catalog: Catalog, execute: Executor, cache: Optional[ResultCache] = None):
        self.catalog, self.execute = catalog, execute
        self.cache = cache or ResultCache()
        self.runner = Runner(catalog)

    @classmethod
    def from_catalog(cls, catalog: Catalog, resolve_secret=None, connectors=None, datasource: str = "primary") -> "DataLayer":
        """Build a DataLayer that queries the catalog's registered datasource (see datasources.py)."""
        from .datasources import open_executor

        return cls(catalog, open_executor(catalog, resolve_secret, datasource, connectors))

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
        model = self._model(q.model_version)
        return Compiler(model, ctx, dialect or self.dialect, self.dialect).compile(q), q

    def query(self, query: Query | dict, ctx: SecurityContext, *, use_cache: bool = True) -> dict[str, Any]:
        compiled, q = self.compile(query, ctx)
        version = q.model_version or self.catalog.head()
        key = ResultCache.key(compiled.fingerprint, q, ctx, self.dialect)
        cached = self.cache.get(key) if use_cache else None
        if cached is None:
            names, rows = self.execute(compiled.sql)
            cached = [dict(zip(names, r)) for r in rows]
            if use_cache:
                self.cache.put(key, cached)
            hit = False
        else:
            hit = True
        return {
            "columns": [c.__dict__ for c in compiled.columns],
            "rows": cached,
            "sql": compiled.sql,
            "model_version": version,
            "fingerprint": compiled.fingerprint,
            "partitions": compiled.partitions,
            "warnings": compiled.warnings,
            "verified": True,
            "cache": "hit" if hit else "miss",
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
        safe = rawsql.validate(sql, self._model(), ctx, self.dialect, limit)
        names, rows = self.execute(safe)
        return {"columns": [{"name": n} for n in names], "rows": [dict(zip(names, r)) for r in rows], "sql": safe,
                "verified": False, "warnings": ["raw SQL: not checked against the semantic model; numbers may not match governed metrics"]}
