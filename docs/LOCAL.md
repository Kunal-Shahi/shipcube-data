# Running shipcube-data on the local stack

Everything in ShipCube runs and is tested locally before it is pushed. On ShipCube's local stack (`deploy/local/compose.yaml` in Kunal-Shahi/shipcube, owned by platform), the data layer is:

| Piece | Local | Production |
|---|---|---|
| Catalog | the stack's Postgres: ShipCube's database, schema `catalog`, FORCE RLS (`SHIPCUBE_DATA_CATALOG_LAYOUT=shared`) | Cloud SQL Postgres, same layout |
| Warehouse | a DuckDB file (`/data/olist.duckdb`), or the stack's Postgres, database `warehouse` | the customer's Cloud SQL Postgres or BigQuery |
| Model | the Olist demo migrations, applied with the migration runner | migrations proposed by agents and approved in the console |
| Tools | `data_*` mounted in the MCP server by `shipcube_data.local.tools_from_env()` | the same |

The demo data is synthetic and generated deterministically. It uses the public Olist schema's table and column names (the real dataset's CC BY-NC-SA license doesn't fit this Apache-2.0 repo):

- two tenants (`tenant_acme`, `tenant_globex`) and a `shared` schema;
- `orders` split into 20 monthly tables, so partition pruning is visible.

## 1. Compose

ShipCube's `deploy/local/compose.yaml` runs the data layer as described here. [`deploy/local/compose.data.yaml`](../deploy/local/compose.data.yaml) is the older standalone example, with a separate catalog database, kept for running this package next to any Postgres.

- **Catalog schema:** the stack's `migrate` step creates it, as on Cloud SQL. It runs `python -m shipcube_api.admin migrate --extra data` as the migration role, which applies the API's schema and then shipcube-data's shared catalog (schema `catalog` in the `shipcube` database, FORCE RLS).
- **`data-seed`:** a one-shot job, run with the API image by `make up`, that executes `shipcube-data seed`. It's idempotent. It:
  - loads the demo warehouse;
  - registers it as the seeded workspace's datasource;
  - applies the model migrations in `examples/olist/migrations` (0001–0006);
  - maps demo Google domains to tenants;
  - registers two saved queries for impact analysis.
- **`api`:** serves both the `/v1/data` routes and the MCP `data_*` tools (the MCP server runs in the same process). It reads the DuckDB volume read-only.

The seed is safe to re-run while the stack is up: an existing DuckDB file is kept (use `--force` to rebuild it), and applied migrations are skipped.

## 2. Environment

| Variable | Local value | Meaning |
|---|---|---|
| `SHIPCUBE_DEV_MODE` | `1` | Allows the DuckDB warehouse and `projects/local` secrets. Never set in production. |
| `SHIPCUBE_DATA_CATALOG_LAYOUT` | `shared` | The catalog is ShipCube's own database, schema `catalog`, with row-level security per workspace |
| `SHIPCUBE_DATA_CATALOG_URL` | `postgresql://postgres:…@db:5432/shipcube` (`data-seed` only) | Where `seed` writes the seeded workspace's catalog rows. The API needs no URL: each request runs in its own `db.tx(caller)` |
| `SHIPCUBE_DATA_WORKSPACE` | the workspace id the local bootstrap creates | The workspace whose catalog is seeded. Callers in other workspaces get `catalog_not_migrated`. |
| `SHIPCUBE_DATA_WAREHOUSE` | `duckdb` (default) or `postgres` | Which demo warehouse to load and register |
| `SHIPCUBE_DATA_DUCKDB_PATH` | `/data/olist.duckdb` | DuckDB file, on a volume shared read-only with the MCP server |
| `SHIPCUBE_DATA_WAREHOUSE_URL` | `postgresql://postgres:…@db:5432/warehouse?sslmode=disable` | Postgres warehouse. It is served to the data layer as the local secret `projects/local/secrets/ws-<workspace>-warehouse`. |
| `SHIPCUBE_DATA_ALLOWED_DB_HOSTS` | `db` | The stack's Postgres is a private host. Database hosts that resolve to private addresses are refused unless they are listed here. |
| `SHIPCUBE_DATA_DEMO_TENANTS` | `shipcube.local=acme,example.com=acme,globex.test=globex` | Google domain → demo tenant. The stack's seeded user is `@shipcube.local`. A caller whose email domain isn't listed gets the workspace tenant, which has no demo data. |
| `SHIPCUBE_DATA_ROLLUP_SCHEMA` | `shipcube_rollups` (default) | Where `rollup-build` writes pre-aggregations, if you define any |
| `META_API_KEY` | optional | `data_propose_migration` drafts with Muse Spark when it is set; with rules otherwise. |

Guardrails take the same variables as in production (`SHIPCUBE_DATA_QUERY_TIMEOUT_S`, `SHIPCUBE_DATA_MAX_ROWS`, …); see [RUNBOOK.md](RUNBOOK.md).

## 3. Wiring

