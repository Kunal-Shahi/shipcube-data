# shipcube-data

ShipCube's data layer is a semantic layer whose models live in a database, not in YAML files.

You define base cubes, measures, dimensions and joins through versioned **model migrations**. Migrations are applied to and stored in a catalog database. Queries are typed and compile to SQL over a single warehouse database, organised by schema and partition.

Part of [ShipCube](https://github.com/Kunal-Shahi/shipcube).

## Docs

- [docs/DESIGN.md](docs/DESIGN.md) covers goals, how this compares to Cube, MetricFlow, Malloy and LookML, the catalog schema, query resolution (schema routing, partition pruning, row-level security) and caching by fingerprint.
- [docs/MIGRATIONS.md](docs/MIGRATIONS.md) covers the migration vocabulary, ordering, idempotency, rollback, conflict detection, dry-run with impact analysis, and how agents propose migrations that humans approve.
- [docs/QUERY.md](docs/QUERY.md) covers the query language (L0 typed, L1 operators, L2 stages, L3 raw SQL) and dialects.
- [docs/MCP.md](docs/MCP.md) covers the MCP tools exposed to coding agents.

## Quick start

You need Python 3.11 or later. With [uv](https://docs.astral.sh/uv/):

```bash
uv venv -p 3.12 .venv
```

```bash
uv pip install -p .venv -e '.[dev]'
```

```bash
.venv/bin/pytest
```

Or with plain pip:

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]' && .venv/bin/pytest
```

The tests build an Olist-shaped DuckDB warehouse in memory and apply the demo migrations in `examples/olist/migrations`. They then check compiled queries against hand-written SQL. The warehouse has:

- one database;
- schemas `tenant_acme`, `tenant_globex` and `shared`;
- `orders` split into monthly tables;
- `order_items` with a partition column.

## Try the CLI

```bash
.venv/bin/shipcube-data demo --warehouse demo.duckdb
```

```bash
.venv/bin/shipcube-data status --catalog catalog.db --dir examples/olist/migrations
```

```bash
.venv/bin/shipcube-data dry-run --catalog catalog.db --dir examples/olist/migrations
```

```bash
.venv/bin/shipcube-data apply --catalog catalog.db --dir examples/olist/migrations
```

```bash
.venv/bin/shipcube-data query --catalog catalog.db --warehouse demo.duckdb --tenant globex '{"measures": ["orders.count", "order_items.revenue"], "dimensions": ["states.region"], "time": {"dimension": "orders.purchased_at", "range": ["2018-01-01", "2018-03-31"]}}'
```

```bash
.venv/bin/shipcube-data sql --catalog catalog.db --tenant acme --roles regional_manager --attr region=South --dialect bigquery '{"measures": ["orders.count"], "time": {"dimension": "orders.purchased_at", "granularity": "month", "range": ["2018-01-01", "2018-02-28"]}}'
```

```bash
.venv/bin/shipcube-data rollback --catalog catalog.db --dry-run
```

## Layout

```
src/shipcube_data/
  model.py       in-memory semantic model, fingerprints
  ops.py         migration vocabulary (18 ops), inference, conflict keys
  catalog.py     catalog DB (SQLite here, Postgres in production), SCD2 versioning
  migrate.py     runner: status, dry-run + impact, apply, rollback, propose/approve
  query.py       typed query language (L0 + L1)
  compiler.py    query -> sqlglot AST -> SQL; routing, RLS, grants, fan-out, stitching
  partitions.py  partition pruning
  rawsql.py      L3 validator
  engine.py      DataLayer facade used by MCP tools; result cache
  demo.py        Olist-shaped DuckDB warehouse
examples/olist/migrations/   the demo model as migrations
tests/
```

## Data

The demo data is **synthetic**. It is generated deterministically with the table and column names of the public [Olist e-commerce dataset](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce). The real dataset needs a Kaggle login, and its CC BY-NC-SA license doesn't fit an Apache-2.0 repo.

## License

Apache-2.0
