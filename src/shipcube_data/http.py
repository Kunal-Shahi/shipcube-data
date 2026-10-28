"""HTTP routes for the console's Data page (`/v1/data/…`), mounted by ShipCube's API.

    from shipcube_data.http import data_router
    app.include_router(data_router(caller_dependency=caller), prefix="/v1/data")

`caller_dependency` is the API's own FastAPI dependency for the verified caller (its
`auth.Caller`: user_id, workspace_id, email, role, token_id, scopes, via). Response bodies are
the console's types in `apps/web/src/lib/types.ts` (Warehouse, Cube, Migration), so the console
passes them through. Errors are `HTTPException(status, {code, message, hint})`, which the API's
handler wraps as `{"error": {...}}`.

Scoping: every request opens the caller's own workspace catalog (a Postgres schema per
workspace). Reads need `data:read` on a PAT. Changing the model or the warehouse needs a
person's console session: agents only ever propose.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Callable, Literal, Optional, Union

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .catalog import _loads
from .compiler import Compiler
from .datasources import (Datasource, SecretRef, check_db_host, dev_mode, get_datasource, register_datasource)
from .identity import Caller, security_context
from .model import ModelError, humanize
from .obs import log_event
from .tools import DataTools, ToolError

STATUS = {
    "forbidden": 403, "permission_denied": 403, "not_found": 404, "unknown_cube": 404, "catalog_not_migrated": 404,
    "no_datasource": 404, "tenant_not_provisioned": 404, "conflict": 409, "needs_approval": 409, "would_break": 409,
    "checksum_mismatch": 409, "invalid": 400, "invalid_arguments": 400, "invalid_op": 400, "invalid_sql": 400,
    "warehouse_unavailable": 503, "query_timeout": 504, "internal_error": 500,
}
API_CODE = {"forbidden": "permission_denied", "invalid": "invalid_argument", "invalid_arguments": "invalid_argument"}

PrincipalNames = Callable[[Optional[str], Optional[str]], dict[str, str]]


def http_error(code: str, message: str, hint: Optional[str] = None) -> HTTPException:
    return HTTPException(STATUS.get(code, 400), {"code": API_CODE.get(code, code), "message": message,
                                                 **({"hint": hint} if hint else {})})


def to_caller(c: Any) -> Caller:
    """The API's auth.Caller (or our own Caller) as a shipcube_data Caller."""
    if isinstance(c, Caller):
        return c
    via = getattr(c, "via", "mcp")
    token = getattr(c, "token_id", None)
    return Caller(
        workspace_id=uuid.UUID(str(c.workspace_id)), via="console" if via == "console" else "mcp",
        email=c.email, user_id=uuid.UUID(str(c.user_id)) if getattr(c, "user_id", None) else None,
        membership_role=getattr(c, "role", None),
        token_id=(uuid.UUID(str(token)) if _is_uuid(token) else uuid.uuid5(uuid.NAMESPACE_URL, f"shipcube-token:{token}")) if token else None,
        scopes=sorted(getattr(c, "scopes", ()) or ()),
    )


def _is_uuid(v: Any) -> bool:
    try:
        uuid.UUID(str(v))
        return True
    except ValueError:
        return False


def default_names(principal: Optional[str], user_id: Optional[str]) -> dict[str, str]:
    kind, _, ident = (principal or "system").partition(":")
    if kind == "token":
        return {"agent": "MCP agent", "tokenName": f"token {ident[:8]}", "user": user_id or "unknown"}
    if kind == "user":
        return {"agent": "Console", "tokenName": "console session", "user": user_id or ident}
    return {"agent": "ShipCube", "tokenName": "system", "user": principal or "system"}


# ---------------------------------------------------------------- serializers (console shapes)

def _iso(v: Any) -> Optional[str]:
    if v is None:
        return None
    return v.isoformat() if isinstance(v, datetime) else str(v)


def _camel_impact(impact: Optional[dict[str, Any]]) -> dict[str, Any]:
    impact = impact or {}
    return {"breaks": impact.get("breaks", []), "changes": impact.get("changes", []), "warnings": impact.get("warnings", []),
            "unaffected": impact.get("unaffected", 0), "appsBroken": impact.get("apps_broken", [])}


