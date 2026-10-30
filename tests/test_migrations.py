import json

import pytest

from shipcube_data import demo
from shipcube_data.catalog import Catalog
from shipcube_data.migrate import MigrationError, Runner, load_dir
from shipcube_data.model import ModelError
from shipcube_data.ops import Migration, infer_agg, infer_type


def mig(mid, *ops, base=None):
    return Migration.model_validate({"id": mid, "base_version": base, "ops": list(ops)})


def test_demo_migrations_apply_in_order(runner):
    status = runner.status(load_dir(demo.MIGRATIONS))
    assert [s["state"] for s in status] == ["applied"] * 5
    assert runner.catalog.head() == 5
    model = runner.catalog.load()
    assert set(model.cubes) == {"customers", "states", "orders", "products", "order_items"}


def test_apply_is_idempotent(runner):
    assert runner.apply_all(load_dir(demo.MIGRATIONS)) == []
    assert runner.apply(load_dir(demo.MIGRATIONS)[0])["status"] == "already_applied"
    assert runner.catalog.head() == 5


def test_editing_an_applied_migration_is_refused(runner):
    m = load_dir(demo.MIGRATIONS)[0]
    edited = Migration.model_validate({**m.model_dump(by_alias=True), "ops": [{"op": "bind_schema", "schema": "other_{tenant}"}]})
    with pytest.raises(MigrationError, match="edited after it was applied"):
        runner.apply(edited)


def test_minimal_cube_gets_inferred_members():
    r = Runner(Catalog())
    r.apply(mig("0001_x",
                {"op": "create_cube", "name": "events", "table": "events"},
                {"op": "add_dimension", "name": "events.created_at", "sql": "created_at"},
                {"op": "add_dimension", "name": "events.is_bot", "sql": "bot"},
                {"op": "add_measure", "name": "events.unique_users", "sql": "user_id"},
                {"op": "add_measure", "name": "events.avg_latency", "sql": "latency_ms"},
                {"op": "add_measure", "name": "events.bot_count", "agg": "count", "filter": "{is_bot}"},
                {"op": "add_measure", "name": "events.bot_share", "sql": "1.0 * {bot_count} / {count}"},
                {"op": "add_measure", "name": "events.duration", "sql": "duration"}))
    cube = r.catalog.load().cubes["events"]
    assert cube.measures["count"].agg == "count"
    assert cube.dimensions["created_at"].type == "time"
    assert cube.dimensions["is_bot"].type == "boolean"
    assert cube.measures["unique_users"].agg == "count_distinct"
    assert cube.measures["avg_latency"].agg == "avg"
    assert cube.measures["duration"].agg == "sum"
    assert cube.measures["bot_share"].agg == "number"


def test_number_measure_refs_must_exist():
    r = Runner(Catalog())
    with pytest.raises(ModelError, match="bot_count"):
        r.apply(mig("0001_x",
                    {"op": "create_cube", "name": "events", "table": "events"},
                    {"op": "add_measure", "name": "events.bot_share", "sql": "1.0 * {bot_count} / {count}"}))
    assert r.catalog.head() == 0  # nothing half-applied


@pytest.mark.parametrize("name,sql,agg", [
    ("total", "SUM(x) / COUNT(*)", "number"),
    ("count_orders", "order_id", "count"),
    ("max_price", "price", "max"),
    ("revenue", "price", "sum"),
])
def test_infer_agg(name, sql, agg):
    assert infer_agg(name, sql, "duckdb") == agg


def test_infer_type():
    assert [infer_type(n) for n in ("shipped_at", "has_coupon", "unit_price", "city")] == ["time", "boolean", "number", "string"]


@pytest.mark.parametrize("op,error", [
    ({"op": "add_dimension", "name": "orders.total", "sql": "SUM(price)"}, "cannot aggregate"),
    ({"op": "add_measure", "name": "orders.count", "sql": "1"}, "already exists"),
    ({"op": "add_measure", "name": "orders.bad", "sql": "price +"}, "cannot parse"),
    ({"op": "add_measure", "name": "nope.x", "sql": "1"}, "unknown cube"),
    ({"op": "add_dimension", "name": "orders.Bad", "sql": "x"}, "snake_case"),
    ({"op": "add_join", "cube": "orders", "to": "products", "on": "orders.x = other.y"}, "must be qualified"),
    ({"op": "drop_member", "member": "orders.delivered_count"}, "used by orders.late_rate"),
    ({"op": "drop_cube", "cube": "customers"}, "joins to customers"),
    ({"op": "add_row_policy", "name": "orders.p", "filter": {"member": "orders.count", "op": "gt", "values": [1]}}, "dimensions"),
])
def test_invalid_ops_are_rejected(runner, op, error):
    with pytest.raises(ModelError, match=error):
        runner.apply(mig("0100_bad", op))
    assert runner.catalog.head() == 5


