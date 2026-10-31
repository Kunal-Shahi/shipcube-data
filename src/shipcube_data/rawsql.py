"""L3: validated, read-only raw SQL. Results are labelled unverified.

This is the escape hatch for questions the typed layers cannot express. It is
deliberately narrow: one SELECT, only the caller's tenant schemas, no table
functions, no row policies in force. Production also runs it on a read-only
warehouse role with a statement timeout.
"""

from __future__ import annotations

from sqlglot import exp

from .compiler import Compiler, SecurityContext
from .model import Model, ModelError
from .sqlutil import forbidden_function, parse_query

FORBIDDEN = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Alter,
    exp.Command, exp.TruncateTable, exp.Set, exp.Use, exp.Transaction, exp.Commit, exp.Rollback,
    exp.Into, exp.Copy, exp.Pragma, exp.Attach, exp.Detach,
)
SYSTEM_SCHEMAS = {"information_schema", "pg_catalog", "system", "main"}


def allowed_schemas(model: Model, ctx: SecurityContext) -> set[str]:
    out = set()
    for b in model.bindings.values():
        if b.tenant in {"*", ctx.tenant} and not b.tenant_column and (b.isolated or b.shared):
            out.add(b.schema_.replace("{tenant}", ctx.tenant))
    return out


def default_schema(model: Model, ctx: SecurityContext) -> str:
    b = model.bindings.get(f"*@{ctx.tenant}") or model.bindings.get("*@*")
    if not b:
        raise ModelError(f"no default schema binding for tenant {ctx.tenant}")
    return b.schema_.replace("{tenant}", ctx.tenant)


def validate(sql: str, model: Model, ctx: SecurityContext, dialect: str, limit: int = 1000) -> str:
    tree = parse_query(sql, dialect)
    if not isinstance(tree, exp.Query):
        raise ModelError("raw SQL must be a single SELECT")
    for node in tree.walk():
        if isinstance(node, FORBIDDEN):
            raise ModelError(f"raw SQL may not contain {node.key.upper()}")
        if isinstance(node, exp.Func):
            name = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
            if forbidden_function(node):
                raise ModelError(f"raw SQL may not call {name}")
    comp = Compiler(model, ctx, dialect, dialect)
    if any(comp.policies_for(c) for c in model.cubes):
        raise ModelError("row policies apply to you; use a typed query (L0-L2) so they can be enforced")
    allowed = allowed_schemas(model, ctx)
    default = default_schema(model, ctx)
    ctes = {c.alias for c in tree.find_all(exp.CTE)}
    for t in tree.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier):
            raise ModelError("raw SQL may not use table functions")
        if t.args.get("catalog"):
            raise ModelError("raw SQL may not name another database")
        if not t.db:
            if t.name in ctes:
                continue
            t.set("db", exp.to_identifier(default))
        if t.db.lower() in SYSTEM_SCHEMAS or t.db not in allowed:
            raise ModelError(f"schema {t.db} is not readable by tenant {ctx.tenant}")
    wrapped = exp.select("*").from_(tree.subquery("raw")).limit(limit)
    return wrapped.sql(dialect=dialect)
