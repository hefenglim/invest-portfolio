# Phase 0 — API Foundation & Test Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the FastAPI JSON API skeleton, the spec-17 deterministic test harness, the static-frontend layout, and the spec-18 fee-engine structural fixes — ending with `GET /api/dashboard` serving the real `build_dashboard` output as a JSON contract, green against a golden fixture.

**Architecture:** New `portfolio_dash/api/` package = thin routers that call the existing calculation core and serialize to JSON (Decimal→string); no business logic in the API layer. A FastAPI app factory mounts `/api/*` and serves the static `web/` frontend via `StaticFiles`. Determinism for the closed-loop test harness comes from an injectable clock, fake providers, and a fake LLM completer (all injection points already exist in the codebase). This is decision **(B)** (CHANGELOG 2026-06-13).

**Tech Stack:** Python 3.12, FastAPI + Uvicorn, Pydantic v2, sqlite3, Decimal (never float for money), pytest + FastAPI TestClient + httpx + freezegun + pytest-socket, ruff, mypy --strict.

**CRITICAL — interpreter:** All gates MUST run via the repo venv: `./.venv/Scripts/python.exe -m pytest|mypy|ruff` (bash). Bare `python` lacks the project deps and produces spurious errors.

**Branch:** `feat/web-api-foundation` (already created; decision + reconciliation already committed there).

**Source of truth:** specs in `docs/design-handoff/ai-portfolio-watcher/project/specs/` (08, 17, 18, 19); reconciliation + sequence in `docs/design/spec-reconciliation-2026-06-13.md`.

---

## Scope decisions (read before starting)

