"""Warehouse adapters with query guardrails.

Every adapter is a callable `(sql, limits, query_id) -> ExecResult` and enforces, per query:

* a **timeout** (DuckDB: interrupt; Postgres: `statement_timeout`; BigQuery: job timeout + cancel),
* a **row limit** (fetch at most `max_rows + 1`, report `truncated`),
* a **cost guard** (Postgres: planner cost from `EXPLAIN`; BigQuery: dry-run bytes and
  `maximum_bytes_billed`), and
* **read-only** execution (Postgres `READ ONLY` transactions; BigQuery and DuckDB connections
  should use read-only roles / files).

Failures raise `WarehouseError` with a stable code: `query_timeout`, `cost_limit`,
`warehouse_unavailable` or `warehouse_error`. Connection failures are retried with backoff
(queries are reads, so retrying is safe).
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from .model import ModelError
from .obs import log_event


class WarehouseError(ModelError):
    pass


@dataclass(frozen=True)
class QueryLimits:
    timeout_s: float = 30.0
    max_rows: int = 10_000
    max_bytes_billed: int = 10 * 1024**3  # BigQuery: 10 GiB per query
    max_cost: float = 5_000_000.0  # Postgres: planner total cost

    @classmethod
    def from_env(cls) -> "QueryLimits":
        d = cls()
        return cls(
            timeout_s=float(os.environ.get("SHIPCUBE_DATA_QUERY_TIMEOUT_S", d.timeout_s)),
            max_rows=int(os.environ.get("SHIPCUBE_DATA_MAX_ROWS", d.max_rows)),
            max_bytes_billed=int(os.environ.get("SHIPCUBE_DATA_MAX_BYTES_BILLED", d.max_bytes_billed)),
            max_cost=float(os.environ.get("SHIPCUBE_DATA_MAX_PG_COST", d.max_cost)),
        )


@dataclass
class ExecResult:
    columns: list[str]
    rows: list[tuple]
    truncated: bool = False
    stats: dict[str, Any] = field(default_factory=dict)


class Executor(Protocol):
    def __call__(self, sql: str, limits: QueryLimits, query_id: str) -> ExecResult: ...


def _retry(fn, attempts: int = 3, base_delay: float = 0.5, retry_on: tuple = (), what: str = "warehouse"):
    for i in range(attempts):
        try:
            return fn()
        except retry_on as e:
            if i == attempts - 1:
                raise WarehouseError(f"{what} unavailable after {attempts} attempts: {type(e).__name__}", "warehouse_unavailable") from None
            delay = base_delay * (2**i)
            log_event("warehouse.retry", attempt=i + 1, delay_s=delay, error=type(e).__name__, what=what)
            time.sleep(delay)


# ---------------------------------------------------------------- DuckDB (local dev and tests)

class DuckDBWarehouse:
    """DuckDB for local development and tests. Never a production datasource kind.

    The connection is sealed on construction: no file, HTTP or extension access, and the
    configuration is locked so a query cannot turn it back on. (Red-team issue #5.)
    """

    def __init__(self, con):
        self.con = con
        self._lock = threading.Lock()  # one DuckDB connection runs one query at a time
        seal_duckdb(con)

    def __call__(self, sql: str, limits: QueryLimits, query_id: str) -> ExecResult:
        import duckdb

        with self._lock:
            timer = threading.Timer(limits.timeout_s, self.con.interrupt)
            timer.start()
            try:
                cur = self.con.execute(sql)
                rows = cur.fetchmany(limits.max_rows + 1)
            except duckdb.InterruptException:
                raise WarehouseError(f"query exceeded {limits.timeout_s}s", "query_timeout") from None
            except duckdb.Error as e:
                raise WarehouseError(f"duckdb: {e}", "warehouse_error") from None
            finally:
                timer.cancel()
        return ExecResult([d[0] for d in cur.description], rows[: limits.max_rows], len(rows) > limits.max_rows)


def seal_duckdb(con) -> None:
    for stmt in ("SET enable_external_access = false", "SET autoinstall_known_extensions = false",
                 "SET autoload_known_extensions = false", "SET lock_configuration = true"):
        try:
            con.execute(stmt)
        except Exception as e:  # already locked by an earlier seal: fine; anything else is not
            if "lock_configuration" not in str(e) and "locked" not in str(e).lower():
                raise


# ---------------------------------------------------------------- Postgres (Cloud SQL)

class PostgresWarehouse:
    """Cloud SQL for PostgreSQL (or any Postgres). One connection, reconnected on failure.

    `conninfo` is a libpq URL/DSN. On Cloud Run use the Cloud SQL socket
    (`host=/cloudsql/<project>:<region>:<instance>`) or the instance's private IP.
    """

    def __init__(self, conninfo: str, application_name: str = "shipcube-data"):
        self.conninfo, self.application_name = conninfo, application_name
        self.conn = None
        self._lock = threading.Lock()

    def _connect(self):
        import psycopg

        if self.conn is None or self.conn.closed:
            self.conn = _retry(
                lambda: psycopg.connect(self.conninfo, connect_timeout=10, application_name=self.application_name, autocommit=True),
                retry_on=(psycopg.OperationalError,), what="postgres",
            )
        return self.conn

    def __call__(self, sql: str, limits: QueryLimits, query_id: str) -> ExecResult:
        import psycopg

        tagged = f"/* shipcube query_id={query_id} */ {sql}"
        with self._lock:
            for attempt in range(2):
                conn = self._connect()
                try:
                    with conn.transaction():
                        conn.execute("SET TRANSACTION READ ONLY")
                        conn.execute(f"SET LOCAL statement_timeout = {int(limits.timeout_s * 1000)}")
                        plan = conn.execute(f"EXPLAIN (FORMAT JSON) {sql}").fetchone()[0]
                        cost = float(plan[0]["Plan"]["Total Cost"])
                        if cost > limits.max_cost:
                            raise WarehouseError(f"estimated cost {cost:,.0f} exceeds the limit {limits.max_cost:,.0f}; narrow the time range or add filters", "cost_limit")
                        with conn.cursor(name=f"c_{query_id}") as cur:
                            cur.execute(tagged)
                            rows = cur.fetchmany(limits.max_rows + 1)
                            cols = [d[0] for d in cur.description]
                    return ExecResult(cols, rows[: limits.max_rows], len(rows) > limits.max_rows, {"planner_cost": cost})
                except psycopg.errors.QueryCanceled:
                    raise WarehouseError(f"query exceeded {limits.timeout_s}s", "query_timeout") from None
                except psycopg.OperationalError as e:
                    self.conn = None  # dropped connection: reconnect once, then give up
                    if attempt:
                        raise WarehouseError(f"postgres unavailable: {type(e).__name__}", "warehouse_unavailable") from None
                    log_event("warehouse.reconnect", query_id=query_id, error=type(e).__name__)
                except psycopg.Error as e:
                    raise WarehouseError(f"postgres: {e.diag.message_primary or type(e).__name__}", "warehouse_error") from None
        raise AssertionError("unreachable")


# ---------------------------------------------------------------- BigQuery

class BigQueryWarehouse:
    """BigQuery through google-cloud-bigquery. Credentials: a service-account key from Secret
    Manager, or impersonation of a service account in the customer's project (ShipCube's runtime
    identity only needs `iam.serviceAccountTokenCreator` on it). `client` is for tests."""

    def __init__(self, project: str, location: Optional[str] = None, credentials_json: Optional[str] = None,
                 impersonate: Optional[str] = None, client=None):
        if client is None:
            from google.cloud import bigquery

            scopes = ["https://www.googleapis.com/auth/bigquery"]
            if credentials_json:
                import json

                from google.oauth2 import service_account

                creds = service_account.Credentials.from_service_account_info(json.loads(credentials_json), scopes=scopes)
            elif impersonate:
                import google.auth
                from google.auth import impersonated_credentials

                source, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
                creds = impersonated_credentials.Credentials(source_credentials=source, target_principal=impersonate,
                                                             target_scopes=scopes, lifetime=900)
            else:
                raise WarehouseError("BigQuery needs a service-account key or a service account to impersonate", "forbidden")
            client = bigquery.Client(project=project, credentials=creds, location=location)
        self.client, self.location = client, location

    def __call__(self, sql: str, limits: QueryLimits, query_id: str) -> ExecResult:
        from concurrent.futures import TimeoutError as FutureTimeout

        from google.api_core import exceptions as gexc
        from google.cloud import bigquery

        labels = {"shipcube_query_id": query_id.lower()[:63]}
        try:
            dry = self.client.query(sql, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False, labels=labels),
                                    location=self.location, timeout=limits.timeout_s)
            estimate = dry.total_bytes_processed or 0
            if estimate > limits.max_bytes_billed:
                raise WarehouseError(f"query would scan {estimate:,} bytes, over the {limits.max_bytes_billed:,} byte limit; "
                                     "narrow the time range so fewer partitions are read", "cost_limit")
            job = self.client.query(
                sql,
                job_config=bigquery.QueryJobConfig(maximum_bytes_billed=limits.max_bytes_billed, labels=labels, use_query_cache=True),
                job_id_prefix="shipcube_", location=self.location, timeout=limits.timeout_s,
            )
            try:
                result = job.result(timeout=limits.timeout_s, max_results=limits.max_rows + 1)
            except FutureTimeout:
                job.cancel()
                raise WarehouseError(f"query exceeded {limits.timeout_s}s (job {job.job_id} cancelled)", "query_timeout") from None
            cols = [f.name for f in result.schema]
            rows = [tuple(r.values()) for r in result]
        except WarehouseError:
            raise
        except (gexc.ServiceUnavailable, gexc.InternalServerError, gexc.TooManyRequests) as e:
            raise WarehouseError(f"bigquery unavailable: {type(e).__name__}", "warehouse_unavailable") from None
        except gexc.GoogleAPICallError as e:
            msg = getattr(e, "message", str(e))
            code = "cost_limit" if "bytesBilledLimitExceeded" in str(e) else "warehouse_error"
            raise WarehouseError(f"bigquery: {msg}", code) from None
        return ExecResult(cols, rows[: limits.max_rows], len(rows) > limits.max_rows, {
            "job_id": job.job_id, "bytes_estimated": estimate, "bytes_processed": job.total_bytes_processed,
            "bytes_billed": job.total_bytes_billed, "cache_hit": job.cache_hit,
        })
