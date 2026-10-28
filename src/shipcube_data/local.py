"""Running the data layer on ShipCube's local stack: seed, serve (as a library), smoke.

Everything is configured from the environment, the same variables in compose and in Cloud Run:

    SHIPCUBE_DATA_CATALOG_URL     catalog database, e.g. postgresql://postgres:postgres@postgres:5432/shipcube_data
    SHIPCUBE_DATA_WORKSPACE       the workspace uuid to seed (the MCP server mounts the tools for it)
    SHIPCUBE_DATA_WAREHOUSE       duckdb | postgres                               (default duckdb)
    SHIPCUBE_DATA_DUCKDB_PATH     DuckDB warehouse file                           (default .local/olist.duckdb)
    SHIPCUBE_DATA_WAREHOUSE_URL   Postgres warehouse to load and query (warehouse=postgres)
    SHIPCUBE_DATA_DEMO_TENANTS    google-domain=tenant pairs                       (default example.com=acme,globex.test=globex)
    SHIPCUBE_DATA_MIGRATIONS      model migrations to apply                        (default: the packaged Olist demo)
    SHIPCUBE_DEV_MODE=1           required for DuckDB and local secrets
    SHIPCUBE_DATA_ALLOWED_DB_HOSTS  private hosts the stack may use, e.g. postgres,localhost

`seed()` is idempotent: run it on every `make up`.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .catalog import Catalog
from .datasources import Datasource, SecretRef, register_datasource
from .engine import DataLayer
from .identity import Caller
from .migrate import Runner, load_dir
from .model import ModelError
from .obs import log_event
from .tools import DataTools, ToolError

DEFAULT_TENANTS = "example.com=acme,globex.test=globex"
SEED_ACTOR = "system:shipcube-data-seed"
SAVED_QUERIES = {  # what a deployed demo app would register; impact analysis checks these
    "orders_by_month": {"measures": ["orders.count", "order_items.revenue"],
                        "time": {"dimension": "orders.purchased_at", "granularity": "month", "range": ["2018-01-01", "2018-06-30"]}},
    "late_rate_by_region": {"measures": ["orders.late_rate"], "dimensions": ["states.region"]},
}


@dataclass
class Settings:
    catalog_url: str
    workspace: str
    warehouse: str = "duckdb"
    duckdb_path: str = ".local/olist.duckdb"
    warehouse_url: Optional[str] = None
    tenants: dict[str, str] = field(default_factory=dict)
    migrations: Optional[str] = None
    shared: bool = False  # the catalog is ShipCube's database, schema `catalog`, under RLS (production layout)

    @classmethod
    def from_env(cls) -> "Settings":
        catalog = os.environ.get("SHIPCUBE_DATA_CATALOG_URL")
        workspace = os.environ.get("SHIPCUBE_DATA_WORKSPACE") or os.environ.get("SHIPCUBE_DATA_WORKSPACE_ID")
        if not catalog or not workspace:
            raise ModelError("set SHIPCUBE_DATA_CATALOG_URL and SHIPCUBE_DATA_WORKSPACE", "invalid")
        uuid.UUID(workspace)  # must be a workspace id
        pairs = os.environ.get("SHIPCUBE_DATA_DEMO_TENANTS", DEFAULT_TENANTS)
        tenants = dict(p.strip().split("=", 1) for p in pairs.split(",") if "=" in p)
        return cls(
            catalog_url=catalog, workspace=workspace,
            warehouse=os.environ.get("SHIPCUBE_DATA_WAREHOUSE", "duckdb"),
            duckdb_path=os.environ.get("SHIPCUBE_DATA_DUCKDB_PATH", ".local/olist.duckdb"),
            warehouse_url=os.environ.get("SHIPCUBE_DATA_WAREHOUSE_URL"),
            tenants=tenants, migrations=os.environ.get("SHIPCUBE_DATA_MIGRATIONS"),
            shared=shared_layout(),
        )

    @property
    def secret_name(self) -> str:
        return f"ws-{self.workspace}-warehouse"


# ---------------------------------------------------------------- seed

def seed(s: Settings, force: bool = False) -> dict[str, Any]:
    """Load the demo warehouse, migrate the catalog, connect the datasource, apply the model.

    Idempotent. An existing DuckDB file is kept unless `force` (a running MCP server holds it open
    read-only, and DuckDB allows one writer); the Postgres warehouse is reloaded every time."""
    from . import demo
    from .datasources import dev_mode

    if dev_mode():  # the stack's Postgres starts empty; production databases are created by Terraform
        if s.catalog_url.startswith(("postgres://", "postgresql://")):
            ensure_database(s.catalog_url)
        if s.warehouse == "postgres" and s.warehouse_url:
            ensure_database(s.warehouse_url)

    if s.warehouse == "duckdb":
        import duckdb

        path = Path(s.duckdb_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if force or not path.exists():
            tmp = path.with_suffix(".tmp")
            tmp.unlink(missing_ok=True)
            con = duckdb.connect(str(tmp))
            demo.build(con)
            con.close()
            tmp.replace(path)
        ds = Datasource(name="primary", kind="duckdb", options={"path": str(Path(s.duckdb_path).resolve())})
    elif s.warehouse == "postgres":
        import psycopg

        if not s.warehouse_url:
            raise ModelError("SHIPCUBE_DATA_WAREHOUSE_URL is required for warehouse=postgres", "invalid")
        with psycopg.connect(s.warehouse_url, autocommit=True) as con:
            demo.build(con)
        ds = Datasource(name="primary", kind="postgres",
                        secret_ref=SecretRef(id=str(uuid.uuid5(uuid.NAMESPACE_URL, s.secret_name)),
                                             resource_name=f"projects/local/secrets/{s.secret_name}", version="1"))
    else:
        raise ModelError(f"unknown SHIPCUBE_DATA_WAREHOUSE {s.warehouse!r}", "invalid")

    cat = Catalog(s.catalog_url, dialect=ds.kind, workspace_id=s.workspace, shared=s.shared)
    try:
        register_datasource(cat, ds, SEED_ACTOR)
        runner = Runner(cat)
        files = load_dir(s.migrations) if s.migrations else load_dir(demo.MIGRATIONS)
        applied = [r["id"] for r in runner.apply_all(files, actor=SEED_ACTOR)]
        for domain, tenant in s.tenants.items():
            cat.set_attribute("domain", domain, "tenant", tenant, by=SEED_ACTOR)
        for name, q in SAVED_QUERIES.items():
            cat.save_query(name, q, app="olist-demo", tenant=next(iter(s.tenants.values()), None), created_by=SEED_ACTOR)
        out = {"workspace": s.workspace, "catalog_schema": cat.db.schema, "warehouse": ds.kind,
               "applied": applied, "model_version": cat.head(), "fingerprint": cat.load().fingerprint(),
               "tenants": s.tenants}
    finally:
        cat.close()
    log_event("seed.ok", **{k: v for k, v in out.items() if k != "tenants"})
    return out


def ensure_database(url: str) -> None:
    """CREATE DATABASE if it does not exist (local stack only)."""
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(url)
    name = params.get("dbname")
    if not name or name == "postgres":
        return
    with psycopg.connect(make_conninfo(**{**params, "dbname": "postgres"}), autocommit=True) as con:
        if not con.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            con.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            log_event("seed.database_created", database=name)


# ---------------------------------------------------------------- serving

def shared_layout() -> bool:
    """SHIPCUBE_DATA_CATALOG_LAYOUT=shared: ShipCube's database, schema `catalog`, FORCE RLS (production)."""
    layout = os.environ.get("SHIPCUBE_DATA_CATALOG_LAYOUT", "schema")
    if layout not in {"schema", "shared"}:
        raise ModelError(f"SHIPCUBE_DATA_CATALOG_LAYOUT must be schema or shared, not {layout!r}", "invalid")
    return layout == "shared"


