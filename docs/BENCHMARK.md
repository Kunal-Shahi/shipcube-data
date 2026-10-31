# Scale benchmark

`bench/scale.py` generates an Olist-shaped warehouse in Postgres and times the same questions through typed queries (L0/L1), staged queries (L2) and governed SQL. It reports compile time, latency, and how many of the 20 monthly `orders` tables each query reads. Run on 2026-10-31.

## Setup

- **Machine:** Apple M-series laptop, 10 cores, 16 GB RAM, local SSD. Postgres 16 in its own instance:
  - `shared_buffers=2GB`, `effective_cache_size=8GB`, `work_mem=64MB`, 4 parallel workers per query, JIT off;
  - durability settings off for loading only; they don't affect reads.
  - That's roughly a 4-vCPU / 16 GB Cloud SQL machine with warm caches.
- **Data:** generated in Postgres with `generate_series`, deterministic, in the demo's layout:
  - 20 monthly `orders_YYYY_MM` tables, partitions of a range-partitioned parent;
  - one `order_items` table in shipping-date order, with a BRIN index on the partition column;
  - customers, products and states.
- **Runs:** each case runs 3 times and the median is reported. Compile time is the median of 20 compilations. `--default-guards` first runs everything once with the production `QueryLimits`: 30 s timeout and a planner-cost limit of 5,000,000.

| Size | Orders | Order items | Customers | On disk | Load |
|---|---|---|---|---|---|
| 10M | 10,000,000 | 16,667,212 | 2,500,000 | 2.4 GB | 31 s |
| 60M | 60,000,000 | 100,001,664 | 15,000,000 | 14 GB | 235 s |

## Results (guardrails raised: 600 s timeout, no cost limit)

| Case | Level | Compile | Tables read | 10M orders | 60M orders / 100M items |
|---|---|---|---|---|---|
| monthly orders + late rate, Q1 2018 | L0 | 1–2 ms | 3/20 | 168 ms | 1.3 s |
| monthly orders, all time | L0 | 3 ms | 20/20 | 0.9 s | 9.6 s |
| revenue by region (4-cube join), Q1 2018 by shipping date | L0 | 4 ms | 20/20 | 3.1 s | 40 s |
| revenue by region, all time | L0 | 4 ms | 20/20 | 10.4 s | 68 s |
| month-over-month growth by region, 2018 | L1 | 3–11 ms | 8/20 | 1.6 s | 22 s |
| cohort retention, H1 2018 | L2 | 3 ms | 6/20 | 10.9 s | 106 s |
| top-3 categories per region, Q1 2018 | L2 | 5 ms | 20/20 | 5.4 s | 54 s |
| status counts, all time | SQL | 3 ms | 20/20 | 0.65 s | 8.4 s |
| status counts, Q1 2018 | SQL | 1–2 ms | 3/20 | 94 ms | 0.67 s |
| revenue by region (joins), Q1 2018 | SQL | 4 ms | 4/20 | 1.6 s | 30 s |
| 3-month rolling orders, 2018 | SQL | 2–6 ms | 8/20 | 0.35 s | 6.5 s |

**Governed SQL before and after partition pruning (10M orders):**

| Case | Before: tables, median | After: tables, median |
|---|---|---|
| status counts, Q1 2018 | 20/20, 309 ms | 3/20, 94 ms (3.3×) |
| revenue by region (joins), Q1 2018 | 20/20, 1,707 ms | 4/20, 1,643 ms (join-bound) |
| 3-month rolling orders, 2018 | 20/20, 484 ms | 8/20, 347 ms (1.4×) |

**With the production guardrails at 60M orders**, these were refused before reaching the warehouse, or stopped at 30 s:

| Case | Refused by |
|---|---|
| revenue by region, Q1 2018 (L0) | 30 s timeout |
| revenue by region, all time (L0) | cost limit (estimate 7.6M) |
| cohort retention, H1 2018 (L2) | cost limit (estimate 6.3M) |
| top-3 categories per region (L2) | 30 s timeout |

Nothing was refused at 10M.

## With pre-aggregations (60M orders / 100M items, production guardrails)

Two rollups (`bench/scale.py --rollups`), built for every month from 2017-01 to now (118 months):

| Rollup | Holds | Build | Rows | Size |
|---|---|---|---|---|
| `order_items.daily` | revenue, count, avg price by day × region × category × state | 725 s (≈6 s per month) | 81,048 | 9.6 MB |
| `orders.daily` | count, late count, late rate by day × region × status | 86 s | 7,296 | 0.7 MB |

