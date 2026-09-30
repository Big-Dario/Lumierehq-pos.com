"""
app.py — Lumière POS backend API.

Pure standard library (http.server + sqlite3). No pip install required.

Run:
    python3 app.py            # starts on http://localhost:8787
    python3 app.py --reset    # wipes and reseeds the database first

Every endpoint is documented in README.md.
"""
import json
import re
import sys
import time
import random
import string
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import db

PORT = 8787
VAT_RATE = 0.16


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def now_ts():
    return int(time.time())


def gen_txn_ref():
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
    return f"LUM-{suffix}"


def row_to_dict(row):
    return dict(row) if row is not None else None


def product_row_to_dict(row, stock_by_branch=None):
    d = {
        "id": row["id"],
        "cat": row["cat"],
        "brand": row["brand"],
        "name": row["name"],
        "shade": row["shade"],
        "color": row["color"],
        "icon": row["icon"],
        "price": row["price"],
        "badge": row["badge"],
    }
    if stock_by_branch is not None:
        d["stock"] = stock_by_branch
    return d


def fetch_stock_map(conn, product_id):
    rows = conn.execute(
        "SELECT branch_id, qty FROM stock WHERE product_id = ?", (product_id,)
    ).fetchall()
    return {r["branch_id"]: r["qty"] for r in rows}


def compute_totals(subtotal, discount_pct):
    discount_amt = subtotal * (discount_pct / 100.0)
    after_discount = subtotal - discount_amt
    vat = after_discount * VAT_RATE
    total = after_discount + vat
    return {
        "subtotal": round(subtotal, 2),
        "discountPct": discount_pct,
        "discountAmt": round(discount_amt, 2),
        "vat": round(vat, 2),
        "total": round(total, 2),
    }


def get_open_shift(conn, branch_id):
    return conn.execute(
        "SELECT * FROM shifts WHERE branch_id = ? AND status = 'open' "
        "ORDER BY opened_at DESC LIMIT 1",
        (branch_id,),
    ).fetchone()


# ─────────────────────────────────────────────────────────────────────────
# Route handlers — each takes (conn, params, query, body) and returns
# (status_code, response_dict)
# ─────────────────────────────────────────────────────────────────────────

def h_list_branches(conn, params, query, body):
    rows = conn.execute("SELECT * FROM branches ORDER BY name").fetchall()
    return 200, [row_to_dict(r) for r in rows]


def h_list_categories(conn, params, query, body):
    rows = conn.execute("SELECT DISTINCT cat FROM products ORDER BY cat").fetchall()
    return 200, ["All"] + [r["cat"] for r in rows]


def h_list_products(conn, params, query, body):
    branch = query.get("branch", [None])[0]
    cat = query.get("category", [None])[0]
    q = (query.get("q", [None])[0] or "").lower().strip()

    sql = "SELECT * FROM products WHERE 1=1"
    args = []
    if cat and cat != "All":
        sql += " AND cat = ?"
        args.append(cat)
    sql += " ORDER BY id"
    rows = conn.execute(sql, args).fetchall()

    out = []
    for r in rows:
        if q and q not in f"{r['name']}{r['brand']}{r['shade']}{r['cat']}".lower():
            continue
        stock_map = fetch_stock_map(conn, r["id"])
        item = product_row_to_dict(r, stock_map)
        if branch:
            item["branchStock"] = stock_map.get(branch, 0)
        out.append(item)
    return 200, out


def h_get_product(conn, params, query, body):
    pid = int(params["id"])
    row = conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
    if not row:
        raise ApiError(404, "Product not found")
    stock_map = fetch_stock_map(conn, pid)
    return 200, product_row_to_dict(row, stock_map)