- **Golden payload scope:** spec 17.2 makes `mock-data.js` the golden expected values, but the full 8-instrument mock also contains fields owned by later specs (`alerts` → spec 03, `dividend_projection` → spec 05). **Phase 0's golden test asserts only the core `DashboardData` fields** (kpis, holdings, realized, returns, allocation, currency_view, fx, dividends, trend, freshness) + `llm_quota` + `spark_30d`, computed from a seeded golden DB. Full `mock-data.js` alignment (all 8 instruments + add-on fields) completes incrementally in Phase 1 (specs 10/11/12) and Phase 2 (03/05). This is intentional and noted in the plan, not a gap.
- **Schema column migrations** (`instruments += target_low/...`, `transactions += fee_snapshot`, scheduler columns) belong to the specs that need them (10/11/15, Phase 1+). Phase 0 only changes the `DividendType` enum (the `dividends.type` column is already free-text `TEXT`, so no DB migration).
- **Enum lowercase wire format** (Side/DividendType, SR conflict #1) is needed by the ledger/input specs (11/12); the dashboard payload contains no Side/DividendType enum (only `Currency`, which stays uppercase). So Phase 0's serializer handles Decimal/datetime/date/Enum-value generically; the Side/DividendType lowercasing lands with Phase 1.
- **E2E (Playwright)** is scaffolded (directory + one skipped smoke test) but not driven in Phase 0 — the contract tier (FastAPI TestClient) is the Phase 0 closed loop. E2E unlocks as endpoints get wired (spec 17.5).

## File structure (created/modified in this plan)

```
web/                                    # Task 1: static frontend (copied from the design export)
Makefile                                # Task 1: run/test/e2e/regress/all
pyproject.toml                          # Task 1: + fastapi/uvicorn/httpx/freezegun/pytest-socket deps
portfolio_dash/shared/models/enums.py   # Task 2: DividendType += NET
portfolio_dash/data_ingestion/dividend_model.py  # Task 2: NET support
portfolio_dash/data_ingestion/config_seed.py      # Task 3+4: FeeRuleSet fields + FEE_RULES backfill
portfolio_dash/data_ingestion/fees.py             # Task 3: compute_fees structural fixes
portfolio_dash/api/__init__.py          # Task 5
portfolio_dash/api/serialize.py         # Task 5: Decimal/datetime → JSON wire helper
portfolio_dash/api/errors.py            # Task 5: error envelope + exception handlers
portfolio_dash/api/deps.py              # Task 5: per-request conn + now + reporting dependencies
portfolio_dash/api/app.py               # Task 5: create_app factory (lifespan, StaticFiles, routers)
portfolio_dash/api/routers/health.py    # Task 5: GET /api/health (boot smoke)
portfolio_dash/api/routers/dashboard.py # Task 7: GET /api/dashboard
tests/conftest.py                       # Task 6: golden_db, frozen_now, fakes, api_client
tests/contract/test_app_skeleton.py     # Task 5
tests/contract/test_fee_worked_examples.py  # Task 3+4
tests/contract/test_dashboard_api.py    # Task 7
tests/e2e/test_smoke.py                 # Task 6 (skipped placeholder)
tests/golden/.gitkeep                   # Task 6
tests/unit/test_dividend_net.py         # Task 2
```

---

### Task 1: Repo layout, dependencies, Makefile (spec 19.2)

**Files:**
- Create: `web/` (copy of the design export's frontend files)
- Modify: `pyproject.toml`
- Create: `Makefile`

- [ ] **Step 1: Copy the design export frontend into `web/`**

The pristine export stays under `docs/design-handoff/` (provenance); the working copy lives in `web/`.

```bash
cd /c/AI-Workspace/invest-portfolio
mkdir -p web
cp docs/design-handoff/ai-portfolio-watcher/project/*.html web/
cp docs/design-handoff/ai-portfolio-watcher/project/*.js   web/
cp docs/design-handoff/ai-portfolio-watcher/project/*.css  web/
ls web/ | head        # expect index.html, app.js, styles.css, shell.js, ... (no specs/, no uploads/)
```

- [ ] **Step 2: Add runtime + test dependencies to `pyproject.toml`**

In `[project].dependencies`, append after the `APScheduler` line:
```toml
    "fastapi>=0.111",
    "uvicorn[standard]>=0.30",
```
In `[project.optional-dependencies].dev`, append after `pytest-asyncio`:
```toml
    "httpx>=0.27",
    "freezegun>=1.5",
    "pytest-socket>=0.7",
```
Add a new optional group after the `probe` group:
```toml
e2e = [
    "playwright>=1.44",
]
```
In `[[tool.mypy.overrides]]` module list (the `ignore_missing_imports = true` one), add `"uvicorn.*"`, `"freezegun.*"`, `"pytest_socket.*"` to the existing list.

- [ ] **Step 3: Install the new deps into the venv**

Run: `./.venv/Scripts/python.exe -m pip install -e ".[dev]"`
Expected: installs fastapi, uvicorn, httpx, freezegun, pytest-socket; ends "Successfully installed …".

- [ ] **Step 4: Create the `Makefile`**

```makefile
# portfolio-dash — dev tasks. Always uses the repo venv interpreter.
PY := ./.venv/Scripts/python.exe

.PHONY: run test contract e2e regress all mypy ruff

run:
	$(PY) -m uvicorn portfolio_dash.api.app:create_app --factory --port 8400

test:
	$(PY) -m pytest tests/unit tests/contract -q

contract:
	$(PY) -m pytest tests/contract -q

e2e:
	$(PY) -m pytest tests/e2e -q

regress:
	$(PY) -m pytest tests/contract -q -k golden

mypy:
	$(PY) -m mypy portfolio_dash --strict

ruff:
	$(PY) -m ruff check portfolio_dash tests

all: ruff mypy test
	@echo "make all: green"
```

- [ ] **Step 5: Verify uvicorn + fastapi import under the venv**

Run: `./.venv/Scripts/python.exe -c "import fastapi, uvicorn, httpx, freezegun, pytest_socket; print('ok')"`
Expected: `ok`

- [ ] **Step 6: Commit**

```bash
git add web/ pyproject.toml Makefile
git commit -m "build(api): web/ layout, FastAPI+test deps, Makefile (spec 19 foundation)"
```

---

### Task 2: `DividendType.NET` (specs 08 §8.0, 11, 18)

**Files:**
- Modify: `portfolio_dash/shared/models/enums.py`
- Modify: `portfolio_dash/data_ingestion/dividend_model.py`
- Test: `tests/unit/test_dividend_net.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_dividend_net.py`:
```python
from decimal import Decimal

from portfolio_dash.data_ingestion.dividend_model import apply_dividend_model
from portfolio_dash.shared.models.enums import DividendType


def test_dividend_type_has_net_member() -> None:
    assert DividendType.NET.value == "NET"


def test_apply_net_model_records_net_received() -> None:
    # MY single-tier: the recorded amount IS the net received; no withholding.
    out = apply_dividend_model("net", gross=Decimal("170"), net=Decimal("170"))
    assert out.gross == Decimal("170")
    assert out.withholding == Decimal("0")
    assert out.net == Decimal("170")
    assert out.reinvest_shares is None


def test_apply_net_defaults_net_to_gross_minus_withholding() -> None:
    out = apply_dividend_model("net", gross=Decimal("200"))
    assert out.net == Decimal("200") and out.withholding == Decimal("0")
```

- [ ] **Step 2: Run to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/test_dividend_net.py -v`
Expected: FAIL — `AttributeError: NET` (enum member missing).

- [ ] **Step 3: Add the enum member**

In `portfolio_dash/shared/models/enums.py`, add to `DividendType`:
```python
class DividendType(StrEnum):
    """Dividend mechanism: cash payout, stock dividend (配股), DRIP reinvest, or net-received."""

    CASH = "CASH"
    STOCK = "STOCK"
    DRIP = "DRIP"
    NET = "NET"
```

- [ ] **Step 4: Support NET in `apply_dividend_model`**

In `portfolio_dash/data_ingestion/dividend_model.py`, change the final block so `NET` is handled like cash (net received, no withholding). Replace the comment + final block:
```python
    # cash (TW) or net (MY single-tier): recorded amount is net received, no withholding
    wh = withholding if withholding is not None else Decimal("0")
    n = net if net is not None else gross - wh
    return DividendAmounts(gross=gross, withholding=wh, net=n)
```
(The `t == "NET"` case falls through to here alongside `CASH`; no separate branch needed since both mean "net received, zero withholding".)

- [ ] **Step 5: Run to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/test_dividend_net.py -v`
Expected: 3 PASS.

- [ ] **Step 6: Gates + commit**

```bash
./.venv/Scripts/python.exe -m mypy portfolio_dash --strict
./.venv/Scripts/python.exe -m ruff check portfolio_dash tests
git add portfolio_dash/shared/models/enums.py portfolio_dash/data_ingestion/dividend_model.py tests/unit/test_dividend_net.py
git commit -m "feat(ledger): DividendType.NET (MY single-tier net dividends)"
```

---

### Task 3: `FeeRuleSet` structural fixes + `compute_fees` (spec 18.0.1, worked examples 18.1)

**Files:**
- Modify: `portfolio_dash/data_ingestion/config_seed.py` (FeeRuleSet model only)
- Modify: `portfolio_dash/data_ingestion/fees.py`
- Test: `tests/contract/test_fee_worked_examples.py`

- [ ] **Step 1: Write the failing worked-example tests**

Create `tests/contract/test_fee_worked_examples.py` (expected values hand-derived from spec 18.1; never generated by the code under test):
```python
from decimal import Decimal

from portfolio_dash.data_ingestion.config_seed import FeeRuleSet
from portfolio_dash.data_ingestion.fees import compute_fees
from portfolio_dash.shared.enums import Market
from portfolio_dash.shared.models.enums import Side

TW = FeeRuleSet(market=Market.TW, brokerage=Decimal("0.001425"), discount=Decimal("1"),
                min_fee=Decimal("20"), tax_normal=Decimal("0.003"),
                tax_etf=Decimal("0.001"), tax_daytrade=Decimal("0.0015"), round_integer=True)
SCHWAB = FeeRuleSet(market=Market.US, sec_fee=Decimal("0.0000278"))
MOOMOO_US = FeeRuleSet(market=Market.US, flat_fee=Decimal("0.99"), sec_fee=Decimal("0.0000278"))
MOOMOO_MY = FeeRuleSet(market=Market.MY, brokerage=Decimal("0.0008"), min_fee=Decimal("3"),
                       clearing=Decimal("0.0003"), clearing_cap=Decimal("1000"),
                       stamp_duty_rate=Decimal("0.001"))


def test_w1_tw_buy() -> None:  # 612500*0.001425=872.8125 -> 873
    r = compute_fees(TW, Side.BUY, Decimal("1000"), Decimal("612.5"))
    assert r.fee == Decimal("873") and r.tax == Decimal("0")


def test_w2_tw_sell_normal() -> None:  # fee 170.43->170; tax 119600*0.003=358.8->359
    r = compute_fees(TW, Side.SELL, Decimal("200"), Decimal("598"))
    assert r.fee == Decimal("170") and r.tax == Decimal("359")


def test_w3_tw_sell_etf() -> None:  # fee 110.01->110; tax 77200*0.001=77.2->77
    r = compute_fees(TW, Side.SELL, Decimal("2000"), Decimal("38.6"), is_etf=True)
    assert r.fee == Decimal("110") and r.tax == Decimal("77")


def test_w4_tw_buy_min_fee() -> None:  # 3860*0.001425=5.5005 -> min 20
    r = compute_fees(TW, Side.BUY, Decimal("100"), Decimal("38.6"))
    assert r.fee == Decimal("20") and r.tax == Decimal("0")


def test_w5_tw_daytrade_sell_halfup_boundary() -> None:  # 119000*0.0015=178.5 -> 179
    r = compute_fees(TW, Side.SELL, Decimal("200"), Decimal("595"), daytrade=True)
    assert r.fee == Decimal("170") and r.tax == Decimal("179")


def test_w6_schwab_sell_sec_fee() -> None:  # 1002.50*0.0000278=0.0278695 -> 0.03
    r = compute_fees(SCHWAB, Side.SELL, Decimal("5"), Decimal("200.50"))
    assert r.fee == Decimal("0.03") and r.tax == Decimal("0.00")


def test_w7_moomoo_us_buy_flat_fee() -> None:  # flat 0.99
    r = compute_fees(MOOMOO_US, Side.BUY, Decimal("10"), Decimal("165.20"))
    assert r.fee == Decimal("0.99") and r.tax == Decimal("0.00")


def test_w8_moomoo_my_buy() -> None:
    # notional 2886: comm 2886*0.0008=2.3088 -> min 3; clearing 2886*0.0003=0.8658 -> 0.87;
    # stamp 2886*0.001=2.886 -> 2.89; fee = 3 + 0.87 + 2.89 = 6.76
    r = compute_fees(MOOMOO_MY, Side.BUY, Decimal("300"), Decimal("9.62"))
    assert r.fee == Decimal("6.76") and r.tax == Decimal("0.00")


def test_w9_moomoo_my_clearing_cap() -> None:
    # notional 4,000,000: clearing 1200 -> cap 1000; comm 4,000,000*0.0008=3200;
    # stamp 4,000,000*0.001=4000; fee = 3200 + 1000 + 4000 = 8200
    r = compute_fees(MOOMOO_MY, Side.BUY, Decimal("400000"), Decimal("10"))
    assert r.fee == Decimal("8200.00")


def test_tw_zero_notional_no_min_fee() -> None:  # guard: notional 0 must not charge min_fee
    r = compute_fees(TW, Side.BUY, Decimal("0"), Decimal("612.5"))
    assert r.fee == Decimal("0")
```

- [ ] **Step 2: Run to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/contract/test_fee_worked_examples.py -v`
Expected: FAIL — `ValidationError`/`AttributeError` (FeeRuleSet has no `flat_fee`/`stamp_duty_rate`), and MY/US assertions mismatch.

- [ ] **Step 3: Add the missing `FeeRuleSet` fields**

In `portfolio_dash/data_ingestion/config_seed.py`, in `FeeRuleSet`, replace the `stamp_duty` line and add `flat_fee` + cap. The new field block:
```python
    sec_fee: Decimal = Decimal("0")  # US sell-side regulatory fee rate
    flat_fee: Decimal = Decimal("0")  # per-trade fixed fee (e.g. Moomoo US platform fee)
    clearing: Decimal = Decimal("0")  # MY
    clearing_cap: Decimal | None = None
    stamp_duty_rate: Decimal = Decimal("0")  # MY: rate of notional (was a flat constant)
    stamp_duty_cap: Decimal | None = None
    sst: Decimal = Decimal("0")
    round_integer: bool = False  # TW rounds fee/tax to integer NT$
```
(Remove the old `stamp_duty: Decimal = Decimal("0")` line — it is replaced by `stamp_duty_rate`/`stamp_duty_cap`.)

- [ ] **Step 4: Rewrite the US and MY branches of `compute_fees`**

In `portfolio_dash/data_ingestion/fees.py`:

TW branch — guard the min-fee against zero notional. Replace the TW `raw_fee`/`fee` lines:
```python
    if rules.market is Market.TW:
        raw_fee = rules.brokerage * rules.discount * notional
        fee = max(raw_fee, rules.min_fee) if notional > 0 else Decimal("0")
        snap["brokerage"] = str(rules.brokerage)
        snap["discount"] = str(rules.discount)
        snap["min_fee"] = str(rules.min_fee)
```
(keep the rest of the TW branch unchanged.)

US branch — flat fee + brokerage + sell-side SEC fee, then apply `min_fee`:
```python
    if rules.market is Market.US:
        fee = rules.flat_fee + rules.brokerage * notional
        snap["flat_fee"] = str(rules.flat_fee)
        snap["brokerage"] = str(rules.brokerage)
        if side is Side.SELL:
            fee = fee + rules.sec_fee * notional
            snap["sec_fee"] = str(rules.sec_fee)
        if notional > 0 and rules.min_fee > 0:
            fee = max(fee, rules.min_fee)
            snap["min_fee"] = str(rules.min_fee)
        return FeeResult(fee=_round(fee, integer=False), tax=Decimal("0.00"), snapshot=snap)
```

MY branch — brokerage with min_fee, clearing (capped), stamp-duty as a rate (capped), SST:
```python
    # Market.MY
    brokerage = rules.brokerage * notional
    if notional > 0 and rules.min_fee > 0:
        brokerage = max(brokerage, rules.min_fee)
    clearing = rules.clearing * notional
    if rules.clearing_cap is not None and clearing > rules.clearing_cap:
        clearing = rules.clearing_cap
    stamp = rules.stamp_duty_rate * notional
    if rules.stamp_duty_cap is not None and stamp > rules.stamp_duty_cap:
        stamp = rules.stamp_duty_cap
    fee = brokerage + clearing + stamp + rules.sst
    snap["brokerage"] = str(rules.brokerage)
    snap["min_fee"] = str(rules.min_fee)
    snap["clearing"] = str(rules.clearing)
    snap["stamp_duty_rate"] = str(rules.stamp_duty_rate)
    snap["sst"] = str(rules.sst)
    if rules.clearing_cap is not None:
        snap["clearing_cap"] = str(rules.clearing_cap)
    if rules.stamp_duty_cap is not None:
        snap["stamp_duty_cap"] = str(rules.stamp_duty_cap)
    return FeeResult(fee=_round(fee, integer=False), tax=Decimal("0.00"), snapshot=snap)
```

- [ ] **Step 5: Run to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/contract/test_fee_worked_examples.py -v`
Expected: 11 PASS. If W8/W9 differ, re-derive against spec 18.1 — **fix the code, never the expected value** (per spec 18.7).

- [ ] **Step 6: Run the existing fee tests (no regression)**

Run: `./.venv/Scripts/python.exe -m pytest tests/data_ingestion/test_fees.py -v`
Expected: still green (the old MY tests used `stamp_duty`; if any reference it, they are updated in this step to `stamp_duty_rate` — adjust those test inputs, not the assertions' intent).

- [ ] **Step 7: Gates + commit**

```bash
./.venv/Scripts/python.exe -m mypy portfolio_dash --strict
./.venv/Scripts/python.exe -m ruff check portfolio_dash tests
git add portfolio_dash/data_ingestion/config_seed.py portfolio_dash/data_ingestion/fees.py tests/contract/test_fee_worked_examples.py tests/data_ingestion/test_fees.py
git commit -m "fix(fees): FeeRuleSet flat_fee + stamp_duty_rate/cap + US/MY min_fee; worked examples W1-W9 (spec 18)"
```

---

### Task 4: Backfill `FEE_RULES` from the spec-18.0 truth table

**Files:**
- Modify: `portfolio_dash/data_ingestion/config_seed.py` (FEE_RULES dict)
- Test: `tests/contract/test_fee_worked_examples.py` (append seeded-rule checks)

- [ ] **Step 1: Write the failing seeded-rule tests**

Append to `tests/contract/test_fee_worked_examples.py`:
```python
from portfolio_dash.data_ingestion.config_seed import get_fee_rule_set


def test_seeded_schwab_has_sec_fee() -> None:
    assert get_fee_rule_set("schwab").sec_fee == Decimal("0.0000278")


def test_seeded_moomoo_us_has_flat_fee() -> None:
    assert get_fee_rule_set("moomoo_us").flat_fee == Decimal("0.99")


def test_seeded_moomoo_my_rates() -> None:
    r = get_fee_rule_set("moomoo_my")
    assert r.brokerage == Decimal("0.0008") and r.min_fee == Decimal("3")
    assert r.clearing == Decimal("0.0003") and r.clearing_cap == Decimal("1000")
    assert r.stamp_duty_rate == Decimal("0.001")


def test_seeded_moomoo_us_end_to_end_w7() -> None:
    r = compute_fees(get_fee_rule_set("moomoo_us"), Side.BUY, Decimal("10"), Decimal("165.20"))
    assert r.fee == Decimal("0.99")
```

- [ ] **Step 2: Run to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/contract/test_fee_worked_examples.py -k seeded -v`
Expected: FAIL (current US/MY rules are placeholders without these values).

- [ ] **Step 3: Backfill the FEE_RULES dict**

In `portfolio_dash/data_ingestion/config_seed.py`, replace the `schwab` / `moomoo_us` / `moomoo_my` entries in `FEE_RULES` (TW unchanged):
```python
    "schwab": FeeRuleSet(market=Market.US, sec_fee=Decimal("0.0000278")),
    "moomoo_us": FeeRuleSet(
        market=Market.US, flat_fee=Decimal("0.99"), sec_fee=Decimal("0.0000278")
    ),
    "moomoo_my": FeeRuleSet(
        market=Market.MY,
        brokerage=Decimal("0.0008"),
        min_fee=Decimal("3"),
        clearing=Decimal("0.0003"),
        clearing_cap=Decimal("1000"),
        stamp_duty_rate=Decimal("0.001"),
    ),
```
(Add a one-line comment above these: `# Rates per spec 18.0 truth table; pending real-statement confirmation (SEC fee, MY stamp-duty cap, Moomoo platform fee buy/sell).`)

- [ ] **Step 4: Run to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/contract/test_fee_worked_examples.py -v`
Expected: all PASS (15 total).

- [ ] **Step 5: Gates + commit**

```bash
./.venv/Scripts/python.exe -m mypy portfolio_dash --strict
./.venv/Scripts/python.exe -m ruff check portfolio_dash tests
git add portfolio_dash/data_ingestion/config_seed.py tests/contract/test_fee_worked_examples.py
git commit -m "feat(fees): backfill US/MY fee rates from spec 18.0 truth table"
```

---

### Task 5: FastAPI app skeleton (spec 08 §8.0, 19.2)

**Files:**
- Create: `portfolio_dash/api/__init__.py`, `portfolio_dash/api/serialize.py`, `portfolio_dash/api/errors.py`, `portfolio_dash/api/deps.py`, `portfolio_dash/api/app.py`, `portfolio_dash/api/routers/__init__.py`, `portfolio_dash/api/routers/health.py`
- Test: `tests/contract/test_app_skeleton.py`

- [ ] **Step 1: Write the failing skeleton test**

Create `tests/contract/test_app_skeleton.py`:
```python
from fastapi.testclient import TestClient

from portfolio_dash.api.app import create_app


def test_app_boots_and_health_ok() -> None:
    with TestClient(create_app()) as client:
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


def test_unknown_api_route_uses_error_envelope() -> None:
    with TestClient(create_app()) as client:
        r = client.get("/api/does-not-exist")
        assert r.status_code == 404
        body = r.json()
        assert set(body["error"]) >= {"code", "message"}
        assert body["error"]["code"] == "not_found"
```

- [ ] **Step 2: Run to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/contract/test_app_skeleton.py -v`
Expected: FAIL — `ModuleNotFoundError: portfolio_dash.api.app`.

- [ ] **Step 3: Implement the serialize helper**

Create `portfolio_dash/api/__init__.py`:
```python
"""FastAPI JSON API layer (decision B, 2026-06-13): thin routers over the calc core."""
```
Create `portfolio_dash/api/serialize.py`:
```python
"""Wire-format serialization: Decimal -> string, datetime/date -> ISO, Enum -> value.

The API layer never emits money as a JSON number (precision); every Decimal is a string.
Currency enum values stay as-is (uppercase); Side/DividendType lowercasing is added with
the ledger/input specs that surface them.
"""

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any


def to_wire(value: Any) -> Any:
    """Recursively convert a model_dump()/dict tree into JSON-safe wire values."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {k: to_wire(v) for k, v in value.items()}
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence):
        return [to_wire(v) for v in value]
    return value
```

- [ ] **Step 4: Implement the error envelope + handlers**

Create `portfolio_dash/api/errors.py`:
```python
"""Common error envelope (spec 08 §8.0) + exception handlers, incl. LLM 402/409/503."""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from portfolio_dash.shared.llm_config import (
    AINotActivated,
    LLMBudgetExceeded,
    LLMUnavailable,
)


def error_body(code: str, message: str, *, field: str | None = None,
               issues: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if field is not None:
        err["field"] = field
    if issues is not None:
        err["issues"] = issues
    return {"error": err}


_NOT_FOUND = "not_found"
_STATUS_CODE = {400: "validation_error", 401: "unauthorized", 403: "forbidden",
                404: _NOT_FOUND, 422: "unprocessable", 500: "internal_error"}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def _http(_r: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_CODE.get(exc.status_code, "error")
        return JSONResponse(status_code=exc.status_code,
                            content=error_body(code, str(exc.detail)))

    @app.exception_handler(RequestValidationError)
    async def _validation(_r: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(p) for p in first.get("loc", []) if p != "body") or None
        return JSONResponse(status_code=400,
                            content=error_body("validation_error",
                                               first.get("msg", "invalid request"),
                                               field=field))

    @app.exception_handler(LLMBudgetExceeded)
    async def _budget(_r: Request, exc: LLMBudgetExceeded) -> JSONResponse:
        return JSONResponse(status_code=402,
                            content=error_body("budget_exceeded", str(exc) or "AI 額度用盡"))

    @app.exception_handler(AINotActivated)
    async def _inactive(_r: Request, exc: AINotActivated) -> JSONResponse:
        return JSONResponse(status_code=409,
                            content=error_body("ai_not_activated", str(exc) or "AI 未啟用"))

    @app.exception_handler(LLMUnavailable)
    async def _unavailable(_r: Request, exc: LLMUnavailable) -> JSONResponse:
        return JSONResponse(status_code=503,
                            content=error_body("llm_unavailable", str(exc) or "LLM 服務不可用"))
```

- [ ] **Step 5: Implement request dependencies**

Create `portfolio_dash/api/deps.py`:
```python
"""Per-request dependencies: SQLite connection, injectable clock, reporting currency."""

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from zoneinfo import ZoneInfo

from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.db import session
from portfolio_dash.shared.enums import Currency

APP_TZ = ZoneInfo("Asia/Taipei")


def get_conn() -> Iterator[sqlite3.Connection]:
    """A fresh per-request connection (never share one across threads)."""
    with session() as conn:
        yield conn


def get_now() -> datetime:
    """Current time in the application timezone (overridden in tests via freezegun)."""
    return datetime.now(APP_TZ)


def get_reporting() -> Currency:
    return get_settings().reporting_currency
```
> Note: confirm `shared.db.session()` is a context manager yielding a `sqlite3.Connection` with `row_factory = sqlite3.Row`. If it is not a context manager, use `get_connection()` and `close()` in a try/finally instead. (Check `portfolio_dash/shared/db.py` before implementing.)

- [ ] **Step 6: Implement the health router + app factory**

Create `portfolio_dash/api/routers/__init__.py`:
```python
"""API routers (one module per domain)."""
```
Create `portfolio_dash/api/routers/health.py`:
```python
from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
```
Create `portfolio_dash/api/app.py`:
```python
"""FastAPI app factory: lifespan (DB + scheduler), /api routers, static web/ frontend."""

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from portfolio_dash.api.errors import register_error_handlers
from portfolio_dash.api.routers import health
from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.scheduler.jobs import ensure_scheduler_seeded
from portfolio_dash.scheduler.runtime import build_scheduler
from portfolio_dash.shared.db import session

_WEB_DIR = Path(__file__).resolve().parents[2] / "web"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    with session() as conn:
        bootstrap_db(conn)
        ensure_scheduler_seeded(conn)
    scheduler = build_scheduler()
    scheduler.start()
    app.state.scheduler = scheduler
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


def create_app() -> FastAPI:
    app = FastAPI(title="portfolio-dash", lifespan=_lifespan)
    register_error_handlers(app)
    app.include_router(health.router, prefix="/api")
    if _WEB_DIR.is_dir():
        app.mount("/", StaticFiles(directory=_WEB_DIR, html=True), name="web")
    return app
```
> Note: verify `scheduler.runtime.build_scheduler()` / `.start()` / `.shutdown()` signatures (Phase-0-relevant; from the scheduler sub-project). If `build_scheduler` requires a connection argument, pass one from `session()`. Check `portfolio_dash/scheduler/runtime.py` before implementing.

- [ ] **Step 7: Run to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/contract/test_app_skeleton.py -v`
Expected: 2 PASS. (The 404 test exercises the StarletteHTTPException handler under `/api`.)

- [ ] **Step 8: Gates + commit**

```bash
./.venv/Scripts/python.exe -m mypy portfolio_dash --strict
./.venv/Scripts/python.exe -m ruff check portfolio_dash tests
git add portfolio_dash/api tests/contract/test_app_skeleton.py
git commit -m "feat(api): FastAPI app factory, error envelope, serialize helper, /api/health (spec 08)"
```

---

### Task 6: Test harness — golden DB, frozen clock, fakes (spec 17.1–17.3)

**Files:**
- Modify: `tests/conftest.py` (create if absent at repo `tests/` root)
- Create: `tests/golden/.gitkeep`, `tests/e2e/__init__.py`, `tests/e2e/test_smoke.py`, `tests/unit/__init__.py`, `tests/contract/__init__.py`
- Modify: `pyproject.toml` (pytest addopts: pytest-socket)

- [ ] **Step 1: Enable the network ban in pytest config**

In `pyproject.toml` `[tool.pytest.ini_options]`, change `addopts`:
```toml
addopts = "-q --disable-socket --allow-unix-socket"
```
> `--allow-unix-socket` keeps in-process ASGI/TestClient transports working while blocking real TCP. If any existing test legitimately needs sockets, mark it with `@pytest.mark.enable_socket` (from pytest-socket).

- [ ] **Step 2: Write the golden-DB + fakes conftest**

Create/extend `tests/conftest.py` (repo-root tests package):
```python
"""Shared test fixtures: deterministic golden DB, frozen clock, fakes, API client."""

import sqlite3
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api.app import create_app
from portfolio_dash.api.deps import get_conn, get_now, get_reporting
from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_dividend,
    insert_fx_conversion,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.pricing.store import upsert_fx, upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

GOLDEN_NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))


def _seed_golden(conn: sqlite3.Connection) -> None:
    """Reproduce a known scenario (subset of mock-data.js) via real write paths."""
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Semiconductors", name="TSMC", board="TWSE"))
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Tech", name="Apple"))
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("1000"), price=Decimal("500"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 5))
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("10"), price=Decimal("100"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 10))
    insert_dividend(conn, account_id="tw_broker", symbol="2330", div_date=date(2026, 3, 1),
                    div_type="CASH", gross=Decimal("5000"), withholding=Decimal("0"),
                    net=Decimal("5000"))
    insert_fx_conversion(conn, account_id="schwab", date=date(2026, 1, 8),
                         from_ccy=Currency.TWD, from_amount=Decimal("32000"),
                         to_ccy=Currency.USD, to_amount=Decimal("1000"))
    upsert_prices(conn, [
        PriceRow(instrument="2330", market=Market.TW, as_of=date(2026, 6, 9),
                 close=Decimal("600"), source="test"),
        PriceRow(instrument="AAPL", market=Market.US, as_of=date(2026, 6, 9),
                 close=Decimal("120"), source="test"),
    ], fetched_at=GOLDEN_NOW)
    upsert_fx(conn, [
        FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(2026, 1, 8),
              rate=Decimal("32"), source="test"),
        FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(2026, 6, 9),
              rate=Decimal("33"), source="test"),
        FxRow(base=Currency.MYR, quote=Currency.TWD, as_of=date(2026, 6, 9),
              rate=Decimal("7"), source="test"),
        FxRow(base=Currency.USD, quote=Currency.MYR, as_of=date(2026, 6, 9),
              rate=Decimal("4.4"), source="test"),
    ], fetched_at=GOLDEN_NOW)
    conn.commit()


