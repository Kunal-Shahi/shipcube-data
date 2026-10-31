"""Governed SQL: any SELECT over cube names, with tenant routing, grants and row policies enforced by rewriting."""

import pytest
import sqlglot

from shipcube_data.compiler import SecurityContext
from shipcube_data.model import ModelError
from shipcube_data.warehouses import WarehouseError

REGIONAL = SecurityContext(tenant="acme", roles=["regional_manager"], attrs={"region": "South"})


def rows(dl, sql, ctx):
    return dl.governed_sql(sql, ctx)["rows"]


def test_same_numbers_as_typed_queries(dl, acme):
    typed = {r["orders__status"]: r["orders__count"]
             for r in dl.query({"measures": ["orders.count"], "dimensions": ["orders.status"]}, acme)["rows"]}
    sql = {r["status"]: r["n"] for r in rows(dl, "SELECT status, count(*) AS n FROM orders GROUP BY status", acme)}
    assert sql == typed
    typed_rev = {r["states__region"]: r["order_items__revenue"]
                 for r in dl.query({"measures": ["order_items.revenue"], "dimensions": ["states.region"]}, acme)["rows"]}
    joined = rows(dl, """SELECT s.region, SUM(i.revenue) AS revenue
                         FROM order_items i JOIN orders o USING (order_id)
                         JOIN customers c ON c.customer_id = o.customer_id JOIN states s ON s.code = c.state
                         GROUP BY 1""", acme)
    assert {r["region"]: r["revenue"] for r in joined} == typed_rev


def test_measure_inputs_reproduce_filtered_and_distinct_measures(dl, acme):
    typed = dl.query({"measures": ["orders.late_count", "orders.unique_orders"]}, acme)["rows"][0]
    got = rows(dl, "SELECT COUNT(late_count) AS late, COUNT(DISTINCT unique_orders) AS uniq FROM orders", acme)[0]
    assert (got["late"], got["uniq"]) == (typed["orders__late_count"], typed["orders__unique_orders"])


def test_anything_the_warehouse_can_do_on_top(dl, acme):
    out = rows(dl, """
        WITH m AS (SELECT date_trunc('month', purchased_at) AS mo, count(*) AS n FROM orders GROUP BY 1)
        SELECT mo, n, avg(n) OVER (ORDER BY mo ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS ma3,
               SUM(CASE WHEN n > 50 THEN 1 ELSE 0 END) OVER () AS busy_months
        FROM m ORDER BY mo""", acme)
    assert len(out) == 20 and out[2]["ma3"] == pytest.approx((out[0]["n"] + out[1]["n"] + out[2]["n"]) / 3)


def test_row_policies_apply_inside_the_rewrite(dl, acme):
    typed = dl.query({"measures": ["orders.count"]}, REGIONAL)["rows"][0]["orders__count"]
    assert rows(dl, "SELECT count(*) AS n FROM orders", REGIONAL)[0]["n"] == typed < 1500
    regions = rows(dl, "SELECT DISTINCT s.region FROM orders o JOIN customers c ON c.customer_id = o.customer_id "
                       "JOIN states s ON s.code = c.state", REGIONAL)
    assert [r["region"] for r in regions] == ["South"]
    # A policy needing context the caller lacks is refused, as in a typed query.
    no_attr = SecurityContext(tenant="acme", roles=["regional_manager"])
    with pytest.raises(ModelError, match="no 'region'"):
        dl.governed_sql("SELECT count(*) AS n FROM orders", no_attr)


def test_tenants_stay_apart(dl, acme, globex):
    assert rows(dl, "SELECT count(*) AS n FROM orders", acme)[0]["n"] == 1500
    assert rows(dl, "SELECT count(*) AS n FROM orders", globex)[0]["n"] == 500


def test_only_visible_members_are_columns(dl, acme):
    cols = set(rows(dl, "SELECT * FROM order_items LIMIT 1", acme)[0])
    assert "freight" not in cols and "freight_value" not in cols and "gmv" not in cols
    assert {"revenue", "order_id", "product_id"} <= cols
    finance = SecurityContext(tenant="acme", roles=["finance"])
    assert "freight" in rows(dl, "SELECT * FROM order_items LIMIT 1", finance)[0]
    for sql in ["SELECT sum(freight) FROM order_items",              # hidden member
                "SELECT sum(freight_value) FROM order_items",        # its physical column
                "SELECT order_purchase_timestamp FROM orders"]:      # any physical column
        with pytest.raises(WarehouseError):
            dl.governed_sql(sql, acme)


def test_whole_row_references_see_only_the_governed_view(dl, acme):
    text = rows(dl, "SELECT CAST(i AS VARCHAR) AS t FROM order_items i LIMIT 1", acme)[0]["t"]
    assert "freight" not in text


@pytest.mark.parametrize("sql,match", [
    ("SELECT count(*) FROM tenant_globex.customers", "schema-qualified"),
    ("SELECT * FROM information_schema.tables", "schema-qualified"),
    ("SELECT * FROM pg_user", "unknown table"),
    ("SELECT * FROM read_csv('/etc/passwd')", "may not call read_csv|table functions"),
    ("SELECT pg_read_file('/etc/passwd')", "may not call"),
    ("SELECT query_to_xml('select 1', true, true, '')", "may not call"),
    ("SELECT set_config('app.workspace_id', 'x', false)", "may not call"),
    ("WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r) SELECT * FROM r", "recursive"),
    ("WITH orders AS (SELECT 1 AS x) SELECT * FROM orders", "named like a cube"),
    ("DELETE FROM orders", "single SELECT|DELETE"),
    ("SELECT 1; SELECT 2", "exactly one"),
])
def test_refused(dl, acme, sql, match):
    with pytest.raises(ModelError, match=match):
        dl.governed_sql(sql, acme)


def test_ctes_and_aliases_are_not_confused_with_cubes(dl, acme):
    out = rows(dl, "WITH x AS (SELECT status FROM orders) SELECT count(*) AS n FROM x AS customers", acme)
    assert out[0]["n"] == 1500


@pytest.mark.parametrize("dialect", ["postgres", "bigquery", "clickhouse"])
def test_rewrites_for_each_dialect(dl, acme, dialect):
    from shipcube_data import govsql

    safe, _ = govsql.rewrite("SELECT status, count(*) AS n FROM orders GROUP BY status", dl.catalog.load(), acme,
                             dialect, dl.dialect, 100)
    sqlglot.parse_one(safe, read=dialect)
    assert "tenant_acme.orders_2018_01" in safe.replace("`", "").replace('"', "")
