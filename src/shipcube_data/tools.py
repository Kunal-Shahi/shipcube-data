"""MCP tools as an in-process Python library.

ShipCube's MCP server mounts these with a `data_` prefix and no transport of their own:

    tools = DataTools(layer_for=lambda workspace_id: my_data_layer(workspace_id),
                      drafter=MuseDrafter(muse_client))
    for spec in tools.specs():          # data_list_cubes ... data_propose_migration
        server.register(spec["name"], spec["description"], spec["inputSchema"],
                        lambda args, caller, n=spec["name"]: tools.call(n, args, caller))

`caller` is an `identity.Caller` the server builds from the verified token. Every call
returns plain JSON-able data or raises `ToolError(code, message, hint)`.

Agents can never change the model: the MCP surface only *proposes*. Approving, applying and
connecting warehouses are console actions (`approve`, `apply`, `register_datasource`), which
require a person's console session.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, ContextManager, Iterator, Literal, Optional, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .datasources import Datasource, SecretRef, register_datasource
from .drafting import DraftProvider, MuseDrafter
from .engine import DataLayer
from .identity import Caller, security_context
from .model import ModelError
from .obs import log_event, new_query_id
from .catalog import _loads
from .ops import OP_ADAPTER, Migration
from .query import Query
from .stages import StagedQuery

MAX_ROWS = 500
PREFIX = "data_"

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
    query: Optional[Union[Query, StagedQuery]] = Field(
        default=None, description="A typed query (L0/L1), or a staged query (L2: `stages` + `output`).")
    sql: Optional[str] = Field(
        default=None, max_length=20_000,
        description="Governed SQL instead: one SELECT over cube names (columns are member names). Tenant, grants and "
                    "row policies are enforced; results are labelled verified=false.")
    mode: Literal["run", "sql"] = "run"
    dialect: Optional[Literal["duckdb", "postgres", "bigquery", "clickhouse"]] = None

    @model_validator(mode="after")
    def _one(self) -> "QueryArgs":
        if (self.query is None) == (self.sql is None):
            raise ValueError("pass exactly one of `query` or `sql`")
        return self


class SearchArgs(_Args):
    dimension: str
    text: str = ""
    limit: int = Field(default=20, ge=1, le=100)


class ProposeArgs(_Args):
    description: Optional[str] = Field(default=None, min_length=3, description="Why. Stored with the migration and shown to the approver.")
    ops: Optional[list[dict[str, Any]]] = Field(default=None, min_length=1, description="Explicit migration ops; see the Op schema.")
    request: Optional[str] = Field(default=None, description="Plain-English change to draft, e.g. 'add measure unique_customers as count distinct of customer_id on orders'.")
    code: Optional[str] = Field(default=None, description="App code whose SQL should be lifted into governed members.")

    @model_validator(mode="after")
    def _something(self) -> "ProposeArgs":
        if not (self.ops or self.request or self.code):
            raise ValueError("pass ops, or a request and/or code to draft from")
        if self.ops and not self.description:
            raise ValueError("explicit ops need a description")
        return self


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
          "Run a typed query (measures, dimensions, time, filters, segments, calcs, order, limit); a staged query "
          "(`stages`: named L0/L1 queries, then from/join/select/where/group/having/pivot/union over earlier stages, "
          "for cohorts, funnels, retention, top-N per group, re-aggregation); or governed `sql` over cube names "
          "when neither fits. mode=sql compiles without running. Read `warnings`: renamed or deprecated members and full scans."),
    _Tool("search_values", "data:read", SearchArgs,
          "Distinct values of a dimension containing `text`, to get the exact spelling before filtering on it."),
    _Tool("propose_migration", "data:migrate", ProposeArgs,
          "Propose a model change, as explicit ops or drafted from a plain-English `request` and/or app `code` "
          "(Muse Spark drafts; rules are the fallback). Validates, dry-runs and returns a summary, the diff, the impact "
          "on saved queries and apps, and an approval_url. Never changes the model: a person approves and applies it "
          "in the console. Show the user the summary, the impact and the link."),
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


# The data layer of the session in progress on this thread: (workspace_id, DataLayer).
_current: contextvars.ContextVar[Optional[tuple[str, DataLayer]]] = contextvars.ContextVar("shipcube_data_session", default=None)


def _in_session(fn):
    """Run a console action inside one session (one host transaction) for its caller."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        caller = kwargs.get("caller") or next(a for a in args if isinstance(a, Caller))
        with self.session(caller):
            return fn(self, *args, **kwargs)
    return wrapper


