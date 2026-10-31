"""The migration vocabulary.

Every operation is a small Pydantic model with as few required keys as
possible. `apply` mutates a copy of the model and raises ModelError when the
operation does not make sense; `touches` names what it changes, which is what
conflict detection compares.

Naming rule: you *name* new things (`name`) and *point at* existing ones
(`member`, `cube`). Member paths are always `cube.member`.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal, Optional, Union

from sqlglot import exp
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from .model import (
    Agg,
    Cube,
    Dimension,
    DimType,
    Granularity,
    Join,
    Measure,
    Model,
    ModelError,
    PartitionSpec,
    Relationship,
    RowPolicy,
    SchemaBinding,
    Segment,
    check_ident,
)
from .query import filter_members, parse_filter
from .sqlutil import has_aggregate, parse_expr, refs, row_reference

Kind = Literal["additive", "change", "breaking"]


class _Op(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Kind = "additive"

    def apply(self, model: Model, dialect: str) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def touches(self, model: Model) -> set[str]:  # pragma: no cover - abstract
        raise NotImplementedError

    def classify(self) -> Kind:
        return type(self).model_fields["kind"].default


def _split(path: str, what: str = "member") -> tuple[str, str]:
    cube, _, name = path.partition(".")
    if not name:
        raise ModelError(f"{what} {path!r} must look like cube.{what}")
    return cube, name


def _member_or_raise(model: Model, path: str):
    cube_name, name = _split(path)
    cube = model.cube(cube_name)
    member = cube.member(name)
    if member is None:
        target = cube.aliases.get(name)
        hint = f" (it was renamed to {cube_name}.{target})" if target else ""
        raise ModelError(f"unknown member {path!r}{hint}")
    return cube, member


def _check_refs(model: Model, cube: Cube, sql: str | None, *, measures_ok: bool) -> None:
    for ref in refs(sql):
        cube_name, _, name = ref.rpartition(".")
        target = model.cube(cube_name) if cube_name else cube
        if cube_name and cube_name != cube.name:
            raise ModelError(f"{{{ref}}}: members may only reference members of their own cube")
        member = target.member(name)
        if member is None:
            raise ModelError(f"{{{ref}}} does not name a member of {target.name}")
        if member.kind == "measure" and not measures_ok:
            raise ModelError(f"{{{ref}}} is a measure; only measures may reference measures")


def _no_cycle(cube: Cube, start: str) -> None:
    def visit(name: str, path: list[str]) -> None:
        member = cube.member(name)
        for ref in refs(member.sql if member else None):
            nxt = ref.rpartition(".")[2]
            if nxt in path:
                raise ModelError(f"reference cycle: {' -> '.join(path + [nxt])}")
            visit(nxt, path + [nxt])

    visit(start, [start])


def dependents(cube: Cube, name: str, model: Model) -> list[str]:
    """Everything in the model that references member `cube.name`."""
    out = []
    for m in cube.members():
        if name in {r.rpartition(".")[2] for r in refs(m.sql)} and m.name != name:
            out.append(f"{cube.name}.{m.name}")
    for s in cube.segments.values():
        if name in {r.rpartition(".")[2] for r in refs(s.sql)}:
            out.append(f"segment {cube.name}.{s.name}")
    for p in model.policies.values():
        if f"{cube.name}.{name}" in filter_members(parse_filter(p.filter)):
            out.append(f"policy {p.key}")
    return out


# ---------------------------------------------------------------- inference

def infer_agg(name: str, sql: str | None, dialect: str) -> Agg:
    if sql is None:
        return "count"
    if refs(sql) or has_aggregate(parse_expr(sql, dialect)):
        return "number"
    n = name.lower()
    if n.startswith(("unique_", "distinct_")) or n.endswith("_distinct"):
        return "count_distinct"
    if n.startswith("count_") or n.endswith("_count") or n == "count":
        return "count"
    if n.startswith(("avg_", "average_", "mean_")) or n.endswith("_avg"):
        return "avg"
    if n.startswith("min_") or n.endswith("_min"):
        return "min"
    if n.startswith("max_") or n.endswith("_max"):
        return "max"
    return "sum"


def infer_type(name: str) -> DimType:
    n = name.lower()
    if n.endswith(("_at", "_date", "_time", "_timestamp", "_day", "_month")) or n in {"date", "timestamp"}:
        return "time"
    if n.startswith(("is_", "has_")):
        return "boolean"
    if n.endswith(("_amount", "_price", "_value", "_qty", "_quantity", "_weight", "_score")):
        return "number"
    return "string"


# ---------------------------------------------------------------- cubes

class MemberSpec(BaseModel):
    """Inline member definition inside create_cube: a SQL string or this object."""

    model_config = ConfigDict(extra="forbid")
    sql: Optional[str] = None
    agg: Optional[Agg] = None
    type: Optional[DimType] = None
    filter: Optional[str] = None
    primary_key: bool = False
    title: Optional[str] = None
    description: Optional[str] = None
    format: Optional[str] = None


class CreateCube(_Op):
    op: Literal["create_cube"]
    name: str
    table: Optional[str] = None
    sql: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    dimensions: dict[str, Union[str, MemberSpec]] = Field(default_factory=dict)
    measures: dict[str, Union[str, MemberSpec]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _source(self) -> "CreateCube":
        if bool(self.table) == bool(self.sql):
            raise ValueError("create_cube needs exactly one of table or sql")
        return self

    def _inline(self) -> list[_Op]:
        ops: list[_Op] = []
        for kind, specs in (("add_dimension", self.dimensions), ("add_measure", self.measures)):
            for name, spec in specs.items():
                body = {"sql": spec} if isinstance(spec, str) else spec.model_dump(exclude_none=True)
                if kind == "add_dimension":
                    body.pop("agg", None), body.pop("filter", None)
                else:
                    body.pop("type", None), body.pop("primary_key", None)
                ops.append(OP_ADAPTER.validate_python({"op": kind, "name": f"{self.name}.{name}", **body}))
        return ops

    def apply(self, model: Model, dialect: str) -> None:
        check_ident(self.name, "cube name")
        if self.name in model.cubes:
            raise ModelError(f"cube {self.name!r} already exists")
        if self.sql:
            from .sqlutil import forbidden_function, parse_query

            tree = parse_query(self.sql, dialect)
            if not isinstance(tree, exp.Query):
                raise ModelError("a cube's sql must be a single SELECT")
            for node in tree.walk():
                bad = forbidden_function(node)
                if bad:
                    raise ModelError(f"cube SQL may not call {bad}", "invalid_sql")
        cube = Cube(name=self.name, table=self.table, sql=self.sql, title=self.title, description=self.description)
        cube.measures["count"] = Measure(name="count", agg="count")
        model.cubes[self.name] = cube
        for op in self._inline():
            if op.name == f"{self.name}.count":  # type: ignore[attr-defined]
                del cube.measures["count"]
            op.apply(model, dialect)

    def touches(self, model: Model) -> set[str]:
        return {f"cube:{self.name}"}


class DropCube(_Op):
    op: Literal["drop_cube"]
    cube: str
    kind: Kind = "breaking"

    def apply(self, model: Model, dialect: str) -> None:
        model.cube(self.cube)
        for other in model.cubes.values():
            if self.cube in other.joins and other.name != self.cube:
                raise ModelError(f"cube {other.name} joins to {self.cube}; drop that join first")
        del model.cubes[self.cube]
        for key in [k for k, p in model.policies.items() if p.cube == self.cube]:
            del model.policies[key]
        for key in [k for k, b in model.bindings.items() if b.cube == self.cube]:
            del model.bindings[key]

    def touches(self, model: Model) -> set[str]:
        return {f"cube:{self.cube}"}


# ---------------------------------------------------------------- members

class AddMeasure(_Op):
    op: Literal["add_measure"]
    name: str  # cube.member
    sql: Optional[str] = None
    agg: Optional[Agg] = None
    filter: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    format: Optional[str] = None

    def apply(self, model: Model, dialect: str) -> None:
        cube_name, name = _split(self.name)
        cube = model.cube(cube_name)
        check_ident(name, "measure name")
        if cube.member(name) or name in cube.aliases:
            raise ModelError(f"{self.name} already exists")
        agg = self.agg or infer_agg(name, self.sql, dialect)
        if self.sql:
            parse_expr(self.sql, dialect)
            _check_refs(model, cube, self.sql, measures_ok=agg == "number")
        elif agg not in {"count"}:
            raise ModelError(f"{self.name}: agg {agg} needs sql")
        if agg == "number" and not self.sql:
            raise ModelError(f"{self.name}: a number measure needs sql")
        if self.filter:
            parse_expr(self.filter, dialect)
            _check_refs(model, cube, self.filter, measures_ok=False)
        cube.measures[name] = Measure(
            name=name, sql=self.sql, agg=agg, filter=self.filter,
            title=self.title, description=self.description, format=self.format,
        )
        _no_cycle(cube, name)

    def touches(self, model: Model) -> set[str]:
        return {f"member:{self.name}"}


class AddDimension(_Op):
    op: Literal["add_dimension"]
    name: str  # cube.member
    sql: str
    type: Optional[DimType] = None
    primary_key: bool = False
    title: Optional[str] = None
    description: Optional[str] = None
    format: Optional[str] = None

    def apply(self, model: Model, dialect: str) -> None:
        cube_name, name = _split(self.name)
        cube = model.cube(cube_name)
        check_ident(name, "dimension name")
        if cube.member(name) or name in cube.aliases:
            raise ModelError(f"{self.name} already exists")
        expr = parse_expr(self.sql, dialect)
        if has_aggregate(expr):
            raise ModelError(f"{self.name}: a dimension cannot aggregate; use add_measure")
        _check_refs(model, cube, self.sql, measures_ok=False)
        cube.dimensions[name] = Dimension(
            name=name, sql=self.sql, type=self.type or infer_type(name), primary_key=self.primary_key,
            title=self.title, description=self.description, format=self.format,
        )
        _no_cycle(cube, name)

    def touches(self, model: Model) -> set[str]:
        return {f"member:{self.name}"}


class RenameMember(_Op):
    """Rename a member. The old name stays as an alias, so existing queries keep working."""

    op: Literal["rename_member"]
    member: str
    to: str
    kind: Kind = "change"

    def apply(self, model: Model, dialect: str) -> None:
        cube, member = _member_or_raise(model, self.member)
        check_ident(self.to, "member name")
        if cube.member(self.to):
            raise ModelError(f"{cube.name}.{self.to} already exists")
        old = member.name
        coll = cube.measures if member.kind == "measure" else cube.dimensions
        del coll[old]
        coll[self.to] = member.model_copy(update={"name": self.to})
        cube.aliases.pop(self.to, None)
        cube.aliases[old] = self.to
        for alias, target in list(cube.aliases.items()):
            if target == old:
                cube.aliases[alias] = self.to
        _rewrite_refs(model, cube, old, self.to)

    def touches(self, model: Model) -> set[str]:
        cube, _ = _split(self.member)
        return {f"member:{self.member}", f"member:{cube}.{self.to}"}


def _rewrite_refs(model: Model, cube: Cube, old: str, new: str) -> None:
    def sub(sql: str | None) -> str | None:
        if sql is None:
            return None
        return sql.replace(f"{{{old}}}", f"{{{new}}}").replace(f"{{{cube.name}.{old}}}", f"{{{cube.name}.{new}}}")

    for m in cube.members():
        m.sql = sub(m.sql)
        if isinstance(m, Measure):
            m.filter = sub(m.filter)
    for s in cube.segments.values():
        s.sql = sub(s.sql) or s.sql
    for p in model.policies.values():
        p.filter = json.loads(json.dumps(p.filter).replace(f'"{cube.name}.{old}"', f'"{cube.name}.{new}"'))


class DeprecateMember(_Op):
    op: Literal["deprecate_member"]
    member: str
    reason: str = "deprecated"
    replaced_by: Optional[str] = None
    kind: Kind = "change"

    def apply(self, model: Model, dialect: str) -> None:
        _, member = _member_or_raise(model, self.member)
        if self.replaced_by:
            model.resolve(self.replaced_by)
        member.deprecated = self.reason
        member.replaced_by = self.replaced_by

    def touches(self, model: Model) -> set[str]:
        return {f"member:{self.member}"}


class ChangeSql(_Op):
    """Change a member's definition. Old versions stay in the catalog for pinned queries."""

    op: Literal["change_sql"]
    member: str
    sql: str
    agg: Optional[Agg] = None
    filter: Optional[str] = None
    # Queries pinned to an older version get this new definition too, unless the approver marks the
    # change as a restatement that's safe to undo under a pin. Fails closed: a forgotten flag can't
    # leave masked or redacted SQL reachable through an old pin (shipcube-data #8).
    pin_compatible: bool = False
    kind: Kind = "change"

    def apply(self, model: Model, dialect: str) -> None:
        cube, member = _member_or_raise(model, self.member)
        expr = parse_expr(self.sql, dialect)
        if isinstance(member, Measure):
            if self.agg:
                agg = self.agg
            elif refs(self.sql) or has_aggregate(expr):
                agg = "number"  # the new SQL aggregates by itself
            else:
                agg = member.agg if member.agg != "number" else infer_agg(member.name, self.sql, dialect)
            _check_refs(model, cube, self.sql, measures_ok=agg == "number")
            member.sql, member.agg = self.sql, agg
            if self.filter is not None:
                member.filter = self.filter or None
        else:
            if has_aggregate(expr):
                raise ModelError(f"{self.member}: a dimension cannot aggregate")
            _check_refs(model, cube, self.sql, measures_ok=False)
            member.sql = self.sql
        _no_cycle(cube, member.name)

    def touches(self, model: Model) -> set[str]:
        return {f"member:{self.member}"}