def h_stock_levels(conn, params, query, body):
    """Powers the Stock Levels panel: per-product, per-branch quantities."""
    products = conn.execute("SELECT * FROM products ORDER BY id").fetchall()
    branches = conn.execute("SELECT id FROM branches ORDER BY id").fetchall()
    branch_ids = [b["id"] for b in branches]

    out = []
    total_units = 0
    out_count = 0
    low_count = 0
    total_value = 0.0
    for p in products:
        stock_map = fetch_stock_map(conn, p["id"])
        row_total = sum(stock_map.get(b, 0) for b in branch_ids)
        total_units += row_total
        total_value += row_total * p["price"]
        if any(stock_map.get(b, 0) == 0 for b in branch_ids):
            out_count += 1
        if any(0 < stock_map.get(b, 0) < 5 for b in branch_ids):
            low_count += 1
        out.append({
            "id": p["id"], "name": p["name"], "brand": p["brand"], "cat": p["cat"],
            "stock": stock_map, "total": row_total,
        })

    return 200, {
        "summary": {
            "totalUnits": total_units,
            "stockValue": round(total_value, 2),
            "lowStockItems": low_count,
            "outOfStock": out_count,
        },
        "products": out,
    }


def h_add_stock(conn, params, query, body):
    branch_id = body.get("branchId")
    product_id = body.get("productId")
    qty = body.get("qty")
    reference = (body.get("reference") or "Manual entry").strip()

    if not branch_id or not conn.execute(
        "SELECT 1 FROM branches WHERE id = ?", (branch_id,)
    ).fetchone():
        raise ApiError(400, "Unknown branchId")
    if not isinstance(qty, (int, float)) or qty < 1:
        raise ApiError(400, "qty must be a positive number")
    product = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if not product:
        raise ApiError(400, "Unknown productId")

    qty = int(qty)
    conn.execute(
        "UPDATE stock SET qty = qty + ? WHERE product_id = ? AND branch_id = ?",
        (qty, product_id, branch_id),
    )
    ts = now_ts()
    conn.execute(
        "INSERT INTO stock_movements (product_id, branch_id, qty, reference, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (product_id, branch_id, qty, reference, ts),
    )
    conn.commit()

    new_qty = conn.execute(
        "SELECT qty FROM stock WHERE product_id = ? AND branch_id = ?",
        (product_id, branch_id),
    ).fetchone()["qty"]

    return 200, {
        "ok": True,
        "productId": product_id,
        "branchId": branch_id,
        "added": qty,
        "newQty": new_qty,
        "reference": reference,
    }


def h_stock_movements(conn, params, query, body):
    limit = int(query.get("limit", [50])[0])
    rows = conn.execute(
        "SELECT m.*, p.name AS product_name, b.name AS branch_name "
        "FROM stock_movements m "
        "JOIN products p ON p.id = m.product_id "
        "JOIN branches b ON b.id = m.branch_id "
        "ORDER BY m.created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return 200, [
        {
            "id": r["id"], "productId": r["product_id"], "productName": r["product_name"],
            "branchId": r["branch_id"], "branchName": r["branch_name"],
            "qty": r["qty"], "reference": r["reference"], "createdAt": r["created_at"],
        }
        for r in rows
    ]


def h_validate_discount(conn, params, query, body):
    code = (body.get("code") or "").strip().upper()
    row = conn.execute(
        "SELECT pct FROM discount_codes WHERE code = ?", (code,)
    ).fetchone()
    if row:
        return 200, {"valid": True, "pct": row["pct"]}
    # allow a raw numeric percentage too, mirroring the frontend behaviour
    try:
        num = float(code)
        if 0 < num <= 100:
            return 200, {"valid": True, "pct": num}
    except ValueError:
        pass
    return 200, {"valid": False, "pct": 0}


def h_quote(conn, params, query, body):
    """Optional helper: compute pricing without committing anything."""
    items = body.get("items") or []
    discount_pct = float(body.get("discountPct") or 0)
    subtotal = 0.0
    line_items = []
    for it in items:
        product = conn.execute(
            "SELECT * FROM products WHERE id = ?", (it["productId"],)
        ).fetchone()
        if not product:
            raise ApiError(400, f"Unknown productId {it['productId']}")
        qty = int(it["qty"])
        line_total = product["price"] * qty
        subtotal += line_total
        line_items.append({
            "productId": product["id"], "name": product["name"], "qty": qty,
            "unitPrice": product["price"], "lineTotal": line_total,
        })
    totals = compute_totals(subtotal, discount_pct)
    totals["items"] = line_items
    return 200, totals


