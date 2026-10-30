"""Datasource registration and connection guards (red-team issues #3, #4, #5)."""

import pytest

from shipcube_data import Catalog, DataLayer
from shipcube_data.datasources import (Datasource, SecretRef, check_db_host, get_datasource, local_secret_resolver,
                                       open_executor, register_datasource)
from shipcube_data.model import ModelError
from shipcube_data.tools import ToolError
from shipcube_data.warehouses import ExecResult

from .test_tools import WS, agent, console, tools  # noqa: F401  (fixture)

SECRET_PROJECT = "shipcube-control-ks"


def ref(name=None, project=SECRET_PROJECT, version="3", ws=WS):
    return {"id": "0192f3a0-0000-7000-8000-00000000abcd",
            "resource_name": f"projects/{project}/secrets/{name or f'ws-{ws}-db_credential-x'}", "version": version}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("SHIPCUBE_DATA_SECRET_PROJECT", SECRET_PROJECT)
    monkeypatch.delenv("SHIPCUBE_DEV_MODE", raising=False)
    monkeypatch.delenv("SHIPCUBE_DATA_ALLOWED_DB_HOSTS", raising=False)


@pytest.mark.parametrize("who,error", [
    (lambda: agent(role="owner"), "not with a token"),
    (lambda: console(role="developer"), "admin role"),
])
def test_only_console_admins_connect_warehouses(tools, who, error):  # noqa: F811
    with pytest.raises(ToolError, match=error):
        tools.register_datasource(who(), "primary", "postgres", secret_ref=ref(), options={"host": "8.8.8.8"})


@pytest.mark.parametrize("kwargs,error", [
    # secrets (#4)
    ({"kind": "postgres", "options": {"host": "8.8.8.8"}}, "needs a secret_ref"),
    ({"kind": "postgres", "secret_ref": ref(version="latest"), "options": {"host": "8.8.8.8"}}, "version"),
    ({"kind": "postgres", "secret_ref": ref(project="someone-elses-project"), "options": {"host": "8.8.8.8"}}, "secrets project"),
    ({"kind": "postgres", "secret_ref": ref(name="ws-00000000-0000-0000-0000-000000000000-db"), "options": {"host": "8.8.8.8"}}, "belong to this workspace"),
    ({"kind": "postgres", "secret_ref": ref(project="local"), "options": {"host": "8.8.8.8"}}, "only allowed in SHIPCUBE_DEV_MODE"),
    ({"kind": "postgres", "secret_ref": ref(), "options": {"host": "db", "password": "hunter2"}}, "look like credentials"),
    # hosts (#4)
    ({"kind": "postgres", "secret_ref": ref(), "options": {"host": "10.0.0.5"}}, "non-public address"),
    ({"kind": "postgres", "secret_ref": ref(), "options": {"host": "169.254.169.254"}}, "non-public address"),
    ({"kind": "postgres", "secret_ref": ref(), "options": {"host": "localhost"}}, "non-public address"),
    ({"kind": "postgres", "secret_ref": ref(), "options": {"host": "/cloudsql/p:r:i"}}, "socket host"),
    ({"kind": "postgres", "secret_ref": ref(), "options": {"host": "8.8.8.8", "sslmode": "prefer"}}, "verify-full"),
    # BigQuery (#3)
    ({"kind": "bigquery", "options": {"project": "victim-project"}}, "not a verified GCP project"),
    ({"kind": "bigquery", "options": {"project": "acme-data"}, "verified_projects": ["acme-data"]}, "needs a service-account key"),
    ({"kind": "bigquery", "options": {"project": "acme-data", "impersonate_service_account": "sc-api@shipcube-control-ks.iam.gserviceaccount.com"},
      "verified_projects": ["acme-data"]}, "service account in the datasource's project"),
    # DuckDB (#5)
    ({"kind": "duckdb", "options": {"path": "/etc/passwd"}}, "local development only"),
    ({"kind": "oracle", "secret_ref": ref()}, "unsupported warehouse kind"),
])
def test_registration_is_validated(tools, kwargs, error):  # noqa: F811
    with pytest.raises(ToolError, match=error):
        tools.register_datasource(console(role="admin"), "primary", **kwargs)


def test_valid_bigquery_registration_is_marked_verified(tools, runner):  # noqa: F811
    sa = "warehouse-reader@acme-data.iam.gserviceaccount.com"
    with pytest.raises(ToolError, match="model is written in duckdb"):  # the demo model is DuckDB SQL
        tools.register_datasource(console(role="admin"), "primary", "bigquery",
                                  options={"project": "acme-data", "impersonate_service_account": sa}, verified_projects=["acme-data"])
    cat = Catalog(workspace_id=str(WS))
    register_datasource(cat, Datasource(name="primary", kind="bigquery", options={"project": "acme-data", "impersonate_service_account": sa}),
                        "user:x", verified_projects=["acme-data"])
    assert get_datasource(cat).options["verified"] is True


def test_allow_listed_private_hosts(monkeypatch):
    monkeypatch.setenv("SHIPCUBE_DATA_ALLOWED_DB_HOSTS", "postgres,localhost")
    check_db_host("postgres", "disable")
    check_db_host("localhost", "disable")
    with pytest.raises(ModelError, match="non-public"):
        check_db_host("127.0.0.2", "disable")