@pytest.fixture
def golden_db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    bootstrap_db(conn)
    create_pricing_tables(conn)
    _seed_golden(conn)
    yield conn
    conn.close()


@pytest.fixture
def api_client(golden_db: sqlite3.Connection) -> Iterator[TestClient]:
    """TestClient with the golden DB + frozen clock injected (no lifespan side effects)."""
    app = create_app()
    app.dependency_overrides[get_conn] = lambda: iter([golden_db])
    app.dependency_overrides[get_now] = lambda: GOLDEN_NOW
    app.dependency_overrides[get_reporting] = lambda: Currency.TWD
    # Bypass lifespan (DB already seeded; no scheduler in tests):
    with TestClient(app, raise_server_exceptions=True) as client:
        yield client
    app.dependency_overrides.clear()
```
> The `get_conn` override yields the already-open `golden_db` (a generator returning it once). Because lifespan also runs `bootstrap_db`/scheduler, and tests must stay hermetic, **see Step 3** — the app must tolerate lifespan running against its own throwaway file/connection; for contract tests we rely on the dependency override for the request connection, not lifespan state. If lifespan's `session()` touches the real `data/portfolio.db`, override the DB path via the `PD_DB_PATH`/`DB_PATH` env to a temp file in a fixture, or guard lifespan to skip the scheduler under a `PD_TESTING` env. Choose the minimal approach that keeps `make all` hermetic and document it in the commit.

- [ ] **Step 3: Make lifespan test-safe**

To keep contract tests from starting the real scheduler or touching the real DB, gate the scheduler in lifespan behind an env flag. In `portfolio_dash/api/app.py` `_lifespan`, wrap the scheduler start:
```python
    import os
    scheduler = None
    if os.environ.get("PD_DISABLE_SCHEDULER") != "1":
        scheduler = build_scheduler()
        scheduler.start()
    app.state.scheduler = scheduler
    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)