class DataTools:
    """The data_ tools and the console's actions.

    Pass `layer_for(workspace_id) -> DataLayer` (a catalog with its own connections), or
    `session(caller) -> context manager of DataLayer` when every call must run in a host
    transaction scoped to the verified caller (ShipCube's API: the shared catalog under RLS, in
    `db.tx(caller)`). Each tool call or console action is one session; nested calls join it.
    """

    def __init__(self, layer_for: Optional[Callable[[UUID], DataLayer]] = None,
                 approval_url: Callable[[UUID, str], str] = default_approval_url,
                 drafter: Optional[DraftProvider] = None,
                 session: Optional[Callable[[Caller], ContextManager[DataLayer]]] = None):
        if layer_for is None and session is None:
            raise ValueError("DataTools needs layer_for or session")
        self.layer_for, self.approval_url, self._session = layer_for, approval_url, session
        self.drafter = drafter or MuseDrafter.from_env()

    @contextlib.contextmanager
    def session(self, caller: Caller) -> Iterator[DataLayer]:
        ws = str(caller.workspace_id)
        current = _current.get()
        if current is not None and current[0] == ws:  # nested: join the session in progress
            yield current[1]
            return
        cm = self._session(caller) if self._session else contextlib.nullcontext(self.layer_for(caller.workspace_id))  # type: ignore[misc]
        with cm as layer:
            token = _current.set((ws, layer))
            try:
                yield layer
            finally:
                _current.reset(token)

    # ------------------------------------------------------------ registry

    def specs(self, prefix: str = PREFIX) -> list[dict[str, Any]]:
        out = []
        for t in TOOLS:
            schema = t.args.model_json_schema()
            if t.name == "propose_migration":
                ops = schema["properties"]["ops"]
                ops["anyOf"] = [{"type": "array", "minItems": 1, "items": OP_ADAPTER.json_schema()}, {"type": "null"}]
            out.append({"name": prefix + t.name, "description": f"{t.description}\n\n{PREAMBLE}", "inputSchema": schema, "scope": t.scope})
        return out

    def call(self, name: str, args: dict[str, Any], caller: Caller) -> dict[str, Any]:
        tool = _BY_NAME.get(name.removeprefix(PREFIX))
        if tool is None:
            raise ToolError("unknown_tool", f"no tool named {name}")
        if not caller.has_scope(tool.scope):
            raise ToolError("forbidden", f"{name} needs the {tool.scope} scope on your token")
        try:
            parsed = tool.args.model_validate(args or {})
            with self.session(caller):
                return getattr(self, f"_{tool.name}")(parsed, caller)
        except ValidationError as e:
            first = e.errors()[0]
            where = ".".join(str(x) for x in first["loc"])
            raise ToolError("invalid_arguments", f"{where}: {first['msg']}", f"{len(e.errors())} error(s) in total") from None
        except ModelError as e:
            raise ToolError(e.code, str(e), e.hint) from None
        except ToolError:
            raise
        except Exception as e:  # never hand a raw driver exception to an MCP client
            incident = new_query_id().replace("q_", "err_")
            log_event("tool.internal_error", level=40, tool=tool.name, incident=incident, error=type(e).__name__,
                      detail=str(e)[:500], workspace_id=str(caller.workspace_id))
            raise ToolError("internal_error", f"the data layer hit an unexpected {type(e).__name__}",
                            f"logged as {incident}; tell the workspace admin") from None

    def _layer(self, caller: Caller) -> DataLayer:
        current = _current.get()
        if current is None or current[0] != str(caller.workspace_id):
            raise RuntimeError("no data session for this caller")  # every entry point opens one
        return current[1]

    def _ctx(self, caller: Caller):
        layer = self._layer(caller)
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
        if a.sql is not None:
            if a.mode == "sql":
                from . import govsql

                safe, warnings = govsql.rewrite(a.sql, layer.catalog.load(), ctx, a.dialect or layer.dialect, layer.dialect,
                                                layer.limits.max_rows)
                return {"sql": safe, "dialect": a.dialect or layer.dialect, "warnings": warnings, "verified": False, "governed": True}
            res = layer.governed_sql(a.sql, ctx)
            rows = res["rows"]
            res["rows"] = [{k: _jsonable(v) for k, v in r.items()} for r in rows[:MAX_ROWS]]
            res["truncated"] = res["truncated"] or len(rows) > MAX_ROWS
            return res
        if a.mode == "sql":
            compiled, q = layer.compile(a.query, ctx, a.dialect)
            return {"sql": compiled.sql, "dialect": compiled.dialect, "columns": [c.__dict__ for c in compiled.columns],
                    "partitions": compiled.partitions, "warnings": compiled.warnings,
                    "model_version": getattr(q, "model_version", None) or layer.catalog.head()}
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
        layer, ctx = self._ctx(caller)
        drafted = None
        if a.ops:
            ops, description, explained_by, notes = a.ops, a.description, "agent", []
        else:
            drafted = self.drafter.draft(layer.catalog.load(), layer.dialect, a.request or "", a.code or "")
            ops, description, explained_by, notes = drafted.ops, a.description or drafted.description, drafted.explained_by, drafted.notes
        report = layer.runner.propose(ops, description, proposed_by=caller.principal,
                                      proposed_by_user=str(caller.user_id) if caller.user_id else None, drafted_by=explained_by)
        return report | {
            "approval_url": self.approval_url(caller.workspace_id, report["id"]),
            "explained_by": explained_by,
            "draft_notes": notes,
            "next_step": "A person reviews this in the console and approves it; agents cannot apply migrations.",
        }

    # ------------------------------------------------------------ console-only (not MCP tools)

    def _require_console(self, caller: Caller, what: str) -> None:
        if not caller.is_human_session:
            raise ToolError("forbidden", f"{what} is done by a person in the console, not with a token")

    @_in_session
    def apply(self, migration_id: str, caller: Caller, *, allow_breaking: bool = False) -> dict[str, Any]:
        """Apply an approved migration from the console (or the API acting on a console click)."""
        self._require_console(caller, "applying a migration")
        if not caller.at_least("developer") or (allow_breaking and not caller.at_least("admin")):
            raise ToolError("forbidden", "applying needs developer; applying despite broken saved queries needs admin")
        layer, _ = self._ctx(caller)
        try:
            return layer.runner.apply(layer.runner.stored(migration_id), caller.principal,
                                      require_approval=True, allow_breaking=allow_breaking)
        except ModelError as e:
            hint = e.hint
            if e.code == "needs_approval":
                hint = f"approve it first: {self.approval_url(caller.workspace_id, migration_id)}"
            raise ToolError(e.code, str(e), hint) from None

    @_in_session
    def register_datasource(self, caller: Caller, name: str, kind: str, secret_ref: Optional[dict[str, Any]] = None,
                            options: Optional[dict[str, Any]] = None,
                            verified_projects: Optional[list[str]] = None) -> dict[str, Any]:
        """Connect a warehouse from a stored secret reference. Console only, workspace admins only."""
        self._require_console(caller, "connecting a warehouse")
        if not caller.at_least("admin"):
            raise ToolError("forbidden", "connecting a warehouse needs the admin role")
        layer = self._layer(caller)
        security_context(caller, layer.catalog)  # workspace check
        try:
            ds = Datasource(name=name, kind=kind, secret_ref=SecretRef(**secret_ref) if secret_ref else None, options=options or {})
            return register_datasource(layer.catalog, ds, caller.principal, verified_projects)
        except ValidationError as e:
            first = e.errors()[0]
            raise ToolError("invalid_arguments", f"{'.'.join(map(str, first['loc']))}: {first['msg']}") from None
        except ModelError as e:
            raise ToolError(e.code, str(e), e.hint) from None

    @_in_session
    def approve(self, migration_id: str, caller: Caller) -> dict[str, Any]:
        """Approve a proposal from a human console session. Admins approve anything; developers only additive changes."""
        self._require_console(caller, "approving a migration")
        layer, _ = self._ctx(caller)
        rec = layer.catalog.migration(migration_id)
        if rec is None:
            raise ToolError("not_found", f"unknown migration {migration_id}")
        widens = Migration.model_validate({"id": rec["id"], "ops": _loads(rec["ops"])}).widens_exposure()
        if not caller.at_least("admin"):
            if not caller.at_least("developer") or rec["kind"] != "additive" or widens:
                why = f"it can expose more data ({', '.join(sorted(set(widens)))})" if widens else f"it is a {rec['kind']} migration"
                raise ToolError("forbidden", f"approving this needs the admin role: {why}")
        try:
            layer.runner.approve(migration_id, caller.principal, str(caller.user_id) if caller.user_id else None)
        except ModelError as e:
            raise ToolError(e.code, str(e), e.hint) from None
        return {"id": migration_id, "status": "approved", "approved_by": caller.principal}