def h_checkout(conn, params, query, body):
    branch_id = body.get("branchId")
    items = body.get("items") or []
    discount_pct = float(body.get("discountPct") or 0)
    pay_method = body.get("payMethod") or "cash"
    cash_tendered = body.get("cashTendered")
    cashier = body.get("cashier") or "Unknown"

    if not branch_id or not conn.execute(
        "SELECT 1 FROM branches WHERE id = ?", (branch_id,)
    ).fetchone():
        raise ApiError(400, "Unknown branchId")
    if not items:
        raise ApiError(400, "Cart is empty")
    if pay_method not in ("cash", "mpesa", "card", "credit"):
        raise ApiError(400, "Invalid payMethod")

    # Validate stock + build line items
    subtotal = 0.0
    resolved = []
    for it in items:
        pid = it.get("productId")
        qty = int(it.get("qty", 0))
        if qty < 1:
            raise ApiError(400, "Each item needs qty >= 1")
        product = conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
        if not product:
            raise ApiError(400, f"Unknown productId {pid}")
        stock_row = conn.execute(
            "SELECT qty FROM stock WHERE product_id = ? AND branch_id = ?",
            (pid, branch_id),
        ).fetchone()
        available = stock_row["qty"] if stock_row else 0
        if qty > available:
            raise ApiError(
                409,
                f"Only {available} unit(s) of '{product['name']}' left at this branch",
            )
        line_total = product["price"] * qty
        subtotal += line_total
        resolved.append({"product": product, "qty": qty, "lineTotal": line_total})

    totals = compute_totals(subtotal, discount_pct)
    total = totals["total"]

    if pay_method == "cash":
        tendered = float(cash_tendered) if cash_tendered not in (None, "") else total
        if tendered > 0 and tendered < total:
            raise ApiError(400, "Cash tendered is less than total")
    else:
        tendered = total
    change = max(0.0, tendered - total) if pay_method == "cash" else 0.0

    shift = get_open_shift(conn, branch_id)
    shift_id = shift["id"] if shift else None

    txn_ref = gen_txn_ref()
    ts = now_ts()
    hour = time.localtime(ts).tm_hour

    for line in resolved:
        pid = line["product"]["id"]
        qty = line["qty"]
        conn.execute(
            "UPDATE stock SET qty = MAX(0, qty - ?) WHERE product_id = ? AND branch_id = ?",
            (qty, pid, branch_id),
        )
        conn.execute(
            "INSERT INTO sales (txn_ref, product_id, qty, unit_price, revenue, branch_id, "
            "method, discount_pct, shift_id, occurred_at, hour_of_day) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                txn_ref, pid, qty, line["product"]["price"], line["lineTotal"],
                branch_id, pay_method, discount_pct, shift_id, ts, hour,
            ),
        )
    conn.commit()

    receipt = {
        "txnRef": txn_ref,
        "branchId": branch_id,
        "occurredAt": ts,
        "cashier": cashier,
        "payMethod": pay_method,
        "items": [
            {
                "productId": l["product"]["id"], "name": l["product"]["name"],
                "brand": l["product"]["brand"], "qty": l["qty"],
                "unitPrice": l["product"]["price"], "lineTotal": l["lineTotal"],
            }
            for l in resolved
        ],
        **totals,
        "tendered": round(tendered, 2),
        "change": round(change, 2),
    }
    return 200, receipt


def h_sales_log(conn, params, query, body):
    branch = query.get("branch", [None])[0]
    limit = int(query.get("limit", [500])[0])
    sql = "SELECT * FROM sales WHERE 1=1"
    args = []
    if branch:
        sql += " AND branch_id = ?"
        args.append(branch)
    sql += " ORDER BY occurred_at DESC LIMIT ?"
    args.append(limit)
    rows = conn.execute(sql, args).fetchall()
    return 200, [
        {
            "id": r["id"], "txnRef": r["txn_ref"], "productId": r["product_id"],
            "qty": r["qty"], "unitPrice": r["unit_price"], "revenue": r["revenue"],
            "branchId": r["branch_id"], "method": r["method"],
            "discountPct": r["discount_pct"], "shiftId": r["shift_id"],
            "occurredAt": r["occurred_at"], "hourOfDay": r["hour_of_day"],
        }
        for r in rows
    ]


