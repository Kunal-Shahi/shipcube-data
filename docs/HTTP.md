# Console HTTP routes (`/v1/data/…`)

The console's Data page reads the warehouse status, browses cubes, and lets a person apply or reject the migrations that agents proposed. shipcube-data serves these routes as a FastAPI router. ShipCube's control-plane API mounts it:

```python
from shipcube_data.http import data_router

app.include_router(data_router(caller_dependency=caller, verified_projects=verified_gcp_projects), prefix="/v1/data")
```

`caller_dependency` is the API's own dependency for the verified caller. The router reads its attributes: `user_id`, `workspace_id`, `email`, `role`, `token_id`, `scopes` and `via`. Configuration comes from the same environment as the MCP tools (`SHIPCUBE_DATA_CATALOG_URL`, …; see [LOCAL.md](LOCAL.md)). Install with the `http` extra.

## Routes

Response bodies are exactly the console's types in `apps/web/src/lib/types.ts` (`Warehouse`, `Cube`, `Migration`).

| Route | Who | Returns |
|---|---|---|
| `GET /warehouse` | `data:read` (any console session) | `Warehouse`. `connected`, kind, details (never credentials), reachability ("Status"), `modelVersion`. |
| `POST /warehouse:test` | Console session, admin | `{tables, schemas}`. Tests the credentials and stores nothing. |
| `PUT /warehouse` | Console session, admin | `{tables, schemas, warehouse}`. Tests, then writes the credential to Secret Manager (`ws-<workspace>-warehouse-…`) and registers the datasource. |
| `GET /cubes`, `GET /cubes/{name}` | `data:read` | `Cube[]` / `Cube`. Includes the routed schema for the caller's tenant, joins, segments, partitioning, row policies, and the apps whose saved queries use it. Members the caller isn't granted are omitted; workspace admins see them, marked with `restrictedTo`. |
| `GET /migrations`, `GET /migrations/{id}` | `data:read` | `Migration[]` (newest first) / `Migration`, with ops, the dry-run diff and impact, who proposed and drafted it, and the decision. `rawStatus` keeps the catalog's own status (`approved`, `rolled_back`). |
| `POST /migrations/{id}:apply` `{allowBreaking}` | Console session | Approves (if still `proposed`) and applies in one step. Approval rules: never the person who proposed it; admin for anything that widens exposure or isn't `additive`. `allowBreaking` (admin) is needed when saved queries would break. |
| `POST /migrations/{id}:reject` `{note}` | Console session, developer or above | The rejected `Migration`. |

## Errors

Errors are `HTTPException(status, {code, message, hint})`, which the API wraps as `{"error": {...}}`.

| Status | Codes |
|---|---|
| 403 | `permission_denied` (missing scope, not a person, wrong role, same person, non-public warehouse host) |
| 404 | `not_found`, `catalog_not_migrated` (no data layer for this workspace) |
| 409 | `conflict` (already decided, or a conflicting migration landed), `would_break` |
| 400 | `invalid_argument`, `invalid_op` |
| 503 | `warehouse_unavailable` |

## Scoping

- **Workspace.** Every request opens the caller's own workspace catalog (on Postgres, schema `catalog_ws_<uuid hex>`), so one workspace never reads another's model, migrations or warehouse.
- **Grants.** Grants and row policies come from that catalog.
- **Who can change things.** PATs can read, but never apply, reject or touch the warehouse. Agents only ever propose, through `data_propose_migration`.
