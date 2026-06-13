# Spec 12a — Input Center: Context + Manual Entry (Phase 1)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** Build the input-center read context (`GET /api/input/context`) and the manual single-transaction write path (`POST /api/input/manual/preview` + `/commit`) over the existing `enter_transaction` pipeline, with the API-layer wire mappings the design needs (lowercase enum in/out, `Issue` → `{sev,code,text,field}`, fee-rule + dividend-model serialization, ack-gated commit).

**Architecture:** Thin routers over existing `data_ingestion` (`enter_transaction`, `validate`, `compute_fees`, `store`) + `config_seed`. The API layer adds presentation mappers; it does not compute money of record. This is sub-project **12a** of spec 12; **12b** (CSV import + AI input) follows and reuses the mappers built here.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, sqlite3, Decimal, pytest + TestClient, mypy --strict, ruff. Gates via `./.venv/Scripts/python.exe -m ...`.

**Branch:** `feat/input-manual` (create off main @ spec 11).

**Authoritative contract:** `docs/design-handoff/.../specs/12-input-center.md` §12.1–12.2.

## Reconciliations locked (decided 2026-06-13; see analysis)
1. **Issue shape:** existing `Issue{kind, message, needs_confirm}` → API `{sev, code, text, field}`. Map: `code=kind`, `text=message`, `sev = "warn" if needs_confirm else "error"`, `field` via a `kind→field` table (`sell_exceeds_holdings`/`non_positive_quantity`→`"shares"`, `non_positive_price`→`"price"`, `unknown_account`→`"account_id"`, `parse_error`/`symbol_unresolved`→`None`). Build a shared `issue_wire(Issue) -> dict` in `api/wire.py`.
2. **Enum lowercase deserialize:** request `side: "buy"|"sell"` → `Side` via `Side(s.upper())` (a shared `parse_side` in `api/wire.py`). Responses lowercase via `.value.lower()`.
3. **Manual ack enforcement (API-side):** `enter_transaction(confirm=True)` writes even with unacked soft issues, so the COMMIT handler enforces ack itself: run `enter_transaction(confirm=False)` → if any hard issue (`needs_confirm=False`) → 400 with issues; elif a `sell_exceeds_holdings` soft issue and `ack_oversell` is False → 422 `oversell_unacknowledged`; else `enter_transaction(confirm=True)` to write.
4. **`dividend_model` value mapping:** the `accounts.dividend_model` column stores `cash_cost_reduction`/`drip_us`/`cash`; the frontend `div_model` wants `tw`/`drip`/`net`. Map: `cash_cost_reduction→"tw"`, `drip_us→"drip"`, `cash→"net"` (in `api/wire.py`, shared with spec 13).
5. **`fee_rules` serialization:** `FeeRuleSet` → `{rate, discount, min_fee, round_int, tax_sell, tax_sell_etf, label}` (shared `fee_rules_wire` in `api/wire.py`, reused by spec 13). `rate=brokerage`, `round_int=round_integer`, `tax_sell=tax_normal`, `tax_sell_etf=tax_etf`; `label` synthesized per market.

## Verified shapes
- `enter_transaction(conn, inp: TxnInput, *, confirm=False) -> TxnDraft` (`TxnDraft{inp, instrument, fee, tax, fee_rule_snapshot, issues, written, transaction_id}`). On `confirm` it writes iff no hard issues, bypassing soft issues.
- `TxnInput{account_id, symbol, side: Side, quantity, price, trade_date, fee: Decimal|None, tax: Decimal|None, daytrade=False, is_etf=False, note=None}`.
- `Issue{kind, message, needs_confirm=False}`. Soft = `needs_confirm=True`.
- `store.list_accounts(conn) -> list[Account]`; `store.list_instruments(conn) -> list[Instrument]` (`.is_etf` exists). `holdings.current_shares(conn, account_id, symbol) -> Decimal`.
- `accounts` table columns include `dividend_model` and `fee_rule_set`. `config_seed.get_fee_rule_set(name) -> FeeRuleSet`; `FeeRuleSet{market, brokerage, discount, min_fee, tax_normal, tax_etf, tax_daytrade, sec_fee, flat_fee, clearing, clearing_cap, stamp_duty_rate, stamp_duty_cap, sst, round_integer}`.
- Phase-0 api: `get_conn` dep; `api/errors.error_body`; `fastapi.responses.JSONResponse`. Fixture `api_client`/`golden_db` (tw_broker holds 2330 ×1000; schwab holds AAPL ×10).

