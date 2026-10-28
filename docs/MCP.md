# MCP tools

The data layer gives coding agents five tools. ShipCube's MCP server mounts them with a `data_` prefix, as Kunal decided on Oct 28 (shipcube `docs/WORKSTREAMS.md`, under "Decided").

**Agents can never change the model.** They read, query and *propose*. A person approves and applies migrations, and connects warehouses, in the console. Those actions are library functions for the console and API, not MCP tools.

## Install and import

The package is `shipcube-data`. It needs Python 3.12, which is the language decision for the MCP server and API. Add it as a git dependency:

```toml
# services/mcp/pyproject.toml (or services/api)
dependencies = [
  "shipcube-data @ git+https://github.com/Kunal-Shahi/shipcube-data@main",
  # pin a commit or tag once one is cut, e.g. ...@v0.1.0
]
```

Add the `[duckdb]` extra only where the DuckDB demo warehouse is needed:

```bash
uv pip install "shipcube-data[duckdb] @ git+https://github.com/Kunal-Shahi/shipcube-data@main"
```

Everything the server and API need is exported from the top-level package:

```python
from shipcube_data import (
    DataTools, ToolError,          # the MCP tools + console actions
    Caller,                        # identity built from the security schema
    Catalog, DataLayer,            # catalog DB and query engine
    MuseDrafter,                   # migration drafting (Muse Spark when META_API_KEY is set, else rules)
    Datasource, SecretRef,         # warehouse connections
)

from shipcube_api import db
from shipcube_data.local import tools_from_env

# Every tool call runs in the API's db.tx(caller): the shared catalog under RLS, as shipcube_app.
tools = tools_from_env(catalog_session=lambda caller: db.tx(caller))   # drafter defaults to MuseDrafter.from_env()

for spec in tools.specs():                   # data_list_cubes … data_propose_migration
    server.register(spec["name"], spec["description"], spec["inputSchema"],
                    handler=lambda args, caller, n=spec["name"]: tools.call(n, args, caller))

# per request, after verifying the sc_pat_ / OAuth token:
caller = Caller(workspace_id=ws.id, workspace_slug=ws.slug, via="mcp", email=user.email,
                user_id=user.id, hosted_domain=user.hosted_domain, membership_role=m.role,
                token_id=token.id, scopes=token.scopes)
try:
    result = tools.call("data_query", args, caller)   # plain JSON-able dict
except ToolError as e:
    return mcp_error(e.to_dict())                     # {"code", "message", "hint"}
```

`specs(prefix="")` gives unprefixed names if ever needed. `call()` accepts both forms.

| MCP tool | Scope | Calls |
|---|---|---|
| `data_list_cubes` | `data:read` | `DataLayer.list_cubes` |
| `data_describe_cube` | `data:read` | `DataLayer.describe_cube` |
| `data_query` | `data:read` | `DataLayer.query` / `DataLayer.compile` |
| `data_search_values` | `data:read` | `DataLayer.search_values` |
| `data_propose_migration` | `data:migrate` (`data:write` accepted as an alias) | Drafter (optional), then `Runner.propose` |

These console and API actions need a person's console session (`Caller.via == "console"`, no token):

| Function | Who | Does |
|---|---|---|
| `DataTools.approve(id, caller)` | admin; a developer only for `additive` migrations that don't widen exposure (in practice metadata and segments). Never the person who proposed it. | Marks a proposal approved |
| `DataTools.apply(id, caller, allow_breaking=False)` | developer; `allow_breaking=True` needs admin | Re-plans (conflicts, impact), then applies an approved migration |
| `DataTools.register_datasource(caller, name, kind, secret_ref, options, verified_projects)` | admin | Connects a warehouse from a stored secret reference (see the guards below) |
| `Runner.rollback(...)` | admin (console or CLI) | Pops the head model version |

There is also an optional `raw_sql` (L3). It needs the `data:raw` scope and a workspace setting, is off by default, and is not in `specs()` yet.

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
| The token has the tool's scope (`data:read` or `data:migrate`). Console and app requests are bounded by role and IAP instead. | `forbidden` |
| The caller is a workspace member, or is reading through an app. | `forbidden` |
| The catalog belongs to the caller's workspace. | `forbidden` |
| `data_propose_migration` needs `developer` or higher. | `forbidden` |
| Approving needs a human console session, not a token. The approver must be a different **person** from the proposer: an agent's token and its owner's console session are the same person. Developers may approve only `additive` migrations that don't widen exposure; anything that can make more data visible (new cubes, members or joins, `change_sql`, re-binding schemas, revoking grants, dropping row policies) needs `admin`. | `forbidden` |

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

## `data_list_cubes`

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

## `data_describe_cube`

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