def migration_body(rec: dict[str, Any], catalog, names: PrincipalNames) -> dict[str, Any]:
    raw = rec["status"]
    status = {"proposed": "proposed", "approved": "proposed", "applied": "applied"}.get(raw, "rejected")
    body: dict[str, Any] = {
        "id": rec["id"], "description": rec["description"], "status": status, "rawStatus": raw, "kind": rec["kind"],
        "proposedBy": names(rec.get("proposed_by"), rec.get("proposed_by_user")),
        "proposedAt": _iso(rec["created_at"]), "baseVersion": rec.get("base_version") or 0,
        "ops": _loads(rec["ops"]) or [], "diff": _loads(rec.get("diff")) or {"added": [], "changed": [], "removed": []},
        "impact": _camel_impact(_loads(rec.get("impact"))),
    }
    if rec.get("drafted_by"):
        body["draftedBy"] = rec["drafted_by"]
    if raw == "approved":
        body["approved"] = {"at": _iso(rec.get("decided_at")), "by": names(rec.get("approved_by"), rec.get("approved_by_user"))["user"]}
    if raw == "applied":
        v = catalog.db.execute("SELECT applied_at, applied_by FROM model_versions WHERE version = ?", (rec["version"],)).fetchone()
        body["applied"] = {"version": rec["version"], "at": _iso(v["applied_at"] if v else rec.get("applied_at")),
                           "by": names(v["applied_by"] if v else None, rec.get("approved_by_user"))["user"]}
    if raw == "rejected":
        body["rejected"] = {"at": _iso(rec.get("decided_at")), "by": names(rec.get("approved_by"), rec.get("approved_by_user"))["user"],
                            **({"note": rec["decision_note"]} if rec.get("decision_note") else {})}
    if raw == "rolled_back":
        body["rejected"] = {"at": _iso(rec.get("decided_at")), "by": "rollback", "note": f"rolled back (was version {rec['version']})"}
    return body


def cube_body(cube, model, layer, caller: Caller, ctx, admin: bool) -> dict[str, Any]:
    comp = Compiler(model, ctx, layer.dialect, layer.dialect)
    try:
        schema, _ = comp.schema_for(cube)
    except ModelError:
        schema = None

    def member(m) -> dict[str, Any]:
        grants = m.grants or cube.grants
        d: dict[str, Any] = {"name": m.name, "type": getattr(m, "type", None) or getattr(m, "agg", "number"),
                             "sql": m.sql or ("*" if m.kind == "measure" else m.name)}
        if m.description:
            d["description"] = m.description
        if m.deprecated:
            d["deprecated"] = m.deprecated + (f"; use {m.replaced_by}" if m.replaced_by else "")
        if grants:
            d["restrictedTo"] = list(grants)
        return d

    shown = [m for m in cube.members() if admin or comp.visible(cube, m)]
    p = cube.partition
    partition = None
    if p:
        partition = f"{p.kind} by {p.column}" + (f" ({p.granularity})" if p.granularity else "") + (
            f", one table per partition: {p.table_pattern}" if p.strategy == "table" else "")
    used = set()
    for sq in layer.catalog.saved_queries():
        text = json.dumps(sq["query"])
        if f'"{cube.name}.' in text:
            used.add(sq["app"] or sq["name"])
    return {
        "name": cube.name, "title": cube.title or humanize(cube.name), "description": cube.description or "",
        "table": cube.table or "(SQL)", "schema": schema or "",
        "dimensions": [member(m) for m in shown if m.kind == "dimension"],
        "measures": [member(m) for m in shown if m.kind == "measure"],
        "joins": [{"to": e.dst, "relationship": e.relationship, "on": e.on} for e in comp._edges(cube.name)],
        "segments": [{"name": s.name, "sql": s.sql} for s in cube.segments.values()],
        **({"partition": partition} if partition else {}),
        "policies": [f"{pol.name}" + (f" (roles: {', '.join(pol.roles)})" if pol.roles else "")
                     for pol in model.policies.values() if pol.cube == cube.name],
        "usedBy": sorted(used),
    }


