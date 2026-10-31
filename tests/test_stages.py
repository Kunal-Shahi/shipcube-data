"""L2 stages: correct answers against hand-written SQL, and governance holding inside every stage."""

import pytest
import sqlglot

from shipcube_data.compiler import SecurityContext
from shipcube_data.model import ModelError

from .conftest import ORDERS_ACME

ACTIVITY = {"query": {"measures": ["orders.count"], "dimensions": ["customers.customer_id"],
                      "time": {"dimension": "orders.purchased_at", "granularity": "month"}}}

COHORTS = {
    "stages": {
        "activity": ACTIVITY,
        "first": {"from": "activity", "group": {"by": ["customers.customer_id"],
                                                 "agg": {"cohort": {"fn": "min", "of": "orders.purchased_at.month"}}}},
        "joined": {"from": "activity", "as": "a", "join": [{"stage": "first", "as": "f", "on": [["a.customers.customer_id", "f.customer_id"]]}],
                   "select": {"cohort": "f.cohort", "customer_id": "a.customers.customer_id",
                              "k": {"date_diff": ["month", "f.cohort", "a.orders.purchased_at.month"]}}},
        "retention": {"from": "joined", "group": {"by": ["cohort", "k"], "agg": {"customers": {"fn": "count_distinct", "of": "customer_id"}}}},
    },
    "output": "retention", "order": [{"column": "cohort"}, {"column": "k"}],
}


def test_cohort_retention_matches_direct_sql(dl, acme, warehouse):
    got = [(r["cohort"], r["k"], r["customers"]) for r in dl.query(COHORTS, acme)["rows"]]
    want = warehouse.execute(f"""
        WITH o AS (SELECT customer_id, date_trunc('month', order_purchase_timestamp) m FROM ({ORDERS_ACME}) x),
             f AS (SELECT customer_id, min(m) cohort FROM o GROUP BY 1)
        SELECT f.cohort, date_diff('month', f.cohort, o.m) k, count(DISTINCT o.customer_id)
        FROM o JOIN f USING (customer_id) GROUP BY 1, 2 ORDER BY 1, 2""").fetchall()
    assert got == want and got[0][1] == 0


def test_spec_example_year_over_year_growers(dl, acme, warehouse):
    q = {"stages": {
        "y2017": {"query": {"measures": ["orders.count"], "dimensions": ["customers.customer_id"],
                            "time": {"dimension": "orders.purchased_at", "range": ["2017-01-01", "2017-12-31"]}}},
        "y2018": {"query": {"measures": ["orders.count"], "dimensions": ["customers.customer_id"],
                            "time": {"dimension": "orders.purchased_at", "range": ["2018-01-01", "2018-12-31"]}}},
        "growers": {"from": "y2018", "as": "cur",
                    "join": [{"stage": "y2017", "as": "prev", "type": "inner", "on": [["cur.customers__customer_id", "prev.customers__customer_id"]]}],
                    "select": {"customer_id": "cur.customers__customer_id", "orders_2018": "cur.orders__count", "orders_2017": "prev.orders__count"},
                    "where": [{"column": "cur.orders__count", "op": "gt", "column_ref": "prev.orders__count"}]}},
        "output": "growers", "order": [{"column": "customer_id"}]}
    got = [(r["customer_id"], r["orders_2018"], r["orders_2017"]) for r in dl.query(q, acme)["rows"]]
    want = warehouse.execute(f"""
        WITH o AS ({ORDERS_ACME}),
             a AS (SELECT customer_id, count(*) n FROM o WHERE year(order_purchase_timestamp) = 2018 GROUP BY 1),
             b AS (SELECT customer_id, count(*) n FROM o WHERE year(order_purchase_timestamp) = 2017 GROUP BY 1)
        SELECT a.customer_id, a.n, b.n FROM a JOIN b USING (customer_id) WHERE a.n > b.n ORDER BY 1""").fetchall()
    assert got == want and got


