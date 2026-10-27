"""Concurrent appliers on separate connections: they serialise on the catalog lock, and conflicts are detected."""

import threading
import uuid

import pytest

from shipcube_data import demo
from shipcube_data.catalog import Catalog
from shipcube_data.migrate import MigrationError, Runner, load_dir
from shipcube_data.ops import Migration

from .conftest import PG_URL


@pytest.fixture(params=["sqlite", "postgres"])
def opener(request, tmp_path):
    """Returns a function that opens a new connection to the same workspace catalog."""
    if request.param == "sqlite":
        url, ws = str(tmp_path / "catalog.db"), None
    else:
        if not PG_URL:
            pytest.skip("SHIPCUBE_TEST_PG_URL not set")
        url, ws = PG_URL, str(uuid.uuid4())
    first = Catalog(url, workspace_id=ws)
    Runner(first).apply_all(load_dir(demo.MIGRATIONS))
    opened = [first]

    def open_():
        c = Catalog(url, workspace_id=ws)
        opened.append(c)
        return c

    yield open_
    if request.param == "postgres":
        first.db.execute(f'DROP SCHEMA "{first.db.schema}" CASCADE')
    for c in opened:
        c.close()


def race(opener, migrations):
    """Apply each migration from its own thread and connection, all released at once."""
    barrier = threading.Barrier(len(migrations))
    results: dict[str, object] = {}

    def run(m):
        runner = Runner(opener())
        barrier.wait()
        try:
            results[m.id] = runner.apply(m, actor="test")
        except MigrationError as e:
            results[m.id] = e

    threads = [threading.Thread(target=run, args=(m,)) for m in migrations]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return results


def mig(mid, op):
    return Migration.model_validate({"id": mid, "base_version": 5, "ops": [op]})


def test_conflicting_concurrent_applies_one_wins(opener):
    results = race(opener, [
        mig("0006_a", {"op": "change_sql", "member": "order_items.revenue", "sql": "price * 1.0"}),
        mig("0007_b", {"op": "rename_member", "member": "order_items.revenue", "to": "sales"}),
    ])
    errors = [r for r in results.values() if isinstance(r, MigrationError)]
    applied = [r for r in results.values() if isinstance(r, dict)]
    assert len(applied) == 1 and len(errors) == 1
    assert errors[0].code == "conflict" and applied[0]["version"] == 6
    assert opener().head() == 6


def test_disjoint_concurrent_applies_both_land(opener):
    results = race(opener, [
        mig(f"{6 + i:04d}_m{i}", {"op": "add_measure", "name": f"orders.extra_{i}", "sql": "1"}) for i in range(4)
    ])
    assert all(isinstance(r, dict) and r["status"] == "applied" for r in results.values()), results
    assert sorted(r["version"] for r in results.values()) == [6, 7, 8, 9]
    model = opener().load()
    assert {f"extra_{i}" for i in range(4)} <= set(model.cubes["orders"].measures)


def test_same_migration_applied_twice_concurrently(opener):
    m = mig("0006_once", {"op": "add_measure", "name": "orders.once", "sql": "1"})
    barrier = threading.Barrier(2)
    out = []

    def run():
        r = Runner(opener())
        barrier.wait()
        out.append(r.apply(m, actor="test")["status"])

    threads = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert sorted(out) == ["already_applied", "applied"]
    assert opener().head() == 6


def test_concurrent_proposals_get_distinct_ids(opener):
    barrier = threading.Barrier(3)
    ids = []

    def run(i):
        r = Runner(opener())
        barrier.wait()
        ids.append(r.propose([{"op": "add_measure", "name": f"orders.p{i}", "sql": "1"}], f"Proposal {i}", proposed_by=f"token:{i}")["id"])

    threads = [threading.Thread(target=run, args=(i,)) for i in range(3)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert len(set(i.split("_")[0] for i in ids)) == 3
