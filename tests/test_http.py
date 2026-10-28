"""The console's /v1/data routes, mounted the way ShipCube's API mounts them."""

import uuid

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI, Header, HTTPException  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from shipcube_data import local  # noqa: E402
from shipcube_data.http import data_router  # noqa: E402
from shipcube_data.identity import Caller  # noqa: E402

from .conftest import PG_URL  # noqa: E402

WS = uuid.uuid4()
ANA, BOB, CARL = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
TOKEN = uuid.uuid4()
CALLERS = {
    "agent": Caller(workspace_id=WS, via="mcp", email="ana@example.com", user_id=ANA, membership_role="developer",
                    token_id=TOKEN, scopes=["data:read", "data:migrate"]),
    "agent-noscope": Caller(workspace_id=WS, via="mcp", email="ana@example.com", user_id=ANA, membership_role="developer",
                            token_id=TOKEN, scopes=["apps:read"]),
    "ana": Caller(workspace_id=WS, via="console", email="ana@example.com", user_id=ANA, membership_role="admin"),
    "bob-dev": Caller(workspace_id=WS, via="console", email="bob@example.com", user_id=BOB, membership_role="developer"),
    "carl-admin": Caller(workspace_id=WS, via="console", email="carl@example.com", user_id=CARL, membership_role="admin"),
    "outsider": Caller(workspace_id=uuid.uuid4(), via="console", email="eve@example.com", user_id=uuid.uuid4(), membership_role="owner"),
}


@pytest.fixture
def api(tmp_path, monkeypatch):
    for k, v in {"SHIPCUBE_DEV_MODE": "1", "SHIPCUBE_DATA_CATALOG_URL": str(tmp_path / "c.db"), "SHIPCUBE_DATA_WORKSPACE": str(WS),
                 "SHIPCUBE_DATA_DUCKDB_PATH": str(tmp_path / "olist.duckdb")}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("SHIPCUBE_DATA_ALLOWED_DB_HOSTS", raising=False)
    local.seed(local.Settings.from_env())
    tools = local.tools_from_env()

    def caller(x_test_caller: str = Header()):
        return CALLERS[x_test_caller]

    app = FastAPI()

    @app.exception_handler(HTTPException)  # the API's envelope
    async def envelope(_, exc):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    store, recorded = FakeStore(), []

    def record_secret(raw, **ref):
        recorded.append((raw.email, ref))
        return str(uuid.uuid4())

    app.include_router(data_router(caller_dependency=caller, tools=tools, secret_store=store, record_secret=record_secret),
                       prefix="/v1/data")
    client = TestClient(app)

    def call(method: str, path: str, who: str, **kw):
        return client.request(method, f"/v1/data{path}", headers={"X-Test-Caller": who}, **kw)

    call.tools, call.store, call.recorded = tools, store, recorded
    return call


class FakeStore:
    """The API's secret store interface (shipcube_api.secret_store)."""

    def __init__(self):
        self.live: dict[str, str] = {}

    def put(self, secret_id, value, labels):
        self.live[f"projects/local/secrets/{secret_id}"] = value
        return f"projects/local/secrets/{secret_id}", "1"

    def destroy(self, resource_name, version):
        self.live.pop(resource_name, None)


def test_warehouse_status(api):
    w = api("GET", "/warehouse", "agent").json()
    assert w["connected"] and w["kind"] == "duckdb" and w["model_version"] == 5
    assert {"label": "Status", "value": "reachable"} in w["details"] and "olist.duckdb" in str(w["details"])


