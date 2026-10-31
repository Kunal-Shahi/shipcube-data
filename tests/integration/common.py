"""Shared checks: the same governed queries must return the same answers on every warehouse."""

from datetime import datetime
from decimal import Decimal

import duckdb

from shipcube_data import demo
from shipcube_data.catalog import Catalog
from shipcube_data.compiler import SecurityContext
from shipcube_data.engine import DataLayer, duckdb_executor
from shipcube_data.migrate import Runner, load_dir
from shipcube_data.ops import Migration

QUERIES = {
    "monthly_counts_pruned": {"measures": ["orders.count", "orders.late_rate"],
                              "time": {"dimension": "orders.purchased_at", "granularity": "month", "range": ["2017-11-15", "2018-02-10"]}},
    "multi_fact_by_region": {"measures": ["orders.count", "order_items.revenue"], "dimensions": ["states.region"],
                             "order": [{"member": "states.region"}]},
    "category_distinct_orders": {"measures": ["orders.unique_orders"], "dimensions": ["products.category"],
                                 "order": [{"member": "products.category"}]},
    "filters_and_segments": {"measures": ["orders.count"], "segments": ["orders.delivered"], "dimensions": ["customers.state"],
                             "filters": [{"or": [{"member": "customers.city", "op": "contains", "values": ["PAULO"]},
                                                 {"member": "customers.state", "op": "equals", "values": ["RJ", "MG"]}]}],
                             "order": [{"member": "customers.state"}]},
    "column_partition": {"measures": ["order_items.revenue"],
                         "time": {"dimension": "order_items.shipping_limit_at", "range": ["2018-01-01", "2018-03-31"]}},
    "having": {"measures": ["orders.count"], "dimensions": ["customers.state"],
               "filters": [{"member": "orders.count", "op": "gte", "values": [5]}], "order": [{"member": "customers.state"}]},
    "l1_calcs": {"measures": ["order_items.revenue"], "dimensions": ["states.region"],
                 "time": {"dimension": "orders.purchased_at", "granularity": "month", "range": ["2018-01-01", "2018-06-30"]},
                 "calcs": [{"name": "share", "op": "share", "measure": "order_items.revenue", "by": ["orders.purchased_at"]},
                           {"name": "running", "op": "running", "measure": "order_items.revenue", "by": ["states.region"]},
                           {"name": "growth", "op": "compare", "measure": "order_items.revenue", "mode": "diff"}],
                 "order": [{"member": "states.region"}, {"member": "orders.purchased_at"}]},
    "weekly_moving_avg": {"measures": ["orders.count"],
                          "time": {"dimension": "orders.purchased_at", "granularity": "week", "range": ["2017-12-01", "2018-03-31"]},
                          "calcs": [{"name": "ma4", "op": "moving_avg", "measure": "orders.count", "window": 4},
                                    {"name": "wow", "op": "compare", "measure": "orders.count", "mode": "diff"}]},
    "quarterly_by_region": {"measures": ["order_items.revenue"], "dimensions": ["states.region"],
                            "time": {"dimension": "orders.purchased_at", "granularity": "quarter"},
                            "calcs": [{"name": "yoy", "op": "compare", "measure": "order_items.revenue", "offset": 4, "mode": "diff"},
                                      {"name": "ma2", "op": "moving_avg", "measure": "order_items.revenue", "window": 2,
                                       "by": ["states.region"]}],
                            "order": [{"member": "states.region"}, {"member": "orders.purchased_at"}]},
    # L2: cohort retention (portable month arithmetic), a pivot and a rolling window over a stage.
    "l2_cohorts": {"stages": {
        "activity": {"query": {"measures": ["orders.count"], "dimensions": ["customers.customer_id"],
                               "time": {"dimension": "orders.purchased_at", "granularity": "month"}}},
        "first": {"from": "activity", "group": {"by": ["customers.customer_id"],
                                                 "agg": {"cohort": {"fn": "min", "of": "orders.purchased_at.month"}}}},
        "joined": {"from": "activity", "as": "a", "join": [{"stage": "first", "as": "f", "on": [["a.customers.customer_id", "f.customer_id"]]}],
                   "select": {"cohort": "f.cohort", "customer_id": "a.customers.customer_id",
                              "k": {"date_diff": ["month", "f.cohort", "a.orders.purchased_at.month"]}}},
        "retention": {"from": "joined", "group": {"by": ["cohort", "k"], "agg": {"customers": {"fn": "count_distinct", "of": "customer_id"}}}}},
        "output": "retention", "order": [{"column": "cohort"}, {"column": "k"}]},
    "l2_pivot_rolling": {"stages": {
        "by_status": {"query": {"measures": ["orders.count"], "dimensions": ["orders.status"],
                                "time": {"dimension": "orders.purchased_at", "granularity": "quarter"}}},
        "wide": {"from": "by_status", "pivot": {"index": ["orders.purchased_at.quarter"], "column": "orders.status",
                                                "values": ["delivered", "canceled"], "agg": {"fn": "sum", "of": "orders.count"}}},
        "trend": {"from": "wide", "select": {
            "q": "quarter", "delivered": "status_delivered",
            "ma2": {"window": {"fn": "avg", "of": "status_delivered", "order": ["quarter"], "frame": [-1, 0]}},
            "share": {"div": ["status_delivered", {"fn": {"name": "coalesce", "args": [{"add": ["status_delivered", "status_canceled"]}, 0]}}]}}}},
        "output": "trend", "order": [{"column": "q"}]},
}
GOVERNED_SQL = {
    "status_counts": "SELECT status, count(*) AS n FROM orders GROUP BY status ORDER BY status",
    "revenue_by_region": """SELECT s.region, SUM(i.revenue) AS revenue FROM order_items i JOIN orders o USING (order_id)
                            JOIN customers c ON c.customer_id = o.customer_id JOIN states s ON s.code = c.state
                            GROUP BY 1 ORDER BY 1""",
    "rolling": """WITH m AS (SELECT date_trunc('month', purchased_at) AS mo, count(*) AS n FROM orders GROUP BY 1)
                  SELECT mo, n, avg(n) OVER (ORDER BY mo ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS ma3 FROM m ORDER BY mo""",
}
CONTEXTS = {
    "acme": SecurityContext(tenant="acme", roles=["analyst"]),
    "globex_regional": SecurityContext(tenant="globex", roles=["regional_manager"], attrs={"region": "South"}),
}


