"""The in-memory semantic model.

A `Model` is what the catalog holds at one model version. Migrations are pure
functions from one `Model` to the next; the catalog persists the difference.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

IDENT = r"^[a-z][a-z0-9_]*$"
_IDENT_RE = re.compile(IDENT)

Agg = Literal["count", "count_distinct", "sum", "avg", "min", "max", "number"]
DimType = Literal["string", "number", "time", "boolean"]
Relationship = Literal["many_to_one", "one_to_many", "one_to_one"]
Granularity = Literal["day", "month", "year"]  # partition ranges
TimeGranularity = Literal["day", "week", "month", "quarter", "year"]  # query time buckets; weeks start on Monday

FANOUT_SAFE_AGGS = {"count_distinct", "min", "max"}
ADDITIVE_AGGS = {"count", "sum"}


class ModelError(ValueError):
    """Raised when an operation or query is not valid against the model.

    `code` is a stable, machine-readable reason that MCP tools return to agents.
    """

    def __init__(self, message: str, code: str = "invalid", hint: str | None = None):
        super().__init__(message)
        self.code, self.hint = code, hint


def check_ident(value: str, what: str) -> str:
    if not _IDENT_RE.match(value):
        raise ModelError(f"{what} {value!r} must be snake_case: lowercase letters, digits and _")
    return value


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Member(_Base):
    name: str
    sql: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    format: Optional[str] = None
    deprecated: Optional[str] = None  # reason; None means not deprecated
    replaced_by: Optional[str] = None
    grants: list[str] = Field(default_factory=list)  # roles; empty = inherit cube grants


class Measure(Member):
    kind: Literal["measure"] = "measure"
    agg: Agg = "sum"
    filter: Optional[str] = None  # row filter applied inside the aggregate


class Dimension(Member):
    kind: Literal["dimension"] = "dimension"
    type: DimType = "string"
    primary_key: bool = False


class Join(_Base):
    to: str
    relationship: Relationship = "many_to_one"
    on: str


class Segment(_Base):
    name: str
    sql: str
    title: Optional[str] = None
    description: Optional[str] = None


class PartitionSpec(_Base):
    """How a cube's physical data is partitioned.

    kind=range partitions by time (`granularity`); kind=list partitions by a key
    with a known set of `values` (for example a customer set or region).
    strategy=column means the warehouse partitions one table by `column` and we
    only add a pruning predicate. strategy=table means each partition is its own
    table named by `table_pattern` and we read only the matching tables.
    """

    column: str
    kind: Literal["range", "list"] = "range"
    granularity: Optional[Granularity] = None
    strategy: Literal["column", "table"] = "column"
    table_pattern: Optional[str] = None  # e.g. orders_{yyyy}_{mm} or orders_{value}
    start: Optional[str] = None  # first range partition, ISO date
    end: Optional[str] = None  # last range partition, ISO date; None = today
    values: list[str] = Field(default_factory=list)
    column_type: Literal["timestamp", "date"] = "timestamp"
    require_filter: bool = False


class Cube(_Base):
    name: str
    table: Optional[str] = None
    sql: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    grants: list[str] = Field(default_factory=list)
    measures: dict[str, Measure] = Field(default_factory=dict)
    dimensions: dict[str, Dimension] = Field(default_factory=dict)
    joins: dict[str, Join] = Field(default_factory=dict)
    segments: dict[str, Segment] = Field(default_factory=dict)
    aliases: dict[str, str] = Field(default_factory=dict)  # old member name -> current name
    partition: Optional[PartitionSpec] = None

    def member(self, name: str) -> Optional[Member]:
        return self.measures.get(name) or self.dimensions.get(name)

    def members(self) -> list[Member]:
        return [*self.dimensions.values(), *self.measures.values()]

    def header(self) -> dict[str, Any]:
        return self.model_dump(include={"name", "table", "sql", "title", "description", "grants"})


class SchemaBinding(_Base):
    """Routes a cube (or every cube, '*') to a warehouse schema for a tenant (or every tenant, '*').

    `schema` may contain `{tenant}`. `tenant_column` turns on pooled tenancy: the
    tables are shared and rows are filtered by that column instead.
    """

    cube: str = "*"
    tenant: str = "*"
    schema_: str = Field(alias="schema")
    tenant_column: Optional[str] = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @property
    def key(self) -> str:
        return f"{self.cube}@{self.tenant}"


class RowPolicy(_Base):
    """A filter tree ANDed into every query that reads `cube`.

    Values may reference the security context as "$ctx.<key>".
    """

    name: str
    cube: str
    filter: dict[str, Any]
    roles: list[str] = Field(default_factory=list)  # empty = applies to every role
    exempt_roles: list[str] = Field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.cube}.{self.name}"


class Model(_Base):
    cubes: dict[str, Cube] = Field(default_factory=dict)
    bindings: dict[str, SchemaBinding] = Field(default_factory=dict)
    policies: dict[str, RowPolicy] = Field(default_factory=dict)

    def cube(self, name: str) -> Cube:
        if name not in self.cubes:
            raise ModelError(f"unknown cube {name!r}")
        return self.cubes[name]

    def resolve(self, path: str) -> tuple[Cube, Member]:
        """Resolve `cube.member`, following rename aliases."""
        cube_name, _, member_name = path.partition(".")
        if not member_name:
            raise ModelError(f"member path {path!r} must look like cube.member")
        cube = self.cube(cube_name)
        seen = set()
        while member_name in cube.aliases and member_name not in seen:
            seen.add(member_name)
            member_name = cube.aliases[member_name]
        member = cube.member(member_name)
        if member is None:
            raise ModelError(f"unknown member {path!r}")
        return cube, member

    def canonical(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)

    def fingerprint(self) -> str:
        return _digest(self.canonical())

    def cube_fingerprint(self, names: set[str]) -> str:
        """Fingerprint of only the parts of the model that a query over `names` depends on."""
        part = {
            "cubes": {n: self.cubes[n].model_dump(mode="json") for n in sorted(names) if n in self.cubes},
            "bindings": {k: b.model_dump(mode="json", by_alias=True) for k, b in self.bindings.items() if b.cube in names or b.cube == "*"},
            "policies": {k: p.model_dump(mode="json") for k, p in self.policies.items() if p.cube in names},
        }
        return _digest(part)


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def humanize(name: str) -> str:
    return name.replace("_", " ").strip().capitalize()
