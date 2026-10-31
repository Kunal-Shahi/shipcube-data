"""Governed SQL: ordinary SELECTs against cube names, rewritten so governance holds.

A caller writes SQL as if each cube were a table:

    SELECT region, count(*) FROM orders JOIN customers USING (customer_id) ...

Every reference to a cube becomes a subquery that is the governed view of that cube for this caller:

* its rows: the tenant's schema (and tenant column), every row policy that applies to the caller
  (joining the cubes a policy needs, many-to-one only, so rows aren't duplicated), all partitions;
* its columns: the caller's visible dimensions under their member names, and the row-level input of
  each visible sum/avg/min/max/count/count_distinct measure (so `SUM(revenue)` is the governed revenue).
  Join keys are columns only when the model declares them as dimensions (so grants apply to them too);
  physical columns never are.
  Physical columns, hidden members and the columns of policy-only joins are never exposed.

Anything else in the statement may be any read-only SQL the warehouse supports: CTEs, joins, windows,
subqueries, pivots via CASE. Table references must be cubes or the statement's own CTEs; schema-qualified
names, table functions, recursive CTEs, DDL/DML and functions outside the model-SQL allowlist are refused.
Results are labelled `verified: false` (the layer can't prove arbitrary joins don't fan out) and `governed: true`.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from sqlglot import exp

from .compiler import Compiler, SecurityContext
from .model import Cube, Dimension, Measure, Model, ModelError
from .partitions import Bounds
from .query import Condition, filter_members
from .rawsql import FORBIDDEN
from .sqlutil import forbidden_function, parse_expr, parse_query

MEASURE_INPUTS = {"sum", "avg", "min", "max", "count", "count_distinct"}


def rewrite(sql: str, model: Model, ctx: SecurityContext, dialect: str, source_dialect: str, limit: int) -> tuple[str, list[str]]:
    """The executable SQL for `sql`, and warnings. Raises ModelError("forbidden_sql") when it can't be governed."""
    tree = parse_query(sql, dialect)
    if not isinstance(tree, exp.Query):
        raise ModelError("governed SQL must be a single SELECT", "forbidden_sql")
    for node in tree.walk():
        if isinstance(node, FORBIDDEN):
            raise ModelError(f"governed SQL may not contain {node.key.upper()}", "forbidden_sql")
        bad = forbidden_function(node)
        if bad:
            raise ModelError(f"governed SQL may not call {bad}", "forbidden_sql",
                             "only standard scalar, aggregate and window functions are allowed")
        if isinstance(node, exp.With) and node.args.get("recursive"):
            raise ModelError("governed SQL may not use recursive CTEs", "forbidden_sql")
    ctes = {c.alias.lower() for c in tree.find_all(exp.CTE)}
    clash = ctes & set(model.cubes)
    if clash:
        raise ModelError(f"a CTE may not be named like a cube: {sorted(clash)}", "forbidden_sql")

    comp = Compiler(model, ctx, dialect, source_dialect)
    pushed = _pushdown_conditions(tree, model, ctes)
    views: dict[tuple[str, str], exp.Select] = {}
    warnings: list[str] = []
    for t in list(tree.find_all(exp.Table)):
        if not isinstance(t.this, exp.Identifier):
            raise ModelError("governed SQL may not use table functions", "forbidden_sql")
        if t.args.get("db") or t.args.get("catalog"):
            raise ModelError(f"use cube names, not schema-qualified tables ({t.sql(dialect=dialect)})", "forbidden_sql",
                             "list_cubes shows the cubes you can query")
        name = t.name.lower()
        if name in ctes:
            continue
        if name not in model.cubes:
            raise ModelError(f"unknown table {t.name!r}: governed SQL reads cubes", "forbidden_sql",
                             f"cubes: {', '.join(sorted(model.cubes))}")
        cube = model.cube(name)
        bounds = comp._bounds(cube, pushed.get(id(t), []), None)
        key = (name, repr(bounds))
        if key not in views:
            views[key] = governed_view(comp, cube, bounds)
        alias = t.alias or t.name
        t.replace(exp.Subquery(this=views[key].copy(), alias=exp.TableAlias(this=exp.to_identifier(alias))))
    warnings += comp.warnings
    wrapped = exp.select("*").from_(exp.Subquery(this=tree, alias=exp.TableAlias(this=exp.to_identifier("governed")))).limit(limit)
    return wrapped.sql(dialect=dialect), list(dict.fromkeys(warnings))


_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FLIP = {exp.GT: "lt", exp.GTE: "lte", exp.LT: "gt", exp.LTE: "gte", exp.EQ: "equals"}
_OPS = {exp.GT: "gt", exp.GTE: "gte", exp.LT: "lt", exp.LTE: "lte", exp.EQ: "equals"}