class DropMember(_Op):
    op: Literal["drop_member"]
    member: str
    kind: Kind = "breaking"

    def apply(self, model: Model, dialect: str) -> None:
        cube, member = _member_or_raise(model, self.member)
        deps = dependents(cube, member.name, model)
        if deps:
            raise ModelError(f"cannot drop {self.member}: used by {', '.join(deps)}")
        (cube.measures if member.kind == "measure" else cube.dimensions).pop(member.name)
        for alias, target in list(cube.aliases.items()):
            if target == member.name:
                del cube.aliases[alias]

    def touches(self, model: Model) -> set[str]:
        return {f"member:{self.member}"}


class SetMeta(_Op):
    """Update the descriptive metadata that LLM agents read. Never changes results."""

    op: Literal["set_meta"]
    target: str  # cube or cube.member
    title: Optional[str] = None
    description: Optional[str] = None
    format: Optional[str] = None
    kind: Kind = "additive"

    def apply(self, model: Model, dialect: str) -> None:
        if "." in self.target:
            _, obj = _member_or_raise(model, self.target)
        else:
            obj = model.cube(self.target)
            if self.format is not None:
                raise ModelError("format applies to members, not cubes")
        for field in ("title", "description", "format"):
            value = getattr(self, field)
            if value is not None:
                setattr(obj, field, value or None)

    def touches(self, model: Model) -> set[str]:
        return {f"meta:{self.target}"}


