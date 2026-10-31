"""shipcube-data CLI.

  shipcube-data demo     --warehouse demo.duckdb
  shipcube-data status   --catalog catalog.db --dir examples/olist/migrations
  shipcube-data dry-run  --catalog catalog.db --dir examples/olist/migrations [ID]
  shipcube-data apply    --catalog catalog.db --dir examples/olist/migrations
  shipcube-data rollback --catalog catalog.db [--dry-run]
  shipcube-data query    --catalog catalog.db --warehouse demo.duckdb --tenant acme '{"measures": ["orders.count"]}'
  shipcube-data sql      --catalog catalog.db --tenant acme --dialect bigquery '{...}'
  shipcube-data catalog-migrate --catalog postgresql://... --workspace <uuid>
  shipcube-data catalog-migrate --catalog postgresql://.../shipcube --shared   # production: as the migration role
  shipcube-data rollup-build --catalog ... --tenant acme --rollup order_items.daily --writer-url postgresql://... [--period 2018-01-01]
  shipcube-data seed  [--force]   # local stack, configured from env (see shipcube_data/local.py)
  shipcube-data smoke             # list cubes → describe → query → search → propose → agent cannot apply

`--catalog` is a SQLite path or a postgresql:// URL (default: $SHIPCUBE_DATA_CATALOG_URL, else catalog.db).
`--workspace` selects the workspace's catalog schema on Postgres (default: $SHIPCUBE_DATA_WORKSPACE_ID).
Logs are JSON lines on stderr; command output is on stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from .catalog import Catalog
from .compiler import SecurityContext
from .migrate import Runner, load_dir
from .model import ModelError
from .obs import configure_logging


def _no_warehouse(sql, limits, query_id):
    raise ModelError("compile-only: no warehouse connected", "no_datasource")


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="shipcube-data")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("seed", "smoke"):
        s = sub.add_parser(name, help="local stack: seed the demo warehouse and catalog / run the tool smoke test")
        s.add_argument("--force", action="store_true", help="seed: rebuild the DuckDB file even if it exists")
        s.add_argument("--mcp", action="store_true", help="smoke: go through a running MCP server (SHIPCUBE_MCP_URL, SHIPCUBE_PAT)")
    for name in ("status", "dry-run", "apply", "rollback", "query", "sql", "demo", "catalog-migrate", "rollup-build"):
        s = sub.add_parser(name)
        s.add_argument("--catalog", default=os.environ.get("SHIPCUBE_DATA_CATALOG_URL", "catalog.db"))
        s.add_argument("--workspace", default=os.environ.get("SHIPCUBE_DATA_WORKSPACE_ID"))
        s.add_argument("--dir", default="examples/olist/migrations")
        s.add_argument("--warehouse", default="demo.duckdb")
        s.add_argument("--tenant", default="acme")
        s.add_argument("--roles", default="")
        s.add_argument("--attr", action="append", default=[], help="security context attribute key=value")
        s.add_argument("--dialect", default=None)
        s.add_argument("--dry-run", action="store_true")
        s.add_argument("--shared", action="store_true", default=os.environ.get("SHIPCUBE_DATA_CATALOG_LAYOUT") == "shared",
                       help="the shared catalog in ShipCube's database (schema catalog, RLS); $SHIPCUBE_DATA_CATALOG_LAYOUT=shared")
        s.add_argument("--actor", default=os.environ.get("USER"))
        s.add_argument("--rollup", help="rollup-build: cube.rollup")
        s.add_argument("--writer-url", default=os.environ.get("SHIPCUBE_DATA_ROLLUP_WRITER_URL"),
                       help="rollup-build: a warehouse connection that may write the rollup schema (Postgres URL or DuckDB file)")
        s.add_argument("--period", action="append", default=[], help="rollup-build: a month to (re)build, YYYY-MM-01; default all")
        s.add_argument("--rollup-schema", default=None)
        s.add_argument("arg", nargs="?")
    a = p.parse_args(argv)
    configure_logging(os.environ.get("SHIPCUBE_DATA_LOG_LEVEL", "WARNING"), stream=sys.stderr)

    try:
        if a.cmd in {"seed", "smoke"}:
            from . import local

            if a.cmd == "smoke" and a.mcp:
                from . import mcp_smoke

                results = mcp_smoke.run()
                failed = [n for n, good, _ in results if not good]
                print(f"mcp smoke: {len(results) - len(failed)}/{len(results)} passed" + (f"; failed: {', '.join(failed)}" if failed else ""))
                return 1 if failed or len(results) < 7 else 0
            settings = local.Settings.from_env()
            if a.cmd == "seed":
                _print(local.seed(settings, force=a.force))
                return 0
            steps = local.smoke(settings)
            failed = [st.name for st in steps if not st.ok]
            print(f"smoke: {len(steps) - len(failed)}/{len(steps)} passed" + (f"; failed: {', '.join(failed)}" if failed else ""))
            return 1 if failed else 0
        if a.cmd == "demo":
            import duckdb

            from . import demo

            con = duckdb.connect(a.warehouse)
            demo.build(con)
            print(f"built {a.warehouse}: schemas shared, tenant_acme, tenant_globex")
            return 0

        if a.cmd == "catalog-migrate":
            from .db import Db, workspace_schema

            db = Db(a.catalog, shared=True) if a.shared else Db(a.catalog, schema=workspace_schema(a.workspace))
            applied = db.migrate_schema()
            print(f"catalog schema {db.schema or a.catalog}: applied {applied or 'nothing (up to date)'}")
            return 0
        cat = Catalog(a.catalog, workspace_id=a.workspace, shared=a.shared, migrate=not a.shared)
        if a.cmd == "rollup-build":
            from . import rollups
            from .engine import DataLayer

            if not (a.rollup and a.writer_url):
                raise ModelError("rollup-build needs --rollup cube.rollup and --writer-url", "invalid")
            if a.writer_url.startswith(("postgres://", "postgresql://")):
                writer = rollups.postgres_writer(a.writer_url)
            else:
                import duckdb

                writer = rollups.duckdb_writer(duckdb.connect(a.writer_url))
            _print(rollups.build(DataLayer(cat, _no_warehouse), a.tenant, a.rollup, writer, periods=a.period or None,
                                 schema=a.rollup_schema))
            return 0
        runner = Runner(cat)
        if a.cmd == "status":
            for row in runner.status(load_dir(a.dir)):
                print(f"{row['id']:<40} {row['state']:<12} v{row['version'] or '-':<4} {row['kind']}")
            print(f"head: v{cat.head()}  fingerprint: {cat.load().fingerprint()}")
        elif a.cmd == "dry-run":
            files = [m for m in load_dir(a.dir) if not a.arg or m.id == a.arg]
            for m in files:
                rec = cat.migration(m.id)
                if rec and rec["status"] == "applied":
                    continue
                _print(runner.dry_run(m, a.actor))
                break  # later files build on this one; dry-run one at a time
        elif a.cmd == "apply":
            for rep in runner.apply_all(load_dir(a.dir), a.actor):
                print(f"applied {rep['id']} -> v{rep['version']} ({rep['kind']})")
            print(f"head: v{cat.head()}")
        elif a.cmd == "rollback":
            _print(runner.rollback(a.actor, dry_run=a.dry_run))
        elif a.cmd in {"query", "sql"}:
            ctx = SecurityContext(tenant=a.tenant, roles=[r for r in a.roles.split(",") if r],
                                  attrs=dict(kv.split("=", 1) for kv in a.attr))
            query = json.loads(a.arg or sys.stdin.read())
            if a.cmd == "sql":
                from .engine import DataLayer

                compiled, _ = DataLayer(cat, _no_warehouse).compile(query, ctx, a.dialect)
                print(compiled.sql)
                for w in compiled.warnings:
                    print(f"-- warning: {w}", file=sys.stderr)
            else:
                import duckdb

                from .engine import DataLayer, duckdb_executor

                con = duckdb.connect(a.warehouse, read_only=True)
                res = DataLayer(cat, duckdb_executor(con)).query(query, ctx)
                _print({k: res[k] for k in ("rows", "warnings", "partitions", "model_version", "sql")})
        return 0
    except ModelError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