| Case | Level | Answered from | Before (raised limits) | With rollups (production limits) |
|---|---|---|---|---|
| monthly orders + late rate, Q1 2018 | L0 | `orders.daily` | 1.3 s | 3 ms |
| monthly orders, all time | L0 | `orders.daily` | 9.6 s | 3 ms |
| revenue by region (4-cube join), Q1 2018 | L0 | `order_items.daily` | 40 s (refused: timeout) | 5 ms |
| revenue by region, all time | L0 | `order_items.daily` | 68 s (refused: cost limit) | 8 ms |
| month-over-month growth by region, 2018 | L1 | `orders.daily` | 22 s | 4 ms |
| top-3 categories per region, Q1 2018 | L2 | `order_items.daily` (in its query stage) | 54 s (refused: timeout) | 6 ms |
| cohort retention, H1 2018 | L2 | no rollup: per-customer grain | 106 s (refused: cost limit) | refused: cost limit |
| governed SQL cases | SQL | no rollups for SQL yet | 0.6–30 s | unchanged |

**Correctness at scale:** three routed queries were run both ways on the 100M-row data, and the answers matched exactly.

| Query | Rollup | Warehouse |
|---|---|---|
| monthly orders and late rate, Q1 | 15 ms | 1.0 s |
| revenue by region, Q1 | 9 ms | 36.8 s |
| weekly revenue, avg price and count by category, Q1 | 18 ms | 6.8 s |

`tests/test_rollups.py` does the same for every routing case on the demo data.

## Findings

1. **The layer isn't the bottleneck.** Compiling takes 1–11 ms at every size, and the generated SQL is what you'd write by hand. Latency is the warehouse joining and aggregating.
2. **Pruning matters most for single-table and time-filtered questions.** Q1 counts read 3 of 20 tables and run 7–14× faster than all-time counts at both sizes. In governed SQL, pruning cut the Q1 status count from 309 to 94 ms.
   - Postgres was already skipping some tables before pruning, using its partition bounds. Warehouses without those bounds, such as BigQuery date-sharded tables, gain more.
3. **Joins on 100M rows are the expensive part.** Revenue by region joins 100M items to orders, customers and states: 40–68 s. A filter on shipping date doesn't prune `orders`, because the model doesn't know purchase and shipping dates are a few days apart. The governed-SQL version of the same question was faster (30 s) only because it also filtered `orders` by purchase date.
4. **Some L2 questions are heavy at this scale.** Cohort retention groups 21M orders per customer: 106 s.
5. **The default guardrails refuse those heavy queries on purpose,** with clear `cost_limit` and `query_timeout` errors and hints. That's the intended behaviour for an interactive agent. It also means production can't answer them today without pre-aggregation or an asynchronous path.

6. **Pre-aggregations turn the heavy dashboard-style questions into milliseconds.** That holds for every grouped question over region, category, status and time, at any time range. It includes the three the production guardrails refused.
7. **Building rollups costs one warehouse query per month.** That's about 6 s per month for the 4-cube `order_items` rollup at this size, so 725 s for a first build of 118 months. Two things make it slower than it needs to be:
   - every month reads all 20 `orders` tables, because the shipping-date filter doesn't prune them (related bounds would fix that);
   - empty months after the data ends still run.
   Refreshing the current and previous month costs about 12 s.
8. **Rollups don't help per-entity analyses.** Cohort retention groups by customer, so it needs the raw rows. It's still refused by the cost limit in production.

## What would make the slow cases fast

Effects of the items not built yet are estimates.

| What | Effect | Status |
|---|---|---|
| **Pre-aggregations:** materialized rollups per day × region × category, kept fresh by the model's partitions, with matching L0 queries routed to them | 5–68 s → 3–8 ms on the cases above | **Built** (`rollups.py`). Refresh scheduling isn't. |
| **Related partition bounds:** declare that `order_items.shipping_limit_at` is within N days of `orders.purchased_at`, so a filter on one prunes the other | The 4-cube Q1 join reads 4/20 order tables instead of 20/20 | Not built |
| **Integer surrogate keys** in the warehouse instead of text ids | Faster hash joins | A data-modelling choice, outside the layer |
| **Asynchronous long queries** (an operation id, polled) for L2 analyses that legitimately take minutes | Cohorts at 100M+ rows without raising interactive limits | Not built |
| **BigQuery run** of the same benchmark: columnar storage and slot parallelism change the picture | Unknown until measured | Needs `GCP_WIF_PROVIDER`, or a run against the demo project |

## Reproduce

```bash
python bench/scale.py --url postgresql://postgres@localhost:5432/postgres --orders 10000000 --load --default-guards --out bench-10m.json
```