# ---------------------------------------------------------------- joins & segments

class AddJoin(_Op):
    op: Literal["add_join"]
    cube: str
    to: str
    on: Optional[str] = None  # SQL using cube names as qualifiers
    key: Optional[str] = None  # shorthand: cube.key = to.key
    relationship: Relationship = "many_to_one"

    @model_validator(mode="after")
    def _cond(self) -> "AddJoin":
        if bool(self.on) == bool(self.key):
            raise ValueError("add_join needs exactly one of on or key")
        return self

    def apply(self, model: Model, dialect: str) -> None:
        cube, target = model.cube(self.cube), model.cube(self.to)
        if self.to in cube.joins or self.cube in target.joins:
            raise ModelError(f"{self.cube} and {self.to} are already joined")
        on = self.on or f"{self.cube}.{self.key} = {self.to}.{self.key}"
        expr = parse_expr(on, dialect)
        from sqlglot import exp

        for col in expr.find_all(exp.Column):
            if col.table not in {self.cube, self.to}:
                raise ModelError(f"join condition column {col.sql()} must be qualified with {self.cube} or {self.to}")
        cube.joins[self.to] = Join(to=self.to, relationship=self.relationship, on=on)

    def touches(self, model: Model) -> set[str]:
        a, b = sorted([self.cube, self.to])
        return {f"join:{a}~{b}"}


