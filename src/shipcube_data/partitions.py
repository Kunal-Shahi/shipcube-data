"""Partition pruning.

The compiler hands us the conservative date bounds (and list values) implied by
the AND-ed filters on a cube's partition column. We return either the physical
tables to read (strategy=table) or a predicate the warehouse can prune on
(strategy=column).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from sqlglot import exp

from .model import ModelError, PartitionSpec


@dataclass
class Bounds:
    lo: Optional[date] = None  # inclusive
    hi: Optional[date] = None  # inclusive
    values: Optional[set[str]] = None  # list partitions

    def narrow(self, lo: Optional[date] = None, hi: Optional[date] = None, values: Optional[set[str]] = None) -> None:
        if lo and (self.lo is None or lo > self.lo):
            self.lo = lo
        if hi and (self.hi is None or hi < self.hi):
            self.hi = hi
        if values is not None:
            self.values = values if self.values is None else self.values & values

    @property
    def empty(self) -> bool:
        return (self.lo is not None and self.hi is not None and self.lo > self.hi) or self.values == set()

    @property
    def bounded(self) -> bool:
        return self.lo is not None or self.hi is not None or self.values is not None


@dataclass
class Pruned:
    tables: Optional[list[str]] = None  # strategy=table: tables to UNION ALL
    predicate: Optional[exp.Expression] = None  # strategy=column: extra WHERE
    total: int = 0  # partitions that exist
    scanned: Optional[int] = None  # partitions read (None = unknown, all)
    notes: list[str] = field(default_factory=list)


def to_date(value: str) -> date:
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        raise ModelError(f"not an ISO date: {value!r}") from None


def _step(d: date, granularity: str) -> date:
    if granularity == "day":
        return d + timedelta(days=1)
    if granularity == "month":
        return date(d.year + d.month // 12, d.month % 12 + 1, 1)
    return date(d.year + 1, 1, 1)


def _floor(d: date, granularity: str) -> date:
    return d if granularity == "day" else date(d.year, d.month, 1) if granularity == "month" else date(d.year, 1, 1)


def range_partitions(spec: PartitionSpec, today: Optional[date] = None) -> list[tuple[date, date]]:
    """Every range partition as [start, next_start)."""
    g = spec.granularity or "day"
    cur = _floor(to_date(spec.start), g)  # type: ignore[arg-type]
    last = _floor(to_date(spec.end) if spec.end else (today or date.today()), g)
    out = []
    while cur <= last:
        nxt = _step(cur, g)
        out.append((cur, nxt))
        cur = nxt
    return out


def table_name(pattern: str, start: Optional[date] = None, value: Optional[str] = None) -> str:
    name = pattern
    if start:
        name = name.replace("{yyyy}", f"{start.year:04d}").replace("{mm}", f"{start.month:02d}").replace("{dd}", f"{start.day:02d}")
    if value is not None:
        name = name.replace("{value}", value)
    return name


def prune(spec: PartitionSpec, column: exp.Expression, bounds: Bounds, cube: str) -> Pruned:
    if spec.require_filter and not bounds.bounded:
        raise ModelError(f"{cube} is partitioned by {spec.column}; filter on it (require_filter is set)")

    if spec.strategy == "column":
        if spec.kind == "list" or not bounds.bounded:
            return Pruned(notes=[] if bounds.bounded else [f"{cube}: no filter on partition column {spec.column}; full scan"])
        cast_to = "DATE" if spec.column_type == "date" else "TIMESTAMP"
        parts = []
        if bounds.lo:
            parts.append(column.copy() >= exp.cast(exp.Literal.string(bounds.lo.isoformat()), cast_to))
        if bounds.hi:
            parts.append(column.copy() < exp.cast(exp.Literal.string((bounds.hi + timedelta(days=1)).isoformat()), cast_to))
        return Pruned(predicate=exp.and_(*parts))

    # strategy == "table"
    if spec.kind == "list":
        keep = [v for v in spec.values if bounds.values is None or v in bounds.values]
        tables = [table_name(spec.table_pattern, value=v) for v in keep]  # type: ignore[arg-type]
        return Pruned(tables=tables, total=len(spec.values), scanned=len(tables))

    parts = range_partitions(spec)
    keep = [
        (s, e) for s, e in parts
        if (bounds.lo is None or e > bounds.lo) and (bounds.hi is None or s <= bounds.hi)
    ]
    notes = [] if bounds.bounded else [f"{cube}: no filter on partition column {spec.column}; reading all {len(parts)} partitions"]
    return Pruned(tables=[table_name(spec.table_pattern, start=s) for s, _ in keep], total=len(parts), scanned=len(keep), notes=notes)  # type: ignore[arg-type]
