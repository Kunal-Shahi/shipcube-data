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


@pytest.fixture(params=["sqlite", "postgres"])
def catalog(request):
    """Every catalog-backed test runs on SQLite and, when SHIPCUBE_TEST_PG_URL is set, on Postgres."""
    if request.param == "sqlite":
        cat = Catalog()
        yield cat
        cat.close()
        return
    if not PG_URL:
        pytest.skip("SHIPCUBE_TEST_PG_URL not set")
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