ShipCube pins the package by commit in `services/api/pyproject.toml` and `services/mcp/pyproject.toml` (the `data` extra), and `uv sync --locked --extra data` installs it from this public repo. The API builds the tools once, so every call runs in the request's own transaction, under the caller's row-level security (`services/api/shipcube_api/data_layer.py`):

```python
from shipcube_data.local import tools_from_env

tools = tools_from_env(catalog_session=lambda caller: db.tx(caller))   # the API's RLS-scoped transaction
tools.specs()                                # the five data_ tools; apply is never among them
tools.call("data_query", args, caller)       # caller: shipcube_data.Caller from the verified token
```

`data_query` takes a typed query, a staged (L2) query, or governed `sql`; see [QUERY.md](QUERY.md). Without `catalog_session`, `tools_from_env()` opens the catalog from `SHIPCUBE_DATA_CATALOG_URL` instead, as in section 5.

## 4. Smoke

```bash
docker compose run --rm data-seed shipcube-data smoke
```

The smoke runs the tools exactly as the MCP server calls them. It acts as an agent (PAT, `developer`, scopes `data:read` and `data:migrate`) whose email domain is the first demo tenant's. The steps:

1. **tools:** exactly the five `data_` tools; no apply.
2. **`data_list_cubes`:** the five demo cubes.
3. **`data_describe_cube`:** `orders`, including its partitioning.
4. **`data_query` (L0):** orders and revenue by region for Q1 2018. It must read exactly 3 of the 20 monthly partitions.
5. **`data_search_values`:** `customers.city` containing "sao".
6. **`data_propose_migration`:** a new measure. It must come back `proposed`, with an approval URL and an impact report.
7. **The agent cannot apply:**
   - `apply_migration` and `data_apply_migration` are unknown tools;
   - `DataTools.apply` and `DataTools.approve` refuse an agent token;
   - the model version is unchanged and the proposal is still `proposed`.

   The smoke then rejects its proposal, so runs don't pile up.

It exits non-zero if any step fails. The same sequence runs in CI and in `tests/test_local.py` (SQLite + DuckDB always; Postgres + Postgres when `SHIPCUBE_TEST_PG_URL` is set).

### Through the MCP server

`shipcube-data smoke --mcp` runs the same steps end to end through a running MCP server, with a PAT. That covers auth, mounting, caller resolution and error mapping. It needs only the standard library: a minimal Streamable HTTP client that handles JSON and SSE responses. For the agent-cannot-apply step, it checks that no apply tool is listed or callable, and that the proposed measure is not in `data_describe_cube`.

From the host (for `deploy/local/smoke/data_smoke.sh`, which receives `SHIPCUBE_MCP_URL` and `SHIPCUBE_PAT`):

```bash
uvx --from "shipcube-data @ git+https://github.com/Kunal-Shahi/shipcube-data@<commit>" shipcube-data smoke --mcp
```

Or inside the compose network:

```bash
docker compose run --rm -e SHIPCUBE_MCP_URL=http://mcp:8080/mcp -e SHIPCUBE_PAT data-seed shipcube-data smoke --mcp
```

`tests/test_mcp_smoke.py` runs it against a real MCP SDK 2.x server (`MCPServer`, the same class ShipCube's server uses) serving these tools.

### Unprovisioned tenants

A caller whose Google domain or email isn't mapped to a demo tenant falls back to the workspace's own tenant, which has no demo data. `data_query` and `data_search_values` then return `tenant_not_provisioned`, with a hint. Map the domain in `SHIPCUBE_DATA_DEMO_TENANTS`, for example `shipcube.local=acme` for the stack's seeded user. Any unexpected exception becomes `internal_error`, with an incident id in the hint and the details in the logs; MCP clients never see raw driver errors.

## 5. Without Docker (no cloud account needed)

This is the quickest way to see the agent tools work on the demo data. You need Python 3.12 and [uv](https://docs.astral.sh/uv/); nothing private, no Docker, no GCP:

```bash
uv venv -p 3.12 .venv && uv pip install -p .venv "shipcube-data[duckdb] @ git+https://github.com/Kunal-Shahi/shipcube-data"
```

```bash
export SHIPCUBE_DEV_MODE=1 SHIPCUBE_DATA_CATALOG_URL=catalog.db \
       SHIPCUBE_DATA_WORKSPACE=00000000-0000-0000-0000-000000000001 SHIPCUBE_DATA_DUCKDB_PATH=olist.duckdb
```

```bash
.venv/bin/shipcube-data seed && .venv/bin/shipcube-data smoke      # smoke: 7/7 passed
```

The catalog is a SQLite file and the warehouse a DuckDB file. For a Postgres catalog, set `SHIPCUBE_DATA_CATALOG_URL=postgresql://…`. For a Postgres warehouse, also set:
- `SHIPCUBE_DATA_WAREHOUSE=postgres`;
- `SHIPCUBE_DATA_WAREHOUSE_URL=postgresql://postgres@localhost:5432/warehouse?sslmode=disable`;
- `SHIPCUBE_DATA_ALLOWED_DB_HOSTS=localhost`.
