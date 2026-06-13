# Spec 10 — Instruments API Implementation Plan (Phase 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Expose the instrument registry over `/api/instruments` (list with live price/held/target, TW board probe, register, update), with the schema/model additions the design requires (`target_low`, `is_etf`, `board_status`).

**Architecture:** Thin routers (decision B) wrapping the existing `data_ingestion.store` + `data_ingestion.register.register_instrument` (injected `pricing.board.probe_tw_board`) + `pricing.store` price reads. New columns added via the existing idempotent `_add_column_if_missing` migration. `is_etf` becomes the single source of truth for ETF (no more `sector=="ETF"`). Per CLAUDE.md the web layer reads/serializes; it does not compute.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, sqlite3, Decimal, pytest + FastAPI TestClient, mypy --strict, ruff.

**CRITICAL — interpreter:** all gates via `./.venv/Scripts/python.exe -m pytest|mypy|ruff`.

**Branch:** `feat/instruments-api` (already created off main @ Phase 0).

**Authoritative contract:** `docs/design-handoff/ai-portfolio-watcher/project/specs/10-instruments.md` (committed). The exact JSON response shapes are reproduced per-task below; the spec doc is the tie-breaker.

## Verified existing shapes (do not re-derive)
- `data_ingestion.store`: `upsert_instrument(conn, inst)`, `get_instrument(conn, symbol) -> Instrument | None`, `list_instruments(conn) -> list[Instrument]`, `_row_to_instrument(row)`. The `instruments` table columns today: `symbol, market, quote_ccy, sector, name, board`. `upsert_instrument` writes exactly those 6.
- `data_ingestion.register.register_instrument(conn, instrument, *, prober=None, confirm=False) -> InstrumentDraft`; `InstrumentDraft(instrument: Instrument, issues: list[Issue], written: bool)`. It fills board (US→"", MY→".KL", TW→prober(symbol) or "") unless `instrument.board` already set; appends a soft `Issue(kind="board_unresolved", needs_confirm=True, ...)` when TW board ends empty; upserts on `confirm` if no hard issues.
- `pricing.board.probe_tw_board(symbol, *, twse=None, tpex=None) -> str | None` → "TWSE"/"TPEx"/None.
- `data_ingestion.holdings.current_shares(conn, account_id, symbol) -> Decimal`.
- `pricing.store.get_latest_price(conn, instrument, *, now) -> PriceRead | None` (`.value`, `.as_of`, `.stale`); `get_price_history(conn, instrument, start, end) -> list[PriceRead]`.
- `shared.models.assets.Instrument(symbol, market: Market, quote_ccy: Currency, sector: str, name: str, board: str = "")`.
- API deps (Phase 0): `get_conn`, `get_now`, `get_reporting`; `api.serialize.to_wire`; `api.errors.error_body`. Test fixtures: `api_client`, `golden_db`, `GOLDEN_NOW` in `tests/conftest.py`.
- `data_ingestion.schema._add_column_if_missing(conn, table, column, decl)` + `create_tables(conn)`.

## Design decisions locked for this plan
1. **`target_low` and `is_etf` go on the core `Instrument` model** (intrinsic attributes): `target_low: Decimal | None = None`, `is_etf: bool = False`. `upsert_instrument`/`_row_to_instrument` round-trip them.
2. **`board_status` is registration metadata, NOT on the core `Instrument` model.** It is a column (`'resolved'`/`'unresolved'`) written by `register_instrument` and read directly by the instruments router. Rationale: keeps the core model (used across calc) free of UI/registration state. `register_instrument` sets `board_status='unresolved'` exactly when a TW instrument ends with an empty board, else `'resolved'`.
3. **Wire board serialization** (spec 10.1): the router emits `board = null` for a TW instrument whose `board_status='unresolved'`; otherwise the stored board string (`""` for US, `".KL"` for MY, `"TWSE"/"TPEx"` for TW).
4. **No enum-lowercase concern here** (instruments carry `market`/`board`/`ccy`, not Side/DividendType) — that wire layer arrives with spec 11.

---

### Task 1: schema + model + store + register board_status

**Files:** Modify `portfolio_dash/data_ingestion/schema.py`, `portfolio_dash/shared/models/assets.py`, `portfolio_dash/data_ingestion/store.py`, `portfolio_dash/data_ingestion/register.py`; Test `tests/data_ingestion/test_instruments_schema.py` (create).

- [ ] **Step 1: failing test**