class ExecutorCache:
    """Warehouse connections, shared by every request of a workspace and reopened when its
    datasource changes (the key includes the credential's pinned version)."""

    def __init__(self, resolver: Callable) -> None:
        self.resolver, self.cache, self.lock = resolver, {}, threading.Lock()

    def executor(self, catalog: Catalog) -> Callable:
        from .datasources import get_datasource, open_executor

        def run(sql: str, limits: Any, query_id: str):
            ds = get_datasource(catalog)
            ref = ds.secret_ref
            key = (catalog.ws, ds.kind, ref.resource_name if ref else None, ref.version if ref else None,
                   json.dumps(ds.options, sort_keys=True))
            with self.lock:
                ex = self.cache.get(key)
            if ex is None:
                ex = open_executor(catalog, self.resolver)
                with self.lock:
                    self.cache[key] = ex
            return ex(sql, limits, query_id)

        return run

    def forget(self, workspace_id: Any) -> None:
        with self.lock:
            for k in [k for k in self.cache if k[0] == str(workspace_id)]:
                del self.cache[k]


def tools_from_env(catalog_session: Optional[Callable[[Caller], Any]] = None, **kwargs: Any) -> DataTools:
    """The DataTools the MCP server and the API mount.

    With `catalog_session(caller) -> context manager of a DB-API connection`, every tool call runs
    in that host transaction (ShipCube's API: `db.tx(caller)`, whose RLS context is the caller's
    workspace) against the shared catalog. This is the production path: nothing here opens a
    catalog connection of its own or creates anything.

    Without it, the catalog is opened from SHIPCUBE_DATA_CATALOG_URL (SHIPCUBE_DATA_CATALOG_LAYOUT
    schema|shared), one connection per (thread, workspace), lazily. Workspaces without a catalog
    get catalog_not_migrated. Warehouse connections are cached per workspace and datasource either way.
    """
    from .secrets import resolver_from_env

    executors = ExecutorCache(resolver_from_env())

    if catalog_session is not None:
        @contextlib.contextmanager
        def session(caller: Caller):
            with catalog_session(caller) as conn:
                catalog = Catalog.on_connection(conn, str(caller.workspace_id))
                yield DataLayer(catalog, executors.executor(catalog))

        tools = DataTools(session=session, **kwargs)
        tools.forget_workspace = executors.forget  # type: ignore[attr-defined]
        return tools

    url = os.environ.get("SHIPCUBE_DATA_CATALOG_URL")
    if not url:
        raise ModelError("SHIPCUBE_DATA_CATALOG_URL is not set", "invalid")
    shared = shared_layout()
    cache: dict[tuple[int, str], DataLayer] = {}  # (thread, workspace): connections are per thread
    lock = threading.Lock()

    def layer_for(workspace_id: uuid.UUID) -> DataLayer:
        key = (threading.get_ident(), str(workspace_id))
        with lock:
            layer = cache.get(key)
        if layer is None:
            catalog = Catalog(url, workspace_id=key[1], migrate=False, shared=shared)
            layer = DataLayer(catalog, executors.executor(catalog))
            with lock:
                cache[key] = layer
        return layer

    def forget(workspace_id: uuid.UUID) -> None:
        """Drop a workspace's cached layers and warehouse connections (e.g. after its credentials change)."""
        executors.forget(workspace_id)
        with lock:  # don't close them: another thread may be mid-request; they're released when unreferenced
            for k in [k for k in cache if k[1] == str(workspace_id)]:
                del cache[k]

    tools = DataTools(layer_for, **kwargs)
    tools.forget_workspace = forget  # type: ignore[attr-defined]
    return tools


