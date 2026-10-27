"""Small helpers for parsing the SQL snippets that live in the model."""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from .model import ModelError

# {member} or {cube.member} inside member/segment SQL.
REF_RE = re.compile(r"\{([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)?)\}")
REF_PREFIX = "__ref__"


def refs(sql: str | None) -> list[str]:
    return REF_RE.findall(sql or "")


def parse_expr(sql: str, dialect: str) -> exp.Expression:
    """Parse a scalar SQL expression, with {refs} replaced by placeholder columns."""
    text = REF_RE.sub(lambda m: REF_PREFIX + m.group(1).replace(".", "__"), sql)
    try:
        parsed = sqlglot.parse_one(text, read=dialect)
    except ParseError as e:
        raise ModelError(f"cannot parse SQL {sql!r}: {e.errors[0]['description'] if e.errors else e}") from None
    if isinstance(parsed, (exp.Query, exp.Command)) or parsed is None:
        raise ModelError(f"expected a SQL expression, got a statement: {sql!r}")
    return parsed


def ref_name(col: exp.Column) -> str | None:
    """If `col` is a {ref} placeholder, return 'member' or 'cube.member'."""
    if col.table or not col.name.startswith(REF_PREFIX):
        return None
    return col.name[len(REF_PREFIX):].replace("__", ".", 1)


def has_aggregate(e: exp.Expression) -> bool:
    return e.find(exp.AggFunc) is not None


def parse_query(sql: str, dialect: str) -> exp.Expression:
    try:
        stmts = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except ParseError as e:
        raise ModelError(f"cannot parse SQL: {e.errors[0]['description'] if e.errors else e}") from None
    if len(stmts) != 1:
        raise ModelError(f"expected exactly one SQL statement, got {len(stmts)}")
    return stmts[0]
