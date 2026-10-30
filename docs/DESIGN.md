# ShipCube data layer: design

ShipCube's data layer is a semantic layer. Apps and LLM agents ask for `orders.count by states.region for March` instead of writing SQL. The layer turns that into correct, tenant-scoped, partition-pruned SQL for one warehouse.

It differs from most semantic layers in one way: **there are no model files.** The model (cubes, measures, dimensions, joins, segments, partitions, schema bindings, access rules) lives in a **catalog database**. It changes only through versioned **model migrations**, which work like schema migrations but for the semantic layer.

Related docs:

- [MIGRATIONS.md](MIGRATIONS.md): the migration vocabulary, which is the core of the design.
- [QUERY.md](QUERY.md): the query language, levels L0 to L3.
- [MCP.md](MCP.md): the tools coding agents use.
- [RUNBOOK.md](RUNBOOK.md): configuring, deploying and operating the service.

The implementation is in `src/shipcube_data`. Its parts:

- the catalog: Postgres in production, SQLite for local development, with versioned catalog-schema migrations;
- the migration runner, safe under concurrency;
- the L0 and L1 compiler, with schema routing, partition pruning and row-level security;
- warehouse adapters for Cloud SQL Postgres and BigQuery (DuckDB for local development), with timeouts, row limits and cost guards;
- the result cache, structured logs and metrics;
- the L3 validator;
- the MCP tools library.

Tests run on SQLite and Postgres catalogs. Integration tests run the same governed queries on DuckDB, real Postgres and real BigQuery, and require identical answers.

---

## 1. Goals and non-goals

### Goals

1. **Typed queries, not SQL.** Apps and agents query members such as `cube.member`, filters and time ranges. The compiler owns joins, aggregation, fan-out safety and dialect differences.
2. **The model is data.** It lives in a database, so ShipCube's MCP server, console and agents can read and change it through an API. Nothing needs to commit YAML to a repo and redeploy a server.
3. **Every change is a migration.** Each migration is small, reviewable, ordered, checksummed and auditable. Each one produces a new model version with a fingerprint.
4. **Changes are safe to make.**
   - A dry-run reports which saved queries and apps would break or return different numbers.
   - Renames keep the old name working.
   - Definition changes are versioned, so a pinned query keeps its old meaning.
   - Two migrations that touch the same member are detected as a conflict.
5. **Agents propose and humans approve.** An LLM can write a migration and see its impact. A different principal must approve it before it is applied.
6. **One warehouse with many schemas and partitions.** Cubes are defined once against logical tables. Each query is routed to the caller's tenant schema and reads only the partitions it needs.
7. **Security happens in the compiler.** Row-level security is injected as typed filters. Member grants hide columns. The security context comes from ShipCube's verified identity, never from the query.
8. **Portable SQL.** We build sqlglot expressions and render them for DuckDB (demo), Postgres, BigQuery and ClickHouse.

### Non-goals (for now)

- **Pre-aggregations or materialization.** The design leaves room for them (§6), but queries always read base tables today.
- **Many warehouses.** The design assumes one warehouse connection and dialect per catalog. Federation is out of scope.
- **Company-specific metric kinds.** There are no "conversion funnels" or "cohorts" as built-in types. You build them from generic L1 and L2 operators.
- **Data writes.** The layer is read-only against the warehouse. Migrations change the catalog, never warehouse tables.
- **A visual modelling UI.** The console renders migrations and diffs, but the source of truth is the op list.

---

## 2. How this compares

Each system below is described from its public documentation.

