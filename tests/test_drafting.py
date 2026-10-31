import json

import pytest

from shipcube_data.drafting import MuseDrafter, RuleDrafter
from shipcube_data.testing import MockMuse
from shipcube_data.model import ModelError

from .test_tools import WS, agent, tools  # noqa: F401  (fixture)
from shipcube_data.tools import DataTools, ToolError

APP_CODE = '''
def revenue_by_status(con):
    return con.execute("""
        SELECT o.order_status, COUNT(DISTINCT o.customer_id) AS buyers, MAX(o.order_purchase_timestamp) AS last_order,
               COUNT(*) AS n
        FROM orders o GROUP BY 1
    """).fetchall()
'''


def test_rules_draft_from_english(runner):
    model = runner.catalog.load()
    d = RuleDrafter().draft(model, "duckdb",
                            "Add measure unique_customers as count distinct of customer_id on orders. "
                            "Create dimension seller on order_items from seller_id. "
                            "Rename order_items.avg_price to mean_price", "")
    assert d.explained_by == "rules"
    assert d.ops == [
        {"op": "add_measure", "name": "orders.unique_customers", "agg": "count_distinct", "sql": "customer_id"},
        {"op": "add_dimension", "name": "order_items.seller", "sql": "seller_id"},
        {"op": "rename_member", "member": "order_items.avg_price", "to": "mean_price"},
    ]


def test_rules_lift_aggregates_out_of_app_sql(runner):
    d = RuleDrafter().draft(runner.catalog.load(), "duckdb", "", APP_CODE)
    assert d.ops == [
        {"op": "add_measure", "name": "orders.buyers", "sql": "customer_id", "agg": "count_distinct"},
        {"op": "add_measure", "name": "orders.last_order", "sql": "order_purchase_timestamp", "agg": "max"},
    ]
    assert any("already modelled as orders.count" in n for n in d.notes)  # COUNT(*) exists


def test_rules_give_up_clearly(runner):
    with pytest.raises(ModelError) as e:
        RuleDrafter().draft(runner.catalog.load(), "duckdb", "make it better", "")
    assert e.value.code == "cannot_draft" and "count distinct" in e.value.hint


def test_muse_draft_is_used_when_valid(runner):
    reply = 'Sure! {"description": "Track buyers", "ops": [{"op": "add_measure", "name": "orders.buyers", "sql": "customer_id", "agg": "count_distinct"}]}'
    muse = MockMuse(reply)
    d = MuseDrafter(muse).draft(runner.catalog.load(), "duckdb", "track how many distinct people buy", "")
    assert d.explained_by == "muse-spark" and d.description == "Track buyers"  # MockMuse has no .model
    system, user = muse.calls[0]
    assert "add_measure" in system and "orders (table orders" in user  # model summary grounds the draft


@pytest.mark.parametrize("reply", [
    "I cannot help with that",
    '{"ops": [{"op": "launch_rockets"}]}',
    '{"ops": []}',
])
def test_muse_failures_fall_back_to_rules(runner, reply):
    d = MuseDrafter(MockMuse(reply)).draft(runner.catalog.load(), "duckdb", "add measure buyers as count distinct of customer_id on orders", "")
    assert d.explained_by == "rules" and d.ops[0]["name"] == "orders.buyers"
    assert "drafted with rules instead" in d.notes[0]


def test_muse_exception_and_absence_fall_back(runner):
    def boom(system, user):
        raise TimeoutError("gateway timeout")

    for drafter in (MuseDrafter(boom), MuseDrafter(None)):
        d = drafter.draft(runner.catalog.load(), "duckdb", "add measure buyers as count distinct of customer_id on orders", "")
        assert d.explained_by == "rules"


def test_propose_tool_drafts_then_dry_runs(tools, runner):  # noqa: F811
    muse = MockMuse(json.dumps({"description": "Buyers per order status",
                                "ops": [{"op": "add_measure", "name": "orders.buyers", "sql": "customer_id"}]}))
    t = DataTools(tools.layer_for, drafter=MuseDrafter(muse))
    out = t.call("data_propose_migration", {"request": "how many distinct buyers", "code": APP_CODE}, agent())
    assert out["explained_by"] == "muse-spark" and out["status"] == "proposed"
    assert out["summary"] == ["[additive] Add measure orders.buyers = sum(customer_id)"]  # reviewer sees the inferred agg
    assert runner.catalog.migration(out["id"])["status"] == "proposed"
    assert runner.catalog.head() == 6  # nothing applied

    with pytest.raises(ToolError) as e:
        t.call("data_propose_migration", {}, agent())
    assert e.value.code == "invalid_arguments"
    with pytest.raises(ToolError) as e:
        tools.call("data_propose_migration", {"request": "make it better"}, agent())
    assert e.value.code == "cannot_draft"
