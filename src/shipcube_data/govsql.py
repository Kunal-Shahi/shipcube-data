"""Governed SQL: ordinary SELECTs against cube names, rewritten so governance holds.

A caller writes SQL as if each cube were a table:

    SELECT region, count(*) FROM orders JOIN customers USING (customer_id) ...

Every reference to a cube becomes a subquery that is the governed view of that cube for this caller:

* its rows: the tenant's schema (and tenant column), every row policy that applies to the caller
  (joining the cubes a policy needs, many-to-one only, so rows aren't duplicated), all partitions;
* its columns: the caller's visible dimensions under their member names, the row-level input of each
  visible sum/avg/min/max/count/count_distinct measure (so `SUM(revenue)` is the governed revenue), and
  the key columns of the model's joins (unless a hidden member reads them), so cubes join as the model does.
  Physical columns, hidden members and the columns of policy-only joins are never exposed.

Anything else in the statement may be any read-only SQL the warehouse supports: CTEs, joins, windows,
subqueries, pivots via CASE. Table references must be cubes or the statement's own CTEs; schema-qualified
names, table functions, recursive CTEs, DDL/DML and functions outside the model-SQL allowlist are refused.
Results are labelled `verified: false` (the layer can't prove arbitrary joins don't fan out) and `governed: true`.
"""

from __future__ import annotations

from sqlglot import exp

from .compiler import Compiler, SecurityContext
from .model import Cube, Measure, Model, ModelError
from .partitions import Bounds
from .query import filter_members
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
    views: dict[str, exp.Select] = {}
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
        if name not in views:
            views[name] = governed_view(comp, model.cube(name))
        alias = t.alias or t.name
        t.replace(exp.Subquery(this=views[name].copy(), alias=exp.TableAlias(this=exp.to_identifier(alias))))
    warnings += comp.warnings
    wrapped = exp.select("*").from_(exp.Subquery(this=tree, alias=exp.TableAlias(this=exp.to_identifier("governed")))).limit(limit)
    return wrapped.sql(dialect=dialect), list(dict.fromkeys(warnings))


def governed_view(comp: Compiler, cube: Cube) -> exp.Select:
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
    # The key columns of the model's own joins, so cubes can be joined in SQL the way the model joins them.
    hidden = comp.hidden_columns(cube)
    for edge in comp._edges(cube.name):
        for c in parse_expr(edge.on, comp.src).find_all(exp.Column):
            key = c.name.lower()
            if c.table == cube.name and key not in names and key not in hidden:
                names.add(key)
                cols.append(exp.column(c.name, cube.name).as_(key))

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
    src, preds = comp.source(cube, Bounds())
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