Create `tests/data_ingestion/test_instruments_schema.py`:
```python
import sqlite3
from decimal import Decimal

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.register import register_instrument
from portfolio_dash.data_ingestion.store import get_instrument, upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    return c


def test_instrument_new_fields_default(conn: sqlite3.Connection) -> None:
    inst = Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                      sector="Semis", name="TSMC", board="TWSE")
    assert inst.target_low is None and inst.is_etf is False
    upsert_instrument(conn, inst)
    got = get_instrument(conn, "2330")
    assert got is not None and got.target_low is None and got.is_etf is False


def test_instrument_fields_round_trip(conn: sqlite3.Connection) -> None:
    inst = Instrument(symbol="0056", market=Market.TW, quote_ccy=Currency.TWD,
                      sector="ETF", name="高股息", board="TWSE",
                      target_low=Decimal("36.50"), is_etf=True)
    upsert_instrument(conn, inst)
    got = get_instrument(conn, "0056")
    assert got is not None and got.target_low == Decimal("36.50") and got.is_etf is True


def test_register_sets_board_status_unresolved_for_tw_without_board(conn: sqlite3.Connection) -> None:
    inst = Instrument(symbol="8069", market=Market.TW, quote_ccy=Currency.TWD,
                      sector="Optoelectronics", name="元太")
    draft = register_instrument(conn, inst, prober=lambda _s: None, confirm=True)
    assert draft.written is True
    row = conn.execute("SELECT board, board_status FROM instruments WHERE symbol='8069'").fetchone()
    assert row["board"] == "" and row["board_status"] == "unresolved"


def test_register_sets_board_status_resolved_for_us(conn: sqlite3.Connection) -> None:
    inst = Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                      sector="Tech", name="Apple")
    register_instrument(conn, inst, confirm=True)
    row = conn.execute("SELECT board, board_status FROM instruments WHERE symbol='AAPL'").fetchone()
    assert row["board"] == "" and row["board_status"] == "resolved"
```

- [ ] **Step 2: run, expect fail** — `./.venv/Scripts/python.exe -m pytest tests/data_ingestion/test_instruments_schema.py -v` → FAIL (no `target_low`/`is_etf` on model; no `board_status` column).

- [ ] **Step 3: migration.** In `portfolio_dash/data_ingestion/schema.py`, add the three columns to the `instruments` `CREATE TABLE` (`target_low TEXT`, `board_status TEXT`, `is_etf INTEGER`) AND add idempotent migrations in `create_tables` after the existing board migration:
```python
    _add_column_if_missing(conn, "instruments", "board", "TEXT")  # migrate legacy DBs
    _add_column_if_missing(conn, "instruments", "target_low", "TEXT")
    _add_column_if_missing(conn, "instruments", "board_status", "TEXT NOT NULL DEFAULT 'resolved'")
    _add_column_if_missing(conn, "instruments", "is_etf", "INTEGER NOT NULL DEFAULT 0")
```
Also add the columns to the `CREATE TABLE IF NOT EXISTS instruments (...)` DDL string so fresh DBs have them:
```sql
CREATE TABLE IF NOT EXISTS instruments (
    symbol TEXT PRIMARY KEY, market TEXT NOT NULL, quote_ccy TEXT NOT NULL,
    sector TEXT, name TEXT, board TEXT,
    target_low TEXT, board_status TEXT NOT NULL DEFAULT 'resolved',
    is_etf INTEGER NOT NULL DEFAULT 0
);
```

- [ ] **Step 4: model.** In `portfolio_dash/shared/models/assets.py`, add to `Instrument` (after `board`):
```python
    target_low: Decimal | None = None  # price-alert floor (spec 10)
    is_etf: bool = False  # single source of truth for ETF (never derive from sector)
```
Add `from decimal import Decimal` import at top if absent.

- [ ] **Step 5: store r/w.** In `portfolio_dash/data_ingestion/store.py`:
  - `upsert_instrument`: extend the INSERT column list + conflict update to include `target_low`, `is_etf` (NOT `board_status` — that is set by `register_instrument`, see Step 6). Use `to_db(inst.target_low) if inst.target_low is not None else None` and `1 if inst.is_etf else 0`. Preserve any existing `board_status` on conflict by NOT overwriting it here.
