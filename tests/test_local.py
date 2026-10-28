"""The local-stack path end to end: seed (warehouse + catalog + model) then the MCP smoke sequence."""

import uuid

import pytest

from shipcube_data import local

from .conftest import PG_URL


@pytest.fixture
def dev(monkeypatch):
    monkeypatch.setenv("SHIPCUBE_DEV_MODE", "1")
    for var in ("SHIPCUBE_DATA_ALLOWED_DB_HOSTS", "SHIPCUBE_DATA_WAREHOUSE_URL"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def run(dev, **env):
    for k, v in env.items():
        dev.setenv(k, v)
    s = local.Settings.from_env()
    first = local.seed(s)
    again = local.seed(s)  # idempotent
    assert first["model_version"] == again["model_version"] == 5 and again["applied"] == []
    steps = local.smoke(s, say=lambda line: None)
    assert all(st.ok for st in steps), [(st.name, st.detail) for st in steps if not st.ok]
    assert [st.name for st in steps][-1] == "agent cannot apply"
    return s


def test_sqlite_catalog_duckdb_warehouse(dev, tmp_path):
    run(dev, SHIPCUBE_DATA_CATALOG_URL=str(tmp_path / "catalog.db"), SHIPCUBE_DATA_WORKSPACE=str(uuid.uuid4()),
        SHIPCUBE_DATA_DUCKDB_PATH=str(tmp_path / "olist.duckdb"))


@pytest.mark.skipif(not PG_URL, reason="SHIPCUBE_TEST_PG_URL not set")
def test_postgres_catalog_postgres_warehouse(dev):
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    base, _, _ = PG_URL.rpartition("/")  # postgresql://user[:pw]@host:port
    run_id = uuid.uuid4().hex[:8]
    catalog = f"{base}/it_catalog_{run_id}"
    warehouse = f"{base}/it_warehouse_{run_id}?sslmode=disable"
    host = conninfo_to_dict(PG_URL).get("host", "localhost")
    try:
        run(dev, SHIPCUBE_DATA_CATALOG_URL=catalog, SHIPCUBE_DATA_WORKSPACE=str(uuid.uuid4()), SHIPCUBE_DATA_WAREHOUSE="postgres",
            SHIPCUBE_DATA_WAREHOUSE_URL=warehouse, SHIPCUBE_DATA_ALLOWED_DB_HOSTS=host)
    finally:
        with psycopg.connect(PG_URL, autocommit=True) as con:
            for db in (f"it_catalog_{run_id}", f"it_warehouse_{run_id}"):
                con.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


def test_tools_refuse_unseeded_workspaces(dev, tmp_path):
    dev.setenv("SHIPCUBE_DATA_CATALOG_URL", str(tmp_path / "c.db"))
    from shipcube_data.identity import Caller
    from shipcube_data.tools import ToolError

    tools = local.tools_from_env()
    caller = Caller(workspace_id=uuid.uuid4(), via="mcp", email="a@example.com", user_id=uuid.uuid4(),
                    membership_role="developer", token_id=uuid.uuid4(), scopes=["data:read"])
    with pytest.raises(ToolError) as e:
        tools.call("data_list_cubes", {}, caller)
    assert e.value.code in {"catalog_not_migrated", "no_datasource"}


def test_forget_workspace_drops_only_that_workspaces_layers(dev, tmp_path):
    dev.setenv("SHIPCUBE_DATA_CATALOG_URL", str(tmp_path / "c.db"))
    tools = local.tools_from_env()
    a, b = uuid.uuid4(), uuid.uuid4()
    la, lb = tools.layer_for(a), tools.layer_for(b)
    assert tools.layer_for(a) is la
    tools.forget_workspace(a)
    assert tools.layer_for(a) is not la and tools.layer_for(b) is lb
