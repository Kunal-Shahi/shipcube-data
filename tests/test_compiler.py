from decimal import Decimal

import pytest
import sqlglot

from shipcube_data.compiler import SecurityContext
from shipcube_data.model import ModelError
from shipcube_data.ops import Migration

from .conftest import ORDERS_ACME, scalar


def apply(runner, mid, *ops):
    return runner.apply(Migration.model_validate({"id": mid, "ops": list(ops)}))


# ---------------------------------------------------------------- correctness vs hand-written SQL

def test_monthly_counts_match_direct_sql(dl, acme, warehouse):
    res = dl.query({"measures": ["orders.count"],
                    "time": {"dimension": "orders.purchased_at", "granularity": "month", "range": ["2017-03-01", "2017-05-31"]}}, acme)
    got = [(r["orders__purchased_at__month"].month, r["orders__count"]) for r in res["rows"]]
    want = [(m, scalar(warehouse, f"SELECT COUNT(*) FROM tenant_acme.orders_2017_{m:02d}")) for m in (3, 4, 5)]
    assert got == want


def test_tenant_routing(dl, acme, globex, warehouse):
    a = dl.query({"measures": ["orders.count"]}, acme)["rows"][0]["orders__count"]
    g = dl.query({"measures": ["orders.count"]}, globex)["rows"][0]["orders__count"]
    assert (a, g) == (1500, 500)
    sql = dl.compile({"measures": ["customers.count"]}, globex)[0].sql
    assert "tenant_globex.customers" in sql and "tenant_acme" not in sql


def test_unbound_tenant_fails_closed(runner, dl):
    apply(runner, "0006_rebind", {"op": "bind_schema", "schema": "tenant_acme", "tenant": "acme"})
    runner.catalog.db.execute("DELETE FROM schema_bindings WHERE tenant = '*' AND cube = '*'")
    runner.catalog._cache.clear()
    with pytest.raises(ModelError, match="no schema binding for cube orders and tenant globex"):
        dl.query({"measures": ["orders.count"]}, SecurityContext(tenant="globex"))


def test_cross_schema_join_and_values(dl, acme, warehouse):
    res = dl.query({"measures": ["customers.count"], "dimensions": ["states.region"], "order": [{"member": "states.region"}]}, acme)
    want = warehouse.execute(
        "SELECT s.region, COUNT(*) FROM tenant_acme.customers c LEFT JOIN shared.states s ON c.customer_state = s.state_code GROUP BY 1 ORDER BY 1"
    ).fetchall()
    assert [(r["states__region"], r["customers__count"]) for r in res["rows"]] == want


def test_multi_fact_query_does_not_fan_out(dl, acme, warehouse):
    res = dl.query({"measures": ["orders.count", "order_items.revenue"], "dimensions": ["customers.state"]}, acme)
    sp = next(r for r in res["rows"] if r["customers__state"] == "SP")
    assert "FULL OUTER JOIN" in res["sql"]
    want_orders = scalar(warehouse, f"SELECT COUNT(*) FROM ({ORDERS_ACME}) o JOIN tenant_acme.customers c USING (customer_id) WHERE c.customer_state = 'SP'")
    want_rev = scalar(warehouse, f"SELECT SUM(i.price) FROM tenant_acme.order_items i JOIN ({ORDERS_ACME}) o USING (order_id) JOIN tenant_acme.customers c USING (customer_id) WHERE c.customer_state = 'SP'")
    assert (sp["orders__count"], sp["order_items__revenue"]) == (want_orders, want_rev)


def test_fan_out_is_refused_but_distinct_counts_are_allowed(dl, acme, warehouse):
    with pytest.raises(ModelError, match="orders.count would be inflated.*one_to_many"):
        dl.query({"measures": ["orders.count"], "dimensions": ["products.category"]}, acme)
    res = dl.query({"measures": ["orders.unique_orders"], "dimensions": ["products.category"], "limit": 100}, acme)
    want = scalar(warehouse, "SELECT COUNT(DISTINCT i.order_id) FROM tenant_acme.order_items i JOIN tenant_acme.products p USING (product_id) WHERE p.product_category_name = 'toys'")
    assert next(r for r in res["rows"] if r["products__category"] == "toys")["orders__unique_orders"] == want


