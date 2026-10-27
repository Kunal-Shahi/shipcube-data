"""The in-process MCP library, driven by callers shaped like the security workstream's identity model."""

import json
from uuid import UUID, uuid4

import pytest

from shipcube_data.engine import DataLayer, duckdb_executor
from shipcube_data.identity import Caller, security_context, workspace_tenant
from shipcube_data.tools import DataTools, ToolError

WS = UUID("0192f3a0-0000-7000-8000-000000000001")
ANA, BOB = uuid4(), uuid4()
TOKEN = uuid4()


@pytest.fixture
def tools(runner, warehouse):
    cat = runner.catalog
    cat.db.execute("INSERT OR REPLACE INTO settings VALUES ('workspace_id', ?)", (str(WS),))
    # Viewers from each customer's Google domain read that customer's schema.
    cat.set_attribute("domain", "acme.test", "tenant", "acme", by="user:admin")
    cat.set_attribute("domain", "globex.test", "tenant", "globex", by="user:admin")
    layer = DataLayer(cat, duckdb_executor(warehouse))
    return DataTools(lambda ws: layer)


def agent(role="developer", scopes=("data:read", "data:write"), email="ana@acme.test"):
    return Caller(workspace_id=WS, via="mcp", email=email, user_id=ANA, membership_role=role,
                  token_id=TOKEN, scopes=list(scopes))


def console(role="admin", user=BOB, email="bob@acme.test"):
    return Caller(workspace_id=WS, via="console", email=email, user_id=user, membership_role=role)


def test_specs_are_json_schemas(tools):
    specs = {s["name"]: s for s in tools.specs()}
    assert set(specs) == {"list_cubes", "describe_cube", "query", "search_values", "propose_migration", "apply_migration"}
    json.dumps(specs)  # serialisable as-is
    assert specs["query"]["scope"] == "data:read" and specs["apply_migration"]["scope"] == "data:write"
    assert "add_measure" in specs["propose_migration"]["inputSchema"]["properties"]["ops"]["items"]["discriminator"]["mapping"]
    assert "measures" in json.dumps(specs["query"]["inputSchema"])


def test_query_through_the_tool(tools):
    out = tools.call("query", {"query": {"measures": ["orders.count"], "dimensions": ["states.region"], "limit": 3}}, agent())
    assert out["verified"] and len(out["rows"]) == 3 and out["truncated"] is False
    json.dumps(out)  # decimals and timestamps are converted
    sql = tools.call("query", {"query": {"measures": ["orders.count"]}, "mode": "sql", "dialect": "bigquery"}, agent())
    assert sql["dialect"] == "bigquery" and "tenant_acme" in sql["sql"]


def test_tenant_comes_from_identity(tools):
    acme = tools.call("query", {"query": {"measures": ["orders.count"]}}, agent())["rows"][0]["orders__count"]
    globex = tools.call("query", {"query": {"measures": ["orders.count"]}}, agent(email="gil@globex.test"))["rows"][0]["orders__count"]
    assert (acme, globex) == (1500, 500)
    # No sub-tenant attribute: the workspace itself is the tenant.
    other = tools.call("query", {"query": {"measures": ["orders.count"]}, "mode": "sql"}, agent(email="x@elsewhere.test"))
    assert f"tenant_{workspace_tenant(WS)}" in other["sql"]


def test_scopes_membership_and_workspace_are_enforced(tools):
    with pytest.raises(ToolError) as e:
        tools.call("query", {"query": {"measures": ["orders.count"]}}, agent(scopes=["apps:read"]))
    assert e.value.code == "forbidden" and "data:read" in e.value.message
    with pytest.raises(ToolError, match="workspace members"):
        tools.call("list_cubes", {}, agent(role=None))
    with pytest.raises(ToolError, match="another workspace"):
        tools.call("list_cubes", {}, agent().model_copy(update={"workspace_id": uuid4()}))
    # An app viewer (allowed in by IAP) is not a member but may read through the app.
    viewer = Caller(workspace_id=WS, via="app", email="carol@acme.test", app_id=uuid4())
    assert tools.call("list_cubes", {}, viewer)["cubes"]