---

### Task 1: `api/wire.py` shared mappers + `GET /api/input/context`

**Files:** Create `portfolio_dash/api/wire.py`, `portfolio_dash/api/routers/input_center.py`; Modify `portfolio_dash/api/app.py`; Test `tests/contract/test_input_context_api.py`.

- [ ] **Step 1: failing test**
```python
from fastapi.testclient import TestClient


def test_input_context_shape(api_client: TestClient) -> None:
    r = api_client.get("/api/input/context")
    assert r.status_code == 200
    b = r.json()
    accts = {a["id"]: a for a in b["accounts"]}
    assert accts["tw_broker"]["div_model"] == "tw" and accts["tw_broker"]["ccy"] == "TWD"
    assert accts["schwab"]["div_model"] == "drip"
    assert accts["moomoo_my_my"]["div_model"] == "net"
    fr = b["fee_rules"]["tw_broker"]
    assert fr["rate"] == "0.001425" and fr["min_fee"] == "20" and fr["round_int"] is True
    assert fr["tax_sell"] == "0.003" and "label" in fr
    insts = {i["symbol"]: i for i in b["instruments"]}
    assert insts["2330"]["etf"] is False and insts["2330"]["ccy"] == "TWD"
    assert b["holdings"]["tw_broker"]["2330"] == "1000"
    assert b["holdings"]["schwab"]["AAPL"] == "10"
```

- [ ] **Step 2: run, expect fail** (404).

