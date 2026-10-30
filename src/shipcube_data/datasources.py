"""Warehouse connections.

A datasource is created in the console by a person, never by an agent. The catalog stores
only a *reference* to the credential (a row of the security schema's `secret_refs`: id,
Secret Manager resource name, pinned version). The value is fetched at connect time by a
resolver the API supplies, and never stored or logged here.

Guards (red-team issues #3, #4, #5), checked when a datasource is registered *and* again at
connect time:

* **Secrets belong to the workspace.** `secret_ref` must live in the configured secrets project
  (`SHIPCUBE_DATA_SECRET_PROJECT`) and be named `ws-<workspace id>-…`, the security schema's
  convention, so one workspace cannot point at another's credential.
* **Postgres hosts are public and verified.** Hosts that resolve to private, loopback,
  link-local or reserved addresses, and Unix sockets such as `/cloudsql/…`, are refused unless
  listed in `SHIPCUBE_DATA_ALLOWED_DB_HOSTS`. TLS defaults to `sslmode=verify-full`; only
  allow-listed hosts may lower it. A full URL inside the secret gets the same checks.
* **BigQuery never runs as ShipCube.** The project must be one the API verified for this
  workspace (`verified_projects`, from its linked GCP projects). Credentials are either a
  service-account key in Secret Manager or impersonation of a service account *in that
  project*; ShipCube's own ADC identity is never used directly.
* **DuckDB is local development only** (`SHIPCUBE_DEV_MODE=1`), and its connections are sealed.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
from typing import Any, Callable, Iterable, Optional

from pydantic import BaseModel, ConfigDict, Field

from .catalog import Catalog, _loads, _now
from .compiler import DIALECTS
from .model import ModelError
from .warehouses import BigQueryWarehouse, DuckDBWarehouse, Executor, PostgresWarehouse

_RESOURCE_RE = re.compile(r"^projects/(?P<project>[a-z][a-z0-9-]{1,61}[a-z0-9]|local)/secrets/(?P<name>[A-Za-z0-9_-]+)$")
_SECRETISH = re.compile(r"(password|passwd|secret|token|key|credential)", re.I)
_SA_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]@(?P<project>[a-z][a-z0-9-]{4,28}[a-z0-9])\.iam\.gserviceaccount\.com$")
LOCAL_SECRET_PROJECT = "local"


class SecretRef(BaseModel):
    """A row of `secret_refs` (security schema). Holds no secret value."""

    model_config = ConfigDict(extra="forbid")
    id: str
    resource_name: str
    version: str = Field(pattern=r"^[0-9]+$", description="a pinned version, never 'latest'")


class Datasource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    kind: str
    secret_ref: Optional[SecretRef] = None
    options: dict[str, Any] = Field(default_factory=dict)  # host, database, project, dataset, path ... never credentials


SecretResolver = Callable[[SecretRef], str]
Connector = Callable[[Datasource, Optional[str]], Executor]


def dev_mode() -> bool:
    return os.environ.get("SHIPCUBE_DEV_MODE") == "1"


def _allowed_hosts() -> set[str]:
    return {h.strip().lower() for h in os.environ.get("SHIPCUBE_DATA_ALLOWED_DB_HOSTS", "").split(",") if h.strip()}


# ---------------------------------------------------------------- checks

def check_secret_ref(ref: SecretRef, workspace_id: Optional[str]) -> None:
    m = _RESOURCE_RE.match(ref.resource_name)
    if not m:
        raise ModelError("secret_ref.resource_name must look like projects/<p>/secrets/<name>", "invalid")
    project, name = m["project"], m["name"]
    if project == LOCAL_SECRET_PROJECT:
        if not dev_mode():
            raise ModelError("local secrets are only allowed in SHIPCUBE_DEV_MODE", "forbidden")
    elif project != os.environ.get("SHIPCUBE_DATA_SECRET_PROJECT"):
        raise ModelError("secret_ref must be in ShipCube's secrets project (SHIPCUBE_DATA_SECRET_PROJECT)", "forbidden")
    if not workspace_id:
        raise ModelError("this catalog has no workspace; secrets cannot be attached", "forbidden")
    if not name.startswith(f"ws-{workspace_id}-"):
        raise ModelError(f"secret_ref must belong to this workspace (named ws-{workspace_id}-…)", "forbidden")


def check_db_host(host: Optional[str], sslmode: Optional[str]) -> None:
    """Refuse private/loopback/link-local/reserved targets and sockets unless allow-listed."""
    if not host:
        raise ModelError("a postgres datasource needs options.host", "invalid")
    allowed = _allowed_hosts()
    for h in str(host).split(","):  # libpq accepts a host list
        h = h.strip()
        if h.lower() in allowed:
            continue
        if h.startswith("/"):
            raise ModelError(f"socket host {h!r} is not allowed; ask an operator to allow-list it", "forbidden")
        try:
            addrs = {ai[4][0] for ai in socket.getaddrinfo(h, None)}
        except socket.gaierror:
            raise ModelError(f"cannot resolve database host {h!r}", "invalid") from None
        for a in addrs:
            ip = ipaddress.ip_address(a.split("%")[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
                raise ModelError(f"database host {h!r} resolves to a non-public address; ask an operator to allow-list it", "forbidden")
        if (sslmode or "verify-full") != "verify-full":
            raise ModelError("public database hosts must use sslmode=verify-full", "forbidden")


def _check(ds: Datasource, workspace_id: Optional[str], verified_projects: Optional[Iterable[str]], registering: bool) -> None:
    if ds.kind not in DIALECTS:
        raise ModelError(f"unsupported warehouse kind {ds.kind!r}; pick one of {', '.join(DIALECTS)}", "invalid")
    bad = [k for k in ds.options if _SECRETISH.search(k)]
    if bad:
        raise ModelError(f"options {bad} look like credentials; put them in Secret Manager and pass a secret_ref", "invalid")
    if ds.secret_ref:
        check_secret_ref(ds.secret_ref, workspace_id)
    if ds.kind == "duckdb":
        if not dev_mode():
            raise ModelError("duckdb datasources are for local development only (SHIPCUBE_DEV_MODE=1)", "forbidden")
    elif ds.kind in {"postgres", "clickhouse"}:
        if ds.secret_ref is None:
            raise ModelError(f"a {ds.kind} datasource needs a secret_ref", "invalid")
        if ds.kind == "postgres" and ds.options.get("host"):
            check_db_host(ds.options["host"], ds.options.get("sslmode"))
    elif ds.kind == "bigquery":
        project = ds.options.get("project")
        if not project:
            raise ModelError("a bigquery datasource needs options.project", "invalid")
        if registering and project not in set(verified_projects or ()):
            raise ModelError(f"project {project!r} is not a verified GCP project of this workspace; link it first", "forbidden")
        sa = ds.options.get("impersonate_service_account")
        if ds.secret_ref is None and not sa:
            raise ModelError("a bigquery datasource needs a service-account key secret_ref or impersonate_service_account", "invalid")
        if sa:
            m = _SA_RE.match(sa)
            if not m or m["project"] != project:
                raise ModelError("impersonate_service_account must be a service account in the datasource's project", "forbidden")


def register_datasource(catalog: Catalog, ds: Datasource, actor: Optional[str] = None,
                        verified_projects: Optional[Iterable[str]] = None) -> dict[str, Any]:
    """Validate and store a datasource. The catalog's dialect follows the datasource's kind.

    `verified_projects` are the workspace's linked, verified GCP projects (from the API); a
    BigQuery datasource must use one of them.
    """
    _check(ds, catalog.workspace_id, verified_projects, registering=True)
    has_model = catalog.head() > 0 and catalog.load().cubes
    if has_model and catalog.dialect != ds.kind:
        raise ModelError(f"the model is written in {catalog.dialect} SQL; a {ds.kind} warehouse needs a new catalog", "invalid")
    options = dict(ds.options)
    if ds.kind == "bigquery":
        options["verified"] = True
    with catalog.transaction() as cat:
        cat.db.execute(
            "INSERT INTO datasources (workspace_id, name, kind, secret_ref_id, secret_resource, secret_version, options, created_by, created_at) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT ({cat._pk('name')}) DO UPDATE SET kind = excluded.kind, "
            "secret_ref_id = excluded.secret_ref_id, secret_resource = excluded.secret_resource, "
            "secret_version = excluded.secret_version, options = excluded.options, "
            "created_by = excluded.created_by, created_at = excluded.created_at",
            (cat.ws, ds.name, ds.kind, ds.secret_ref.id if ds.secret_ref else None,
             ds.secret_ref.resource_name if ds.secret_ref else None, ds.secret_ref.version if ds.secret_ref else None,
             json.dumps(options, sort_keys=True), actor, _now()),
        )
        cat.set_setting("dialect", ds.kind)
        cat.audit("data.datasource.register", actor, target_type="datasource", target_id=ds.name, kind=ds.kind,
                  secret_ref_id=ds.secret_ref.id if ds.secret_ref else None)
    return {"name": ds.name, "kind": ds.kind, "secret_ref_id": ds.secret_ref.id if ds.secret_ref else None}


def get_datasource(catalog: Catalog, name: str = "primary") -> Datasource:
    r = catalog.db.execute("SELECT * FROM datasources WHERE workspace_id = ? AND name = ?", (catalog.ws, name)).fetchone()
    if r is None:
        raise ModelError(f"no datasource {name!r}; connect a warehouse in the console", "no_datasource")
    ref = SecretRef(id=r["secret_ref_id"], resource_name=r["secret_resource"], version=r["secret_version"]) if r["secret_ref_id"] else None
    return Datasource(name=r["name"], kind=r["kind"], secret_ref=ref, options=_loads(r["options"]))


# ---------------------------------------------------------------- connectors

def duckdb_connector(ds: Datasource, secret: Optional[str]) -> Executor:
    import duckdb

    path = ds.options.get("path", ":memory:")
    return DuckDBWarehouse(duckdb.connect(path, read_only=path != ":memory:", config={"enable_external_access": False}))


_URL_KEYS = {"host", "port", "dbname", "user", "password", "sslmode"}


def postgres_connector(ds: Datasource, secret: Optional[str]) -> Executor:
    """Cloud SQL Postgres (or any Postgres). The secret is the password for options host / port /
    dbname / user / sslmode, or a full libpq URL; either way the final host is checked."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    if secret and secret.startswith(("postgres://", "postgresql://")):
        try:
            params = conninfo_to_dict(secret)
        except Exception:
            raise ModelError("the warehouse secret is not a valid Postgres URL", "invalid") from None
        # Only plain connection keys: hostaddr would connect somewhere other than the checked host;
        # service/passfile/ssl* files read the server's filesystem; options can change session settings.
        extra = sorted(set(params) - _URL_KEYS)
        if extra:
            raise ModelError(f"the warehouse URL may not set {', '.join(extra)}; use host, port, dbname, user, password "
                             "and sslmode only", "forbidden")
    else:
        o = ds.options
        params = {"host": o.get("host"), "port": o.get("port"), "dbname": o.get("dbname"), "user": o.get("user"),
                  "password": secret, "sslmode": o.get("sslmode", "verify-full")}
    params.setdefault("sslmode", "verify-full")
    check_db_host(params.get("host"), params.get("sslmode"))
    return PostgresWarehouse(make_conninfo(**{k: v for k, v in params.items() if v is not None}))


