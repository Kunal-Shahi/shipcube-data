"""Warehouse connections.

A datasource is created in the console by a person, never by an agent. The catalog stores
only a *reference* to the credential (a row of the security schema's `secret_refs`: id,
Secret Manager resource name, pinned version). The value is fetched at connect time by a
resolver the API supplies, and never stored or logged here.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

from pydantic import BaseModel, ConfigDict, Field

from .catalog import Catalog, _loads, _now
from .compiler import DIALECTS
from .model import ModelError
from .warehouses import BigQueryWarehouse, DuckDBWarehouse, Executor, PostgresWarehouse

_RESOURCE_RE = re.compile(r"^projects/[^/]+/secrets/[A-Za-z0-9_-]+$")
_SECRETISH = re.compile(r"(password|passwd|secret|token|key|credential)", re.I)


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


def register_datasource(catalog: Catalog, ds: Datasource, actor: Optional[str] = None) -> dict[str, Any]:
    """Validate and store a datasource. The catalog's dialect follows the datasource's kind."""
    if ds.kind not in DIALECTS:
        raise ModelError(f"unsupported warehouse kind {ds.kind!r}; pick one of {', '.join(DIALECTS)}", "invalid")
    if ds.kind in {"postgres", "clickhouse"} and ds.secret_ref is None:
        raise ModelError(f"a {ds.kind} datasource needs a secret_ref", "invalid")
    if ds.kind == "bigquery" and not ds.options.get("project"):
        raise ModelError("a bigquery datasource needs options.project", "invalid")
    if ds.secret_ref and not _RESOURCE_RE.match(ds.secret_ref.resource_name):
        raise ModelError("secret_ref.resource_name must look like projects/<p>/secrets/<name>", "invalid")
    bad = [k for k in ds.options if _SECRETISH.search(k)]
    if bad:
        raise ModelError(f"options {bad} look like credentials; put them in Secret Manager and pass a secret_ref", "invalid")
    has_model = catalog.head() > 0 and catalog.load().cubes
    if has_model and catalog.dialect != ds.kind:
        raise ModelError(f"the model is written in {catalog.dialect} SQL; a {ds.kind} warehouse needs a new catalog", "invalid")
    with catalog.transaction() as cat:
        cat.db.execute(
            "INSERT INTO datasources (name, kind, secret_ref_id, secret_resource, secret_version, options, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (name) DO UPDATE SET kind = excluded.kind, "
            "secret_ref_id = excluded.secret_ref_id, secret_resource = excluded.secret_resource, "
            "secret_version = excluded.secret_version, options = excluded.options, "
            "created_by = excluded.created_by, created_at = excluded.created_at",
            (ds.name, ds.kind, ds.secret_ref.id if ds.secret_ref else None,
             ds.secret_ref.resource_name if ds.secret_ref else None, ds.secret_ref.version if ds.secret_ref else None,
             json.dumps(ds.options, sort_keys=True), actor, _now()),
        )
        cat.set_setting("dialect", ds.kind)
        cat.audit("data.datasource.register", actor, target_type="datasource", target_id=ds.name, kind=ds.kind,
                  secret_ref_id=ds.secret_ref.id if ds.secret_ref else None)
    return {"name": ds.name, "kind": ds.kind, "secret_ref_id": ds.secret_ref.id if ds.secret_ref else None}


def get_datasource(catalog: Catalog, name: str = "primary") -> Datasource:
    r = catalog.db.execute("SELECT * FROM datasources WHERE name = ?", (name,)).fetchone()
    if r is None:
        raise ModelError(f"no datasource {name!r}; connect a warehouse in the console", "no_datasource")
    ref = SecretRef(id=r["secret_ref_id"], resource_name=r["secret_resource"], version=r["secret_version"]) if r["secret_ref_id"] else None
    return Datasource(name=r["name"], kind=r["kind"], secret_ref=ref, options=_loads(r["options"]))


def duckdb_connector(ds: Datasource, secret: Optional[str]) -> Executor:
    import duckdb

    return DuckDBWarehouse(duckdb.connect(ds.options.get("path", ":memory:"), read_only=bool(ds.options.get("path"))))


def postgres_connector(ds: Datasource, secret: Optional[str]) -> Executor:
    """Cloud SQL Postgres. The secret is a full libpq URL, or just the password for options
    host / port / dbname / user / sslmode (host may be /cloudsql/<project>:<region>:<instance>)."""
    if secret and secret.startswith(("postgres://", "postgresql://")):
        return PostgresWarehouse(secret)
    from psycopg.conninfo import make_conninfo

    o = ds.options
    conninfo = make_conninfo(host=o.get("host"), port=o.get("port"), dbname=o.get("dbname"), user=o.get("user"),
                             password=secret, sslmode=o.get("sslmode", "prefer"))
    return PostgresWarehouse(conninfo)


def bigquery_connector(ds: Datasource, secret: Optional[str]) -> Executor:
    """BigQuery. Without a secret the runtime service account (ADC / workload identity) is used."""
    return BigQueryWarehouse(project=ds.options["project"], location=ds.options.get("location"), credentials_json=secret)


DEFAULT_CONNECTORS: dict[str, Connector] = {
    "duckdb": duckdb_connector, "postgres": postgres_connector, "bigquery": bigquery_connector,
}


def open_executor(catalog: Catalog, resolve_secret: Optional[SecretResolver] = None, name: str = "primary",
                  connectors: Optional[dict[str, Connector]] = None) -> Executor:
    """Connect to a registered datasource. Built-in connectors: duckdb, postgres, bigquery."""
    ds = get_datasource(catalog, name)
    connector = {**DEFAULT_CONNECTORS, **(connectors or {})}.get(ds.kind)
    if connector is None:
        raise ModelError(f"no connector registered for {ds.kind}", "no_datasource")
    secret = None
    if ds.secret_ref:
        if resolve_secret is None:
            raise ModelError("this datasource needs a secret resolver", "no_datasource")
        secret = resolve_secret(ds.secret_ref)
    return connector(ds, secret)
