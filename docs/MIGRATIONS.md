# Model migrations

The semantic model changes only through **migrations**. A migration is an ordered list of small, typed **ops**. Applying one produces a new model version in the catalog. This document defines:

- the ops (the vocabulary);
- the rules for ordering, idempotency, rollback, conflicts and dry-runs;
- how an LLM agent proposes a migration that a human approves.

The vocabulary is implemented in `src/shipcube_data/ops.py` and the runner in `src/shipcube_data/migrate.py`. Every example on this page is a real op that the test suite applies.

---

## 1. Serialization: JSON ops

A migration is a JSON document:

```json
{
  "id": "0006_net_revenue",
  "description": "Revenue is net of freight from now on",
  "ops": [
    {"op": "change_sql", "member": "order_items.revenue", "sql": "price - freight_value"}
  ]
}
```

In a repo, it lives in a file named after its id, such as `migrations/0006_net_revenue.json`. Through MCP, the same object is the `data_propose_migration` argument.

### Why JSON ops

We considered three forms.

| Option | For | Against |
|---|---|---|
| **SQL-like DDL** (`ALTER CUBE orders ADD MEASURE revenue AS SUM(price)`) | Familiar to analysts. Reads well in a diff. | Needs its own grammar and parser, plus a second grammar for embedded SQL. LLMs make up plausible syntax that doesn't exist, and errors are parse errors, not field errors. Hard to give a JSON Schema to MCP clients. |
| **Python DSL** (`orders.add_measure("revenue", sum_("price"))`) | Expressive. Easy to loop and compose. | It is code. Running agent-written code to learn what it *would* do is exactly what we must not do. It can't be stored, diffed or checksummed as data. Behaviour depends on the Python version. |
| **JSON ops** ✅ | MCP tool arguments are already JSON. Pydantic gives a discriminated union and a JSON Schema for free, so agents get exact field errors (`ops.2.name: field required`). Deterministic checksums. Storable as rows. Trivial to render in the console. Easy to generate and to lint. | More verbose than DDL. No comments. |

We reduce the verbosity in three ways:

1. **Tiny required keys.** A cube needs `name` + `table` (or `sql`). A member needs `name` + `sql`. Everything else is inferred (§3).
2. **Inline members in `create_cube`.** Pass `"dimensions": {"status": "order_status"}` instead of one op per member.
3. **One naming rule:** you *name* new things (`name: "orders.revenue"`) and *point at* existing ones (`member: "orders.revenue"`, `cube: "orders"`). Member paths are always `cube.member`.

`description` carries the "why" that a comment would have held, and it is stored with the migration.

---

## 2. The vocabulary

Each op has a **kind**:

- `additive`: cannot change existing results.
- `change`: existing queries keep working, but results or metadata may change.
- `breaking`: existing queries may stop working, or a caller may lose access.

A migration's kind is the most severe kind among its ops. The kind feeds the approval policy (§8).

### 2.1 Cubes

| Op | Required | Optional | Kind |
|---|---|---|---|
| `create_cube` | `name`, one of `table` / `sql` | `title`, `description`, `dimensions{}`, `measures{}` | additive |
| `drop_cube` | `cube` | | breaking |

- **`table`** is a *logical* name: `orders`, not `tenant_acme.orders`. `bind_schema` decides where it lives.
- **`sql`** is a `SELECT` whose unqualified tables are routed the same way.
- Every new cube gets a `count` measure (`COUNT(*)`). Define your own `count` inline to replace it.
- **Inline members:** `"dimensions": {"status": "order_status", "order_id": {"sql": "order_id", "primary_key": true}}`. A string is the SQL. An object takes the same keys as `add_dimension` or `add_measure`.
- `drop_cube` refuses while another cube joins to this one. It also removes the cube's policies and bindings.

