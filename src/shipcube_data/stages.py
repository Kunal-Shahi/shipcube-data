"""L2: named stages composed into one governed query.

Every stage that reads data is an L0/L1 `query`, compiled by the regular compiler, so tenant
routing, grants, row policies, fan-out checks and partition pruning all still apply. The other
stage kinds only see the columns of earlier stages: they join, filter, derive, re-aggregate,
pivot and combine them. Expressions are a typed JSON tree, never SQL strings, so nothing a caller
writes reaches the warehouse as text. See QUERY.md, "L2: stages".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlglot import exp

from .compiler import Column, Compiled, Compiler, output_alias
from .model import ModelError, humanize
from .query import Query

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
RESERVED = {"base", "keys", "raw", "q", "g", "p"}
TIME_UNITS = {"day", "week", "month", "quarter", "year"}
MAX_STAGES = 30
MAX_PIVOT_VALUES = 100


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Join(_Base):
    stage: str
    as_: Optional[str] = Field(default=None, alias="as")
    type: Literal["inner", "left", "right", "full", "cross"] = "inner"
    on: list[tuple[Any, Any]] = Field(default_factory=list, description="Pairs of column references compared with `=`.")


class Agg(_Base):
    fn: Literal["sum", "avg", "min", "max", "count", "count_distinct", "median", "stddev", "variance"]
    of: Any = None  # an expression; count without `of` counts rows


class Group(_Base):
    by: list[str] = Field(default_factory=list, description="Columns of this stage's select (or its single source) to group by.")
    agg: dict[str, Agg] = Field(default_factory=dict)


class Pivot(_Base):
    index: list[str] = Field(default_factory=list, description="Columns that stay as rows.")
    column: str = Field(description="Column whose values become output columns.")
    values: list[Any] = Field(min_length=1, max_length=MAX_PIVOT_VALUES, description="The values to spread, explicitly.")
    agg: Agg
    prefix: Optional[str] = None


class OrderBy(_Base):
    column: str
    dir: Literal["asc", "desc"] = "asc"


class Stage(_Base):
    """One of: `query` (reads data), `from` (+ join/where/select/group/having/pivot), or a set operation."""

    query: Optional[Query] = None
    from_: Optional[str] = Field(default=None, alias="from")
    as_: Optional[str] = Field(default=None, alias="as")
    join: list[Join] = Field(default_factory=list)
    where: list[Any] = Field(default_factory=list)
    select: Optional[dict[str, Any]] = None
    distinct: bool = False
    group: Optional[Group] = None
    pivot: Optional[Pivot] = None
    having: list[Any] = Field(default_factory=list, description="Conditions on this stage's output columns (after select/group).")
    order: list[OrderBy] = Field(default_factory=list)
    limit: Optional[int] = Field(default=None, ge=1, le=100_000)
    union_all: Optional[list[str]] = None
    union: Optional[list[str]] = None
    intersect: Optional[list[str]] = None
    except_: Optional[list[str]] = Field(default=None, alias="except")

    @model_validator(mode="after")
    def _one_kind(self) -> "Stage":
        sets = [k for k in ("union_all", "union", "intersect", "except_") if getattr(self, k)]
        kinds = (self.query is not None) + (self.from_ is not None) + bool(sets)
        if kinds != 1:
            raise ValueError("a stage is exactly one of: query, from, union_all/union/intersect/except")
        if self.pivot and (self.select or self.group or self.join):
            raise ValueError("a pivot stage takes from, where and pivot only")
        return self

    def set_op(self) -> Optional[tuple[str, list[str]]]:
        for k in ("union_all", "union", "intersect", "except_"):
            if getattr(self, k):
                return k.rstrip("_"), getattr(self, k)
        return None


class StagedQuery(_Base):
    stages: dict[str, Stage] = Field(min_length=1, description="Named stages, in order; a stage may use earlier ones.")
    output: str
    order: list[OrderBy] = Field(default_factory=list)
    limit: Optional[int] = Field(default=None, ge=1, le=100_000)
    offset: Optional[int] = Field(default=None, ge=0)


@dataclass
class _StageInfo:
    columns: list[str]
    members: dict[str, str] = field(default_factory=dict)  # member path or calc name -> column


class StageCompiler:
    """Compiles a StagedQuery. `compile_query(query)` compiles one L0/L1 stage (no default limit)."""

    def __init__(self, compiler: Compiler, compile_query: Callable[[Query], Compiled], cube_names: set[str]):
        self.c = compiler
        self.dialect = compiler.dialect
        self.compile_query = compile_query
        self.cube_names = cube_names
        self.info: dict[str, _StageInfo] = {}
        self.warnings: list[str] = []
        self.partitions: dict[str, Any] = {}
        self.cubes: set[str] = set()

    # ------------------------------------------------------------ top level

    def compile(self, sq: StagedQuery, default_limit: int) -> Compiled:
        if len(sq.stages) > MAX_STAGES:
            raise ModelError(f"at most {MAX_STAGES} stages", "invalid")
        ctes: list[tuple[str, exp.Expression]] = []
        for name, stage in sq.stages.items():
            if not NAME_RE.match(name) or name in RESERVED or name in self.cube_names:
                raise ModelError(f"stage name {name!r} must be snake_case and not a cube or reserved name", "invalid")
            try:
                body = self._stage(name, stage)
            except ModelError as e:
                raise ModelError(f"stage {name}: {e}", e.code, e.hint) from None
            ctes.append((name, body))
        if sq.output not in self.info:
            raise ModelError(f"output {sq.output!r} is not a stage", "invalid")
        out = exp.select(*[_col(c) for c in self.info[sq.output].columns]).from_(_table(sq.output))
        for name, body in ctes:
            out = out.with_(_id(name), as_=body, copy=False)
        for o in sq.order:
            out = out.order_by(exp.Ordered(this=self._output_col(sq.output, o.column), desc=o.dir == "desc"), copy=False)
        out = out.limit(sq.limit or default_limit, copy=False)
        if sq.offset:
            out = out.offset(sq.offset, copy=False)
        cols = [Column(c, c, "stage", "unknown", humanize(c)) for c in self.info[sq.output].columns]
        return Compiled(sql=out.sql(dialect=self.dialect), dialect=self.dialect, columns=cols, cubes=sorted(self.cubes),
                        fingerprint=self.c.model.cube_fingerprint(self.cubes), partitions=self.partitions,
                        warnings=list(dict.fromkeys(self.warnings)), expression=out)

    def _stage(self, name: str, s: Stage) -> exp.Expression:
        if s.query is not None:
            compiled = self.compile_query(s.query)
            self.cubes |= set(compiled.cubes)
            self.warnings += [f"{name}: {w}" for w in compiled.warnings]
            self.partitions |= {f"{name}.{k}": v for k, v in compiled.partitions.items()}
            cols = [c.name for c in compiled.columns]
            members = {c.member: c.name for c in compiled.columns if c.kind != "calc"}
            members |= {c.name: c.name for c in compiled.columns}
            self.info[name] = _StageInfo(cols, members)
            return compiled.expression  # type: ignore[return-value]
        if (setop := s.set_op()) is not None:
            return self._set(name, *setop)
        return self._relational(name, s)

    # ------------------------------------------------------------ set operations

    def _set(self, name: str, op: str, parts: list[str]) -> exp.Expression:
        if len(parts) < 2:
            raise ModelError(f"{op} needs at least two stages")
        cols = None
        selects = []
        for p in parts:
            info = self._earlier(p)
            if cols is not None and info.columns != cols:
                raise ModelError(f"{op}: stage {p} has columns {info.columns}, expected {cols} (same names, same order)")
            cols = info.columns
            selects.append(exp.select(*[_col(c) for c in info.columns]).from_(_table(p)))
        fn = {"union_all": exp.union, "union": exp.union, "intersect": exp.intersect, "except": exp.except_}[op]
        body = selects[0]
        for sel in selects[1:]:
            body = fn(body, sel, distinct=op != "union_all")
        self.info[name] = _StageInfo(list(cols or []))
        return body

    # ------------------------------------------------------------ from / join / select / group / pivot

    def _relational(self, name: str, s: Stage) -> exp.Expression:
        scope = _Scope(self)
        scope.add(s.as_ or s.from_, self._earlier(s.from_))  # type: ignore[arg-type]
        sel = exp.select().from_(exp.Table(this=_id(s.from_), alias=exp.TableAlias(this=_id(s.as_ or s.from_))))
        for j in s.join:
            alias = j.as_ or j.stage
            if not NAME_RE.match(alias):
                raise ModelError(f"join alias {alias!r} must be snake_case")
            scope.add(alias, self._earlier(j.stage))
            if j.type == "cross":
                if j.on:
                    raise ModelError("a cross join takes no `on`")
                on = None
            else:
                if not j.on:
                    raise ModelError(f"join {alias}: `on` needs at least one column pair")
                on = exp.and_(*[exp.EQ(this=scope.expr(a), expression=scope.expr(b)) for a, b in j.on])
            table = exp.Table(this=_id(j.stage), alias=exp.TableAlias(this=_id(alias)))
            sel = sel.join(table, on=on, join_type=j.type.upper() if j.type != "cross" else "CROSS", copy=False)
        if s.where:
            sel = sel.where(exp.and_(*[scope.cond(w) for w in s.where]), copy=False)

        if s.pivot:
            body, cols = self._pivot(scope, sel, s.pivot)
        else:
            if s.select is not None:
                names = list(s.select)
                exprs = [scope.expr(e).as_(_name(n), quoted=True) for n, e in s.select.items()]
            else:
                names, exprs = scope.all_columns()
            sel.set("expressions", exprs)
            if s.distinct:
                sel = sel.distinct(copy=False)
            body, cols = sel, names
            if s.group is not None:
                body, cols = self._group(body, cols, s.group)
        if s.having or s.order or s.limit:
            out_scope = _Scope(self)
            out_scope.add("t", _StageInfo(cols))
            wrapped = exp.select(*[_col(c, "t") for c in cols]).from_(exp.Subquery(this=body, alias=exp.TableAlias(this=_id("t"))))
            if s.having:
                wrapped = wrapped.where(exp.and_(*[out_scope.cond(h) for h in s.having]), copy=False)
            for o in s.order:
                wrapped = wrapped.order_by(exp.Ordered(this=out_scope.expr(o.column), desc=o.dir == "desc"), copy=False)
            if s.limit:
                wrapped = wrapped.limit(s.limit, copy=False)
            body = wrapped
        self.info[name] = _StageInfo(cols)
        return body

    def _group(self, inner: exp.Select, cols: list[str], g: Group) -> tuple[exp.Expression, list[str]]:
        scope = _Scope(self)
        scope.add("g", _StageInfo(cols))
        by = [scope.expr(b) for b in g.by]
        by_names = [b.split(".")[-1] for b in g.by]
        aggs = [self._agg(scope, a).as_(_name(n), quoted=True) for n, a in g.agg.items()]
        if not aggs and not by:
            raise ModelError("group needs `by` or `agg`")
        out = exp.select(*[b.as_(n, quoted=True) for b, n in zip(by, by_names)], *aggs).from_(
            exp.Subquery(this=inner, alias=exp.TableAlias(this=_id("g"))))
        if by:
            out = out.group_by(*[b.copy() for b in by], copy=False)
        names = by_names + list(g.agg)
        if len(set(names)) != len(names):
            raise ModelError(f"group output names collide: {names}")
        return out, names

    def _agg(self, scope: "_Scope", a: Agg) -> exp.Expression:
        if a.of is None:
            if a.fn != "count":
                raise ModelError(f"{a.fn} needs `of`")
            return exp.Count(this=exp.Star())
        x = scope.expr(a.of)
        if a.fn == "count_distinct":
            return exp.Count(this=exp.Distinct(expressions=[x]))
        if a.fn == "median" and self.dialect == "bigquery":  # BigQuery has no MEDIAN aggregate
            return exp.Bracket(this=exp.func("APPROX_QUANTILES", x, exp.Literal.number(2)),
                               expressions=[exp.Anonymous(this="OFFSET", expressions=[exp.Literal.number(1)])])
        return exp.func(a.fn, x)

    def _pivot(self, scope: "_Scope", sel: exp.Select, p: Pivot) -> tuple[exp.Expression, list[str]]:
        index = [scope.expr(i) for i in p.index]
        index_names = [i.split(".")[-1] for i in p.index]
        col = scope.expr(p.column)
        prefix = p.prefix or p.column.split(".")[-1]
        names, exprs = [], []
        for v in p.values:
            n = _name(f"{prefix}_{_slug(v)}")
            if n in names or n in index_names:
                raise ModelError(f"pivot column {n} would appear twice; give a `prefix` or distinct values")
            cond = exp.Is(this=col.copy(), expression=exp.Null()) if v is None else exp.EQ(this=col.copy(), expression=_lit(v))
            of = scope.expr(p.agg.of) if p.agg.of is not None else exp.Literal.number(1)
            inner = exp.Case(ifs=[exp.If(this=cond, true=of)])
            fn = "count" if p.agg.fn == "count" else p.agg.fn
            agg = exp.Count(this=exp.Distinct(expressions=[inner])) if fn == "count_distinct" else exp.func(fn, inner)
            names.append(n)
            exprs.append(agg.as_(n, quoted=True))
        sel.set("expressions", [i.as_(n, quoted=True) for i, n in zip(index, index_names)] + exprs)
        if index:
            sel = sel.group_by(*[i.copy() for i in index], copy=False)
        return sel, index_names + names

    # ------------------------------------------------------------ helpers

    def _earlier(self, stage: Optional[str]) -> _StageInfo:
        if not stage or stage not in self.info:
            raise ModelError(f"{stage!r} is not an earlier stage")
        return self.info[stage]

    def _output_col(self, stage: str, col: str) -> exp.Column:
        info = self.info[stage]
        name = info.members.get(col, col)
        if name not in info.columns:
            raise ModelError(f"cannot order by {col}: {stage} has columns {info.columns}")
        return _col(name)


class _Scope:
    """Columns visible in one stage: alias -> earlier stage's columns. Compiles expressions and conditions."""

    def __init__(self, sc: StageCompiler):
        self.sc = sc
        self.sources: dict[str, _StageInfo] = {}

    def add(self, alias: Optional[str], info: _StageInfo) -> None:
        if not alias or alias in self.sources:
            raise ModelError(f"alias {alias!r} is used twice")
        self.sources[alias] = info

    def all_columns(self) -> tuple[list[str], list[exp.Expression]]:
        if len(self.sources) == 1:
            alias, info = next(iter(self.sources.items()))
            return list(info.columns), [_col(c, alias) for c in info.columns]
        seen: dict[str, str] = {}
        for alias, info in self.sources.items():
            for c in info.columns:
                if c in seen:
                    raise ModelError(f"column {c} is in both {seen[c]} and {alias}; list the output columns in `select`")
                seen[c] = alias
        return list(seen), [_col(c, a) for c, a in seen.items()]

    def column(self, ref: str) -> exp.Column:
        head, _, rest = ref.partition(".")
        if rest and head in self.sources:
            return exp.column(self._resolve(head, rest), head)
        hits = [(a, self._find(info, ref)) for a, info in self.sources.items()]
        hits = [(a, c) for a, c in hits if c]
        if not hits:
            raise ModelError(f"unknown column {ref!r}; available: {self._available()}")
        if len(hits) > 1:
            raise ModelError(f"column {ref!r} is ambiguous; prefix it with one of {[a for a, _ in hits]}")
        return _col(hits[0][1], hits[0][0])

    def _find(self, info: _StageInfo, name: str) -> Optional[str]:
        if name in info.columns:
            return name
        if name in info.members:
            return info.members[name]
        alias = output_alias(name)
        return alias if alias in info.columns else None

    def _resolve(self, alias: str, name: str) -> str:
        col = self._find(self.sources[alias], name)
        if col is None:
            raise ModelError(f"{alias} has no column {name!r}; it has {self.sources[alias].columns}")
        return col

    def _available(self) -> list[str]:
        return [f"{a}.{c}" for a, info in self.sources.items() for c in info.columns][:40]

    # ------------------------------------------------------------ expressions

    def expr(self, e: Any) -> exp.Expression:
        if isinstance(e, str):
            return self.column(e)
        if e is None or isinstance(e, (bool, int, float)):
            return _lit(e)
        if not isinstance(e, dict) or not e:
            raise ModelError(f"cannot read expression {e!r}: use a column name, a number, or one operator object")
        if "case" in e:
            if set(e) - {"case", "else"}:
                raise ModelError("case takes only `case` and `else`")
            return self._case(e)
        if "op" in e and "column" in e:  # a condition used as a boolean value
            return self.cond(e)
        if len(e) != 1:
            raise ModelError(f"an expression object has exactly one operator; got {sorted(e)}")
        (op, arg), = e.items()
        if op == "lit":
            if isinstance(arg, (dict, list)):
                raise ModelError("lit takes a string, number, boolean or null")
            return _lit(arg)
        if op in {"add", "sub", "mul"}:
            parts = self._list(op, arg, 2)
            cls = {"add": exp.Add, "sub": exp.Sub, "mul": exp.Mul}[op]
            out = parts[0]
            for p in parts[1:]:
                out = cls(this=out, expression=p)
            return exp.Paren(this=out)
        if op == "div":
            a, b = self._list(op, arg, 2, 2)
            return exp.Paren(this=exp.Div(this=exp.cast(a, "DOUBLE"), expression=exp.Nullif(this=b, expression=exp.Literal.number(0))))
        if op == "neg":
            return exp.Neg(this=self.expr(arg))
        if op == "fn":
            return self._fn(arg)
        if op == "cast":
            x, t = self._raw_list(op, arg, 2, 2)
            types = {"number": "DOUBLE", "integer": "BIGINT", "string": "TEXT", "date": "DATE", "timestamp": "TIMESTAMP", "boolean": "BOOLEAN"}
            if t not in types:
                raise ModelError(f"cast type must be one of {sorted(types)}")
            return exp.cast(self.expr(x), types[t])
        if op == "date_trunc":
            unit, x = self._raw_list(op, arg, 2, 2)
            return self._trunc(self._unit(unit), self.expr(x))
        if op == "date_diff":
            unit, a, b = self._raw_list(op, arg, 3, 3)
            return self._date_diff(self._unit(unit), self.expr(a), self.expr(b))
        if op == "date_add":
            unit, n, x = self._raw_list(op, arg, 3, 3)
            if not isinstance(n, int) or isinstance(n, bool):
                raise ModelError("date_add takes a whole number of units")
            from .compiler import _shift_expr

            return _shift_expr(self.expr(x), n, self._unit(unit), self.sc.dialect)
        if op == "extract":
            part, x = self._raw_list(op, arg, 2, 2)
            if part not in {"year", "quarter", "month", "week", "day", "dow", "hour"}:
                raise ModelError("extract part must be year, quarter, month, week, day, dow or hour")
            unit = {"dow": "DAYOFWEEK"}.get(part, part.upper())
            return exp.Extract(this=exp.Var(this=unit), expression=self.expr(x))
        if op == "window":
            return self._window(arg)
        if op in {"and", "or", "not"}:
            return self.cond(e)
        raise ModelError(f"unknown expression operator {op!r}")

    def _list(self, op: str, arg: Any, lo: int, hi: int = 50) -> list[exp.Expression]:
        return [self.expr(a) for a in self._raw_list(op, arg, lo, hi)]

    @staticmethod
    def _raw_list(op: str, arg: Any, lo: int, hi: int) -> list[Any]:
        if not isinstance(arg, list) or not lo <= len(arg) <= hi:
            raise ModelError(f"{op} takes a list of {lo}" + (f"-{hi}" if hi != lo else "") + " items")
        return arg

    @staticmethod
    def _unit(u: Any) -> str:
        if u not in TIME_UNITS:
            raise ModelError(f"time unit must be one of {sorted(TIME_UNITS)}")
        return u

    def _trunc(self, unit: str, x: exp.Expression) -> exp.Expression:
        u = "ISOWEEK" if unit == "week" and self.sc.dialect == "bigquery" else unit.upper()
        return exp.TimestampTrunc(this=x, unit=exp.Var(this=u))

    def _date_diff(self, unit: str, a: exp.Expression, b: exp.Expression) -> exp.Expression:
        """Whole `unit` boundaries crossed from a to b, the same in every dialect (DATE_DIFF and AGE disagree)."""
        def part(p: str, x: exp.Expression) -> exp.Expression:
            return exp.Extract(this=exp.Var(this=p), expression=x.copy())

        years = exp.Sub(this=part("YEAR", b), expression=part("YEAR", a))
        if unit == "year":
            return exp.Paren(this=years)
        if unit == "quarter":
            return exp.Paren(this=exp.Add(this=exp.Mul(this=exp.Paren(this=years), expression=exp.Literal.number(4)),
                                          expression=exp.Sub(this=part("QUARTER", b), expression=part("QUARTER", a))))
        if unit == "month":
            return exp.Paren(this=exp.Add(this=exp.Mul(this=exp.Paren(this=years), expression=exp.Literal.number(12)),
                                          expression=exp.Sub(this=part("MONTH", b), expression=part("MONTH", a))))
        da, db = exp.cast(self._trunc(unit, a) if unit == "week" else a.copy(), "DATE"), exp.cast(self._trunc(unit, b) if unit == "week" else b.copy(), "DATE")
        days = exp.DateDiff(this=db, expression=da, unit=exp.Var(this="DAY"))
        return exp.Paren(this=exp.Div(this=days, expression=exp.Literal.number(7))) if unit == "week" else days

    def _case(self, e: dict) -> exp.Expression:
        whens = e["case"]
        if not isinstance(whens, list) or not whens:
            raise ModelError('case takes [{"when": condition, "then": expression}, ...] and an optional "else"')
        ifs = []
        for w in whens:
            if not isinstance(w, dict) or set(w) != {"when", "then"}:
                raise ModelError('each case item is {"when": condition, "then": expression}')
            ifs.append(exp.If(this=self.cond(w["when"]), true=self.expr(w["then"])))
        return exp.Case(ifs=ifs, default=self.expr(e["else"]) if "else" in e else None)

    def _fn(self, arg: Any) -> exp.Expression:
        if not isinstance(arg, dict) or "name" not in arg:
            raise ModelError('fn takes {"name": ..., "args": [...]}')
        name, args = arg["name"], arg.get("args", [])
        if name not in SCALAR_FNS:
            raise ModelError(f"function {name!r} isn't available; use one of {sorted(SCALAR_FNS)}")
        lo, hi = SCALAR_FNS[name]
        parts = self._list(f"fn {name}", args, lo, hi)
        return exp.func(name, *parts)

    def _window(self, w: Any) -> exp.Expression:
        if not isinstance(w, dict) or "fn" not in w:
            raise ModelError('window takes {"fn": ..., "of": ..., "partition": [...], "order": [...], "frame": [start, end]}')
        extra = set(w) - {"fn", "of", "offset", "buckets", "partition", "order", "frame"}
        if extra:
            raise ModelError(f"unknown window keys {sorted(extra)}")
        fn = w["fn"]
        if fn not in WINDOW_FNS:
            raise ModelError(f"window function {fn!r} isn't available; use one of {sorted(WINDOW_FNS)}")
        of = self.expr(w["of"]) if w.get("of") is not None else None
        if fn in {"sum", "avg", "min", "max", "count", "lag", "lead", "first_value", "last_value", "stddev"} and of is None and fn != "count":
            raise ModelError(f"window {fn} needs `of`")
        if fn in {"lag", "lead"}:
            off = w.get("offset", 1)
            if not isinstance(off, int) or isinstance(off, bool) or not 0 < off <= 1000:
                raise ModelError("offset must be 1-1000")
            call: exp.Expression = exp.Anonymous(this=fn.upper(), expressions=[of, exp.Literal.number(off)])
        elif fn in {"rank", "dense_rank", "row_number", "percent_rank", "cume_dist"}:
            call = exp.Anonymous(this=fn.upper())
        elif fn == "ntile":
            n = w.get("buckets")
            if not isinstance(n, int) or isinstance(n, bool) or not 1 < n <= 1000:
                raise ModelError("ntile needs buckets: 2-1000")
            call = exp.Anonymous(this="NTILE", expressions=[exp.Literal.number(n)])
        elif fn in {"first_value", "last_value"}:
            call = exp.Anonymous(this=fn.upper(), expressions=[of])
        elif fn == "count":
            call = exp.Count(this=of if of is not None else exp.Star())
        else:
            call = exp.func(fn, of)
        order = []
        for o in w.get("order", []):
            if isinstance(o, str):
                o = {"by": o}
            if not isinstance(o, dict) or "by" not in o or o.get("dir", "asc") not in {"asc", "desc"}:
                raise ModelError('window order items are a column or {"by": expression, "dir": "asc"|"desc"}')
            order.append(exp.Ordered(this=self.expr(o["by"]), desc=o.get("dir") == "desc"))
        spec = None
        if "frame" in w:
            frame = w["frame"]
            if not order:
                raise ModelError("a window frame needs `order`")
            if not isinstance(frame, list) or len(frame) != 2 or not all(f is None or (isinstance(f, int) and not isinstance(f, bool)) for f in frame):
                raise ModelError("frame is [start, end] in rows relative to the current one (negative = preceding), null = unbounded")
            start, end = frame
            if start is not None and end is not None and start > end:
                raise ModelError("frame start must not be after its end")
            spec = exp.WindowSpec(kind="ROWS", **_bound("start", start, "PRECEDING"), **_bound("end", end, "FOLLOWING"))
        return exp.Window(this=call, partition_by=[self.expr(p) for p in w.get("partition", [])] or None,
                          order=exp.Order(expressions=order) if order else None, spec=spec)

    # ------------------------------------------------------------ conditions

    def cond(self, c: Any) -> exp.Expression:
        if not isinstance(c, dict):
            raise ModelError(f"cannot read condition {c!r}")
        if set(c) == {"and"} or set(c) == {"or"}:
            (k, items), = c.items()
            if not isinstance(items, list) or not items:
                raise ModelError(f"{k} takes a non-empty list of conditions")
            parts = [self.cond(i) for i in items]
            return exp.Paren(this=exp.and_(*parts) if k == "and" else exp.or_(*parts))
        if set(c) == {"not"}:
            return exp.Not(this=exp.Paren(this=self.cond(c["not"])))
        if "column" not in c or "op" not in c:
            raise ModelError(f'a condition is {{"column": ..., "op": ..., "values": [...]}} or and/or/not; got {c!r}')
        allowed = {"column", "op", "values", "column_ref", "stage", "stage_column"}
        if set(c) - allowed:
            raise ModelError(f"unknown condition keys {sorted(set(c) - allowed)}")
        x, op = self.expr(c["column"]), c["op"]
        if op in {"in_stage", "not_in_stage"}:
            info = self.sc._earlier(c.get("stage"))
            sc = c.get("stage_column")
            col = info.members.get(sc, sc)
            if col not in info.columns:
                raise ModelError(f"{c.get('stage')} has no column {sc!r}")
            sub = exp.select(_col(col)).from_(_table(c["stage"])).where(exp.Not(this=exp.Is(this=_col(col), expression=exp.Null())))
            e = exp.In(this=x, query=exp.Subquery(this=sub))
            return exp.Not(this=e) if op == "not_in_stage" else e
        if op in {"set", "not_set"}:
            e = exp.Is(this=x, expression=exp.Null())
            return e if op == "not_set" else exp.Not(this=e)
        if "column_ref" in c:
            rhs = [self.expr(c["column_ref"])]
        else:
            vals = c.get("values")
            if not isinstance(vals, list) or not vals or any(isinstance(v, (dict, list)) for v in vals):
                raise ModelError(f"{op} needs `values`: a list of literals (or `column_ref`)")
            rhs = [_lit(v) for v in vals]
        if op in {"equals", "not_equals"}:
            e = exp.EQ(this=x, expression=rhs[0]) if len(rhs) == 1 else exp.In(this=x, expressions=rhs)
            return exp.Not(this=e) if op == "not_equals" else e
        if op in {"gt", "gte", "lt", "lte"}:
            if len(rhs) != 1:
                raise ModelError(f"{op} takes one value")
            cls = {"gt": exp.GT, "gte": exp.GTE, "lt": exp.LT, "lte": exp.LTE}[op]
            return cls(this=x, expression=rhs[0])
        if op in {"contains", "starts_with", "ends_with"}:
            if "column_ref" in c or not all(isinstance(v, str) for v in c["values"]):
                raise ModelError(f"{op} takes string values")
            pats = [{"contains": f"%{_esc(v.lower())}%", "starts_with": f"{_esc(v.lower())}%", "ends_with": f"%{_esc(v.lower())}"}[op]
                    for v in c["values"]]
            ors = [exp.Like(this=exp.Lower(this=x.copy()), expression=exp.Literal.string(p)) for p in pats]
            return exp.Paren(this=exp.or_(*ors))
        raise ModelError(f"unknown condition op {op!r}")