```
And in `tests/conftest.py` set `os.environ["PD_DISABLE_SCHEDULER"] = "1"` at import time (top of file), plus point the DB at a temp file so lifespan's `bootstrap_db` never touches `data/portfolio.db`:
```python
import os
os.environ["PD_DISABLE_SCHEDULER"] = "1"
os.environ.setdefault("DB_PATH", ":memory:")
```
> Verify the settings env var name for the DB path (`shared/config.py` `db_path`; pydantic-settings field `db_path` ← env `DB_PATH`). Use the real env name. `:memory:` per lifespan connection is throwaway and harmless.

- [ ] **Step 4: Scaffold golden + e2e directories**

```bash
mkdir -p tests/golden tests/e2e tests/unit
touch tests/golden/.gitkeep tests/unit/__init__.py tests/contract/__init__.py tests/e2e/__init__.py
```
Create `tests/e2e/test_smoke.py`:
```python
import pytest

pytest.skip("E2E (Playwright) unlocks as endpoints are wired — spec 17.5", allow_module_level=True)
```

- [ ] **Step 5: Verify the harness imports and the suite still runs**

Run: `./.venv/Scripts/python.exe -m pytest tests/contract/test_app_skeleton.py -v`
Expected: still 2 PASS (now under the socket ban; TestClient uses in-process transport).
Run: `./.venv/Scripts/python.exe -m pytest -q`
Expected: full suite green (existing 269 + new tasks' tests); e2e smoke skipped.

- [ ] **Step 6: Gates + commit**

```bash
./.venv/Scripts/python.exe -m mypy portfolio_dash tests --strict
./.venv/Scripts/python.exe -m ruff check portfolio_dash tests
git add tests/conftest.py tests/golden tests/e2e tests/unit/__init__.py tests/contract/__init__.py portfolio_dash/api/app.py pyproject.toml
git commit -m "test(harness): golden_db + frozen clock + api_client fixtures, socket ban, e2e scaffold (spec 17)"
```

---

### Task 7: `GET /api/dashboard` (spec 08 §8.1) — first real endpoint, golden-tested

**Files:**
- Create: `portfolio_dash/api/routers/dashboard.py`
- Modify: `portfolio_dash/api/app.py` (include the dashboard router)
- Test: `tests/contract/test_dashboard_api.py`

- [ ] **Step 1: Write the failing golden contract test**

Create `tests/contract/test_dashboard_api.py`:
```python
from fastapi.testclient import TestClient


