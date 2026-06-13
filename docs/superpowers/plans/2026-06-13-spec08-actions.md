# Spec 08 actions — refresh-quotes + recompute (Phase 1 close-out)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** Implement the two top-bar action endpoints `POST /api/actions/refresh-quotes` and `POST /api/actions/recompute` (spec 08 §8.2–8.3), closing the Phase-1 backend.

**Architecture:** A thin `actions` router. `refresh-quotes` triggers the existing per-market `scheduler.jobs` quotes jobs **synchronously** and returns their `job_runs` ids. `recompute` re-runs `portfolio.cost_basis.build_book` over the ledgers to validate consistency (catching `OversellError`), append-only (no writes).

**Tech Stack:** Python 3.12, FastAPI, sqlite3, Decimal, pytest + TestClient, mypy --strict, ruff. Gates via `./.venv/Scripts/python.exe -m ...`.

**Branch:** `feat/api-actions` (off main @ spec 12b).

## Decision — refresh-quotes is synchronous (200), not background (202)
Spec §8.2 wants 202 + background + `run_ids`, with the frontend polling `GET /api/scheduler/runs` (**spec 15 — NOT built**). `run_job` is synchronous and returns `None`. So v1: run the per-market jobs **synchronously on the request connection** and return real `run_ids` (200). Justified: nothing can poll yet; `run_job` **swallows** provider errors (logs `job_runs.status="error"`, never raises) so a failed fetch yields a logged run, not a 500; providers are faked in tests. When spec 15 lands, this can move to a background task. (Noted reconciliation; the senior review will scrutinize.)

## Verified shapes
- `scheduler.jobs.run_job(conn, job_id, *, now) -> None` (CURRENT) — inserts a `job_runs` row (`run_id = cur.lastrowid`), runs `spec.func`, finalizes status/detail; swallows func exceptions. Job ids: `quotes_tw`, `quotes_us`, `quotes_my` (+ history_daily, dividends_daily). `trigger_job(job_id) -> None`.
- `scheduler.jobs.create_scheduler_tables(conn)` creates `schedule_config` + `job_runs`. **The api_client/golden_db fixture (tests/conftest.py) calls `bootstrap_db` + `create_pricing_tables` but may NOT create the scheduler tables** — verify; if `job_runs` is absent in the fixture DB, `refresh-quotes` tests need it. (The scheduler tests use their own conftest that calls `create_scheduler_tables`.)
- `portfolio.cost_basis.build_book(transactions, dividends, opening, instruments) -> Book`; raises `OversellError` (cost_basis.py) when a sell exceeds holdings.
- Ledger loading pattern: see `portfolio/dashboard.py` `build_dashboard` steps 1 (maps `store.list_transactions/list_dividends/list_fx_conversions/list_opening` + `list_instruments` → `shared.models.ledger` models). Reuse that mapping.
- `shared.enums.Market` (TW/US/MY). Phase-0 api: `get_conn`, `get_now`; `api/errors.error_body`; `JSONResponse`.

---

### Task 1: `run_job` returns its `run_id`

**Files:** Modify `portfolio_dash/scheduler/jobs.py`; Test: `tests/scheduler/test_jobs.py` (append) — or wherever run_job is tested.

- [ ] **Step 1: failing test** — append to the existing scheduler jobs test (find the file via `grep -rl "run_job" tests/scheduler`):
```python
def test_run_job_returns_run_id(conn) -> None:  # conn fixture has scheduler tables
    from datetime import UTC, datetime
    from portfolio_dash.scheduler.jobs import run_job
    rid = run_job(conn, "history_daily", now=datetime(2026, 6, 11, tzinfo=UTC))
    assert isinstance(rid, int)
    row = conn.execute("SELECT id FROM job_runs WHERE id=?", (rid,)).fetchone()
    assert row is not None
```
(Use a job id that exists and is safe with faked/empty providers; `history_daily` over an empty instruments table is a no-op summary. If the scheduler test conftest injects fakes, follow its pattern.)

- [ ] **Step 2: run, expect fail** (run_job returns None → `isinstance(None, int)` False).

- [ ] **Step 3: implement** — change `run_job` signature `-> None` to `-> int` and `return run_id` at the end. `run_id = cur.lastrowid` is `int | None`; coerce: `run_id = int(cur.lastrowid or 0)` right after the insert, and `return run_id` after the finalize UPDATE. Update `trigger_job` (it calls `run_job`; it can stay `-> None`, just not return — or return the id; keep `-> None` and ignore).

- [ ] **Step 4: run, expect pass.** Run the full scheduler suite (`./.venv/Scripts/python.exe -m pytest tests/scheduler -q`) — no regression.

- [ ] **Step 5: gates + commit**
```bash
./.venv/Scripts/python.exe -m mypy portfolio_dash tests --strict ; ./.venv/Scripts/python.exe -m ruff check portfolio_dash tests
git add portfolio_dash/scheduler/jobs.py tests/scheduler/
git commit -m "feat(scheduler): run_job returns its job_runs id (for the refresh action)"
```

