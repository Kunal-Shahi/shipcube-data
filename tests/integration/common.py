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