def test_dashboard_money_fields_are_strings(api_client: TestClient) -> None:
    r = api_client.get("/api/dashboard")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["kpis"]["total_market_value"], str)
    assert body["kpis"]["total_market_value"] == "639600"      # golden (2330 600k + AAPL 1200@33)
    assert body["reporting_currency"] == "TWD"
    assert body["as_of"].startswith("2026-06-11T14:30")        # frozen clock, +08:00


def test_dashboard_holdings_enriched_and_llm_quota_present(api_client: TestClient) -> None:
    body = api_client.get("/api/dashboard").json()
    by_symbol = {h["symbol"]: h for h in body["holdings"]}
    assert by_symbol["2330"]["name"] == "TSMC"
    assert by_symbol["2330"]["market_value"] == "600000"
    assert isinstance(by_symbol["2330"]["spark_30d"], list)
    assert "llm_quota" in body                                 # spec 08 §8.1 add-on field


def test_dashboard_freshness_and_currency_kept_uppercase(api_client: TestClient) -> None:
    body = api_client.get("/api/dashboard").json()
    assert body["currency_view"]["by_currency_value"]["USD"] == "1200"   # Currency stays UPPER
    assert body["freshness"]["missing_prices"] == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/contract/test_dashboard_api.py -v`
Expected: FAIL — 404 (route not yet mounted) / KeyError.

- [ ] **Step 3: Implement the dashboard router**

Create `portfolio_dash/api/routers/dashboard.py`:
```python
"""GET /api/dashboard — serialize build_dashboard output + spark_30d + llm_quota.

Pure read. The router calls the calc core and serializes; it computes nothing.
Add-on fields owned by later specs (alerts → 03, dividend_projection → 05) are added
when those specs land; Phase 0 serves the core payload + spark_30d + llm_quota.
"""

