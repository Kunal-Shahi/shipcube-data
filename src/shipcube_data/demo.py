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


def build(con, tenants: dict[str, tuple[int, int, int]] = TENANTS) -> None:
    con.execute("CREATE SCHEMA IF NOT EXISTS shared")
    con.execute("CREATE OR REPLACE TABLE shared.states (state_code VARCHAR, state_name VARCHAR, region VARCHAR)")
    con.executemany("INSERT INTO shared.states VALUES (?, ?, ?)", STATES)
    for tenant, (n_customers, n_orders, seed) in tenants.items():
        rng = random.Random(seed)
        s = f"tenant_{tenant}"
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {s}")
        con.execute(f"CREATE OR REPLACE TABLE {s}.customers (customer_id VARCHAR, customer_unique_id VARCHAR, customer_city VARCHAR, customer_state VARCHAR)")
        con.execute(f"CREATE OR REPLACE TABLE {s}.products (product_id VARCHAR, product_category_name VARCHAR)")
        con.execute(f"CREATE OR REPLACE TABLE {s}.order_items (order_id VARCHAR, order_item_id INTEGER, product_id VARCHAR, seller_id VARCHAR, shipping_limit_date TIMESTAMP, price DECIMAL(10,2), freight_value DECIMAL(10,2))")
        for y, m in MONTHS:
            con.execute(
                f"CREATE OR REPLACE TABLE {s}.orders_{y:04d}_{m:02d} (order_id VARCHAR, customer_id VARCHAR, order_status VARCHAR, "
                "order_purchase_timestamp TIMESTAMP, order_delivered_customer_date TIMESTAMP, order_estimated_delivery_date TIMESTAMP)"
            )

        uniques = [_uid(rng) for _ in range(int(n_customers * 0.9))]
        customers = []
        for _ in range(n_customers):
            st = rng.choice(BUSY_STATES)
            customers.append((_uid(rng), rng.choice(uniques), rng.choice(CITIES.get(st, [f"capital {st.lower()}"])), st))
        con.executemany(f"INSERT INTO {s}.customers VALUES (?, ?, ?, ?)", customers)
        products = [(_uid(rng), rng.choice(CATEGORIES)) for _ in range(80)]
        con.executemany(f"INSERT INTO {s}.products VALUES (?, ?)", products)
        sellers = [_uid(rng) for _ in range(25)]

        start, span = datetime(2017, 1, 1), (datetime(2018, 9, 1) - datetime(2017, 1, 1)).total_seconds()
        by_month: dict[tuple[int, int], list[tuple]] = {}
        items = []
        for _ in range(n_orders):
            oid = _uid(rng)
            ts = start + timedelta(seconds=int(rng.random() ** 0.8 * span))  # more orders later, like a growing shop
            status = rng.choice(STATUSES)
            est = ts + timedelta(days=rng.randint(10, 30))
            delivered = ts + timedelta(days=rng.randint(3, 40)) if status == "delivered" else None
            by_month.setdefault((ts.year, ts.month), []).append((oid, rng.choice(customers)[0], status, ts, delivered, est))
            for i in range(1, rng.choice([1, 1, 1, 2, 2, 3]) + 1):
                price = round(rng.uniform(10, 400), 2)
                items.append((oid, i, rng.choice(products)[0], rng.choice(sellers), ts + timedelta(days=rng.randint(1, 7)),
                              price, round(price * rng.uniform(0.05, 0.25), 2)))
        for (y, m), rows in by_month.items():
            con.executemany(f"INSERT INTO {s}.orders_{y:04d}_{m:02d} VALUES (?, ?, ?, ?, ?, ?)", rows)
        con.executemany(f"INSERT INTO {s}.order_items VALUES (?, ?, ?, ?, ?, ?, ?)", items)


MIGRATIONS = Path(__file__).resolve().parents[2] / "examples" / "olist" / "migrations"
