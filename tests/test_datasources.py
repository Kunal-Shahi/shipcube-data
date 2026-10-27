import pytest

from shipcube_data import Catalog, DataLayer
from shipcube_data.datasources import get_datasource, open_executor

from .test_tools import WS, agent, console, tools  # noqa: F401  (fixture)
from shipcube_data.tools import ToolError

REF = {"id": "0192f3a0-0000-7000-8000-00000000abcd", "resource_name": "projects/shipcube-control/secrets/ws-1-db_credential-x", "version": "3"}


def test_console_admin_registers_from_a_secret_ref(tools, runner):  # noqa: F811
    out = tools.register_datasource(console(role="admin"), "primary", "duckdb", options={"path": ":memory:"})
    assert out == {"name": "primary", "kind": "duckdb", "secret_ref_id": None}
    log = runner.catalog.audit_log()[-1]
    assert log["action"] == "data.datasource.register" and log["actor_type"] == "user"


@pytest.mark.parametrize("who,error", [
    (lambda: agent(role="owner"), "not with a token"),
    (lambda: console(role="developer"), "admin role"),
])
def test_only_console_admins_connect_warehouses(tools, who, error):  # noqa: F811
    with pytest.raises(ToolError, match=error):
        tools.register_datasource(who(), "primary", "duckdb")


@pytest.mark.parametrize("kwargs,error", [
    ({"kind": "postgres"}, "needs a secret_ref"),
    ({"kind": "postgres", "secret_ref": {**REF, "version": "latest"}}, "version"),
    ({"kind": "postgres", "secret_ref": {**REF, "resource_name": "my-password"}}, "projects/<p>/secrets"),
    ({"kind": "postgres", "secret_ref": REF, "options": {"host": "db", "password": "hunter2"}}, "look like credentials"),
    ({"kind": "oracle", "secret_ref": REF}, "unsupported warehouse kind"),
    ({"kind": "bigquery", "secret_ref": REF}, "model is written in duckdb"),
])
def test_registration_is_validated(tools, kwargs, error):  # noqa: F811
    with pytest.raises(ToolError, match=error):
        tools.register_datasource(console(role="admin"), "primary", **kwargs)


def test_secret_is_resolved_only_at_connect_time(tmp_path):
    cat = Catalog(tmp_path / "c.db")
    from shipcube_data.datasources import Datasource, SecretRef, register_datasource

    register_datasource(cat, Datasource(name="primary", kind="postgres", secret_ref=SecretRef(**REF), options={"host": "10.0.0.5"}), "user:x")
    stored = cat.db.execute("SELECT * FROM datasources").fetchone()
    assert "hunter2" not in str(tuple(stored)) and cat.dialect == "postgres"

    seen = {}

    def resolve(ref):
        seen["ref"] = ref.resource_name + "@" + ref.version
        return "postgres://app:hunter2@10.0.0.5/warehouse"

    def fake_pg(ds, secret):
        seen["secret"] = secret
        return lambda sql: (["one"], [(1,)])

    layer = DataLayer.from_catalog(cat, resolve, connectors={"postgres": fake_pg})
    assert layer.execute("SELECT 1") == (["one"], [(1,)])
    assert seen == {"ref": REF["resource_name"] + "@3", "secret": "postgres://app:hunter2@10.0.0.5/warehouse"}
    assert get_datasource(cat).secret_ref.id == REF["id"]


def test_duckdb_datasource_end_to_end(tmp_path, runner):
    import duckdb

    from shipcube_data import demo
    from shipcube_data.datasources import Datasource, register_datasource

    path = tmp_path / "w.duckdb"
    con = duckdb.connect(str(path))
    demo.build(con, {"acme": (20, 50, 1)})
    con.close()
    register_datasource(runner.catalog, Datasource(name="primary", kind="duckdb", options={"path": str(path)}))
    layer = DataLayer.from_catalog(runner.catalog)
    from shipcube_data import SecurityContext

    assert layer.query({"measures": ["orders.count"]}, SecurityContext(tenant="acme"))["rows"][0]["orders__count"] == 50
    with pytest.raises(Exception):
        open_executor(Catalog(), name="primary")