```json
{"op": "create_cube", "name": "orders", "table": "orders",
 "dimensions": {"status": "order_status", "purchased_at": "order_purchase_timestamp"},
 "measures": {"delivered_count": {"agg": "count", "filter": "order_status = 'delivered'"}}}
```

### 2.2 Members

| Op | Required | Optional | Kind |
|---|---|---|---|
| `add_measure` | `name` (`cube.member`) | `sql`, `agg`, `filter`, `title`, `description`, `format` | additive |
| `add_dimension` | `name`, `sql` | `type`, `primary_key`, `title`, `description`, `format` | additive |
| `rename_member` | `member`, `to` | | change |
| `deprecate_member` | `member` | `reason`, `replaced_by` | change |
| `change_sql` | `member`, `sql` | `agg`, `filter`, `pin_compatible` | change |
| `drop_member` | `member` | | breaking |
| `set_meta` | `target` (`cube` or `cube.member`) | `title`, `description`, `format` | additive |

**Member SQL** is a scalar expression over the cube's columns, written in the warehouse dialect:

- Unqualified columns are qualified with the cube automatically.
- `{member}` or `{cube.member}` references another member of the same cube. References are expanded recursively, and cycles are rejected.
- A measure that references measures, or that already aggregates (`SUM(x) / COUNT(*)`), is a derived `number` measure.

**`filter`** on a measure makes a filtered aggregate. `{"agg": "count", "filter": "{is_late}"}` becomes `COUNT(CASE WHEN … THEN 1 END)`.

**`rename_member`:**

- Moves the definition to the new name.
- Records `old → new` in `member_aliases`, so queries using the old name still resolve and get a warning. Their output column keeps the old name, so the app's result shape doesn't change.
- Rewrites `{old}` references in the cube's members, segments and policies.
- Follows alias chains (`a → b → c`).

**`change_sql`** closes the old definition at this version and opens a new one (SCD2). The old definition stays in the catalog.

By default a query pinned to an earlier `model_version` gets the **new** definition, with a warning. A change might mask, hash or redact a value, and a pin must never bring the unmasked SQL back, so a forgotten flag fails closed. To let pinned queries keep the old meaning while apps move over, mark the change `"pin_compatible": true`. That says the change is a restatement and the old SQL is safe to run again. The admin who approves the change reviews the flag.

Pins never restore the SQL of a member that was dropped after the pin, or of any member of a cube dropped after the pin, even if something was re-created under the same name.

**`deprecate_member`** keeps the member working. Every query that uses it gets `"orders.x is deprecated (reason); use orders.y"`, and `describe_cube` shows the deprecation to agents.

**`drop_member`** refuses while anything references the member: another member, a segment or a policy. It lists those dependents. It also removes aliases pointing at the member.

### 2.3 Joins and segments

| Op | Required | Optional | Kind |
|---|---|---|---|
| `add_join` | `cube`, `to`, one of `on` / `key` | `relationship` (default `many_to_one`) | additive |
| `drop_join` | `cube`, `to` | | breaking |
| `add_segment` | `name` (`cube.segment`), `sql` | `title`, `description` | additive; widens exposure, so an admin approves it |

- A join is declared once and walked both ways. Walking it backwards inverts the relationship, so `many_to_one` becomes `one_to_many`.
- `key: "customer_id"` is shorthand for `on: "orders.customer_id = customers.customer_id"`.
- Every column in `on` must be qualified with one of the two cube names.

```json
{"op": "add_join", "cube": "orders", "to": "customers", "key": "customer_id"}
{"op": "add_join", "cube": "customers", "to": "states", "on": "customers.customer_state = states.state_code"}
```

### 2.4 Physical layout: schemas and partitions

| Op | Required | Optional | Kind |
|---|---|---|---|
| `bind_schema` | `schema` | `cube` (default `*`), `tenant` (default `*`), `tenant_column`, `shared` | change |
| `define_partition` | `cube`, one of `column` / `dimension` | `granularity`, `values`, `table_pattern`, `start`, `end`, `column_type`, `require_filter` | change |