def h_analytics_performance(conn, params, query, body):
    """Powers the Shop Performance panel."""
    sales = conn.execute("SELECT * FROM sales").fetchall()
    products = {p["id"]: p for p in conn.execute("SELECT * FROM products").fetchall()}
    branches = {b["id"]: b for b in conn.execute("SELECT * FROM branches").fetchall()}

    total_rev = sum(s["revenue"] for s in sales)
    total_units = sum(s["qty"] for s in sales)
    txns = len(sales)
    avg_basket = (total_rev / txns) if txns else 0

    branch_rev = {}
    for s in sales:
        branch_rev[s["branch_id"]] = branch_rev.get(s["branch_id"], 0) + s["revenue"]
    max_branch = max(branch_rev.values()) if branch_rev else 0
    branch_cards = []
    for bid, b in branches.items():
        rev = branch_rev.get(bid, 0)
        txn_count = sum(1 for s in sales if s["branch_id"] == bid)
        pct = round(rev / max_branch * 100) if max_branch else 0
        branch_cards.append({
            "branchId": bid, "name": b["name"], "revenue": round(rev, 2),
            "transactions": txn_count, "pctOfTop": pct,
        })

    cat_rev = {}
    for s in sales:
        p = products.get(s["product_id"])
        if p:
            cat_rev[p["cat"]] = cat_rev.get(p["cat"], 0) + s["revenue"]
    max_cat = max(cat_rev.values()) if cat_rev else 0
    category_chart = [
        {"category": c, "revenue": round(r, 2), "pctOfTop": round(r / max_cat * 100) if max_cat else 0}
        for c, r in sorted(cat_rev.items(), key=lambda x: -x[1])
    ]

    hourly = {}
    for s in sales:
        hourly[s["hour_of_day"]] = hourly.get(s["hour_of_day"], 0) + s["revenue"]
    max_hour = max(hourly.values()) if hourly else 0
    hourly_chart = [
        {"hour": h, "revenue": round(r, 2), "pctOfTop": round(r / max_hour * 100) if max_hour else 0}
        for h, r in sorted(hourly.items())
    ]

    return 200, {
        "summary": {
            "totalRevenue": round(total_rev, 2), "transactions": txns,
            "unitsSold": total_units, "avgBasket": round(avg_basket, 2),
        },
        "branches": branch_cards,
        "byCategory": category_chart,
        "byHour": hourly_chart,
    }


def h_analytics_most_sold(conn, params, query, body):
    limit = int(query.get("limit", [10])[0])
    sales = conn.execute("SELECT * FROM sales").fetchall()
    products = {p["id"]: p for p in conn.execute("SELECT * FROM products").fetchall()}

    units_by_id = {}
    rev_by_id = {}
    for s in sales:
        units_by_id[s["product_id"]] = units_by_id.get(s["product_id"], 0) + s["qty"]
        rev_by_id[s["product_id"]] = rev_by_id.get(s["product_id"], 0) + s["revenue"]

    top_units = sorted(units_by_id.items(), key=lambda x: -x[1])[:limit]
    top_rev = sorted(rev_by_id.items(), key=lambda x: -x[1])[:limit]
    max_units = top_units[0][1] if top_units else 1
    max_rev = top_rev[0][1] if top_rev else 1

    def enrich(pid):
        p = products.get(pid)
        return {"id": pid, "name": p["name"] if p else "—", "brand": p["brand"] if p else "",
                "cat": p["cat"] if p else "", "icon": p["icon"] if p else "", "price": p["price"] if p else 0}

    return 200, {
        "topByUnits": [
            {**enrich(pid), "units": u, "revenue": round(rev_by_id.get(pid, 0), 2),
             "pctOfTop": round(u / max_units * 100)}
            for pid, u in top_units
        ],
        "topByRevenue": [
            {**enrich(pid), "revenue": round(r, 2), "units": units_by_id.get(pid, 0),
             "pctOfTop": round(r / max_rev * 100)}
            for pid, r in top_rev
        ],
    }


