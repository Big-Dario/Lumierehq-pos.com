"""
db.py — SQLite schema + seed data for the Lumière POS backend.

Uses only the Python standard library (sqlite3). No external dependencies.
Seed data (branches, products, stock, historical sales) mirrors the
hardcoded arrays from the original front-end exactly, so the API behaves
identically to the in-browser demo on first run.
"""
import sqlite3
import os
import time

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lumiere.db")

BRANCHES = [
    ("westlands", "Westlands", "Westlands Branch", "Ring Road, Westlands"),
    ("cbd", "CBD Plaza", "CBD Plaza Branch", "Moi Avenue, CBD"),
    ("karen", "Karen", "Karen Branch", "Karen Shopping Centre"),
    ("gigiri", "Gigiri", "Gigiri Branch", "Village Market, Gigiri"),
]

# id, cat, brand, name, shade, color, icon, price, badge
PRODUCTS = [
    (1, "Skincare", "La Roche-Posay", "Effaclar Cleanser", "Unscented", "#B8D4E8", "🧴", 2800, ""),
    (2, "Skincare", "CeraVe", "Hydrating Moisturiser", "All Skin Types", "#D4EBF5", "🫙", 3200, "low"),
    (3, "Skincare", "The Ordinary", "Niacinamide 10%", "Serum", "#E8F4E8", "💧", 1850, "new"),
    (4, "Skincare", "Neutrogena", "Retinol Serum", "Night Use", "#F5E8D4", "✨", 4500, ""),
    (5, "Skincare", "Kiehl's", "Ultra Facial Cream", "24h Moisture", "#EAD5F5", "🫧", 6800, ""),
    (6, "Makeup", "MAC", "Lipstick", "Ruby Woo", "#C4283A", "💄", 3500, ""),
    (7, "Makeup", "Fenty Beauty", "Pro Filt'r Foundation", "240N", "#C8956A", "🏺", 5200, "new"),
    (8, "Makeup", "Charlotte Tilbury", "Flawless Filter", "3 Fair Medium", "#E8C4A0", "🌟", 7800, "sale"),
    (9, "Makeup", "NARS", "Blush", "Orgasm", "#E8A0A0", "🌸", 4200, ""),
    (10, "Makeup", "Urban Decay", "Naked Palette", "Neutral Smoky", "#B8936A", "🎨", 8900, "low"),
    (11, "Makeup", "Maybelline", "Sky High Mascara", "Blackest Black", "#2C2826", "👁", 1800, "sale"),
    (12, "Fragrance", "Chanel", "Coco Mademoiselle", "100ml EDP", "#F5D5A8", "🌸", 18500, ""),
    (13, "Fragrance", "Jo Malone", "Peony & Blush Suede", "30ml Cologne", "#F5C5D5", "💐", 12800, ""),
    (14, "Fragrance", "Dior", "Miss Dior", "50ml EDP", "#F0B8D4", "🌹", 14200, ""),
    (15, "Fragrance", "Yves Saint Laurent", "Black Opium", "90ml EDP", "#2C1A3A", "🌙", 16000, "new"),
    (16, "Hair", "Olaplex", "Bond Maintenance Shampoo", "No.4", "#E8E4D4", "🧪", 4800, ""),
    (17, "Hair", "Moroccanoil", "Treatment Oil", "125ml", "#F5D0A8", "💫", 5600, ""),
    (18, "Hair", "GHD", "Gold Styler", "Universal Voltage", "#B8A090", "⚡", 22000, "low"),
    (19, "Hair", "Dyson", "Airwrap Styler", "Fuchsia/Nickel", "#D45A8A", "🌀", 68000, ""),
    (20, "Tools", "Sigma Beauty", "F80 Kabuki Brush", "Flat Kabuki", "#C8B4A0", "🖌", 3800, ""),
    (21, "Tools", "Real Techniques", "Sponge Set", "3-Piece", "#F5C0B0", "🧽", 2200, "sale"),
    (22, "Tools", "Tweezerman", "Professional Tweezers", "Rose Gold", "#E8B0A8", "🔧", 2800, ""),
]