| | **ShipCube data layer** | **Cube** (cube.dev) | **dbt Semantic Layer / MetricFlow** | **Malloy** | **LookML** (Looker) |
|---|---|---|---|---|---|
| Where models live | Catalog DB rows, versioned | YAML/JS files in a repo | YAML in a dbt project | `.malloy` source files | `.lkml` files in a Git repo |
| How they change | Typed migration ops: dry-run, approve, apply | Edit files, then redeploy or reload | Edit YAML, then `dbt parse` and deploy | Edit source | Edit in dev mode, then deploy to production |
| Change history | Model versions (SCD2 rows) plus migrations and an audit log | Git | Git | Git | Git |
| Impact of a change | Dry-run compiles saved queries against old and new models | Not built in | Not built in (dbt has lineage for models) | Compiler errors | Content Validator finds broken Looks and dashboards |
| Join model | Explicit joins with a relationship; BFS from the measure's cube | Explicit joins with a relationship | Entities (primary and foreign keys) infer joins | Explicit `join_one` and `join_many` | Explores with explicit joins and relationship |
| Fan-out handling | Per-fact CTEs stitched on dimensions; refuse unsafe measures across one-to-many | Primary-key based deduplication | Joins only along safe entity paths | Symmetric aggregates (automatic) | Symmetric aggregates |
| Multi-tenancy | `bind_schema` routing per tenant, or a pooled tenant column | Driver or schema switching in config code (`contextToAppId`, `driverFactory`, `queryRewrite`) | Not a primary concern | Not a primary concern | User attributes, `access_filter`, connection per model |
| Row security | Typed row policies with `$ctx` values, injected per cube | `queryRewrite` hook in JS | Not built in | Not built in | `access_filter`, `sql_always_where` |
| Partitions | `define_partition`: table-per-partition or column, with pruning | Pre-aggregation partitions | Not built in | Not built in | `sql_trigger`, PDT incremental |
| Query API | JSON (L0/L1/L2), validated raw SQL (L3), MCP | REST/GraphQL/SQL API | JDBC/GraphQL, MetricFlow CLI | Malloy language | Explore UI, API |
| Agent story | MCP tools; agents propose migrations | AI API | Integrations | Language for humans | Gemini in Looker |

### What we borrow

- **From Cube:** the cube, measure and dimension vocabulary; joins with explicit relationships; segments; a JSON query shape with `measures`, `dimensions`, `timeDimensions` and `filters`; a security context that rewrites queries.
- **From MetricFlow:** treating fan-out as a modelling error to avoid, not something to patch up; a time-spine idea for gap-free period comparisons. We get the same correctness with a period self-join (see [QUERY.md](QUERY.md), "Why `compare` uses a self-join").
- **From Malloy:** a small, composable query core, with "nesting" and pipelines as later stages (our L2).
- **From LookML:** content validation before deploy (our impact analysis), and `access_filter`-style row security driven by user attributes (our `$ctx`).

### Why a catalog instead of files

File-based models fit teams whose analytics engineers already work in Git. ShipCube's users build internal apps through a coding agent and an MCP server. For them, an API-first model is a better fit:

- **Agents need a write API with validation.** An agent needs to know immediately whether `add_measure orders.aov` makes sense, what it breaks, and who must approve it. Editing a YAML file gives none of that until deploy.
- **Many tenants share one model.** Changing the model is a governed operation with an audit trail, not a pull request in one tenant's repo.
- **History is queryable.** "What did `revenue` mean on 3 March?" is a SQL query over SCD2 rows. With files, the answer is a `git blame` across deploys.

You can still keep migrations in Git, as the demo's `examples/olist/migrations/*.json` does. Migrations are the reviewable artifact. The catalog is the source of truth.

---

## 3. Architecture

```
 coding agent / app ──MCP or HTTP──►  ShipCube API ──► DataLayer (engine.py)
                                         │                  │
                         verified identity → SecurityContext │
                                                            ▼
            ┌── catalog: ShipCube's Postgres, schema `catalog`, FORCE RLS (SQLite in dev) ──┐
            │ cubes, members, member_aliases, joins, segments, partition_specs, │
            │ schema_bindings, row_policies   (all SCD2: since/until version)   │
            │ model_versions, migrations, saved_queries, audit_log, settings    │
            └───────────────────────────────────────────────────────────────────┘
                     │ load(version) → Model (Pydantic)
                     ▼
   Query (Pydantic) ─► Compiler ─► sqlglot AST ─► SQL (duckdb | postgres | bigquery | clickhouse)
                                                   │
                                     warehouse: one DB, many schemas, partitions
```

