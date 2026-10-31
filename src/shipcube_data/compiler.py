"""Compile typed queries (L0 + L1) to SQL through sqlglot expressions.

Pipeline:
  resolve members (aliases, grants)  ->  add row policies  ->  group measures by cube
  -> per group: join tree from the measure's cube, fan-out check, sources with
     schema routing and partition pruning, WHERE / GROUP BY / HAVING
  -> one SELECT, or one CTE per group stitched on the dimensions
  -> L1 calcs as window functions over that result
  -> render in the target dialect.
"""

from __future__ import annotations

import difflib
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Optional

from pydantic import BaseModel, Field
from sqlglot import exp

from .model import ADDITIVE_AGGS, FANOUT_SAFE_AGGS, IDENT, Cube, Dimension, Measure, Member, Model, ModelError, humanize
from .partitions import Bounds, prune, timestamp_type, to_date
from .query import And, Calc, Condition, Filter, Not, Or, Query, filter_members, parse_filter
from .sqlutil import parse_expr, parse_query, raw_columns, ref_name, refs, row_reference

DIALECTS = ("duckdb", "postgres", "bigquery", "clickhouse")
DEFAULT_LIMIT = 10_000
_IDENT_RE = re.compile(IDENT)
_INVERSE = {"many_to_one": "one_to_many", "one_to_many": "many_to_one", "one_to_one": "one_to_one"}


class SecurityContext(BaseModel):
    """Who is asking. Set by ShipCube from the verified identity, never by the query body."""

    tenant: str
    user: Optional[str] = None
    roles: list[str] = Field(default_factory=list)
    attrs: dict[str, Any] = Field(default_factory=dict)

    def get(self, key: str) -> Any:
        if key in {"tenant", "user", "roles"}:
            return getattr(self, key)
        if key not in self.attrs:
            raise ModelError(f"security context has no {key!r}; refusing to run without it", "forbidden")
        return self.attrs[key]


@dataclass
class Column:
    name: str
    member: str
    kind: str  # dimension | measure | calc
    type: str
    title: str
    granularity: Optional[str] = None


