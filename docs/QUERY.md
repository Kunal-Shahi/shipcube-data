# Query language

Queries are JSON documents validated by Pydantic (`src/shipcube_data/query.py`). They compile to sqlglot expressions (`compiler.py`) and render for one of four dialects. The language has four levels. Each level adds power and loses some of the guarantees of the level below it.

| Level | Adds | Guarantees | Built |
|---|---|---|---|
| **L0** | Typed measures, dimensions, filters, time, segments, order, limit | Governed definitions, joins chosen by the model, fan-out safety, RLS, routing, pruning | ✅ |
| **L1** | Operators: `share`, `rank`, `lag`, `running`, `compare` | Same as L0. Operators work on L0 output columns only. | ✅ |
| **L2** | Stages: CTEs, set operations, aliased self-joins, subqueries | Every stage that reads data is an L0/L1 query, so it is still governed | Next (specified below) |
| **L3** | Read-only raw SQL | Only scoping: one SELECT, own schemas, no table functions. Results are labelled `verified: false`. | ✅ (validator) |

No level has company-specific metric kinds. Funnels, cohorts and retention are built from L1 and L2.

---

## L0: typed queries

```json
{
  "measures":   ["orders.count", "order_items.revenue"],
  "dimensions": ["states.region"],
  "time":       {"dimension": "orders.purchased_at", "granularity": "month", "range": ["2018-01-01", "2018-03-31"]},
  "filters":    [{"member": "customers.city", "op": "contains", "values": ["rio"]}],
  "segments":   ["orders.delivered"],
  "order":      [{"member": "orders.count", "dir": "desc"}],
  "limit":      100,
  "offset":     0,
  "model_version": null
}
```

| Field | Meaning |
|---|---|
| `measures` | Aggregates. Each is aggregated from its own cube (see "fan-out" below). |
| `dimensions` | Group-by columns. Their cubes must be reachable through the model's joins. |
| `time` | One time dimension. `granularity` (`day`, `month` or `year`) adds a truncated group-by column. `range` is an inclusive `[from, to]`: a date-only `to` covers the whole day. Without a granularity, `time` only filters. |
| `filters` | A list (AND) of conditions or `{"and": […]}`, `{"or": […]}`, `{"not": …}` trees. A tree is either all dimensions (goes to `WHERE`) or all measures (goes to `HAVING`), never mixed. |
| `segments` | Named filters from the model (`cube.segment`). |
| `order` | Member paths, output column names, or calc names. Default: time ascending if grouped by time, else the first measure descending. |
| `limit` / `offset` | `limit` defaults to 10,000, which is also the cap. The result warns when the default applies. |
| `model_version` | Compile against an older model version (see [MIGRATIONS.md](MIGRATIONS.md), "Changing a definition, with a pinned reader"). |

### Filter operators

| `op` | Values | SQL |
|---|---|---|
| `equals` / `not_equals` | 1 or more | `=` or `IN`, and `NOT` of that |
| `contains` / `not_contains` / `starts_with` / `ends_with` | 1 or more (OR-ed) | `LOWER(x) LIKE '%v%'`, with `%` and `_` in the value escaped |
| `gt` / `gte` / `lt` / `lte` | exactly 1 | Comparison. A value on a time dimension is cast to `TIMESTAMP`. |
| `set` / `not_set` | none | `IS NOT NULL` / `IS NULL` |
| `in_date_range` | `[from, to]` | `x >= from AND x < to + 1 day` (date-only `to`) |
| `before_date` / `after_date` | exactly 1 | `x < v` / `x >= v` |

Values are JSON scalars and always become SQL **literals** through sqlglot. Neither query values nor `$ctx` values are ever spliced into SQL text.

### Result

```json
{
  "columns": [{"name": "states__region", "member": "states.region", "kind": "dimension", "type": "string", "title": "Region"}, …],
  "rows": [{"states__region": "Southeast", "orders__count": 206, …}],
  "sql": "…", "model_version": 5, "fingerprint": "…",
  "partitions": {"orders": {"strategy": "table", "total": 20, "scanned": 3, "tables": ["orders_2018_01", …]}},
  "warnings": ["…"], "verified": true, "cache": "miss"
}
```

- Column names are `cube__member` or `cube__member__granularity`. Double underscores keep them valid in every dialect; BigQuery forbids dots in column names.
- A column keeps the name the query used, even if the member was renamed since.

### Fan-out and multi-fact queries

Measures are grouped by cube. Each group walks only `many_to_one` (or `one_to_one`) joins to reach dimensions. When a query has measures from two cubes, each group becomes a CTE, and the CTEs are joined on the dimensions:

