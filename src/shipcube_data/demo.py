"""A small Olist-shaped warehouse in DuckDB: one database, two tenant schemas, monthly partitions.

The real Olist dataset (Kaggle) needs a login and is licensed CC BY-NC-SA, so we
generate deterministic synthetic rows with the same table and column names
instead. Layout:

  shared.states                          reference data (27 Brazilian states)
  tenant_<t>.customers                   one table
  tenant_<t>.products                    one table
  tenant_<t>.orders_YYYY_MM              one table per month, 2017-01 .. 2018-08
  tenant_<t>.order_items                 one table, partition column shipping_limit_date
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from decimal import Decimal
from datetime import datetime, timedelta
from pathlib import Path

STATES = [
    ("AC", "Acre", "North"), ("AL", "Alagoas", "Northeast"), ("AP", "Amapá", "North"), ("AM", "Amazonas", "North"),
    ("BA", "Bahia", "Northeast"), ("CE", "Ceará", "Northeast"), ("DF", "Distrito Federal", "Center-West"),
    ("ES", "Espírito Santo", "Southeast"), ("GO", "Goiás", "Center-West"), ("MA", "Maranhão", "Northeast"),
    ("MT", "Mato Grosso", "Center-West"), ("MS", "Mato Grosso do Sul", "Center-West"), ("MG", "Minas Gerais", "Southeast"),
    ("PA", "Pará", "North"), ("PB", "Paraíba", "Northeast"), ("PR", "Paraná", "South"), ("PE", "Pernambuco", "Northeast"),
    ("PI", "Piauí", "Northeast"), ("RJ", "Rio de Janeiro", "Southeast"), ("RN", "Rio Grande do Norte", "Northeast"),
    ("RS", "Rio Grande do Sul", "South"), ("RO", "Rondônia", "North"), ("RR", "Roraima", "North"),
    ("SC", "Santa Catarina", "South"), ("SP", "São Paulo", "Southeast"), ("SE", "Sergipe", "Northeast"), ("TO", "Tocantins", "North"),
]
BUSY_STATES = ["SP", "SP", "SP", "RJ", "RJ", "MG", "MG", "RS", "PR", "SC", "BA", "DF", "GO", "PE", "CE"]
CITIES = {"SP": ["sao paulo", "campinas", "santos"], "RJ": ["rio de janeiro", "niteroi"], "MG": ["belo horizonte", "uberlandia"]}
CATEGORIES = ["bed_bath_table", "health_beauty", "sports_leisure", "furniture_decor", "computers_accessories",
              "housewares", "watches_gifts", "telephony", "garden_tools", "auto", "toys", "cool_stuff"]
STATUSES = ["delivered"] * 18 + ["shipped", "canceled"]
TENANTS = {"acme": (400, 1500, 7), "globex": (150, 500, 11)}  # customers, orders, seed
MONTHS = [(2017, m) for m in range(1, 13)] + [(2018, m) for m in range(1, 9)]


def _uid(rng: random.Random) -> str:
    return "%032x" % rng.getrandbits(128)


@dataclass
class Table:
    schema: str
    name: str
    columns: list[tuple[str, str]]  # (name, type): VARCHAR | INTEGER | TIMESTAMP | DECIMAL
    rows: list[tuple] = field(default_factory=list)


CUSTOMERS = [("customer_id", "VARCHAR"), ("customer_unique_id", "VARCHAR"), ("customer_city", "VARCHAR"), ("customer_state", "VARCHAR")]
PRODUCTS = [("product_id", "VARCHAR"), ("product_category_name", "VARCHAR")]
ITEMS = [("order_id", "VARCHAR"), ("order_item_id", "INTEGER"), ("product_id", "VARCHAR"), ("seller_id", "VARCHAR"),
         ("shipping_limit_date", "TIMESTAMP"), ("price", "DECIMAL"), ("freight_value", "DECIMAL")]
ORDERS = [("order_id", "VARCHAR"), ("customer_id", "VARCHAR"), ("order_status", "VARCHAR"), ("order_purchase_timestamp", "TIMESTAMP"),
          ("order_delivered_customer_date", "TIMESTAMP"), ("order_estimated_delivery_date", "TIMESTAMP")]


def generate(tenants: dict[str, tuple[int, int, int]] = TENANTS, prefix: str = "") -> list[Table]:
    """The whole warehouse as tables of rows, deterministic for a given `tenants`.

    `prefix` namespaces the schemas (e.g. per integration-test run): `<prefix>shared`, `<prefix>tenant_<t>`.
    """
    out = [Table(f"{prefix}shared", "states", [("state_code", "VARCHAR"), ("state_name", "VARCHAR"), ("region", "VARCHAR")], list(STATES))]
    for tenant, (n_customers, n_orders, seed) in tenants.items():
        rng = random.Random(seed)
        s = f"{prefix}tenant_{tenant}"
        uniques = [_uid(rng) for _ in range(int(n_customers * 0.9))]
        customers = []
        for _ in range(n_customers):
            st = rng.choice(BUSY_STATES)
            customers.append((_uid(rng), rng.choice(uniques), rng.choice(CITIES.get(st, [f"capital {st.lower()}"])), st))
        products = [(_uid(rng), rng.choice(CATEGORIES)) for _ in range(80)]
        sellers = [_uid(rng) for _ in range(25)]
        start, span = datetime(2017, 1, 1), (datetime(2018, 9, 1) - datetime(2017, 1, 1)).total_seconds()
        by_month: dict[tuple[int, int], list[tuple]] = {m: [] for m in MONTHS}
        items = []
        for _ in range(n_orders):
            oid = _uid(rng)
            ts = start + timedelta(seconds=int(rng.random() ** 0.8 * span))  # more orders later, like a growing shop
            status = rng.choice(STATUSES)
            est = ts + timedelta(days=rng.randint(10, 30))
            delivered = ts + timedelta(days=rng.randint(3, 40)) if status == "delivered" else None
            by_month[(ts.year, ts.month)].append((oid, rng.choice(customers)[0], status, ts, delivered, est))
            for i in range(1, rng.choice([1, 1, 1, 2, 2, 3]) + 1):
                price = Decimal(str(round(rng.uniform(10, 400), 2)))
                items.append((oid, i, rng.choice(products)[0], rng.choice(sellers), ts + timedelta(days=rng.randint(1, 7)),
                              price, (price * Decimal(str(round(rng.uniform(0.05, 0.25), 4)))).quantize(Decimal("0.01"))))
        out += [Table(s, "customers", CUSTOMERS, customers), Table(s, "products", PRODUCTS, products),
                Table(s, "order_items", ITEMS, items)]
        out += [Table(s, f"orders_{y:04d}_{m:02d}", ORDERS, rows) for (y, m), rows in by_month.items()]
    return out


def _sql_type(t: str, dialect: str) -> str:
    if t == "DECIMAL":
        return "NUMERIC" if dialect == "bigquery" else "DECIMAL(10,2)"
    if dialect == "bigquery":
        return {"VARCHAR": "STRING", "INTEGER": "INT64"}.get(t, t)
    return t


def build(con, tenants: dict[str, tuple[int, int, int]] = TENANTS, prefix: str = "") -> None:
    """Load into DuckDB (a duckdb connection) or Postgres (a psycopg connection)."""
    pg = type(con).__module__.startswith("psycopg")
    ph = "%s" if pg else "?"
    schemas = set()
    for t in generate(tenants, prefix):
        if t.schema not in schemas:
            con.execute(f"DROP SCHEMA IF EXISTS {t.schema} CASCADE" if pg else f"CREATE SCHEMA IF NOT EXISTS {t.schema}")
            if pg:
                con.execute(f"CREATE SCHEMA {t.schema}")
            schemas.add(t.schema)
        cols = ", ".join(f"{c} {_sql_type(ty, 'postgres' if pg else 'duckdb')}" for c, ty in t.columns)
        con.execute(f"CREATE OR REPLACE TABLE {t.schema}.{t.name} ({cols})" if not pg else f"CREATE TABLE {t.schema}.{t.name} ({cols})")
        if t.rows:
            sql = f"INSERT INTO {t.schema}.{t.name} VALUES ({', '.join([ph] * len(t.columns))})"
            if pg:
                with con.cursor() as cur:
                    cur.executemany(sql, t.rows)
            else:
                con.executemany(sql, t.rows)


def build_bigquery(client, tenants: dict[str, tuple[int, int, int]] = TENANTS, prefix: str = "", location: str = "US") -> list[str]:
    """Load into BigQuery datasets `<prefix>shared` / `<prefix>tenant_<t>` with load jobs (free). Returns dataset ids."""
    from google.cloud import bigquery

    tables = generate(tenants, prefix)
    datasets = sorted({t.schema for t in tables})
    for ds in datasets:
        d = bigquery.Dataset(f"{client.project}.{ds}")
        d.location = location
        d.default_table_expiration_ms = 24 * 3600 * 1000  # safety net if cleanup is skipped
        client.create_dataset(d, exists_ok=True)
    jobs = []
    for t in tables:
        schema = [bigquery.SchemaField(c, _sql_type(ty, "bigquery")) for c, ty in t.columns]
        rows = [{c: (v.isoformat() if isinstance(v, datetime) else str(v) if isinstance(v, Decimal) else v)
                 for (c, _), v in zip(t.columns, r)} for r in t.rows]
        cfg = bigquery.LoadJobConfig(schema=schema, write_disposition="WRITE_TRUNCATE")
        jobs.append(client.load_table_from_json(rows, f"{client.project}.{t.schema}.{t.name}", job_config=cfg, location=location))
    for j in jobs:
        j.result(timeout=300)
    return datasets


_PACKAGED = Path(__file__).resolve().parent / "examples" / "olist" / "migrations"  # installed wheel
MIGRATIONS = _PACKAGED if _PACKAGED.is_dir() else Path(__file__).resolve().parents[2] / "examples" / "olist" / "migrations"
