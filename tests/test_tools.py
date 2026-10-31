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
    cat.set_setting("workspace_id", str(WS))
    # Viewers from each customer's Google domain read that customer's schema.
    cat.set_attribute("domain", "acme.test", "tenant", "acme", by="user:admin")
    cat.set_attribute("domain", "globex.test", "tenant", "globex", by="user:admin")
    layer = DataLayer(cat, duckdb_executor(warehouse))
    return DataTools(lambda ws: layer)


def agent(role="developer", scopes=("data:read", "data:migrate"), email="ana@acme.test"):
    return Caller(workspace_id=WS, via="mcp", email=email, user_id=ANA, membership_role=role,
                  token_id=TOKEN, scopes=list(scopes))


def console(role="admin", user=BOB, email="bob@acme.test"):
    return Caller(workspace_id=WS, via="console", email=email, user_id=user, membership_role=role)


def test_specs_are_json_schemas(tools):
    specs = {s["name"]: s for s in tools.specs()}
    assert set(specs) == {"data_list_cubes", "data_describe_cube", "data_query", "data_search_values", "data_propose_migration"}
    json.dumps(specs)  # serialisable as-is
    assert specs["data_query"]["scope"] == "data:read" and specs["data_propose_migration"]["scope"] == "data:migrate"
    ops = specs["data_propose_migration"]["inputSchema"]["properties"]["ops"]["anyOf"][0]
    assert "add_measure" in ops["items"]["discriminator"]["mapping"]
    assert "measures" in json.dumps(specs["data_query"]["inputSchema"])


def test_prefixed_and_bare_names_both_resolve_but_agents_cannot_apply(tools):
    assert tools.call("data_list_cubes", {}, agent()) == tools.call("list_cubes", {}, agent())
    for name in ("apply_migration", "data_apply_migration"):
        with pytest.raises(ToolError) as e:
            tools.call(name, {"id": "0006_x"}, agent())
        assert e.value.code == "unknown_tool"


def test_query_through_the_tool(tools):
    out = tools.call("query", {"query": {"measures": ["orders.count"], "dimensions": ["states.region"], "limit": 3}}, agent())
    assert out["verified"] and len(out["rows"]) == 3 and out["truncated"] is False
    json.dumps(out)  # decimals and timestamps are converted
    sql = tools.call("query", {"query": {"measures": ["orders.count"]}, "mode": "sql", "dialect": "bigquery"}, agent())
    assert sql["dialect"] == "bigquery" and "tenant_acme" in sql["sql"]


def test_staged_queries_and_governed_sql_through_the_tool(tools):
    staged = {"stages": {"m": {"query": {"measures": ["orders.count"], "time": {"dimension": "orders.purchased_at", "granularity": "quarter"}}},
                         "t": {"from": "m", "group": {"agg": {"avg_per_quarter": {"fn": "avg", "of": "orders.count"}}}}},
              "output": "t"}
    out = tools.call("query", {"query": staged}, agent())
    assert out["level"] == "L2" and out["verified"] and out["rows"][0]["avg_per_quarter"] > 0
    json.dumps(out)
    out = tools.call("query", {"sql": "SELECT status, count(*) AS n FROM orders GROUP BY status"}, agent())
    assert out["governed"] and out["verified"] is False and sum(r["n"] for r in out["rows"]) == 1500
    compiled = tools.call("query", {"sql": "SELECT count(*) FROM orders", "mode": "sql", "dialect": "bigquery"}, agent())
    assert compiled["dialect"] == "bigquery" and "tenant_acme.orders_2018_08" in compiled["sql"]
    with pytest.raises(ToolError) as e:
        tools.call("query", {"query": {"measures": ["orders.count"]}, "sql": "SELECT 1"}, agent())
    assert e.value.code == "invalid_arguments"
    with pytest.raises(ToolError) as e:
        tools.call("query", {"sql": "SELECT * FROM tenant_globex.orders_2018_01"}, agent())
    assert e.value.code == "forbidden_sql"


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
    cat.set_setting("workspace_id", str(WS))
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
    assert out["summary"] == ["[additive] Add measure orders.unique_customers = count_distinct(customer_id)"]
    assert out["explained_by"] == "agent"

    ana = console(role="developer", user=ANA, email="ana@acme.test")
    with pytest.raises(ToolError) as e:
        tools.apply(mid, ana)
    assert e.value.code == "needs_approval" and "console.shipcube.dev" in e.value.hint
    for who in (agent(role="owner"),):
        with pytest.raises(ToolError, match="not with a token"):
            tools.approve(mid, who)
        with pytest.raises(ToolError, match="not with a token"):
            tools.apply(mid, who)
    # Red-team #6: the token's owner is the same person, so they cannot approve their agent's proposal.
    with pytest.raises(ToolError, match="someone other than the person"):
        tools.approve(mid, ana.model_copy(update={"membership_role": "admin"}))
    # A new measure can expose data, so another developer is not enough either; an admin is.
    with pytest.raises(ToolError, match="admin role: it can expose more data"):
        tools.approve(mid, console(role="developer"))
    tools.approve(mid, console(role="admin"))
    applied = tools.apply(mid, ana)
    assert applied["status"] == "applied" and applied["version"] == 7

    log = runner.catalog.audit_log()
    propose, approve, apply_ = [r for r in log if r["target_id"] == mid]
    assert (propose["actor_type"], propose["actor_token_id"]) == ("token", str(TOKEN))
    assert (approve["actor_type"], approve["actor_user_id"]) == ("user", str(BOB))
    assert runner.catalog.migration(mid)["proposed_by_user"] == str(ANA)
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
        tools.apply(out["id"], console(role="developer"))
    assert e.value.code == "would_break"
    with pytest.raises(ToolError, match="needs admin"):
        tools.apply(out["id"], console(role="developer"), allow_breaking=True)
    assert tools.apply(out["id"], console(role="admin"), allow_breaking=True)["status"] == "applied"