SCALAR_FNS: dict[str, tuple[int, int]] = {
    "coalesce": (2, 10), "nullif": (2, 2), "abs": (1, 1), "round": (1, 2), "floor": (1, 1), "ceil": (1, 1),
    "ln": (1, 1), "exp": (1, 1), "sqrt": (1, 1), "power": (2, 2), "greatest": (2, 10), "least": (2, 10),
    "lower": (1, 1), "upper": (1, 1), "length": (1, 1), "concat": (2, 10), "sign": (1, 1),
}
WINDOW_FNS = {"sum", "avg", "min", "max", "count", "stddev", "rank", "dense_rank", "row_number", "percent_rank",
              "cume_dist", "ntile", "lag", "lead", "first_value", "last_value"}


def _bound(side: str, n: Optional[int], far: str) -> dict[str, Any]:
    if n is None:
        return {side: "UNBOUNDED", f"{side}_side": "PRECEDING" if side == "start" else "FOLLOWING"}
    if n == 0:
        return {side: "CURRENT ROW"}
    return {side: exp.Literal.number(abs(n)), f"{side}_side": "PRECEDING" if n < 0 else "FOLLOWING"}


def _id(name: str) -> exp.Identifier:
    """Every name L2 emits is quoted, so a stage or column may be called `returning` or `order`."""
    return exp.to_identifier(name, quoted=True)


def _col(name: Any, table: Any = None) -> exp.Column:
    return exp.column(name, table, quoted=True)


def _table(name: str) -> exp.Table:
    return exp.Table(this=_id(name))


def _lit(v: Any) -> exp.Expression:
    if v is None:
        return exp.Null()
    if isinstance(v, bool):
        return exp.Boolean(this=v)
    if isinstance(v, (int, float)):
        return exp.Literal.number(v)
    if isinstance(v, str):
        return exp.Literal.string(v)
    raise ModelError(f"unsupported literal {v!r}")


def _name(n: str) -> str:
    if not NAME_RE.match(n):
        raise ModelError(f"output column {n!r} must be snake_case")
    return n


def _slug(v: Any) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(v).lower()).strip("_")
    return s or "null"


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