class DropJoin(_Op):
    op: Literal["drop_join"]
    cube: str
    to: str
    kind: Kind = "breaking"

    def apply(self, model: Model, dialect: str) -> None:
        cube = model.cube(self.cube)
        if self.to not in cube.joins:
            raise ModelError(f"{self.cube} has no join to {self.to}")
        del cube.joins[self.to]

    def touches(self, model: Model) -> set[str]:
        a, b = sorted([self.cube, self.to])
        return {f"join:{a}~{b}"}


class AddSegment(_Op):
    op: Literal["add_segment"]
    name: str  # cube.segment
    sql: str
    title: Optional[str] = None
    description: Optional[str] = None

    def apply(self, model: Model, dialect: str) -> None:
        cube_name, name = _split(self.name, "segment")
        cube = model.cube(cube_name)
        check_ident(name, "segment name")
        if name in cube.segments:
            raise ModelError(f"segment {self.name} already exists")
        parse_expr(self.sql, dialect)
        bad = row_reference(self.sql, dialect, set(model.cubes))
        if bad:
            raise ModelError(f"segment SQL may only read {cube_name}'s own columns by name, not {bad!r} "
                             "(no qualified columns, stars or whole-row references)", "invalid_sql")
        _check_refs(model, cube, self.sql, measures_ok=False)
        cube.segments[name] = Segment(name=name, sql=self.sql, title=self.title, description=self.description)

    def touches(self, model: Model) -> set[str]:
        return {f"segment:{self.name}"}


