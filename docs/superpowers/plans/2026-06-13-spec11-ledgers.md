# Spec 11 — Ledgers Read API Implementation Plan (Phase 1)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Expose the four append-only ledgers read-only over `GET /api/ledgers/{kind}` (transactions/dividends/fx/openings) with account-name join, filters (account/symbol/date-range), desc pagination, the Side/DividendType **lowercase wire format**, and the buy/sell `total` sign convention.

**Architecture:** A thin `ledgers` router wrapping the existing `data_ingestion.store.list_*` reads. No writes (writes are spec 12 only). Computation is limited to presentation-level derived display fields (`total` sign, `implied_rate` via the existing model property) over already-stored ledger values — no numbers of record are computed here.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, sqlite3, Decimal, pytest + TestClient, mypy --strict, ruff.

**CRITICAL — interpreter:** all gates via `./.venv/Scripts/python.exe -m pytest|mypy|ruff`.

**Branch:** `feat/ledgers-api` (created off main @ spec 10).

**Authoritative contract:** `docs/design-handoff/.../specs/11-ledgers-read.md` (committed; exact JSON shapes there, reproduced per-task).

## Reconciliations locked for this plan (important)
1. **No new `fee_snapshot` column.** The `transactions` table already has `fee_rule_snapshot TEXT` (the `compute_fees` snapshot dict), and `StoredTransaction.fee_rule_snapshot: dict[str,str]`. The ledger read maps that existing dict to the API field `fee_snapshot` (raw values) + a best-effort `label`; `null`/`{}` when absent (old rows). The spec's migration note is satisfied by the existing column — **do not add a column**. (A standardized snapshot+label is finalized with the write path in spec 12; spec 11 is read-only and passes through what's stored.)
2. **Enum lowercase wire format is localized.** Do NOT change `api/serialize.to_wire` (it must keep `Currency` uppercase). The ledgers router lowercases `side` (`StoredTransaction.side: Side` → `.value.lower()`) and dividend `type` (`StoredDividend.type: str` → `.lower()`) explicitly. (This is SR conflict #1; the shared enum-wire concern is satisfied per-field here.)
3. **Pagination/sort/date-filter in the router.** `store.list_*` return ASC and unpaginated; the router applies account/symbol filters via the store's existing params, then filters by date range, reverses to **desc by (date, rowid)**, sets `total_count = len(filtered)`, and slices `[offset:offset+limit]`. Data volume is tiny.
4. **`openings` has no `id`** (PK = account_id+symbol). The API `id` for openings is a 1-based synthetic index over the sorted result (display key only). Note it; do not fabricate a DB id.
5. **`total` sign convention** (spec 11): buy = `-(gross + fee + tax)`, sell = `+(gross - fee - tax)`, where `gross = quantity*price`. Computed in the router as a display field; money stays Decimal→string.

## Verified shapes
- `store.list_transactions(conn, *, account_id=None, symbol=None) -> list[StoredTransaction]` (fields: `id, account_id, symbol, side: Side, quantity, price, fees, tax, trade_date, fee_rule_snapshot: dict[str,str], note: str|None`). ASC by trade_date,id.
- `store.list_dividends(conn, *, account_id=None, symbol=None) -> list[StoredDividend]` (`id, account_id, symbol, date, type: str, gross, withholding, net, reinvest_shares: Decimal|None, reinvest_price: Decimal|None`). ASC.
- `store.list_fx_conversions(conn, *, account_id=None) -> list[StoredFxConversion]` (`id, account_id, date, from_ccy: Currency, from_amount, to_ccy: Currency, to_amount`; `.implied_rate` property = from/to). ASC.
- `store.list_opening(conn, *, account_id=None) -> list[StoredOpening]` (`account_id, symbol, shares, original_avg_cost, original_cost_total, build_date`; NO id). ASC by account_id,symbol.
- `store.list_accounts(conn) -> list[Account]` (`.account_id`, `.name`); `store.list_instruments(conn) -> list[Instrument]` (`.symbol`, `.name`, `.quote_ccy`).
- Phase-0 api: `get_conn` dep; `api/errors.error_body`; `fastapi.responses.JSONResponse`. Test fixture `api_client` (golden_db: tw_broker 2330 BUY 1000@500 on 2026-01-05; schwab AAPL BUY 10@100 on 2026-01-10; tw_broker 2330 CASH dividend net 5000 on 2026-03-01; schwab fx TWD→USD 32000→1000 on 2026-01-08; no openings).

---

### Task 1: ledgers router foundation + transactions + dividends endpoints

**Files:** Create `portfolio_dash/api/routers/ledgers.py`; Modify `portfolio_dash/api/app.py` (include router); Test `tests/contract/test_ledgers_api.py`.

- [ ] **Step 1: failing test**

Create `tests/contract/test_ledgers_api.py`:
```python
from fastapi.testclient import TestClient


def test_transactions_shape_lowercase_side_and_total_sign(api_client: TestClient) -> None:
    r = api_client.get("/api/ledgers/transactions")
    assert r.status_code == 200
    body = r.json()
    assert body["total_count"] == 2
    rows = {(x["symbol"], x["account_id"]): x for x in body["rows"]}
    tx = rows[("2330", "tw_broker")]
    assert tx["side"] == "buy"                      # lowercase wire
    assert tx["account"] == "TW Broker"             # account-name join
    assert tx["shares"] == "1000" and tx["price"] == "500"
    assert tx["total"] == "-500000"                 # buy: -(1000*500 + 0 + 0)
    assert tx["ccy"] == "TWD"
    assert "fee_snapshot" in tx                      # passthrough (may be {} / null)


def test_transactions_filter_and_pagination(api_client: TestClient) -> None:
    r = api_client.get("/api/ledgers/transactions", params={"account_id": "schwab"})
    body = r.json()
    assert body["total_count"] == 1 and body["rows"][0]["symbol"] == "AAPL"
    r2 = api_client.get("/api/ledgers/transactions", params={"limit": 1, "offset": 0})
    assert len(r2.json()["rows"]) == 1 and r2.json()["total_count"] == 2


def test_transactions_bad_date_range_400(api_client: TestClient) -> None:
    r = api_client.get("/api/ledgers/transactions",
                       params={"from": "2026-12-01", "to": "2026-01-01"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "validation_error"


def test_dividends_lowercase_type(api_client: TestClient) -> None:
    body = api_client.get("/api/ledgers/dividends").json()
    assert body["total_count"] == 1
    d = body["rows"][0]
    assert d["type"] == "cash" and d["symbol"] == "2330"
    assert d["net"] == "5000" and d["account"] == "TW Broker" and d["ccy"] == "TWD"
```

- [ ] **Step 2: run, expect fail** — `./.venv/Scripts/python.exe -m pytest tests/contract/test_ledgers_api.py -v` → 404.

- [ ] **Step 3: implement** `portfolio_dash/api/routers/ledgers.py`:
```python
"""Four append-only ledgers, read-only (spec 11). Thin over store.list_*; no writes.

Side/DividendType serialize lowercase (SR #1); Currency stays uppercase. The `total`
sign + `implied_rate` are presentation-level derived fields over stored ledger values.
"""

import sqlite3
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from portfolio_dash.api.deps import get_conn
from portfolio_dash.api.errors import error_body
from portfolio_dash.data_ingestion.store import (
    list_accounts,
    list_dividends,
    list_fx_conversions,
    list_instruments,
    list_opening,
    list_transactions,
)

router = APIRouter()


def _names(conn: sqlite3.Connection) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    accts = {a.account_id: a.name for a in list_accounts(conn)}
    insts = list_instruments(conn)
    names = {i.symbol: i.name for i in insts}
    ccys = {i.symbol: i.quote_ccy.value for i in insts}
    return accts, names, ccys


def _page(rows: list[dict[str, Any]], limit: int, offset: int) -> dict[str, Any]:
    # rows arrive ASC; present desc by recency.
    desc = list(reversed(rows))
    return {"rows": desc[offset:offset + limit], "total_count": len(desc)}


def _check_dates(frm: str | None, to: str | None) -> JSONResponse | None:
    if frm and to and frm > to:
        return JSONResponse(status_code=400,
                            content=error_body("validation_error", "日期區間無效", field="from"))
    return None


def _in_range(d: date, frm: str | None, to: str | None) -> bool:
    if frm and d.isoformat() < frm:
        return False
    if to and d.isoformat() > to:
        return False
    return True


@router.get("/ledgers/transactions")
def transactions(
    account_id: str | None = None, symbol: str | None = None,
    frm: str | None = Query(None, alias="from"), to: str | None = None,
    limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    bad = _check_dates(frm, to)
    if bad is not None:
        return bad
    accts, names, ccys = _names(conn)
    out: list[dict[str, Any]] = []
    for t in list_transactions(conn, account_id=account_id, symbol=symbol):
        if not _in_range(t.trade_date, frm, to):
            continue
        gross = t.quantity * t.price
        total = -(gross + t.fees + t.tax) if t.side.value == "BUY" else (gross - t.fees - t.tax)
        out.append({
            "id": t.id, "date": t.trade_date.isoformat(), "account_id": t.account_id,
            "account": accts.get(t.account_id, t.account_id), "symbol": t.symbol,
            "name": names.get(t.symbol, ""), "side": t.side.value.lower(),
            "shares": str(t.quantity), "price": str(t.price), "fee": str(t.fees),
            "tax": str(t.tax), "total": str(total), "ccy": ccys.get(t.symbol, ""),
            "fee_snapshot": (t.fee_rule_snapshot or None), "note": t.note,
        })
    return _page(out, limit, offset)


@router.get("/ledgers/dividends")
def dividends(
    account_id: str | None = None, symbol: str | None = None,
    frm: str | None = Query(None, alias="from"), to: str | None = None,
    limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    bad = _check_dates(frm, to)
    if bad is not None:
        return bad
    accts, names, ccys = _names(conn)
    out: list[dict[str, Any]] = []
    for d in list_dividends(conn, account_id=account_id, symbol=symbol):
        if not _in_range(d.date, frm, to):
            continue
        out.append({
            "id": d.id, "date": d.date.isoformat(), "account_id": d.account_id,
            "account": accts.get(d.account_id, d.account_id), "symbol": d.symbol,
            "name": names.get(d.symbol, ""), "type": d.type.lower(),
            "gross": str(d.gross), "withhold": str(d.withholding), "net": str(d.net),
            "reinvest_shares": str(d.reinvest_shares) if d.reinvest_shares is not None else None,
            "reinvest_price": str(d.reinvest_price) if d.reinvest_price is not None else None,
            "ccy": ccys.get(d.symbol, ""),
        })
    return _page(out, limit, offset)
```
In `portfolio_dash/api/app.py`: add `ledgers` to the routers import and `app.include_router(ledgers.router, prefix="/api")`.

- [ ] **Step 4: run, expect pass** → 4 PASS.

- [ ] **Step 5: gates + commit**
```bash
./.venv/Scripts/python.exe -m pytest -q          # report N passed
./.venv/Scripts/python.exe -m mypy portfolio_dash tests --strict
./.venv/Scripts/python.exe -m ruff check portfolio_dash tests
git add portfolio_dash/api/routers/ledgers.py portfolio_dash/api/app.py tests/contract/test_ledgers_api.py
git commit -m "feat(api): GET /api/ledgers/{transactions,dividends} (lowercase enum wire, total sign) (spec 11)"
```

---

### Task 2: fx + openings endpoints

**Files:** Modify `portfolio_dash/api/routers/ledgers.py`; Test append to `tests/contract/test_ledgers_api.py`.

- [ ] **Step 1: failing test** (append):
```python
def test_fx_rows(api_client: TestClient) -> None:
    body = api_client.get("/api/ledgers/fx").json()
    assert body["total_count"] == 1
    fx = body["rows"][0]
    assert fx["from_ccy"] == "TWD" and fx["from_amt"] == "32000"
    assert fx["to_ccy"] == "USD" and fx["to_amt"] == "1000"
    assert fx["implied_rate"] == "32" and fx["account"] == "Charles Schwab"


def test_openings_empty_and_shape(api_client: TestClient) -> None:
    body = api_client.get("/api/ledgers/openings").json()
    assert body["total_count"] == 0 and body["rows"] == []
```

- [ ] **Step 2: run, expect fail** (404 on /fx, /openings).

- [ ] **Step 3: implement** — append to `ledgers.py`:
```python
@router.get("/ledgers/fx")
def fx(
    account_id: str | None = None,
    frm: str | None = Query(None, alias="from"), to: str | None = None,
    limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    bad = _check_dates(frm, to)
    if bad is not None:
        return bad
    accts, _names_, _ccys = _names(conn)
    out: list[dict[str, Any]] = []
    for c in list_fx_conversions(conn, account_id=account_id):
        if not _in_range(c.date, frm, to):
            continue
        out.append({
            "id": c.id, "date": c.date.isoformat(), "account_id": c.account_id,
            "account": accts.get(c.account_id, c.account_id),
            "from_ccy": c.from_ccy.value, "from_amt": str(c.from_amount),
            "to_ccy": c.to_ccy.value, "to_amt": str(c.to_amount),
            "implied_rate": str(c.implied_rate),
        })
    return _page(out, limit, offset)


@router.get("/ledgers/openings")
def openings(
    account_id: str | None = None, symbol: str | None = None,
    limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    accts, names, ccys = _names(conn)
    out: list[dict[str, Any]] = []
    for o in list_opening(conn, account_id=account_id):
        if symbol is not None and o.symbol != symbol:
            continue
        out.append({
            "date": o.build_date.isoformat(), "account_id": o.account_id,
            "account": accts.get(o.account_id, o.account_id), "symbol": o.symbol,
            "name": names.get(o.symbol, ""), "shares": str(o.shares),
            "avg": str(o.original_avg_cost), "total": str(o.original_cost_total),
            "ccy": ccys.get(o.symbol, ""),
        })
    # openings has no DB id; assign a 1-based display index over the presented (desc) order.
    paged = _page(out, limit, offset)
    for i, row in enumerate(paged["rows"], start=1):
        row["id"] = i
    return paged
```
> `list_opening` accepts only `account_id` (not `symbol`), so the symbol filter is applied in Python. The synthetic `id` is a display key (the table's real PK is account_id+symbol).

- [ ] **Step 4: run, expect pass** → all ledger tests PASS (6 total).

- [ ] **Step 5: gates + commit**
```bash
./.venv/Scripts/python.exe -m pytest tests/contract/test_ledgers_api.py -v
./.venv/Scripts/python.exe -m mypy portfolio_dash tests --strict
./.venv/Scripts/python.exe -m ruff check portfolio_dash tests
git add portfolio_dash/api/routers/ledgers.py tests/contract/test_ledgers_api.py
git commit -m "feat(api): GET /api/ledgers/{fx,openings} (spec 11)"
```

---

### Task 3: CHANGELOG + full green

- [ ] **Step 1:** Append to `CHANGELOG.md` `[Unreleased] › ### Added` (after the spec-10 bullet):
```markdown
- **Ledgers read API (spec 11, Phase 1):** `GET /api/ledgers/{transactions,dividends,fx,openings}`
  read-only over the four append-only ledgers — account-name join, account/symbol/date-range
  filters, desc pagination (`limit`/`offset`/`total_count`), the buy/sell `total` sign convention,
  `implied_rate`, and the **lowercase wire format** for `side`/`type` (Currency stays uppercase).
  Reuses the existing `transactions.fee_rule_snapshot` column (mapped to API `fee_snapshot`) — no
  new column. No write routes (writes are spec 12 only).
```
- [ ] **Step 2:** `grep -c "^## \[v" CHANGELOG.md` → `1`.
- [ ] **Step 3:** `./.venv/Scripts/python.exe -m ruff check portfolio_dash tests && ./.venv/Scripts/python.exe -m mypy portfolio_dash tests --strict && ./.venv/Scripts/python.exe -m pytest -q` → clean, 0 failed.
- [ ] **Step 4:** `git add CHANGELOG.md && git commit -m "docs: CHANGELOG for ledgers read API (spec 11)"`

## Self-review
- Spec 11 coverage: 4 read endpoints (Tasks 1–2), filters + desc pagination + date-range 400, lowercase side/type, account-name join, total sign, implied_rate, fee_snapshot passthrough. Deferred (noted): standardized fee_snapshot+label (write path, spec 12); `name` join assumes ingestion guarantees the instrument exists.
- Consistency: `_names`/`_page`/`_check_dates`/`_in_range` shared across all four endpoints; `from` is a reserved word so the query param uses `alias="from"` with python name `frm`; money fields all Decimal→string; enum lowercasing localized (to_wire untouched, Currency stays upper).
