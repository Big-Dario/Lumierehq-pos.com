# Lumière POS — Backend

A REST API backend for the Lumière cosmetics POS front-end. It replaces the
hardcoded, in-browser `PRODUCTS` / `BRANCHES` / `salesLog` arrays with a real
persisted SQLite database and covers every panel in the UI: Point of Sale,
Stock Levels, Add Stock, Cashier Shift, Shop Performance, and Top Products.

**Zero external dependencies.** Built with only the Python standard library
(`http.server`, `sqlite3`), so there's nothing to `pip install`.

## Requirements

- Python 3.9+

## Run it

```bash
python3 app.py            # starts on http://localhost:8787, keeps existing data
python3 app.py --reset    # wipes lumiere.db and reseeds from scratch
```

The first run creates `lumiere.db` next to `app.py` and seeds it with the
same 4 branches, 22 products, per-branch stock levels, and 20 historical
sales rows that were hardcoded in the original front-end — so totals and
charts look the same as the demo on first load.

## Project structure

```
lumiere-backend/
  app.py       API server + routing (start here)
  db.py        Schema + seed data
  lumiere.db   SQLite database (created on first run)
  README.md
```

## Data model

| Table                    | Purpose                                              |
|---------------------------|-------------------------------------------------------|
| `branches`                | The 4 store locations                                 |
| `products`                | Product catalog                                       |
| `stock`                   | Live per-branch quantity for every product            |
| `discount_codes`          | Named discount codes (`VIP15`, `LUMIERE10`, …)        |
| `shifts`                  | Cashier shift open/close lifecycle                     |
| `shift_stock_snapshots`   | Opening/closing stock counts captured per shift        |
| `sales`                   | One row per line item sold (powers all analytics)      |
| `stock_movements`         | Audit log of stock received via "Add Stock"            |

All money fields are plain numbers in KES, matching the front-end.

## API reference

All responses are JSON. All endpoints are prefixed `/api`. CORS is wide open
(`Access-Control-Allow-Origin: *`) so you can call it straight from the
`index.html` file during development.

### Catalog

| Method | Path                         | Notes |
|--------|-------------------------------|-------|
| GET    | `/api/branches`               | List all 4 branches |
| GET    | `/api/categories`              | `["All", "Skincare", "Makeup", ...]` |
| GET    | `/api/products?branch=&category=&q=` | Filtered product list. `branch` adds a `branchStock` field per item |
| GET    | `/api/products/:id`           | Single product with full per-branch `stock` map |

### Stock

| Method | Path                    | Body | Notes |
|--------|--------------------------|------|-------|
| GET    | `/api/stock`             | —    | Powers the **Stock Levels** panel: per-product/per-branch table + summary stats |
| POST   | `/api/stock/add`         | `{branchId, productId, qty, reference}` | Powers **Add Stock**. Increments stock and logs a movement |
| GET    | `/api/stock/movements?limit=` | — | Recent stock receipts, newest first |

### Cart / checkout

| Method | Path                      | Body | Notes |
|--------|----------------------------|------|-------|
| POST   | `/api/discounts/validate` | `{code}` | Checks named codes and raw numeric percentages, like the front-end |
| POST   | `/api/cart/quote`         | `{items:[{productId,qty}], discountPct}` | Computes subtotal/discount/VAT/total without committing anything — good for live cart totals |
| POST   | `/api/checkout`           | `{branchId, items:[{productId,qty}], discountPct, payMethod, cashTendered, cashier}` | Validates stock, computes totals (16% VAT), deducts stock, logs the sale, returns a full receipt. Returns `409` if any item is short on stock, `400` if cash tendered is less than the total |

### Sales & analytics

| Method | Path                              | Notes |
|--------|-------------------------------------|-------|
| GET    | `/api/sales?branch=&limit=`        | Raw sales log, newest first |
| GET    | `/api/analytics/performance`       | Powers **Shop Performance**: total revenue/txns/units/avg basket, per-branch revenue, revenue by category, revenue by hour |
| GET    | `/api/analytics/most-sold?limit=`  | Powers **Top Products**: best sellers by units and by revenue |

### Shifts

| Method | Path                       | Body | Notes |
|--------|-----------------------------|------|-------|
| POST   | `/api/shifts/open`         | `{branchId, cashier}` | Opens a shift, snapshots opening stock for that branch. `409` if one's already open |
| POST   | `/api/shifts/:id/close`    | —    | Closes the shift, snapshots closing stock, returns the full summary |
| GET    | `/api/shifts/current?branch=` | — | The open shift for a branch (sales count, revenue, cash collected, payment breakdown, stock variance). `{"open": false}` if none |
| GET    | `/api/shifts/:id`          | —    | Summary for any shift, open or closed |
| GET    | `/api/shifts?branch=`      | —    | List shifts, newest first |

## Example: a full sale

```bash
# 1. Open a shift for the day
curl -X POST localhost:8787/api/shifts/open \
  -H "Content-Type: application/json" \
  -d '{"branchId":"westlands","cashier":"Amara K."}'

# 2. Ring up a sale
curl -X POST localhost:8787/api/checkout \
  -H "Content-Type: application/json" \
  -d '{
    "branchId":"westlands",
    "items":[{"productId":6,"qty":2},{"productId":3,"qty":1}],
    "discountPct":10,
    "payMethod":"cash",
    "cashTendered":10000,
    "cashier":"Amara K."
  }'

# 3. Check shift totals so far
curl "localhost:8787/api/shifts/current?branch=westlands"

# 4. End of day
curl -X POST localhost:8787/api/shifts/1/close
```

## Wiring this into the existing `index.html`

The front-end currently does everything against in-memory arrays
(`PRODUCTS`, `cart`, `salesLog`, etc.) inside `<script>`. To connect it to
this backend, the main changes are:

1. Replace the hardcoded `PRODUCTS`/`BRANCHES` consts with a `fetch('/api/products?branch=...')` / `fetch('/api/branches')` call on load, and re-fetch whenever the branch switches.
2. In `checkout()`, instead of computing totals and mutating `PRODUCTS` locally, `POST /api/checkout` with the cart contents and render the receipt from the response (it already returns everything the receipt needs — txnRef, items, totals, change).
3. In `addStock()`, `POST /api/stock/add` instead of mutating `p.stock[branch]` directly.
4. In `renderStockPanel`, `renderShiftPanel`, `renderPerformancePanel`, and `renderMostSoldPanel`, replace the local array-reducing logic with `fetch` calls to `/api/stock`, `/api/shifts/current`, `/api/analytics/performance`, and `/api/analytics/most-sold` respectively — each endpoint already returns data shaped close to what those render functions expect.

I kept the JSON field names close to the original JS variable names
(`cat`, `brand`, `shade`, `qty`, `revenue`, `branch_id` → `branchId`, etc.)
to make that swap mostly mechanical. Happy to do that integration pass on
`index.html` directly if you'd like — just say the word.

## Notes & limitations

- Single SQLite file, no auth/multi-user locking — fine for one till or local dev, not for concurrent production traffic. Swappable for Postgres later with minimal changes since all access goes through `db.py`.
- No authentication on any endpoint yet. Add an API key / session check in `Handler._dispatch` before wiring this up to anything public.
- Timestamps are Unix seconds (UTC-ish, server local time for `hour_of_day`); format on the client however you like.
- `--reset` destroys all data — only use it in development.