def normalize(rows):
    def v(x):
        if isinstance(x, (Decimal, float)):
            return round(float(x), 4)
        if isinstance(x, datetime):
            return x.replace(tzinfo=None).isoformat()
        return x

    return [{k: v(x) for k, x in r.items()} for r in rows]


def model_catalog(dialect: str, prefix: str) -> Catalog:
    cat = Catalog(dialect=dialect)
    r = Runner(cat)
    r.apply_all(load_dir(demo.MIGRATIONS))
    if prefix:
        r.apply(Migration.model_validate({"id": "0100_route_test_run", "ops": [
            {"op": "bind_schema", "schema": f"{prefix}tenant_{{tenant}}"},
            {"op": "bind_schema", "cube": "states", "schema": f"{prefix}shared"},
        ]}))
    return cat


def reference_layer(tenants) -> DataLayer:
    con = duckdb.connect()
    demo.build(con, tenants)
    return DataLayer(model_catalog("duckdb", ""), duckdb_executor(con))


def assert_same_answers(layer: DataLayer, reference: DataLayer) -> None:
    for name, q in QUERIES.items():
        seen_rows = 0
        for ctx_name, ctx in CONTEXTS.items():
            got = layer.query(q, ctx, use_cache=False)
            want = reference.query(q, ctx, use_cache=False)
            assert normalize(got["rows"]) == normalize(want["rows"]), (name, ctx_name, got["sql"])
            seen_rows += len(got["rows"])
        assert seen_rows, (name, "returned no rows in any context; the check would be vacuous")
    for name, sql in GOVERNED_SQL.items():
        for ctx_name, ctx in CONTEXTS.items():
            got = layer.governed_sql(sql, ctx)
            want = reference.governed_sql(sql, ctx)
            assert normalize(got["rows"]) == normalize(want["rows"]), (name, ctx_name, got["sql"])
