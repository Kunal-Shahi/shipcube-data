"""Pre-aggregations: built per month in the warehouse, used only when they give exactly the warehouse's answer."""

import pytest

from shipcube_data import rollups
from shipcube_data.compiler import SecurityContext
from shipcube_data.engine import DataLayer
from shipcube_data.migrate import MigrationError
from shipcube_data.model import ModelError
from shipcube_data.ops import Migration

DAILY = {"op": "define_rollup", "cube": "order_items", "name": "daily",
         "measures": ["order_items.revenue", "order_items.count", "order_items.avg_price", "order_items.max_price", "order_items.gmv"],
         "dimensions": ["states.region", "products.category", "customers.state"],
         "time_dimension": "order_items.shipping_limit_at", "granularity": "day", "start": "2017-01-01"}
REGIONAL = SecurityContext(tenant="acme", roles=["regional_manager"], attrs={"region": "South"})
FINANCE = SecurityContext(tenant="acme", roles=["analyst", "finance"])


def apply(runner, mid, *ops):
    return runner.apply(Migration.model_validate({"id": mid, "ops": list(ops)}))


@pytest.fixture
def built(runner, dl, warehouse):
    apply(runner, "0006_rollup", DAILY)
    out = rollups.build(dl, "acme", "order_items.daily", rollups.duckdb_writer(warehouse), schema="rollups")
    assert len(out["built"]) >= 20 and sum(b["rows"] for b in out["built"]) > 0
    return out


def both(dl, q, ctx, monkeypatch):
    """The query answered with rollups, and without (straight from the warehouse)."""
    routed = dl.query(q, ctx, use_cache=False)
    with monkeypatch.context() as m:
        m.setattr(DataLayer, "_rollups", lambda *a, **k: [])
        direct = dl.query(q, ctx, use_cache=False)
    return routed, direct


def same(a, b):
    def norm(rows):
        return sorted([{k: (round(float(v), 6) if isinstance(v, (int, float)) or type(v).__name__ == "Decimal" else v)
                        for k, v in r.items()} for r in rows], key=repr)
    return norm(a["rows"]) == norm(b["rows"])


ROUTED = [
    {"measures": ["order_items.revenue"], "dimensions": ["states.region"]},
    {"measures": ["order_items.revenue", "order_items.count", "order_items.avg_price", "order_items.max_price"],
     "dimensions": ["products.category"]},
    {"measures": ["order_items.revenue"], "dimensions": ["states.region"],
     "time": {"dimension": "order_items.shipping_limit_at", "granularity": "month", "range": ["2018-01-01", "2018-03-31"]}},
    {"measures": ["order_items.count"], "time": {"dimension": "order_items.shipping_limit_at", "granularity": "week"}},
    {"measures": ["order_items.revenue"], "time": {"dimension": "order_items.shipping_limit_at", "granularity": "quarter"}},
    {"measures": ["order_items.revenue"], "time": {"dimension": "order_items.shipping_limit_at", "range": ["2017-06-10", "2017-08-20"]}},
    {"measures": ["order_items.revenue"], "dimensions": ["customers.state"],
     "filters": [{"member": "states.region", "op": "equals", "values": ["Southeast", "South"]},
                 {"member": "order_items.revenue", "op": "gt", "values": [1000]}]},
    {"measures": ["order_items.revenue"], "dimensions": ["states.region"],
     "time": {"dimension": "order_items.shipping_limit_at", "granularity": "month"},
     "calcs": [{"name": "share", "op": "share", "measure": "order_items.revenue", "by": ["order_items.shipping_limit_at"]},
               {"name": "growth", "op": "compare", "measure": "order_items.revenue"}]},
]


@pytest.mark.parametrize("q", ROUTED)
def test_routed_answers_equal_the_warehouse(dl, acme, built, monkeypatch, q):
    routed, direct = both(dl, q, acme, monkeypatch)
    assert routed["rollup"] == "order_items.daily" and direct["rollup"] is None
    assert "rollups.order_items__daily__" in routed["sql"] and "tenant_acme" not in routed["sql"]
    assert same(routed, direct) and routed["rows"]