def test_derived_and_filtered_measures(dl, warehouse):
    finance = SecurityContext(tenant="acme", roles=["finance"])  # gmv uses freight, which is finance-only
    r = dl.query({"measures": ["orders.delivered_count", "orders.late_count", "orders.late_rate", "order_items.gmv"]}, finance)["rows"]
    # gmv joins nothing from orders, so this is two groups stitched with a CROSS JOIN
    row = r[0]
    delivered = scalar(warehouse, f"SELECT COUNT(*) FROM ({ORDERS_ACME}) WHERE order_status = 'delivered'")
    late = scalar(warehouse, f"SELECT COUNT(*) FROM ({ORDERS_ACME}) WHERE order_delivered_customer_date > order_estimated_delivery_date")
    assert (row["orders__delivered_count"], row["orders__late_count"]) == (delivered, late)
    assert row["orders__late_rate"] == pytest.approx(late / delivered)
    assert row["order_items__gmv"] == scalar(warehouse, "SELECT SUM(price) + SUM(freight_value) FROM tenant_acme.order_items")


# ---------------------------------------------------------------- filters, segments, order

def test_filters_and_segments(dl, acme, warehouse):
    q = {"measures": ["orders.count"], "segments": ["orders.delivered"],
         "filters": [{"or": [{"member": "customers.city", "op": "contains", "values": ["PAULO"]},
                             {"member": "customers.state", "op": "equals", "values": ["RJ", "MG"]}]},
                     {"member": "orders.delivered_at", "op": "set"}]}
    got = dl.query(q, acme)["rows"][0]["orders__count"]
    want = scalar(warehouse, f"""SELECT COUNT(*) FROM ({ORDERS_ACME}) o JOIN tenant_acme.customers c USING (customer_id)
        WHERE o.order_status = 'delivered' AND o.order_delivered_customer_date IS NOT NULL
          AND (lower(c.customer_city) LIKE '%paulo%' OR c.customer_state IN ('RJ', 'MG'))""")
    assert got == want


def test_measure_filter_becomes_having(dl, acme):
    res = dl.query({"measures": ["orders.count"], "dimensions": ["customers.state"],
                    "filters": [{"member": "orders.count", "op": "gte", "values": [100]}]}, acme)
    assert "HAVING" in res["sql"] and all(r["orders__count"] >= 100 for r in res["rows"])


def test_order_limit_offset(dl, acme):
    rows = dl.query({"measures": ["orders.count"], "dimensions": ["customers.state"],
                     "order": [{"member": "orders.count", "dir": "desc"}], "limit": 2, "offset": 1}, acme)["rows"]
    full = dl.query({"measures": ["orders.count"], "dimensions": ["customers.state"],
                     "order": [{"member": "orders.count", "dir": "desc"}]}, acme)["rows"]
    assert rows == full[1:3]


@pytest.mark.parametrize("bad,error", [
    ({"measures": ["orders.nope"]}, "unknown or not permitted"),
    ({"measures": ["orders.status"]}, "is a dimension"),
    ({"dimensions": ["orders.count"]}, "is a measure"),
    ({"measures": ["orders.count"], "time": {"dimension": "orders.status"}}, "not a time dimension"),
    ({"measures": ["orders.count"], "filters": [{"or": [{"member": "orders.count", "op": "gt", "values": [1]},
                                                       {"member": "orders.status", "op": "equals", "values": ["x"]}]}]}, "cannot mix"),
    ({"measures": ["orders.count"], "order": [{"member": "orders.status"}]}, "not in the query"),
])
def test_bad_queries(dl, acme, bad, error):
    with pytest.raises(ModelError, match=error):
        dl.query(bad, acme)


# ---------------------------------------------------------------- partitions

def test_table_partitions_are_pruned(dl, acme):
    res = dl.query({"measures": ["orders.count"], "filters": [{"member": "orders.purchased_at", "op": "in_date_range", "values": ["2017-12-15", "2018-01-10"]}]}, acme)
    assert res["partitions"]["orders"] == {"strategy": "table", "total": 20, "scanned": 2, "tables": ["orders_2017_12", "orders_2018_01"]}
    assert "orders_2017_11" not in res["sql"]