import sqlite3
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query

from portfolio_dash.api.deps import get_conn, get_now, get_reporting
from portfolio_dash.api.serialize import to_wire
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.pricing.store import get_price_history
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.llm_config import budget_remaining

router = APIRouter()

_SPARK_DAYS = 30


@router.get("/dashboard")
def dashboard(
    trend_days: int = Query(90, ge=1, le=3650),
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
    reporting: Currency = Depends(get_reporting),
) -> dict[str, Any]:
    data = build_dashboard(conn, now=now, reporting=reporting)
    payload: dict[str, Any] = to_wire(data.model_dump())

    # spark_30d: last ~22 trading-day closes per held symbol (spec 01 add-on; batch read).
    end = now.date()
    start = end.fromordinal(end.toordinal() - _SPARK_DAYS)
    for row in payload["holdings"]:
        history = get_price_history(conn, row["symbol"], start, end)
        row["spark_30d"] = [str(p.value) for p in history]

    remaining = budget_remaining(conn)
    payload["llm_quota"] = {"remaining_usd": None if remaining is None else str(remaining)}
    return payload
```

- [ ] **Step 4: Mount the router**

In `portfolio_dash/api/app.py`, add the import and include line:
```python
from portfolio_dash.api.routers import dashboard, health
...
    app.include_router(health.router, prefix="/api")
    app.include_router(dashboard.router, prefix="/api")
