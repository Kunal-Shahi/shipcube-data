"""One real Muse Spark drafting call. Runs only when META_API_KEY is set."""

import os

import pytest

from shipcube_data.drafting import MuseDrafter
from shipcube_data.ops import OP_ADAPTER

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not os.environ.get("META_API_KEY"), reason="META_API_KEY not set")]


@pytest.mark.parametrize("catalog", ["sqlite"], indirect=True)
def test_muse_drafts_a_valid_migration(runner):
    d = MuseDrafter.from_env().draft(
        runner.catalog.load(), "duckdb",
        "We want to know how many distinct customers placed orders, per anything we can group orders by.", "")
    assert d.explained_by.startswith("muse-spark"), d.notes
    for op in d.ops:
        OP_ADAPTER.validate_python(op)
    # It should reuse the existing orders cube rather than invent a new one.
    assert any(op.get("op") == "add_measure" and str(op.get("name", "")).startswith("orders.") for op in d.ops), d.ops
    runner.propose(d.ops, d.description, proposed_by="token:test")
