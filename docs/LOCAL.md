# Running shipcube-data on the local stack

Everything in ShipCube runs and is tested locally before it is pushed. On ShipCube's local stack (`deploy/local/compose.yaml` in Kunal-Shahi/shipcube, owned by platform), the data layer is:

| Piece | Local | Production |
|---|---|---|
| Catalog | the stack's Postgres, database `shipcube_data`, schema `catalog_ws_<workspace hex>` | Cloud SQL Postgres, same layout |
| Warehouse | a DuckDB file (`/data/olist.duckdb`), or the stack's Postgres, database `warehouse` | the customer's Cloud SQL Postgres or BigQuery |
| Model | the Olist demo migrations, applied with the migration runner | migrations proposed by agents and approved in the console |
| Tools | `data_*` mounted in the MCP server by `shipcube_data.local.tools_from_env()` | the same |

The demo data is synthetic and generated deterministically. It uses the public Olist schema's table and column names (the real dataset's CC BY-NC-SA license doesn't fit this Apache-2.0 repo):

- two tenants (`tenant_acme`, `tenant_globex`) and a `shared` schema;
- `orders` split into 20 monthly tables, so partition pruning is visible.

## 1. Compose

[`deploy/local/compose.data.yaml`](../deploy/local/compose.data.yaml) holds the service definitions to merge into the stack:

- **`data-seed`**: a one-shot job, run with the MCP image, that executes `shipcube-data seed`. It is idempotent. It creates `shipcube_data` and `warehouse` if they are missing, migrates the catalog schema, loads the warehouse, registers it as the workspace's datasource, applies the model migrations, maps demo Google domains to tenants, and registers two saved queries for impact analysis.
- **`mcp`**: gets the same environment, plus the DuckDB volume read-only. It starts after `data-seed` completes.

The seed is safe to re-run while the stack is up: an existing DuckDB file is kept (use `--force` to rebuild it), and applied migrations are skipped.

## 2. Environment

| Variable | Local value | Meaning |
|---|---|---|
| `SHIPCUBE_DEV_MODE` | `1` | Allows the DuckDB warehouse and `projects/local` secrets. Never set in production. |
| `SHIPCUBE_DATA_CATALOG_URL` | `postgresql://postgres:postgres@postgres:5432/shipcube_data` | Catalog database |
| `SHIPCUBE_DATA_WORKSPACE` | the workspace id the local bootstrap creates | The workspace whose catalog is seeded. Callers in other workspaces get `catalog_not_migrated`. |
| `SHIPCUBE_DATA_WAREHOUSE` | `duckdb` (default) or `postgres` | Which demo warehouse to load and register |
| `SHIPCUBE_DATA_DUCKDB_PATH` | `/data/olist.duckdb` | DuckDB file, on a volume shared read-only with the MCP server |
| `SHIPCUBE_DATA_WAREHOUSE_URL` | `postgresql://postgres:postgres@postgres:5432/warehouse?sslmode=disable` | Postgres warehouse. It is served to the data layer as the local secret `projects/local/secrets/ws-<workspace>-warehouse`. |
| `SHIPCUBE_DATA_ALLOWED_DB_HOSTS` | `postgres` | The stack's Postgres is a private host. Database hosts that resolve to private addresses are refused unless they are listed here. |
| `SHIPCUBE_DATA_DEMO_TENANTS` | `example.com=acme,globex.test=globex` | Google domain → demo tenant. A caller whose email domain isn't listed gets the workspace tenant, which has no demo data. |
| `META_API_KEY` | optional | `data_propose_migration` drafts with Muse Spark when it is set; with rules otherwise. |

Guardrails take the same variables as in production (`SHIPCUBE_DATA_QUERY_TIMEOUT_S`, `SHIPCUBE_DATA_MAX_ROWS`, …); see [RUNBOOK.md](RUNBOOK.md).

## 3. Wiring the MCP server

Pin the package to a commit, or to a tag once one is cut:

```toml
# services/mcp/pyproject.toml
[project.optional-dependencies]
data = ["shipcube-data[duckdb,postgres]"]

[tool.uv.sources]
shipcube-data = { git = "https://github.com/Kunal-Shahi/shipcube-data", rev = "<commit>" }
```

Build the tools from the environment. The backend no longer needs its own warehouse, migrations or worker thread: `tools_from_env()` opens one catalog and warehouse connection per thread and workspace, lazily.

```python
from shipcube_data.local import tools_from_env

tools = tools_from_env()                     # reads SHIPCUBE_DATA_CATALOG_URL etc.
tools.specs()                                # the five data_ tools; apply is never among them
tools.call("data_query", args, caller)       # caller: shipcube_data.Caller from the verified token
```

The propose scope is now `data:migrate`, security's name. `data:write` is still accepted, so the server's current scope translation keeps working; it can be removed.

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

## 5. Without Docker

```bash
export SHIPCUBE_DEV_MODE=1 SHIPCUBE_DATA_CATALOG_URL=postgresql://postgres@localhost:5432/shipcube_data \
       SHIPCUBE_DATA_WORKSPACE=0e364bd8-4622-42d1-839c-35aae720b52b SHIPCUBE_DATA_DUCKDB_PATH=.local/olist.duckdb
```

```bash
shipcube-data seed && shipcube-data smoke
```

For a Postgres warehouse, also set `SHIPCUBE_DATA_WAREHOUSE=postgres`, `SHIPCUBE_DATA_WAREHOUSE_URL=postgresql://postgres@localhost:5432/warehouse?sslmode=disable` and `SHIPCUBE_DATA_ALLOWED_DB_HOSTS=localhost`.
