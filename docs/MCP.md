# MCP tools

The data layer gives coding agents six tools. They ship as an **in-process Python library**, `shipcube_data.tools.DataTools`, which ShipCube's MCP server mounts directly. The library has no transport of its own.

```python
from shipcube_data.tools import DataTools, ToolError
from shipcube_data.identity import Caller

tools = DataTools(layer_for=lambda workspace_id: data_layer_for(workspace_id))

for spec in tools.specs():            # name, description, inputSchema (JSON Schema), scope
    server.register(spec, handler=lambda args, caller, n=spec["name"]: tools.call(n, args, caller))

# per request, after verifying the sc_pat_ / OAuth token:
caller = Caller(workspace_id=ws.id, workspace_slug=ws.slug, via="mcp", email=user.email,
                user_id=user.id, hosted_domain=user.hosted_domain, membership_role=m.role,
                token_id=token.id, scopes=token.scopes)
try:
    result = tools.call("query", args, caller)   # plain JSON-able dict
except ToolError as e:
    return mcp_error(e.to_dict())                # {"code", "message", "hint"}
```

`DataTools.approve(migration_id, caller)` is the console's approve action. It is not an MCP tool.

**Language decision.** Mounting in-process assumes the MCP server is Python, as language's PR #4 recommends. If the server stays in TypeScript (networking's PR #1), the same `DataTools.specs()`/`call()` pair sits behind a small internal HTTP endpoint, and nothing else changes. This waits on Kunal's language decision.

| Tool | Scope | Calls |
|---|---|---|
| `list_cubes` | `data:read` | `DataLayer.list_cubes` |
| `describe_cube` | `data:read` | `DataLayer.describe_cube` |
| `query` | `data:read` | `DataLayer.query` / `DataLayer.compile` |
| `search_values` | `data:read` | `DataLayer.search_values` |
| `propose_migration` | `data:write` | `Runner.propose` |
| `apply_migration` | `data:write` | `Runner.apply(require_approval=True, allow_breaking=False)` |

There is also an optional seventh tool, `raw_sql` (L3). It needs the `data:raw` scope and a workspace setting, and is off by default.

## Identity: from the security schema to a security context

Tool arguments never carry identity. The server builds a `Caller` (`identity.py`) from the tables in the security workstream's `docs/research/database-design.md`:

| `Caller` field | From |
|---|---|
| `workspace_id`, `workspace_slug` | `workspaces` (the tenant boundary), resolved from the token or URL |
| `email`, `user_id`, `hosted_domain` | `users` (`email`, `id`, Google `hd` claim) |
| `membership_role` | `memberships.role`: `owner`, `admin`, `developer`, `viewer`, or none for an app viewer |
| `via`, `token_id`, `scopes` | `mcp` + `api_tokens.id`/`scopes`, or `console` (human session), or `app` (gateway assertion) |
| `groups`, `app_id` | Google groups when the gateway resolved them; `apps.id` for app traffic (viewer allowed by `access_grants`/IAP) |

`security_context(caller, catalog)` turns that into what the compiler uses:

- **tenant** is the workspace: `ws_<uuid hex>`, which bindings use through `{tenant}`. A data attribute `tenant` can instead assign a principal to a sub-tenant inside the workspace, for an app that serves several customers from one warehouse. For example, domain `acme.com` gets `tenant = acme` and routes to `tenant_acme`.
- **roles** combine two sources. The first is `workspace:<role>` for the membership role and every role below it, so `workspace:admin` implies `workspace:developer` and `workspace:viewer`. The second is the data roles from the catalog's `data_role_assignments`, matched by user email, group, domain or workspace role. These are the same principal types as security's `access_grants`.
- **attrs** for `$ctx` come from the catalog's `data_attributes`, where the most specific principal wins: user > group > domain > workspace role. Verified built-ins come last and cannot be overridden: `email`, `domain`, `user_id`, `workspace_id`, `workspace_slug` and `app_id`.

The library also checks, before anything runs:

| Rule | Error |
|---|---|
| The token has the tool's scope (`data:read` or `data:write`). Console and app requests are bounded by role and IAP instead. | `forbidden` |
| The caller is a workspace member, or is reading through an app. | `forbidden` |
| The catalog belongs to the caller's workspace. | `forbidden` |
| `propose_migration` and `apply_migration` need `developer` or higher. | `forbidden` |
| Approving needs a human console session, not a token. Developers may approve `additive` migrations; `change` and `breaking` migrations need `admin`. The approver must be a different principal from the proposer. The token's owner may approve their own agent's proposal from the console. | `forbidden` |

Data roles and attributes are workspace access settings, not semantic-model changes, so they live outside migrations. The catalog's `assign_role`, `unassign_role` and `set_attribute` record every change in `audit_log`. The console's "Data access" page writes them.

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

**Errors** are structured as `ToolError.to_dict()` → `{"code", "message", "hint"}`, so agents can repair a query without guessing. The codes come from `ModelError.code`:

| `code` | Example `message` | `hint` |
|---|---|---|
| `unknown_member` | `unknown or not permitted: orders.cuont` | `did you mean orders.count?` (only visible members are suggested) |
| `unknown_cube` | `unknown cube 'nope'` | `call list_cubes for the cubes you can see` |
| `fan_out` | `orders.count would be inflated: joining orders -> order_items is one_to_many…` | (in the message) |
| `no_join_path` | `no join path from order_items to states` | `propose an add_join …` |
| `partition_filter_required` | `order_items is partitioned by shipping_limit_date; filter on it` | |
| `invalid_arguments` | `query.limit: Input should be greater than or equal to 1` | `1 error(s) in total` |
| `invalid` | Other query errors, such as mixing measures and dimensions in one filter group | |
| `forbidden` | Missing scope, not a member, wrong workspace, missing `$ctx` value, no schema binding | |

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

**Errors:** `invalid_op` (`op 2 (change_sql): …`), `conflict` (from the conflict check), `invalid_arguments` and `forbidden` (needs `developer`).

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

- The status must be `approved`. Otherwise the result is error `needs_approval`, with the `approval_url` in `hint`, and the model doesn't change.
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

- [ ] Register `DataTools.specs()` as tools. The input schemas are already JSON Schema, generated from the Pydantic models.
- [ ] Build `Caller` from the verified token, membership and workspace (see the identity section above), never from tool arguments.
- [ ] Return `ToolError.to_dict()` as the MCP error payload.
- [ ] Add a console page at `/w/<ws>/data/migrations/<id>` whose approve button calls `DataTools.approve` with the console session's `Caller`.
- [ ] Add a console "Data access" page for data roles and attributes (`Catalog.assign_role` / `set_attribute`).
- [ ] Register each deployed app's typed queries as `saved_queries` (name, app, tenant, roles), so impact analysis has something to check.
- [ ] Settle the scope names `data:read`/`data:write` with the console's scope list.
