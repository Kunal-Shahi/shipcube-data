# MCP tools

The data layer gives coding agents six tools. ShipCube's MCP server (in the main `shipcube` repo) will register them and forward each call to the ShipCube API. The API builds the `SecurityContext` and calls `DataLayer` (`src/shipcube_data/engine.py`).

| Tool | Scope | Calls |
|---|---|---|
| `list_cubes` | `data:read` | `DataLayer.list_cubes` |
| `describe_cube` | `data:read` | `DataLayer.describe_cube` |
| `query` | `data:read` | `DataLayer.query` / `DataLayer.compile` |
| `search_values` | `data:read` | `DataLayer.search_values` |
| `propose_migration` | `data:write` | `Runner.propose` |
| `apply_migration` | `data:write` | `Runner.apply(require_approval=True, allow_breaking=False)` |

There is also an optional seventh tool, `raw_sql` (L3). It needs the `data:raw` scope and a workspace setting, and is off by default.

**Security context.** The MCP server never accepts `tenant`, `roles` or policy attributes as tool arguments. The API derives them:

- the **workspace** comes from the bearer token (`sc_pat_…` or an OAuth access token);
- the **tenant** is the app or workspace binding the token is scoped to;
- **roles and attributes** come from the token owner's workspace membership.

An agent can only ever see what its human could see.

**Scope names.** `data:read` matches the scope list the console uses. The networking design note calls it `data:query`. The two need to settle on one name before the tools are wired.

---

## Agent guidance (shared tool preamble)

> ShipCube's data layer is a semantic layer. Ask for **members** (`cube.member`), not tables and columns.
>
> 1. Call `list_cubes`, then `describe_cube` for the cubes you need. Members have titles, descriptions, types, formats and deprecations: read them.
> 2. Use `search_values` to find the exact spelling of a filter value before filtering on it.
> 3. Query with `query`. Prefer one query with several measures over several queries. Add `time.range` whenever a cube is partitioned (`describe_cube` shows `partitioned_by`).
> 4. If a member you need is missing, use `propose_migration`. Never invent SQL. A human approves it in the console. Show the user the `impact` and the `approval_url`.
> 5. Treat `warnings` (renamed, deprecated, full scan) as things to fix in the app you are writing.

---

## `list_cubes`

Lists the cubes and members the caller can see.

**Input**

```json
{"type": "object", "properties": {}, "additionalProperties": false}
```