def test_reference_cycles_are_rejected(runner):
    with pytest.raises(ModelError, match="cycle"):
        runner.apply(mig("0100_cycle",
                         {"op": "add_measure", "name": "orders.a", "sql": "{count} + 1"},
                         {"op": "change_sql", "member": "orders.count", "sql": "{a}"}))


def test_rename_keeps_old_name_working(runner, dl, acme):
    runner.apply(mig("0006_rename", {"op": "rename_member", "member": "order_items.revenue", "to": "item_revenue"}))
    model = runner.catalog.load()
    assert "item_revenue" in model.cubes["order_items"].measures
    # gmv referenced {revenue}; it was rewritten
    assert model.cubes["order_items"].measures["gmv"].sql == "{item_revenue} + {freight}"
    res = dl.query({"measures": ["order_items.revenue"], "limit": 1}, acme)
    assert "renamed to order_items.item_revenue" in " ".join(res["warnings"])
    assert list(res["rows"][0]) == ["order_items__revenue"]  # result shape unchanged for old callers
    new = dl.query({"measures": ["order_items.item_revenue"], "limit": 1}, acme)
    assert list(res["rows"][0].values()) == list(new["rows"][0].values())
    assert dl.describe_cube("order_items", acme)["aliases"] == {"order_items.revenue": "order_items.item_revenue"}


def test_change_sql_is_versioned_and_pinnable(runner, dl, acme):
    before = dl.query({"measures": ["order_items.revenue"]}, acme)["rows"][0]["order_items__revenue"]
    runner.apply(mig("0006_net", {"op": "change_sql", "member": "order_items.revenue", "sql": "price - freight_value",
                                  "pin_compatible": True}))
    after = dl.query({"measures": ["order_items.revenue"]}, acme)["rows"][0]["order_items__revenue"]
    pinned = dl.query({"measures": ["order_items.revenue"], "model_version": 5}, acme)["rows"][0]["order_items__revenue"]
    assert after < before and pinned == before
    hist = runner.catalog.history("order_items", "revenue")
    assert [(h["sql"], h["since_version"], h["until_version"]) for h in hist] == [("price", 2, 6), ("price - freight_value", 6, None)]


def _cities(dl, ctx, **pin):
    rows = dl.query({"dimensions": ["customers.city"], "measures": ["customers.count"], "order": [{"member": "customers.city"}],
                     "limit": 5, **pin}, ctx)
    return [r["customers__city"] for r in rows["rows"]], rows["warnings"]


def test_a_pin_cannot_undo_a_masking_change(runner, dl, acme):
    """shipcube-data #8: pinning a version from before a masking change_sql must not unmask the member.
    Fails closed: no flag is needed, an old definition comes back only for `pin_compatible` changes."""
    plain, _ = _cities(dl, acme)
    runner.apply(mig("0006_mask", {"op": "change_sql", "member": "customers.city", "sql": "md5(customer_city)"}))
    masked, _ = _cities(dl, acme)
    pinned, warnings = _cities(dl, acme, model_version=5)
    assert masked != plain and pinned == masked
    assert any("customers.city uses the current definition" in w for w in warnings)
    assert not any("customers.count" in w for w in warnings)
    # Renaming the member afterwards doesn't reopen the gap.
    runner.apply(mig("0007_rename", {"op": "rename_member", "member": "customers.city", "to": "town"}))
    rows = dl.query({"dimensions": ["customers.city"], "measures": ["customers.count"], "order": [{"member": "customers.town"}],
                     "limit": 5, "model_version": 5}, acme)["rows"]
    assert [r["customers__city"] for r in rows] == masked


def test_a_pin_compatible_change_sql_still_pins(runner, dl, acme):
    plain, _ = _cities(dl, acme)
    runner.apply(mig("0006_upper", {"op": "change_sql", "member": "customers.city", "sql": "upper(customer_city)", "pin_compatible": True}))
    pinned, warnings = _cities(dl, acme, model_version=5)
    assert pinned == plain and not any("current definition" in w for w in warnings)