def test_unfiltered_partitioned_query_warns(dl, acme):
    res = dl.query({"measures": ["orders.count"]}, acme)
    assert res["partitions"]["orders"]["scanned"] == 20
    assert any("reading all 20 partitions" in w for w in res["warnings"])


def test_column_partition_predicate_on_derived_dimension(runner, dl, acme):
    apply(runner, "0006_ship_day", {"op": "add_dimension", "name": "order_items.ship_day", "sql": "CAST(shipping_limit_date AS DATE)", "type": "time"})
    res = dl.query({"measures": ["order_items.revenue"], "time": {"dimension": "order_items.ship_day", "range": ["2018-02-01", "2018-02-28"]}}, acme)
    # The user filtered on an expression; the layer adds a predicate on the raw column the warehouse can prune by.
    assert "order_items.shipping_limit_date >= CAST('2018-02-01' AS TIMESTAMP)" in res["sql"]
    assert "order_items.shipping_limit_date < CAST('2018-03-01' AS TIMESTAMP)" in res["sql"]


def test_require_filter(runner, dl, acme):
    apply(runner, "0006_strict", {"op": "define_partition", "cube": "order_items", "column": "shipping_limit_date", "require_filter": True})
    with pytest.raises(ModelError, match="require_filter"):
        dl.query({"measures": ["order_items.revenue"]}, acme)
    dl.query({"measures": ["order_items.revenue"], "filters": [{"member": "order_items.shipping_limit_at", "op": "after_date", "values": ["2018-01-01"]}]}, acme)


def test_list_partitions_by_customer_set(runner, dl, acme, warehouse):
    for st in ("sp", "rj", "mg"):
        warehouse.execute(f"CREATE OR REPLACE TABLE tenant_acme.customers_{st} AS "
                          f"SELECT *, lower(customer_state) AS state_key FROM tenant_acme.customers WHERE customer_state = '{st.upper()}'")
    apply(runner, "0006_split",
          {"op": "create_cube", "name": "big_customers", "table": "customers", "dimensions": {"state_key": "state_key"}},
          {"op": "define_partition", "cube": "big_customers", "dimension": "state_key", "values": ["sp", "rj", "mg"],
           "table_pattern": "customers_{value}"})
    res = dl.query({"measures": ["big_customers.count"], "filters": [{"member": "big_customers.state_key", "op": "equals", "values": ["rj"]}]}, acme)
    assert res["partitions"]["big_customers"]["tables"] == ["customers_rj"]
    assert res["rows"][0]["big_customers__count"] == scalar(warehouse, "SELECT COUNT(*) FROM tenant_acme.customers WHERE customer_state = 'RJ'")
    every = dl.query({"measures": ["big_customers.count"]}, acme)
    assert every["partitions"]["big_customers"]["scanned"] == 3


# ---------------------------------------------------------------- access control

def test_row_policy_is_injected(dl, warehouse):
    ctx = SecurityContext(tenant="acme", roles=["regional_manager"], attrs={"region": "South"})
    res = dl.query({"measures": ["orders.count"], "dimensions": ["customers.state"]}, ctx)
    assert {r["customers__state"] for r in res["rows"]} <= {"RS", "PR", "SC"}
    # even without asking for the joined cube, the policy pulls it in
    total = dl.query({"measures": ["orders.count"]}, ctx)["rows"][0]["orders__count"]
    assert total == sum(r["orders__count"] for r in res["rows"])
    assert "states.region = 'South'" in dl.compile({"measures": ["orders.count"]}, ctx)[0].sql


def test_row_policy_needs_its_context(dl):
    with pytest.raises(ModelError, match="no 'region'"):
        dl.query({"measures": ["orders.count"]}, SecurityContext(tenant="acme", roles=["regional_manager"]))


def test_row_policy_does_not_apply_to_other_roles(dl, acme):
    assert "states.region" not in dl.compile({"measures": ["orders.count"]}, acme)[0].sql


def test_member_grants_hide_members_and_what_depends_on_them(dl, acme):
    with pytest.raises(ModelError, match="unknown or not permitted: order_items.freight"):
        dl.query({"measures": ["order_items.freight"]}, acme)
    with pytest.raises(ModelError, match="not permitted: order_items.gmv"):
        dl.query({"measures": ["order_items.gmv"]}, acme)
    measures = [m["path"] for m in dl.describe_cube("order_items", acme)["measures"]]
    assert "order_items.freight" not in measures and "order_items.gmv" not in measures
    finance = SecurityContext(tenant="acme", roles=["finance"])
    assert dl.query({"measures": ["order_items.freight", "order_items.gmv"]}, finance)["rows"][0]["order_items__freight"] > 0


