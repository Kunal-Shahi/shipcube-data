"""Scale benchmark: an Olist-shaped warehouse with millions of rows on Postgres, queried through L0/L1,
L2 and governed SQL. Measures compile time, query latency and partition pruning.

    python bench/scale.py --url postgresql://postgres@localhost:54331/postgres --orders 10000000 --load
    python bench/scale.py --url ... --orders 10000000            # reuse the loaded data
    python bench/scale.py --url ... --orders 10000000 --default-guards   # also show what the default guardrails refuse

The data is generated inside Postgres with generate_series (no Python round trips), deterministic in
--orders. Layout matches the demo: `<prefix>tenant_acme.orders_YYYY_MM` (20 monthly partitions of a
range-partitioned parent), one `order_items` table clustered by shipping date with a BRIN index (append-only
order, as a growing shop has), customers, products and `<prefix>shared.states`. About 1.67 items per order.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from shipcube_data import demo  # noqa: E402
from shipcube_data.catalog import Catalog  # noqa: E402
from shipcube_data.compiler import SecurityContext  # noqa: E402
from shipcube_data.engine import DataLayer  # noqa: E402
from shipcube_data.migrate import Runner, load_dir  # noqa: E402
from shipcube_data.model import ModelError  # noqa: E402
from shipcube_data.ops import Migration  # noqa: E402
from shipcube_data.warehouses import PostgresWarehouse, QueryLimits  # noqa: E402

PREFIX = "bench_"
SCHEMA = f"{PREFIX}tenant_acme"
SHARED = f"{PREFIX}shared"
START, END = "2017-01-01", "2018-09-01"


def load(url: str, n_orders: int) -> dict:
    n_customers, n_products, n_sellers = max(n_orders // 4, 100), 10_000, 2_000
    t0 = time.perf_counter()
    busy = "ARRAY[" + ",".join(f"'{s}'" for s in demo.BUSY_STATES) + "]"
    cats = "ARRAY[" + ",".join(f"'{c}'" for c in demo.CATEGORIES) + "]"
    span = "(timestamp '2018-09-01' - timestamp '2017-01-01')"
    # ts grows like the demo's (more orders later); i -> ts is monotonic, so tables are time-clustered.
    ts = f"timestamp '2017-01-01' + power((i - 1)::float8 / {n_orders}, 1.25) * {span}"
    with psycopg.connect(url, autocommit=True) as c:
        c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        c.execute(f"DROP SCHEMA IF EXISTS {SHARED} CASCADE")
        c.execute(f"CREATE SCHEMA {SCHEMA}")
        c.execute(f"CREATE SCHEMA {SHARED}")
        c.execute(f"CREATE TABLE {SHARED}.states (state_code TEXT PRIMARY KEY, state_name TEXT, region TEXT)")
        with c.cursor() as cur:
            cur.executemany(f"INSERT INTO {SHARED}.states VALUES (%s, %s, %s)", demo.STATES)
        c.execute(f"""CREATE TABLE {SCHEMA}.customers AS
            SELECT 'c' || i AS customer_id, 'u' || (abs(hashint8(i)) % {int(n_customers * 0.9)}) AS customer_unique_id,
                   st AS customer_state,
                   CASE st WHEN 'SP' THEN (ARRAY['sao paulo','campinas','santos'])[1 + abs(hashint8(i + 1)) % 3]
                           WHEN 'RJ' THEN (ARRAY['rio de janeiro','niteroi'])[1 + abs(hashint8(i + 1)) % 2]
                           ELSE 'capital ' || lower(st) END AS customer_city
            FROM (SELECT i, ({busy})[1 + abs(hashint8(i + 2)) % {len(demo.BUSY_STATES)}] AS st
                  FROM generate_series(1, {n_customers}) i) x""")
        c.execute(f"ALTER TABLE {SCHEMA}.customers ADD PRIMARY KEY (customer_id)")
        c.execute(f"""CREATE TABLE {SCHEMA}.products AS
            SELECT 'p' || i AS product_id, ({cats})[1 + abs(hashint8(i)) % {len(demo.CATEGORIES)}] AS product_category_name
            FROM generate_series(1, {n_products}) i""")
        c.execute(f"ALTER TABLE {SCHEMA}.products ADD PRIMARY KEY (product_id)")
        c.execute(f"""CREATE TABLE {SCHEMA}.orders_all (order_id TEXT, customer_id TEXT, order_status TEXT,
            order_purchase_timestamp TIMESTAMP, order_delivered_customer_date TIMESTAMP, order_estimated_delivery_date TIMESTAMP)
            PARTITION BY RANGE (order_purchase_timestamp)""")
        for y, m in demo.MONTHS:
            ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
            c.execute(f"CREATE TABLE {SCHEMA}.orders_{y:04d}_{m:02d} PARTITION OF {SCHEMA}.orders_all "
                      f"FOR VALUES FROM ('{y:04d}-{m:02d}-01') TO ('{ny:04d}-{nm:02d}-01')")
        c.execute(f"""INSERT INTO {SCHEMA}.orders_all
            SELECT 'o' || i, 'c' || (1 + abs(hashint8(i)) % {n_customers}), status, ts,
                   CASE WHEN status = 'delivered' THEN ts + (3 + abs(hashint8(i + 3)) % 38) * interval '1 day' END,
                   ts + (10 + abs(hashint8(i + 4)) % 21) * interval '1 day'
            FROM (SELECT i, {ts} AS ts,
                         CASE abs(hashint8(i + 5)) % 20 WHEN 18 THEN 'shipped' WHEN 19 THEN 'canceled' ELSE 'delivered' END AS status
                  FROM generate_series(1, {n_orders}) i) o""")
        c.execute(f"""CREATE TABLE {SCHEMA}.order_items AS
            SELECT 'o' || i AS order_id, j AS order_item_id,
                   'p' || (1 + abs(hashint8(i * 7 + j)) % {n_products}) AS product_id,
                   's' || (1 + abs(hashint8(i * 13 + j)) % {n_sellers}) AS seller_id,
                   ts + (1 + abs(hashint8(i * 17 + j)) % 7) * interval '1 day' AS shipping_limit_date,
                   price, round(price * (0.05 + (abs(hashint8(i * 19 + j)) % 2000) / 10000.0), 2)::numeric(10,2) AS freight_value
            FROM (SELECT i, {ts} AS ts, (10 + (abs(hashint8(i * 23)) % 39000) / 100.0)::numeric(10,2) AS price,
                         (ARRAY[1,1,1,2,2,3])[1 + abs(hashint8(i * 29)) % 6] AS k
                  FROM generate_series(1, {n_orders}) i) o, generate_series(1, o.k) j""")
        c.execute(f"CREATE INDEX ON {SCHEMA}.order_items USING brin (shipping_limit_date)")
        c.execute(f"ANALYZE {SCHEMA}.orders_all")
        c.execute(f"ANALYZE {SCHEMA}.order_items")
        c.execute(f"ANALYZE {SCHEMA}.customers")
        c.execute(f"ANALYZE {SCHEMA}.products")
        items = c.execute(f"SELECT count(*) FROM {SCHEMA}.order_items").fetchone()[0]
        size = c.execute(f"SELECT pg_size_pretty(sum(pg_total_relation_size(c.oid))) FROM pg_class c "
                         f"JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = '{SCHEMA}'").fetchone()[0]
    return {"orders": n_orders, "order_items": items, "customers": n_customers, "size": size, "load_s": round(time.perf_counter() - t0, 1)}


def layer(url: str, limits: QueryLimits) -> DataLayer:
    cat = Catalog(dialect="postgres")
    r = Runner(cat)
    r.apply_all(load_dir(demo.MIGRATIONS))
    r.apply(Migration.model_validate({"id": "0100_route_bench", "ops": [
        {"op": "bind_schema", "schema": f"{PREFIX}tenant_{{tenant}}"},
        {"op": "bind_schema", "cube": "states", "schema": SHARED},
    ]}))
    return DataLayer(cat, PostgresWarehouse(url, application_name="shipcube-bench"), limits=limits)


Q1_2018 = ["2018-01-01", "2018-03-31"]
COHORTS = {"stages": {
    "activity": {"query": {"measures": ["orders.count"], "dimensions": ["customers.customer_id"],
                           "time": {"dimension": "orders.purchased_at", "granularity": "month", "range": ["2018-01-01", "2018-06-30"]}}},
    "first": {"from": "activity", "group": {"by": ["customers.customer_id"], "agg": {"cohort": {"fn": "min", "of": "orders.purchased_at.month"}}}},
    "joined": {"from": "activity", "as": "a", "join": [{"stage": "first", "as": "f", "on": [["a.customers.customer_id", "f.customer_id"]]}],
               "select": {"cohort": "f.cohort", "customer_id": "a.customers.customer_id",
                          "k": {"date_diff": ["month", "f.cohort", "a.orders.purchased_at.month"]}}},
    "retention": {"from": "joined", "group": {"by": ["cohort", "k"], "agg": {"customers": {"fn": "count_distinct", "of": "customer_id"}}}}},
    "output": "retention", "order": [{"column": "cohort"}, {"column": "k"}]}
TOP_CATEGORIES = {"stages": {
    "by_cat": {"query": {"measures": ["order_items.revenue"], "dimensions": ["states.region", "products.category"],
                         "time": {"dimension": "order_items.shipping_limit_at", "range": Q1_2018}}},
    "ranked": {"from": "by_cat", "select": {"region": "states.region", "category": "products.category", "revenue": "order_items.revenue",
                                            "rank": {"window": {"fn": "row_number", "partition": ["states.region"],
                                                                "order": [{"by": "order_items.revenue", "dir": "desc"}]}}},
               "having": [{"column": "rank", "op": "lte", "values": [3]}]}},
    "output": "ranked", "order": [{"column": "region"}, {"column": "rank"}]}

CASES = [
    # (name, level, query or sql)
    ("monthly orders + late rate, Q1 2018", "L0", {"measures": ["orders.count", "orders.late_rate"],
                                                   "time": {"dimension": "orders.purchased_at", "granularity": "month", "range": Q1_2018}}),
    ("monthly orders, all time", "L0", {"measures": ["orders.count"], "time": {"dimension": "orders.purchased_at", "granularity": "month"}}),
    ("revenue by region (4-cube join), Q1 2018", "L0", {"measures": ["order_items.revenue"], "dimensions": ["states.region"],
                                                        "time": {"dimension": "order_items.shipping_limit_at", "range": Q1_2018}}),
    ("revenue by region, all time", "L0", {"measures": ["order_items.revenue"], "dimensions": ["states.region"]}),
    ("MoM growth by region (L1 compare), 2018", "L1", {"measures": ["orders.count"], "dimensions": ["states.region"],
                                                        "time": {"dimension": "orders.purchased_at", "granularity": "month", "range": ["2018-01-01", "2018-08-31"]},
                                                        "calcs": [{"name": "mom", "op": "compare", "measure": "orders.count"}]}),
    ("cohort retention, H1 2018", "L2", COHORTS),
    ("top-3 categories per region, Q1 2018", "L2", TOP_CATEGORIES),
    ("status counts, all time", "SQL", "SELECT status, count(*) AS n FROM orders GROUP BY status"),
    ("status counts, Q1 2018", "SQL", "SELECT status, count(*) AS n FROM orders "
                                      "WHERE purchased_at >= '2018-01-01' AND purchased_at < '2018-04-01' GROUP BY status"),
    ("revenue by region (joins), Q1 2018", "SQL",
     """SELECT s.region, SUM(i.revenue) AS revenue FROM order_items i JOIN orders o USING (order_id)
        JOIN customers c ON c.customer_id = o.customer_id JOIN states s ON s.code = c.state
        WHERE i.shipping_limit_at >= '2018-01-01' AND i.shipping_limit_at < '2018-04-01'
          AND o.purchased_at >= '2017-12-01' AND o.purchased_at < '2018-04-01'
        GROUP BY 1"""),
    ("3-month rolling orders, 2018", "SQL",
     """WITH m AS (SELECT date_trunc('month', purchased_at) AS mo, count(*) AS n FROM orders
                   WHERE purchased_at >= '2018-01-01' GROUP BY 1)
        SELECT mo, n, avg(n) OVER (ORDER BY mo ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS ma3 FROM m ORDER BY mo"""),
]
CTX = SecurityContext(tenant="acme", roles=["analyst"])


def partitions_read(sql: str) -> int:
    return len({f"orders_{y:04d}_{m:02d}" for y, m in demo.MONTHS if f"orders_{y:04d}_{m:02d}" in sql})


def run(dl: DataLayer, runs: int) -> list[dict]:
    from shipcube_data import govsql

    out = []
    for name, level, q in CASES:
        row: dict = {"case": name, "level": level}
        try:
            t = []
            for _ in range(20):
                t0 = time.perf_counter()
                if level == "SQL":
                    sql, _ = govsql.rewrite(q, dl.catalog.load(), CTX, dl.dialect, dl.dialect, dl.limits.max_rows)
                else:
                    sql = dl.compile(q, CTX)[0].sql
                t.append((time.perf_counter() - t0) * 1000)
            row["compile_ms"] = round(statistics.median(t), 1)
            row["orders_partitions"] = f"{partitions_read(sql)}/20"
            lat = []
            for _ in range(runs):
                t0 = time.perf_counter()
                res = dl.governed_sql(q, CTX) if level == "SQL" else dl.query(q, CTX, use_cache=False)
                lat.append((time.perf_counter() - t0) * 1000)
            row["first_ms"] = round(lat[0])
            row["median_ms"] = round(statistics.median(lat))
            row["rows"] = len(res["rows"])
        except ModelError as e:
            row["error"] = f"{e.code}: {str(e)[:140]}"
        out.append(row)
        print(json.dumps(row), flush=True)
    return out


def markdown(rows: list[dict]) -> str:
    lines = ["| Case | Level | Compile (ms) | Monthly order tables read | First run (ms) | Median (ms) | Rows |",
             "|---|---|---|---|---|---|---|"]
    for r in rows:
        if "error" in r:
            lines.append(f"| {r['case']} | {r['level']} | {r.get('compile_ms', '')} | {r.get('orders_partitions', '')} | refused: `{r['error']}` | | |")
        else:
            lines.append(f"| {r['case']} | {r['level']} | {r['compile_ms']} | {r['orders_partitions']} | {r['first_ms']} | {r['median_ms']} | {r['rows']} |")
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("--orders", type=int, default=10_000_000)
    p.add_argument("--load", action="store_true")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--default-guards", action="store_true", help="also run with the production default QueryLimits")
    p.add_argument("--out", default=None, help="write results JSON here")
    a = p.parse_args()
    info = load(a.url, a.orders) if a.load else {"orders": a.orders}
    print(json.dumps({"data": info}), flush=True)
    results = {"data": info}
    if a.default_guards:
        print("== default guardrails", flush=True)
        results["default_guards"] = run(layer(a.url, QueryLimits()), 1)
        print(markdown(results["default_guards"]))
    print("== raised guardrails (timeout 600 s, no cost guard)", flush=True)
    results["raised"] = run(layer(a.url, QueryLimits(timeout_s=600, max_rows=100_000, max_cost=1e15)), a.runs)
    print(markdown(results["raised"]))
    if a.out:
        Path(a.out).write_text(json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    main()