# product_id -> {branch_id: qty}
STOCK = {
    1: {"westlands": 12, "cbd": 8, "karen": 5, "gigiri": 15},
    2: {"westlands": 7, "cbd": 2, "karen": 11, "gigiri": 3},
    3: {"westlands": 20, "cbd": 14, "karen": 18, "gigiri": 22},
    4: {"westlands": 6, "cbd": 0, "karen": 9, "gigiri": 4},
    5: {"westlands": 3, "cbd": 5, "karen": 2, "gigiri": 7},
    6: {"westlands": 15, "cbd": 11, "karen": 8, "gigiri": 12},
    7: {"westlands": 9, "cbd": 6, "karen": 14, "gigiri": 8},
    8: {"westlands": 4, "cbd": 3, "karen": 6, "gigiri": 5},
    9: {"westlands": 11, "cbd": 7, "karen": 9, "gigiri": 10},
    10: {"westlands": 2, "cbd": 4, "karen": 3, "gigiri": 1},
    11: {"westlands": 22, "cbd": 19, "karen": 16, "gigiri": 24},
    12: {"westlands": 3, "cbd": 2, "karen": 4, "gigiri": 2},
    13: {"westlands": 5, "cbd": 3, "karen": 6, "gigiri": 4},
    14: {"westlands": 4, "cbd": 0, "karen": 3, "gigiri": 5},
    15: {"westlands": 6, "cbd": 4, "karen": 5, "gigiri": 7},
    16: {"westlands": 10, "cbd": 8, "karen": 12, "gigiri": 9},
    17: {"westlands": 7, "cbd": 5, "karen": 8, "gigiri": 6},
    18: {"westlands": 2, "cbd": 1, "karen": 3, "gigiri": 2},
    19: {"westlands": 1, "cbd": 0, "karen": 2, "gigiri": 1},
    20: {"westlands": 8, "cbd": 6, "karen": 10, "gigiri": 7},
    21: {"westlands": 14, "cbd": 11, "karen": 16, "gigiri": 13},
    22: {"westlands": 9, "cbd": 7, "karen": 11, "gigiri": 8},
}

# Seeded historical sales so analytics aren't empty on first run.
# (product_id, qty, branch_id, method, hour_of_day)
SEED_SALES = [
    (6, 3, "westlands", "cash", 8), (11, 5, "cbd", "mpesa", 9),
    (3, 4, "karen", "card", 9), (7, 2, "gigiri", "cash", 10),
    (1, 3, "westlands", "mpesa", 10), (12, 1, "cbd", "card", 11),
    (9, 4, "karen", "cash", 11), (6, 2, "gigiri", "mpesa", 12),
    (11, 6, "westlands", "cash", 12), (8, 1, "cbd", "card", 13),
    (3, 5, "karen", "mpesa", 13), (15, 2, "gigiri", "cash", 14),
    (16, 3, "westlands", "card", 14), (6, 4, "cbd", "cash", 15),
    (21, 7, "karen", "mpesa", 15), (13, 2, "gigiri", "card", 16),
    (9, 3, "westlands", "cash", 16), (11, 4, "cbd", "mpesa", 9),
    (7, 3, "karen", "card", 10), (17, 2, "gigiri", "cash", 11),
]