def test_ctx_values_are_literals_not_sql(dl):
    ctx = SecurityContext(tenant="acme", roles=["regional_manager"], attrs={"region": "x' OR '1'='1"})
    res = dl.query({"measures": ["orders.count"]}, ctx)
    assert res["rows"][0]["orders__count"] == 0


def test_tenant_must_be_an_identifier(dl):
    with pytest.raises(ModelError, match="not a valid identifier"):
        dl.query({"measures": ["orders.count"]}, SecurityContext(tenant="acme; DROP SCHEMA x"))


# ---------------------------------------------------------------- L1 operators

def test_share_rank_running_lag(dl, acme):
    res = dl.query({
        "measures": ["order_items.revenue"],
        "dimensions": ["states.region"],
        "time": {"dimension": "orders.purchased_at", "granularity": "year"},
        "calcs": [
            {"name": "share_of_year", "op": "share", "measure": "order_items.revenue", "by": ["orders.purchased_at"]},
            {"name": "rank_in_year", "op": "rank", "measure": "order_items.revenue", "by": ["orders.purchased_at"]},
            {"name": "running", "op": "running", "measure": "order_items.revenue", "by": ["states.region"]},
            {"name": "last_year", "op": "lag", "measure": "order_items.revenue", "by": ["states.region"]},
        ],
    }, acme)
    rows = res["rows"]
    for year in {r["orders__purchased_at__year"] for r in rows}:
        yr = [r for r in rows if r["orders__purchased_at__year"] == year]
        assert sum(r["share_of_year"] for r in yr) == pytest.approx(1.0)
        top = max(yr, key=lambda r: r["order_items__revenue"])
        assert top["rank_in_year"] == 1
    se = sorted((r for r in rows if r["states__region"] == "Southeast"), key=lambda r: r["orders__purchased_at__year"])
    assert se[1]["running"] == se[0]["order_items__revenue"] + se[1]["order_items__revenue"]
    assert se[0]["last_year"] is None and se[1]["last_year"] == se[0]["order_items__revenue"]


def test_compare_is_correct_across_gaps(dl, acme):
    # Filter out March 2017 so the series has a hole: April must compare to March (missing), not February.
    res = dl.query({
        "measures": ["orders.count"],
        "time": {"dimension": "orders.purchased_at", "granularity": "month", "range": ["2017-01-01", "2017-06-30"]},
        "filters": [{"not": {"member": "orders.purchased_at", "op": "in_date_range", "values": ["2017-03-01", "2017-03-31"]}}],
        "calcs": [{"name": "prev", "op": "compare", "measure": "orders.count", "mode": "prior"},
                  {"name": "growth", "op": "compare", "measure": "orders.count"}],
    }, acme)
    by_month = {r["orders__purchased_at__month"].month: r for r in res["rows"]}
    assert 3 not in by_month
    assert by_month[4]["prev"] is None
    assert by_month[2]["prev"] == by_month[1]["orders__count"]
    assert by_month[2]["growth"] == pytest.approx(by_month[2]["orders__count"] / by_month[1]["orders__count"] - 1)


def test_running_needs_additive_measure(dl, acme):
    with pytest.raises(ModelError, match="additive"):
        dl.query({"measures": ["order_items.avg_price"], "time": {"dimension": "orders.purchased_at", "granularity": "month"},
                  "calcs": [{"name": "r", "op": "running", "measure": "order_items.avg_price"}]}, acme)


# ---------------------------------------------------------------- dialects

QUERY = {"measures": ["orders.count", "order_items.revenue"], "dimensions": ["states.region"],
         "time": {"dimension": "orders.purchased_at", "granularity": "month", "range": ["2018-01-01", "2018-03-31"]},
         "filters": [{"member": "customers.city", "op": "contains", "values": ["rio"]}],
         "calcs": [{"name": "growth", "op": "compare", "measure": "orders.count"}], "limit": 50}


