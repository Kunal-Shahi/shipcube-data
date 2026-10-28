# Console HTTP routes (`/v1/data/…`)

The console's Data page reads the warehouse status, browses cubes, and lets a person apply or reject the migrations that agents proposed. shipcube-data serves these routes as a FastAPI router. ShipCube's control-plane API mounts it:

```python
from shipcube_data.http import data_router

app.include_router(
    data_router(caller_dependency=caller, secret_store=secret_store(), record_secret=record_secret,
                verified_projects=verified_gcp_projects),
    prefix="/v1/data",
)
```

Arguments:

- **`caller_dependency`** is the API's own dependency for the verified caller. The router reads its attributes: `user_id`, `workspace_id`, `email`, `role`, `token_id`, `scopes` and `via`.
- **`secret_store`** is the API's secret store: `put(secret_id, value, labels) -> (resource_name, version)` and `destroy(resource_name, version)`. That's Secret Manager in production, or the DEV_MODE JSON file security's emulator serves.
- **`record_secret(raw_caller, kind, label, resource_name, version, last4, fingerprint) -> secret_refs.id`** writes the reference into the API's `secret_refs` table, inside the caller's RLS-scoped transaction. This package never opens the API's database or a Secret Manager client of its own. Without these two arguments, `PUT /warehouse` answers 501.

Configuration comes from the same environment as the MCP tools (`SHIPCUBE_DATA_CATALOG_URL`, …; see [LOCAL.md](LOCAL.md)). Install with the `http` extra. When the package isn't installed, the API answers these routes with 501 `data_unavailable`.

## Routes

Bodies are snake_case, like every other `/v1` route. They map one-to-one onto the console's `Warehouse`, `Cube` and `Migration` types in `apps/web/src/lib/types.ts` (camelCase in the console).

| Route | Who | Returns |
|---|---|---|
| `GET /warehouse` | `data:read` (any console session) | `Warehouse`. `connected`, kind, details (never credentials), reachability ("Status"), `modelVersion`. |
| `POST /warehouse:test` | Console session, admin | `{tables, schemas}`. Tests the credentials and stores nothing. |
| `PUT /warehouse` | Console session, admin | `{tables, schemas, warehouse}`. It tests the credential first, then:<br>• writes it through the API's `secret_store` (`ws-<workspace>-warehouse_credential-<uuid>`);<br>• records the reference in `secret_refs` (`kind = 'warehouse_credential'`, last 4 characters and fingerprint, never the value);<br>• registers the datasource;<br>• destroys the previous credential.<br>If registering fails, the new credential is destroyed again. |
| `GET /cubes`, `GET /cubes/{name}` | `data:read` | `Cube[]` / `Cube`. Includes the routed schema for the caller's tenant, joins, segments, partitioning, row policies, and the apps whose saved queries use it. Members the caller isn't granted are omitted; workspace admins see them, marked with `restricted_to`. |
| `GET /migrations`, `GET /migrations/{id}` | `data:read` | `Migration[]` (newest first) / `Migration`, with ops, the dry-run diff and impact, who proposed and drafted it, and the decision. `raw_status` keeps the catalog's own status (`approved`, `rolled_back`). |
| `POST /migrations/{id}:apply` `{allow_breaking}` | Console session | Approves (if still `proposed`) and applies in one step. Approval rules: never the person who proposed it; admin for anything that widens exposure or isn't `additive`. `allow_breaking` (admin) is needed when saved queries would break. |
| `POST /migrations/{id}:reject` `{note}` | Console session, developer or above | The rejected `Migration`. |

## Errors

Errors are `HTTPException(status, {code, message, hint})`, which the API wraps as `{"error": {...}}`.

| Status | Codes |
|---|---|
| 403 | `permission_denied` (missing scope, not a person, wrong role, same person, non-public warehouse host) |
| 404 | `not_found`, `catalog_not_migrated` (no data layer for this workspace) |
| 409 | `conflict` (already decided, or a conflicting migration landed), `would_break` |
| 400 | `invalid_argument`, `invalid_op` |
| 501 | `data_unavailable` (shipcube-data isn't installed or configured, or there's no secret store for `PUT /warehouse`) |
| 503 | `warehouse_unavailable` |

## Scoping

**Who may do what.** A person means a console session (`via == "console"`, no token), mirroring the API's `console_session`. A PAT gets 403 on `:test`, `PUT`, `:apply` and `:reject`.

**Databases.** The catalog is shipcube-data's own database (`SHIPCUBE_DATA_CATALOG_URL`), with one schema per workspace. At request time the router never creates schemas: a workspace without a catalog gets `catalog_not_migrated`. Provisioning (`shipcube-data catalog-migrate` / `seed`) runs as the migration role (sc-migrator), never as the API's runtime role. The API's own database is touched only through `record_secret`, in the API's transaction.

- **Workspace.** Every request opens the caller's own workspace catalog (on Postgres, schema `catalog_ws_<uuid hex>`), so one workspace never reads another's model, migrations or warehouse.
- **Grants.** Grants and row policies come from that catalog.
- **Who can change things.** PATs can read, but never apply, reject or touch the warehouse. Agents only ever propose, through `data_propose_migration`.