---

### Task 2: `POST /api/actions/refresh-quotes`

**Files:** Create `portfolio_dash/api/routers/actions.py`; Modify `portfolio_dash/api/app.py`; Test `tests/contract/test_actions_api.py`. Possibly modify `tests/conftest.py` (scheduler tables in golden_db).

- [ ] **Step 1: ensure scheduler tables in the test DB.** Check `tests/conftest.py` `golden_db`: if it does not call `create_scheduler_tables(conn)`, add it (import from `portfolio_dash.scheduler.jobs`) right after `create_pricing_tables(conn)`, and call `ensure_scheduler_seeded(conn)` if needed for job rows. (refresh-quotes only needs the `job_runs` table to exist; `run_job` looks the spec up from the in-code `JOBS` registry, not the DB, so `ensure_job_rows` is not strictly required — but `create_scheduler_tables` IS.)

- [ ] **Step 2: failing test** — create `tests/contract/test_actions_api.py`:
```python
from fastapi.testclient import TestClient


def test_refresh_quotes_all_markets(api_client: TestClient) -> None:
    r = api_client.post("/api/actions/refresh-quotes", json={})
    assert r.status_code == 200
    b = r.json()
    assert set(b["jobs"]) == {"quotes_tw", "quotes_us", "quotes_my"}
    assert len(b["run_ids"]) == 3 and all(isinstance(x, int) for x in b["run_ids"])


def test_refresh_quotes_subset(api_client: TestClient) -> None:
    r = api_client.post("/api/actions/refresh-quotes", json={"markets": ["TW"]})
    assert r.status_code == 200
    assert r.json()["jobs"] == ["quotes_tw"] and len(r.json()["run_ids"]) == 1


def test_refresh_quotes_unknown_market_400(api_client: TestClient) -> None:
    r = api_client.post("/api/actions/refresh-quotes", json={"markets": ["XX"]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "validation_error"
```
> The golden_db has 2330 (TW) + AAPL (US) instruments but the test providers are not wired into `refresh_quotes` here — `run_job` calls the real `default_registry()`. **This will attempt real network unless blocked.** pytest-socket blocks it → `refresh_quotes` raises → `run_job` SWALLOWS it (logs status=error) and still returns a run_id. So the endpoint still returns 200 + 3 run_ids (the runs are logged as errors, which is correct degradation). Assert only on run_ids/jobs/status-200, NOT on fetched prices. Confirm this behavior in Step 4; if `run_job`'s swallow doesn't cover the socket block, report.

- [ ] **Step 3: implement** `portfolio_dash/api/routers/actions.py`:
```python
"""Top-bar actions (spec 08 §8.2–8.3): refresh quotes, recompute."""

import sqlite3
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from portfolio_dash.api.deps import get_conn, get_now
from portfolio_dash.api.errors import error_body
from portfolio_dash.scheduler.jobs import run_job

router = APIRouter()

_MARKET_JOB = {"TW": "quotes_tw", "US": "quotes_us", "MY": "quotes_my"}


class RefreshBody(BaseModel):
    markets: list[str] | None = None


@router.post("/actions/refresh-quotes", status_code=200)
def refresh_quotes_action(
    body: RefreshBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    markets = body.markets if body.markets else list(_MARKET_JOB)
    unknown = [m for m in markets if m not in _MARKET_JOB]
    if unknown:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"未知市場代碼 {unknown[0]}", field="markets"))
    jobs = [_MARKET_JOB[m] for m in markets]
    run_ids = [run_job(conn, job_id, now=now) for job_id in jobs]
    return {"run_ids": run_ids, "jobs": jobs}
```
In `app.py`: add `actions` to the routers import + `app.include_router(actions.router, prefix="/api")`.

- [ ] **Step 4: run, expect pass.** **Step 5: gates + commit**
```bash
./.venv/Scripts/python.exe -m pytest -q ; ./.venv/Scripts/python.exe -m mypy portfolio_dash tests --strict ; ./.venv/Scripts/python.exe -m ruff check portfolio_dash tests
git add portfolio_dash/api/routers/actions.py portfolio_dash/api/app.py tests/contract/test_actions_api.py tests/conftest.py
git commit -m "feat(api): POST /api/actions/refresh-quotes (sync run_job, run_ids) (spec 08.2)"
```

---

### Task 3: `POST /api/actions/recompute`

**Files:** Modify `portfolio_dash/api/routers/actions.py`; Test append to `tests/contract/test_actions_api.py`.

Spec §8.3: body empty `{}` → 200 `{as_of, rebuilt: true}`. On ledger inconsistency (build_book raises `OversellError`) → 422 `{error:{code:"oversell", message}}`. Append-only; no writes.

