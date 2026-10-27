"""BigQuery adapter against real BigQuery. Runs only when SHIPCUBE_TEST_BQ_PROJECT is set (ADC credentials).

It creates two or three datasets named shipcube_it_<run>_*, loads ~1k synthetic rows with free load
jobs, runs the shared query suite and deletes the datasets. Datasets also expire after 24 hours.
"""

import os
import uuid

import pytest

from shipcube_data import SecurityContext, demo
from shipcube_data.engine import DataLayer
from shipcube_data.warehouses import BigQueryWarehouse, QueryLimits, WarehouseError

from .common import assert_same_answers, model_catalog, reference_layer

PROJECT = os.environ.get("SHIPCUBE_TEST_BQ_PROJECT")
LOCATION = os.environ.get("SHIPCUBE_TEST_BQ_LOCATION", "US")
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not PROJECT, reason="SHIPCUBE_TEST_BQ_PROJECT not set")]
TENANTS = {"acme": (60, 220, 7), "globex": (30, 90, 11)}


@pytest.fixture(scope="module")
def bq():
    from google.cloud import bigquery

    client = bigquery.Client(project=PROJECT, location=LOCATION)
    prefix = f"shipcube_it_{uuid.uuid4().hex[:8]}_"
    datasets = demo.build_bigquery(client, TENANTS, prefix=prefix, location=LOCATION)
    yield client, prefix
    for ds in datasets:
        client.delete_dataset(f"{PROJECT}.{ds}", delete_contents=True, not_found_ok=True)


def test_same_answers_as_reference(bq):
    client, prefix = bq
    layer = DataLayer(model_catalog("bigquery", prefix), BigQueryWarehouse(PROJECT, LOCATION, client=client))
    assert_same_answers(layer, reference_layer(TENANTS))


def test_stats_labels_and_pruning(bq):
    client, prefix = bq
    layer = DataLayer(model_catalog("bigquery", prefix), BigQueryWarehouse(PROJECT, LOCATION, client=client))
    res = layer.query({"measures": ["orders.count"], "time": {"dimension": "orders.purchased_at", "range": ["2018-03-01", "2018-03-31"]}},
                      SecurityContext(tenant="acme"), use_cache=False)
    assert res["partitions"]["orders"]["tables"] == ["orders_2018_03"]
    job = client.get_job(res["stats"]["job_id"], location=LOCATION)
    assert job.labels["shipcube_query_id"] == res["query_id"].lower()
    assert job.maximum_bytes_billed == layer.limits.max_bytes_billed


def test_cost_guard_uses_dry_run(bq):
    client, prefix = bq
    wh = BigQueryWarehouse(PROJECT, LOCATION, client=client)
    with pytest.raises(WarehouseError) as e:
        wh(f"SELECT * FROM `{PROJECT}.{prefix}tenant_acme.order_items`", QueryLimits(max_bytes_billed=10), "q_cost")
    assert e.value.code == "cost_limit"


def test_row_limit_truncates(bq):
    client, _ = bq
    res = BigQueryWarehouse(PROJECT, LOCATION, client=client)("SELECT x FROM UNNEST(GENERATE_ARRAY(1, 50)) x", QueryLimits(max_rows=5), "q_rows")
    assert len(res.rows) == 5 and res.truncated