def test_cubes_match_the_console_shape_and_respect_grants(api):
    cubes = {c["name"]: c for c in api("GET", "/cubes", "bob-dev").json()}
    assert set(cubes) == {"customers", "states", "orders", "products", "order_items"}
    orders = cubes["orders"]
    assert set(orders) >= {"name", "title", "description", "table", "schema", "dimensions", "measures", "joins", "segments", "policies", "used_by"}
    assert orders["schema"] == "tenant_acme" and orders["used_by"] == ["olist-demo"]
    assert orders["policies"] == ["own_region (roles: regional_manager)"]
    assert "one table per partition" in orders["partition"]
    assert {"to": "customers", "relationship": "many_to_one", "on": "orders.customer_id = customers.customer_id"} in orders["joins"]
    # bob (developer, no finance role) doesn't see finance-only members; admins see them, marked.
    assert "freight" not in {m["name"] for m in cubes["order_items"]["measures"]}
    admin = api("GET", "/cubes/order_items", "carl-admin").json()
    freight = next(m for m in admin["measures"] if m["name"] == "freight")
    assert freight["restricted_to"] == ["finance"] and freight["type"] == "sum"
    assert api("GET", "/cubes/nope", "bob-dev").status_code == 404


def test_scopes_and_workspace_isolation(api):
    r = api("GET", "/cubes", "agent-noscope")
    assert r.status_code == 403 and r.json()["error"]["code"] == "permission_denied"
    # Another workspace never reads this one's catalog: on Postgres its own schema doesn't exist (404);
    # a single-file SQLite catalog refuses the foreign workspace (403).
    r = api("GET", "/migrations", "outsider")
    assert r.status_code in (403, 404) and r.json()["error"]["code"] in {"catalog_not_migrated", "permission_denied"}
    assert "migrations" not in r.text or "error" in r.json()


def propose(api, ops, description="Proposal"):
    return api.tools.call("data_propose_migration", {"description": description, "ops": ops}, CALLERS["agent"])["id"]


def test_migration_review_and_person_only_apply(api):
    listed = api("GET", "/migrations", "bob-dev").json()
    assert {m["status"] for m in listed} == {"applied"} and len(listed) == 5
    assert listed[-1]["applied"]["version"] in range(1, 6)

    mid = propose(api, [{"op": "add_measure", "name": "orders.buyers", "sql": "customer_id", "agg": "count_distinct"}], "Buyers")
    m = api("GET", f"/migrations/{mid}", "bob-dev").json()
    assert m["status"] == "proposed" and m["kind"] == "additive" and m["drafted_by"] == "agent"
    assert m["diff"]["added"] == ["members:orders.buyers"] and m["impact"]["apps_broken"] == []
    assert m["proposed_by"]["agent"] == "MCP agent" and m["proposed_by"]["user"] == str(ANA)

    assert api("POST", f"/migrations/{mid}:apply", "agent").json()["error"]["code"] == "permission_denied"  # agents never apply
    r = api("POST", f"/migrations/{mid}:apply", "ana")  # the agent's own person
    assert r.status_code == 403 and "person who proposed" in r.json()["error"]["message"]
    r = api("POST", f"/migrations/{mid}:apply", "bob-dev")  # a new measure can expose data: admin only
    assert r.status_code == 403 and "admin" in r.json()["error"]["message"]
    done = api("POST", f"/migrations/{mid}:apply", "carl-admin").json()
    assert done["status"] == "applied" and done["applied"]["version"] == 6
    assert api("GET", "/warehouse", "bob-dev").json()["model_version"] == 6
    assert api("POST", f"/migrations/{mid}:apply", "carl-admin").status_code == 409
    assert api("POST", f"/migrations/{mid}:launch", "carl-admin").status_code == 404


def test_reject_with_note(api):
    mid = propose(api, [{"op": "set_meta", "target": "orders", "description": "x"}], "Describe")
    out = api("POST", f"/migrations/{mid}:reject", "bob-dev", json={"note": "not now"}).json()
    assert out["status"] == "rejected" and out["rejected"]["note"] == "not now" and out["rejected"]["by"] == str(BOB)


def test_breaking_apply_needs_explicit_confirmation(api):
    mid = propose(api, [{"op": "drop_member", "member": "orders.late_rate"}], "Drop late rate")
    m = api("GET", f"/migrations/{mid}", "carl-admin").json()
    assert m["kind"] == "breaking" and m["impact"]["breaks"][0]["query"] == "late_rate_by_region"
    r = api("POST", f"/migrations/{mid}:apply", "carl-admin")
    assert r.status_code == 409 and r.json()["error"]["code"] == "would_break"
    assert api("POST", f"/migrations/{mid}:apply", "carl-admin", json={"allow_breaking": True}).json()["status"] == "applied"