**`bind_schema`:**

- The most specific binding wins: `(cube, tenant) > (cube, *) > (*, tenant) > (*, *)`.
- `schema` may contain `{tenant}`.
- Binding the same `(cube, tenant)` again replaces the binding.
- `tenant_column` switches to pooled tenancy: shared tables, filtered by that column.
- **Tenant isolation is explicit.** A binding for every tenant (`tenant: "*"`) with neither a `{tenant}` schema nor a `tenant_column` gives every tenant the same rows. That's right for reference data and dangerous by accident, so it must say `"shared": true`.
  - An unmarked one is refused by every query that reads it: typed, staged and governed SQL alike. Raw SQL doesn't see its schema.
  - Re-bind with the flag to fix it.
  - `shared` on a tenant-isolated binding is an error.

```json
{"op": "bind_schema", "schema": "tenant_{tenant}"}
{"op": "bind_schema", "cube": "states", "schema": "shared", "shared": true}
{"op": "bind_schema", "tenant": "bigco", "schema": "bigco_dedicated"}
```

**`define_partition`** infers as much as it can:

- `values` present → `kind: list`, otherwise `range`.
- `table_pattern` present → `strategy: table`, otherwise `column`.
- `granularity` defaults to `month` when the pattern has `{mm}`, otherwise `day`.
- `dimension` → use that dimension's SQL as the column.

Range table strategies need `start`. List patterns need `{value}`.

```json
{"op": "define_partition", "cube": "orders", "dimension": "purchased_at",
 "table_pattern": "orders_{yyyy}_{mm}", "start": "2017-01-01", "end": "2018-08-01"}
{"op": "define_partition", "cube": "order_items", "column": "shipping_limit_date", "granularity": "month"}
{"op": "define_partition", "cube": "big_customers", "dimension": "state_key",
 "values": ["sp", "rj", "mg"], "table_pattern": "customers_{value}"}
```

Re-defining a partition replaces the spec.

### 2.5 Access

| Op | Required | Optional | Kind |
|---|---|---|---|
| `grant_member` | `member` (`cube.member` or `cube.*`), `roles` | | breaking |
| `revoke_member` | `member`, `roles` | | breaking |
| `add_row_policy` | `name` (`cube.policy`), `filter` | `roles`, `exempt_roles` | breaking |
| `drop_row_policy` | `policy` | | change |

**`grant_member`:**

- A member with no grants (on itself or its cube) is visible to everyone in the workspace.
- Granting narrows who can see it, which is why the op is `breaking`.
- A member that references a restricted member inherits the restriction, so `gmv = {revenue} + {freight}` is finance-only if `freight` is.

