"""shipcube-data CLI.

  shipcube-data demo     --warehouse demo.duckdb
  shipcube-data status   --catalog catalog.db --dir examples/olist/migrations
  shipcube-data dry-run  --catalog catalog.db --dir examples/olist/migrations [ID]
  shipcube-data apply    --catalog catalog.db --dir examples/olist/migrations
  shipcube-data rollback --catalog catalog.db [--dry-run]
  shipcube-data query    --catalog catalog.db --warehouse demo.duckdb --tenant acme '{"measures": ["orders.count"]}'
  shipcube-data sql      --catalog catalog.db --tenant acme --dialect bigquery '{...}'
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


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="shipcube-data")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("status", "dry-run", "apply", "rollback", "query", "sql", "demo"):
        s = sub.add_parser(name)
        s.add_argument("--catalog", default="catalog.db")
        s.add_argument("--dir", default="examples/olist/migrations")
        s.add_argument("--warehouse", default="demo.duckdb")
        s.add_argument("--tenant", default="acme")
        s.add_argument("--roles", default="")
        s.add_argument("--attr", action="append", default=[], help="security context attribute key=value")
        s.add_argument("--dialect", default=None)
        s.add_argument("--dry-run", action="store_true")
        s.add_argument("--actor", default=os.environ.get("USER"))
        s.add_argument("arg", nargs="?")
    a = p.parse_args(argv)

    try:
        if a.cmd == "demo":
            import duckdb

            from . import demo

            con = duckdb.connect(a.warehouse)
            demo.build(con)
            print(f"built {a.warehouse}: schemas shared, tenant_acme, tenant_globex")
            return 0

        cat = Catalog(a.catalog)
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

                compiled, _ = DataLayer(cat, lambda s: ([], [])).compile(query, ctx, a.dialect)
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