# ---------------------------------------------------------------- physical layout

class BindSchema(_Op):
    op: Literal["bind_schema"]
    schema_: str = Field(alias="schema")
    cube: str = "*"
    tenant: str = "*"
    tenant_column: Optional[str] = None
    shared: bool = False  # reference data every tenant reads alike; required when the binding isn't tenant-isolated
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    def apply(self, model: Model, dialect: str) -> None:
        if self.cube != "*":
            model.cube(self.cube)
        binding = SchemaBinding(cube=self.cube, tenant=self.tenant, schema=self.schema_, tenant_column=self.tenant_column,
                                shared=self.shared)
        if "{tenant}" in self.schema_ and self.tenant != "*":
            raise ModelError("a {tenant} schema template only makes sense with tenant '*'")
        if binding.shared and binding.isolated:
            binding.check_tenancy()
        # An unmarked shared binding is allowed here (migration history has them) but refused by every query
        # that reads it (Compiler.schema_for), until it's re-bound tenant-isolated or `shared: true`.
        model.bindings[binding.key] = binding  # rebinding replaces

    def touches(self, model: Model) -> set[str]:
        return {f"binding:{self.cube}@{self.tenant}"}

    def classify(self) -> Kind:
        return "change"


class DefinePartition(_Op):
    op: Literal["define_partition"]
    cube: str
    column: Optional[str] = None
    dimension: Optional[str] = None  # shorthand: use this dimension's column
    granularity: Optional[Granularity] = None
    values: list[str] = Field(default_factory=list)
    table_pattern: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None
    column_type: Literal["timestamp", "date"] = "timestamp"
    require_filter: bool = False

    def apply(self, model: Model, dialect: str) -> None:
        cube = model.cube(self.cube)
        column = self.column
        if self.dimension:
            dim = cube.dimensions.get(self.dimension)
            if dim is None:
                raise ModelError(f"unknown dimension {self.cube}.{self.dimension}")
            column = column or dim.sql
        if not column:
            raise ModelError("define_partition needs column or dimension")
        kind = "list" if self.values else "range"
        granularity = self.granularity
        if kind == "range" and granularity is None:
            granularity = "month" if self.table_pattern and "{mm}" in self.table_pattern else "day"
        strategy = "table" if self.table_pattern else "column"
        if strategy == "table":
            if kind == "range" and not self.start:
                raise ModelError("table-per-partition ranges need start")
            if kind == "list" and "{value}" not in (self.table_pattern or ""):
                raise ModelError("list table_pattern needs {value}")
            if not cube.table:
                raise ModelError("table-per-partition needs a table cube")
        cube.partition = PartitionSpec(
            column=column, kind=kind, granularity=granularity if kind == "range" else None,
            strategy=strategy, table_pattern=self.table_pattern, start=self.start, end=self.end,
            values=self.values, column_type=self.column_type, require_filter=self.require_filter,
        )

    def touches(self, model: Model) -> set[str]:
        return {f"partition:{self.cube}"}

    def classify(self) -> Kind:
        return "change"