def test_funnel_with_in_stage(dl, acme, warehouse):
    q = {"stages": {
        "bought_2017": {"query": {"dimensions": ["customers.customer_id"],
                                  "time": {"dimension": "orders.purchased_at", "range": ["2017-01-01", "2017-12-31"]}, "measures": ["orders.count"]}},
        "bought_2018": {"query": {"dimensions": ["customers.customer_id"], "measures": ["orders.count"],
                                  "time": {"dimension": "orders.purchased_at", "range": ["2018-01-01", "2018-12-31"]}}},
        "returning": {"from": "bought_2018", "where": [{"column": "customers.customer_id", "op": "in_stage", "stage": "bought_2017",
                                                         "stage_column": "customers.customer_id"}],
                      "group": {"agg": {"customers": {"fn": "count"}}}},
        "new": {"from": "bought_2018", "where": [{"column": "customers.customer_id", "op": "not_in_stage", "stage": "bought_2017",
                                                   "stage_column": "customers.customer_id"}],
                "group": {"agg": {"customers": {"fn": "count"}}}},
        "funnel": {"from": "returning", "as": "r", "join": [{"stage": "new", "as": "n", "type": "cross"}],
                   "select": {"returning": "r.customers", "new": "n.customers",
                              "returning_share": {"div": ["r.customers", {"add": ["r.customers", "n.customers"]}]}}}},
        "output": "funnel"}
    row = dl.query(q, acme)["rows"][0]
    years = f"SELECT DISTINCT customer_id FROM ({ORDERS_ACME}) o WHERE year(order_purchase_timestamp) = "
    returning = warehouse.execute(f"SELECT count(*) FROM ({years}2018) a WHERE customer_id IN ({years}2017)").fetchone()[0]
    total = warehouse.execute(f"SELECT count(*) FROM ({years}2018) a").fetchone()[0]
    assert (row["returning"], row["new"]) == (returning, total - returning)
    assert row["returning_share"] == pytest.approx(returning / total)  # division is in floating point everywhere


def test_top_n_per_group_with_a_window_and_having(dl, acme):
    q = {"stages": {
        "by_cat": {"query": {"measures": ["order_items.revenue"], "dimensions": ["states.region", "products.category"]}},
        "ranked": {"from": "by_cat",
                   "select": {"region": "states.region", "category": "products.category", "revenue": "order_items.revenue",
                              "rank": {"window": {"fn": "row_number", "partition": ["states.region"],
                                                  "order": [{"by": "order_items.revenue", "dir": "desc"}]}}},
                   "having": [{"column": "rank", "op": "lte", "values": [2]}]}},
        "output": "ranked", "order": [{"column": "region"}, {"column": "rank"}]}
    rows = dl.query(q, acme)["rows"]
    by_region: dict = {}
    for r in rows:
        by_region.setdefault(r["region"], []).append(r)
    assert by_region and all(len(v) == 2 and v[0]["revenue"] >= v[1]["revenue"] for v in by_region.values())
    full = dl.query({"measures": ["order_items.revenue"], "dimensions": ["states.region", "products.category"]}, acme)["rows"]
    for region, top in by_region.items():
        best = max(r["order_items__revenue"] for r in full if r["states__region"] == region)
        assert top[0]["revenue"] == best


def test_reaggregation_and_rolling_window(dl, acme, warehouse):
    q = {"stages": {
        "monthly": {"query": {"measures": ["orders.count"], "time": {"dimension": "orders.purchased_at", "granularity": "month"}}},
        "rolling": {"from": "monthly", "select": {
            "month": "orders.purchased_at.month", "n": "orders.count",
            "ma3": {"window": {"fn": "avg", "of": "orders.count", "order": ["orders.purchased_at.month"], "frame": [-2, 0]}}}},
        "summary": {"from": "monthly", "group": {"agg": {"avg_monthly": {"fn": "avg", "of": "orders.count"},
                                                         "median_monthly": {"fn": "median", "of": "orders.count"}}}}},
        "output": "rolling", "order": [{"column": "month"}]}
    rows = dl.query(q, acme)["rows"]
    counts = [r["n"] for r in rows]
    assert rows[2]["ma3"] == pytest.approx(sum(counts[:3]) / 3) and rows[0]["ma3"] == counts[0]
    s = dl.query({**q, "output": "summary", "order": []}, acme)["rows"][0]
    monthly = [n for _, n in warehouse.execute(
        f"SELECT date_trunc('month', order_purchase_timestamp), count(*) FROM ({ORDERS_ACME}) o GROUP BY 1").fetchall()]
    assert s["avg_monthly"] == pytest.approx(sum(monthly) / len(monthly))


def test_pivot(dl, acme):
    q = {"stages": {
        "by_status": {"query": {"measures": ["orders.count"], "dimensions": ["states.region", "orders.status"]}},
        "wide": {"from": "by_status", "pivot": {"index": ["states.region"], "column": "orders.status",
                                                "values": ["delivered", "canceled"], "agg": {"fn": "sum", "of": "orders.count"}}}},
        "output": "wide", "order": [{"column": "region"}]}
    rows = dl.query(q, acme)["rows"]
    assert set(rows[0]) == {"region", "status_delivered", "status_canceled"}
    long = dl.query({"measures": ["orders.count"], "dimensions": ["states.region", "orders.status"]}, acme)["rows"]
    want = {(r["states__region"], r["orders__status"]): r["orders__count"] for r in long}
    for r in rows:
        assert r["status_delivered"] == want.get((r["region"], "delivered"))