def test_metadata_only_changes_can_be_approved_by_a_developer(tools):
    out = tools.call("data_propose_migration", {"description": "Describe orders",
                                                "ops": [{"op": "set_meta", "target": "orders", "description": "One row per order"}]}, agent())
    assert out["kind"] == "additive" and out["widens_exposure"] == []
    tools.approve(out["id"], console(role="developer"))  # BOB, not the proposer


@pytest.mark.parametrize("sql,error", [
    ("(SELECT max(freight_value) FROM order_items)", "got a statement|subqueries"),
    ("1 + (SELECT max(freight_value) FROM order_items)", "subqueries or table references"),
    ("EXISTS (SELECT 1 FROM tenant_globex.customers)", "subqueries or table references"),
    ("read_text('/proc/self/environ')", "may not call read_text"),
    ("getenv('META_API_KEY')", "may not call getenv"),
])
def test_member_sql_cannot_reach_other_tables_or_the_host(tools, sql, error):
    with pytest.raises(ToolError, match=error):
        tools.call("data_propose_migration", {"description": "Sneaky", "ops": [{"op": "add_dimension", "name": "orders.x", "sql": sql}]}, agent())


def test_old_data_write_scope_still_proposes(tools):
    out = tools.call("data_propose_migration", {"description": "Describe orders",
                                                "ops": [{"op": "set_meta", "target": "orders", "description": "x"}]},
                     agent(scopes=["data:read", "data:write"]))
    assert out["status"] == "proposed"
    with pytest.raises(ToolError, match="data:migrate"):
        tools.call("data_propose_migration", {"description": "x y", "ops": [{"op": "set_meta", "target": "orders", "description": "x"}]},
                   agent(scopes=["data:read"]))


def test_unprovisioned_tenant_is_a_typed_error(tools):
    # A caller whose domain maps to no demo tenant falls back to the workspace tenant, whose schema doesn't exist.
    for name, args in [("data_query", {"query": {"measures": ["orders.count"]}}),
                       ("data_search_values", {"dimension": "customers.city", "text": "a"})]:
        with pytest.raises(ToolError) as e:
            tools.call(name, args, agent(email="someone@unmapped.test"))
        assert e.value.code == "tenant_not_provisioned", e.value.to_dict()
        assert "data attribute `tenant`" in e.value.hint


def test_unexpected_errors_become_internal_error(tools, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("driver exploded with secret-ish detail")

    monkeypatch.setattr(tools, "layer_for", boom)
    with pytest.raises(ToolError) as e:
        tools.call("data_list_cubes", {}, agent())
    assert e.value.code == "internal_error" and "secret-ish" not in e.value.message and e.value.hint.startswith("logged as err_")
