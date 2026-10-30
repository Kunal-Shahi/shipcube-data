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
    check_scalar_sql(parsed, sql)
    return parsed


# Functions that read files, the network, the environment or other databases. Never allowed in
# model SQL (members, filters, segments, joins) or raw SQL. (Red-team issues #5, #6.)
FORBIDDEN_FUNCS = {
    "read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto", "read_ndjson", "read_text", "read_blob",
    "glob", "getenv", "current_setting", "query", "query_table", "sniff_csv", "pg_read_file", "pg_read_binary_file",
    "pg_ls_dir", "pg_stat_file", "lo_import", "lo_export", "dblink", "external_query", "url", "file", "s3", "remote",
    "mysql", "postgresql", "postgres_scan", "sqlite_scan", "iceberg_scan", "delta_scan", "parquet_scan", "load_file",
}


FORBIDDEN_FUNCS |= {
    # Postgres: run a query or a statement, change settings, touch sequences (shipcube-data #8)
    "set_config", "setval", "nextval", "currval", "lastval", "xpath", "xpath_exists", "xmlparse", "txid_current",
    "current_query", "inet_server_addr", "inet_client_addr", "version",
}
FORBIDDEN_PREFIXES = ("read_", "pg_", "lo_", "dblink", "duckdb_", "txid_")
FORBIDDEN_SUFFIXES = ("_scan", "_to_xml", "_to_xmlschema", "_to_xml_and_xmlschema")

# Functions sqlglot doesn't know (exp.Anonymous) are refused unless listed here, so a new
# dialect-specific function that runs SQL or reaches outside the warehouse can't slip through a
# denylist. Functions sqlglot types (SUM, CAST, DATE_TRUNC, UPPER, ...) are checked against the
# denylist only.
ALLOWED_UNTYPED_FUNCS = {
    "abs", "age", "any_value", "array_length", "bool_and", "bool_or", "btrim", "cardinality", "ceil", "ceiling",
    "char_length", "concat_ws", "contains", "corr", "countif", "date_add", "date_diff", "date_part", "date_sub",
    "date_trunc", "datediff", "datetime_trunc", "ends_with", "epoch", "epoch_ms", "farm_fingerprint", "format_date",
    "format_timestamp", "from_unixtime", "greatest", "hash", "ifnull", "initcap", "last_day", "least", "left", "length",
    "list_contains", "ln", "log", "lpad", "ltrim", "make_date", "md5", "median", "mod", "nullif", "nvl",
    "octet_length", "parse_date", "parse_timestamp", "percentile_cont", "percentile_disc", "position", "power",
    "regexp_contains", "regexp_extract", "regexp_matches", "regexp_replace", "repeat", "replace", "reverse",
    "right", "round", "rpad", "rtrim", "safe_divide", "sha1", "sha256", "sign", "split_part", "sqrt",
    "starts_with", "stddev", "strpos", "timestamp_trunc", "to_char", "to_date", "to_hex", "to_timestamp",
    "translate", "trunc", "unix_timestamp", "variance",
}


def func_name(node: exp.Func) -> str:
    return (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()


def forbidden_function(node: exp.Expression) -> str | None:
    if isinstance(node, exp.ReadCSV):
        return "read_csv"
    if isinstance(node, exp.Func):
        name = func_name(node)
        if name in FORBIDDEN_FUNCS or name.startswith(FORBIDDEN_PREFIXES) or name.endswith(FORBIDDEN_SUFFIXES):
            return name
        if isinstance(node, exp.Anonymous) and name not in ALLOWED_UNTYPED_FUNCS:
            return name
    return None


def check_scalar_sql(tree: exp.Expression, sql: str) -> None:
    """Model SQL is a scalar expression over one cube's columns: no subqueries, no table
    references, no file/network functions. Otherwise a new member could read another table or
    cube and side-step its grants and row policies."""
    for node in tree.walk():
        if isinstance(node, (exp.Query, exp.Subquery, exp.Table, exp.Exists)):
            raise ModelError(f"model SQL may not contain subqueries or table references: {sql!r}", "invalid_sql")
        bad = forbidden_function(node)
        if bad:
            raise ModelError(f"model SQL may not call {bad}: {sql!r}", "invalid_sql",
                             "only standard scalar and aggregate functions are allowed in model SQL")


def raw_columns(sql: str, dialect: str) -> set[str]:
    """Physical columns a snippet reads directly (not through {member} references), lowercased."""
    return {c.name.lower() for c in parse_expr(sql, dialect).find_all(exp.Column) if ref_name(c) is None}


# Relation aliases the compiler itself uses, besides the cube names.
INTERNAL_ALIASES = {"keys", "base", "raw"}


def row_reference(sql: str, dialect: str, relations: set[str]) -> str | None:
    """A reference in filter SQL that reads a whole row, or another relation's columns: a qualified
    column (`customers.city`), a star, or a bare name that is a relation alias (`CAST(order_items AS
    TEXT)` is the whole row in Postgres). Segments may only read their own cube's plain columns."""
    names = {r.lower() for r in relations} | INTERNAL_ALIASES
    tree = parse_expr(sql, dialect)
    if tree.find(exp.Star):
        return "*"
    for c in tree.find_all(exp.Column):
        if ref_name(c) is not None:
            continue
        if c.table:
            return c.sql()
        if c.name.lower() in names:
            return c.name
    return None


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