@dataclass
class Compiled:
    sql: str
    dialect: str
    columns: list[Column]
    cubes: list[str]
    fingerprint: str  # of the parts of the model this query depends on
    partitions: dict[str, dict[str, Any]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    expression: Optional[exp.Expression] = None
    rollup: Optional[str] = None  # cube.rollup when the query was answered from a pre-aggregation


@dataclass
class _Ref:
    cube: Cube
    member: Member
    granularity: Optional[str] = None
    requested: Optional[str] = None  # the name the query used, which may be a pre-rename alias

    @property
    def path(self) -> str:
        return f"{self.cube.name}.{self.member.name}"

    @property
    def alias(self) -> str:
        # Output columns keep the name the caller asked for, so a rename does not change result shape.
        return output_alias(self.requested or self.path, self.granularity)


@dataclass
class _Edge:
    src: str
    dst: str
    relationship: str
    on: str


def output_alias(path: str, granularity: Optional[str] = None) -> str:
    return path.replace(".", "__") + (f"__{granularity}" if granularity else "")


class Compiler:
    def __init__(self, model: Model, ctx: SecurityContext, dialect: str = "duckdb", source_dialect: str = "duckdb",
                 system: bool = False, rollups: Optional[list] = None):
        """`system`: every member visible and no row policies. Only rollup builds use it, never a caller's query.
        `rollups`: built pre-aggregations this query may be answered from (rollups.available)."""
        if dialect not in DIALECTS:
            raise ModelError(f"unsupported dialect {dialect!r}; pick one of {', '.join(DIALECTS)}")
        if not _IDENT_RE.match(ctx.tenant):
            raise ModelError(f"tenant {ctx.tenant!r} is not a valid identifier", "forbidden")
        self.model, self.ctx, self.dialect, self.src = model, ctx, dialect, source_dialect
        self.warnings: list[str] = []
        self.partitions: dict[str, dict[str, Any]] = {}
        self.system, self.rollups = system, rollups or []
        self.col_override: dict[tuple, exp.Expression] = {}  # (cube, member, granularity) -> expression (rollup columns)
        self.measure_override: dict[tuple, exp.Expression] = {}  # (cube, measure) -> re-aggregation
        self.rollup_used: Optional[str] = None

    # ------------------------------------------------------------ resolution & access

    def visible(self, cube: Cube, member: Member, _seen: Optional[set[str]] = None) -> bool:
        """A member is visible if the caller holds one of its grants (or the cube's), and
        so is everything it references: {revenue} + {freight} must not leak freight."""
        if self.system:
            return True
        grants = member.grants or cube.grants
        if grants and not set(grants) & set(self.ctx.roles):
            return False
        seen = (_seen or set()) | {member.name}
        texts = [member.sql, getattr(member, "filter", None)]
        for ref in (r for t in texts for r in refs(t)):
            name = ref.rpartition(".")[2]
            target = cube.member(name)
            if target and name not in seen and not self.visible(cube, target, seen):
                return False
        return True

    def hidden_columns(self, cube: Cube) -> set[str]:
        """Physical columns that members hidden from this caller read."""
        out: set[str] = set()
        for m in cube.members():
            if not self.visible(cube, m):
                texts = [m.sql or (m.name if m.kind == "dimension" else None), getattr(m, "filter", None)]
                out |= {c for t in texts if t for c in raw_columns(t, self.src)}
        return out

    def segment_visible(self, cube: Cube, sql: str) -> bool:
        """Segments are filters, so they leak through row counts: a segment is usable only if every
        member it references is visible and it reads no column a hidden member reads (shipcube-data #8).
        Whole-row and other-relation references are refused here too, for segments stored before add_segment checked."""
        if row_reference(sql, self.src, set(self.model.cubes)):
            return False
        for ref in refs(sql):
            target = cube.member(ref.rpartition(".")[2])
            if target is None or not self.visible(cube, target):
                return False
        return not (raw_columns(sql, self.src) & self.hidden_columns(cube))

    def resolve(self, path: str, granularity: Optional[str] = None) -> _Ref:
        try:
            cube, member = self.model.resolve(path)
        except ModelError:
            raise ModelError(f"unknown or not permitted: {path}", "unknown_member", self._closest(path)) from None
        if not self.visible(cube, member):
            raise ModelError(f"unknown or not permitted: {path}", "unknown_member", self._closest(path))
        if path != f"{cube.name}.{member.name}":
            self.warnings.append(f"{path} was renamed to {cube.name}.{member.name}; update the query")
        if member.deprecated:
            hint = f"; use {member.replaced_by}" if member.replaced_by else ""
            self.warnings.append(f"{cube.name}.{member.name} is deprecated ({member.deprecated}){hint}")
        return _Ref(cube, member, granularity, requested=path)

    def _closest(self, path: str) -> str | None:
        visible = [f"{c.name}.{m.name}" for c in self.model.cubes.values() for m in c.members() if self.visible(c, m)]
        near = difflib.get_close_matches(path, visible, n=3, cutoff=0.6)
        return f"did you mean {', '.join(near)}?" if near else None

    def policies_for(self, cube: str) -> list[Filter]:
        if self.system:
            return []
        out = []
        roles = set(self.ctx.roles)
        for p in self.model.policies.values():
            if p.cube != cube or (p.roles and not roles & set(p.roles)) or roles & set(p.exempt_roles):
                continue
            out.append(parse_filter(self._bind_ctx(p.filter)))
        return out

    def _bind_ctx(self, node: Any) -> Any:
        if isinstance(node, dict):
            if "values" in node:
                vals: list[Any] = []
                for v in node["values"]:
                    if isinstance(v, str) and v.startswith("$ctx."):
                        got = self.ctx.get(v[5:])
                        vals.extend(got if isinstance(got, list) else [got])
                    else:
                        vals.append(v)
                if not vals:
                    vals = ["\x00no-match"]  # an empty ctx list matches nothing, never everything
                return {**node, "values": vals}
            return {k: self._bind_ctx(v) for k, v in node.items()}
        if isinstance(node, list):
            return [self._bind_ctx(v) for v in node]
        return node

    # ------------------------------------------------------------ member SQL

    def member_sql(self, cube: Cube, member: Member, granularity: Optional[str] = None) -> exp.Expression:
        if isinstance(member, Measure):
            return self.measure_sql(cube, member)
        if (cube.name, member.name, granularity) in self.col_override:
            return self.col_override[(cube.name, member.name, granularity)].copy()
        e = self._expand(cube, member.sql or member.name, stack=[member.name])
        if granularity:
            unit = "ISOWEEK" if granularity == "week" and self.dialect == "bigquery" else granularity.upper()  # Monday everywhere
            e = exp.TimestampTrunc(this=e, unit=exp.Var(this=unit))
        return e

    def measure_sql(self, cube: Cube, m: Measure, stack: Optional[list[str]] = None) -> exp.Expression:
        if (cube.name, m.name) in self.measure_override:
            return self.measure_override[(cube.name, m.name)].copy()
        stack = (stack or []) + [m.name]
        if m.agg == "number":
            return self._expand(cube, m.sql or "", stack=stack, measures=True)
        arg: exp.Expression = self._expand(cube, m.sql, stack=stack) if m.sql else exp.Star()
        if m.filter:
            cond = self._expand(cube, m.filter, stack=stack)
            arg = exp.Case(ifs=[exp.If(this=cond, true=exp.Literal.number(1) if not m.sql else arg)])
        if m.agg == "count":
            return exp.Count(this=arg)
        if m.agg == "count_distinct":
            return exp.Count(this=exp.Distinct(expressions=[arg]))
        fn = {"sum": exp.Sum, "avg": exp.Avg, "min": exp.Min, "max": exp.Max}[m.agg]
        return fn(this=arg)

    def _expand(self, cube: Cube, sql: str, stack: list[str], measures: bool = False) -> exp.Expression:
        tree = parse_expr(sql, self.src)

        def fix(node: exp.Expression) -> exp.Expression:
            if not isinstance(node, exp.Column):
                return node
            ref = ref_name(node)
            if ref is None:
                if not node.table:
                    return exp.column(node.name, cube.name, quoted=node.this.quoted)
                return node
            name = ref.rpartition(".")[2]
            if name in stack:
                raise ModelError(f"reference cycle in {cube.name}: {' -> '.join(stack + [name])}")
            target = cube.member(name)
            if target is None:
                raise ModelError(f"{cube.name}: {{{ref}}} does not exist")
            if isinstance(target, Measure):
                if not measures:
                    raise ModelError(f"{cube.name}: {{{ref}}} is a measure")
                return exp.Paren(this=self.measure_sql(cube, target, stack))
            return exp.Paren(this=self._expand(cube, target.sql or target.name, stack + [name]))

        return tree.transform(fix)

    # ------------------------------------------------------------ joins

    def _edges(self, cube: str) -> list[_Edge]:
        out = []
        for j in self.model.cube(cube).joins.values():
            out.append(_Edge(cube, j.to, j.relationship, j.on))
        for other in self.model.cubes.values():
            if cube in other.joins and other.name != cube:
                j = other.joins[cube]
                out.append(_Edge(cube, other.name, _INVERSE[j.relationship], j.on))
        return sorted(out, key=lambda e: e.dst)

    def join_tree(self, root: str, needed: set[str]) -> list[_Edge]:
        """Shortest join paths from root to every needed cube, as an ordered edge list."""
        prev: dict[str, Optional[_Edge]] = {root: None}
        queue = deque([root])
        while queue:
            cur = queue.popleft()
            for e in self._edges(cur):
                if e.dst not in prev:
                    prev[e.dst] = e
                    queue.append(e.dst)
        edges: list[_Edge] = []
        for target in sorted(needed - {root}):
            if target not in prev:
                raise ModelError(f"no join path from {root} to {target}", "no_join_path", f"propose an add_join between {target} and a cube reachable from {root}")
            path = []
            node = target
            while prev[node] is not None:
                path.append(prev[node])
                node = prev[node].src  # type: ignore[union-attr]
            for e in reversed(path):
                if e not in edges:
                    edges.append(e)
        return edges

    # ------------------------------------------------------------ sources

    def schema_for(self, cube: Cube) -> tuple[Optional[str], Optional[str]]:
        if cube.table and "." in cube.table:
            return None, None
        b = self.model.bindings
        tenant = self.ctx.tenant
        binding = (
            b.get(f"{cube.name}@{tenant}") or b.get(f"{cube.name}@*") or b.get(f"*@{tenant}") or b.get("*@*")
        )
        if binding is None:
            raise ModelError(f"no schema binding for cube {cube.name} and tenant {tenant}", "forbidden")
        binding.check_tenancy()  # fail closed on bindings from before `shared` existed
        return binding.schema_.replace("{tenant}", tenant), binding.tenant_column

    def source(self, cube: Cube, bounds: Bounds) -> tuple[exp.Expression, list[exp.Expression]]:
        """FROM item for a cube, plus predicates it needs in WHERE."""
        schema, tenant_column = self.schema_for(cube)
        preds: list[exp.Expression] = []
        if tenant_column:
            preds.append(exp.column(tenant_column, cube.name).eq(exp.Literal.string(self.ctx.tenant)))
        alias = exp.TableAlias(this=exp.to_identifier(cube.name))

        def table(name: str) -> exp.Table:
            db, _, tbl = name.rpartition(".")
            return exp.Table(this=exp.to_identifier(tbl), db=exp.to_identifier(db or schema) if (db or schema) else None)

        spec = cube.partition
        if cube.sql:
            q = parse_query(cube.sql, self.src)
            ctes = {c.alias for c in q.find_all(exp.CTE)}
            for t in q.find_all(exp.Table):
                if not t.args.get("db") and t.name not in ctes and schema:
                    t.set("db", exp.to_identifier(schema))
            src: exp.Expression = exp.Subquery(this=q, alias=alias)
        elif spec and spec.strategy == "table":
            pruned = prune(spec, exp.column(spec.column, cube.name), bounds, cube.name, self.dialect)
            self.warnings.extend(pruned.notes)
            self.partitions[cube.name] = {"strategy": "table", "total": pruned.total, "scanned": pruned.scanned, "tables": pruned.tables}
            names = pruned.tables or []
            if not names:
                first = table_from_pattern(spec)
                q = exp.select("*").from_(table(first)).where(exp.false())
            else:
                q = exp.select("*").from_(table(names[0]))
                for n in names[1:]:
                    q = exp.union(q, exp.select("*").from_(table(n)), distinct=False)
            src = exp.Subquery(this=q, alias=alias)
        else:
            t = table(cube.table)  # type: ignore[arg-type]
            t.set("alias", alias)
            src = t
        if spec and spec.strategy == "column":
            pruned = prune(spec, exp.column(spec.column, cube.name), bounds, cube.name, self.dialect)
            self.warnings.extend(pruned.notes)
            self.partitions[cube.name] = {"strategy": "column", "predicate": pruned.predicate.sql(self.dialect) if pruned.predicate else None}
            if pruned.predicate is not None:
                preds.append(pruned.predicate)
        return src, preds

    # ------------------------------------------------------------ filters

    def literal(self, v: Any, ref: _Ref) -> exp.Expression:
        if isinstance(v, bool):
            return exp.Boolean(this=v)
        if isinstance(v, (int, float)):
            return exp.Literal.number(v)
        lit = exp.Literal.string(str(v))
        if getattr(ref.member, "type", None) == "time":
            return exp.cast(lit, timestamp_type(self.dialect))
        return lit

    def condition(self, c: Condition) -> exp.Expression:
        ref = self.resolve(c.member)
        e = self.member_sql(ref.cube, ref.member)
        vals = [self.literal(v, ref) for v in c.values]
        op = c.op
        if op in {"equals", "not_equals"}:
            cond = e.eq(vals[0]) if len(vals) == 1 else e.isin(*vals)
            return exp.not_(cond) if op == "not_equals" else cond
        if op in {"contains", "not_contains", "starts_with", "ends_with"}:
            ors = []
            for v in c.values:
                s = str(v).lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                pat = {"contains": f"%{s}%", "not_contains": f"%{s}%", "starts_with": f"{s}%", "ends_with": f"%{s}"}[op]
                ors.append(exp.Like(this=exp.Lower(this=e.copy()), expression=exp.Literal.string(pat)))
            cond = exp.or_(*ors)
            return exp.not_(cond) if op == "not_contains" else cond
        if op == "set":
            return exp.not_(exp.Is(this=e, expression=exp.Null()))
        if op == "not_set":
            return exp.Is(this=e, expression=exp.Null())
        if op == "in_date_range":
            return exp.and_(e >= vals[0], e.copy() < self._end(c.values[1], ref))
        cmp = {"gt": exp.GT, "gte": exp.GTE, "lt": exp.LT, "lte": exp.LTE, "before_date": exp.LT, "after_date": exp.GTE}[op]
        return cmp(this=e, expression=vals[0])

    def _end(self, value: Any, ref: _Ref) -> exp.Expression:
        """Inclusive end of a date range -> exclusive bound."""
        s = str(value)
        if len(s) == 10:  # a date: include the whole day
            s = (to_date(s) + timedelta(days=1)).isoformat()
            return self.literal(s, ref)
        return self.literal(s, ref)  # datetimes are used as given (exclusive)

    def filter_sql(self, f: Filter) -> exp.Expression:
        if isinstance(f, Condition):
            return self.condition(f)
        if isinstance(f, Not):
            return exp.not_(self.filter_sql(f.not_))
        children = [self.filter_sql(x) for x in (f.and_ if isinstance(f, And) else f.or_)]
        return exp.and_(*children) if isinstance(f, And) else exp.or_(*children)

    def _bounds(self, cube: Cube, conds: list[Filter], time: Optional[tuple[str, tuple[str, str]]]) -> Bounds:
        b = Bounds()
        spec = cube.partition
        if not spec:
            return b

        def on_partition(path: str) -> bool:
            try:
                c, m = self.model.resolve(path)
            except ModelError:
                return False
            if c.name != cube.name or not isinstance(m, Dimension):
                return False
            e = parse_expr(m.sql or m.name, self.src)
            while isinstance(e, (exp.Cast, exp.TimestampTrunc, exp.DateTrunc, exp.Date, exp.Paren)):
                e = e.this
            return isinstance(e, exp.Column) and e.name == spec.column and not e.table

        if time and on_partition(time[0]):
            b.narrow(lo=to_date(time[1][0]), hi=to_date(time[1][1]))
        for f in conds:
            if not isinstance(f, Condition) or not on_partition(f.member):
                continue
            v = [str(x) for x in f.values]
            if spec.kind == "list":
                if f.op == "equals":
                    b.narrow(values=set(v))
                continue
            if f.op == "in_date_range":
                b.narrow(lo=to_date(v[0]), hi=to_date(v[1]))
            elif f.op in {"gt", "gte", "after_date"}:
                b.narrow(lo=to_date(v[0]))
            elif f.op in {"lt", "lte", "before_date"}:
                b.narrow(hi=to_date(v[0]))
            elif f.op == "equals" and len(v) == 1:
                b.narrow(lo=to_date(v[0]), hi=to_date(v[0]))
        return b

    # ------------------------------------------------------------ L0

    def compile(self, q: Query, as_stage: bool = False) -> Compiled:
        """`as_stage`: an L2 stage, so no default order or row cap (the staged query's own limit applies)."""
        dims = [self.resolve(d) for d in q.dimensions]
        time_ref: Optional[_Ref] = None
        if q.time:
            time_ref = self.resolve(q.time.dimension, q.time.granularity)
            if getattr(time_ref.member, "type", None) != "time":
                raise ModelError(f"{time_ref.path} is not a time dimension")
            if q.time.granularity:
                dims.append(time_ref)
        measures = [self.resolve(m) for m in q.measures]
        for r in dims:
            if isinstance(r.member, Measure):
                raise ModelError(f"{r.path} is a measure; put it in measures")
        for r in measures:
            if not isinstance(r.member, Measure):
                raise ModelError(f"{r.path} is a dimension; put it in dimensions")

        dim_filters: list[Filter] = []
        measure_filters: list[Filter] = []
        for f in q.filters:
            kinds = {self.resolve(p).member.kind for p in filter_members(f)}
            if kinds == {"measure"}:
                measure_filters.append(f)
            elif kinds == {"dimension"}:
                dim_filters.append(f)
            else:
                raise ModelError("a filter group cannot mix measures and dimensions")
        if q.time and q.time.range:
            dim_filters.append(Condition(member=q.time.dimension, op="in_date_range", values=list(q.time.range)))
        segments = []
        for s in q.segments:
            cube_name, _, name = s.partition(".")
            cube = self.model.cube(cube_name)
            if name not in cube.segments or not self.segment_visible(cube, cube.segments[name].sql):
                raise ModelError(f"unknown or not permitted: segment {s}", "unknown_member")
            segments.append((cube, cube.segments[name]))

        from .rollups import route

        routed = route(self, q, dims, measures, dim_filters, measure_filters, segments, time_ref, self.rollups) if self.rollups else None
        if routed is not None:
            body, used, self.rollup_used = routed
            self.partitions = {"rollup": {"name": self.rollup_used}}
        else:
            groups: dict[str, list[_Ref]] = {}
            for r in measures:
                groups.setdefault(r.cube.name, []).append(r)
            if not groups:
                groups[(dims[0].cube.name if dims else time_ref.cube.name)] = []  # type: ignore[union-attr]

            used = set()
            selects: list[tuple[str, exp.Select]] = []
            for root, ms in groups.items():
                sel, cubes = self._group(root, ms, dims, dim_filters, segments, measure_filters if len(groups) == 1 else [], q, time_ref)
                used |= cubes
                selects.append((root, sel))

            if len(selects) == 1:
                body = selects[0][1]
            else:
                body = self._stitch(selects, dims, measures, measure_filters)

        columns = [
            Column(r.alias, r.path, "dimension", getattr(r.member, "type", "string"), r.member.title or humanize(r.member.name), r.granularity)
            for r in dims
        ] + [Column(r.alias, r.path, "measure", "number", r.member.title or humanize(r.member.name)) for r in measures]

        if q.calcs:
            body, calc_cols = self._calcs(body, q, dims, measures, time_ref)
            columns += calc_cols

        order = q.order or ([] if as_stage else self._default_order(dims, measures, time_ref))
        names = {c.member: c.name for c in columns if c.kind != "calc" and not c.granularity}
        for c in columns:  # ordering by a time member's path means its truncated column
            if c.granularity:
                names.setdefault(c.member, c.name)
        names |= {c.name: c.name for c in columns}
        for o in order:
            key = o.member if o.member in names else output_alias(o.member)
            if key not in names:
                try:
                    r = self.resolve(o.member)
                    key = r.alias
                except ModelError:
                    pass
                if key not in names:
                    raise ModelError(f"cannot order by {o.member}: it is not in the query")
            body = body.order_by(exp.Ordered(this=exp.column(names[key]), desc=o.dir == "desc"), copy=False)
        if q.limit or not as_stage:
            body = body.limit(q.limit or DEFAULT_LIMIT, copy=False)
        if q.offset:
            body = body.offset(q.offset, copy=False)
        if not q.limit and not as_stage:
            self.warnings.append(f"no limit given; capped at {DEFAULT_LIMIT} rows")

        return Compiled(
            sql=body.sql(dialect=self.dialect),
            dialect=self.dialect,
            columns=columns,
            cubes=sorted(used),
            fingerprint=self.model.cube_fingerprint(used),
            partitions=self.partitions,
            warnings=list(dict.fromkeys(self.warnings)),
            expression=body,
            rollup=self.rollup_used,
        )

    def _default_order(self, dims: list[_Ref], measures: list[_Ref], time_ref: Optional[_Ref]):
        from .query import Order

        if time_ref and time_ref.granularity:
            return [Order(member=time_ref.alias, dir="asc")]
        if measures:
            return [Order(member=measures[0].alias, dir="desc")]
        return [Order(member=dims[0].alias, dir="asc")] if dims else []

    def _group(self, root: str, ms: list[_Ref], dims: list[_Ref], dim_filters: list[Filter],
               segments: list, measure_filters: list[Filter], q: Query, time_ref: Optional[_Ref]):
        needed = {root} | {r.cube.name for r in dims} | {c.name for c, _ in segments}
        for f in dim_filters:
            needed |= {self.resolve(p).cube.name for p in filter_members(f)}
        # Row policies may pull in more cubes; iterate to a fixed point.
        policies: dict[str, list[Filter]] = {}
        while True:
            tree = self.join_tree(root, needed)
            in_tree = {root} | {e.dst for e in tree}
            for c in in_tree - policies.keys():
                policies[c] = self.policies_for(c)
            extra = {self.model.resolve(p)[0].name for fs in policies.values() for f in fs for p in filter_members(f)}
            if extra <= in_tree:
                break
            needed |= extra
        fanout = [e for e in tree if e.relationship == "one_to_many"]
        unsafe = [r for r in ms if not self._fanout_safe(r)]
        if fanout and unsafe:
            e = fanout[0]
            raise ModelError(
                f"{unsafe[0].path} would be inflated: joining {e.src} -> {e.dst} is one_to_many. "
                f"Query it from {e.dst}'s side, or use a count_distinct/min/max measure",
                "fan_out",
            )

        all_filters = dim_filters + [f for fs in policies.values() for f in fs]
        time = (q.time.dimension, q.time.range) if q.time and q.time.range else None
        sources = {}
        where: list[exp.Expression] = []
        for c in [root] + [e.dst for e in tree]:
            cube = self.model.cube(c)
            src, preds = self.source(cube, self._bounds(cube, all_filters, time))
            sources[c] = src
            where += preds
        sel = exp.select(*[self.member_sql(r.cube, r.member, r.granularity).as_(r.alias) for r in dims])
        sel = sel.select(*[self.measure_sql(r.cube, r.member).as_(r.alias) for r in ms]) if ms else sel
        sel = sel.from_(sources[root])
        for e in tree:
            on = parse_expr(e.on, self.src)
            sel = sel.join(sources[e.dst], on=on, join_type="LEFT")
        where += [self.filter_sql(f) for f in all_filters]
        where += [self._expand(c, s.sql, stack=[]) for c, s in segments]
        # A pruning predicate is often identical to the user's own time filter; keep one copy.
        where = list({w.sql(dialect=self.src): w for w in where}.values())
        if where:
            sel = sel.where(exp.and_(*where))
        if dims:
            sel = sel.group_by(*[self.member_sql(r.cube, r.member, r.granularity) for r in dims])
        if not ms and not dims:
            sel = sel.select(exp.Literal.number(1).as_("one"))
        if measure_filters:
            sel = sel.having(exp.and_(*[self.filter_sql(f) for f in measure_filters]))
        return sel, {root} | {e.dst for e in tree}

    def _shift(self, col: exp.Expression, n: int, granularity: str) -> exp.Expression:
        return _shift_expr(col, n, granularity, self.dialect)

    def _fanout_safe(self, r: _Ref) -> bool:
        return getattr(r.member, "agg", None) in FANOUT_SAFE_AGGS

    def _stitch(self, selects, dims: list[_Ref], measures: list[_Ref], measure_filters: list[Filter]) -> exp.Select:
        """Join per-cube aggregates on the dimensions (avoids fan-out across facts).

        A `keys` CTE is the UNION of every group's dimension values; each group is LEFT JOINed to
        it null-safely. (FULL JOIN on IS NOT DISTINCT FROM is not supported by Postgres.)
        """
        names = [f"g_{root}" for root, _ in selects]
        by_root = {r.path: f"g_{r.cube.name}" for r in measures}
        if dims:
            keys: exp.Expression = exp.select(*[exp.column(d.alias) for d in dims]).from_(names[0])
            for n in names[1:]:
                keys = exp.union(keys, exp.select(*[exp.column(d.alias) for d in dims]).from_(n), distinct=True)
            out = exp.select(*[exp.column(d.alias, "keys").as_(d.alias) for d in dims])
            out = out.select(*[exp.column(m.alias, by_root[m.path]).as_(m.alias) for m in measures]).from_("keys")
            for n in names:
                on = exp.and_(*[exp.NullSafeEQ(this=exp.column(d.alias, n), expression=exp.column(d.alias, "keys")) for d in dims])
                out = out.join(n, on=on, join_type="LEFT")
        else:
            out = exp.select(*[exp.column(m.alias, by_root[m.path]).as_(m.alias) for m in measures]).from_(names[0])
            for n in names[1:]:
                out = out.join(n, join_type="CROSS")
        for name, (_, sel) in zip(names, selects):
            out = out.with_(name, as_=sel)
        if dims:
            out = out.with_("keys", as_=keys)
        if measure_filters:
            # Measure filters see the stitched columns.
            alias_of = {m.path: exp.column(m.alias, by_root[m.path]) for m in measures}
            cond = exp.and_(*[self._alias_filter(f, alias_of) for f in measure_filters])
            return exp.select("*").from_(out.subquery("stitched")).where(cond)
        return out

    def _alias_filter(self, f: Filter, alias_of: dict[str, exp.Expression]) -> exp.Expression:
        if isinstance(f, Condition):
            r = self.resolve(f.member)
            if r.path not in alias_of:
                raise ModelError(f"measure filter on {f.member} needs it in measures")
            col = exp.column(r.alias)
            vals = [self.literal(v, r) for v in f.values]
            cmp = {"gt": exp.GT, "gte": exp.GTE, "lt": exp.LT, "lte": exp.LTE}.get(f.op)
            if cmp:
                return cmp(this=col, expression=vals[0])
            if f.op == "equals":
                return col.eq(vals[0]) if len(vals) == 1 else col.isin(*vals)
            if f.op == "not_equals":
                return exp.not_(col.isin(*vals))
            if f.op in {"set", "not_set"}:
                isnull = exp.Is(this=col, expression=exp.Null())
                return isnull if f.op == "not_set" else exp.not_(isnull)
            raise ModelError(f"{f.op} does not apply to measures")
        if isinstance(f, Not):
            return exp.not_(self._alias_filter(f.not_, alias_of))
        kids = [self._alias_filter(x, alias_of) for x in (f.and_ if isinstance(f, And) else f.or_)]
        return exp.and_(*kids) if isinstance(f, And) else exp.or_(*kids)

    # ------------------------------------------------------------ L1

    def _calcs(self, body: exp.Select, q: Query, dims: list[_Ref], measures: list[_Ref], time_ref: Optional[_Ref]):
        by_path = {m.path: m for m in measures} | {m.alias: m for m in measures}
        dim_alias = {d.path: d.alias for d in dims} | {d.alias: d.alias for d in dims}
        out = exp.select(exp.Column(this=exp.Star(), table=exp.to_identifier("base"))).from_("base")
        cols = []
        for i, c in enumerate(q.calcs):
            if not _IDENT_RE.match(c.name):
                raise ModelError(f"calc name {c.name!r} must be snake_case")
            m = by_path.get(c.measure) or by_path.get(self.resolve(c.measure).path)
            if m is None:
                raise ModelError(f"calc {c.name}: {c.measure} must be one of the query's measures")
            part = []
            for d in c.by:
                if d not in dim_alias:
                    d = self.resolve(d).path
                if d not in dim_alias:
                    raise ModelError(f"calc {c.name}: {d} must be one of the query's dimensions")
                part.append(exp.column(dim_alias[d], "base"))
            val = exp.column(m.alias, "base")
            needs_time = c.op in {"lag", "running", "compare", "moving_avg"}
            if needs_time and not (time_ref and time_ref.granularity):
                raise ModelError(f"calc {c.name}: {c.op} needs time with a granularity")
            t = exp.column(time_ref.alias, "base") if time_ref and time_ref.granularity else None

            def win(fn: exp.Expression, order: Optional[exp.Expression] = None, desc: bool = False, frame: bool = False,
                    preceding: Optional[int] = None) -> exp.Window:
                spec = None
                if frame or preceding is not None:
                    start = "UNBOUNDED" if preceding is None else exp.Literal.number(preceding)
                    spec = exp.WindowSpec(kind="ROWS", start=start, start_side="PRECEDING", end="CURRENT ROW")
                return exp.Window(
                    this=fn,
                    partition_by=[p.copy() for p in part] or None,
                    order=exp.Order(expressions=[exp.Ordered(this=order, desc=desc)]) if order is not None else None,
                    spec=spec,
                )

            if c.op == "share":
                e = val / exp.Nullif(this=win(exp.Sum(this=val.copy())), expression=exp.Literal.number(0))
            elif c.op == "rank":
                e = win(exp.Anonymous(this="RANK"), order=val.copy(), desc=True)
            elif c.op == "lag":
                e = win(exp.Anonymous(this="LAG", expressions=[val.copy(), exp.Literal.number(c.offset)]), order=t)
            elif c.op == "running":
                if m.member.agg not in ADDITIVE_AGGS:  # type: ignore[union-attr]
                    raise ModelError(f"calc {c.name}: running totals need an additive (sum/count) measure")
                e = win(exp.Sum(this=val.copy()), order=t, frame=True)
            elif c.op == "moving_avg":
                # The mean of the measure over the last `window` periods present, NULL until there are that many
                # (like pandas rolling(window)). Gaps in the series aren't filled: a missing period isn't counted.
                n = c.window - 1
                avg = win(exp.Avg(this=val.copy()), order=t, preceding=n)
                count = win(exp.Count(this=val.copy()), order=t, preceding=n)
                e = exp.Case(ifs=[exp.If(this=exp.GTE(this=count, expression=exp.Literal.number(c.window)), true=avg)])
            else:  # compare: period-over-period through a self-join, so gaps in the series stay correct
                p = f"p{i}"
                shifted = self._shift(exp.column(time_ref.alias, p), c.offset, time_ref.granularity)  # type: ignore[arg-type,union-attr]
                conds = [exp.EQ(this=shifted, expression=t.copy())]  # type: ignore[union-attr]
                for d in dims:
                    if d is not time_ref:
                        conds.append(exp.NullSafeEQ(this=exp.column(d.alias, p), expression=exp.column(d.alias, "base")))
                out = out.join(exp.Table(this=exp.to_identifier("base"), alias=exp.TableAlias(this=exp.to_identifier(p))), on=exp.and_(*conds), join_type="LEFT")
                prior = exp.column(m.alias, p)
                if c.mode == "prior":
                    e = prior
                elif c.mode == "diff":
                    e = val - prior
                else:
                    e = (val - prior) / exp.Nullif(this=prior.copy(), expression=exp.Literal.number(0))
            out = out.select(e.as_(c.name))
            cols.append(Column(c.name, c.measure, "calc", "number", humanize(c.name)))
        return out.with_("base", as_=body), cols


def _shift_expr(col: exp.Expression, n: int, granularity: str, dialect: str) -> exp.Expression:
    # Weeks and quarters move as days and months: every dialect's interval arithmetic has those.
    n, granularity = {"week": (n * 7, "day"), "quarter": (n * 3, "month")}.get(granularity, (n, granularity))
    unit = exp.Var(this=granularity.upper())
    if dialect == "bigquery":  # TIMESTAMP_ADD has no MONTH/YEAR; go through DATETIME
        return exp.cast(exp.DatetimeAdd(this=exp.cast(col, "DATETIME"), expression=exp.Literal.number(n), unit=unit), "TIMESTAMPTZ")
    return exp.DateAdd(this=col, expression=exp.Literal.number(n), unit=unit)


def table_from_pattern(spec) -> str:
    from .partitions import range_partitions, table_name

    if spec.kind == "list":
        return table_name(spec.table_pattern, value=spec.values[0])
    return table_name(spec.table_pattern, start=range_partitions(spec)[0][0])
