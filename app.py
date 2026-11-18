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
import os
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import db
import auth

PORT = int(os.environ.get("PORT", 8787))     # Render injects PORT
VAT_RATE = 0.16
# Set LUMIERE_ALLOWED_ORIGIN to your front-end's origin in production (e.g. https://pos.example.com)
ALLOWED_ORIGIN = os.environ.get("LUMIERE_ALLOWED_ORIGIN", "*")
MAX_BODY = 1_000_000
REQ = threading.local()     # per-request ip + Authorization header (for login/logout/etc.)


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

def h_health(conn, params, query, body, user):
    conn.execute("SELECT 1")
    return 200, {"status": "ok"}


def h_list_branches(conn, params, query, body, user):
    rows = conn.execute("SELECT * FROM branches ORDER BY name").fetchall()
    return 200, [row_to_dict(r) for r in rows]


def h_list_categories(conn, params, query, body, user):
    rows = conn.execute("SELECT DISTINCT cat FROM products ORDER BY cat").fetchall()
    return 200, ["All"] + [r["cat"] for r in rows]


def h_list_products(conn, params, query, body, user):
    branch = auth.scoped_branch(user, query.get("branch", [None])[0])
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


def h_get_product(conn, params, query, body, user):
    pid = int(params["id"])
    row = conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
    if not row:
        raise ApiError(404, "Product not found")
    stock_map = fetch_stock_map(conn, pid)
    return 200, product_row_to_dict(row, stock_map)


def h_stock_levels(conn, params, query, body, user):
    """Powers the Stock Levels panel: per-product, per-branch quantities."""
    products = conn.execute("SELECT * FROM products ORDER BY id").fetchall()
    branches = conn.execute("SELECT id FROM branches ORDER BY id").fetchall()
    branch_ids = [b["id"] for b in branches]
    scope = auth.scoped_branch(user)
    if scope:
        branch_ids = [scope]

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
            "stock": {b: stock_map.get(b, 0) for b in branch_ids}, "total": row_total,
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


def h_add_stock(conn, params, query, body, user):
    branch_id = body.get("branchId")
    product_id = body.get("productId")
    qty = body.get("qty")
    reference = (body.get("reference") or "Manual entry").strip()

    if not branch_id or not conn.execute(
        "SELECT 1 FROM branches WHERE id = ?", (branch_id,)
    ).fetchone():
        raise ApiError(400, "Unknown branchId")
    auth.scoped_branch(user, branch_id)
    if not isinstance(qty, (int, float)) or isinstance(qty, bool) or qty < 1:
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
        "INSERT INTO stock_movements (product_id, branch_id, qty, reference, created_at, user_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (product_id, branch_id, qty, reference, ts, user["id"]),
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


def h_stock_movements(conn, params, query, body, user):
    limit = min(int(query.get("limit", [50])[0]), 500)
    scope = auth.scoped_branch(user)
    rows = conn.execute(
        "SELECT m.*, p.name AS product_name, b.name AS branch_name "
        "FROM stock_movements m "
        "JOIN products p ON p.id = m.product_id "
        "JOIN branches b ON b.id = m.branch_id "
        "WHERE (? IS NULL OR m.branch_id = ?) "
        "ORDER BY m.created_at DESC LIMIT ?",
        (scope, scope, limit),
    ).fetchall()
    return 200, [
        {
            "id": r["id"], "productId": r["product_id"], "productName": r["product_name"],
            "branchId": r["branch_id"], "branchName": r["branch_name"],
            "qty": r["qty"], "reference": r["reference"], "createdAt": r["created_at"],
        }
        for r in rows
    ]


def h_validate_discount(conn, params, query, body, user):
    code = (body.get("code") or "").strip().upper()
    cap = auth.MAX_DISCOUNT[user["role"]]
    row = conn.execute(
        "SELECT pct FROM discount_codes WHERE code = ?", (code,)
    ).fetchone()
    if row:
        if row["pct"] > cap:
            return 200, {"valid": False, "pct": 0, "reason": "Needs manager approval"}
        return 200, {"valid": True, "pct": row["pct"]}
    # allow a raw numeric percentage too, mirroring the frontend behaviour
    try:
        num = float(code)
        if 0 < num <= cap:
            return 200, {"valid": True, "pct": num}
        if cap < num <= 100:
            return 200, {"valid": False, "pct": 0, "reason": "Needs manager approval"}
    except ValueError:
        pass
    return 200, {"valid": False, "pct": 0}