## `data_query`

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
| `cannot_draft` | `could not draft a migration from that request` | A sentence the rules understand |
| `unknown_tool` | `no tool named data_apply_migration` (agents cannot apply) | |
| `no_datasource` | `no datasource 'primary'; connect a warehouse in the console` | |
| `tenant_not_provisioned` | `no data is provisioned for tenant 'ws_…' in this workspace (…)` | An admin maps your domain or email to a provisioned tenant, or loads data for it |
| `catalog_not_migrated` | `no catalog for this workspace` | The workspace has no data layer yet |
| `internal_error` | `the data layer hit an unexpected RuntimeError` | `logged as err_…`; the details are only in the logs |
| `forbidden` | Missing scope, not a member, wrong workspace, missing `$ctx` value, no schema binding | |

## `data_search_values`

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

## `data_propose_migration`

Validates, dry-runs and stores a migration for a human to approve. **It never changes the model.** The agent passes one of:

- **explicit `ops`** (with a `description`), or
- **a plain-English `request` and/or app `code`.** The layer drafts the ops. Muse Spark goes first, and deterministic rules are the fallback.

**Input**

```json
{
  "type": "object",
  "properties": {
    "description": {"type": "string", "description": "Why. Required with explicit ops; otherwise taken from the draft."},
    "ops": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/Op"}},
    "request": {"type": "string", "description": "e.g. 'add measure unique_customers as count distinct of customer_id on orders'"},
    "code": {"type": "string", "description": "App code whose SQL should be lifted into governed members"}
  },
  "additionalProperties": false
}
```

At least one of `ops`, `request` or `code` is required. `$defs/Op` is the JSON Schema of the discriminated union in `ops.py`. [MIGRATIONS.md](MIGRATIONS.md) documents the vocabulary.

**Output:** the dry-run report (see [MIGRATIONS.md](MIGRATIONS.md), "Dry-run and impact analysis"), which is everything the console's review screen needs:

```json
{
  "id": "0006_count_distinct_ordering_customers", "status": "proposed", "kind": "additive",
  "summary": ["[additive] Add measure orders.unique_customers = count_distinct(customer_id)"],
  "diff": {"added": ["members:orders.unique_customers"], "changed": [], "removed": []},
  "impact": {"breaks": [], "changes": [], "warnings": [], "unaffected": 3, "apps_broken": []},
  "migration": {"id": "…", "description": "…", "base_version": 5, "ops": […]},
  "approval_url": "https://console.shipcube.dev/w/<workspace>/data/migrations/0006_count_distinct_ordering_customers",
  "explained_by": "agent | muse-spark | rules",
  "draft_notes": ["Muse Spark draft unusable (…); drafted with rules instead", "n: already modelled as orders.count; query that instead"],
  "next_step": "A person reviews this in the console and approves it; agents cannot apply migrations."
}
```

The `id` is what the console approves and applies. `summary` has one plain-English line per op, showing inferred values such as the aggregation, so a reviewer sees what will actually be stored.

**Errors:**

- `invalid_op` (`op 2 (change_sql): …`)
- `conflict`
- `cannot_draft` (the hint shows a sentence the rules understand)
- `invalid_arguments`
- `forbidden` (needs `developer`)

The proposer is recorded as `token:<id>`, and the audit row carries `actor_token_id`.

### Drafting with Muse Spark

`drafting.py` defines the hook:

```python
MuseClient = Callable[[str, str], str]   # (system prompt, user prompt) -> reply text
tools = DataTools(layer_for, drafter=MuseDrafter(muse_client))
```

ShipCube implements `muse_client` over its AI gateway. Here is how a draft is made:

- **The prompt.** It lists the allowed ops and their required keys, plus a compact summary of the current model (cubes, members, joins). That keeps Muse reusing existing members instead of duplicating them. Muse must reply with one JSON object `{description, ops}`.
- **Muse's reply is never trusted.** Each op is validated against the op schema here, then validated against the model, conflict-checked and dry-run by `Runner.propose`, the same as hand-written ops.
- **The fallback.** If Muse isn't configured, times out, or returns no JSON or invalid ops, `RuleDrafter` drafts instead. `draft_notes` says why, and `explained_by` is `rules`.
- **What `RuleDrafter` understands:**
  - a small English grammar: `add measure <name> as <sum|count|count distinct|average|min|max> of <column> on <cube>`, `add dimension <name> on <cube> from <column>`, `rename <cube.member> to <name>`, `deprecate <cube.member> because …`;
  - **lifting aggregates from SQL strings in app code.** `COUNT(DISTINCT customer_id) AS buyers FROM orders` becomes `add_measure orders.buyers`. Aggregates already in the model are reported as "already modelled as orders.count; query that instead".
