"""Cloud SQL Postgres adapter against a real Postgres (CI service container, or SHIPCUBE_TEST_PG_URL locally)."""

import uuid

import psycopg
import pytest

from shipcube_data import demo
from shipcube_data.engine import DataLayer
from shipcube_data.warehouses import PostgresWarehouse, QueryLimits, WarehouseError

from ..conftest import PG_URL
from .common import assert_same_answers, model_catalog, reference_layer

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not PG_URL, reason="SHIPCUBE_TEST_PG_URL not set")]
TENANTS = {"acme": (200, 700, 7), "globex": (80, 250, 11)}


@pytest.fixture(scope="module")
def pg_warehouse():
    prefix = f"it_{uuid.uuid4().hex[:8]}_"
    with psycopg.connect(PG_URL, autocommit=True) as con:
        demo.build(con, TENANTS, prefix=prefix)
    yield prefix
    with psycopg.connect(PG_URL, autocommit=True) as con:
        for s in ("shared", "tenant_acme", "tenant_globex"):
            con.execute(f"DROP SCHEMA IF EXISTS {prefix}{s} CASCADE")


def test_same_answers_as_reference(pg_warehouse):
    layer = DataLayer(model_catalog("postgres", pg_warehouse), PostgresWarehouse(PG_URL))
    assert_same_answers(layer, reference_layer(TENANTS))


def test_partition_pruning_reaches_postgres(pg_warehouse):
    layer = DataLayer(model_catalog("postgres", pg_warehouse), PostgresWarehouse(PG_URL))
    from shipcube_data import SecurityContext

    res = layer.query({"measures": ["orders.count"], "time": {"dimension": "orders.purchased_at", "range": ["2018-01-05", "2018-01-20"]}},
                      SecurityContext(tenant="acme"))
    assert res["partitions"]["orders"]["tables"] == ["orders_2018_01"]
    assert res["stats"]["planner_cost"] > 0 and res["query_id"].startswith("q_")


def test_timeout_is_enforced():
    with pytest.raises(WarehouseError) as e:
        PostgresWarehouse(PG_URL)("SELECT pg_sleep(3)", QueryLimits(timeout_s=0.3), "q_timeout")
    assert e.value.code == "query_timeout"


def test_cost_guard():
    sql = "SELECT count(*) FROM generate_series(1, 10000000) a, generate_series(1, 1000) b"
    with pytest.raises(WarehouseError) as e:
        PostgresWarehouse(PG_URL)(sql, QueryLimits(max_cost=1000), "q_cost")
    assert e.value.code == "cost_limit" and "exceeds the limit" in str(e.value)


def test_row_limit_truncates():
    res = PostgresWarehouse(PG_URL)("SELECT g FROM generate_series(1, 50) g", QueryLimits(max_rows=10), "q_rows")
    assert len(res.rows) == 10 and res.truncated


def test_read_only():
    # A SELECT with a side effect: still refused, because every query runs in a READ ONLY transaction.
    seq = f"public.ro_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(PG_URL, autocommit=True) as con:
        con.execute(f"CREATE SEQUENCE {seq}")
    try:
        with pytest.raises(WarehouseError) as e:
            PostgresWarehouse(PG_URL)(f"SELECT nextval('{seq}')", QueryLimits(), "q_ro")
        assert e.value.code == "warehouse_error" and "read-only" in str(e.value)
    finally:
        with psycopg.connect(PG_URL, autocommit=True) as con:
            con.execute(f"DROP SEQUENCE {seq}")


def test_reconnects_after_a_dropped_connection():
    wh = PostgresWarehouse(PG_URL)
    assert wh("SELECT 1", QueryLimits(), "q_a").rows == [(1,)]
    wh.conn.close()
    assert wh("SELECT 2", QueryLimits(), "q_b").rows == [(2,)]
