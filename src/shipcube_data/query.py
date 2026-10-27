"""Typed query language: L0 (members, filters, time, order) and L1 (window operators)."""

from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .model import Granularity

Scalar = Union[str, int, float, bool]

FilterOp = Literal[
    "equals", "not_equals",
    "contains", "not_contains", "starts_with", "ends_with",
    "gt", "gte", "lt", "lte",
    "set", "not_set",
    "in_date_range", "before_date", "after_date",
]
NO_VALUE_OPS = {"set", "not_set"}
SINGLE_VALUE_OPS = {"gt", "gte", "lt", "lte", "before_date", "after_date"}


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Condition(_Base):
    member: str
    op: FilterOp
    values: list[Scalar] = Field(default_factory=list)

    @model_validator(mode="after")
    def _arity(self) -> "Condition":
        n = len(self.values)
        if self.op in NO_VALUE_OPS and n:
            raise ValueError(f"{self.op} takes no values")
        if self.op in SINGLE_VALUE_OPS and n != 1:
            raise ValueError(f"{self.op} takes exactly one value")
        if self.op == "in_date_range" and n != 2:
            raise ValueError("in_date_range takes [from, to]")
        if self.op not in NO_VALUE_OPS and n == 0:
            raise ValueError(f"{self.op} needs at least one value")
        return self


class And(_Base):
    and_: list["Filter"] = Field(alias="and")
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Or(_Base):
    or_: list["Filter"] = Field(alias="or")
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Not(_Base):
    not_: "Filter" = Field(alias="not")
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


Filter = Union[Condition, And, Or, Not]
And.model_rebuild()
Or.model_rebuild()
Not.model_rebuild()


def parse_filter(raw: dict[str, Any]) -> Filter:
    if "and" in raw:
        return And.model_validate(raw)
    if "or" in raw:
        return Or.model_validate(raw)
    if "not" in raw:
        return Not.model_validate(raw)
    return Condition.model_validate(raw)


def filter_members(f: Filter) -> list[str]:
    if isinstance(f, Condition):
        return [f.member]
    if isinstance(f, Not):
        return filter_members(f.not_)
    return [m for child in (f.and_ if isinstance(f, And) else f.or_) for m in filter_members(child)]


class TimeSpec(_Base):
    dimension: str
    granularity: Optional[Granularity] = None
    range: Optional[tuple[str, str]] = None  # inclusive ISO dates or datetimes


class Order(_Base):
    member: str  # a member path, or a calc name
    dir: Literal["asc", "desc"] = "asc"


class Calc(_Base):
    """L1 operator, compiled to a window function (or a period self-join for compare)."""

    name: str
    op: Literal["share", "rank", "lag", "running", "compare"]
    measure: str
    by: list[str] = Field(default_factory=list)  # dimensions to partition by
    offset: int = 1  # lag / compare: periods back
    mode: Literal["pct", "diff", "prior"] = "pct"  # compare only


class Query(_Base):
    measures: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    time: Optional[TimeSpec] = None
    filters: list[Filter] = Field(default_factory=list)
    segments: list[str] = Field(default_factory=list)
    calcs: list[Calc] = Field(default_factory=list)
    order: list[Order] = Field(default_factory=list)
    limit: Optional[int] = Field(default=None, ge=1, le=100_000)
    offset: Optional[int] = Field(default=None, ge=0)
    model_version: Optional[int] = None  # pin to an older model version

    @model_validator(mode="before")
    @classmethod
    def _filters(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("filters"):
            data = {**data, "filters": [parse_filter(f) if isinstance(f, dict) else f for f in data["filters"]]}
        return data

    @model_validator(mode="after")
    def _non_empty(self) -> "Query":
        if not (self.measures or self.dimensions or self.time):
            raise ValueError("a query needs at least one measure, dimension or time dimension")
        return self