@pytest.mark.parametrize("dialect,marker", [
    ("duckdb", "DATE_TRUNC('MONTH'"),
    ("postgres", "DATE_TRUNC('MONTH'"),
    ("bigquery", "TIMESTAMP_TRUNC("),
    ("clickhouse", "dateTrunc('MONTH'"),
])
def test_compiles_to_each_dialect(dl, acme, dialect, marker):
    compiled, _ = dl.compile(QUERY, acme, dialect=dialect)
    assert marker in compiled.sql
    sqlglot.parse_one(compiled.sql, read=dialect)  # round-trips through the dialect's parser
    assert "orders_2018_02" in compiled.sql and "orders_2017_12" not in compiled.sql


def test_duckdb_compile_runs(dl, acme):
    res = dl.query(QUERY, acme)
    assert res["verified"] and res["rows"]


# ---------------------------------------------------------------- cache, search, discovery

def test_cache_is_keyed_by_the_fingerprint_of_used_cubes(runner, dl, acme):
    q = {"measures": ["customers.count"], "dimensions": ["customers.state"]}
    assert dl.query(q, acme)["cache"] == "miss"
    assert dl.query(q, acme)["cache"] == "hit"
    apply(runner, "0006_unrelated", {"op": "add_measure", "name": "products.unique_categories", "sql": "product_category_name"})
    assert dl.query(q, acme)["cache"] == "hit"  # products is not part of this query
    apply(runner, "0007_related", {"op": "change_sql", "member": "customers.state", "sql": "upper(customer_state)"})
    assert dl.query(q, acme)["cache"] == "miss"
    other_tenant = SecurityContext(tenant="globex", roles=["analyst"])
    assert dl.query(q, other_tenant)["cache"] == "miss"


def test_search_values(dl, acme):
    assert dl.search_values("customers.city", "rio", acme) == ["rio de janeiro"]
    assert "Southeast" in dl.search_values("states.region", "", acme)


def test_list_and_describe(dl, acme):
    cubes = {c["name"]: c for c in dl.list_cubes(acme)}
    assert set(cubes) == {"customers", "states", "orders", "products", "order_items"}
    assert "freight" not in cubes["order_items"]["measures"]
    d = dl.describe_cube("orders", acme)
    assert d["partitioned_by"]["granularity"] == "month"
    late = next(m for m in d["measures"] if m["path"] == "orders.late_rate")
    assert late["format"] == "percent" and late["agg"] == "number"
    assert {"to": "order_items", "relationship": "one_to_many"} in d["joins"]


# ---------------------------------------------------------------- L3 raw SQL

def test_raw_sql_is_scoped_and_unverified(dl, acme):
    res = dl.raw_sql("SELECT customer_state, COUNT(*) AS n FROM customers GROUP BY 1 ORDER BY 2 DESC", acme)
    assert res["verified"] is False and res["rows"][0]["customer_state"] == "SP"
    assert "tenant_acme.customers" in res["sql"]
    assert dl.raw_sql("SELECT COUNT(*) AS n FROM shared.states", acme)["rows"][0]["n"] == 27


@pytest.mark.parametrize("sql,error", [
    ("SELECT * FROM tenant_globex.customers", "not readable by tenant acme"),
    ("DELETE FROM customers", "single SELECT"),
    ("SELECT 1; SELECT 2", "exactly one"),
    ("SELECT * FROM read_csv('/etc/passwd')", "may not"),
    ("SELECT * FROM information_schema.tables", "not readable"),
    ("SELECT * FROM other_db.tenant_acme.customers", "another database"),
])
def test_raw_sql_rejections(dl, acme, sql, error):
    with pytest.raises(ModelError, match=error):
        dl.raw_sql(sql, acme)


def test_raw_sql_refused_when_row_policies_apply(dl):
    ctx = SecurityContext(tenant="acme", roles=["regional_manager"], attrs={"region": "South"})
    with pytest.raises(ModelError, match="row policies apply"):
        dl.raw_sql("SELECT COUNT(*) FROM customers", ctx)


def test_decimal_results_are_exact(dl, acme):
    rev = dl.query({"measures": ["order_items.revenue"]}, acme)["rows"][0]["order_items__revenue"]
    assert isinstance(rev, Decimal)