```python
def upsert_instrument(conn: sqlite3.Connection, inst: Instrument) -> None:
    """Insert or update an instrument row (idempotent). board_status is owned by
    register_instrument and intentionally not written here (preserved on conflict)."""
    conn.execute(
        """INSERT INTO instruments (symbol, market, quote_ccy, sector, name, board,
               target_low, is_etf)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(symbol) DO UPDATE SET
               market=excluded.market, quote_ccy=excluded.quote_ccy,
               sector=excluded.sector, name=excluded.name, board=excluded.board,
               target_low=excluded.target_low, is_etf=excluded.is_etf""",
        (
            inst.symbol, inst.market.value, inst.quote_ccy.value,
            inst.sector, inst.name, inst.board,
            to_db(inst.target_low) if inst.target_low is not None else None,
            1 if inst.is_etf else 0,
        ),
    )
    conn.commit()
```
  - `_row_to_instrument`: map the new fields:
```python
def _row_to_instrument(row: sqlite3.Row) -> Instrument:
    return Instrument(
        symbol=row["symbol"], market=Market(row["market"]),
        quote_ccy=Currency(row["quote_ccy"]), sector=row["sector"], name=row["name"],
        board=row["board"] or "",
        target_low=from_db(row["target_low"]) if row["target_low"] else None,
        is_etf=bool(row["is_etf"]),
    )
```
  - `get_instrument`/`list_instruments`: add `target_low, is_etf` to the SELECT column lists (board_status not needed by the core model reads).

- [ ] **Step 6: register sets board_status.** In `portfolio_dash/data_ingestion/register.py`, after computing `board` and before/at the upsert, persist `board_status`. Since `upsert_instrument` does not write `board_status`, write it explicitly in `register_instrument` when `confirm` and written:
```python
    inst = instrument.model_copy(update={"board": board})
    written = False
    hard = [i for i in issues if not i.needs_confirm]
    if confirm and not hard:
        upsert_instrument(conn, inst)
        status = "unresolved" if (inst.market is Market.TW and not board) else "resolved"
        conn.execute("UPDATE instruments SET board_status=? WHERE symbol=?",
                     (status, inst.symbol))
        conn.commit()
        written = True
    return InstrumentDraft(instrument=inst, issues=issues, written=written)
```

- [ ] **Step 7: run, expect pass** — `./.venv/Scripts/python.exe -m pytest tests/data_ingestion/test_instruments_schema.py -v` → 4 PASS. Then existing instrument tests: `./.venv/Scripts/python.exe -m pytest tests/data_ingestion -q` → green (existing `Instrument(...)` constructions still valid via defaults).

- [ ] **Step 8: gates + commit**
```bash
./.venv/Scripts/python.exe -m mypy portfolio_dash tests --strict
./.venv/Scripts/python.exe -m ruff check portfolio_dash tests
git add portfolio_dash/data_ingestion/schema.py portfolio_dash/shared/models/assets.py portfolio_dash/data_ingestion/store.py portfolio_dash/data_ingestion/register.py tests/data_ingestion/test_instruments_schema.py
git commit -m "feat(instruments): target_low/is_etf model+schema + board_status registration column (spec 10)"
```

---

### Task 2: GET /api/instruments

**Files:** Create `portfolio_dash/api/routers/instruments.py`; Modify `portfolio_dash/api/app.py` (include router); Test `tests/contract/test_instruments_api.py`.

Response shape (spec 10.1): `{ "as_of": <iso>, "list": [ {symbol, name, market, board, sector, ccy, held: bool, last: str|null, chg_pct: str|null, target_low: str|null} ] }`. `board` is `null` when TW + `board_status='unresolved'`. `last`/`chg_pct` null when no price. `held` = any account holds > 0 shares.

- [ ] **Step 1: failing test**

Create `tests/contract/test_instruments_api.py`:
```python
from fastapi.testclient import TestClient


def test_instruments_list_shape_and_enrichment(api_client: TestClient) -> None:
    r = api_client.get("/api/instruments")
    assert r.status_code == 200
    body = r.json()
    assert "as_of" in body
    by_symbol = {i["symbol"]: i for i in body["list"]}
    # golden_db has 2330 (held, TWSE, priced 600) and AAPL (held, US, priced 120).
    tsmc = by_symbol["2330"]
    assert tsmc["name"] == "TSMC" and tsmc["market"] == "TW" and tsmc["board"] == "TWSE"
    assert tsmc["ccy"] == "TWD" and tsmc["held"] is True
    assert tsmc["last"] == "600"          # Decimal string
    aapl = by_symbol["AAPL"]
    assert aapl["board"] == "" and aapl["held"] is True and aapl["last"] == "120"
```