def test_warehouse_changes_are_console_admin_only_and_guarded(api):
    body = {"kind": "postgres", "host": "10.0.0.7", "port": 5432, "database": "w", "user": "r", "password": "pw", "ssl": "verify-full"}
    assert api("POST", "/warehouse:test", "agent", json=body).status_code == 403
    assert api("POST", "/warehouse:test", "bob-dev", json=body).status_code == 403
    r = api("POST", "/warehouse:test", "carl-admin", json=body)
    assert r.status_code == 403 and "non-public" in r.json()["error"]["message"]
    bq = {"kind": "bigquery", "project": "someone-else", "location": "US", "service_account_key": "{}"}
    assert "not a verified GCP project" in api("PUT", "/warehouse", "carl-admin", json=bq).json()["error"]["message"]
    assert "pw" not in r.text


@pytest.mark.skipif(not PG_URL, reason="SHIPCUBE_TEST_PG_URL not set")
def test_test_and_put_a_postgres_warehouse(api, monkeypatch):
    from psycopg.conninfo import conninfo_to_dict

    p = conninfo_to_dict(PG_URL)
    monkeypatch.setenv("SHIPCUBE_DATA_ALLOWED_DB_HOSTS", p.get("host", "localhost"))
    body = {"kind": "postgres", "host": p.get("host", "localhost"), "port": int(p.get("port", 5432)), "database": p.get("dbname", "postgres"),
            "user": p.get("user", "postgres"), "password": p.get("password", ""), "ssl": "disable"}
    tested = api("POST", "/warehouse:test", "carl-admin", json=body)
    assert tested.status_code == 200 and tested.json()["tables"] >= 0
    # The demo model is DuckDB SQL; switching this workspace to Postgres is refused rather than half-done,
    # and the credential that was already written to the secret store is destroyed again.
    r = api("PUT", "/warehouse", "carl-admin", json=body)
    assert r.status_code == 400 and "model is written in duckdb" in r.json()["error"]["message"]
    assert api.store.live == {}
    email, ref = api.recorded[0]
    assert email == "carl@example.com" and ref["kind"] == "warehouse_credential" and ref["version"] == "1"
    assert ref["last4"] == body["password"][-4:] and "value" not in ref


def test_put_warehouse_without_the_api_secret_store_is_501(tmp_path, monkeypatch):
    for k, v in {"SHIPCUBE_DEV_MODE": "1", "SHIPCUBE_DATA_CATALOG_URL": str(tmp_path / "c.db"), "SHIPCUBE_DATA_WORKSPACE": str(WS),
                 "SHIPCUBE_DATA_DUCKDB_PATH": str(tmp_path / "olist.duckdb"), "SHIPCUBE_DATA_ALLOWED_DB_HOSTS": "127.0.0.1"}.items():
        monkeypatch.setenv(k, v)
    local.seed(local.Settings.from_env())
    app = FastAPI()
    app.include_router(data_router(caller_dependency=lambda: CALLERS["carl-admin"], tools=local.tools_from_env()), prefix="/v1/data")
    import shipcube_data.http as h

    from shipcube_data import datasources
    from shipcube_data.warehouses import ExecResult

    monkeypatch.setattr(h, "check_db_host", lambda *a: None)
    monkeypatch.setitem(datasources.DEFAULT_CONNECTORS, "postgres",
                        lambda ds, secret: (lambda sql, limits, qid: ExecResult(["t", "s"], [(3, 1)])))
    with TestClient(app) as client:
        body = {"kind": "postgres", "host": "127.0.0.1", "port": 5432, "database": "x", "user": "u", "password": "p", "ssl": "disable"}
        assert client.post("/v1/data/warehouse:test", json=body).json() == {"tables": 3, "schemas": 1}
        r = client.put("/v1/data/warehouse", json=body)
    assert r.status_code == 501 and r.json()["detail"]["code"] == "data_unavailable"