| Module | Role |
|---|---|
| `model.py` | The in-memory model. Fingerprints for the whole model and per cube. |
| `ops.py` | The migration vocabulary. Each op is a Pydantic model with `apply()` and `touches()`. |
| `db.py`, `catalog_migrations/` | Connections (Postgres or SQLite), transactions with the per-catalog lock, versioned catalog-schema migrations. |
| `catalog.py` | SCD2 persistence by diffing two models. Model loading at any version. |
| `migrate.py` | Status, dry-run with impact analysis, apply, rollback, propose and approve. |
| `query.py`, `compiler.py`, `partitions.py` | Typed queries in, SQL out. |
| `rawsql.py` | The L3 validator. |
| `engine.py` | The facade the MCP tools call: `list_cubes`, `describe_cube`, `query`, `search_values`, `raw_sql`. Also the result cache, query ids and query logs. |
| `warehouses.py`, `datasources.py` | DuckDB, Cloud SQL Postgres and BigQuery adapters with guardrails; datasources registered from secret references. |
| `identity.py`, `tools.py` | The caller model from the security schema; the `data_` MCP tools and console actions. |
| `drafting.py`, `muse.py` | Migration drafting with Muse Spark, and the deterministic rule fallback. |
| `obs.py` | JSON logs, request and query ids, the metrics hook. |

Migrations are **pure functions on the model**. `apply_ops(model, ops)` returns a new `Model`. The catalog persists the *difference* between the old and new model as SCD2 rows. Because of that:

- dry-run is just "apply in memory and don't persist";
- rollback is generic (§4.3);
- the catalog never stores something the in-memory validator would reject.

---

## 4. The catalog schema

The DDL lives in versioned catalog-schema migrations, `src/shipcube_data/catalog_migrations/{postgres_shared,postgres,sqlite}/NNNN_*.sql`. They are recorded in `catalog_schema_migrations` with a checksum, so editing an applied file is refused. `shipcube-data catalog-migrate` applies them under a session advisory lock. Postgres uses `JSONB` and `TIMESTAMPTZ`; SQLite stores the same data as `TEXT`.

**Production layout: ShipCube's database, schema `catalog`, forced row-level security.** This is security's decision (Oct 29).

- **Rows.** Every table has `workspace_id`, and every statement filters and writes it explicitly.
- **Policy.** Postgres enforces `ENABLE` + `FORCE ROW LEVEL SECURITY` with `USING (workspace_id = app_workspace()) WITH CHECK (workspace_id = app_workspace())`. This is the same mechanism as the API's 17 tenant tables, and the same cross-workspace check covers it.
- **Requests.** They run on the API's own transaction, `db.tx(caller)`, as `shipcube_app` (no BYPASSRLS, no CREATE), which has set `app.workspace_id` from the verified caller. `Catalog.on_connection` and `DataTools(session=…)` do this.
- **It fails closed.** A missing `app.workspace_id` makes `app_workspace()` NULL and every query returns zero rows. A wrong workspace id in code cannot read or write another tenant's rows (`WITH CHECK`).
- **Locking.** Writers in one workspace serialise on `pg_advisory_xact_lock(hashtext('catalog:' || workspace_id))`. Model versions are rows, never DDL.
- **DDL.** `catalog_migrations/postgres_shared/` is applied by the migration role (`shipcube-data catalog-migrate --shared`), after the API's own migrations (it needs `app_workspace()`). Nothing is created at request time.
- **Tests.** `tests/test_shared_catalog.py` checks all of this as a non-owner role. Every catalog-backed test also runs on this layout.

**Standalone layouts** (no ShipCube API): a SQLite file, or one Postgres schema per workspace (`catalog_ws_<hex>`). They use the same workspace-scoped statements.

### 4.1 Model tables (versioned)

Every model table has `since_version INTEGER NOT NULL` and `until_version INTEGER NULL`. A row is valid for versions in `[since, until)`. A partial unique index enforces one current row per key: `... WHERE until_version IS NULL`. Each table has a few typed columns for humans and SQL, plus `spec`, the canonical JSON the loader uses.