```

- [ ] **Step 5: Run to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/contract/test_dashboard_api.py -v`
Expected: 3 PASS. If `total_market_value` differs, the bug is in serialization/wiring (the golden seed reuses the proven combiner scenario) — fix the code, not the expected value.

- [ ] **Step 6: Full gates + commit**

```bash
./.venv/Scripts/python.exe -m pytest > pytest_out.txt 2>&1; grep -E "passed|failed|error" pytest_out.txt | tail -2; rm pytest_out.txt
./.venv/Scripts/python.exe -m mypy portfolio_dash tests --strict
./.venv/Scripts/python.exe -m ruff check portfolio_dash tests
git add portfolio_dash/api/routers/dashboard.py portfolio_dash/api/app.py tests/contract/test_dashboard_api.py
git commit -m "feat(api): GET /api/dashboard (build_dashboard + spark_30d + llm_quota), golden-tested (spec 08 §8.1)"
```

---

### Task 8: CHANGELOG + Phase 0 close-out

**Files:**
- Modify: `CHANGELOG.md` (`[Unreleased]` → `### Added`)

- [ ] **Step 1: Add the CHANGELOG entry**

Append to `## [Unreleased]` → `### Added` (bounded-section edit, not surgical string insert):
```markdown
- **Phase 0 — web API foundation (decision B):** `portfolio_dash/api/` FastAPI app
  factory (lifespan boots DB + scheduler; serves static `web/` via StaticFiles; routers
  under `/api/*`), the common error envelope (incl. LLM 402/409/503 mapping), the
  Decimal→string wire serializer, and `GET /api/health` + `GET /api/dashboard`
  (serialized `build_dashboard` + `spark_30d` + `llm_quota`). Spec-17 test harness:
  `golden_db` fixture (seeded via real write paths), frozen clock, `api_client`,
  pytest-socket network ban, `Makefile` (`make all`). Fee engine (spec 18): `FeeRuleSet`
  gains `flat_fee`/`stamp_duty_rate`/`stamp_duty_cap` + US/MY `min_fee`; worked examples
  W1–W9; US/MY rates backfilled from the spec-18.0 truth table (pending real-statement
  confirmation). `DividendType += NET` (MY single-tier).
```

- [ ] **Step 2: Verify CHANGELOG integrity**

Run: `grep -c "^## \[v" CHANGELOG.md`
Expected: `1` (unchanged — `[Unreleased]` is not counted).

- [ ] **Step 3: `make all` green**

Run: `./.venv/Scripts/python.exe -m ruff check portfolio_dash tests && ./.venv/Scripts/python.exe -m mypy portfolio_dash tests --strict && ./.venv/Scripts/python.exe -m pytest -q`
Expected: ruff clean; mypy `Success`; pytest all passed (existing 269 + Phase 0 additions), e2e skipped.

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: CHANGELOG for Phase 0 web API foundation"
```

---

## Plan self-review notes (applied)

- **Spec coverage:** 08 §8.0 (app skeleton, error format, serialization, tz) → Task 5; 08 §8.1 (dashboard) → Task 7; 17 (harness, determinism, golden) → Task 6; 18.0/18.0.1/18.1 (fee truth table, FeeRuleSet fixes, worked examples) → Tasks 3–4; 19.2 (layout, Makefile, deps) → Task 1; `DividendType.NET` (08/11/18) → Task 2. Deferred-by-design (noted in Scope decisions): full mock-data.js golden, alerts/dividend_projection add-ons, column migrations, Side/DividendType lowercasing, Playwright E2E.
- **Verification dependencies flagged inline:** `shared.db.session()` shape (Task 5 Step 5), `scheduler.runtime` signatures (Task 5 Step 6), DB-path env name (Task 6 Step 3). The implementer must confirm these against the codebase before writing — they are existing modules from earlier sub-projects.
- **Type/name consistency:** `to_wire`, `error_body`, `get_conn`/`get_now`/`get_reporting`, `golden_db`/`api_client`, `GOLDEN_NOW`, `create_app` are used consistently across tasks. Fee fields `flat_fee`/`stamp_duty_rate`/`stamp_duty_cap` defined in Task 3, seeded in Task 4, asserted in both.
- **TDD + frequent commits:** every task is test-first with its own commit; Task 8 closes with `make all` green.
