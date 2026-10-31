"""The shared catalog (ShipCube's own database, schema `catalog`) under FORCE ROW LEVEL SECURITY.

Run as a non-owner role without BYPASSRLS whose transactions set app.workspace_id, like the API's
db.tx(caller). These are security's requirements for the catalog (Oct 29)."""

import threading
import uuid

import psycopg
import pytest

from shipcube_data import demo
from shipcube_data.catalog import Catalog
from shipcube_data.identity import Caller
from shipcube_data.migrate import MigrationError, Runner, load_dir
from shipcube_data.model import ModelError
from shipcube_data.ops import Migration

from .conftest import PG_URL, app_session

pytestmark = pytest.mark.skipif(not PG_URL, reason="SHIPCUBE_TEST_PG_URL not set")


def seed(shared_db, ws):
    with app_session(shared_db, ws) as conn:
        Runner(Catalog.on_connection(conn, ws)).apply_all(load_dir(demo.MIGRATIONS), actor="test")


@pytest.fixture
def two(shared_db):
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    seed(shared_db, a)
    return a, b


def test_workspaces_see_only_their_own_catalog(shared_db, two):
    a, b = two
    with app_session(shared_db, a) as conn:
        assert Catalog.on_connection(conn, a).head() == 6
    with app_session(shared_db, b) as conn:
        cat = Catalog.on_connection(conn, b)
        assert cat.head() == 0 and cat.load().cubes == {} and cat.migrations() == []


def test_fails_closed_without_a_workspace_context(shared_db, two):
    a, _ = two
    with app_session(shared_db, None) as conn:  # app.workspace_id never set: app_workspace() is NULL
        cat = Catalog.on_connection(conn, a)  # even when the code asks for workspace A explicitly
        assert cat.head() == 0 and cat.load().cubes == {}
        assert conn.execute("SELECT count(*) FROM catalog.cubes").fetchone()[0] == 0


def test_a_wrong_workspace_in_code_cannot_read_or_write_another_tenant(shared_db, two):
    a, b = two
    with app_session(shared_db, b) as conn:  # the verified caller is in B ...
        cat = Catalog.on_connection(conn, a)  # ... and buggy code asks for A
        assert cat.head() == 0 and cat.load().cubes == {}
        with pytest.raises(psycopg.errors.InsufficientPrivilege):  # WITH CHECK: rows for A can't be written from B
            cat.set_setting("dialect", "postgres")


def test_requests_never_run_ddl(shared_db):
    ws = str(uuid.uuid4())
    with app_session(shared_db, ws) as conn:
        cat = Catalog.on_connection(conn, ws)
        with pytest.raises(ModelError, match="migration role"):
            cat.db.migrate_schema()
    with psycopg.connect(PG_URL) as conn:  # a database without the catalog schema: refused, nothing created
        with pytest.raises(ModelError) as e:
            Catalog.on_connection(conn, ws)
        assert e.value.code == "catalog_not_migrated"
        assert conn.execute("SELECT count(*) FROM pg_namespace WHERE nspname = 'catalog'").fetchone()[0] == 0


def test_tools_run_in_the_hosts_transaction(shared_db, two, tmp_path, monkeypatch):
    """tools_from_env(catalog_session=...): what ShipCube's API and MCP server mount."""
    import duckdb

    from shipcube_data.datasources import Datasource, register_datasource
    from shipcube_data.local import tools_from_env

    a, b = two
    monkeypatch.setenv("SHIPCUBE_DEV_MODE", "1")
    path = tmp_path / "w.duckdb"
    con = duckdb.connect(str(path))
    demo.build(con, {"acme": (30, 80, 3)})
    con.close()
    with app_session(shared_db, a) as conn:
        cat = Catalog.on_connection(conn, a)
        register_datasource(cat, Datasource(name="primary", kind="duckdb", options={"path": str(path)}), "user:x")
        cat.set_attribute("domain", "example.com", "tenant", "acme", by="user:x")

    tools = tools_from_env(catalog_session=lambda caller: app_session(shared_db, str(caller.workspace_id)))

    def agent(ws):
        return Caller(workspace_id=uuid.UUID(ws), via="mcp", email="ana@example.com", user_id=uuid.uuid4(),
                      membership_role="developer", token_id=uuid.uuid4(), scopes=["data:read", "data:migrate"])

    assert len(tools.call("data_list_cubes", {}, agent(a))["cubes"]) == 5
    assert tools.call("data_list_cubes", {}, agent(b))["cubes"] == []
    rows = tools.call("data_query", {"query": {"measures": ["orders.count"]}}, agent(a))["rows"]
    assert rows == [{"orders__count": 80}]
    mid = tools.call("data_propose_migration", {"description": "Buyers", "ops": [
        {"op": "add_measure", "name": "orders.buyers", "sql": "customer_id", "agg": "count_distinct"}]}, agent(a))["id"]
    with app_session(shared_db, a) as conn:
        assert Catalog.on_connection(conn, a).migration(mid)["status"] == "proposed"  # committed by the session
    with app_session(shared_db, b) as conn:
        assert Catalog.on_connection(conn, b).migration(mid) is None