def bigquery_connector(ds: Datasource, secret: Optional[str]) -> Executor:
    """BigQuery with a service-account key from Secret Manager, or impersonating a service account
    in the customer's verified project. Never ShipCube's own identity."""
    if not ds.options.get("verified"):
        raise ModelError("this BigQuery datasource was not registered against a verified project", "forbidden")
    return BigQueryWarehouse(project=ds.options["project"], location=ds.options.get("location"), credentials_json=secret,
                             impersonate=ds.options.get("impersonate_service_account"))


DEFAULT_CONNECTORS: dict[str, Connector] = {
    "duckdb": duckdb_connector, "postgres": postgres_connector, "bigquery": bigquery_connector,
}


def open_executor(catalog: Catalog, resolve_secret: Optional[SecretResolver] = None, name: str = "primary",
                  connectors: Optional[dict[str, Connector]] = None) -> Executor:
    """Connect to a registered datasource, re-checking it first. Built-in connectors: duckdb, postgres, bigquery."""
    ds = get_datasource(catalog, name)
    _check(ds, catalog.workspace_id, None, registering=False)
    connector = {**DEFAULT_CONNECTORS, **(connectors or {})}.get(ds.kind)
    if connector is None:
        raise ModelError(f"no connector registered for {ds.kind}", "no_datasource")
    secret = None
    if ds.secret_ref:
        if resolve_secret is None:
            raise ModelError("this datasource needs a secret resolver", "no_datasource")
        secret = resolve_secret(ds.secret_ref)
    return connector(ds, secret)


