# shipcube-data runbook

How to run, configure, deploy and operate the data layer. The data layer runs in-process inside ShipCube's MCP server and API (Cloud Run service `shipcube-mcp`), so most of this is about the catalog, the warehouses and the knobs that process exposes.

## 1. Components

| Piece | Where it runs | State |
|---|---|---|
| Library `shipcube_data` | inside `shipcube-mcp` (MCP tools, API routes, console actions) | none |
| Catalog | Cloud SQL Postgres, one schema per workspace (`catalog_ws_<uuid hex>`) | model versions, migrations, audit, saved queries, datasources, data roles and attributes |
| Warehouses | the customer's Cloud SQL Postgres or BigQuery, registered per workspace | read-only access |
| Muse Spark | Meta Model API (`https://api.meta.ai/v1`) | none |
| Result cache | in-process LRU per instance | disposable |

## 2. Configuration

Everything environment-specific comes from environment variables. Secrets come from Secret Manager, mounted as env vars by Cloud Run.

| Variable | Default | Meaning |
|---|---|---|
| `SHIPCUBE_DATA_CATALOG_URL` | `catalog.db` (SQLite) | Catalog database: `postgresql://…` in production. Mount it from Secret Manager. |
| `SHIPCUBE_DATA_WORKSPACE_ID` | – | CLI only: which workspace's catalog schema to use |
| `SHIPCUBE_DATA_QUERY_TIMEOUT_S` | `30` | Per-query warehouse timeout |
| `SHIPCUBE_DATA_MAX_ROWS` | `10000` | Row cap per query; larger `limit`s are refused |
| `SHIPCUBE_DATA_MAX_BYTES_BILLED` | `10737418240` (10 GiB) | BigQuery per-query bytes cap (dry run and `maximum_bytes_billed`) |
| `SHIPCUBE_DATA_MAX_PG_COST` | `5000000` | Postgres planner-cost cap (from `EXPLAIN`) |
| `SHIPCUBE_DATA_LOG_LEVEL` | `WARNING` (CLI) | Log level for the CLI. Services call `obs.configure_logging("INFO")`. |
| `META_API_KEY` | – | Enables Muse Spark drafting. Secret `projects/shipcube-control-ks/secrets/meta-api-key`. |
| `META_API_BASE_URL` | `https://api.meta.ai/v1` | Meta Model API base URL |
| `META_MUSE_MODEL` | `muse-spark-1.3` | Model id (`muse-spark` alone returns 404) |
| `META_MUSE_TIMEOUT_S` | `60` | Per-call timeout; calls usually take 5–12 s |

Warehouse credentials are **not** environment variables. A workspace admin connects a warehouse in the console. The console writes the credential to Secret Manager and calls `DataTools.register_datasource` with the `secret_refs` row. The library resolves the secret at connect time.

- **BigQuery:** prefer no secret at all. Grant the runtime service account `roles/bigquery.jobUser` on the billing project and `roles/bigquery.dataViewer` on the datasets.
- **Cloud SQL Postgres:** use a role with `SELECT` only, connected through `/cloudsql/<project>:<region>:<instance>` or the private IP.

## 3. Run locally

```bash
uv venv -p 3.12 .venv && uv pip install -p .venv -e '.[dev]'
```

```bash
.venv/bin/pytest -q
```

Add a local Postgres to also run the catalog tests and the Postgres warehouse integration tests on it:

```bash
SHIPCUBE_TEST_PG_URL=postgresql://postgres@localhost:5432/postgres .venv/bin/pytest -q
```

To also run the BigQuery integration tests (they create and delete `shipcube_it_*` datasets; about 3 minutes):

```bash
SHIPCUBE_TEST_BQ_PROJECT=shipcube-control-ks .venv/bin/pytest -q -m integration tests/integration/test_bigquery_warehouse.py
```

To run one real Muse drafting call:

```bash
META_API_KEY="$(gcloud secrets versions access latest --secret=meta-api-key --project=shipcube-control-ks)" .venv/bin/pytest -q tests/integration/test_muse_live.py
```

The README's CLI section shows a DuckDB demo warehouse end to end.

## 4. Deploy

1. **Dependency.** `shipcube-mcp` depends on `shipcube-data[postgres,bigquery] @ git+https://github.com/Kunal-Shahi/shipcube-data@<tag or commit>`. Pin a tag or commit; never track `main` in production.
2. **Catalog database.** Create it in the Cloud SQL instance, for example `shipcube_data`, owned by a migration role. The service connects as `shipcube_app`, which has DML only. Use IAM database auth, so no password lives in the URL.
3. **Catalog-schema migrations run before the new revision takes traffic** (a Cloud Run job, or a step in the deploy pipeline), once per workspace schema:

   ```bash
   shipcube-data catalog-migrate --catalog "$SHIPCUBE_DATA_CATALOG_URL" --workspace <workspace uuid>
   ```

   This step:
   - is idempotent, and serialised per schema by an advisory lock;
   - refuses to run if an applied file was edited (`changed after it was applied`).

   Never edit an applied `catalog_migrations/*.sql`; add the next number instead. A service that opens a catalog with `migrate=False` refuses to start on a stale schema (`catalog_not_migrated`).