- [ ] **Step 1: failing test** (append):
```python
def test_recompute_ok(api_client: TestClient) -> None:
    r = api_client.post("/api/actions/recompute", json={})
    assert r.status_code == 200
    b = r.json()
    assert b["rebuilt"] is True and "as_of" in b
```
(The golden ledger is consistent — 2330 buy 1000, AAPL buy 10, a cash dividend — no oversell → 200. An oversell 422 test would require seeding a sell-exceeds-holdings ledger row directly; optional — add only if a clean seed path exists, else note the 422 path is covered by `build_book`'s own unit tests.)

- [ ] **Step 2: run, expect fail.**

- [ ] **Step 3: implement** — add to `actions.py` (mirror the ledger loading from `portfolio/dashboard.py` build_dashboard step 1):
```python
from portfolio_dash.data_ingestion.store import (
    list_dividends, list_instruments, list_opening, list_transactions,
)
from portfolio_dash.portfolio.cost_basis import OversellError, build_book
from portfolio_dash.shared.models.enums import DividendType
from portfolio_dash.shared.models.ledger import Dividend, OpeningInventory, Transaction


@router.post("/actions/recompute", status_code=200)
def recompute(
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    txs = [Transaction(account_id=s.account_id, symbol=s.symbol, side=s.side,
                       quantity=s.quantity, price=s.price, fees=s.fees, tax=s.tax,
                       trade_date=s.trade_date) for s in list_transactions(conn)]
    divs = [Dividend(account_id=s.account_id, symbol=s.symbol, date=s.date,
                     type=DividendType(s.type), gross=s.gross, withholding=s.withholding,
                     net=s.net, reinvest_shares=s.reinvest_shares,
                     reinvest_price=s.reinvest_price) for s in list_dividends(conn)]
    opening = [OpeningInventory(account_id=s.account_id, symbol=s.symbol, shares=s.shares,
                                original_avg_cost=s.original_avg_cost,
                                original_cost_total=s.original_cost_total,
                                build_date=s.build_date) for s in list_opening(conn)]
    instruments = {i.symbol: i for i in list_instruments(conn)}
    try:
        build_book(txs, divs, opening, instruments)
    except OversellError as exc:
        return JSONResponse(status_code=422, content=error_body("oversell", str(exc)))
    return {"as_of": now.isoformat(), "rebuilt": True}
```
> This mirrors `build_dashboard`'s ledger loading exactly (same `Stored* → ledger model` mapping). Recompute validates consistency only (build_book); it writes nothing (stats are derived-on-read elsewhere; no cache layer exists yet to clear).

- [ ] **Step 4: run, expect pass.** **Step 5: gates + commit**
```bash
./.venv/Scripts/python.exe -m pytest -q ; ./.venv/Scripts/python.exe -m mypy portfolio_dash tests --strict ; ./.venv/Scripts/python.exe -m ruff check portfolio_dash tests
git add portfolio_dash/api/routers/actions.py tests/contract/test_actions_api.py
git commit -m "feat(api): POST /api/actions/recompute (build_book validate, oversell 422) (spec 08.3)"
```

---

### Task 4: CHANGELOG + full green (Phase 1 close-out)

- [ ] **Step 1:** Append to `CHANGELOG.md` `[Unreleased] › ### Added` (after the spec-12b bullet):
```markdown
- **Top-bar actions (spec 08 §8.2–8.3, Phase 1 close-out):** `POST /api/actions/refresh-quotes`
  (triggers the per-market `quotes_*` jobs synchronously, returns their `job_runs` ids;
  unknown market → 400) and `POST /api/actions/recompute` (re-runs `build_book` over the
  ledgers to validate consistency, `OversellError` → 422; append-only). `run_job` now returns
  its run id. (Sync 200 instead of the spec's 202-background: the `GET /api/scheduler/runs`
  poll endpoint is spec 15, not yet built; `run_job` swallows provider errors, so a failed
  fetch is a logged run, not a 500. Revisit when spec 15 lands.) **Phase 1 core data flow
  (specs 08/10/11/12) backend complete.**
```
- [ ] **Step 2:** `grep -c "^## \[v" CHANGELOG.md` → `1`.
- [ ] **Step 3:** `./.venv/Scripts/python.exe -m ruff check portfolio_dash tests && ./.venv/Scripts/python.exe -m mypy portfolio_dash tests --strict && ./.venv/Scripts/python.exe -m pytest -q` → clean, 0 failed.
- [ ] **Step 4:** `git add CHANGELOG.md && git commit -m "docs: CHANGELOG for spec 08 actions (Phase 1 close-out)"`

## Self-review
- Coverage: §8.2 refresh-quotes (T2) + §8.3 recompute (T3); run_job id (T1). Decision: sync/200 documented. recompute mirrors build_dashboard's ledger loading.
- Watch: golden_db must have `job_runs` (T2 Step 1); refresh-quotes relies on `run_job` swallowing the pytest-socket network block (verify in T2 Step 4 — if it doesn't, the test must inject faked providers or the endpoint needs adjustment; report).
- Append-only honored (recompute writes nothing; refresh writes prices via the existing job path).