DISCOUNT_CODES = [
    ("LUMIERE10", 10),
    ("BEAUTY20", 20),
    ("VIP15", 15),
    ("WELCOME5", 5),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS branches (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    full_name TEXT NOT NULL,
    address TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    cat TEXT NOT NULL,
    brand TEXT NOT NULL,
    name TEXT NOT NULL,
    shade TEXT NOT NULL,
    color TEXT NOT NULL,
    icon TEXT NOT NULL,
    price REAL NOT NULL,
    badge TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS stock (
    product_id INTEGER NOT NULL REFERENCES products(id),
    branch_id TEXT NOT NULL REFERENCES branches(id),
    qty INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (product_id, branch_id)
);

CREATE TABLE IF NOT EXISTS discount_codes (
    code TEXT PRIMARY KEY,
    pct REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS shifts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_id TEXT NOT NULL REFERENCES branches(id),
    cashier TEXT NOT NULL,
    opened_at INTEGER NOT NULL,
    closed_at INTEGER,
    status TEXT NOT NULL DEFAULT 'open'  -- open | closed
);

CREATE TABLE IF NOT EXISTS shift_stock_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shift_id INTEGER NOT NULL REFERENCES shifts(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    kind TEXT NOT NULL,   -- opening | closing
    qty INTEGER NOT NULL,
    taken_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sales (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_ref TEXT NOT NULL,
    product_id INTEGER NOT NULL REFERENCES products(id),
    qty INTEGER NOT NULL,
    unit_price REAL NOT NULL,
    revenue REAL NOT NULL,
    branch_id TEXT NOT NULL REFERENCES branches(id),
    method TEXT NOT NULL,       -- cash | mpesa | card | credit
    discount_pct REAL NOT NULL DEFAULT 0,
    shift_id INTEGER REFERENCES shifts(id),
    occurred_at INTEGER NOT NULL,
    hour_of_day INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS stock_movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    branch_id TEXT NOT NULL REFERENCES branches(id),
    qty INTEGER NOT NULL,
    reference TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sales_branch ON sales(branch_id);
CREATE INDEX IF NOT EXISTS idx_sales_shift ON sales(shift_id);
CREATE INDEX IF NOT EXISTS idx_sales_product ON sales(product_id);
CREATE INDEX IF NOT EXISTS idx_stock_branch ON stock(branch_id);
"""


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(reset=False):
    """Create tables if missing and seed them if empty. If reset=True,
    drops and recreates everything from scratch."""
    if reset and os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    conn = get_conn()
    conn.executescript(SCHEMA)

    already_seeded = conn.execute("SELECT COUNT(*) AS c FROM products").fetchone()["c"] > 0
    if not already_seeded:
        conn.executemany(
            "INSERT INTO branches (id, name, full_name, address) VALUES (?, ?, ?, ?)",
            BRANCHES,
        )
        conn.executemany(
            "INSERT INTO products (id, cat, brand, name, shade, color, icon, price, badge) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            PRODUCTS,
        )
        stock_rows = [
            (pid, branch_id, qty)
            for pid, branch_map in STOCK.items()
            for branch_id, qty in branch_map.items()
        ]
        conn.executemany(
            "INSERT INTO stock (product_id, branch_id, qty) VALUES (?, ?, ?)",
            stock_rows,
        )
        conn.executemany(
            "INSERT INTO discount_codes (code, pct) VALUES (?, ?)",
            DISCOUNT_CODES,
        )

        now = int(time.time())
        price_by_id = {p[0]: p[7] for p in PRODUCTS}
        seed_rows = []
        for pid, qty, branch_id, method, hour in SEED_SALES:
            unit_price = price_by_id[pid]
            revenue = unit_price * qty
            txn_ref = "LUM-SEED%04d" % len(seed_rows)
            seed_rows.append(
                (txn_ref, pid, qty, unit_price, revenue, branch_id, method, 0, None, now, hour)
            )
        conn.executemany(
            "INSERT INTO sales (txn_ref, product_id, qty, unit_price, revenue, branch_id, "
            "method, discount_pct, shift_id, occurred_at, hour_of_day) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            seed_rows,
        )
        conn.commit()

    conn.close()


if __name__ == "__main__":
    init_db(reset=True)
    print(f"Database initialised at {DB_PATH}")
