"""MCP tools as an in-process Python library.

ShipCube's MCP server mounts these without any transport of their own:

    tools = DataTools(layer_for=lambda workspace_id: my_data_layer(workspace_id))
    for spec in tools.specs():          # name, description, inputSchema, scope
        server.register(spec["name"], spec["description"], spec["inputSchema"],
                        lambda args, caller, n=spec["name"]: tools.call(n, args, caller))

`caller` is an `identity.Caller` the server builds from the verified token. Every call
returns plain JSON-able data or raises `ToolError(code, message, hint)`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .engine import DataLayer
from .identity import Caller, security_context
from .model import ModelError
from .ops import OP_ADAPTER
from .query import Query

MAX_ROWS = 500

PREAMBLE = (
    "ShipCube's data layer is a semantic layer: ask for members (cube.member), not tables. "
    "Discover with list_cubes and describe_cube, ground filter values with search_values, "
    "and never invent SQL; if a member is missing, propose_migration and let a human approve it."
)


class ToolError(Exception):
    def __init__(self, code: str, message: str, hint: Optional[str] = None):
        super().__init__(message)
        self.code, self.message, self.hint = code, message, hint

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **({"hint": self.hint} if self.hint else {})}


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoArgs(_Args):
    pass


class DescribeArgs(_Args):
    cube: str


class QueryArgs(_Args):
    query: Query
    mode: Literal["run", "sql"] = "run"
    dialect: Optional[Literal["duckdb", "postgres", "bigquery", "clickhouse"]] = None


class SearchArgs(_Args):
    dimension: str
    text: str = ""
    limit: int = Field(default=20, ge=1, le=100)


class ProposeArgs(_Args):
    description: str = Field(min_length=3, description="Why. Stored with the migration and shown to the approver.")
    ops: list[dict[str, Any]] = Field(min_length=1, description="Migration ops; see the Op schema.")


class ApplyArgs(_Args):
    id: str


@dataclass
class _Tool:
    name: str
    scope: str
    args: type[_Args]
    description: str


TOOLS = [
    _Tool("list_cubes", "data:read", NoArgs,
          "List the cubes you can query, with their measure and dimension names and joined cubes. Start here."),
    _Tool("describe_cube", "data:read", DescribeArgs,
          "Full metadata for one cube: each member's title, description, type or aggregation, format, deprecation; "
          "joins with relationship (one_to_many joins fan out); partitioning (add a time range when present)."),
    _Tool("query", "data:read", QueryArgs,
          "Run a typed query (measures, dimensions, time, filters, segments, calcs, order, limit). "
          "mode=sql compiles without running. Read `warnings`: renamed or deprecated members and full scans."),
    _Tool("search_values", "data:read", SearchArgs,
          "Distinct values of a dimension containing `text`, to get the exact spelling before filtering on it."),
    _Tool("propose_migration", "data:write", ProposeArgs,
          "Propose a model change (create_cube, add_measure, add_dimension, add_join, rename_member, ...). "
          "Validates, dry-runs and returns the diff, the impact on saved queries and apps, and an approval_url. "
          "Never changes the model; a human approves in the console. Show the user the impact and the link."),
    _Tool("apply_migration", "data:write", ApplyArgs,
          "Apply a migration a human has approved. Fails with needs_approval, conflict or would_break otherwise."),
]
_BY_NAME = {t.name: t for t in TOOLS}


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, UUID):
        return str(v)
    return v


def default_approval_url(workspace_id: UUID, migration_id: str) -> str:
    return f"https://console.shipcube.dev/w/{workspace_id}/data/migrations/{migration_id}"


class DataTools:
    def __init__(self, layer_for: Callable[[UUID], DataLayer],
                 approval_url: Callable[[UUID, str], str] = default_approval_url):
        self.layer_for, self.approval_url = layer_for, approval_url

    # ------------------------------------------------------------ registry

    def specs(self) -> list[dict[str, Any]]:
        out = []
        for t in TOOLS:
            schema = t.args.model_json_schema()
            if t.name == "propose_migration":
                schema["properties"]["ops"]["items"] = OP_ADAPTER.json_schema()
            out.append({"name": t.name, "description": f"{t.description}\n\n{PREAMBLE}", "inputSchema": schema, "scope": t.scope})
        return out

    def call(self, name: str, args: dict[str, Any], caller: Caller) -> dict[str, Any]:
        tool = _BY_NAME.get(name)
        if tool is None:
            raise ToolError("unknown_tool", f"no tool named {name}")
        if not caller.has_scope(tool.scope):
            raise ToolError("forbidden", f"{name} needs the {tool.scope} scope on your token")
        try:
            parsed = tool.args.model_validate(args or {})
            return getattr(self, f"_{name}")(parsed, caller)
        except ValidationError as e:
            first = e.errors()[0]
            where = ".".join(str(x) for x in first["loc"])
            raise ToolError("invalid_arguments", f"{where}: {first['msg']}", f"{len(e.errors())} error(s) in total") from None
        except ModelError as e:
            raise ToolError(e.code, str(e), e.hint) from None

    def _ctx(self, caller: Caller):
        layer = self.layer_for(caller.workspace_id)
        return layer, security_context(caller, layer.catalog)

    # ------------------------------------------------------------ tools

    def _list_cubes(self, a: NoArgs, caller: Caller) -> dict[str, Any]:
        layer, ctx = self._ctx(caller)
        return {"model_version": layer.catalog.head(), "cubes": layer.list_cubes(ctx)}

    def _describe_cube(self, a: DescribeArgs, caller: Caller) -> dict[str, Any]:
        layer, ctx = self._ctx(caller)
        try:
            return layer.describe_cube(a.cube, ctx)
        except ModelError as e:
            raise ModelError(str(e), "unknown_cube", "call list_cubes for the cubes you can see") from None

    def _query(self, a: QueryArgs, caller: Caller) -> dict[str, Any]:
        layer, ctx = self._ctx(caller)
        if a.mode == "sql":
            compiled, q = layer.compile(a.query, ctx, a.dialect)
            return {"sql": compiled.sql, "dialect": compiled.dialect, "columns": [c.__dict__ for c in compiled.columns],
                    "partitions": compiled.partitions, "warnings": compiled.warnings,
                    "model_version": q.model_version or layer.catalog.head()}
        res = layer.query(a.query, ctx)
        rows = res["rows"]
        res["rows"] = [{k: _jsonable(v) for k, v in r.items()} for r in rows[:MAX_ROWS]]
        res["truncated"] = len(rows) > MAX_ROWS
        return res

    def _search_values(self, a: SearchArgs, caller: Caller) -> dict[str, Any]:
        layer, ctx = self._ctx(caller)
        return {"dimension": a.dimension, "values": [_jsonable(v) for v in layer.search_values(a.dimension, a.text, ctx, a.limit)]}

    def _propose_migration(self, a: ProposeArgs, caller: Caller) -> dict[str, Any]:
        if not caller.at_least("developer"):
            raise ToolError("forbidden", "proposing model changes needs the developer role in this workspace")
        layer, _ = self._ctx(caller)
        report = layer.runner.propose(a.ops, a.description, proposed_by=caller.principal)
        return report | {"approval_url": self.approval_url(caller.workspace_id, report["id"]), "auto_approved": False}

    def _apply_migration(self, a: ApplyArgs, caller: Caller) -> dict[str, Any]:
        if not caller.at_least("developer"):
            raise ToolError("forbidden", "applying model changes needs the developer role in this workspace")
        layer, _ = self._ctx(caller)
        try:
            return layer.runner.apply(layer.runner.stored(a.id), caller.principal, require_approval=True, allow_breaking=False)
        except ModelError as e:
            if e.code == "needs_approval":
                raise ToolError(e.code, str(e), f"ask a workspace admin to approve it: {self.approval_url(caller.workspace_id, a.id)}") from None
            raise

    # ------------------------------------------------------------ console-only (not MCP tools)

    def approve(self, migration_id: str, caller: Caller) -> dict[str, Any]:
        """Approve a proposal from a human console session. Admins approve anything; developers only additive changes."""
        if not caller.is_human_session:
            raise ToolError("forbidden", "migrations are approved by a person in the console, not with a token")
        layer, _ = self._ctx(caller)
        rec = layer.catalog.migration(migration_id)
        if rec is None:
            raise ToolError("not_found", f"unknown migration {migration_id}")
        if not (caller.at_least("admin") or (rec["kind"] == "additive" and caller.at_least("developer"))):
            raise ToolError("forbidden", f"approving a {rec['kind']} migration needs the admin role")
        try:
            layer.runner.approve(migration_id, caller.principal)
        except ModelError as e:
            raise ToolError(e.code, str(e), e.hint) from None
        return {"id": migration_id, "status": "approved", "approved_by": caller.principal}