- [ ] **Step 3: implement** `portfolio_dash/api/wire.py`:
```python
"""Shared API wire mappers: enum case, Issue shape, fee-rule + dividend-model serialization."""

from typing import Any

from portfolio_dash.data_ingestion.config_seed import FeeRuleSet
from portfolio_dash.data_ingestion.validate import Issue
from portfolio_dash.shared.enums import Market
from portfolio_dash.shared.models.enums import Side

_ISSUE_FIELD = {
    "sell_exceeds_holdings": "shares", "non_positive_quantity": "shares",
    "non_positive_price": "price", "unknown_account": "account_id",
}

_DIV_MODEL = {"cash_cost_reduction": "tw", "drip_us": "drip", "cash": "net"}


def parse_side(value: str) -> Side:
    """Accept lowercase/any-case wire side ('buy'/'sell') -> core Side enum."""
    return Side(value.strip().upper())


def issue_wire(issue: Issue) -> dict[str, Any]:
    """Map the core Issue to the frontend's {sev, code, text, field} shape."""
    return {
        "sev": "warn" if issue.needs_confirm else "error",
        "code": issue.kind,
        "text": issue.message,
        "field": _ISSUE_FIELD.get(issue.kind),
    }


def div_model_wire(dividend_model: str) -> str:
    """Map the stored accounts.dividend_model to the frontend div_model (tw/drip/net)."""
    return _DIV_MODEL.get(dividend_model, dividend_model)


def _tw_label(r: FeeRuleSet) -> str:
    return (f"{r.brokerage * 100}%・最低 NT${r.min_fee}・"
            f"賣出證交稅 {r.tax_normal * 100}%（ETF {r.tax_etf * 100}%）")


def fee_rules_wire(r: FeeRuleSet) -> dict[str, Any]:
    """Serialize a FeeRuleSet to the frontend fee-rule shape (shared with spec 13)."""
    if r.market is Market.TW:
        label = _tw_label(r)
    elif r.market is Market.US:
        label = (f"平台費 USD {r.flat_fee}/筆" if r.flat_fee > 0
                 else f"$0 佣金 + SEC fee {r.sec_fee}")
    else:
        label = f"佣金 {r.brokerage * 100}%・清算 {r.clearing * 100}%・印花稅 {r.stamp_duty_rate * 100}%"
    return {
        "rate": str(r.brokerage), "discount": str(r.discount), "min_fee": str(r.min_fee),
        "round_int": r.round_integer, "tax_sell": str(r.tax_normal),
        "tax_sell_etf": str(r.tax_etf), "label": label,
    }
```
Create `portfolio_dash/api/routers/input_center.py`:
```python
"""Input center API (spec 12): read context + manual/CSV/AI write paths (12a: context+manual)."""

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends

from portfolio_dash.api.deps import get_conn
from portfolio_dash.api.wire import div_model_wire, fee_rules_wire
from portfolio_dash.data_ingestion.config_seed import get_fee_rule_set
from portfolio_dash.data_ingestion.holdings import current_shares
from portfolio_dash.data_ingestion.store import list_accounts, list_instruments

router = APIRouter()


@router.get("/input/context")
def context(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT account_id, settlement_ccy, fee_rule_set, dividend_model FROM accounts "
        "ORDER BY account_id"
    ).fetchall()
    accounts_meta = {r["account_id"]: r for r in rows}
    accts = [a for a in list_accounts(conn)]
    accounts_out = [
        {"id": a.account_id, "name": a.name,
         "ccy": a.settlement_ccy.value,
         "div_model": div_model_wire(accounts_meta[a.account_id]["dividend_model"])}
        for a in accts
    ]
    fee_rules = {
        aid: fee_rules_wire(get_fee_rule_set(m["fee_rule_set"]))
        for aid, m in accounts_meta.items()
    }
    instruments = [
        {"symbol": i.symbol, "name": i.name, "market": i.market.value,
         "ccy": i.quote_ccy.value, "etf": i.is_etf}
        for i in list_instruments(conn)
    ]
    holdings: dict[str, dict[str, str]] = {}
    insts = list_instruments(conn)
    for a in accts:
        per: dict[str, str] = {}
        for inst in insts:
            sh = current_shares(conn, a.account_id, inst.symbol)
            if sh != 0:
                per[inst.symbol] = str(sh)
        if per:
            holdings[a.account_id] = per
    return {"accounts": accounts_out, "fee_rules": fee_rules,
            "instruments": instruments, "holdings": holdings}
```
In `app.py`: import `input_center` and `app.include_router(input_center.router, prefix="/api")`.

- [ ] **Step 4: run, expect pass.** **Step 5: gates + commit**
```bash
./.venv/Scripts/python.exe -m pytest -q
./.venv/Scripts/python.exe -m mypy portfolio_dash tests --strict
./.venv/Scripts/python.exe -m ruff check portfolio_dash tests
git add portfolio_dash/api/wire.py portfolio_dash/api/routers/input_center.py portfolio_dash/api/app.py tests/contract/test_input_context_api.py
git commit -m "feat(api): GET /api/input/context + shared wire mappers (spec 12.1)"
```

---

### Task 2: `POST /api/input/manual/preview`

**Files:** Modify `portfolio_dash/api/routers/input_center.py`; Test `tests/contract/test_input_manual_api.py`.

Spec 12.2 preview → `{fee, tax, gross, total, fee_rule_label, fee_overridden, tax_overridden, issues:[{sev,code,text,field}]}`. Body: `{account_id, symbol, side, date, shares, price, fee_override, tax_override, note}`.