def local_secret_resolver(ref: SecretRef) -> str:
    """DEV_MODE only: `projects/local/secrets/<name>` reads env `SHIPCUBE_DATA_SECRET_<NAME>`
    (upper-cased, '-' → '_'), then the API's local secrets file (SHIPCUBE_LOCAL_SECRETS_FILE).
    Production passes a Secret Manager resolver instead."""
    m = _RESOURCE_RE.match(ref.resource_name)
    if not m or m["project"] != LOCAL_SECRET_PROJECT or not dev_mode():
        raise ModelError("the local secret resolver only serves projects/local secrets in SHIPCUBE_DEV_MODE", "forbidden")
    var = "SHIPCUBE_DATA_SECRET_" + re.sub(r"[^A-Za-z0-9]", "_", m["name"]).upper()
    value = os.environ.get(var)
    if value is None:  # the API's DEV_MODE secret store: a JSON file {secret id: value}
        path = os.environ.get("SHIPCUBE_LOCAL_SECRETS_FILE")
        if path and os.path.exists(path):
            with open(path) as f:
                value = json.load(f).get(m["name"])
    if value is None and m["name"].endswith("-warehouse"):  # the local stack's seeded warehouse
        value = os.environ.get("SHIPCUBE_DATA_WAREHOUSE_URL")
    if value is None:
        raise ModelError(f"local secret {ref.resource_name} is not set (env {var})", "no_datasource")
    return value