4. **Secrets.** Mount `META_API_KEY` and `SHIPCUBE_DATA_CATALOG_URL` from Secret Manager.
5. **CI must be green.** The `test` job runs every test on SQLite and Postgres. The `bigquery` job runs when `GCP_WIF_PROVIDER`, `GCP_PROJECT` and `GCP_CI_SERVICE_ACCOUNT` are set as repository variables.

Backups: Cloud SQL automated daily backups cover the catalog. The model can also be rebuilt from migration history, because `migrations.ops` holds every applied op list.

## 5. Operate

### Logs

Every line is JSON. These Cloud Logging queries cover the common investigations:

```
jsonPayload.logger="shipcube_data" AND jsonPayload.event="query.error"
jsonPayload.query_id="q_…"                                  -- one query, end to end
jsonPayload.event="query.ok" AND jsonPayload.elapsed_ms>5000  -- slow queries
jsonPayload.event="draft.muse_fallback"                      -- Muse problems
jsonPayload.event=~"^migration\."                            -- model changes
```

The `query_id` also appears:

- in Postgres `pg_stat_activity.query`, as `/* shipcube query_id=… */`;
- in BigQuery as the job label `shipcube_query_id`.

That lets you go from a log line to the warehouse job.

### Metrics

Wire `DataLayer(metrics=…)` to Cloud Monitoring. The metrics and suggested alerts:

| Metric | Labels | Alert when |
|---|---|---|
| `data_queries_total` | `cache=hit\|miss` | – |
| `data_query_duration_ms` | `cache` | p95 > 10 s for 10 min |
| `data_query_errors_total` | `code` | `warehouse_unavailable` > 5/min; `query_timeout` or `cost_limit` spikes |

### Common situations

| Symptom | Likely cause | What to do |
|---|---|---|
| `query_timeout` | Unbounded time range, or a slow warehouse | Check `partitions_scanned` in the log. Ask the app to add `time.range`, or set `require_filter` on the partition (a migration). Raise `SHIPCUBE_DATA_QUERY_TIMEOUT_S` only for a reason. |
| `cost_limit` | The query would scan too much (BigQuery bytes) or plan too expensively (Postgres) | Same as above. To raise the cap for one workspace, give its `DataLayer` its own `QueryLimits`. |
| `warehouse_unavailable` | The warehouse is down, network, or credentials | The adapter has already retried with backoff. Check the customer's Cloud SQL / BigQuery status and the datasource's secret version. |
| `conflict` on apply | Another migration touched the same member after the proposal's base version | Expected. Re-propose against the new head; the console shows both migrations. |
| `would_break` on apply | The impact now includes broken saved queries | Fix the app first, or have an admin apply with "allow breaking". |
| `catalog_not_migrated` at startup | The deploy step was skipped | Run `catalog-migrate` (§4.3) for the workspace. |
| A bad migration was applied | – | Run `shipcube-data rollback --dry-run` (shows impact), then `rollback`. Only the head version can be rolled back. For older migrations, write a forward migration that reverses them. |
| Muse drafts fail (`draft.muse_fallback`) | API key, quota or outage | Drafting continues with rules, and the proposal shows `explained_by: rules`. Check the key's Secret Manager version and the Meta API status. |
| Lock waits on apply | A long `apply`: impact analysis compiles every saved query inside the lock | Expected to be seconds. Find blockers with `SELECT * FROM pg_locks WHERE locktype = 'advisory'`. |

### Inspecting a workspace's catalog

```sql
SET search_path TO catalog_ws_<uuid hex>;
SELECT version, migration_id, fingerprint, applied_at, applied_by, rolled_back_at FROM model_versions ORDER BY version;
SELECT id, status, kind, proposed_by, approved_by, version FROM migrations ORDER BY created_at DESC LIMIT 20;
SELECT at, actor_type, action, target_id, detail FROM audit_log ORDER BY id DESC LIMIT 50;
SELECT sql, since_version, until_version FROM members WHERE cube = 'orders' AND name = 'revenue' ORDER BY since_version;
```

Never edit these tables by hand. Every model change goes through a migration, and every access change through the console (so it is audited).

## 6. Known limits

See [DESIGN.md §8](DESIGN.md#8-status-and-known-limits).