**Output** (members the caller isn't granted are omitted)

```json
{"model_version": 5, "cubes": [
  {"name": "orders", "title": "Orders", "description": null,
   "measures": ["count", "delivered_count", "late_count", "late_rate", "unique_orders"],
   "dimensions": ["delivered_at", "estimated_delivery_at", "is_late", "order_id", "purchased_at", "status"],
   "joins": ["customers", "order_items"]}
]}
```

## `describe_cube`

Returns the full metadata for one cube: the LLM metadata contract.

**Input**

```json
{"type": "object", "required": ["cube"], "properties": {"cube": {"type": "string"}}, "additionalProperties": false}
```

**Output**

```json
{
  "name": "orders", "title": "Orders", "description": "One row per order",
  "measures": [
    {"path": "orders.late_rate", "title": "Late rate", "agg": "number", "additive": false, "format": "percent",
     "description": "Share of delivered orders that arrived after the estimated date"},
    {"path": "orders.count", "title": "Count", "agg": "count", "additive": true}
  ],
  "dimensions": [
    {"path": "orders.purchased_at", "title": "Purchased at", "type": "time", "granularities": ["day", "month", "year"]},
    {"path": "orders.status", "title": "Status", "type": "string"}
  ],
  "segments": [{"path": "orders.delivered", "title": "Delivered"}],
  "joins": [{"to": "customers", "relationship": "many_to_one"}, {"to": "order_items", "relationship": "one_to_many"}],
  "aliases": {"orders.ordered_at": "orders.purchased_at"},
  "partitioned_by": {"column": "order_purchase_timestamp", "kind": "range", "granularity": "month", "filter_required": false},
  "model_version": 5
}
```

The metadata contract has these rules:

- Every field an agent needs to write a correct query is here: types, additivity (whether `running` makes sense), join directions (whether a query will fan out), partitioning (whether a time range is needed), formats and deprecations.
- Hidden members are absent, not marked hidden.
- A cube with no visible members behaves as if it doesn't exist.

## `query`

Runs a typed L0/L1 query, or compiles it without running it.

**Input**

```json
{
  "type": "object",
  "required": ["query"],
  "properties": {
    "query": {"$ref": "#/$defs/Query"},
    "mode": {"enum": ["run", "sql"], "default": "run",
             "description": "sql: compile only and return the SQL for the warehouse dialect"},
    "dialect": {"enum": ["duckdb", "postgres", "bigquery", "clickhouse"],
                "description": "mode=sql only: render for this dialect"}
  },
  "additionalProperties": false
}
```

`$defs/Query` is `Query.model_json_schema()` from `query.py`. The server publishes it as is, so it never drifts from the validator. The query shape is in [QUERY.md](QUERY.md).

**Output** is the result object from [QUERY.md](QUERY.md#result): `columns`, `rows`, `sql`, `model_version`, `fingerprint`, `partitions`, `warnings`, `verified`, `cache`. The server truncates `rows` to the MCP response size budget and sets `"truncated": true`. Apps that need everything call the HTTP API.

**Errors** are structured, so agents can repair a query without guessing:

| `code` | Example `message` | Hint to the agent |
|---|---|---|
| `unknown_member` | `unknown or not permitted: orders.revnue` | Closest visible names: `order_items.revenue` |
| `fan_out` | `orders.count would be inflated: joining orders -> order_items is one_to_many…` | Query from the other cube, or use a distinct count |
| `no_join_path` | `no join path from order_items to states` | Propose an `add_join` |
| `partition_filter_required` | `order_items is partitioned by shipping_limit_date; filter on it` | Add `time.range` |
| `invalid_query` | Pydantic field errors (`filters.0.values: in_date_range takes [from, to]`) | |
| `forbidden` | Missing scope or tenant binding | |

## `search_values`

Finds distinct values of a dimension, for grounding filters ("which spelling of São Paulo is in the data?").

**Input**

```json
{"type": "object", "required": ["dimension"],
 "properties": {"dimension": {"type": "string"}, "text": {"type": "string", "default": ""},
                "limit": {"type": "integer", "default": 20, "maximum": 100}},
 "additionalProperties": false}
```

**Output:** `{"dimension": "customers.city", "values": ["rio de janeiro"]}`

This is an ordinary L0 query (`dimensions: [d]`, `contains` filter, `limit`), so RLS, grants and routing apply. A regional manager only sees cities in their region. Production should add a per-dimension `searchable: false` flag, settable with `set_meta`, for free-text or PII dimensions.

## `propose_migration`

Validates, dry-runs and stores a migration for a human to approve. **It never changes the model.**

**Input**

```json
{
  "type": "object",
  "required": ["description", "ops"],
  "properties": {
    "description": {"type": "string", "description": "Why. Stored with the migration and shown to the approver."},
    "ops": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/Op"}}
  },
  "additionalProperties": false
}
```

`$defs/Op` is the JSON Schema of the discriminated union in `ops.py` (`OP_ADAPTER.json_schema()`). [MIGRATIONS.md](MIGRATIONS.md) documents the vocabulary.

**Output:** the dry-run report (see [MIGRATIONS.md](MIGRATIONS.md), "Dry-run and impact analysis") plus three fields:

```json
{
  "id": "0006_count_distinct_ordering_customers", "status": "proposed", "kind": "additive",
  "diff": {"added": ["members:orders.unique_customers"], "changed": [], "removed": []},
  "impact": {"breaks": [], "changes": [], "warnings": [], "unaffected": 3, "apps_broken": []},
  "migration": {"id": "…", "description": "…", "base_version": 5, "ops": […]},
  "approval_url": "https://console.shipcube.dev/w/<workspace>/data/migrations/0006_count_distinct_ordering_customers",
  "auto_approved": false
}
```

**Errors:** `invalid_op` (the op index and message), `conflict` (from the conflict check), and Pydantic field errors.

The proposer is recorded as `agent:<token id>` together with the token owner. ShipCube treats model changes as risky actions, so `approval_url` is a console page where a human reviews:

- the ops;
- the entity diff;
- each broken or changed saved query, with links to the app.

## `apply_migration`

Applies a migration that a human has approved.

**Input**

```json
{"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}, "additionalProperties": false}
```

**Behaviour**

- The status must be `approved`. Otherwise the result is error `needs_approval` with the `approval_url`, and the model doesn't change.
- Apply re-plans before writing. If another migration landed since the proposal, the result can be:
  - `conflict` (re-propose against the new head);
  - `would_break` (the impact now includes `breaks`; a human must re-approve with "allow breaking", which only the console offers).
- On success:

  ```json
  {"id": "…", "status": "applied", "version": 6,
   "fingerprint": {"before": "e7ea…", "after": "6946…"}, "diff": {…}}
  ```

**Notes**

- Applying is idempotent: a second call returns `already_applied`.
- Rollback is not an MCP tool. It is a console or CLI action for workspace admins.

## `raw_sql` (optional, L3)

**Input:** `{"sql": "SELECT …", "limit": 1000}`

**Output:** `{"columns", "rows", "sql", "verified": false, "warnings": ["raw SQL: not checked against the semantic model; …"]}`

**Errors:** `forbidden_sql`, with a reason such as `schema tenant_globex is not readable by tenant acme`, `raw SQL may not call read_csv` or `row policies apply to you; use a typed query`.

The tool description tells agents to use `query` first. `raw_sql` is for one-off exploration, and its results must not be shown as governed metrics.

---

## Wiring checklist (for the ShipCube MCP server)

- [ ] Register the six tools. Generate their input schemas from `Query.model_json_schema()` and `OP_ADAPTER.json_schema()`.
- [ ] Map token → `SecurityContext(tenant, user, roles, attrs)` in the API, never from tool arguments.
- [ ] Map `ModelError` / `MigrationError` messages to the structured error codes above.
- [ ] Add a console page at `/w/<ws>/data/migrations/<id>` with approve and reject buttons that call `Runner.approve` / `Runner.reject` under a human session.
- [ ] Register each deployed app's typed queries as `saved_queries` (name, app, tenant, roles), so impact analysis has something to check.
- [ ] Settle the scope names `data:read`/`data:write` with the console's scope list.