def test_set_operations(dl, acme):
    q = {"stages": {
        "a": {"query": {"dimensions": ["customers.state"], "filters": [{"member": "customers.state", "op": "equals", "values": ["SP", "RJ"]}]}},
        "b": {"query": {"dimensions": ["customers.state"], "filters": [{"member": "customers.state", "op": "equals", "values": ["RJ", "MG"]}]}},
        "both": {"intersect": ["a", "b"]},
        "either": {"union": ["a", "b"]}},
        "output": "both"}
    assert [r["customers__state"] for r in dl.query(q, acme)["rows"]] == ["RJ"]
    assert sorted(r["customers__state"] for r in dl.query({**q, "output": "either"}, acme)["rows"]) == ["MG", "RJ", "SP"]
    bad = {"stages": {"a": q["stages"]["a"], "c": {"query": {"measures": ["orders.count"]}}, "u": {"union_all": ["a", "c"]}}, "output": "u"}
    with pytest.raises(ModelError, match="same names"):
        dl.query(bad, acme)


# ---------------------------------------------------------------- governance holds inside stages

def test_stages_keep_grants_policies_and_tenants(dl, acme, globex):
    hidden = {"stages": {"x": {"query": {"measures": ["order_items.freight"]}}}, "output": "x"}
    with pytest.raises(ModelError, match="not permitted: order_items.freight"):
        dl.query(hidden, acme)
    regional = SecurityContext(tenant="acme", roles=["regional_manager"], attrs={"region": "South"})
    l0 = dl.query({"measures": ["orders.count"]}, regional)["rows"][0]["orders__count"]
    staged = {"stages": {"x": {"query": {"measures": ["orders.count"]}}, "y": {"from": "x"}}, "output": "y"}
    assert dl.query(staged, regional)["rows"][0]["orders__count"] == l0
    assert dl.query(staged, acme)["rows"][0]["orders__count"] == 1500
    assert dl.query(staged, globex)["rows"][0]["orders__count"] == 500


@pytest.mark.parametrize("q,match", [
    ({"stages": {"y": {"from": "x"}, "x": {"query": {"measures": ["orders.count"]}}}, "output": "y"}, "not an earlier stage"),
    ({"stages": {"orders": {"query": {"measures": ["orders.count"]}}}, "output": "orders"}, "not a cube"),
    ({"stages": {"x": {"query": {"measures": ["orders.count"]}}, "y": {"from": "x", "select": {"n": {"fn": {"name": "pg_sleep", "args": [1]}}}}},
      "output": "y"}, "isn't available"),
    ({"stages": {"x": {"query": {"measures": ["orders.count"]}}, "y": {"from": "x", "select": {"Bad Name": "orders.count"}}},
      "output": "y"}, "snake_case"),
    ({"stages": {"x": {"query": {"measures": ["orders.count"]}}, "y": {"from": "x", "select": {"n": "orders.nope"}}},
      "output": "y"}, "unknown column"),
    ({"stages": {"x": {"query": {"measures": ["orders.count"]}}, "y": {"from": "x", "select": {"n": "1; DROP TABLE x"}}},
      "output": "y"}, "unknown column"),
])
def test_bad_stages_are_refused(dl, acme, q, match):
    with pytest.raises(ModelError, match=match):
        dl.query(q, acme)


def test_literals_are_values_not_sql(dl, acme):
    q = {"stages": {"x": {"query": {"measures": ["orders.count"], "dimensions": ["orders.status"]}},
                    "y": {"from": "x", "where": [{"column": "orders.status", "op": "equals", "values": ["x' OR '1'='1"]}]}},
         "output": "y"}
    assert dl.query(q, acme)["rows"] == []


@pytest.mark.parametrize("dialect", ["duckdb", "postgres", "bigquery", "clickhouse"])
def test_staged_queries_compile_to_each_dialect(dl, acme, dialect):
    compiled, _ = dl.compile(COHORTS, acme, dialect=dialect)
    sqlglot.parse_one(compiled.sql, read=dialect)
    assert "DATE_DIFF" not in compiled.sql.upper().replace("DATE_DIFF('DAY'", "")  # month diffs are portable arithmetic