def test_concurrent_appliers_in_one_workspace_serialise(shared_db, two):
    a, _ = two
    migs = [Migration.model_validate({"id": "0006_x", "base_version": 6, "ops": [
                {"op": "change_sql", "member": "order_items.revenue", "sql": "price * 1.0"}]}),
            Migration.model_validate({"id": "0007_y", "base_version": 6, "ops": [
                {"op": "rename_member", "member": "order_items.revenue", "to": "sales"}]})]
    barrier, results = threading.Barrier(2), {}

    def run(m):
        with app_session(shared_db, a) as conn:
            r = Runner(Catalog.on_connection(conn, a))
            barrier.wait()
            try:
                results[m.id] = r.apply(m, actor="test")["version"]
            except MigrationError as e:
                results[m.id] = e.code

    threads = [threading.Thread(target=run, args=(m,)) for m in migs]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert sorted(map(str, results.values())) == ["7", "conflict"]


def test_migration_role_without_create_on_the_database(tmp_path):
    """Like Cloud SQL: the migrator logs in, acts as a NOLOGIN owner role, and has no CREATE on the
    database. bootstrap.sql provisions schema `catalog` once; catalog-migrate then only creates tables."""
    from shipcube_data.db import migrate_shared_catalog

    run = uuid.uuid4().hex[:8]
    dbname, owner, migrator = f"scdata_boot_{run}", f"scdata_owner_{run}", f"scdata_migrator_{run}"
    base = PG_URL.rsplit("/", 1)[0]
    with psycopg.connect(PG_URL, autocommit=True) as c:
        c.execute(f"CREATE DATABASE {dbname}")
        c.execute(f"CREATE ROLE {owner} NOLOGIN")
        c.execute(f"CREATE ROLE {migrator} LOGIN PASSWORD '{run}'")  # CI's Postgres requires a password
        c.execute(f"GRANT {owner} TO {migrator}")
        c.execute(f"ALTER ROLE {migrator} SET role = '{owner}'")
        c.execute("DO $$ BEGIN CREATE ROLE shipcube_app NOLOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$")
    admin_url = f"{base}/{dbname}"
    migrator_url = f"{base.split('//', 1)[0]}//{migrator}:{run}@{base.rsplit('@', 1)[1]}/{dbname}"
    try:
        with psycopg.connect(admin_url, autocommit=True) as c:  # bootstrap.sql's part, as the administrator
            c.execute("REVOKE CREATE ON DATABASE " + dbname + " FROM PUBLIC")
            c.execute(f"GRANT USAGE, CREATE ON SCHEMA public TO {owner}")
            c.execute("CREATE FUNCTION app_workspace() RETURNS uuid LANGUAGE sql STABLE AS "
                      "$$ SELECT nullif(current_setting('app.workspace_id', true), '')::uuid $$")
        with pytest.raises(ModelError) as e:  # without the schema provisioned: a clear error, not a crash
            migrate_shared_catalog(migrator_url)
        assert e.value.code == "catalog_not_migrated" and "bootstrap.sql" in e.value.hint
        with psycopg.connect(admin_url, autocommit=True) as c:
            c.execute("CREATE SCHEMA catalog")
            c.execute(f"GRANT USAGE, CREATE ON SCHEMA catalog TO {owner}")
        assert migrate_shared_catalog(migrator_url) == ["0001_catalog.sql", "0002_ledger_read_only.sql"]
        assert migrate_shared_catalog(migrator_url) == []  # idempotent
        with psycopg.connect(admin_url) as c:
            owners = {r[0] for r in c.execute("SELECT tableowner FROM pg_tables WHERE schemaname = 'catalog'")}
            privs = dict(c.execute(
                "SELECT t, has_table_privilege('shipcube_app', t, 'SELECT') AND NOT "
                "(has_table_privilege('shipcube_app', t, 'INSERT') OR has_table_privilege('shipcube_app', t, 'UPDATE') "
                "OR has_table_privilege('shipcube_app', t, 'DELETE') OR has_table_privilege('shipcube_app', t, 'TRUNCATE')) "
                "FROM unnest(ARRAY['catalog.catalog_schema_migrations', 'catalog.settings']) t").fetchall())
        assert owners == {owner}
        # The service roles read the ledger but can't write it; catalog tables stay writable.
        assert privs == {"catalog.catalog_schema_migrations": True, "catalog.settings": False}
    finally:
        with psycopg.connect(PG_URL, autocommit=True) as c:
            c.execute(f"DROP DATABASE IF EXISTS {dbname} WITH (FORCE)")
            c.execute(f"DROP ROLE IF EXISTS {migrator}")
            c.execute(f"DROP ROLE IF EXISTS {owner}")