# ---------------------------------------------------------------- inputs

class PostgresInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["postgres", "clickhouse"]
    host: str
    port: int = 5432
    database: str
    user: str
    password: str = Field(repr=False)
    ssl: str = "verify-full"


class BigQueryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["bigquery"]
    project: str
    location: str = "US"
    serviceAccountKey: str = Field(repr=False)


WarehouseInput = Union[PostgresInput, BigQueryInput]


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    allowBreaking: bool = False
    note: Optional[str] = Field(default=None, max_length=1000)


# ---------------------------------------------------------------- secret store (where PUT /warehouse writes)

class SecretStore:
    def put(self, workspace_id: str, name: str, value: str) -> SecretRef:  # pragma: no cover - interface
        raise NotImplementedError


class LocalSecretStore(SecretStore):
    """DEV_MODE only: keeps the value in this process's environment, where local_secret_resolver reads it."""

    def put(self, workspace_id: str, name: str, value: str) -> SecretRef:
        if not dev_mode():
            raise ModelError("the local secret store only works in SHIPCUBE_DEV_MODE", "forbidden")
        import re

        os.environ["SHIPCUBE_DATA_SECRET_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()] = value
        return SecretRef(id=str(uuid.uuid4()), resource_name=f"projects/local/secrets/{name}", version="1")


class GcpSecretStore(SecretStore):
    """Secret Manager in SHIPCUBE_DATA_SECRET_PROJECT, with the runtime service account (ADC)."""

    def put(self, workspace_id: str, name: str, value: str) -> SecretRef:
        import base64

        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        project = os.environ.get("SHIPCUBE_DATA_SECRET_PROJECT")
        if not project:
            raise ModelError("SHIPCUBE_DATA_SECRET_PROJECT is not set", "invalid")
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        s = AuthorizedSession(creds)
        base = f"https://secretmanager.googleapis.com/v1/projects/{project}/secrets"
        r = s.post(f"{base}?secretId={name}", json={"replication": {"automatic": {}},
                                                    "labels": {"shipcube_workspace": workspace_id.replace("-", "")}}, timeout=10)
        if r.status_code not in (200, 409):
            raise ModelError(f"cannot create the secret (HTTP {r.status_code})", "warehouse_unavailable")
        r = s.post(f"{base}/{name}:addVersion", json={"payload": {"data": base64.b64encode(value.encode()).decode()}}, timeout=10)
        if r.status_code != 200:
            raise ModelError(f"cannot store the secret (HTTP {r.status_code})", "warehouse_unavailable")
        version = r.json()["name"].rsplit("/", 1)[1]
        return SecretRef(id=str(uuid.uuid4()), resource_name=f"projects/{project}/secrets/{name}", version=version)


# ---------------------------------------------------------------- router

def data_router(caller_dependency: Callable[..., Any], tools: Optional[DataTools] = None,
                secret_store: Optional[SecretStore] = None,
                verified_projects: Callable[[str], list[str]] = lambda ws: [],
                principal_names: PrincipalNames = default_names) -> APIRouter:
    """Routes for the console's Data page. Mount under /v1/data."""
    router = APIRouter(tags=["data"])
    state: dict[str, Any] = {"tools": tools}

    def get_tools() -> DataTools:
        if state["tools"] is None:
            try:
                from .local import tools_from_env

                state["tools"] = tools_from_env()
            except ModelError as e:
                raise http_error("data_unavailable", f"the data layer is not configured: {e}") from None
        return state["tools"]

    def store() -> SecretStore:
        return secret_store or (LocalSecretStore() if dev_mode() else GcpSecretStore())

    def session(raw: Any = Depends(caller_dependency)) -> Caller:
        return to_caller(raw)

    def reader(c: Caller = Depends(session)) -> Caller:
        if not c.has_scope("data:read"):
            raise http_error("forbidden", "token lacks scope data:read")
        return c

    def person(c: Caller = Depends(session)) -> Caller:
        if not c.is_human_session:
            raise http_error("forbidden", "only a person in the console can do this; agents can only propose")
        return c

    def layer_ctx(c: Caller):
        t = get_tools()
        try:
            layer = t.layer_for(c.workspace_id)
            return t, layer, security_context(c, layer.catalog)
        except ModelError as e:
            raise http_error(e.code, str(e), e.hint) from None

    def guard(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except ToolError as e:
            raise http_error(e.code, e.message, e.hint) from None
        except ModelError as e:
            raise http_error(e.code, str(e), e.hint) from None

    # ---- warehouse

    @router.get("/warehouse")
    def get_warehouse(c: Caller = Depends(reader)) -> dict[str, Any]:
        _, layer, _ = layer_ctx(c)
        cat = layer.catalog
        row = cat.db.execute("SELECT * FROM datasources WHERE name = 'primary'").fetchone()
        if row is None:
            return {"connected": False, "modelVersion": cat.head()}
        ds = get_datasource(cat)
        details = [{"label": k.replace("_", " ").capitalize(), "value": str(v)} for k, v in sorted(ds.options.items())
                   if k in {"host", "port", "dbname", "user", "project", "location", "impersonate_service_account"}]
        if ds.kind == "duckdb":
            details.append({"label": "File", "value": os.path.basename(str(ds.options.get("path", "")))})
        if ds.secret_ref:
            details.append({"label": "Credential", "value": f"Secret Manager reference {ds.secret_ref.id[:8]} (version {ds.secret_ref.version})"})
        checked = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            layer.execute("SELECT 1", layer.limits, "q_health")
            details.append({"label": "Status", "value": "reachable"})
        except ModelError as e:
            details.append({"label": "Status", "value": f"unreachable ({e.code})"})
        return {"connected": True, "kind": ds.kind, "name": ds.options.get("dbname") or ds.options.get("project") or ds.name,
                "details": details, "connectedAt": _iso(row["created_at"]), "connectedBy": principal_names(row["created_by"], None)["user"],
                "lastCheckedAt": checked, "modelVersion": cat.head()}

    def admin_person(c: Caller) -> None:
        if not c.at_least("admin"):
            raise http_error("forbidden", "connecting a warehouse needs the admin role")

    def candidate(c: Caller, body: WarehouseInput) -> tuple[Datasource, str]:
        if isinstance(body, BigQueryInput):
            if body.project not in set(verified_projects(str(c.workspace_id))):
                raise http_error("forbidden", f"project {body.project!r} is not a verified GCP project of this workspace; link it first")
            return Datasource(name="primary", kind="bigquery", options={"project": body.project, "location": body.location}), body.serviceAccountKey
        if body.kind == "clickhouse":
            raise http_error("invalid", "ClickHouse warehouses are not supported yet")
        guard(lambda: check_db_host(body.host, body.ssl))
        return Datasource(name="primary", kind="postgres", options={"host": body.host, "port": body.port, "dbname": body.database,
                                                                    "user": body.user, "sslmode": body.ssl}), body.password

    def probe(ds: Datasource, secret: str) -> dict[str, int]:
        from .datasources import DEFAULT_CONNECTORS
        from .warehouses import QueryLimits

        opts = dict(ds.options, verified=True) if ds.kind == "bigquery" else ds.options
        execute = DEFAULT_CONNECTORS[ds.kind](ds.model_copy(update={"options": opts}), secret)
        if ds.kind == "bigquery":
            sql = (f"SELECT COUNT(*), COUNT(DISTINCT table_schema) FROM `{ds.options['project']}`."
                   f"`region-{ds.options.get('location', 'US').lower()}`.INFORMATION_SCHEMA.TABLES")
        else:
            sql = ("SELECT COUNT(*), COUNT(DISTINCT table_schema) FROM information_schema.tables "
                   "WHERE table_schema NOT IN ('pg_catalog', 'information_schema')")
        res = guard(lambda: execute(sql, QueryLimits(timeout_s=15, max_rows=1), "q_conntest"))
        tables, schemas = res.rows[0]
        return {"tables": int(tables), "schemas": int(schemas)}

    @router.post("/warehouse:test")
    def test_warehouse(body: Annotated[WarehouseInput, Field(discriminator="kind")], c: Caller = Depends(person)) -> dict[str, Any]:
        admin_person(c)
        ds, secret = candidate(c, body)
        out = probe(ds, secret)
        log_event("data.warehouse.test", workspace_id=str(c.workspace_id), kind=ds.kind, **out)
        return out

    @router.put("/warehouse")
    def put_warehouse(body: Annotated[WarehouseInput, Field(discriminator="kind")], c: Caller = Depends(person)) -> dict[str, Any]:
        admin_person(c)
        _, layer, _ = layer_ctx(c)
        ds, secret = candidate(c, body)
        out = probe(ds, secret)  # never store credentials that don't work
        ref = guard(lambda: store().put(str(c.workspace_id), f"ws-{c.workspace_id}-warehouse-{uuid.uuid4().hex[:8]}", secret))
        guard(lambda: register_datasource(layer.catalog, ds.model_copy(update={"secret_ref": ref}), c.principal,
                                          verified_projects(str(c.workspace_id))))
        state["tools"] = None  # reconnect with the new credentials
        return {**out, "warehouse": get_warehouse(c)}

    # ---- cubes

    @router.get("/cubes")
    def list_cubes(c: Caller = Depends(reader)) -> list[dict[str, Any]]:
        _, layer, ctx = layer_ctx(c)
        model = layer.catalog.load()
        admin = c.at_least("admin")
        return [cube_body(cube, model, layer, c, ctx, admin) for cube in model.cubes.values()]

    @router.get("/cubes/{name}")
    def get_cube(name: str, c: Caller = Depends(reader)) -> dict[str, Any]:
        _, layer, ctx = layer_ctx(c)
        model = layer.catalog.load()
        if name not in model.cubes:
            raise http_error("not_found", f"no cube {name!r}")
        return cube_body(model.cubes[name], model, layer, c, ctx, c.at_least("admin"))

    # ---- migrations

    @router.get("/migrations")
    def list_migrations(c: Caller = Depends(reader)) -> list[dict[str, Any]]:
        _, layer, _ = layer_ctx(c)
        recs = sorted(layer.catalog.migrations(), key=lambda r: str(r["created_at"]), reverse=True)
        return [migration_body(r, layer.catalog, principal_names) for r in recs]

    def one(layer, mid: str) -> dict[str, Any]:
        rec = layer.catalog.migration(mid)
        if rec is None:
            raise http_error("not_found", f"no migration {mid!r}")
        return rec

    @router.get("/migrations/{mid}")
    def get_migration(mid: str, c: Caller = Depends(reader)) -> dict[str, Any]:
        _, layer, _ = layer_ctx(c)
        return migration_body(one(layer, mid), layer.catalog, principal_names)

    @router.post("/migrations/{mid_action}")
    def decide(mid_action: str, body: Optional[DecisionBody] = None, c: Caller = Depends(person)) -> dict[str, Any]:
        body = body or DecisionBody()
        mid, _, action = mid_action.rpartition(":")
        if action not in {"apply", "reject"} or not mid:
            raise http_error("not_found", "use POST /v1/data/migrations/{id}:apply or :reject")
        t, layer, _ = layer_ctx(c)
        rec = one(layer, mid)
        if rec["status"] not in {"proposed", "approved"}:
            raise http_error("conflict", f"{mid} is already {rec['status']}")
        if action == "reject":
            if not c.at_least("developer"):
                raise http_error("forbidden", "rejecting a proposal needs the developer role")
            guard(lambda: layer.runner.reject(mid, c.principal, body.note or "", str(c.user_id) if c.user_id else None))
        else:
            if rec["status"] == "proposed":
                guard(lambda: t.approve(mid, c))
            guard(lambda: t.apply(mid, c, allow_breaking=body.allowBreaking))
        log_event(f"data.migration.{action}", migration_id=mid, workspace_id=str(c.workspace_id), via="console")
        return migration_body(one(layer, mid), layer.catalog, principal_names)

    return router
