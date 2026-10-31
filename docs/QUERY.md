# Query language

Queries are JSON documents validated by Pydantic (`src/shipcube_data/query.py`, `stages.py`). They compile to sqlglot expressions and render for one of four dialects. Each level adds power and gives up some of the guarantees of the level below it.

| Level | Adds | Guarantees | Built |
|---|---|---|---|
| **L0** | Typed measures, dimensions, filters, time, segments, order, limit | Governed definitions, joins chosen by the model, fan-out safety, RLS, routing, pruning | ✅ |
| **L1** | Operators: `share`, `rank`, `lag`, `running`, `compare`, `moving_avg` | Same as L0. Operators work on L0 output columns only. | ✅ |
| **L2** | Stages: joins, derived columns, any window, re-aggregation, pivots, set operations, semi-joins | Every stage that reads data is an L0/L1 query, so it is still governed. `verified: true` | ✅ |
| **SQL** | Any read-only SELECT over cube names | Tenant, grants and row policies by rewriting each cube into its governed view. Your joins aren't checked for fan-out: `verified: false`, `governed: true` | ✅ |
| **L3** | Read-only raw SQL over physical tables | Only scoping: one SELECT, own schemas, no table functions, refused under row policies. `verified: false` | ✅ (off by default) |

There are no company-specific metric kinds. Funnels, cohorts and retention are compositions of L2 stages, or governed SQL. New analytical patterns shouldn't need new operators: L1's operators are shorthands for what L2's `window` expression does in general.

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
| `time` | One time dimension. `granularity` (`day`, `week`, `month`, `quarter` or `year`) adds a truncated group-by column. Weeks start on Monday in every dialect; BigQuery uses `ISOWEEK`, because its `WEEK` starts on Sunday. `range` is an inclusive `[from, to]`: a date-only `to` covers the whole day. Without a granularity, `time` only filters. |
| `filters` | A list (AND) of conditions or `{"and": […]}`, `{"or": […]}`, `{"not": …}` trees. A tree is either all dimensions (goes to `WHERE`) or all measures (goes to `HAVING`), never mixed. |
| `segments` | Named filters from the model (`cube.segment`). A segment that reads a member hidden from the caller, or that member's column, is refused like the member itself. Segment SQL reads only its own cube's columns by plain name: no qualified columns, stars or whole-row references such as `CAST(orders AS TEXT)`. |
| `order` | Member paths, output column names, or calc names. Default: time ascending if grouped by time, else the first measure descending. |
| `limit` / `offset` | `limit` defaults to 10,000, which is also the cap. The result warns when the default applies. |
| `model_version` | Use member definitions (SQL, aggregation, filter) from an older model version (see [MIGRATIONS.md](MIGRATIONS.md), "Changing a definition, with a pinned reader"). Access rules (grants, row policies, bindings), joins and partitions always come from the current version, and members dropped since are gone. |

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
  {"name": "growth",        "op": "compare", "measure": "orders.count", "offset": 1, "mode": "pct"},
  {"name": "trend",         "op": "moving_avg", "measure": "orders.count", "window": 3}
]
```

| `op` | Compiles to | Notes |
|---|---|---|
| `share` | `m / NULLIF(SUM(m) OVER (PARTITION BY by), 0)` | `by: []` gives the share of the grand total. |
| `rank` | `RANK() OVER (PARTITION BY by ORDER BY m DESC)` | |
| `lag` | `LAG(m, offset) OVER (PARTITION BY by ORDER BY time)` | Needs `time.granularity`. Works row by row, so gaps shift it. |
| `running` | `SUM(m) OVER (PARTITION BY by ORDER BY time ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)` | Refused for non-additive measures (`avg`, `count_distinct`, `number`). |
| `compare` | Self-join of `base` on `time = prior.time + offset·granularity` and the other dimensions (null-safe). `mode`: `pct` (default), `diff` or `prior`. | Correct across gaps in the series (see below). Weeks shift by 7 days and quarters by 3 months, so `offset: 4` on quarters is year over year. |
| `moving_avg` | `AVG(m) OVER (PARTITION BY by ORDER BY time ROWS BETWEEN window-1 PRECEDING AND CURRENT ROW)`, NULL until `window` periods are present | Needs `time.granularity`. `window` is 2–366 periods, default 3, the current one included. Like `lag`, it works row by row, so a missing period isn't counted as zero. |

`by` lists dimensions that are already in the query. `measure` must be one of the query's measures.

**Why `compare` uses a self-join.** `LAG` compares with the previous *row*, not the previous *period*. When March is missing (filtered out, or no orders), `LAG` compares April with February. Period-over-period has to be exact, so `compare` joins on the shifted period instead. `test_compare_is_correct_across_gaps` covers this.

---

## L2: stages

L2 composes named stages into one query. **Every stage that reads data is an L0/L1 query**, compiled by the regular compiler, so routing, row policies, grants, fan-out checks and pruning apply inside it. The other stage kinds only see the columns of earlier stages. Expressions are a typed JSON tree, never SQL text. This is how L2 adds power without opening a way around governance (`stages.py`).

```json
{
  "stages": {
    "activity":  {"query": {"measures": ["orders.count"], "dimensions": ["customers.customer_id"],
                            "time": {"dimension": "orders.purchased_at", "granularity": "month"}}},
    "first":     {"from": "activity", "group": {"by": ["customers.customer_id"],
                                                 "agg": {"cohort": {"fn": "min", "of": "orders.purchased_at.month"}}}},
    "joined":    {"from": "activity", "as": "a",
                  "join": [{"stage": "first", "as": "f", "on": [["a.customers.customer_id", "f.customer_id"]]}],
                  "select": {"cohort": "f.cohort", "customer_id": "a.customers.customer_id",
                             "k": {"date_diff": ["month", "f.cohort", "a.orders.purchased_at.month"]}}},
    "retention": {"from": "joined", "group": {"by": ["cohort", "k"],
                                               "agg": {"customers": {"fn": "count_distinct", "of": "customer_id"}}}}
  },
  "output": "retention",
  "order": [{"column": "cohort"}, {"column": "k"}],
  "limit": 500
}
```

Stages are compiled in order, and a stage may use only earlier ones. Up to 30 stages.

| Stage | Fields | Compiles to |
|---|---|---|
| **query** | `query`: an L0/L1 query | A CTE with the compiled query (no default row cap inside) |
| **from** | `from`, `as`, `join[{stage, as, type: inner/left/right/full/cross, on: [[a, b], …]}]` | Joins between earlier stages; a stage may appear twice under different aliases |
| | `where`: conditions on the joined columns | `WHERE` |
| | `select`: `{name: expression}` (default: every column) and `distinct` | Projection, evaluated before `group` |
| | `group`: `{by: [columns], agg: {name: {fn, of}}}` | Re-aggregation of the selected rows. `fn`: sum, avg, min, max, count (rows without `of`), count_distinct, median, stddev, variance |
| | `pivot`: `{index, column, values, agg, prefix}` | One output column per listed value, `<prefix>_<value>` (conditional aggregation, so it's portable). Takes `from` and `where` only |
| | `having`: conditions on the stage's output | Filters after select/group: a window rank, an aggregate |
| | `order`, `limit` | Per-stage ordering and cap (top-N) |
| **set** | `union_all`, `union`, `intersect` or `except`: `[stage, …]` | Set operations; the column lists must match by name and order |

**Column references** are strings: `alias.column`, or just `column` when it's unambiguous. A query stage's columns can be named by output name (`orders__count`) or by member path (`orders.count`, `orders.purchased_at.month`). Group and pivot outputs take the reference's last segment (`orders.purchased_at.quarter` → `quarter`).

**Expressions**: a column reference, a number, `true`/`false`/`null`, or one operator object:

| Operator | Example | Notes |
|---|---|---|
| `lit` | `{"lit": "delivered"}` | A string literal (bare strings are column references) |
| `add`, `sub`, `mul` | `{"add": ["a", "b", 1]}` | |
| `div` | `{"div": ["a", "b"]}` | Floating point, and `NULL` instead of an error on zero |
| `neg` | `{"neg": "a"}` | |
| `fn` | `{"fn": {"name": "coalesce", "args": ["a", 0]}}` | coalesce, nullif, abs, round, floor, ceil, ln, exp, sqrt, power, sign, greatest, least, lower, upper, length, concat |
| `cast` | `{"cast": ["a", "number"]}` | number, integer, string, date, timestamp, boolean |
| `date_trunc` | `{"date_trunc": ["week", "t"]}` | day, week (Monday), month, quarter, year |
| `date_diff` | `{"date_diff": ["month", "start", "end"]}` | Unit boundaries crossed, computed from date parts, so every dialect agrees |
| `date_add` | `{"date_add": ["month", -1, "t"]}` | |
| `extract` | `{"extract": ["dow", "t"]}` | year, quarter, month, week, day, dow, hour |
| `case` | `{"case": [{"when": cond, "then": e}], "else": e}` | |
| `window` | `{"window": {"fn": "avg", "of": "n", "partition": ["region"], "order": ["month"], "frame": [-2, 0]}}` | sum, avg, min, max, count, stddev, rank, dense_rank, row_number, percent_rank, cume_dist, ntile (`buckets`), lag/lead (`offset`), first_value, last_value. `frame` is `[start, end]` in rows relative to the current one; `null` is unbounded |
| a condition | `{"column": "n", "op": "gt", "values": [10]}` | As a boolean value |

**Conditions**: `{"column": expr, "op": op, "values": [literals]}`, or `"column_ref": expr` instead of `values` to compare two columns, combined with `and`, `or` and `not`. Ops: equals (several values become `IN`), not_equals, gt, gte, lt, lte, set, not_set, contains, starts_with, ends_with (case-insensitive), and `in_stage` / `not_in_stage` with `stage` and `stage_column` (a semi-join, NULLs excluded).

Every name L2 emits is quoted, so stages and columns may be called `returning` or `order`. Output names must be snake_case.

Typical shapes:

- **Cohorts and retention:** the example above.
- **Funnels and conversion:** a query stage per step, `in_stage` / `not_in_stage`, and `div` for the rates (`tests/test_stages.py`).
- **Top N per group:** a `row_number` window partitioned by the group, then `having: rank <= N`.
- **Rolling statistics:** `window` with a `frame`.
- **Re-aggregation:** "average monthly revenue" is a monthly query stage, then `group` with `avg`.
- **Crosstabs:** `pivot`.

---

## Governed SQL

When a question doesn't fit typed queries, write SQL against cube names instead of tables:

```sql
SELECT s.region, SUM(i.revenue) AS revenue
FROM order_items i JOIN orders o USING (order_id)
JOIN customers c ON c.customer_id = o.customer_id JOIN states s ON s.code = c.state
GROUP BY 1
```

`govsql.py` parses the statement and replaces every cube reference with that cube's **governed view** for the caller:

- **Rows:**
  - the tenant's schema (and tenant column) and all of its partitions;
  - every row policy that applies to the caller, with the cubes a policy needs joined in. Those joins must be many-to-one, so no row is duplicated; otherwise the cube is refused in SQL.
- **Columns**, under member names:
  - the caller's visible dimensions;
  - the per-row input of each visible sum/avg/min/max/count/count_distinct measure. `SUM(revenue)` is the governed revenue, and `COUNT(late_count)` is the filtered count;
  - join keys only when the model declares them as dimensions, so grants apply to them too. The demo's `0006` declares `orders.customer_id` and `order_items.product_id`.
  - Physical columns, hidden members, and the columns of joins needed only for policies are never exposed. Whole-row references (`CAST(o AS TEXT)`) see only the view.

Everything else is up to the caller: CTEs, joins, windows, subqueries, `CASE`.

**Refused:**
- schema-qualified or unknown tables;
- table functions;
- recursive CTEs, and CTEs named like a cube;
- anything but one SELECT;
- functions outside the model-SQL allowlist (`pg_*`, `*_to_xml`, `dblink*`, `set_config`, file readers, unknown functions).

**What's guaranteed:**
- Results are `governed: true` but `verified: false`. Tenant, grants and row policies hold, but the layer can't check your joins for double counting the way a typed query does.
- **Partition pruning.** A cube's view is narrowed to the partitions the statement can need when it's provably safe: a top-level `AND` term of the `WHERE` of the same SELECT that names the cube, comparing one of its dimensions with a string literal (`>=`, `>`, `<`, `<=`, `=`, `BETWEEN`). Such comparisons reject NULLs, so pruning can't change the answer, even through an outer join (`tests/test_govsql.py` checks pruned and unpruned results are equal). Qualify the column (`o.purchased_at`) when the SELECT joins several sources. Filters in an `OR`, on a CTE's columns, or in another SELECT don't prune; those reads cover every partition.
- The statement is parsed in the warehouse's dialect.

Through MCP: `data_query` with `{"sql": "…"}` instead of `query`. `mode: "sql"` returns the rewritten SQL without running it.

---

## L3: validated raw SQL

`raw_sql(sql)` is the escape hatch. The validator (`rawsql.py`) works like this:

1. The input must parse as **exactly one statement**, and that statement must be a query (`SELECT`/`UNION`/`WITH … SELECT`).
2. It rejects any DDL, DML, `SET`, `COPY`, `ATTACH`, `PRAGMA` or `SELECT … INTO` node anywhere in the tree.
3. It rejects **table functions** and file or network readers (`read_csv`, `read_parquet`, `glob`, `getenv`, `pg_read_file`, `external_query`, …), functions that run SQL or change settings (`query_to_xml` and the other `*_to_xml`, `dblink*`, `set_config`, `xpath`, sequences), and any function sqlglot doesn't recognise unless it is on the allowlist (`ALLOWED_UNTYPED_FUNCS` in `sqlutil.py`). Model SQL (member, segment and join SQL) follows the same rules.
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