def test_l2_stages_use_rollups_too(dl, acme, built, monkeypatch):
    q = {"stages": {"m": {"query": {"measures": ["order_items.revenue"], "time": {"dimension": "order_items.shipping_limit_at", "granularity": "month"}}},
                    "avg": {"from": "m", "group": {"agg": {"avg_monthly": {"fn": "avg", "of": "order_items.revenue"}}}}}, "output": "avg"}
    routed, direct = both(dl, q, acme, monkeypatch)
    assert routed["rollup"] == "order_items.daily" and "rollups.order_items__daily__" in routed["sql"] and same(routed, direct)


@pytest.mark.parametrize("q", [
    {"measures": ["order_items.revenue"], "dimensions": ["order_items.seller_id"]},              # dimension not in the rollup
    {"measures": ["orders.count"], "dimensions": ["states.region"]},                             # another cube's measure
    {"measures": ["order_items.revenue"], "segments": ["orders.delivered"]},                     # segments
    {"measures": ["order_items.revenue"],                                                        # a datetime bound inside a day
     "time": {"dimension": "order_items.shipping_limit_at", "range": ["2018-01-01T12:00:00", "2018-02-01"]}},
    {"measures": ["order_items.revenue"],                                                        # a filter on a time dimension it doesn't bucket
     "filters": [{"member": "orders.purchased_at", "op": "after_date", "values": ["2018-01-01"]}]},
    {"measures": ["order_items.revenue"], "model_version": 5},                                   # pinned versions
])
def test_not_routed_when_the_rollup_cant_answer_exactly(dl, acme, built, monkeypatch, q):
    routed, direct = both(dl, q, acme, monkeypatch)
    assert routed["rollup"] is None and same(routed, direct)


def test_grants_and_row_policies_apply_to_rollups(dl, acme, built, monkeypatch):
    with pytest.raises(ModelError, match="not permitted: order_items.gmv"):  # gmv reads freight: finance only, rollup or not
        dl.query({"measures": ["order_items.gmv"]}, acme)
    routed, direct = both(dl, {"measures": ["order_items.gmv"], "dimensions": ["states.region"]}, FINANCE, monkeypatch)
    assert routed["rollup"] and same(routed, direct)
    # The orders policy (a region for regional managers) is evaluated on the rollup's region column.
    q = {"measures": ["order_items.revenue"], "dimensions": ["customers.state"]}
    routed, direct = both(dl, q, REGIONAL, monkeypatch)
    assert routed["rollup"] and same(routed, direct)
    everyone = dl.query(q, acme, use_cache=False)["rows"]
    assert len(routed["rows"]) < len(everyone)
    with pytest.raises(ModelError, match="no 'region'"):
        dl.query(q, SecurityContext(tenant="acme", roles=["regional_manager"]))


def test_a_policy_the_rollup_cant_evaluate_sends_the_query_to_the_warehouse(runner, dl, built, warehouse, monkeypatch):
    apply(runner, "0007_city_policy", {"op": "add_row_policy", "name": "orders.sao_paulo_only", "roles": ["city_manager"],
                                       "filter": {"member": "customers.city", "op": "equals", "values": ["sao paulo"]}})
    rollups.build(dl, "acme", "order_items.daily", rollups.duckdb_writer(warehouse), schema="rollups")  # same data, still current
    ctx = SecurityContext(tenant="acme", roles=["city_manager"])
    routed, direct = both(dl, {"measures": ["order_items.revenue"], "dimensions": ["states.region"]}, ctx, monkeypatch)
    assert routed["rollup"] is None and same(routed, direct)


def test_tenants_only_use_their_own_rollups(dl, globex, built, monkeypatch):
    routed, direct = both(dl, {"measures": ["order_items.revenue"], "dimensions": ["states.region"]}, globex, monkeypatch)
    assert routed["rollup"] is None and same(routed, direct)


