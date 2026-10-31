"""Pre-aggregations ("rollups"): declared in the model, built in the warehouse one month at a time, and
used for any typed query they can answer exactly.

* **Declared** with `define_rollup`: one cube's measures by some dimensions and a time bucket. It
  changes no answers, so it's an additive migration.
* **Built** by `build()`: for each month, the regular compiler compiles the rollup as an L0 query with
  grants and row policies bypassed (system mode, internal only), so the rollup holds the tenant's
  complete data. The rows go into `<rollup schema>.<cube>__<rollup>__<hash>` in the warehouse, through
  a separate writer connection; the catalog records which months are built, per tenant and per model
  fingerprint. A change to any definition the rollup reads changes the fingerprint, so stale tables
  are never used.
* **Routed** in `Compiler.compile`: a query is answered from a rollup only when it can be, exactly.
  - Its measures re-aggregate from the rollup's parts: sum, count, min, max; avg as sum/count; number
    measures built from those. Count-distinct can't, and is refused in rollups.
  - Its dimensions and filters are in the rollup, and its time filters align with the rollup's grain.
  - Every row policy that applies to the caller can be evaluated on the rollup's columns, and is.
    Grants are checked as for any query, before routing.
  - Every month it needs is built from the current definitions, and fresh: built after the month
    ended, or less than `max_age_hours` ago.
  Otherwise the query goes to the warehouse as usual.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, Optional

from sqlglot import exp

from .model import Cube, Dimension, Measure, Model, ModelError, Rollup
from .query import Condition, Query, filter_members
from .sqlutil import has_aggregate, parse_expr, ref_name, refs

if TYPE_CHECKING:
    from .compiler import Compiler
    from .engine import DataLayer

REAGG = {"sum": "SUM", "count": "SUM", "min": "MIN", "max": "MAX"}
GRAIN_ORDER = {"day": 0, "week": 1, "month": 2, "quarter": 3, "year": 4}
SERVES = {"day": {"day", "week", "month", "quarter", "year"}, "week": {"week"}, "month": {"month", "quarter", "year"}}
DEFAULT_SCHEMA = "shipcube_rollups"


# ---------------------------------------------------------------- definition

@dataclass(frozen=True)
class Component:
    path: str  # measure path in the build model (synthetic for avg parts)
    reagg: str  # SUM / MIN / MAX


def components(model: Model, cube: Cube, m: Measure, dialect: str, seen: frozenset = frozenset()) -> list[Component]:
    """The re-aggregatable parts measure `m` is computed from. Raises if there are none."""
    path = f"{cube.name}.{m.name}"
    if m.agg in REAGG:
        return [Component(path, REAGG[m.agg])]
    if m.agg == "avg":
        return [Component(f"{cube.name}.rollup_sum__{m.name}", "SUM"), Component(f"{cube.name}.rollup_cnt__{m.name}", "SUM")]
    if m.agg == "number":
        tree = parse_expr(m.sql or "", dialect)
        if has_aggregate(tree) or any(ref_name(c) is None for c in tree.find_all(exp.Column)):
            raise ModelError(f"{path} aggregates in its own SQL, so it can't be re-aggregated from a rollup")
        out: list[Component] = []
        for ref in refs(m.sql):
            target = cube.member(ref.rpartition(".")[2])
            if not isinstance(target, Measure) or target.name in seen:
                raise ModelError(f"{path}: {{{ref}}} isn't a measure a rollup can hold")
            out += [c for c in components(model, cube, target, dialect, seen | {m.name}) if c not in out]
        return out
    raise ModelError(f"{path} is {m.agg}: it can't be re-aggregated from a rollup (count_distinct needs the raw rows)")


def build_model(model: Model, cube: Cube, r: Rollup) -> Model:
    """The model the rollup is built from: plus a sum and a count measure for every avg it holds."""
    out = model.model_copy(deep=True)
    c = out.cube(cube.name)
    for path in r.measures:
        m = c.member(path.split(".", 1)[1])
        if isinstance(m, Measure) and m.agg == "avg":
            c.measures[f"rollup_sum__{m.name}"] = Measure(name=f"rollup_sum__{m.name}", agg="sum", sql=m.sql, filter=m.filter)
            c.measures[f"rollup_cnt__{m.name}"] = Measure(name=f"rollup_cnt__{m.name}", agg="count", sql=m.sql, filter=m.filter)
    return out


def rollup_components(model: Model, cube: Cube, r: Rollup, dialect: str) -> list[Component]:
    out: list[Component] = []
    for path in r.measures:
        m = cube.member(path.split(".", 1)[1]) if path.startswith(cube.name + ".") else None
        if not isinstance(m, Measure):
            raise ModelError(f"rollup measure {path} must be a measure of {cube.name}")
        out += [c for c in components(model, cube, m, dialect) if c not in out]
    return out


def involved_cubes(comp: "Compiler", cube: Cube, r: Rollup) -> set[str]:
    needed = {cube.name} | {comp.model.resolve(p)[0].name for p in [*r.dimensions, r.time_dimension]}
    tree = comp.join_tree(cube.name, needed)
    bad = [e for e in tree if e.relationship not in {"many_to_one", "one_to_one"}]
    if bad:
        raise ModelError(f"rollup {cube.name}.{r.name}: {bad[0].dst} is joined {bad[0].relationship}; "
                         "a rollup's dimensions must be reachable many-to-one")
    return {cube.name} | {e.dst for e in tree}


def validate_rollup(model: Model, cube: Cube, r: Rollup, dialect: str) -> None:
    from .compiler import Compiler, SecurityContext

    rollup_components(model, cube, r, dialect)
    for p in r.dimensions:
        c, m = model.resolve(p)
        if not isinstance(m, Dimension) or p != f"{c.name}.{m.name}":
            raise ModelError(f"rollup dimension {p} must be a dimension, by its current name")
    c, t = model.resolve(r.time_dimension)
    if not isinstance(t, Dimension) or t.type != "time":
        raise ModelError(f"rollup time_dimension {r.time_dimension} must be a time dimension")
    try:
        date.fromisoformat(r.start)
    except ValueError:
        raise ModelError(f"rollup start {r.start!r} must be an ISO date") from None
    involved_cubes(Compiler(model, SecurityContext(tenant="system"), dialect, dialect, system=True), cube, r)


def fingerprint(model: Model, cube: Cube, r: Rollup, cubes: set[str]) -> str:
    """Changes whenever anything the rollup's numbers depend on changes: its own definition, and the
    sources, joins, partitions, bindings and member definitions of the cubes it reads. Grants and
    policies aren't in it: they're applied when the rollup is read."""
    def definition(c: Cube) -> dict:
        return {"table": c.table, "sql": c.sql, "partition": c.partition.model_dump(mode="json") if c.partition else None,
                "joins": {k: j.model_dump(mode="json") for k, j in sorted(c.joins.items())},
                "members": {m.name: [m.kind, m.sql, getattr(m, "agg", None), getattr(m, "filter", None), getattr(m, "type", None)]
                            for m in c.members()}}

    part = {"rollup": r.model_dump(mode="json", exclude={"max_age_hours"}),
            "cubes": {n: definition(model.cubes[n]) for n in sorted(cubes)},
            "bindings": {k: b.model_dump(mode="json", by_alias=True) for k, b in sorted(model.bindings.items())
                         if b.cube in cubes or b.cube == "*"}}
    return hashlib.sha256(json.dumps(part, sort_keys=True, default=str).encode()).hexdigest()[:16]


def table_name(cube: str, rollup: str, tenant: str, fp: str) -> str:
    h = hashlib.sha256(f"{tenant}:{fp}".encode()).hexdigest()[:12]
    return f"{cube}__{rollup}"[:48] + f"__{h}"


def months(start: str, end: Optional[date] = None) -> list[date]:
    d = date.fromisoformat(start).replace(day=1)
    end = (end or datetime.now(timezone.utc).date()).replace(day=1)
    out = []
    while d <= end:
        out.append(d)
        d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return out


def _next_month(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1)


# ---------------------------------------------------------------- build

Writer = Callable[[list[str]], int]


def postgres_writer(conninfo: str) -> Writer:
    """Runs a build step in one transaction on a connection that may write (the warehouse adapter's can't)."""
    import psycopg

    def write(statements: list[str]) -> int:
        with psycopg.connect(conninfo) as c:
            c.execute("SET statement_timeout = 0")
            count = 0
            with c.transaction():
                for s in statements:
                    cur = c.execute(s)
                    count = cur.rowcount
            return count

    return write


def duckdb_writer(con) -> Writer:
    def write(statements: list[str]) -> int:
        count = 0
        con.execute("BEGIN")
        try:
            for s in statements:
                res = con.execute(s)
                if s.lstrip().upper().startswith("INSERT"):
                    count = res.fetchone()[0]
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        return count

    return write


def build(layer: "DataLayer", tenant: str, rollup: str, write: Writer, *, periods: Optional[list[str]] = None,
          schema: Optional[str] = None) -> dict[str, Any]:
    """Build (or rebuild) months of `cube.rollup` for `tenant`. Default: every month from its start to now."""
    from .compiler import Compiler, SecurityContext

    schema = schema or layer.rollup_schema
    model = layer.catalog.load()
    cube_name, _, name = rollup.partition(".")
    cube = model.cube(cube_name)
    if name not in cube.rollups:
        raise ModelError(f"unknown rollup {rollup}")
    r = cube.rollups[name]
    dialect = layer.dialect
    system = Compiler(model, SecurityContext(tenant=tenant), dialect, dialect, system=True)
    cubes = involved_cubes(system, cube, r)
    fp = fingerprint(model, cube, r, cubes)
    parts = rollup_components(model, cube, r, dialect)
    bmodel = build_model(model, cube, r)
    table = f"{schema}.{table_name(cube_name, name, tenant, fp)}"
    todo = [date.fromisoformat(p).replace(day=1) for p in periods] if periods else months(r.start)
    built = []
    for p in todo:
        q = Query(measures=[c.path for c in parts], dimensions=list(r.dimensions),
                  time={"dimension": r.time_dimension, "granularity": r.granularity,
                        "range": [p.isoformat(), (_next_month(p) - timedelta(days=1)).isoformat()]})
        compiled = Compiler(bmodel, SecurityContext(tenant=tenant), dialect, dialect, system=True).compile(q, as_stage=True)
        body = compiled.expression.sql(dialect=dialect)  # type: ignore[union-attr]
        period = f"CAST('{p.isoformat()}' AS DATE)"
        rows = write([
            f"CREATE SCHEMA IF NOT EXISTS {schema}",
            f"CREATE TABLE IF NOT EXISTS {table} AS SELECT q.*, CAST(NULL AS DATE) AS period FROM ({body}) AS q WHERE 1 = 0",
            f"DELETE FROM {table} WHERE period = {period}",
            f"INSERT INTO {table} SELECT q.*, {period} AS period FROM ({body}) AS q",
        ])
        layer.catalog.record_rollup_partition(tenant, cube_name, name, fp, p.isoformat(), table, rows)
        built.append({"period": p.isoformat(), "rows": rows})
    return {"rollup": rollup, "tenant": tenant, "table": table, "fingerprint": fp, "built": built}


# ---------------------------------------------------------------- routing

@dataclass
class Available:
    cube: str
    rollup: Rollup
    table: str
    periods: dict[str, datetime] = field(default_factory=dict)  # month -> built_at


def available(model: Model, rows: list[dict], dialect: str, tenant: str) -> list[Available]:
    """Rollups whose built months match the current definitions (fingerprint) for this model."""
    from .compiler import Compiler, SecurityContext

    if not rows:
        return []
    by_key: dict[tuple[str, str, str], list[dict]] = {}
    for row in rows:
        by_key.setdefault((row["cube"], row["rollup"], row["fingerprint"]), []).append(row)
    system = Compiler(model, SecurityContext(tenant=tenant), dialect, dialect, system=True)
    out = []
    for cube in model.cubes.values():
        for r in cube.rollups.values():
            try:
                fp = fingerprint(model, cube, r, involved_cubes(system, cube, r))
            except ModelError:
                continue
            built = by_key.get((cube.name, r.name, fp))
            if built:
                out.append(Available(cube.name, r, built[0]["table_name"],
                                     {b["period"]: _ts(b["built_at"]) for b in built}))
    return sorted(out, key=lambda a: -GRAIN_ORDER[a.rollup.granularity])  # coarsest (smallest) first


def _ts(v: Any) -> datetime:
    d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def route(comp: "Compiler", q: Query, dims: list, measures: list, dim_filters: list, measure_filters: list,
          segments: list, time_ref: Any, candidates: list[Available]) -> Optional[tuple[exp.Expression, set[str], str]]:
    if segments or not measures or q.model_version is not None:
        return None
    roots = {m.cube.name for m in measures}
    if len(roots) != 1:
        return None
    root = roots.pop()
    for a in candidates:
        if a.cube != root:
            continue
        try:
            body = _route_one(comp, a, dims, measures, dim_filters, measure_filters, time_ref)
        except (ModelError, KeyError):
            body = None
        finally:
            comp.col_override, comp.measure_override = {}, {}
        if body is not None:
            return body, involved_cubes(comp, comp.model.cube(root), a.rollup), f"{a.cube}.{a.rollup.name}"
    return None


def _route_one(comp: "Compiler", a: Available, dims: list, measures: list, dim_filters: list, measure_filters: list,
               time_ref: Any) -> Optional[exp.Expression]:
    from .compiler import output_alias

    r, model = a.rollup, comp.model
    cube = model.cube(a.cube)
    have = set(rollup_components(model, cube, r, comp.src))
    for m in measures:
        if not set(components(model, m.cube, m.member, comp.src)) <= have:
            return None
    allowed = set(r.dimensions)
    for d in dims:
        if d is time_ref:
            if d.path != r.time_dimension or d.granularity not in SERVES[r.granularity]:
                return None
        elif d.path not in allowed or d.granularity:
            return None
    # Filters: on rollup dimensions, or day/week/month-aligned ranges on its time dimension.
    lo, hi = None, None
    for f in dim_filters:
        for path in filter_members(f):
            p = comp.resolve(path).path
            if p == r.time_dimension:
                bounds = _time_bounds(f, r.granularity)
                if bounds is None:
                    return None
                lo = max(filter(None, [lo, bounds[0]]), default=None)
                hi = min(filter(None, [hi, bounds[1]]), default=None)
            elif p not in allowed:
                return None
    for f in measure_filters:
        for path in filter_members(f):
            ref = comp.resolve(path)
            if not set(components(model, ref.cube, ref.member, comp.src)) <= have:
                return None
    # Row policies that a warehouse query would apply: same fixed point as the compiler's.
    needed = {cube.name} | {d.cube.name for d in dims} | {comp.resolve(p).cube.name for f in dim_filters for p in filter_members(f)}
    policies: dict[str, list] = {}
    while True:
        tree = comp.join_tree(cube.name, needed)
        in_tree = {cube.name} | {e.dst for e in tree}
        for c in in_tree - policies.keys():
            policies[c] = comp.policies_for(c)
        extra = {model.resolve(p)[0].name for fs in policies.values() for f in fs for p in filter_members(f)}
        if extra <= in_tree:
            break
        needed |= extra
    policy_filters = [f for fs in policies.values() for f in fs]
    if any(comp.resolve(p).path not in allowed for f in policy_filters for p in filter_members(f)):
        return None
    # Coverage and freshness of every month the query can touch.
    start = date.fromisoformat(r.start).replace(day=1)
    first = max(lo.replace(day=1), start) if lo else start
    now = datetime.now(timezone.utc)
    last = (hi - timedelta(days=1)) if hi else now.date()
    for m in months(first.isoformat(), last):
        built = a.periods.get(m.isoformat())
        if built is None:
            return None
        ended = datetime.combine(_next_month(m), datetime.min.time(), tzinfo=timezone.utc)
        if built < ended and now - built > timedelta(hours=r.max_age_hours):
            return None
    # Read the rollup: its columns stand in for members, its parts re-aggregate into measures.
    t = exp.column
    col = {p: t(output_alias(p), "rollup") for p in r.dimensions}
    tcube, tmember = model.resolve(r.time_dimension)
    bucket = t(output_alias(r.time_dimension, r.granularity), "rollup")
    comp.col_override = {(model.resolve(p)[0].name, model.resolve(p)[1].name, None): c for p, c in col.items()}
    comp.col_override[(tcube.name, tmember.name, None)] = bucket
    for g in SERVES[r.granularity]:
        unit = "ISOWEEK" if g == "week" and comp.dialect == "bigquery" else g.upper()
        comp.col_override[(tcube.name, tmember.name, g)] = bucket if g == r.granularity else exp.TimestampTrunc(this=bucket.copy(), unit=exp.Var(this=unit))
    comp.measure_override = {}
    for m in cube.measures.values():
        parts = None
        if m.agg in REAGG:
            parts = [Component(f"{cube.name}.{m.name}", REAGG[m.agg])]
        elif m.agg == "avg":
            parts = components(model, cube, m, comp.src)
        if not parts or not set(parts) <= have:
            continue
        cols = [exp.func(c.reagg, t(output_alias(c.path), "rollup")) for c in parts]
        if m.agg == "count":
            cols = [exp.cast(cols[0], "BIGINT")]
        comp.measure_override[(cube.name, m.name)] = cols[0] if len(cols) == 1 else exp.Div(
            this=exp.cast(cols[0], "DOUBLE"), expression=exp.Nullif(this=cols[1], expression=exp.Literal.number(0)))
    sel = exp.select(*[comp.member_sql(d.cube, d.member, d.granularity).as_(d.alias) for d in dims],
                     *[comp.measure_sql(m.cube, m.member).as_(m.alias) for m in measures])
    sel = sel.from_(exp.Table(this=exp.to_identifier(a.table.split(".")[1]), db=exp.to_identifier(a.table.split(".")[0]),
                              alias=exp.TableAlias(this=exp.to_identifier("rollup"))))
    where = [comp.filter_sql(f) for f in dim_filters + policy_filters]
    if where:
        sel = sel.where(exp.and_(*where))
    if dims:
        sel = sel.group_by(*[comp.member_sql(d.cube, d.member, d.granularity) for d in dims])
    if measure_filters:
        sel = sel.having(exp.and_(*[comp.filter_sql(f) for f in measure_filters]))
    return sel


def _time_bounds(f: Any, grain: str) -> Optional[tuple[Optional[date], Optional[date]]]:
    """[lo, hi) dates for a filter on the rollup's time dimension, or None if it isn't aligned to the grain."""
    if not isinstance(f, Condition):
        return None
    vals = [str(v) for v in f.values]
    if any(len(v) != 10 for v in vals):
        return None  # datetimes don't align with buckets
    ds = [date.fromisoformat(v) for v in vals]

    def aligned(d: date) -> bool:
        return grain == "day" or (grain == "week" and d.weekday() == 0) or (grain == "month" and d.day == 1)

    if f.op == "in_date_range":
        lo, hi = ds[0], ds[1] + timedelta(days=1)
    elif f.op in {"gte", "after_date"}:
        lo, hi = ds[0], None
    elif f.op in {"lt", "before_date"}:
        lo, hi = None, ds[0]
    else:
        return None
    if (lo and not aligned(lo)) or (hi and not aligned(hi)):
        return None
    return lo, hi