- [ ] **Step 2: run, expect fail** (404).

- [ ] **Step 3: implement** `portfolio_dash/api/routers/instruments.py`:
```python
"""Instruments registry API (spec 10): list + probe + register/update. Thin over store."""

import sqlite3
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends

from portfolio_dash.api.deps import get_conn, get_now
from portfolio_dash.data_ingestion.holdings import current_shares
from portfolio_dash.data_ingestion.store import list_accounts, list_instruments
from portfolio_dash.pricing.store import get_latest_price, get_price_history
from portfolio_dash.shared.models.assets import Instrument

router = APIRouter()


def _held(conn: sqlite3.Connection, account_ids: list[str], symbol: str) -> bool:
    return any(current_shares(conn, aid, symbol) > 0 for aid in account_ids)


def _board_wire(conn: sqlite3.Connection, inst: Instrument) -> str | None:
    row = conn.execute("SELECT board_status FROM instruments WHERE symbol=?",
                       (inst.symbol,)).fetchone()
    status = row["board_status"] if row is not None else "resolved"
    if inst.market.value == "TW" and status == "unresolved":
        return None
    return inst.board


@router.get("/instruments")
def list_all(
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    account_ids = [a.account_id for a in list_accounts(conn)]
    out: list[dict[str, Any]] = []
    for inst in list_instruments(conn):
        pr = get_latest_price(conn, inst.symbol, now=now)
        last = str(pr.value) if pr is not None else None
        chg_pct: str | None = None
        if pr is not None:
            hist = get_price_history(conn, inst.symbol, pr.as_of.replace(day=1), pr.as_of)
            if len(hist) >= 2 and hist[-2].value != 0:
                chg_pct = str((hist[-1].value - hist[-2].value) / hist[-2].value)
        out.append({
            "symbol": inst.symbol, "name": inst.name, "market": inst.market.value,
            "board": _board_wire(conn, inst), "sector": inst.sector,
            "ccy": inst.quote_ccy.value, "held": _held(conn, account_ids, inst.symbol),
            "last": last, "chg_pct": chg_pct,
            "target_low": str(inst.target_low) if inst.target_low is not None else None,
        })
    return {"as_of": now.isoformat(), "list": out}
```
> `chg_pct` uses the two most recent stored closes; if `get_price_history` over the available window returns < 2 points, leave it `null` (golden_db has one price per symbol → `chg_pct` null, `last` present). Keep the date window simple (month-start to as_of); the precise lookback is refined when historical backfill is richer.

In `portfolio_dash/api/app.py` add `instruments` to the routers import and `app.include_router(instruments.router, prefix="/api")`.

- [ ] **Step 4: run, expect pass** (adjust: golden_db has single prices so `chg_pct` is null — the test only asserts `last`). → PASS.

- [ ] **Step 5: gates + commit**
```bash
./.venv/Scripts/python.exe -m mypy portfolio_dash tests --strict
./.venv/Scripts/python.exe -m ruff check portfolio_dash tests
git add portfolio_dash/api/routers/instruments.py portfolio_dash/api/app.py tests/contract/test_instruments_api.py
git commit -m "feat(api): GET /api/instruments (list + held + last + board wire) (spec 10.1)"
```

---

### Task 3: POST /api/instruments/probe

**Files:** Modify `portfolio_dash/api/routers/instruments.py`; Test append to `tests/contract/test_instruments_api.py`.

Spec 10.2: body `{ "symbol": "2330" }` → `{ "symbol", "name": null, "board": "TWSE"|null, "board_label": "TWSE 上市"|"未解析" }`. The probe uses `pricing.board.probe_tw_board`. For tests, the route must accept an injectable prober so no live network is hit.

- [ ] **Step 1: failing test** (append):
```python
from portfolio_dash.api.routers import instruments as instruments_router


def test_probe_returns_board(api_client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(instruments_router, "probe_tw_board", lambda s, **k: "TPEx")
    r = api_client.post("/api/instruments/probe", json={"symbol": "6488"})
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "6488" and body["board"] == "TPEx"
    assert body["board_label"] == "TPEx 上櫃"


def test_probe_unresolved(api_client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(instruments_router, "probe_tw_board", lambda s, **k: None)
    r = api_client.post("/api/instruments/probe", json={"symbol": "9999"})
    assert r.json()["board"] is None and r.json()["board_label"] == "未解析"
```