- [ ] **Step 1: failing test**
```python
from fastapi.testclient import TestClient


def test_manual_preview_buy_computes_fee_and_total(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-11", "shares": "1000", "price": "612.5"})
    assert r.status_code == 200
    b = r.json()
    assert b["fee"] == "873" and b["tax"] == "0"          # W1 worked example
    assert b["gross"] == "612500" and b["total"] == "-613373"
    assert b["fee_overridden"] is False and b["issues"] == []


def test_manual_preview_oversell_soft_issue(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "sell",
        "date": "2026-06-11", "shares": "5000", "price": "600"})
    b = r.json()
    codes = {i["code"]: i for i in b["issues"]}
    assert "sell_exceeds_holdings" in codes
    assert codes["sell_exceeds_holdings"]["sev"] == "warn"
    assert codes["sell_exceeds_holdings"]["field"] == "shares"


def test_manual_preview_fee_override(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-11", "shares": "1000", "price": "612.5",
        "fee_override": "500"})
    b = r.json()
    assert b["fee"] == "500" and b["fee_overridden"] is True
```

- [ ] **Step 2: run, expect fail.**

- [ ] **Step 3: implement** — add to `input_center.py` (imports + a shared body model + preview):
```python
from datetime import date
from decimal import Decimal

from pydantic import BaseModel

from portfolio_dash.api.wire import issue_wire, parse_side
from portfolio_dash.data_ingestion.manual import enter_transaction
from portfolio_dash.data_ingestion.validate import TxnInput


class ManualBody(BaseModel):
    account_id: str
    symbol: str
    side: str
    date: date
    shares: Decimal
    price: Decimal
    fee_override: Decimal | None = None
    tax_override: Decimal | None = None
    note: str | None = None
    ack_oversell: bool = False  # commit only


def _txn_input(body: ManualBody) -> TxnInput:
    return TxnInput(
        account_id=body.account_id, symbol=body.symbol, side=parse_side(body.side),
        quantity=body.shares, price=body.price, trade_date=body.date,
        fee=body.fee_override, tax=body.tax_override, note=body.note,
    )


@router.post("/input/manual/preview")
def manual_preview(body: ManualBody, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    draft = enter_transaction(conn, _txn_input(body), confirm=False)
    gross = body.shares * body.price
    total = (-(gross + draft.fee + draft.tax) if draft.inp.side.value == "BUY"
             else (gross - draft.fee - draft.tax))
    return {
        "fee": str(draft.fee), "tax": str(draft.tax), "gross": str(gross),
        "total": str(total),
        "fee_rule_label": fee_rules_wire(_rule_for(conn, body.account_id))["label"]
                          if _rule_for(conn, body.account_id) else None,
        "fee_overridden": body.fee_override is not None,
        "tax_overridden": body.tax_override is not None,
        "issues": [issue_wire(i) for i in draft.issues],
    }
```
Add a helper `_rule_for(conn, account_id) -> FeeRuleSet | None` reading the account's `fee_rule_set` then `get_fee_rule_set` (return None if account unknown). Import `get_fee_rule_set`, `FeeRuleSet` accordingly.

- [ ] **Step 4: run, expect pass** (W1: 1000×612.5 buy → fee 873, total -(612500+873) = -613373). **Step 5: gates + commit**
```bash
git add portfolio_dash/api/routers/input_center.py tests/contract/test_input_manual_api.py
git commit -m "feat(api): POST /api/input/manual/preview (issue wire, fee/total) (spec 12.2)"
```

---

### Task 3: `POST /api/input/manual/commit` (ack-gated write)

**Files:** Modify `portfolio_dash/api/routers/input_center.py`; Test append to `tests/contract/test_input_manual_api.py`.

Spec 12.2 commit → 201 `{txn_id, total}`. 400 hard errors (issues in envelope). 422 `oversell_unacknowledged`.

- [ ] **Step 1: failing test** (append):
```python
def test_manual_commit_writes(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/commit", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-11", "shares": "100", "price": "600"})
    assert r.status_code == 201
    b = r.json()
    assert isinstance(b["txn_id"], int) and b["total"].startswith("-")
    # written: appears in the ledger
    lg = api_client.get("/api/ledgers/transactions", params={"account_id": "tw_broker"}).json()
    assert lg["total_count"] == 2  # golden had 1 tw_broker txn + this one


def test_manual_commit_oversell_unacked_422(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/commit", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "sell",
        "date": "2026-06-11", "shares": "5000", "price": "600", "ack_oversell": False})
    assert r.status_code == 422 and r.json()["error"]["code"] == "oversell_unacknowledged"


def test_manual_commit_oversell_acked_writes(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/commit", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "sell",
        "date": "2026-06-11", "shares": "5000", "price": "600", "ack_oversell": True})
    assert r.status_code == 201


def test_manual_commit_hard_error_400(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/commit", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-11", "shares": "0", "price": "600"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "validation_error"
```