def test_missing_or_stale_months_send_the_query_to_the_warehouse(runner, dl, acme, warehouse, monkeypatch):
    apply(runner, "0006_rollup", DAILY)
    rollups.build(dl, "acme", "order_items.daily", rollups.duckdb_writer(warehouse), schema="rollups",
                  periods=["2018-01-01", "2018-02-01"])
    q = {"measures": ["order_items.revenue"], "time": {"dimension": "order_items.shipping_limit_at", "range": ["2018-01-01", "2018-02-28"]}}
    assert dl.query(q, acme, use_cache=False)["rollup"] == "order_items.daily"
    assert dl.query({"measures": ["order_items.revenue"]}, acme, use_cache=False)["rollup"] is None  # all time: months missing
    # January built before the month ended, more than max_age ago: stale.
    runner.catalog.db.execute("UPDATE rollup_partitions SET built_at = '2018-01-15T00:00:00+00:00' WHERE period = '2018-01-01'")
    routed, direct = both(dl, q, acme, monkeypatch)
    assert routed["rollup"] is None and same(routed, direct)


def test_definition_changes_retire_built_rollups(runner, dl, acme, built, warehouse, monkeypatch):
    q = {"measures": ["order_items.revenue"], "dimensions": ["states.region"]}
    apply(runner, "0007_net", {"op": "change_sql", "member": "order_items.revenue", "sql": "price - freight_value"})
    routed, direct = both(dl, q, acme, monkeypatch)
    assert routed["rollup"] is None and same(routed, direct)  # old numbers are never served
    rollups.build(dl, "acme", "order_items.daily", rollups.duckdb_writer(warehouse), schema="rollups")
    routed, direct = both(dl, q, acme, monkeypatch)
    assert routed["rollup"] == "order_items.daily" and same(routed, direct)


@pytest.mark.parametrize("change,match", [
    ({"measures": ["orders.unique_orders"], "cube": "orders", "time_dimension": "orders.purchased_at"}, "count_distinct"),
    ({"measures": ["order_items.revenue"], "dimensions": ["order_items.nope"]}, "nope"),
    ({"measures": ["order_items.revenue"], "time_dimension": "states.region"}, "time dimension"),
    ({"measures": ["orders.count"], "cube": "orders", "time_dimension": "orders.purchased_at",
      "dimensions": ["products.category"]}, "many-to-one"),
    ({"start": "last year"}, "ISO date"),
])
def test_invalid_rollups_are_refused(runner, change, match):
    with pytest.raises((ModelError, MigrationError), match=match):
        apply(runner, "0006_bad", {**DAILY, **change})


def test_members_in_rollups_follow_renames_and_cant_be_dropped(runner, built):
    apply(runner, "0007_ren", {"op": "rename_member", "member": "states.region", "to": "macro_region"})
    assert "states.macro_region" in runner.catalog.load().cube("order_items").rollups["daily"].dimensions
    with pytest.raises((ModelError, MigrationError), match="rollup order_items.daily"):
        apply(runner, "0008_drop", {"op": "drop_member", "member": "states.macro_region"})


def test_cli_builds_months_and_queries_use_them(tmp_path):
    import duckdb

    from shipcube_data import demo
    from shipcube_data.catalog import Catalog
    from shipcube_data.cli import main
    from shipcube_data.engine import duckdb_executor
    from shipcube_data.migrate import Runner, load_dir

    wh, cat_path = tmp_path / "wh.duckdb", tmp_path / "catalog.db"
    con = duckdb.connect(str(wh))
    demo.build(con)
    con.close()
    cat = Catalog(str(cat_path))
    Runner(cat).apply_all(load_dir(demo.MIGRATIONS), actor="t")
    Runner(cat).apply(Migration.model_validate({"id": "0006_rollup", "ops": [DAILY]}))
    assert main(["rollup-build", "--catalog", str(cat_path), "--tenant", "acme", "--rollup", "order_items.daily",
                 "--writer-url", str(wh), "--period", "2018-01-01", "--rollup-schema", "rollups"]) == 0
    dl = DataLayer(Catalog(str(cat_path)), duckdb_executor(duckdb.connect(str(wh), read_only=True)))
    q = {"measures": ["order_items.revenue"], "time": {"dimension": "order_items.shipping_limit_at", "range": ["2018-01-01", "2018-01-31"]}}
    assert dl.query(q, SecurityContext(tenant="acme"))["rollup"] == "order_items.daily"