- [ ] **Step 2: run, expect fail** (404).

- [ ] **Step 3: implement.** Add to `instruments.py` (import `probe_tw_board` at module level so the test can monkeypatch it; add `from pydantic import BaseModel`):
```python
from pydantic import BaseModel

from portfolio_dash.pricing.board import probe_tw_board

_BOARD_LABEL = {"TWSE": "TWSE 上市", "TPEx": "TPEx 上櫃"}


class ProbeBody(BaseModel):
    symbol: str


@router.post("/instruments/probe")
def probe(body: ProbeBody) -> dict[str, Any]:
    sym = body.symbol.strip()
    if not sym:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="symbol 不可為空")
    board = probe_tw_board(sym)
    return {"symbol": sym, "name": None, "board": board,
            "board_label": _BOARD_LABEL.get(board or "", "未解析")}
```
> Network failure inside `probe_tw_board` returns `None` (it swallows errors), so the probe degrades to "未解析" rather than 503 — acceptable for v1 (spec notes provider-failure handling; the swallow keeps it a 200 unresolved). Keep it simple.

- [ ] **Step 4: run, expect pass.** **Step 5: gates + commit**
```bash
git add portfolio_dash/api/routers/instruments.py tests/contract/test_instruments_api.py
git commit -m "feat(api): POST /api/instruments/probe (TW board probe) (spec 10.2)"
```

---

### Task 4: POST /api/instruments + PUT /api/instruments/{symbol}

**Files:** Modify `portfolio_dash/api/routers/instruments.py`; Test append.

Spec 10.3: POST registers (probe-confirmed); PUT updates (board/sector/name/target_low). Both via `register_instrument`/`upsert_instrument`. 409 duplicate on POST; 404 on PUT missing; 400 on illegal market/board combo (US/MY with a TW board).

- [ ] **Step 1: failing test** (append):
```python
def test_register_new_instrument(api_client: TestClient) -> None:
    r = api_client.post("/api/instruments", json={
        "symbol": "6488", "market": "TW", "name": "環球晶", "sector": "Semis",
        "board": "TPEx", "quote_ccy": "TWD", "target_low": "450"})
    assert r.status_code == 201
    body = r.json()
    assert body["symbol"] == "6488" and body["board"] == "TPEx" and body["target_low"] == "450"


def test_register_duplicate_409(api_client: TestClient) -> None:
    r = api_client.post("/api/instruments", json={"symbol": "2330", "market": "TW",
                                                  "name": "x", "sector": "y", "board": "TWSE"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "duplicate_symbol"


def test_put_updates_target_low(api_client: TestClient) -> None:
    r = api_client.put("/api/instruments/2330", json={"target_low": "550"})
    assert r.status_code == 200 and r.json()["target_low"] == "550"


def test_put_missing_404(api_client: TestClient) -> None:
    r = api_client.put("/api/instruments/NOPE", json={"sector": "z"})
    assert r.status_code == 404
```

- [ ] **Step 2: run, expect fail.**