def _pushdown_conditions(tree: exp.Expression, model: Model, ctes: set[str]) -> dict[int, list[Condition]]:
    """Partition bounds each cube reference may be read with: {id(Table node): [conditions]}.

    Only what's provably safe to apply early: a top-level AND term of the WHERE of the same SELECT that
    names the cube in its FROM/JOIN, comparing one of that cube's dimensions with a literal (>, >=, <, <=,
    =, BETWEEN). Those comparisons reject NULLs, so rows they'd drop never reach the result, even through
    an outer join. A column is matched to a cube by its qualifier, or unqualified when the SELECT reads
    exactly one source. Everything else (OR, subqueries, CTE columns) is just not pruned. The compiler's
    own `_bounds` turns the conditions into partitions, as it does for typed queries."""
    out: dict[int, list[Condition]] = {}
    for sel in tree.find_all(exp.Select):
        where = sel.args.get("where")
        frm = sel.args.get("from_") or sel.args.get("from")  # the key differs across sqlglot versions
        sources = [frm.this] if frm else []
        sources += [j.this for j in sel.args.get("joins") or []]
        if where is None or not sources:
            continue
        by_alias: dict[str, exp.Table] = {}
        for s in sources:
            if isinstance(s, exp.Table) and s.name.lower() in model.cubes and s.name.lower() not in ctes and not s.args.get("db"):
                by_alias[(s.alias or s.name).lower()] = s
        if not by_alias:
            continue
        only = next(iter(by_alias.values())) if len(sources) == 1 else None
        for term in where.this.flatten() if isinstance(where.this, exp.And) else [where.this]:
            for col, op, value in _comparisons(term):
                table = by_alias.get(col.table.lower()) if col.table else only
                if table is None:
                    continue
                cube = model.cube(table.name.lower())
                if not isinstance(cube.member(col.name), Dimension):
                    continue
                if op == "lt" and _DATE_ONLY.match(value):  # strictly before midnight: the day before, inclusive
                    op, value = "lte", (date.fromisoformat(value) - timedelta(days=1)).isoformat()
                out.setdefault(id(table), []).append(Condition(member=f"{cube.name}.{col.name}", op=op, values=[value]))
    return out


def _comparisons(term: exp.Expression) -> list[tuple[exp.Column, str, str]]:
    def literal(e: exp.Expression) -> str | None:
        while isinstance(e, (exp.Cast, exp.Paren)):
            e = e.this
        return e.this if isinstance(e, exp.Literal) and e.is_string else None

    if isinstance(term, exp.Between) and isinstance(term.this, exp.Column):
        lo, hi = literal(term.args["low"]), literal(term.args["high"])
        return [(term.this, "gte", lo), (term.this, "lte", hi)] if lo and hi else []
    for cls, op in _OPS.items():
        if type(term) is cls:
            a, b = term.this, term.expression
            if isinstance(a, exp.Column) and (v := literal(b)):
                return [(a, op, v)]
            if isinstance(b, exp.Column) and (v := literal(a)):
                return [(b, _FLIP[cls], v)]
    return []


def governed_view(comp: Compiler, cube: Cube, bounds: Bounds | None = None) -> exp.Select:
    """SELECT <visible members> FROM <cube's tenant source, plus many-to-one joins for its policies> WHERE <policies>."""
    cols = []
    names: set[str] = set()
    for m in cube.members():
        if not comp.visible(cube, m):
            continue
        if isinstance(m, Measure):
            e = _measure_input(comp, cube, m)
            if e is None:
                continue
        else:
            e = comp.member_sql(cube, m)
        if m.name not in names:
            names.add(m.name)
            cols.append(e.as_(m.name))
    if not cols:
        raise ModelError(f"unknown table {cube.name!r}: governed SQL reads cubes", "forbidden_sql")

    # Row policies may need other cubes (a region on states for orders): join them, to a fixed point,
    # and apply their own policies too, exactly as a typed query would.
    needed, policies = {cube.name}, {}
    while True:
        tree = comp.join_tree(cube.name, needed)
        in_tree = {cube.name} | {e.dst for e in tree}
        for c in in_tree - policies.keys():
            policies[c] = comp.policies_for(c)
        extra = {comp.model.resolve(p)[0].name for fs in policies.values() for f in fs for p in filter_members(f)}
        if extra <= in_tree:
            break
        needed |= extra
    fan = [e for e in tree if e.relationship not in {"many_to_one", "one_to_one"}]
    if fan:
        raise ModelError(f"a row policy on {cube.name} needs {fan[0].dst} through a {fan[0].relationship} join, which would "
                         "duplicate rows; use a typed query (L0-L2) for this cube", "forbidden_sql")
    where: list[exp.Expression] = []
    src, preds = comp.source(cube, bounds or Bounds())
    sel = exp.select(*cols).from_(src)
    where += preds
    for e in tree:
        other, other_preds = comp.source(comp.model.cube(e.dst), Bounds())
        sel = sel.join(other, on=parse_expr(e.on, comp.src), join_type="LEFT")
        where += other_preds
    where += [comp.filter_sql(f) for fs in policies.values() for f in fs]
    if where:
        sel = sel.where(exp.and_(*where))
    return sel


def _measure_input(comp: Compiler, cube: Cube, m: Measure) -> exp.Expression | None:
    """The per-row value a measure aggregates, so SUM/AVG/... over it gives the governed number."""
    if m.agg not in MEASURE_INPUTS:
        return None
    if m.agg == "count" and not m.sql:
        if not m.filter:
            return None  # plain count: COUNT(*) already says it
        val: exp.Expression = exp.Literal.number(1)
    elif not m.sql:
        return None
    else:
        val = comp._expand(cube, m.sql, stack=[m.name])
    if m.filter:
        val = exp.Case(ifs=[exp.If(this=comp._expand(cube, m.filter, stack=[m.name]), true=val)])
    return val