def test_a_pin_cannot_bring_back_a_dropped_members_sql(runner, dl, acme):
    """Dropping a member and adding a masked one under the same name is a masking change too."""
    runner.apply(mig("0006_drop", {"op": "drop_member", "member": "customers.city"}))
    runner.apply(mig("0007_readd", {"op": "add_dimension", "name": "customers.city", "type": "string", "sql": "left(customer_city, 1)"}))
    masked, _ = _cities(dl, acme)
    pinned, _ = _cities(dl, acme, model_version=5)
    assert pinned == masked and all(len(c) == 1 for c in masked)


def test_a_pin_cannot_bring_back_a_dropped_cubes_members(runner, dl, acme):
    """drop_cube then create_cube under the same name: every member is new, whatever its name."""
    runner.apply(mig("0006_dropcube", {"op": "drop_join", "cube": "orders", "to": "customers"},
                     {"op": "drop_join", "cube": "customers", "to": "states"}, {"op": "drop_cube", "cube": "customers"}))
    runner.apply(mig("0007_recreate", {"op": "create_cube", "name": "customers", "table": "customers",
                                       "dimensions": {"customer_id": {"sql": "customer_id", "primary_key": True},
                                                      "city": "left(customer_city, 1)", "state": "customer_state"},
                                       "measures": {"unique_customers": "customer_unique_id"}}))
    masked, _ = _cities(dl, acme)
    pinned, warnings = _cities(dl, acme, model_version=5)
    assert pinned == masked and all(len(c) == 1 for c in masked)
    assert any("customers.city uses the current definition" in w for w in warnings)


def test_deprecation_warns(runner, dl, acme):
    runner.apply(mig("0006_dep", {"op": "deprecate_member", "member": "order_items.max_price",
                                  "reason": "use avg_price", "replaced_by": "order_items.avg_price"}))
    res = dl.query({"measures": ["order_items.max_price"]}, acme)
    assert any("deprecated" in w and "order_items.avg_price" in w for w in res["warnings"])


def test_rollback_restores_previous_model_exactly(runner):
    fp = runner.catalog.load().fingerprint()
    runner.apply(mig("0006_a", {"op": "rename_member", "member": "orders.status", "to": "order_status"},
                     {"op": "add_segment", "name": "orders.late", "sql": "{is_late}"}))
    assert runner.catalog.load().fingerprint() != fp
    report = runner.rollback(actor="test")
    assert report["status"] == "rolled_back" and report["to_version"] == 5
    assert runner.catalog.load().fingerprint() == fp
    assert runner.catalog.migration("0006_a")["status"] == "rolled_back"
    # versions are never reused
    runner.apply(mig("0007_b", {"op": "set_meta", "target": "orders", "description": "One row per order"}))
    assert runner.catalog.head() == 7


def test_conflicting_migrations_are_detected(runner):
    base = runner.catalog.head()
    runner.apply(mig("0006_alice", {"op": "change_sql", "member": "order_items.revenue", "sql": "price * 1.0"}, base=base))
    with pytest.raises(MigrationError, match="conflicts with.*0006_alice.*order_items.revenue"):
        runner.apply(mig("0007_bob", {"op": "rename_member", "member": "order_items.revenue", "to": "sales"}, base=base))
    # A disjoint change written against the same base rebases cleanly.
    rep = runner.apply(mig("0007_carol", {"op": "add_measure", "name": "orders.unique_customers", "sql": "customer_id"}, base=base))
    assert rep["rebased"] and rep["status"] == "applied"


def test_cube_level_change_conflicts_with_member_change(runner):
    base = runner.catalog.head()
    runner.apply(mig("0006_a", {"op": "add_measure", "name": "products.unique_categories", "sql": "product_category_name"}, base=base))
    with pytest.raises(MigrationError, match="conflicts"):
        runner.apply(mig("0007_b", {"op": "drop_join", "cube": "order_items", "to": "products"},
                         {"op": "drop_cube", "cube": "products"}, base=base))