**`add_row_policy`:** the filter uses the query filter language (see [QUERY.md](QUERY.md)). Values may be `$ctx.<key>`, and only dimensions may appear in it. See [DESIGN.md §5.3](DESIGN.md#53-row-level-security-as-injected-filters) for how policies are injected.

```json
{"op": "add_row_policy", "name": "orders.own_region",
 "filter": {"member": "states.region", "op": "equals", "values": ["$ctx.region"]},
 "roles": ["regional_manager"]}
{"op": "grant_member", "member": "order_items.freight", "roles": ["finance"]}
```

---

### 2.6 Pre-aggregations

| Op | Required | Optional | Kind |
|---|---|---|---|
| `define_rollup` | `cube`, `name`, `measures`, `time_dimension`, `start` | `dimensions`, `granularity` (day/week/month, default day), `max_age_hours` (24) | additive |
| `drop_rollup` | `rollup` (`cube.rollup`) | | additive |

**What a rollup can hold:**
- **Measures:** the cube's sum, count, min and max measures; avg (stored as a sum and a count); and number measures built only from those. `count_distinct` and measures that aggregate in their own SQL are refused: they can't be re-aggregated.
- **Dimensions:** on the cube, or on cubes it joins many-to-one, by their current names.
- **Start:** the first month there's data for. Months are built from there to now.

Neither op changes an answer: a query is answered from a rollup only when the rollup gives exactly the warehouse's result (QUERY.md, "Pre-aggregations").

**Renames and drops:**
- Renaming a member updates the rollups that use it.
- Dropping a member a rollup uses is refused, like one a segment uses.

**Data and definitions:** building writes data, not model, so it isn't a migration: `shipcube-data rollup-build`. A change to any definition the rollup reads changes its fingerprint. The months already built then stop being used until they're rebuilt.

## 3. Inference rules

Inference runs when the op is applied, and its result is **stored**. A later change to these rules never changes an existing model. To override an inference, set the field explicitly.

| What | Rule |
|---|---|
| Measure `agg` | No `sql` → `count`. SQL with `{refs}` or an aggregate function → `number`. Otherwise by name: `count_*`/`*_count` → `count`; `unique_*`/`distinct_*` → `count_distinct`; `avg_*`/`average_*`/`mean_*` → `avg`; `min_*` → `min`; `max_*` → `max`; anything else → `sum`. |
| Dimension `type` | `*_at`, `*_date`, `*_time`, `*_timestamp`, `*_day`, `*_month` → `time`. `is_*`/`has_*` → `boolean`. `*_amount`, `*_price`, `*_value`, `*_qty`, `*_quantity`, `*_weight`, `*_score` → `number`. Otherwise `string`. |
| `change_sql` on a measure | New SQL that aggregates → `number`. Otherwise keep the old `agg`, or re-infer it if the old one was `number`. |
| Titles | Not stored. `describe_cube` shows `humanize(name)` until `set_meta` sets a title. |
| Cube | Gets `count`. `relationship` defaults to `many_to_one`. |

A planned improvement is warehouse introspection at apply time: read `INFORMATION_SCHEMA.COLUMNS`, so a dimension's type comes from the column type and a missing column fails the migration instead of the first query.

---

## 4. Ordering and versions

- **Ids** are `NNNN_slug`. The runner applies files in lexical id order. `data_propose_migration` assigns the next free number.
- **Versions.**
  - Each applied migration produces exactly one model version.
  - Versions are monotonic and **never reused**, even after a rollback, because cache keys and pinned queries refer to them.
  - The head is the highest version that hasn't been rolled back.
- **All or nothing.** A migration's ops apply in order to an in-memory copy of the model. If any op fails, nothing is written: the error names the op (`op 2 (change_sql): …`) and the catalog stays at the same head.
- **One transaction.** The diff is written, the version row added and the migration recorded in a single catalog transaction (SQLite `BEGIN IMMEDIATE`, or a Postgres advisory lock per workspace).

## 5. Idempotency

- Applying a migration that is already applied, with the same checksum, is a no-op that returns `already_applied`. `apply_all` skips migrations that are applied, rolled back or rejected.
- The **checksum** is a SHA-256 over the id and the canonical op JSON, with defaults stripped. If a file is edited after it was applied, `status` shows it as `modified` and `apply` refuses with: *"0003_x was edited after it was applied (checksum changed); write a new migration instead."*
- Individual ops are deliberately **strict**, not "create if missing": `create_cube` on an existing cube is an error. Strictness turns copy-paste mistakes and stale proposals into errors instead of silent no-ops. Migration-level idempotency covers re-running.

## 6. Rollback: up and down, or forward-only

Ops have **no hand-written `down`**. The catalog is SCD2 (see [DESIGN.md §4.3](DESIGN.md#43-why-scd2)), so undoing the head version is generic and exact: delete the rows it opened and reopen the rows it closed. The test suite checks that the model fingerprint after a rollback equals the fingerprint before the migration.

| Command | What it does | When |
|---|---|---|
| `rollback --dry-run` | Shows the diff and the impact on saved queries of popping the head. | Always first. |
| `rollback` | Pops the head version. Repeat it to go further back. Only the head can be rolled back. | Development workspaces, or right after a bad apply. |
| *revert* (planned) | Writes a **new forward migration** that undoes an older one: inverse ops computed from the diff. | Shared workspaces where other migrations landed after the bad one. |

The policy is **forward-only in shared workspaces, rollback in development**. Rollback is safe for the catalog, but an app may already use a member the migration added. That is why rollback also runs impact analysis.

## 7. Conflict detection

Each op declares what it **touches**: `cube:orders`, `member:orders.revenue`, `join:customers~orders`, `binding:*@acme`, `partition:orders`, `policy:orders.own_region`, `grant:order_items.freight` or `meta:orders`. The migration stores the union of its ops' touches. Two touch sets overlap when they share a key, or when one has `cube:X` and the other touches anything inside `X`.

A migration also has a **base version**: the head it was written against. `data_propose_migration` sets it automatically. For a file without one:

- If the file sorts after everything applied, the base is the current head.
- If it sorts before an applied migration (out of order, for example from a parallel branch), the base is the version of the latest applied migration with a smaller id.

On apply, the runner compares this migration's touches with every migration applied **after its base**:

- **They overlap: conflict.** The migration is refused and nothing is written:

  ```
  0008_bob conflicts with migrations applied after version 5:
  0007_alice also changed member:order_items.revenue. Rebase it on version 6
  ```

  To rebase, re-propose against the new head. The agent or human re-reads the member and decides again.
- **They don't overlap: it rebases cleanly.** The report says `"rebased": true`.

This is the same rule as a merge conflict, at member granularity.

## 8. Dry-run and impact analysis

`dry_run(migration)` (CLI: `shipcube-data dry-run`; MCP: part of `data_propose_migration`):

1. Validates the JSON (Pydantic) and every op against the current model.
2. Checks conflicts (§7).
3. Computes the **entity diff**: added, changed and removed catalog rows.
4. **Impact.** Compiles every saved query against the old and the new model, under that query's tenant and roles:
   - `breaks`: compiled before, fails after. Includes the error.
   - `changes`: compiles both times, but to different SQL, so the numbers may change.
   - `warnings`: same SQL, new warnings such as "renamed" or "deprecated".
   - `unaffected`: no difference.
   - `apps_broken`: distinct apps among `breaks`.
5. Writes nothing except an audit row.

Here is a real report from the test fixtures:

```json
{
  "id": "0006_rework_items",
  "status": "dry_run",
  "kind": "breaking",
  "base_version": 5, "head_version": 5, "rebased": false,
  "touches": ["join:order_items~products", "member:order_items.revenue",
              "member:orders.ordered_at", "member:orders.purchased_at"],
  "diff": {
    "added":   ["member_aliases:orders.purchased_at", "members:orders.ordered_at"],
    "changed": ["members:order_items.revenue"],
    "removed": ["joins:order_items.products", "members:orders.purchased_at"]
  },
  "fingerprint": {"before": "e7ea17c55c4b1683", "after": "82e5bbff0e8e394b"},
  "impact": {
    "breaks":   [{"query": "revenue_by_category", "app": "sales-dashboard",
                  "error": "no join path from order_items to products"}],
    "changes":  [],
    "warnings": [{"query": "orders_by_month", "app": "ops-app",
                  "warnings": ["orders.purchased_at was renamed to orders.ordered_at; update the query"]}],
    "unaffected": 1,
    "apps_broken": ["sales-dashboard"]
  }
}
```

`apply(..., allow_breaking=False)` refuses a migration whose impact has any `breaks`. The console's Apply button uses this by default, and only an admin can override it.

## 9. Agents propose, humans approve

```
agent (MCP)                     data layer                              person (console)
  │ data_describe_cube ──►                                                   │
  │ data_propose_migration(ops | request | code) ──►                         │
  │        [draft: Muse Spark → validate; else rules]                        │
  │        validate → conflicts → diff → impact → summary                    │
  │        store status=proposed, base_version=head, audit                   │
  │ ◄── {id, summary, diff, impact, approval_url}                            │
  │ (shows the user the summary and impact, links approval_url) ────────────►│
  │                                                       approve(id) ◄──────┤ reviews summary, diff, impact
  │                                                   status=approved        │
  │                                                         apply(id) ◄──────┤ re-plan (conflicts, impact
  │                                                 new model version        │ may have changed), apply
```

Agents have no apply tool at all. Approve and apply are console actions (`DataTools.approve` / `DataTools.apply`), which need a person's console session.

These rules are enforced in `Runner`:

- `propose` never applies anything.
- `approve` requires `status = proposed` and an approver **different from the proposer**. ShipCube's API also requires the approver to be a human session (console ID token), not a PAT or an agent's OAuth token.
- `apply(require_approval=True)` refuses anything that isn't `approved`.
- Drafted proposals (Muse Spark or rules) go through exactly the same validation and dry-run as hand-written ops. `explained_by` records who drafted them.
- Apply re-runs the plan, because other migrations may have landed since the proposal. A conflict at that point sends the proposal back for a rebase.
- Every step is in `audit_log`: `propose → approve → apply`, each with its actor.

**What agents should do** (also in the MCP tool descriptions):

1. Read before writing: call `describe_cube` and check for an existing member before adding one.
2. Put the business meaning in `description`. It is what the next agent reads.
3. Prefer `rename_member` plus `deprecate_member` over `drop_member`.
4. Keep one intent per migration, so conflicts stay small and reviews stay easy.

---

## 10. Worked examples

### 10.1 The Olist demo model

These are `examples/olist/migrations/0001…0005`. The test fixture applies them.

```json
// 0001_bind_schemas.json
{"description": "Every cube reads from its tenant's schema; reference data lives in shared",
 "ops": [{"op": "bind_schema", "schema": "tenant_{tenant}"}]}
```

```json
// 0002_base_cubes.json (excerpt)
{"description": "Olist-shaped base cubes",
 "ops": [
  {"op": "create_cube", "name": "orders", "table": "orders",
   "dimensions": {
     "order_id": {"sql": "order_id", "primary_key": true},
     "status": "order_status",
     "purchased_at": "order_purchase_timestamp",
     "is_late": "order_delivered_customer_date > order_estimated_delivery_date"
   },
   "measures": {
     "unique_orders": "order_id",
     "delivered_count": {"agg": "count", "filter": "order_status = 'delivered'"},
     "late_count": {"agg": "count", "filter": "{is_late}"}
   }},
  {"op": "add_measure", "name": "orders.late_rate",
   "sql": "1.0 * {late_count} / NULLIF({delivered_count}, 0)", "format": "percent",
   "description": "Share of delivered orders that arrived after the estimated date"},
  {"op": "add_segment", "name": "orders.delivered", "sql": "order_status = 'delivered'"},
  {"op": "create_cube", "name": "states", "table": "states",
   "dimensions": {"code": "state_code", "name": "state_name", "region": "region"}},
  {"op": "bind_schema", "cube": "states", "schema": "shared"}
 ]}
```

From this input, the runner infers:

- `purchased_at` is a `time` dimension;
- `unique_orders` is a `count_distinct` measure;
- `late_rate` is a `number` measure;
- `orders` gets a `count` measure.

```json
// 0003_joins.json
{"ops": [
  {"op": "add_join", "cube": "orders", "to": "customers", "key": "customer_id"},
  {"op": "add_join", "cube": "customers", "to": "states", "on": "customers.customer_state = states.state_code"},
  {"op": "add_join", "cube": "order_items", "to": "orders", "key": "order_id"},
  {"op": "add_join", "cube": "order_items", "to": "products", "key": "product_id"}
]}
```

```json
// 0004_partitions.json
{"ops": [
  {"op": "define_partition", "cube": "orders", "dimension": "purchased_at",
   "table_pattern": "orders_{yyyy}_{mm}", "start": "2017-01-01", "end": "2018-08-01"},
  {"op": "define_partition", "cube": "order_items", "column": "shipping_limit_date", "granularity": "month"}
]}
```

```json
// 0005_access.json
{"ops": [
  {"op": "add_row_policy", "name": "orders.own_region",
   "filter": {"member": "states.region", "op": "equals", "values": ["$ctx.region"]},
   "roles": ["regional_manager"]},
  {"op": "grant_member", "member": "order_items.freight", "roles": ["finance"]}
]}
```

```
$ shipcube-data apply --catalog catalog.db --dir examples/olist/migrations
applied 0001_bind_schemas -> v1 (change)
applied 0002_base_cubes -> v2 (change)
applied 0003_joins -> v3 (additive)
applied 0004_partitions -> v4 (change)
applied 0005_access -> v5 (breaking)
head: v5
```

### 10.2 Renaming without breaking anyone

```json
{"id": "0006_rename_revenue", "description": "Revenue is item revenue; free the name for order-level revenue",
 "ops": [
   {"op": "rename_member", "member": "order_items.revenue", "to": "item_revenue"},
   {"op": "deprecate_member", "member": "order_items.item_revenue", "reason": "prefer gmv for totals", "replaced_by": "order_items.gmv"}
 ]}
```

After this migration:

- `{"measures": ["order_items.revenue"]}` still runs.
- Its output column is still `order_items__revenue`.
- It returns two warnings: renamed, and deprecated with `gmv` as the replacement.
- `order_items.gmv`, which was `{revenue} + {freight}`, is rewritten to `{item_revenue} + {freight}`.

### 10.3 Changing a definition, with a pinned reader

This example starts again from the demo model at v5, independent of 10.2.

```json
{"id": "0006_net_revenue", "ops": [{"op": "change_sql", "member": "order_items.revenue", "sql": "price - freight_value"}]}
```

- New queries get the net revenue.
- A dashboard that must not move during the quarter sends `"model_version": 5` and keeps the old definition. The pin only affects definitions: who may see what is always decided by the current version.
- The catalog row history for `order_items.revenue` is `price [v2, v6)`, then `price - freight_value [v6, …)`. `test_change_sql_is_versioned_and_pinnable` asserts exactly this.

### 10.4 An agent proposal

The agent calls `data_propose_migration` with:

```json
{"description": "Count distinct ordering customers",
 "ops": [{"op": "add_measure", "name": "orders.unique_customers", "sql": "customer_id",
          "description": "Distinct customers who ordered"}]}
```

It gets back:

```json
{"id": "0006_count_distinct_ordering_customers", "status": "proposed", "kind": "additive",
 "diff": {"added": ["members:orders.unique_customers"], "changed": [], "removed": []},
 "impact": {"breaks": [], "changes": [], "warnings": [], "unaffected": 3, "apps_broken": []},
 "approval_url": "https://console.shipcube.dev/w/<ws>/data/migrations/0006_count_distinct_ordering_customers"}
```

The agent then tells the user: *"This adds `orders.unique_customers` (count distinct of customer_id). It affects none of your 3 saved queries. Approve it here: …"*

### 10.5 A conflict

Alice and Bob both start from v5:

- Alice: `change_sql order_items.revenue` → applied as v6.
- Bob: `rename_member order_items.revenue → sales` with `base_version: 5` → **refused**: *"0008_bob conflicts with migrations applied after version 5: 0007_alice also changed member:order_items.revenue. Rebase it on version 6."*
- Carol: `add_measure orders.unique_customers` with `base_version: 5` → applied with `"rebased": true`, because her change touches nothing Alice touched.