```sql
WITH g_orders AS (SELECT states.region AS states__region, COUNT(*) AS orders__count
                  FROM (…orders shards…) AS orders LEFT JOIN … GROUP BY states.region),
     g_order_items AS (SELECT states.region AS states__region, SUM(order_items.price) AS order_items__revenue
                  FROM tenant_acme.order_items AS order_items LEFT JOIN … GROUP BY states.region)
     keys AS (SELECT states__region FROM g_orders UNION SELECT states__region FROM g_order_items)
SELECT keys.states__region, g_orders.orders__count, g_order_items.order_items__revenue
FROM keys
LEFT JOIN g_orders      ON g_orders.states__region      IS NOT DISTINCT FROM keys.states__region
LEFT JOIN g_order_items ON g_order_items.states__region IS NOT DISTINCT FROM keys.states__region
```

Suppose a group would have to walk a `one_to_many` edge, such as `orders.count` by `products.category` (orders → order_items → products):

- If the measures are fan-out safe (`count_distinct`, `min`, `max`), the query compiles.
- Otherwise it is refused: *"orders.count would be inflated: joining orders -> order_items is one_to_many. Query it from order_items's side, or use a count_distinct/min/max measure."*

---

## L1: operators

`calcs` adds columns computed over the L0 result. The compiler wraps L0 as `WITH base AS (…)` and selects `base.*` plus one expression per calc.

```json
"calcs": [
  {"name": "share_of_year", "op": "share",   "measure": "order_items.revenue", "by": ["orders.purchased_at"]},
  {"name": "rank_in_year",  "op": "rank",    "measure": "order_items.revenue", "by": ["orders.purchased_at"]},
  {"name": "running",       "op": "running", "measure": "order_items.revenue", "by": ["states.region"]},
  {"name": "last_period",   "op": "lag",     "measure": "order_items.revenue", "by": ["states.region"], "offset": 1},
  {"name": "growth",        "op": "compare", "measure": "orders.count", "offset": 1, "mode": "pct"}
]
```

| `op` | Compiles to | Notes |
|---|---|---|
| `share` | `m / NULLIF(SUM(m) OVER (PARTITION BY by), 0)` | `by: []` gives the share of the grand total. |
| `rank` | `RANK() OVER (PARTITION BY by ORDER BY m DESC)` | |
| `lag` | `LAG(m, offset) OVER (PARTITION BY by ORDER BY time)` | Needs `time.granularity`. Works row by row, so gaps shift it. |
| `running` | `SUM(m) OVER (PARTITION BY by ORDER BY time ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)` | Refused for non-additive measures (`avg`, `count_distinct`, `number`). |
| `compare` | Self-join of `base` on `time = prior.time + offset·granularity` and the other dimensions (null-safe). `mode`: `pct` (default), `diff` or `prior`. | Correct across gaps in the series (see below). |

`by` lists dimensions that are already in the query. `measure` must be one of the query's measures.

**Why `compare` uses a self-join.** `LAG` compares with the previous *row*, not the previous *period*. When March is missing (filtered out, or no orders), `LAG` compares April with February. Period-over-period has to be exact, so `compare` joins on the shifted period instead. `test_compare_is_correct_across_gaps` covers this.

---

## L2: stages (specified; next to build)

L2 composes named stages. **Every stage that reads data is an L0/L1 query**, so routing, RLS, grants and pruning still apply. The remaining stage kinds only work on the columns of earlier stages. This is how L2 adds power without opening a way around governance.

```json
{
  "stages": {
    "y2017": {"query": {"measures": ["orders.count"], "dimensions": ["customers.customer_id"],
                        "time": {"dimension": "orders.purchased_at", "range": ["2017-01-01", "2017-12-31"]}}},
    "y2018": {"query": {"measures": ["orders.count"], "dimensions": ["customers.customer_id"],
                        "time": {"dimension": "orders.purchased_at", "range": ["2018-01-01", "2018-12-31"]}}},
    "growers": {
      "from": "y2018", "as": "cur",
      "join": [{"stage": "y2017", "as": "prev", "type": "left", "on": [["cur.customers__customer_id", "prev.customers__customer_id"]]}],
      "select": {"customer_id": "cur.customers__customer_id", "orders_2018": "cur.orders__count", "orders_2017": "prev.orders__count"},
      "where": [{"column": "orders_2018", "op": "gt", "column_ref": "orders_2017"}]
    }
  },
  "output": "growers",
  "order": [{"column": "orders_2018", "dir": "desc"}],
  "limit": 50
}
```