def h_quote(conn, params, query, body, user):
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


def h_checkout(conn, params, query, body, user):
    branch_id = body.get("branchId")
    items = body.get("items") or []
    discount_pct = float(body.get("discountPct") or 0)
    pay_method = body.get("payMethod") or "cash"
    cash_tendered = body.get("cashTendered")
    cashier = user["full_name"]          # from the session, never from the client

    if not branch_id or not conn.execute(
        "SELECT 1 FROM branches WHERE id = ?", (branch_id,)
    ).fetchone():
        raise ApiError(400, "Unknown branchId")
    auth.scoped_branch(user, branch_id)
    if not 0 <= discount_pct <= auth.MAX_DISCOUNT[user["role"]]:
        raise ApiError(403, f"Discounts above {auth.MAX_DISCOUNT[user['role']]}% need a manager")
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
            "method, discount_pct, shift_id, occurred_at, hour_of_day, user_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                txn_ref, pid, qty, line["product"]["price"], line["lineTotal"],
                branch_id, pay_method, discount_pct, shift_id, ts, hour, user["id"],
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


def h_sales_log(conn, params, query, body, user):
    branch = auth.scoped_branch(user, query.get("branch", [None])[0])
    limit = min(int(query.get("limit", [500])[0]), 2000)
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


def _scoped_sales(conn, user):
    scope = auth.scoped_branch(user)
    if scope:
        return conn.execute("SELECT * FROM sales WHERE branch_id = ?", (scope,)).fetchall()
    return conn.execute("SELECT * FROM sales").fetchall()


def h_analytics_performance(conn, params, query, body, user):
    """Powers the Shop Performance panel."""
    sales = _scoped_sales(conn, user)
    products = {p["id"]: p for p in conn.execute("SELECT * FROM products").fetchall()}
    branches = {b["id"]: b for b in conn.execute("SELECT * FROM branches").fetchall()}
    if auth.scoped_branch(user):
        branches = {k: v for k, v in branches.items() if k == user["branch_id"]}

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


def h_analytics_most_sold(conn, params, query, body, user):
    limit = int(query.get("limit", [10])[0])
    sales = _scoped_sales(conn, user)
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