def test_secret_is_resolved_only_at_connect_time_and_rechecked(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIPCUBE_DATA_ALLOWED_DB_HOSTS", "warehouse.internal")
    cat = Catalog(tmp_path / "c.db", workspace_id=str(WS))
    register_datasource(cat, Datasource(name="primary", kind="postgres", secret_ref=SecretRef(**ref()),
                                        options={"host": "warehouse.internal", "dbname": "w", "user": "reader"}), "user:x")
    stored = cat.db.execute("SELECT * FROM datasources").fetchone()
    assert "hunter2" not in str(dict(stored)) and cat.dialect == "postgres"

    seen = {}

    def resolve(r):
        seen["ref"] = r.resource_name + "@" + r.version
        return "hunter2"

    def fake_pg(ds, secret):
        seen["secret"] = secret
        return lambda sql, limits, query_id: ExecResult(["one"], [(1,)])

    layer = DataLayer.from_catalog(cat, resolve, connectors={"postgres": fake_pg})
    assert layer.execute("SELECT 1", layer.limits, "q_1").rows == [(1,)]
    assert seen == {"ref": ref()["resource_name"] + "@3", "secret": "hunter2"}

    # A row tampered with after registration is refused at connect time.
    cat.db.execute("UPDATE datasources SET secret_resource = 'projects/shipcube-control-ks/secrets/ws-other-db'")
    with pytest.raises(ModelError, match="belong to this workspace"):
        open_executor(cat, resolve, connectors={"postgres": fake_pg})


def test_secret_url_hosts_are_checked_too(monkeypatch):
    from shipcube_data.datasources import postgres_connector

    ds = Datasource(name="primary", kind="postgres", secret_ref=SecretRef(**ref()), options={})
    with pytest.raises(ModelError, match="non-public"):
        postgres_connector(ds, "postgresql://u:p@10.1.2.3/db?sslmode=verify-full")


@pytest.mark.parametrize("extra", ["hostaddr=10.1.2.3", "service=prod", "passfile=/etc/passwd", "options=-c%20statement_timeout%3D0",
                                   "sslrootcert=/etc/ssl/key", "target_session_attrs=any"])
def test_secret_urls_may_only_set_plain_connection_keys(extra):
    """A URL secret can't redirect the connection past check_db_host or read the server's files (shipcube-data #8)."""
    from shipcube_data.datasources import postgres_connector

    ds = Datasource(name="primary", kind="postgres", secret_ref=SecretRef(**ref()), options={})
    with pytest.raises(ModelError, match="may not set") as e:
        postgres_connector(ds, f"postgresql://u:p@db.example.com/db?sslmode=verify-full&{extra}")
    assert e.value.code == "forbidden"


def test_local_secrets_only_in_dev_mode(monkeypatch):
    r = SecretRef(id="x", resource_name=f"projects/local/secrets/ws-{WS}-warehouse", version="1")
    with pytest.raises(ModelError, match="SHIPCUBE_DEV_MODE"):
        local_secret_resolver(r)
    monkeypatch.setenv("SHIPCUBE_DEV_MODE", "1")
    monkeypatch.setenv(f"SHIPCUBE_DATA_SECRET_WS_{str(WS).upper().replace('-', '_')}_WAREHOUSE", "pw")
    assert local_secret_resolver(r) == "pw"


def test_duckdb_datasource_in_dev_mode_is_sealed(tmp_path, runner, monkeypatch):
    import duckdb

    from shipcube_data import SecurityContext, demo

    monkeypatch.setenv("SHIPCUBE_DEV_MODE", "1")
    path = tmp_path / "w.duckdb"
    con = duckdb.connect(str(path))
    demo.build(con, {"acme": (20, 50, 1)})
    con.close()
    register_datasource(runner.catalog, Datasource(name="primary", kind="duckdb", options={"path": str(path)}))
    layer = DataLayer.from_catalog(runner.catalog)
    assert layer.query({"measures": ["orders.count"]}, SecurityContext(tenant="acme"))["rows"][0]["orders__count"] == 50
    with pytest.raises(ModelError, match="file system operations are disabled|Permission"):
        layer.execute("SELECT * FROM read_text('/etc/hosts')", layer.limits, "q_x")


def test_warehouse_connects_lazily(tmp_path, monkeypatch):
    """Opening a workspace's data layer never needs the warehouse; the first query does."""
    monkeypatch.setenv("SHIPCUBE_DATA_ALLOWED_DB_HOSTS", "warehouse.internal")
    cat = Catalog(tmp_path / "c.db", workspace_id=str(WS))
    register_datasource(cat, Datasource(name="primary", kind="postgres", secret_ref=SecretRef(**ref()),
                                        options={"host": "warehouse.internal"}), "user:x")
    calls = []

    def resolve(r):
        calls.append(r)
        raise ModelError("secret unavailable", "no_datasource")

    layer = DataLayer.from_catalog(cat, resolve)
    assert layer.catalog.head() == 0 and calls == []  # opened without touching the secret or warehouse
    with pytest.raises(ModelError, match="secret unavailable"):
        layer.execute("SELECT 1", layer.limits, "q_1")
    assert len(calls) == 1