def test_data_roles_and_attributes_drive_policies_and_grants(tools, runner):
    cat = runner.catalog
    cat.assign_role("domain", "acme.test", "regional_manager", by="user:admin")
    cat.set_attribute("domain", "acme.test", "region", "Southeast", by="user:admin")
    cat.set_attribute("user", "ana@acme.test", "region", "South", by="user:admin")  # user beats domain
    rows = tools.call("query", {"query": {"measures": ["orders.count"], "dimensions": ["states.region"]}}, agent())["rows"]
    assert [r["states__region"] for r in rows] == ["South"]
    rows = tools.call("query", {"query": {"measures": ["orders.count"], "dimensions": ["states.region"]}}, agent(email="dan@acme.test"))["rows"]
    assert [r["states__region"] for r in rows] == ["Southeast"]

    with pytest.raises(ToolError) as e:
        tools.call("query", {"query": {"measures": ["order_items.freight"]}}, agent())
    assert e.value.code == "unknown_member"
    finance = agent().model_copy(update={"groups": ["finance@acme.test"]})
    cat.assign_role("group", "finance@acme.test", "finance", by="user:admin")
    assert tools.call("query", {"query": {"measures": ["order_items.freight"]}}, finance)["rows"]


def test_workspace_roles_are_hierarchical_and_identity_wins(runner):
    cat = runner.catalog
    cat.set_attribute("domain", "acme.test", "email", "ceo@acme.test", by="user:admin")
    ctx = security_context(console(role="admin"), cat)
    assert {"workspace:admin", "workspace:developer", "workspace:viewer"} <= set(ctx.roles)
    assert "workspace:owner" not in ctx.roles
    assert ctx.attrs["email"] == "bob@acme.test"


def test_errors_carry_codes_and_hints(tools):
    with pytest.raises(ToolError) as e:
        tools.call("query", {"query": {"measures": ["orders.cuont"]}}, agent())
    assert e.value.code == "unknown_member" and "orders.count" in e.value.hint
    with pytest.raises(ToolError) as e:
        tools.call("query", {"query": {"measures": ["orders.count"], "dimensions": ["products.category"]}}, agent())
    assert e.value.code == "fan_out"
    with pytest.raises(ToolError) as e:
        tools.call("query", {"query": {"measures": ["orders.count"], "limit": 0}}, agent())
    assert e.value.code == "invalid_arguments" and "limit" in e.value.message
    with pytest.raises(ToolError) as e:
        tools.call("describe_cube", {"cube": "nope"}, agent())
    assert e.value.code == "unknown_cube"


def test_search_values_tool(tools):
    assert tools.call("search_values", {"dimension": "customers.city", "text": "rio"}, agent())["values"] == ["rio de janeiro"]


def test_propose_approve_apply(tools, runner):
    with pytest.raises(ToolError, match="developer role"):
        tools.call("propose_migration", {"description": "Add it", "ops": [{"op": "add_measure", "name": "orders.unique_customers", "sql": "customer_id"}]}, agent(role="viewer"))
    out = tools.call("propose_migration", {"description": "Count distinct ordering customers",
                                           "ops": [{"op": "add_measure", "name": "orders.unique_customers", "sql": "customer_id"}]}, agent())
    mid = out["id"]
    assert out["status"] == "proposed" and out["approval_url"].endswith(f"/w/{WS}/data/migrations/{mid}")

    with pytest.raises(ToolError) as e:
        tools.call("apply_migration", {"id": mid}, agent())
    assert e.value.code == "needs_approval" and "console.shipcube.dev" in e.value.hint
    with pytest.raises(ToolError, match="not with a token"):
        tools.approve(mid, agent(role="owner"))
    # The token's owner may approve their agent's proposal from the console: different principal.
    tools.approve(mid, console(role="developer", user=ANA, email="ana@acme.test"))
    applied = tools.call("apply_migration", {"id": mid}, agent())
    assert applied["status"] == "applied" and applied["version"] == 6

    log = runner.catalog.audit_log()
    propose, approve, apply_ = [r for r in log if r["target_id"] == mid]
    assert (propose["actor_type"], propose["actor_token_id"]) == ("token", str(TOKEN))
    assert (approve["actor_type"], approve["actor_user_id"]) == ("user", str(ANA))
    assert apply_["workspace_id"] == str(WS)


def test_breaking_changes_need_an_admin_and_no_breaks(tools, runner):
    runner.catalog.save_query("by_category", {"measures": ["order_items.revenue"], "dimensions": ["products.category"]},
                              app="sales", tenant="acme")
    out = tools.call("propose_migration", {"description": "Drop products join",
                                           "ops": [{"op": "drop_join", "cube": "order_items", "to": "products"}]}, agent())
    assert out["kind"] == "breaking" and out["impact"]["apps_broken"] == ["sales"]
    with pytest.raises(ToolError, match="needs the admin role"):
        tools.approve(out["id"], console(role="developer"))
    tools.approve(out["id"], console(role="admin"))
    with pytest.raises(ToolError) as e:
        tools.call("apply_migration", {"id": out["id"]}, agent())
    assert e.value.code == "would_break"