def h_open_shift(conn, params, query, body):
    branch_id = body.get("branchId")
    cashier = body.get("cashier") or "Unknown"
    if not branch_id or not conn.execute(
        "SELECT 1 FROM branches WHERE id = ?", (branch_id,)
    ).fetchone():
        raise ApiError(400, "Unknown branchId")

    existing = get_open_shift(conn, branch_id)
    if existing:
        raise ApiError(409, f"A shift is already open for this branch (shift #{existing['id']})")

    ts = now_ts()
    cur = conn.execute(
        "INSERT INTO shifts (branch_id, cashier, opened_at, status) VALUES (?, ?, ?, 'open')",
        (branch_id, cashier, ts),
    )
    shift_id = cur.lastrowid

    # Snapshot opening stock for every product at this branch
    stock_rows = conn.execute(
        "SELECT product_id, qty FROM stock WHERE branch_id = ?", (branch_id,)
    ).fetchall()
    conn.executemany(
        "INSERT INTO shift_stock_snapshots (shift_id, product_id, kind, qty, taken_at) "
        "VALUES (?, ?, 'opening', ?, ?)",
        [(shift_id, r["product_id"], r["qty"], ts) for r in stock_rows],
    )
    conn.commit()
    return 200, {"id": shift_id, "branchId": branch_id, "cashier": cashier,
                 "openedAt": ts, "status": "open"}


def h_close_shift(conn, params, query, body):
    shift_id = int(params["id"])
    shift = conn.execute("SELECT * FROM shifts WHERE id = ?", (shift_id,)).fetchone()
    if not shift:
        raise ApiError(404, "Shift not found")
    if shift["status"] == "closed":
        raise ApiError(409, "Shift is already closed")

    ts = now_ts()
    stock_rows = conn.execute(
        "SELECT product_id, qty FROM stock WHERE branch_id = ?", (shift["branch_id"],)
    ).fetchall()
    conn.executemany(
        "INSERT INTO shift_stock_snapshots (shift_id, product_id, kind, qty, taken_at) "
        "VALUES (?, ?, 'closing', ?, ?)",
        [(shift_id, r["product_id"], r["qty"], ts) for r in stock_rows],
    )
    conn.execute(
        "UPDATE shifts SET status = 'closed', closed_at = ? WHERE id = ?", (ts, shift_id)
    )
    conn.commit()
    return 200, _shift_summary(conn, shift_id)


def _shift_summary(conn, shift_id):
    shift = conn.execute("SELECT * FROM shifts WHERE id = ?", (shift_id,)).fetchone()
    if not shift:
        raise ApiError(404, "Shift not found")
    sales = conn.execute("SELECT * FROM sales WHERE shift_id = ?", (shift_id,)).fetchall()

    revenue = sum(s["revenue"] for s in sales)
    pay_totals = {"cash": 0.0, "mpesa": 0.0, "card": 0.0, "credit": 0.0}
    for s in sales:
        pay_totals[s["method"]] = pay_totals.get(s["method"], 0.0) + s["revenue"]

    opening = {
        r["product_id"]: r["qty"] for r in conn.execute(
            "SELECT product_id, qty FROM shift_stock_snapshots "
            "WHERE shift_id = ? AND kind = 'opening'", (shift_id,)
        ).fetchall()
    }
    closing = {
        r["product_id"]: r["qty"] for r in conn.execute(
            "SELECT product_id, qty FROM shift_stock_snapshots "
            "WHERE shift_id = ? AND kind = 'closing'", (shift_id,)
        ).fetchall()
    }
    sold_by_product = {}
    for s in sales:
        sold_by_product[s["product_id"]] = sold_by_product.get(s["product_id"], 0) + s["qty"]

    products = {p["id"]: p for p in conn.execute("SELECT * FROM products").fetchall()}
    variance_rows = []
    for pid, open_qty in opening.items():
        sold = sold_by_product.get(pid, 0)
        close_qty = closing.get(pid)
        if close_qty is None:
            # shift still open — use live stock as a stand-in "current" count
            live = conn.execute(
                "SELECT qty FROM stock WHERE product_id = ? AND branch_id = ?",
                (pid, shift["branch_id"]),
            ).fetchone()
            close_qty = live["qty"] if live else open_qty - sold
        variance = close_qty - (open_qty - sold)
        p = products.get(pid)
        variance_rows.append({
            "productId": pid, "name": p["name"] if p else "—",
            "opening": open_qty, "sold": sold, "closing": close_qty, "variance": variance,
        })

    return {
        "id": shift["id"], "branchId": shift["branch_id"], "cashier": shift["cashier"],
        "openedAt": shift["opened_at"], "closedAt": shift["closed_at"], "status": shift["status"],
        "salesCount": len(sales), "revenue": round(revenue, 2),
        "cashCollected": round(pay_totals["cash"], 2),
        "paymentBreakdown": {k: round(v, 2) for k, v in pay_totals.items()},
        "stockVariance": variance_rows,
    }