- [ ] **Step 3: implement.** Add to `instruments.py` (reuse the list element shape via a helper `_one(conn, inst)` that returns the same dict as Task 2's list element, so POST/PUT echo the registered row). Default `quote_ccy` from market when omitted (TW→TWD, US→USD, MY→MYR). Validate US/MY must not carry a TW board (`TWSE`/`TPEx`) → 400. Duplicate symbol on POST → 409. PUT on missing symbol → 404; PUT applies the provided subset then re-upserts.
```python
from portfolio_dash.data_ingestion.register import register_instrument
from portfolio_dash.data_ingestion.store import get_instrument, upsert_instrument
from portfolio_dash.shared.enums import Currency, Market

_DEFAULT_CCY = {Market.TW: Currency.TWD, Market.US: Currency.USD, Market.MY: Currency.MYR}
_TW_BOARDS = {"TWSE", "TPEx"}


class RegisterBody(BaseModel):
    symbol: str
    market: Market
    name: str = ""
    sector: str = ""
    board: str | None = None
    quote_ccy: Currency | None = None
    target_low: Decimal | None = None
    is_etf: bool = False


def _one(conn: sqlite3.Connection, inst: Instrument, now: datetime) -> dict[str, Any]:
    # same element shape as GET /api/instruments list
    ...  # build the dict exactly like list_all's per-row dict (extract a shared helper)


@router.post("/instruments", status_code=201)
def register(body: RegisterBody, conn=Depends(get_conn), now=Depends(get_now)) -> dict[str, Any]:
    from fastapi import HTTPException
    if get_instrument(conn, body.symbol) is not None:
        raise HTTPException(status_code=409, detail=f"{body.symbol} 已註冊")  # -> duplicate? see note
    if body.market in (Market.US, Market.MY) and (body.board or "") in _TW_BOARDS:
        raise HTTPException(status_code=400, detail="US/MY 不可帶台股板別")
    ccy = body.quote_ccy or _DEFAULT_CCY[body.market]
    inst = Instrument(symbol=body.symbol, market=body.market, quote_ccy=ccy,
                      sector=body.sector, name=body.name, board=body.board or "",
                      target_low=body.target_low, is_etf=body.is_etf)
    register_instrument(conn, inst, prober=probe_tw_board, confirm=True)
    saved = get_instrument(conn, body.symbol)
    assert saved is not None
    return _one(conn, saved, now)
```
> **409 code:** the Phase-0 error handler maps status 409 to code `"unprocessable"`? No — check `api/errors.py` `_STATUS_CODE`: it has no 409 entry, so the generic handler yields code `"error"`. The test wants `"duplicate_symbol"`. Therefore raise via a small custom path: return `JSONResponse(status_code=409, content=error_body("duplicate_symbol", "..."))` directly from the route (import `from fastapi.responses import JSONResponse` and `from portfolio_dash.api.errors import error_body`) instead of `HTTPException`. Do the same pattern for the 400 (`error_body("validation_error", ...)`) and PUT 404 (`error_body("not_found", ...)`) so the envelope codes match the spec. Implement PUT analogously: load existing (404 if missing via JSONResponse), apply the provided subset (symbol/market immutable), re-`upsert_instrument`, echo `_one`.

Extract the per-row dict construction from Task 2's `list_all` into a module-level `_one(conn, inst, now)` and call it from `list_all`, `register`, and `update` (DRY — one source for the element shape).

- [ ] **Step 4: run, expect pass.** **Step 5: full gates + commit**
```bash
./.venv/Scripts/python.exe -m pytest tests/contract/test_instruments_api.py -v
./.venv/Scripts/python.exe -m mypy portfolio_dash tests --strict
./.venv/Scripts/python.exe -m ruff check portfolio_dash tests
git add portfolio_dash/api/routers/instruments.py tests/contract/test_instruments_api.py
git commit -m "feat(api): POST/PUT /api/instruments register+update (spec 10.3)"
```

---

### Task 5: CHANGELOG + full green

- [ ] **Step 1:** Append to `CHANGELOG.md` `[Unreleased] › ### Added`:
```markdown
- **Instruments API (spec 10):** `GET/POST/PUT /api/instruments` + `POST /api/instruments/probe`
  over the existing registry (`register_instrument` + injected `probe_tw_board`); list enriches
  with held flag, latest price, and `chg_pct`; TW board serializes `null` until confirmed.
  Schema/model: `instruments += target_low/board_status/is_etf` (idempotent migration);
  `is_etf` is the single source of truth for ETF (no `sector=="ETF"` derivation).
```
- [ ] **Step 2:** `grep -c "^## \[v" CHANGELOG.md` → `1`.
- [ ] **Step 3:** `./.venv/Scripts/python.exe -m ruff check portfolio_dash tests && ./.venv/Scripts/python.exe -m mypy portfolio_dash tests --strict && ./.venv/Scripts/python.exe -m pytest -q` → ruff/mypy clean, 0 failed.
- [ ] **Step 4:** `git add CHANGELOG.md && git commit -m "docs: CHANGELOG for instruments API (spec 10)"`

## Self-review
- Spec 10 coverage: 10.1 list (Task 2), 10.2 probe (Task 3), 10.3 register/update (Task 4), schema/model migration + board_status + is_etf SoT (Task 1). Deferred (noted): `q` filter (v1 optional per spec), `held` via SQL aggregate optimization (current per-symbol loop is fine at this scale), `chg_pct` precise lookback (refined when richer history exists).
- Type consistency: `_one(conn, inst, now)` is the single element-shape source used by list/register/update; `RegisterBody`/`ProbeBody` Pydantic; 409/400/404 emit the spec's envelope codes via `error_body` + `JSONResponse` (not bare HTTPException, whose generic mapping lacks `duplicate_symbol`).