| Table | Key | Typed columns | Notes |
|---|---|---|---|
| `cubes` | `name` | `table_name`, `sql`, `title`, `description`, `grants` | A cube has exactly one of `table` (logical name, routed per tenant) or `sql`. |
| `members` | `cube, name` | `kind` (measure or dimension), `agg`, `type`, `sql`, `deprecated`, `grants` | `spec` also holds `filter`, `format`, `primary_key` and `replaced_by`. |
| `member_aliases` | `cube, alias` | `target` | Written by `rename_member`. Old names resolve to new ones. |
| `joins` | `cube, target` | `relationship`, `on_sql` | Stored once and traversed in both directions. The relationship is inverted when walking backwards. |
| `segments` | `cube, name` | `sql` | Named boolean filters. |
| `partition_specs` | `cube` | `column_name`, `kind` (range or list), `strategy` (column or table), `granularity`, `table_pattern` | At most one per cube. |
| `schema_bindings` | `cube, tenant` | `schema_name`, `tenant_column` | `'*'` wildcards. `schema_name` may contain `{tenant}`. |
| `row_policies` | `cube, name` | `filter` (JSON filter tree), `roles` | `spec` also holds `exempt_roles`. |

### 4.2 Bookkeeping tables

| Table | Purpose |
|---|---|
| `model_versions` | `version` (monotonic, never reused), `migration_id`, `fingerprint`, `applied_at`, `applied_by`, `rolled_back_at` |
| `migrations` | `id`, `description`, `checksum`, `ops` (JSON), `touches` (JSON), `kind` (additive, change or breaking), `status` (proposed, approved, applied, rolled_back or rejected), `base_version`, `version`, `proposed_by`, `approved_by`, `impact` (JSON dry-run report), timestamps |
| `saved_queries` | Queries that apps and dashboards depend on, with `app`, `tenant` and `roles`. These are the inputs to impact analysis. ShipCube registers them automatically when an app is deployed. |
| `audit_log` | Every `propose`, `approve`, `reject`, `dry_run`, `apply` and `rollback`, with actor, migration, version and details (including the entity diff). |
| `settings` | `dialect` of the warehouse the member SQL is written in, and the `workspace_id` that owns the catalog. |
| `datasources` | Warehouse connections created in the console: `kind`, and the `secret_refs` reference (`secret_ref_id`, `secret_resource`, pinned `secret_version`). Non-secret `options` only. Credentials are resolved from Secret Manager at connect time. |
| `data_role_assignments` | `(principal_type, principal, role)`. Principal types follow security's `access_grants`: `user` (email), `group`, `domain`, plus `workspace_role`. Gives data roles such as `finance` or `regional_manager`. |
| `data_attributes` | `(principal_type, principal, key, value)`. These are the `$ctx` values for row policies, such as `region`, or a sub-tenant `tenant`. The most specific principal wins. |

`audit_log` uses the same columns as the security schema's `audit_log`: `workspace_id`, `actor_user_id`, `actor_token_id`, `actor_type`, `action` (`data.migration.apply`, `data.role.assign`, …), `target_type`, `target_id`, `detail` and `at`. In production the data layer writes to that table.

Data roles and attributes are **not** versioned or migration-managed. They are access administration, which changes as people join and leave, and they are audited instead. Grants and row policies, which *reference* roles and attributes, are part of the model and do go through migrations.

### 4.3 Why SCD2