| Stage kind | Fields | Compiles to |
|---|---|---|
| **query** | `query` (L0/L1) | A CTE holding the compiled L0/L1 SQL |
| **from/join** | `from`, `as`, `join[{stage, as, type, on}]` | Joins between CTEs. The same stage may appear twice under different aliases (self-join). |
| **select** | `{name: column-or-expression}` | Projection. Expressions are a whitelisted, typed arithmetic tree (`{"div": [a, b]}`), not SQL strings. |
| **where** | Conditions over columns. `column_ref` compares two columns. | `WHERE` |
| **group** | `{"by": [...], "agg": {name: {"sum": column}}}` | Re-aggregation over a stage. For example, "average monthly revenue": monthly sum, then average. |
| **set** | `{"union_all" \| "union" \| "intersect" \| "except": [stage, …]}` | Set operations. The column lists must match by name and type. |
| **in_stage** filter | `{"column": c, "op": "in_stage", "stage": s, "stage_column": c2}` | `c IN (SELECT c2 FROM s)` as a semi-join subquery. Also `not_in_stage`. |

Typical shapes:

- **Funnels and conversion:** stages per step, then `in_stage`.
- **Cohorts:** a query stage for the first-order month per customer, joined to activity per month.
- **Retention:** a self-join of an activity stage with an offset.
- **Top N per group:** L1 `rank` in a query stage, then `where rank <= N`.

---

## L3: validated raw SQL

`raw_sql(sql)` is the escape hatch. The validator (`rawsql.py`) works like this:

1. The input must parse as **exactly one statement**, and that statement must be a query (`SELECT`/`UNION`/`WITH … SELECT`).
2. It rejects any DDL, DML, `SET`, `COPY`, `ATTACH`, `PRAGMA` or `SELECT … INTO` node anywhere in the tree.
3. It rejects **table functions** and file or network readers (`read_csv`, `read_parquet`, `glob`, `getenv`, `pg_read_file`, `external_query`, …).
4. It rejects three-part names: no other database or catalog.
5. Unqualified tables are qualified with the tenant's default schema. Qualified tables must be in a schema the tenant is bound to. `information_schema`, `pg_catalog`, `system` and `main` are always refused.
6. **It refuses outright when any row policy applies to the caller.** Raw SQL cannot have policies injected reliably.
7. It wraps the statement as `SELECT * FROM (…) AS raw LIMIT n`.

The response has `"verified": false` and a warning that the numbers are not governed metrics. Production also runs L3 on a read-only warehouse role with a statement timeout and, on BigQuery, `maximum_bytes_billed`.

A deny-list is weaker than an allow-list. A production version should allow-list functions per dialect using sqlglot's typed function classes, and treat any `exp.Anonymous` function as denied.

---

## Compilation and dialects

All SQL is built as sqlglot expressions and never by string concatenation. Member and segment SQL is parsed in the catalog's `dialect` setting. The output is rendered with `expression.sql(dialect=…)`:

| Construct | DuckDB | Postgres | BigQuery | ClickHouse |
|---|---|---|---|---|
| Time truncation | `DATE_TRUNC('MONTH', x)` | `DATE_TRUNC('MONTH', x)` | `TIMESTAMP_TRUNC(x, MONTH)` | `dateTrunc('MONTH', x)` |
| Timestamp literal | `CAST('…' AS TIMESTAMP)` | `CAST('…' AS TIMESTAMP)` | `CAST('…' AS DATETIME)` ⚠️ | `CAST('…' AS Nullable(DateTime))` |
| Period shift (`compare`) | `x + INTERVAL 1 MONTH` | `x + INTERVAL '1 MONTH'` | `DATE_ADD(x, INTERVAL 1 MONTH)` | `DATE_ADD(MONTH, 1, x)` |
| Null-safe join (stitching) | `LEFT JOIN … IS NOT DISTINCT FROM` | same | same | same ⚠️ |

The tests compile the same multi-fact, filtered, pruned, `compare` query to all four dialects and parse each result back with that dialect's parser.

The integration suite (`tests/integration/`) then *executes* a set of governed queries on DuckDB, Cloud SQL Postgres and BigQuery, and requires identical answers. The set covers:

- partition pruning;
- multi-fact stitching;
- fan-out-safe distinct counts;
- filters and segments;
- HAVING;
- L1 `share`, `running` and `compare`;
- row policies.

Dialect handling found or confirmed this way:

- **BigQuery's `TIMESTAMP`** is sqlglot's `TIMESTAMPTZ`; its `DATETIME` is naive. Timestamp literals are cast to `TIMESTAMP` for BigQuery.
- **BigQuery period shifts.** `TIMESTAMP_ADD` has no `MONTH` or `YEAR`, so `compare` shifts through `DATETIME_ADD`.
- **Postgres stitching.** Postgres rejects `FULL JOIN` on `IS NOT DISTINCT FROM`, hence the `keys` union above.
- **ClickHouse** supports `IS NOT DISTINCT FROM` in `JOIN ON` only in recent versions. The fallback is `isNotDistinctFrom`. It also needs `join_use_nulls = 1` for correct stitching.
- **Integer division** differs by dialect: Postgres truncates. Ratio measures should multiply by `1.0` (the demo's `late_rate` does), or the compiler could cast `number` measures to a float type.