def h_current_shift(conn, params, query, body):
    branch_id = query.get("branch", [None])[0]
    if not branch_id:
        raise ApiError(400, "branch query param is required")
    shift = get_open_shift(conn, branch_id)
    if not shift:
        return 200, {"open": False}
    summary = _shift_summary(conn, shift["id"])
    summary["open"] = True
    return 200, summary


def h_shift_summary(conn, params, query, body):
    return 200, _shift_summary(conn, int(params["id"]))


def h_list_shifts(conn, params, query, body):
    branch = query.get("branch", [None])[0]
    sql = "SELECT * FROM shifts WHERE 1=1"
    args = []
    if branch:
        sql += " AND branch_id = ?"
        args.append(branch)
    sql += " ORDER BY opened_at DESC"
    rows = conn.execute(sql, args).fetchall()
    return 200, [
        {"id": r["id"], "branchId": r["branch_id"], "cashier": r["cashier"],
         "openedAt": r["opened_at"], "closedAt": r["closed_at"], "status": r["status"]}
        for r in rows
    ]


# ─────────────────────────────────────────────────────────────────────────
# Routing table: (METHOD, regex pattern with named groups, handler)
# ─────────────────────────────────────────────────────────────────────────
ROUTES = [
    ("GET", r"^/api/branches$", h_list_branches),
    ("GET", r"^/api/categories$", h_list_categories),
    ("GET", r"^/api/products$", h_list_products),
    ("GET", r"^/api/products/(?P<id>\d+)$", h_get_product),

    ("GET", r"^/api/stock$", h_stock_levels),
    ("POST", r"^/api/stock/add$", h_add_stock),
    ("GET", r"^/api/stock/movements$", h_stock_movements),

    ("POST", r"^/api/discounts/validate$", h_validate_discount),
    ("POST", r"^/api/cart/quote$", h_quote),
    ("POST", r"^/api/checkout$", h_checkout),

    ("GET", r"^/api/sales$", h_sales_log),
    ("GET", r"^/api/analytics/performance$", h_analytics_performance),
    ("GET", r"^/api/analytics/most-sold$", h_analytics_most_sold),

    ("POST", r"^/api/shifts/open$", h_open_shift),
    ("POST", r"^/api/shifts/(?P<id>\d+)/close$", h_close_shift),
    ("GET", r"^/api/shifts/current$", h_current_shift),
    ("GET", r"^/api/shifts/(?P<id>\d+)$", h_shift_summary),
    ("GET", r"^/api/shifts$", h_list_shifts),
]
COMPILED_ROUTES = [(m, re.compile(p), h) for m, p, h in ROUTES]


# ─────────────────────────────────────────────────────────────────────────
# HTTP server
# ─────────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "LumierePOS/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send_json(self, status, payload):
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise ApiError(400, "Malformed JSON body")

    def do_OPTIONS(self):
        self._send_json(204, "")

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        for route_method, pattern, handler in COMPILED_ROUTES:
            if route_method != method:
                continue
            match = pattern.match(parsed.path)
            if not match:
                continue
            params = match.groupdict()
            try:
                body = self._read_json_body() if method in ("POST", "PUT") else {}
                conn = db.get_conn()
                try:
                    status, payload = handler(conn, params, query, body)
                finally:
                    conn.close()
                self._send_json(status, payload)
            except ApiError as e:
                self._send_json(e.status, {"error": e.message})
            except Exception:
                traceback.print_exc()
                self._send_json(500, {"error": "Internal server error"})
            return

        self._send_json(404, {"error": f"No route for {method} {parsed.path}"})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")


def main():
    reset = "--reset" in sys.argv
    db.init_db(reset=reset)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Lumière POS backend running on http://localhost:{PORT}")
    print(f"Database: {db.DB_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