- **Point-in-time models.** `load(version=v)` selects rows with `since <= v AND (until IS NULL OR until > v)`. A query can pin `model_version` to keep a member's *definition* while a migration rolls out. `test_change_sql_is_versioned_and_pinnable` covers this. A pin never brings back old access rules: grants, row policies, schema bindings, joins and partitions always come from head (`engine.pin_definitions`; red-team issue #2). A masking `change_sql` (`"masking": true`) counts as an access rule, so is a member dropped and re-created after the pin. Those members keep head's definition under any pin (shipcube-data #8).
- **Generic, exact rollback.** To undo the head version `v`:
  - delete rows with `since = v`;
  - set `until = NULL` on rows with `until = v`;
  - mark `model_versions.v` as rolled back.

  No op needs a hand-written "down" step. `test_rollback_restores_previous_model_exactly` checks that the fingerprint matches afterwards.
- **History for free.** `SELECT * FROM members WHERE cube='order_items' AND name='revenue' ORDER BY since_version` is the definition history.

### 4.4 Fingerprints

- `Model.fingerprint()` is the SHA-256 of the canonical JSON of the whole model. It is stored per version.
- `Model.cube_fingerprint(cubes)` covers only the given cubes, the bindings that apply to them, and their policies. The compiler returns it for every query. It is the cache key's model component (§6).

---

## 5. How a query resolves

The compiler is in `compiler.py`. Its steps, in order:

1. **Load the model** at head. If the query pins `model_version`, take only member definitions (SQL, aggregation, filter) from that version, for members that still exist at head and haven't been masked or re-created since; access rules always come from head.
2. **Resolve members.** For each `cube.member`:
   - Follow rename aliases and warn that the query should be updated.
   - Warn on deprecation, naming `replaced_by`.
   - Check grants. A member is visible if the caller holds one of its grants, or the cube's grants when the member has none. It must also be able to see every member it references. Otherwise `{revenue} + {freight}` would leak a finance-only `freight`.
   - Hidden members produce the same "unknown or not permitted" error as missing ones, so their existence doesn't leak.
3. **Classify filters.** Dimension filters go to `WHERE` and measure filters to `HAVING`. Mixing both in one `or` group is an error.
4. **Group measures by cube.** Each group is aggregated from its own cube, the *root*.
5. **Build the join tree** for each group. This is a BFS from the root over the join graph to every cube the group needs:
   - dimensions
   - filters
   - segments
   - **row policies of every cube in the tree**

   Policies can need more cubes, so this repeats until the set of cubes stops changing.
6. **Fan-out check.** If the tree walks a `one_to_many` edge, every measure in the group must be fan-out safe (`count_distinct`, `min` or `max`). Otherwise the compiler refuses with an explanation. A sum would be silently inflated.
7. **Sources.** For each cube, apply tenant schema routing (§5.1) and partition pruning (§5.2).
8. **Assemble.** `SELECT dims, aggregates FROM root LEFT JOIN ... WHERE ... GROUP BY ... HAVING ...`.
   - With more than one group, each group becomes a CTE.
   - A `keys` CTE is the `UNION` (distinct) of every group's dimension values. Each group is `LEFT JOIN`ed to it on `dim IS NOT DISTINCT FROM dim`. An earlier `FULL OUTER JOIN` form was rejected by real Postgres ("FULL JOIN is only supported with merge-joinable or hash-joinable join conditions"); the integration tests caught it.
   - This is how "orders and item revenue by state" stays correct without symmetric aggregates.
9. **L1 calcs** wrap the result as CTE `base` and add window functions (see [QUERY.md](QUERY.md)).
10. **Render** for the target dialect. The result includes:
    - column metadata
    - the cubes used
    - the partition report
    - warnings
    - the fingerprint

### 5.1 Tenant schema routing

Cubes name **logical** tables (`orders`). `bind_schema` maps them to physical schemas. The most specific binding wins:

```
(cube, tenant)  >  (cube, '*')  >  ('*', tenant)  >  ('*', '*')
```

The demo uses two bindings:

- `{"op": "bind_schema", "schema": "tenant_{tenant}"}` sends every cube to `tenant_acme` or `tenant_globex`.
- `{"op": "bind_schema", "cube": "states", "schema": "shared"}` sends reference data to `shared`.

These properties hold:

- **Fail closed.** No matching binding means an error, never an unqualified table.
- **The tenant comes only from the verified identity.** `identity.security_context` derives it from the caller's workspace (`ws_<uuid hex>`, per the security schema's tenant boundary). A sub-tenant can be assigned through a `tenant` data attribute, for example per customer domain. It must be a valid identifier, which blocks schema-name injection. See [MCP.md](MCP.md#identity-from-the-security-schema-to-a-security-context).
- **Pooled tenancy.** `bind_schema` with `tenant_column` keeps shared tables and adds `cube.<tenant_column> = '<tenant>'` to every query on that cube.
- **SQL cubes too.** Unqualified tables inside a cube's `sql` are qualified with the routed schema. CTE names are left alone.
- **Escape hatch.** A cube whose `table` is already qualified (`analytics.dim_date`) is not routed. Prefer a binding.

### 5.2 Partition pruning

`define_partition` describes how a cube's data is physically split. There are two strategies:

| Strategy | Physical layout | What the compiler does |
|---|---|---|
| `table` (range) | One table per period, for example `orders_2017_01` (BigQuery date-sharded tables, legacy Postgres, archives) | Computes the partitions overlapping the query's bounds and reads `(SELECT * FROM t1 UNION ALL SELECT * FROM t2 …) AS orders`. If nothing overlaps, it reads an empty `WHERE FALSE` source. |
| `table` (list) | One table per key value, for example `customers_sp` (customer sets, regions, big tenants split out) | Keeps the tables whose value matches an `equals` filter. |
| `column` (range) | One natively partitioned table (BigQuery partition column, Postgres declarative partitions, ClickHouse `PARTITION BY`) | Adds `col >= lo AND col < hi+1` on the **raw** column. |

**Where the bounds come from.** Bounds are taken only from AND-level conditions on dimensions whose SQL is the partition column, or a monotone wrapper of it (`CAST`, `DATE_TRUNC`, `DATE`):

- the query's `time.range`;
- `in_date_range`, `gt`/`gte`/`after_date`, `lt`/`lte`/`before_date` and `equals`;
- the same conditions in row policies (so a policy can prune too).

Bounds are conservative: whole dates, inclusive. Pruning can over-read, never under-read.

**Why add the column predicate.** It matters when users filter through a derived dimension, such as `CAST(shipping_limit_date AS DATE)` or a timezone-converted timestamp. Many engines cannot prune through such an expression. The layer adds the predicate on the raw column; see `test_column_partition_predicate_on_derived_dimension`.

**Guards.**

- `require_filter: true` refuses unbounded queries, like BigQuery's `require_partition_filter`.
- Without it, an unbounded query runs, but the result carries a warning such as "reading all 20 partitions".
- The result includes `partitions: {cube: {strategy, total, scanned, tables}}`. ShipCube's load estimator and caps can use it.

**Dialect-specific improvement (not built yet).** On BigQuery, a contiguous run of date-sharded tables can be rendered as a wildcard table with a `_TABLE_SUFFIX BETWEEN` predicate instead of `UNION ALL`.

### 5.3 Row-level security as injected filters

A row policy is a **typed filter tree**, the same language as query filters. Values may be `$ctx.<key>`:

```json
{"op": "add_row_policy", "name": "orders.own_region",
 "filter": {"member": "states.region", "op": "equals", "values": ["$ctx.region"]},
 "roles": ["regional_manager"]}
```

| Property | Behaviour |
|---|---|
| Who it applies to | Callers holding one of `roles`, or everyone if `roles` is empty, minus `exempt_roles`. |
| Where it is injected | Into the `WHERE` of **every group whose join tree contains the cube**. The cubes the policy references are joined in even if the query never mentions them, so `orders.count` alone is still filtered by region. |
| Where to declare it | A policy restricts rows of *its* cube. Declare it on the fact cube you want restricted. A policy on `customers` doesn't affect a query that never touches `customers`. |
| No SQL injection | Values are bound from the `SecurityContext` and rendered as SQL literals by sqlglot, never spliced in as text. `test_ctx_values_are_literals_not_sql` covers this. |
| Fail closed | A missing `$ctx` key is an error. An empty list in the context matches nothing. |
| Raw SQL (L3) | Refused while any policy applies to the caller. |

Member grants (`grant_member`) complement row policies. Policies hide rows; grants hide columns and metrics.

### 5.4 Fan-out: why per-fact CTEs

Take "orders and item revenue by state". Joining `orders → order_items` multiplies order rows, so a naive `COUNT(*)` or `SUM` is wrong.

Looker and Malloy solve this with symmetric aggregates (sum-distinct over primary-key hashes). That approach is clever, but it:

- needs a primary key on every cube;
- is hard to read in generated SQL;
- relies on dialect-specific big-integer tricks.

We take the MetricFlow-like route:

- Aggregate each measure from its own cube, walking only `many_to_one` edges to reach dimensions.
- Join the per-fact results on the dimensions.
- If a measure would have to walk a `one_to_many` edge, compile it only when the measure is fan-out safe (`count_distinct`, `min`, `max`). Otherwise explain the problem and suggest querying from the other side.

---

## 6. Caching and invalidation by fingerprint

The result-cache key (`ResultCache.key`) combines:

```
sha256(
  cube_fingerprint(cubes the query used)   # model part
  + normalized query (without model_version)
  + tenant + sorted roles + ctx attrs        # security part
  + dialect
  + freshness token                          # data part
)
```

- **Model invalidation is exact and needs no extra work.** A migration that changes `customers.state` changes the fingerprint of every query that touches `customers` (including through joins and policies), so those queries miss. A migration that adds a measure to `products` leaves queries over `customers` alone, and they keep hitting. `test_cache_is_keyed_by_the_fingerprint_of_used_cubes` covers both cases. There is no explicit purge: stale entries age out of the LRU.
- **Security is part of the key.** Two tenants, or two role sets, never share an entry. Neither do two values of a policy attribute.
- **Data freshness.** Today this is a TTL (`ResultCache(ttl_seconds=300)`). The planned freshness token is computed per partition:
  - **Closed** range partitions (`orders_2017_03`, or last month's partition of a column-partitioned table) are immutable after a grace period. Their token is constant, so cache entries over them live until evicted.
  - The **open** partition contributes the warehouse's last-modified time (`INFORMATION_SCHEMA.PARTITIONS.last_modified_time` on BigQuery, `system.parts` on ClickHouse, `pg_stat_user_tables` on Postgres) or a short TTL.

  The pruning report (§5.2) tells us exactly which partitions a query read, so the token is cheap to compute.
- **Compile cache.** The model for each version is cached in-process (`Catalog._cache`). Compiling is cheap enough that SQL is not cached separately.
- **Later: pre-aggregations.** Rollup tables would be catalog entities keyed by `(cube_fingerprint, dims, granularity, partition)`. Migrations that change the fingerprint mark them stale, which is the same mechanism.

---

## 7. Production notes

- **Catalog.** This is Cloud SQL Postgres in ShipCube's control project, alongside the security schema (`docs/research/database-design.md` in the main repo). Every table gains `workspace_id uuid references workspaces(id)`. RLS uses the same `app_workspace()` and `app_has_role()` functions:
  - reads need `viewer`;
  - proposals need `developer`;
  - data roles and attributes need `admin`.

  The API reaches the catalog as `shipcube_app`, which has no `BYPASSRLS`.
- **Concurrency.** Every write (`apply`, `propose`, `approve`, `reject`, `rollback`) runs in one transaction that first takes `pg_advisory_xact_lock(<key of the workspace schema>)`. On SQLite, `BEGIN IMMEDIATE` plays this role. Inside the lock the runner re-reads the head and re-checks four things, so concurrent appliers serialise and the loser sees the winner's version:
  - the approval status;
  - the checksum;
  - conflicts;
  - the impact on saved queries.

  Conflicts are checked before ops are applied. So if one migration renames a member while another edits it, the second gets `conflict, rebase` rather than a confusing op error. `tests/test_concurrency.py` races real connections on both backends.
- **Warehouses and guardrails.** Credentials are referenced through `secret_refs` and resolved from Secret Manager at connect time. For BigQuery, the customer's service account is impersonated (or its key is used); ShipCube's own identity never queries a customer project. Every query, L0 to L3, runs through an adapter that enforces `QueryLimits`:

  | Guard | Cloud SQL Postgres | BigQuery | DuckDB (dev) |
  |---|---|---|---|
  | Timeout | `SET LOCAL statement_timeout` | job `result(timeout)` and `job.cancel()` | `interrupt()` from a timer |
  | Rows | server-side cursor, `fetchmany(max_rows + 1)`, `truncated` flag | `max_results = max_rows + 1` | `fetchmany` |
  | Cost | `EXPLAIN (FORMAT JSON)` total cost vs `max_cost` | dry-run bytes vs `max_bytes_billed`, and `maximum_bytes_billed` on the job | none |
  | Read-only | `SET TRANSACTION READ ONLY` | read-only IAM role | read-only file |
  | Tracing | `/* shipcube query_id=… */` comment | job label `shipcube_query_id` | |

  Failures map to error codes: `query_timeout`, `cost_limit`, `warehouse_unavailable` (after retries with backoff) and `warehouse_error`.
- **Observability.** Every query logs one JSON event, `query.ok` or `query.error`, with:
  - `query_id` and `request_id`, and `trace` when the caller sets them;
  - `workspace_id`, `tenant` and `user`;
  - the cubes, fingerprint and model version;
  - partitions scanned, rows, `truncated`, cache hit or miss, and `elapsed_ms`;
  - warehouse stats: planner cost, or bytes processed and billed.

  Migrations log `migration.proposed`, `migration.applied` and `migration.rolled_back`. Muse logs `muse.ok`, `muse.retry` and `draft.muse_fallback`. Logs never contain row data or secrets; SQL is logged as a hash. Metrics go through a `Metrics` hook (`data_queries_total`, `data_query_duration_ms`, `data_query_errors_total`), which the service wires to Cloud Monitoring.
- **Approval URLs.** `data_propose_migration` returns a console URL. Following ShipCube's rule for risky MCP actions, the human approves there, not through the agent's token (see [MCP.md](MCP.md)).
- **Saved queries.** These are registered when an app is deployed: ShipCube's deploy step records the typed queries an app makes. Impact analysis is only as good as this registry.

---

## 8. Status and known limits

| Area | Status |
|---|---|
| Catalog in ShipCube's Postgres (schema `catalog`, FORCE RLS on workspace_id, requests on the API's `db.tx(caller)`), plus standalone SQLite / schema-per-workspace; SCD2, audit, saved queries, versioned catalog-schema migrations | Built; every catalog test runs on all three, RLS tested as a non-owner role |
| Concurrency-safe apply, propose, approve and rollback | Built; raced on both backends |
| Warehouse adapters with timeouts, row limits and cost guards | Cloud SQL Postgres and BigQuery integration-tested against real servers; DuckDB for development |
| Structured JSON logs with query and request ids; metrics hook | Built |
| Migration runner: apply, status, dry-run with impact, rollback, conflict detection, propose, approve | Built |
| 18 ops (see [MIGRATIONS.md](MIGRATIONS.md)) | Built |
| L0 compiler: routing, pruning (table, column and list), RLS, grants, fan-out, multi-fact stitching | Built |
| L1 operators: `share`, `rank`, `lag`, `running`, `compare` | Built |
| L2 stages | Designed in [QUERY.md](QUERY.md), not built |
| L3 validator | Built (DuckDB-focused deny-list) |
| In-process MCP library (`tools.py`, five `data_` tools plus console-only approve, apply and register_datasource) and identity mapping from the security schema (`identity.py`) | Built |
| Migration drafting: real Muse Spark (`muse-spark-1.3`) when `META_API_KEY` is set, rule fallback otherwise (logged) | Built; live-tested |
| Datasource registration from secret references; DuckDB, Postgres and BigQuery connectors | Built |
| Installable package (`pip install git+…`, examples bundled in the wheel) | Built |
| Dialects | DuckDB, Postgres and BigQuery are executed in tests. ClickHouse compiles and parses back, but has no adapter or live test yet. |
| Next | L1 operators extended and L2 stages (stages are specified in [QUERY.md](QUERY.md)); freshness tokens; pre-aggregations; BigQuery wildcard rendering; a ClickHouse adapter |

Known limits:

- **Result cache.** It is in-process, so each Cloud Run instance has its own. Use Memorystore when instances scale out.
- **Rollback** pops only the head version. Older migrations are reverted with a new forward migration, written by hand for now.
- **Row-limit truncation on Postgres** happens after the planner's LIMIT; the compiler already caps `limit` at `max_rows`.
- **The Postgres cost guard is the planner's estimate,** not a measured cost. Stale statistics can let an expensive query through, which the timeout then catches.
- **The L3 validator is a deny-list** tuned for DuckDB and Postgres. Production keeps L3 off by default.
