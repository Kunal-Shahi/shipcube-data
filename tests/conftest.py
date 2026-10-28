import os
import uuid

import duckdb
import pytest

from shipcube_data import demo
from shipcube_data.catalog import Catalog
from shipcube_data.compiler import SecurityContext
from shipcube_data.engine import DataLayer, duckdb_executor
from shipcube_data.migrate import Runner, load_dir


@pytest.fixture(scope="session")
def warehouse():
    con = duckdb.connect()
    demo.build(con)
    yield con
    con.close()


PG_URL = os.environ.get("SHIPCUBE_TEST_PG_URL")  # e.g. postgresql://postgres@localhost:5432/postgres


# The workspace the shared-layout catalog fixture runs as (test_tools' WS; every test rolls back).
SHARED_WS = "0192f3a0-0000-7000-8000-000000000001"


@pytest.fixture(scope="session")
def shared_db():
    """A database shaped like ShipCube's: app_workspace() in public, the shared catalog created by the
    owner, and a non-owner role without BYPASSRLS that only has DML (like shipcube_app)."""
    if not PG_URL:
        pytest.skip("SHIPCUBE_TEST_PG_URL not set")
    import psycopg

    from shipcube_data.db import Db

    run = uuid.uuid4().hex[:8]
    dbname, role = f"scdata_shared_{run}", f"scdata_app_{run}"
    base = PG_URL.rsplit("/", 1)[0]
    with psycopg.connect(PG_URL, autocommit=True) as c:
        c.execute(f"CREATE DATABASE {dbname}")
        c.execute(f"CREATE ROLE {role} NOLOGIN NOBYPASSRLS")
    url = f"{base}/{dbname}"
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("CREATE FUNCTION app_workspace() RETURNS uuid LANGUAGE sql STABLE AS "
                  "$$ SELECT nullif(current_setting('app.workspace_id', true), '')::uuid $$")
    owner = Db(url, shared=True)
    owner.migrate_schema()  # what `shipcube-data catalog-migrate --shared` runs as the migration role
    owner.close()
    with psycopg.connect(url, autocommit=True) as c:
        c.execute(f"GRANT USAGE ON SCHEMA catalog TO {role}")
        c.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA catalog TO {role}")
        c.execute(f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA catalog TO {role}")
    yield {"url": url, "role": role}
    with psycopg.connect(PG_URL, autocommit=True) as c:
        c.execute(f"DROP DATABASE {dbname} WITH (FORCE)")
        c.execute(f"DROP ROLE {role}")


def app_session(shared_db, workspace_id, commit: bool = True):
    """Like the API's db.tx(caller): SET LOCAL ROLE to the app role and set app.workspace_id."""
    import contextlib

    import psycopg

    @contextlib.contextmanager
    def tx():
        with psycopg.connect(shared_db["url"]) as conn:
            with conn.transaction(force_rollback=not commit):
                conn.execute(f"SET LOCAL ROLE {shared_db['role']}")
                if workspace_id is not None:
                    conn.execute("SELECT set_config('app.workspace_id', %s, true)", (str(workspace_id),))
                yield conn

    return tx()


@pytest.fixture(params=["sqlite", "postgres", "shared"])
def catalog(request):
    """Every catalog-backed test runs on SQLite and, when SHIPCUBE_TEST_PG_URL is set, on Postgres in
    both layouts: a schema per workspace, and the shared catalog under FORCE RLS as a non-owner role."""
    if request.param == "sqlite":
        cat = Catalog()
        yield cat
        cat.close()
        return
    if not PG_URL:
        pytest.skip("SHIPCUBE_TEST_PG_URL not set")
    if request.param == "shared":
        shared = request.getfixturevalue("shared_db")
        with app_session(shared, SHARED_WS, commit=False) as conn:
            yield Catalog.on_connection(conn, SHARED_WS)
        return
    cat = Catalog(PG_URL, workspace_id=str(uuid.uuid4()))
    yield cat
    cat.db.execute(f'DROP SCHEMA "{cat.db.schema}" CASCADE')
    cat.close()


@pytest.fixture
def runner(catalog):
    r = Runner(catalog)
    r.apply_all(load_dir(demo.MIGRATIONS), actor="test")
    return r


@pytest.fixture
def dl(runner, warehouse):
    return DataLayer(runner.catalog, duckdb_executor(warehouse))


@pytest.fixture
def acme():
    return SecurityContext(tenant="acme", user="ana@acme.test", roles=["analyst"])


@pytest.fixture
def globex():
    return SecurityContext(tenant="globex", user="gil@globex.test", roles=["analyst"])


def scalar(con, sql):
    return con.execute(sql).fetchone()[0]


ORDERS_ACME = " UNION ALL ".join(f"SELECT * FROM tenant_acme.orders_{y:04d}_{m:02d}" for y, m in demo.MONTHS)
