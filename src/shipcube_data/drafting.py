"""Drafting migrations from plain English or app code.

`data_propose_migration` accepts explicit `ops`, or a `request` and/or `code` for the layer to
draft from. Drafting tries Muse Spark first (through a client ShipCube's AI gateway supplies)
and falls back to deterministic rules. Either way the draft is only a proposal: it is validated,
dry-run and stored for a human to approve, exactly like hand-written ops.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

import sqlglot
from sqlglot import exp

from .model import Model, ModelError
from .obs import log_event
from .ops import OP_ADAPTER, OP_NAMES, infer_agg

# (system prompt, user prompt) -> model text. ShipCube implements this over its AI gateway.
MuseClient = Callable[[str, str], str]


@dataclass
class Draft:
    ops: list[dict[str, Any]]
    description: str
    explained_by: str  # "muse-spark" | "rules"
    notes: list[str] = field(default_factory=list)


class DraftProvider(Protocol):
    def draft(self, model: Model, dialect: str, request: str, code: str) -> Draft: ...


# ---------------------------------------------------------------- deterministic rules

_AGG_WORDS = {
    "sum": "sum", "total": "sum", "count distinct": "count_distinct", "distinct count": "count_distinct",
    "unique count": "count_distinct", "count": "count", "average": "avg", "avg": "avg", "mean": "avg",
    "min": "min", "minimum": "min", "max": "max", "maximum": "max",
}
_AGG_RE = "|".join(sorted(map(re.escape, _AGG_WORDS), key=len, reverse=True))
_SENTENCES = [
    ("measure", re.compile(
        rf"(?:add|create)\s+(?:a\s+)?(?:measure|metric)\s+(?P<name>[a-z][a-z0-9_]*)\s+(?:(?:as|=|:|for|that is|which is)\s+)?"
        rf"(?:the\s+)?(?P<agg>{_AGG_RE})(?:\s+of)?\s*(?P<col>[a-z][a-z0-9_]*)?\s+(?:on|in|to|for)\s+(?:the\s+)?(?P<cube>[a-z][a-z0-9_]*)", re.I)),
    ("dimension", re.compile(
        r"(?:add|create)\s+(?:a\s+)?dimension\s+(?P<name>[a-z][a-z0-9_]*)\s+(?:(?:as|=|:|from)\s+)?(?:column\s+)?(?P<col>[a-z][a-z0-9_]*)"
        r"\s+(?:on|in|to|for)\s+(?:the\s+)?(?P<cube>[a-z][a-z0-9_]*)", re.I)),
    ("dimension", re.compile(
        r"(?:add|create)\s+(?:a\s+)?dimension\s+(?P<name>[a-z][a-z0-9_]*)\s+(?:on|in|to|for)\s+(?:the\s+)?(?P<cube>[a-z][a-z0-9_]*)"
        r"\s+(?:as|=|from)\s+(?:column\s+)?(?P<col>[a-z][a-z0-9_]*)", re.I)),
    ("rename", re.compile(r"rename\s+(?P<member>[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*)\s+to\s+(?P<to>[a-z][a-z0-9_]*)", re.I)),
    ("deprecate", re.compile(r"deprecate\s+(?P<member>[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*)(?:[,;:]?\s*(?:because|reason:?)\s+(?P<reason>[^.]+))?", re.I)),
]
_SQL_IN_CODE = re.compile(r"(\"\"\"|'''|\"|'|`)(\s*(?:WITH|SELECT)\b.*?)\1", re.I | re.S)
_AGG_CLASSES = {exp.Sum: "sum", exp.Avg: "avg", exp.Min: "min", exp.Max: "max"}


class RuleDrafter:
    """Deterministic drafting: a small English grammar, plus lifting aggregates out of SQL found in code."""

    def draft(self, model: Model, dialect: str, request: str, code: str) -> Draft:
        ops: list[dict[str, Any]] = []
        notes: list[str] = []
        for clause in re.split(r"\.(?=\s|$)|[;\n]|\band then\b", request or ""):
            ops += self._sentence(clause.strip(), dialect, notes)
        if code:
            ops += self._from_code(model, dialect, code, notes)
        if not ops:
            raise ModelError(
                "could not draft a migration from that request", "cannot_draft",
                "say e.g. 'add measure unique_customers as count distinct of customer_id on orders', "
                "or pass explicit ops",
            )
        return Draft(ops=ops, description=(request or "Model members lifted from app SQL").strip()[:200],
                     explained_by="rules", notes=notes)

    def _sentence(self, text: str, dialect: str, notes: list[str]) -> list[dict[str, Any]]:
        for kind, rx in _SENTENCES:
            m = rx.search(text)
            if not m:
                continue
            g = {k: (v.lower() if v and k not in {"reason"} else v) for k, v in m.groupdict().items()}
            if kind == "measure":
                agg = _AGG_WORDS[g["agg"]]
                op = {"op": "add_measure", "name": f"{g['cube']}.{g['name']}", "agg": agg}
                if g["col"]:
                    op["sql"] = g["col"]
                elif agg != "count":
                    notes.append(f"{text!r}: no column given for {agg}")
                    return []
                return [op]
            if kind == "dimension":
                return [{"op": "add_dimension", "name": f"{g['cube']}.{g['name']}", "sql": g["col"]}]
            if kind == "rename":
                return [{"op": "rename_member", "member": g["member"], "to": g["to"]}]
            return [{"op": "deprecate_member", "member": g["member"], **({"reason": g["reason"].strip()} if g.get("reason") else {})}]
        return []

    def _from_code(self, model: Model, dialect: str, code: str, notes: list[str]) -> list[dict[str, Any]]:
        by_table = {c.table.rpartition(".")[2]: c for c in model.cubes.values() if c.table}
        ops: list[dict[str, Any]] = []
        seen: set[str] = set()
        for _, sql in _SQL_IN_CODE.findall(code):
            try:
                tree = sqlglot.parse_one(sql, read=dialect)
            except sqlglot.errors.ParseError:
                notes.append("skipped a SQL string that does not parse")
                continue
            for select in tree.find_all(exp.Select):
                src = select.args.get("from_") or select.args.get("from")
                table = src.find(exp.Table) if src else None
                if table is None or select.args.get("joins"):
                    continue
                cube = by_table.get(table.name)
                if cube is None:
                    notes.append(f"table {table.name} has no cube yet; create_cube first")
                    continue
                for proj in select.expressions:
                    alias = proj.alias_or_name
                    inner = proj.this if isinstance(proj, exp.Alias) else proj
                    if isinstance(inner, exp.Count) and isinstance(inner.this, exp.Distinct):
                        agg, arg = "count_distinct", inner.this.expressions[0]
                    elif isinstance(inner, exp.Count):
                        agg, arg = "count", None if isinstance(inner.this, exp.Star) else inner.this
                    elif type(inner) in _AGG_CLASSES:
                        agg, arg = _AGG_CLASSES[type(inner)], inner.this
                    else:
                        continue
                    sql_text = arg.transform(_unqualify).sql(dialect=dialect) if arg is not None else None
                    existing = [m for m in cube.measures.values() if m.agg == agg and (m.sql or None) == sql_text and not m.filter]
                    if existing:
                        notes.append(f"{alias}: already modelled as {cube.name}.{existing[0].name}; query that instead")
                        continue
                    name = re.sub(r"[^a-z0-9_]+", "_", (alias or f"{agg}_{sql_text}").lower()).strip("_")
                    if not name or not name[0].isalpha() or name in cube.measures or name in cube.dimensions or f"{cube.name}.{name}" in seen:
                        name = f"{agg}_{re.sub(r'[^a-z0-9_]+', '_', (sql_text or 'rows').lower()).strip('_')}"
                    if f"{cube.name}.{name}" in seen or cube.member(name):
                        continue
                    seen.add(f"{cube.name}.{name}")
                    op: dict[str, Any] = {"op": "add_measure", "name": f"{cube.name}.{name}"}
                    if sql_text:
                        op["sql"] = sql_text
                    if infer_agg(name, sql_text, dialect) != agg:
                        op["agg"] = agg
                    ops.append(op)
        return ops


def _unqualify(node: exp.Expression) -> exp.Expression:
    if isinstance(node, exp.Column) and node.table:
        return exp.column(node.name)
    return node


# ---------------------------------------------------------------- Muse Spark

SYSTEM_PROMPT = """You draft semantic-layer migrations for ShipCube. Reply with ONE JSON object:
{"description": "<why, one sentence>", "ops": [<op>, ...]}
Allowed ops: %s.
Required keys: create_cube{name, table}; add_measure{name: "cube.member", sql?, agg?}; add_dimension{name, sql};
add_join{cube, to, key|on}; add_segment{name, sql}; rename_member{member, to}; deprecate_member{member, reason?};
change_sql{member, sql}; set_meta{target, description}. Member SQL is an expression over the cube's columns;
{other_member} references a member of the same cube. Reuse existing members instead of duplicating them.
Never grant access, add row policies, bind schemas or drop anything unless explicitly asked."""


def model_summary(model: Model) -> str:
    lines = []
    for c in model.cubes.values():
        ms = ", ".join(f"{m.name}={m.agg}({m.sql or '*'})" for m in c.measures.values())
        ds = ", ".join(f"{d.name}:{d.type}={d.sql}" for d in c.dimensions.values())
        lines.append(f"- {c.name} (table {c.table or 'sql'}; joins {', '.join(c.joins) or 'none'})\n  measures: {ms}\n  dimensions: {ds}")
    return "\n".join(lines) or "(empty model)"


def _first_json_object(text: str) -> dict[str, Any]:
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    break
        start = text.find("{", start + 1)
    raise ValueError("no JSON object in the reply")


class MuseDrafter:
    """Muse Spark drafts; RuleDrafter is the deterministic fallback when Muse is missing or wrong."""

    def __init__(self, muse: Optional[MuseClient] = None, fallback: Optional[DraftProvider] = None):
        self.muse, self.fallback = muse, fallback or RuleDrafter()

    @classmethod
    def from_env(cls) -> "MuseDrafter":
        """Real Muse Spark when META_API_KEY is set, rules otherwise."""
        from .muse import muse_from_env

        return cls(muse_from_env())

    def draft(self, model: Model, dialect: str, request: str, code: str) -> Draft:
        reason = "Muse Spark is not configured"
        if self.muse is not None:
            user = f"Current model:\n{model_summary(model)}\n\nRequest:\n{request or '(none)'}\n\nApp code:\n{code or '(none)'}"
            try:
                reply = _first_json_object(self.muse(SYSTEM_PROMPT % ", ".join(OP_NAMES), user))
                ops = reply.get("ops")
                if not isinstance(ops, list) or not ops:
                    raise ValueError("reply has no ops")
                for op in ops:
                    OP_ADAPTER.validate_python(op)
                log_event("draft.muse_ok", ops=len(ops))
                return Draft(ops=ops, description=str(reply.get("description") or request or "Drafted by Muse Spark")[:200],
                             explained_by=getattr(self.muse, "model", "muse-spark"))
            except Exception as e:  # any Muse failure falls back to rules; logged and reported, never silent
                reason = f"Muse Spark draft unusable ({type(e).__name__}: {str(e)[:120]})"
                log_event("draft.muse_fallback", level=30, error=type(e).__name__, detail=str(e)[:200])
        else:
            log_event("draft.rules_only", reason="META_API_KEY not set")
        draft = self.fallback.draft(model, dialect, request, code)
        draft.notes.insert(0, f"{reason}; drafted with rules instead")
        return draft