- [ ] **Step 2: run, expect fail.**

- [ ] **Step 3: implement** — add to `input_center.py`:
```python
from fastapi.responses import JSONResponse

from portfolio_dash.api.errors import error_body


@router.post("/input/manual/commit", status_code=201)
def manual_commit(body: ManualBody, conn: sqlite3.Connection = Depends(get_conn)) -> Any:
    inp = _txn_input(body)
    draft = enter_transaction(conn, inp, confirm=False)  # inspect first
    hard = [i for i in draft.issues if not i.needs_confirm]
    if hard:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", hard[0].message,
            issues=[issue_wire(i) for i in draft.issues]))
    oversell = any(i.kind == "sell_exceeds_holdings" for i in draft.issues)
    if oversell and not body.ack_oversell:
        return JSONResponse(status_code=422, content=error_body(
            "oversell_unacknowledged", "需確認賣超",
            issues=[issue_wire(i) for i in draft.issues]))
    written = enter_transaction(conn, inp, confirm=True)
    gross = body.shares * body.price
    total = (-(gross + written.fee + written.tax) if inp.side.value == "BUY"
             else (gross - written.fee - written.tax))
    return {"txn_id": written.transaction_id, "total": str(total)}
```

- [ ] **Step 4: run, expect pass.** **Step 5: gates + commit**
```bash
git add portfolio_dash/api/routers/input_center.py tests/contract/test_input_manual_api.py
git commit -m "feat(api): POST /api/input/manual/commit (ack-gated append) (spec 12.2)"
```

---

### Task 4: CHANGELOG + full green

- [ ] **Step 1:** Append to `CHANGELOG.md` `[Unreleased] › ### Added` (after the spec-11 bullet):
```markdown
- **Input center — context + manual entry (spec 12a, Phase 1):** `GET /api/input/context`
  (accounts + mapped `div_model`, fee-rule serialization with label, instruments + `etf`,
  current holdings) and `POST /api/input/manual/{preview,commit}` over `enter_transaction`.
  New `api/wire.py` shared mappers: lowercase `side` in/out, `Issue` → `{sev,code,text,field}`,
  `fee_rules_wire` (reused by spec 13), `div_model` mapping. Commit is **ack-gated**: hard
  issues → 400, unacked oversell → 422 `oversell_unacknowledged`, else append.
```
- [ ] **Step 2:** `grep -c "^## \[v" CHANGELOG.md` → `1`.
- [ ] **Step 3:** `./.venv/Scripts/python.exe -m ruff check portfolio_dash tests && ./.venv/Scripts/python.exe -m mypy portfolio_dash tests --strict && ./.venv/Scripts/python.exe -m pytest -q` → clean, 0 failed.
- [ ] **Step 4:** `git add CHANGELOG.md && git commit -m "docs: CHANGELOG for input context + manual entry (spec 12a)"`

## Self-review
- Coverage: 12.1 context (Task 1), 12.2 manual preview (Task 2) + commit/ack (Task 3). Reconciliations 1–5 implemented in `api/wire.py` + handlers. Deferred to 12b: CSV import (12.3) + AI input (12.4) reuse `issue_wire`/`parse_side`.
- Consistency: `ManualBody`/`_txn_input`/`_rule_for` shared between preview & commit; `total` sign matches spec 11; money Decimal→string; ack enforced in the API because `enter_transaction(confirm=True)` bypasses soft issues.
- Note: the manual `total` recomputation in the handler is a presentation sum over stored values (not a number of record); the authoritative fee/tax come from `enter_transaction`/`compute_fees`.