# ---------------------------------------------------------------- access

class GrantMember(_Op):
    """Restrict a member (or every member of a cube, `cube.*`) to these roles."""

    op: Literal["grant_member"]
    member: str
    roles: list[str]
    kind: Kind = "breaking"  # it narrows who can see the member

    def apply(self, model: Model, dialect: str) -> None:
        cube_name, name = _split(self.member)
        target = model.cube(cube_name) if name == "*" else _member_or_raise(model, self.member)[1]
        target.grants = sorted(set(target.grants) | set(self.roles))

    def touches(self, model: Model) -> set[str]:
        return {f"grant:{self.member}"}


class RevokeMember(_Op):
    op: Literal["revoke_member"]
    member: str
    roles: list[str]
    kind: Kind = "breaking"

    def apply(self, model: Model, dialect: str) -> None:
        cube_name, name = _split(self.member)
        target = model.cube(cube_name) if name == "*" else _member_or_raise(model, self.member)[1]
        target.grants = sorted(set(target.grants) - set(self.roles))

    def touches(self, model: Model) -> set[str]:
        return {f"grant:{self.member}"}


class AddRowPolicy(_Op):
    op: Literal["add_row_policy"]
    name: str  # cube.policy
    filter: dict[str, Any]
    roles: list[str] = Field(default_factory=list)
    exempt_roles: list[str] = Field(default_factory=list)
    kind: Kind = "breaking"

    def apply(self, model: Model, dialect: str) -> None:
        cube_name, name = _split(self.name, "policy")
        model.cube(cube_name)
        check_ident(name, "policy name")
        policy = RowPolicy(name=name, cube=cube_name, filter=self.filter, roles=self.roles, exempt_roles=self.exempt_roles)
        if policy.key in model.policies:
            raise ModelError(f"policy {policy.key} already exists")
        for path in filter_members(parse_filter(self.filter)):
            _, member = model.resolve(path)
            if member.kind != "dimension":
                raise ModelError(f"policy filters must use dimensions, not {path}")
        model.policies[policy.key] = policy

    def touches(self, model: Model) -> set[str]:
        return {f"policy:{self.name}"}