def test_dry_run_reports_breaking_saved_queries(runner):
    cat = runner.catalog
    cat.save_query("revenue_by_category", {"measures": ["order_items.revenue"], "dimensions": ["products.category"]},
                   app="sales-dashboard", tenant="acme")
    cat.save_query("orders_by_month", {"measures": ["orders.count"], "time": {"dimension": "orders.purchased_at", "granularity": "month"}},
                   app="ops-app", tenant="acme")
    cat.save_query("late_orders", {"measures": ["orders.late_rate"]}, app="ops-app", tenant="acme")
    report = runner.dry_run(mig("0006_drop", {"op": "drop_join", "cube": "order_items", "to": "products"},
                                {"op": "change_sql", "member": "orders.is_late", "sql": "order_delivered_customer_date > order_estimated_delivery_date + INTERVAL 1 DAY"},
                                {"op": "rename_member", "member": "orders.purchased_at", "to": "ordered_at"}))
    assert report["status"] == "dry_run" and report["kind"] == "breaking"
    assert [b["query"] for b in report["impact"]["breaks"]] == ["revenue_by_category"]
    assert report["impact"]["apps_broken"] == ["sales-dashboard"]
    assert [c["query"] for c in report["impact"]["changes"]] == ["late_orders"]
    assert [w["query"] for w in report["impact"]["warnings"]] == ["orders_by_month"]  # rename: still works, but warns
    assert report["impact"]["unaffected"] == 0
    assert "joins:order_items.products" in report["diff"]["removed"]
    assert cat.head() == 5  # dry run wrote nothing
    with pytest.raises(MigrationError, match="would break saved queries: revenue_by_category"):
        runner.apply(mig("0006_drop", {"op": "drop_join", "cube": "order_items", "to": "products"}), allow_breaking=False)


def test_agent_proposal_needs_a_different_human_to_approve(runner):
    rep = runner.propose([{"op": "add_measure", "name": "order_items.min_price", "sql": "price"}],
                         "Add min price", proposed_by="agent:claude")
    mid = rep["id"]
    assert mid == "0006_add_min_price" and rep["status"] == "proposed" and rep["kind"] == "additive"
    with pytest.raises(MigrationError, match="needs human approval"):
        runner.apply(runner.stored(mid), actor="agent:claude", require_approval=True)
    with pytest.raises(MigrationError, match="other than the person who proposed it"):
        runner.approve(mid, "agent:claude")
    runner.approve(mid, "kunal")
    out = runner.apply(runner.stored(mid), actor="agent:claude", require_approval=True)
    assert out["status"] == "applied" and out["version"] == 6
    log = runner.catalog.audit_log()
    assert [a["action"] for a in log[-3:]] == ["data.migration.propose", "data.migration.approve", "data.migration.apply"]
    assert log[-1]["target_id"] == mid and log[-1]["actor_type"] == "system"


def test_catalog_rows_are_scd2(runner):
    rows = runner.catalog.db.execute(
        "SELECT COUNT(*) FROM members WHERE until_version IS NULL").fetchone()[0]
    model = runner.catalog.load()
    assert rows == sum(len(c.members()) for c in model.cubes.values())


def test_migration_files_roundtrip_through_json(tmp_path):
    for f in sorted(demo.MIGRATIONS.glob("*.json")):
        data = json.loads(f.read_text())
        Migration.model_validate({"id": f.stem, **data})


def test_mcp_schemas_generate():
    from shipcube_data.ops import OP_ADAPTER, OP_NAMES
    from shipcube_data.query import Query

    op_schema = OP_ADAPTER.json_schema()
    assert set(op_schema["discriminator"]["mapping"]) == set(OP_NAMES) and len(OP_NAMES) == 18
    assert "measures" in Query.model_json_schema()["properties"]


def test_catalog_schema_migrations_are_versioned(tmp_path):
    from shipcube_data.db import Db

    url = str(tmp_path / "c.db")
    with pytest.raises(ModelError) as e:
        Catalog(url, migrate=False)
    assert e.value.code == "catalog_not_migrated"
    assert Db(url).migrate_schema() == ["0001_initial.sql", "0002_migration_humans.sql", "0003_migration_decisions.sql", "0004_workspace_column.sql"]
    assert Db(url).migrate_schema() == []  # idempotent
    cat = Catalog(url, migrate=False)
    assert [r["name"] for r in cat.db.execute("SELECT name FROM catalog_schema_migrations ORDER BY version")] == [
        "0001_initial.sql", "0002_migration_humans.sql", "0003_migration_decisions.sql", "0004_workspace_column.sql"]
    cat.db.execute("UPDATE catalog_schema_migrations SET checksum = 'edited'")
    with pytest.raises(ModelError, match="changed after it was applied"):
        Db(url).migrate_schema()