def h_open_shift(conn, params, query, body, user):
    branch_id = body.get("branchId")
    cashier = user["full_name"]          # from the session, never from the client
    if not branch_id or not conn.execute(
        "SELECT 1 FROM branches WHERE id = ?", (branch_id,)
    ).fetchone():
        raise ApiError(400, "Unknown branchId")
    auth.scoped_branch(user, branch_id)

    existing = get_open_shift(conn, branch_id)
    if existing:
        raise ApiError(409, f"A shift is already open for this branch (shift #{existing['id']})")

    ts = now_ts()
    cur = conn.execute(
        "INSERT INTO shifts (branch_id, cashier, opened_at, status, user_id) "
        "VALUES (?, ?, ?, 'open', ?)",
        (branch_id, cashier, ts, user["id"]),
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


def h_close_shift(conn, params, query, body, user):
    shift_id = int(params["id"])
    shift = conn.execute("SELECT * FROM shifts WHERE id = ?", (shift_id,)).fetchone()
    if not shift:
        raise ApiError(404, "Shift not found")
    auth.scoped_branch(user, shift["branch_id"])
    if shift["user_id"] != user["id"]:            # not your shift -> need manager rights
        auth.require(user, "shift.close_any")
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


def h_current_shift(conn, params, query, body, user):
    branch_id = auth.scoped_branch(user, query.get("branch", [None])[0])
    if not branch_id:
        raise ApiError(400, "branch query param is required")
    shift = get_open_shift(conn, branch_id)
    if not shift:
        return 200, {"open": False}
    summary = _shift_summary(conn, shift["id"])
    summary["open"] = True
    return 200, summary


def h_shift_summary(conn, params, query, body, user):
    summary = _shift_summary(conn, int(params["id"]))
    auth.scoped_branch(user, summary["branchId"])
    return 200, summary


def h_list_shifts(conn, params, query, body, user):
    branch = auth.scoped_branch(user, query.get("branch", [None])[0])
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
# Authentication + user management
# ─────────────────────────────────────────────────────────────────────────

def h_login(conn, params, query, body, user):
    token, expires, u = auth.login(conn, body.get("username"), body.get("password"),
                                   getattr(REQ, "ip", ""))
    return 200, {"token": token, "expiresAt": expires, "user": auth.public_user(u)}


def h_logout(conn, params, query, body, user):
    auth.logout(conn, REQ.auth_header)
    return 200, {"ok": True}


def h_me(conn, params, query, body, user):
    return 200, auth.public_user(user)


def h_change_password(conn, params, query, body, user):
    current, new = body.get("currentPassword"), body.get("newPassword")
    if not auth.verify_password(current or "", user["password_hash"]):
        raise ApiError(401, "Current password is incorrect")
    auth.check_password_strength(new)
    if new == current:
        raise ApiError(400, "New password must be different")
    conn.execute("UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
                 (auth.hash_password(new), user["id"]))
    auth.revoke_user_sessions(conn, user["id"], keep_header=REQ.auth_header)  # sign out other devices
    conn.commit()
    return 200, {"ok": True}


def h_list_users(conn, params, query, body, user):
    rows = conn.execute("SELECT * FROM users ORDER BY username").fetchall()
    return 200, [dict(auth.public_user(r), lastLogin=r["last_login"]) for r in rows]


def h_create_user(conn, params, query, body, user):
    uid = auth.create_user(conn, body.get("username"), body.get("password"),
                           body.get("role"), body.get("branchId"), body.get("fullName"))
    row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    return 201, auth.public_user(row)


def h_update_user(conn, params, query, body, user):
    uid = int(params["id"])
    target = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if not target:
        raise ApiError(404, "User not found")

    role = body.get("role", target["role"])
    branch = body.get("branchId", target["branch_id"])
    active = int(bool(body.get("active", target["active"])))
    full_name = body.get("fullName", target["full_name"])
    if role not in auth.ROLES:
        raise ApiError(400, "Invalid role")
    if role == "admin":
        branch = None
    elif not branch or not conn.execute("SELECT 1 FROM branches WHERE id = ?", (branch,)).fetchone():
        raise ApiError(400, "Managers and cashiers need a valid branchId")
    if uid == user["id"] and (role != "admin" or not active):
        raise ApiError(400, "You can't demote or disable your own account")

    conn.execute("UPDATE users SET role = ?, branch_id = ?, active = ?, full_name = ? WHERE id = ?",
                 (role, branch, active, full_name, uid))
    if body.get("password"):                      # admin reset: user must pick a new one
        auth.check_password_strength(body["password"])
        conn.execute("UPDATE users SET password_hash = ?, must_change_password = 1, "
                     "failed_attempts = 0, locked_until = NULL WHERE id = ?",
                     (auth.hash_password(body["password"]), uid))
    if (role != target["role"] or branch != target["branch_id"] or not active
            or body.get("password")):
        auth.revoke_user_sessions(conn, uid)      # force re-login with the new rights
    conn.commit()
    return 200, auth.public_user(conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())


# ─────────────────────────────────────────────────────────────────────────
# Routing table: (METHOD, regex pattern with named groups, handler)
# ─────────────────────────────────────────────────────────────────────────
# (METHOD, pattern, handler, permission)   permission: None = public,
# "auth" = any signed-in user, otherwise a permission from auth.PERMISSIONS
ROUTES = [
    ("GET", r"^/api/health$", h_health, None),
    ("POST", r"^/api/auth/login$", h_login, None),
    ("POST", r"^/api/auth/logout$", h_logout, "auth"),
    ("GET", r"^/api/auth/me$", h_me, "auth"),
    ("POST", r"^/api/auth/change-password$", h_change_password, "auth"),

    ("GET", r"^/api/users$", h_list_users, "users.manage"),
    ("POST", r"^/api/users$", h_create_user, "users.manage"),
    ("PUT", r"^/api/users/(?P<id>\d+)$", h_update_user, "users.manage"),

    ("GET", r"^/api/branches$", h_list_branches, "catalog.read"),
    ("GET", r"^/api/categories$", h_list_categories, "catalog.read"),
    ("GET", r"^/api/products$", h_list_products, "catalog.read"),
    ("GET", r"^/api/products/(?P<id>\d+)$", h_get_product, "catalog.read"),

    ("GET", r"^/api/stock$", h_stock_levels, "stock.read"),
    ("POST", r"^/api/stock/add$", h_add_stock, "stock.add"),
    ("GET", r"^/api/stock/movements$", h_stock_movements, "stock.read"),

    ("POST", r"^/api/discounts/validate$", h_validate_discount, "discount.validate"),
    ("POST", r"^/api/cart/quote$", h_quote, "checkout"),
    ("POST", r"^/api/checkout$", h_checkout, "checkout"),

    ("GET", r"^/api/sales$", h_sales_log, "sales.read"),
    ("GET", r"^/api/analytics/performance$", h_analytics_performance, "analytics.read"),
    ("GET", r"^/api/analytics/most-sold$", h_analytics_most_sold, "analytics.read"),

    ("POST", r"^/api/shifts/open$", h_open_shift, "shift.open"),
    ("POST", r"^/api/shifts/(?P<id>\d+)/close$", h_close_shift, "shift.close_own"),
    ("GET", r"^/api/shifts/current$", h_current_shift, "shift.read"),
    ("GET", r"^/api/shifts/(?P<id>\d+)$", h_shift_summary, "shift.read"),
    ("GET", r"^/api/shifts$", h_list_shifts, "shift.read"),
]
COMPILED_ROUTES = [(m, re.compile(p), h, perm) for m, p, h, perm in ROUTES]


# ─────────────────────────────────────────────────────────────────────────
# HTTP server
# ─────────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "LumierePOS/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send_json(self, status, payload):
        body = b"" if status == 204 else json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        if length > MAX_BODY:
            raise ApiError(413, "Request body too large")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise ApiError(400, "Malformed JSON body")
        if not isinstance(data, dict):
            raise ApiError(400, "JSON body must be an object")
        return data

    def do_OPTIONS(self):
        self._send_json(204, "")

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        for route_method, pattern, handler, perm in COMPILED_ROUTES:
            if route_method != method:
                continue
            match = pattern.match(parsed.path)
            if not match:
                continue
            params = match.groupdict()
            REQ.ip = self.client_address[0]
            REQ.auth_header = self.headers.get("Authorization", "")
            try:
                body = self._read_json_body() if method in ("POST", "PUT") else {}
                conn = db.get_conn()
                try:
                    user = None
                    if perm is not None:
                        user = auth.authenticate(conn, REQ.auth_header)      # 401 if not signed in
                        if perm != "auth":
                            if user["must_change_password"]:
                                raise ApiError(403, "Password change required")
                            auth.require(user, perm)                         # 403 if role can't
                    status, payload = handler(conn, params, query, body, user)
                finally:
                    conn.close()
                self._send_json(status, payload)
            except (ApiError, auth.AuthError) as e:
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


def seed_users(conn):
    """Create manager/cashier accounts from the LUMIERE_SEED_USERS env var.
    Format: "username:role:branch_id:password,username:role:branch_id:password"
    Existing usernames are skipped, so it is safe to leave set permanently."""
    raw = os.environ.get("LUMIERE_SEED_USERS", "")
    for entry in filter(None, (e.strip() for e in raw.split(","))):
        parts = entry.split(":", 3)
        if len(parts) != 4:
            print("Bad LUMIERE_SEED_USERS entry (need username:role:branch_id:password)")
            continue
        uname, role, branch, pw = parts
        try:
            auth.create_user(conn, uname, pw, role, branch)
            print(f"Seeded user '{uname.strip().lower()}' ({role}, {branch})")
        except auth.AuthError as e:
            print(f"Skipped user '{uname}': {e.message}")


def main():
    reset = "--reset" in sys.argv
    db.init_db(reset=reset)
    _c = db.get_conn()
    auth.bootstrap_admin(_c)          # first run: creates 'admin'
    seed_users(_c)                    # optional: staff accounts from LUMIERE_SEED_USERS
    _c.close()
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