# ---------------------------------------------------------------- smoke

@dataclass
class Step:
    name: str
    ok: bool
    detail: str


def smoke(s: Settings, tools: Optional[DataTools] = None, say: Callable[[str], None] = print) -> list[Step]:
    """list cubes → describe → L0 query → search values → propose → agent cannot apply.

    Runs the tools exactly as the MCP server calls them, as an agent (PAT, developer role) whose
    Google domain is the first demo tenant's.
    """
    tools = tools or tools_from_env()
    domain = next(iter(s.tenants), "example.com")
    ws = uuid.UUID(s.workspace)
    agent = Caller(workspace_id=ws, via="mcp", email=f"smoke@{domain}", user_id=uuid.uuid5(uuid.NAMESPACE_URL, "smoke-user"),
                   membership_role="developer", token_id=uuid.uuid5(uuid.NAMESPACE_URL, "smoke-token"),
                   scopes=["data:read", "data:migrate"])
    steps: list[Step] = []

    def check(name: str, fn: Callable[[], str]) -> None:
        try:
            steps.append(Step(name, True, fn()))
        except (AssertionError, ToolError, ModelError) as e:
            detail = e.to_dict() if isinstance(e, ToolError) else str(e)
            steps.append(Step(name, False, str(detail)))
        say(f"{'ok  ' if steps[-1].ok else 'FAIL'} {name}: {steps[-1].detail}")

    def tool_names() -> str:
        names = sorted(spec["name"] for spec in tools.specs())
        assert names == ["data_describe_cube", "data_list_cubes", "data_propose_migration", "data_query", "data_search_values"], names
        return ", ".join(names)

    def list_cubes() -> str:
        cubes = tools.call("data_list_cubes", {}, agent)["cubes"]
        assert {"orders", "order_items", "customers", "states", "products"} <= {c["name"] for c in cubes}
        return f"{len(cubes)} cubes"

    def describe() -> str:
        d = tools.call("data_describe_cube", {"cube": "orders"}, agent)
        assert d["partitioned_by"] and any(m["path"] == "orders.late_rate" for m in d["measures"])
        return f"{len(d['measures'])} measures, {len(d['dimensions'])} dimensions, partitioned by {d['partitioned_by']['column']}"

    def query() -> str:
        out = tools.call("data_query", {"query": {
            "measures": ["orders.count", "order_items.revenue"], "dimensions": ["states.region"],
            "time": {"dimension": "orders.purchased_at", "range": ["2018-01-01", "2018-03-31"]}, "limit": 50}}, agent)
        assert out["rows"] and out["verified"], out
        scanned = out["partitions"]["orders"]["scanned"]
        assert scanned == 3, f"expected 3 monthly partitions, read {scanned}"
        return f"{len(out['rows'])} rows, {scanned}/20 partitions, query_id {out['query_id']}"

    def search() -> str:
        values = tools.call("data_search_values", {"dimension": "customers.city", "text": "sao"}, agent)["values"]
        assert "sao paulo" in values, values
        return ", ".join(map(str, values))

    proposal: dict[str, Any] = {}

    def propose() -> str:
        out = tools.call("data_propose_migration", {
            "description": "Smoke test: distinct ordering customers",
            "ops": [{"op": "add_measure", "name": f"orders.smoke_customers_{uuid.uuid4().hex[:6]}", "sql": "customer_id",
                     "agg": "count_distinct"}]}, agent)
        assert out["status"] == "proposed" and out["approval_url"], out
        proposal.update(out)
        return f"{out['id']} ({out['kind']}, widens {out['widens_exposure']}), impact: {len(out['impact']['breaks'])} breaks"

    def head() -> int:
        with tools.session(agent) as layer:
            return layer.catalog.head()

    def cannot_apply() -> str:
        before = head()
        for name in ("apply_migration", "data_apply_migration"):
            try:
                tools.call(name, {"id": proposal["id"]}, agent)
                raise AssertionError(f"{name} ran for an agent")
            except ToolError as e:
                assert e.code == "unknown_tool", e.to_dict()
        for action in (tools.apply, tools.approve):
            try:
                action(proposal["id"], agent)
                raise AssertionError(f"{action.__name__} ran for an agent token")
            except ToolError as e:
                assert e.code == "forbidden", e.to_dict()
        with tools.session(agent) as layer:
            cat = layer.catalog
            assert cat.head() == before and cat.migration(proposal["id"])["status"] == "proposed"
            Runner(cat).reject(proposal["id"], SEED_ACTOR, "smoke test cleanup")
        return "no apply tool; apply/approve refused for the agent token; model unchanged; proposal rejected"

    for name, fn in [("tools", tool_names), ("data_list_cubes", list_cubes), ("data_describe_cube", describe),
                     ("data_query (L0)", query), ("data_search_values", search), ("data_propose_migration", propose)]:
        check(name, fn)
    if proposal:
        check("agent cannot apply", cannot_apply)
    return steps