- **The real client.** `muse.MetaMuseClient` is used whenever `META_API_KEY` is set (mount it from Secret Manager). It calls the Meta Model API, which is OpenAI-compatible (`META_API_BASE_URL`, default `https://api.meta.ai/v1`), with `META_MUSE_MODEL` (default `muse-spark-1.3`), low reasoning effort and 6000 max tokens. Muse Spark spends reasoning tokens inside `max_tokens`. Requests time out (`META_MUSE_TIMEOUT_S`), and 429, 5xx and network errors are retried with backoff. Every fallback to rules is logged as `draft.muse_fallback`. `tests/integration/test_muse_live.py` makes one real drafting call.
- **Testing.** `shipcube_data.testing.MockMuse(reply)` stands in for Muse in unit tests only. It is not on any production path.

## `raw_sql` (optional, L3)

**Input:** `{"sql": "SELECT …", "limit": 1000}`

**Output:** `{"columns", "rows", "sql", "verified": false, "warnings": ["raw SQL: not checked against the semantic model; …"]}`

**Errors:** `forbidden_sql`, with a reason such as `schema tenant_globex is not readable by tenant acme`, `raw SQL may not call read_csv` or `row policies apply to you; use a typed query`.

The tool description tells agents to use `query` first. `raw_sql` is for one-off exploration, and its results must not be shown as governed metrics.

---

## Connecting a warehouse (console)

Credentials never pass through an agent or this library's storage:

1. The console stores the credential in Secret Manager and creates a `secret_refs` row (security schema): `id`, `resource_name`, and a pinned `version`.
2. The API calls `DataTools.register_datasource(caller, "primary", "postgres", secret_ref={"id": …, "resource_name": "projects/<secrets project>/secrets/ws-<workspace id>-…", "version": "3"}, options={"host": …, "dbname": …, "user": …}, verified_projects=[…])`. It checks, and checks again at every connect:
   - **Secrets belong to the workspace.** The secret must be in `SHIPCUBE_DATA_SECRET_PROJECT`, be named `ws-<workspace id>-…`, and use a pinned version, not `latest`. No option name may look like a credential.
   - **Postgres hosts are public.** Hosts that resolve to private, loopback, link-local or reserved addresses, and sockets such as `/cloudsql/…`, are refused unless they are listed in `SHIPCUBE_DATA_ALLOWED_DB_HOSTS`. TLS is `sslmode=verify-full` unless the host is allow-listed. A full URL stored in the secret gets the same checks.
   - **BigQuery runs as the customer.** `options.project` must be in `verified_projects`, which the API passes from the workspace's linked and verified GCP projects. Credentials are a service-account key in Secret Manager, or `impersonate_service_account`, a service account in that same project that ShipCube's runtime identity can impersonate. ShipCube's own identity is never used on a customer project.
   - **DuckDB is local development only** (`SHIPCUBE_DEV_MODE=1`), and its connections are sealed: no file, network or extension access, and the configuration is locked.

   It also refuses to switch the dialect under an existing model. It stores only the reference, sets the catalog dialect, and writes `data.datasource.register` to the audit log.
3. At query time, `DataLayer.from_catalog(catalog, resolve_secret, connectors)` re-checks the datasource, then calls `resolve_secret(SecretRef)`. `shipcube_data.secrets.gcp_secret_resolver()` reads Secret Manager with the runtime service account. Built-in connectors: Postgres, BigQuery and DuckDB (dev). `shipcube_data.local.tools_from_env()` wires all of this from environment variables; see [LOCAL.md](LOCAL.md).

## Wiring checklist (for the ShipCube MCP server)

- [ ] Add the git dependency. Build the tools with `shipcube_data.local.tools_from_env()` and register `tools.specs()` (prefixed `data_`). The input schemas are already JSON Schema.
- [ ] Pass `MuseDrafter(muse_client)` once the AI gateway exposes Muse Spark. Until then the default `RuleDrafter` is used.
- [ ] Build `Caller` from the verified token, membership and workspace (see the identity section above), never from tool arguments.
- [ ] Return `ToolError.to_dict()` as the MCP error payload.
- [ ] Add a console page at `/w/<ws>/data/migrations/<id>`. It shows `summary`, `diff` and `impact`, and has **Approve** (`DataTools.approve`) and **Apply** (`DataTools.apply`) buttons, both using the console session's `Caller`.
- [ ] Add a console "Connect warehouse" form that writes the secret to Secret Manager, then calls `DataTools.register_datasource`.
- [ ] Add a console "Data access" page for data roles and attributes (`Catalog.assign_role` / `set_attribute`).
- [ ] Register each deployed app's typed queries as `saved_queries` (name, app, tenant, roles), so impact analysis has something to check.
- [x] Scope names: `data:read` and `data:migrate` (security's names); `data:write` is still accepted.