class DropRowPolicy(_Op):
    op: Literal["drop_row_policy"]
    policy: str  # cube.policy
    kind: Kind = "change"

    def apply(self, model: Model, dialect: str) -> None:
        if self.policy not in model.policies:
            raise ModelError(f"unknown policy {self.policy}")
        del model.policies[self.policy]

    def touches(self, model: Model) -> set[str]:
        return {f"policy:{self.policy}"}


OP_TYPES = (
    CreateCube, DropCube,
    AddMeasure, AddDimension, RenameMember, DeprecateMember, ChangeSql, DropMember, SetMeta,
    AddJoin, DropJoin, AddSegment,
    BindSchema, DefinePartition,
    GrantMember, RevokeMember, AddRowPolicy, DropRowPolicy,
)
Op = Annotated[Union[OP_TYPES], Field(discriminator="op")]  # type: ignore[valid-type]
OP_ADAPTER: TypeAdapter[Op] = TypeAdapter(Op)
OP_NAMES = [t.model_fields["op"].annotation.__args__[0] for t in OP_TYPES]


class Migration(BaseModel):
    """A migration file: an id, a description and a list of ops."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[0-9]{4,}_[a-z0-9_]+$")
    description: str = ""
    base_version: Optional[int] = None  # model version this was written against
    ops: list[Op] = Field(min_length=1)

    def checksum(self) -> str:
        body = {"id": self.id, "ops": [op.model_dump(mode="json", by_alias=True, exclude_defaults=True) for op in self.ops]}
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]

    def kind(self) -> Kind:
        kinds = {op.classify() for op in self.ops}
        return "breaking" if "breaking" in kinds else "change" if "change" in kinds else "additive"

    def widens_exposure(self) -> list[str]:
        """Ops that can make data visible that was not visible before (new members, cubes, joins,
        re-routing, removed restrictions). Approving these needs an admin (red-team issue #6)."""
        return [op.op for op in self.ops if op.op in WIDENS_EXPOSURE]


WIDENS_EXPOSURE = {
    "create_cube", "add_measure", "add_dimension", "add_join", "change_sql", "bind_schema",
    "revoke_member", "drop_row_policy",
    "add_segment",  # a filter on raw columns: a yes/no oracle over data no member exposes (shipcube-data #8)
}


def apply_ops(model: Model, ops: list[Op], dialect: str) -> tuple[Model, set[str]]:
    """Apply ops to a copy of `model`. Returns the new model and everything touched."""
    new = model.model_copy(deep=True)
    touched: set[str] = set()
    for i, op in enumerate(ops):
        touched |= op.touches(new)
        try:
            op.apply(new, dialect)
        except ModelError as e:
            raise ModelError(f"op {i + 1} ({op.op}): {e}", "invalid_op", e.hint) from None
    return new, touched


def conflicts(a: set[str], b: set[str]) -> set[str]:
    """Touch keys that overlap. A cube-level key overlaps everything in that cube."""

    def scope(key: str) -> str:
        kind, _, rest = key.partition(":")
        return rest.split(".")[0].split("@")[0].split("~")[0] if kind in {"cube"} else ""

    out = a & b
    cube_a = {scope(k) for k in a} - {""}
    cube_b = {scope(k) for k in b} - {""}
    for key in b:
        if any(key.partition(":")[2].startswith((c + ".", c + "@")) or key == f"cube:{c}" for c in cube_a):
            out.add(key)
    for key in a:
        if any(key.partition(":")[2].startswith((c + ".", c + "@")) or key == f"cube:{c}" for c in cube_b):
            out.add(key)
    return out
