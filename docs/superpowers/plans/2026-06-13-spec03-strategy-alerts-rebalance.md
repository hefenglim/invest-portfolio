# Spec 03 — strategy/ module: alerts engine, what-if & rebalance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Move three browser-mirrored calculations to the backend as the single source of truth, in a new `strategy/` module: a risk-alert rule engine (embedded in the dashboard payload + `GET /api/alerts`), buy/sell what-if (`POST /api/whatif`), and rebalance preview (`POST /api/rebalance/preview`), plus editable alert thresholds (`GET/PUT /api/alert-rules`).

**Architecture:** `strategy/` holds **pure functions over already-computed outputs** — it consumes `DashboardData` (from `portfolio.dashboard.build_dashboard`) and the fee engine (`data_ingestion.fees.compute_fees`); it **never writes the ledger**. Dependency direction: `web_ui → strategy → {portfolio, data_ingestion, shared}` (strategy→portfolio is allowed per `architecture.md`). The alert engine is single-sourced: `compute_alerts_from(data, rules, quota_remaining, quota_threshold)` is the one rule function; both the dashboard payload and `GET /api/alerts` call it.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, sqlite3, `decimal.Decimal`, pytest + FastAPI TestClient.

---

## Reconciliation decisions (read before starting)

1. **`calib_gap` and `calibration_regression` are DEFERRED to spec 04.** Their data source (AI calibration / ai-score) does not exist yet. v1 implements the other six rules only; these two are omitted from both the alert-rules config and `compute_alerts` (add when spec 04 lands). Record in CHANGELOG.
2. **`quota_low` threshold is NOT stored in alert-rules.** It reads `shared.llm_config.get_alert_threshold(conn)` (spec 16 single source of truth, default `1.00`). In alert-rules `quota_low` is an enable/disable toggle with `value=null`. `remaining == 0` → severity escalates `warn → risk`.
3. **`stale_price` / `missing_price` are toggle rules** driven by `DashboardData.freshness` (no numeric threshold; `value=null`).
4. **Single alert function, no double-build.** `compute_alerts_from(data, rules, quota_remaining, quota_threshold) -> list[Alert]` is pure over `DashboardData`. The dashboard router (which already built `DashboardData`) calls it directly with the data it has. `GET /api/alerts` uses a thin wrapper `compute_alerts(conn, *, now, reporting)` that builds the dashboard once, reads rules+quota, and calls the same core. Both share identical rule logic. (Honors the SR "same calculation function" intent without building the dashboard twice on the dashboard path.)
5. **`account_id` default (SR Q1):** what-if and rebalance-sell omitting `account_id` default to the account holding the MOST shares of that symbol; echo the chosen `account_id` back. Never split a sell across accounts.
6. **what-if reuses the real fee engine** (`compute_fees`) via the account's `FeeRuleSet` — compute, no write. `oversell=true` still returns full numbers (soft warning, mirroring the write-path intercept).
7. **rebalance uses the same current spot rates as the dashboard** (the promoted `RateResolver`); missing-price symbols are excluded (listed in `summary.excluded`), never faked.

---

## File structure

- Create `portfolio_dash/strategy/__init__.py`
- Create `portfolio_dash/strategy/rules_config.py` — `AlertRules` model + defaults/bounds, `alert_rules` table (create/seed), `get_alert_rules` / `set_alert_rules` / `ensure_alert_rules_seeded`.
- Create `portfolio_dash/strategy/alerts.py` — `Alert` model, `compute_alerts_from(...)` pure core, `compute_alerts(conn, *, now, reporting)` wrapper.
- Create `portfolio_dash/strategy/whatif.py` — `compute_whatif(...)`.
- Create `portfolio_dash/strategy/rebalance.py` — `compute_rebalance(...)`.
- Create `portfolio_dash/api/routers/strategy.py` — the five routes.
- Modify `portfolio_dash/api/routers/dashboard.py` — embed `alerts` in the payload.
- Modify `portfolio_dash/api/app.py` — mount the strategy router + seed alert rules in lifespan.
- Modify `tests/conftest.py` — seed the `alert_rules` table in `golden_db`.
- Tests under `tests/strategy/` and `tests/contract/`.

---

### Task 1: alert-rules config store + GET/PUT /api/alert-rules

**Files:**
- Create `portfolio_dash/strategy/__init__.py` (empty)
- Create `portfolio_dash/strategy/rules_config.py`
- Create `portfolio_dash/api/routers/strategy.py` (with the alert-rules routes; other routes added in later tasks)
- Modify `portfolio_dash/api/app.py` (mount router + lifespan seed)
- Modify `tests/conftest.py` (seed alert_rules in golden_db)
- Test: `tests/strategy/test_rules_config.py`, `tests/contract/test_alert_rules_api.py`

**Rule defaults / bounds** (the six v1 rules):

| id | enabled | value | unit | min | max |
|---|---|---|---|---|---|
| single_weight | true | 0.30 | ratio | 0.05 | 1 |
| sector_weight | true | 0.60 | ratio | 0.10 | 1 |
| stale_price | true | null | — | — | — |
| missing_price | true | null | — | — | — |
| fx_drift | true | 0.03 | ratio | 0.005 | 0.50 |
| exdiv_upcoming | true | 14 | days | 1 | 90 |
| quota_low | true | null | — | — | — |

- [ ] **Step 1: Write failing tests.** `tests/strategy/test_rules_config.py`:

```python
import sqlite3
from decimal import Decimal

from portfolio_dash.shared import config_store
from portfolio_dash.strategy.rules_config import (
    DEFAULT_RULES, ensure_alert_rules_seeded, get_alert_rules, set_alert_rules,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_alert_rules_seeded(conn)
    return conn


def test_defaults_seeded() -> None:
    conn = _conn()
    rules = get_alert_rules(conn)
    assert rules.single_weight.enabled is True
    assert rules.single_weight.value == Decimal("0.30")
    assert rules.exdiv_upcoming.value == Decimal("14")
    assert rules.quota_low.value is None


def test_roundtrip_set_get() -> None:
    conn = _conn()
    rules = get_alert_rules(conn)
    rules.single_weight.value = Decimal("0.25")
    rules.fx_drift.enabled = False
    set_alert_rules(conn, rules)
    got = get_alert_rules(conn)
    assert got.single_weight.value == Decimal("0.25")
    assert got.fx_drift.enabled is False
```

`tests/contract/test_alert_rules_api.py`:

```python
from fastapi.testclient import TestClient


def test_get_alert_rules(api_client: TestClient) -> None:
    r = api_client.get("/api/alert-rules")
    assert r.status_code == 200
    rules = {row["id"]: row for row in r.json()["rules"]}
    assert rules["single_weight"]["value"] == "0.30"
    assert rules["single_weight"]["unit"] == "ratio"
    assert rules["quota_low"]["value"] is None  # threshold lives in LLM quota config
    assert "calib_gap" not in rules  # deferred to spec 04


def test_put_alert_rules_overwrites(api_client: TestClient) -> None:
    body = {"rules": [
        {"id": "single_weight", "enabled": True, "value": "0.25"},
        {"id": "fx_drift", "enabled": False, "value": "0.03"},
    ]}
    r = api_client.put("/api/alert-rules", json=body)
    assert r.status_code == 200
    # PUT echoes the full ruleset back (merged over current), so omitted rules keep defaults
    rules = {row["id"]: row for row in r.json()["rules"]}
    assert rules["single_weight"]["value"] == "0.25"
    assert rules["fx_drift"]["enabled"] is False
    assert rules["sector_weight"]["value"] == "0.60"  # untouched default preserved


def test_put_out_of_bounds_400(api_client: TestClient) -> None:
    r = api_client.put("/api/alert-rules",
                       json={"rules": [{"id": "single_weight", "enabled": True, "value": "2.0"}]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "validation_error"
```

- [ ] **Step 2:** Run, confirm failures.

- [ ] **Step 3: Implement `strategy/rules_config.py`.** Model each rule as a small object `{enabled: bool, value: Decimal | None}`. Persist as a single-row JSON table `alert_rules_config (id INTEGER PRIMARY KEY CHECK (id = 1), rules_json TEXT)`. Store `value` as Decimal **string** in JSON (never float). Provide static metadata (unit/min/max) per rule id for the API response. Use `config_store.ensure_seeded(conn, "alert_rules", create=..., seed=...)`. Key elements:

```python
"""Editable alert-rule thresholds (spec 03 §3.1). Single-row JSON config; pure config,
no ledger writes. quota_low's threshold is NOT here — it reads shared.llm_config
(spec 16 single source of truth). calib_gap/calibration_regression are deferred (spec 04)."""

import json
import sqlite3
from decimal import Decimal

from pydantic import BaseModel

from portfolio_dash.shared import config_store


class Rule(BaseModel):
    enabled: bool = True
    value: Decimal | None = None


class AlertRules(BaseModel):
    single_weight: Rule
    sector_weight: Rule
    stale_price: Rule
    missing_price: Rule
    fx_drift: Rule
    exdiv_upcoming: Rule
    quota_low: Rule


# (id, default_value | None, unit | None, min | None, max | None) — static metadata.
RULE_META: list[tuple[str, str | None, str | None, str | None, str | None]] = [
    ("single_weight", "0.30", "ratio", "0.05", "1"),
    ("sector_weight", "0.60", "ratio", "0.10", "1"),
    ("stale_price", None, None, None, None),
    ("missing_price", None, None, None, None),
    ("fx_drift", "0.03", "ratio", "0.005", "0.50"),
    ("exdiv_upcoming", "14", "days", "1", "90"),
    ("quota_low", None, None, None, None),
]

DEFAULT_RULES = AlertRules(**{
    rid: Rule(enabled=True, value=(Decimal(dv) if dv is not None else None))
    for rid, dv, _u, _mn, _mx in RULE_META
})
```

Implement: `_create(conn)` (CREATE TABLE IF NOT EXISTS), `_seed(conn)` (INSERT id=1 with DEFAULT_RULES serialized — Decimals→str via a small encoder), `ensure_alert_rules_seeded(conn)` (delegates to `config_store.ensure_seeded`), `get_alert_rules(conn) -> AlertRules` (read row → parse JSON; fall back to DEFAULT_RULES if row missing), `set_alert_rules(conn, rules)` (UPSERT id=1). Serialize Decimals as strings in JSON; parse back to Decimal on read. Validate bounds in `set_alert_rules` is NOT required (the router validates), but DO clamp/validate type on parse.

- [ ] **Step 4: Implement `api/routers/strategy.py`** (alert-rules routes). The GET returns `{"rules": [{"id", "enabled", "value", "unit", "min", "max"}]}` (value as Decimal string or null; unit/min/max from RULE_META). The PUT accepts `{"rules": [{"id", "enabled", "value"}]}`, **merges over current** (omitted rules keep their stored value), validates each value against its `[min,max]` bound (out of bounds → 400 `validation_error` via `error_body`, returned as `JSONResponse`), persists, and **echoes the full ruleset back in the GET shape** (so the frontend writes the recomputed set — and, after Task 2, the recomputed alerts; for Task 1 just echo rules). Use `get_conn` dep. Mirror the error-envelope conventions in `ledgers.py`/`export.py`.

- [ ] **Step 5: Mount + seed.** In `app.py`: add `strategy` to the routers import + `app.include_router(strategy.router, prefix="/api")`; in the lifespan `with session() as conn:` block add `ensure_alert_rules_seeded(conn)` (import from `strategy.rules_config`). In `tests/conftest.py` `golden_db` fixture, add `from portfolio_dash.strategy.rules_config import ensure_alert_rules_seeded` and call `ensure_alert_rules_seeded(conn)` alongside the other seeders.

- [ ] **Step 6:** Run tests → pass. **Step 7:** Gates (`pytest -q`, `mypy --strict portfolio_dash`, `ruff check portfolio_dash tests`) clean. **Step 8:** Commit:

```bash
git add portfolio_dash/strategy/__init__.py portfolio_dash/strategy/rules_config.py portfolio_dash/api/routers/strategy.py portfolio_dash/api/app.py tests/conftest.py tests/strategy/ tests/contract/test_alert_rules_api.py
git commit -m "feat(strategy): alert-rules config store + GET/PUT /api/alert-rules (spec 03)"
```

---

### Task 2: compute_alerts engine + GET /api/alerts + dashboard embed

**Files:**
- Create `portfolio_dash/strategy/alerts.py`
- Modify `portfolio_dash/api/routers/strategy.py` (add `GET /api/alerts`; PUT alert-rules now also returns recomputed alerts)
- Modify `portfolio_dash/api/routers/dashboard.py` (embed `alerts`)
- Test: `tests/strategy/test_alerts.py`, `tests/contract/test_alerts_api.py`

**`Alert` model:** `id: str`, `sev: Literal["risk","warn","info"]`, `rule: str`, `title: str`, `detail: str`, `href: str | None`.

**Rule logic** (`compute_alerts_from(data: DashboardData, rules: AlertRules, quota_remaining: Decimal, quota_threshold: Decimal) -> list[Alert]`):
- `single_weight`: for each `data.holdings` with `weight is not None and weight > value` → `risk`, `id=f"single_weight:{symbol}"`, `href=f"/symbol/{symbol}"`.
- `sector_weight`: for each sector in `data.allocation.weights` (if allocation not None) with `weight > value` → `risk`, `id=f"sector_weight:{sector}"`.
- `stale_price`: for each `data.freshness.prices` with `stale` → `warn`, `id=f"stale_price:{symbol}"`.
- `missing_price`: for each symbol in `data.freshness.missing_prices` → `warn`, `id=f"missing_price:{symbol}"`.
- `fx_drift`: if `data.fx` not None, for each `by_account` result with `avg_rate` and `current_spot` not None and `abs(current_spot/avg_rate - 1) > value` → `info`, `id=f"fx_drift:{account_id}"`.
- `exdiv_upcoming`: for each `data.ex_dividend_calendar` item with `0 <= (ex_date - as_of).days <= value_days` → `info`, `id=f"exdiv_upcoming:{symbol}"` (use `data.as_of.date()`).
- `quota_low`: if `quota_remaining < quota_threshold` → `warn`; if `quota_remaining == 0` → `risk`. `id="quota_low"`.
- Each rule only fires when `rules.<id>.enabled`. Titles/details are short human strings (English in code; the frontend localizes display). Skip a rule cleanly when its input is None (degrade, never raise).

`compute_alerts(conn, *, now, reporting) -> list[Alert]`: build the dashboard, read `get_alert_rules(conn)`, `budget_remaining(conn)`, `get_alert_threshold(conn)`, call the core.

- [ ] **Step 1: Write failing tests.** `tests/strategy/test_alerts.py` builds a `DashboardData` (or uses the golden one via `build_dashboard(golden_db, now=GOLDEN_NOW, reporting=TWD)`) and asserts, e.g.: the golden 2330 holding at weight ~0.94 (600000/(600000+39600)) fires `single_weight:2330` as `risk`; a `quota_remaining=0` with threshold 1.00 fires `quota_low` as `risk`. Add a focused unit test of `compute_alerts_from` with a hand-built minimal `DashboardData` for `fx_drift` and `exdiv_upcoming` (since the golden FX avg≈spot and the dividend is past). `tests/contract/test_alerts_api.py`: `GET /api/alerts` returns `{"as_of", "alerts"}`; the dashboard payload (`GET /api/dashboard`) includes an `alerts` array with the SAME entries (assert the `single_weight:2330` id appears in both).

- [ ] **Step 2:** Run, confirm failures. **Step 3:** Implement `strategy/alerts.py`. **Step 4:** Add `GET /api/alerts` to `strategy.py` (calls `compute_alerts`, returns `{"as_of": now-as-iso, "alerts": [to_wire...]}`); and make the Task-1 `PUT /api/alert-rules` ALSO return the recomputed `alerts` (call `compute_alerts` after persisting) — response `{"rules": [...], "alerts": [...]}`. **Step 5:** In `dashboard.py`, after building the payload, compute alerts from the already-built `DashboardData` (call `compute_alerts_from` with the data + rules + quota, NOT a second `build_dashboard`) and set `payload["alerts"] = to_wire([...])`. Import the rule/quota readers.

- [ ] **Step 6:** Tests pass. **Step 7:** Gates clean. **Step 8:** Commit:

```bash
git add portfolio_dash/strategy/alerts.py portfolio_dash/api/routers/strategy.py portfolio_dash/api/routers/dashboard.py tests/strategy/test_alerts.py tests/contract/test_alerts_api.py
git commit -m "feat(strategy): compute_alerts engine (6 v1 rules) + GET /api/alerts + dashboard embed (spec 03)"
```

---

### Task 3: POST /api/whatif

**Files:**
- Create `portfolio_dash/strategy/whatif.py`
- Modify `portfolio_dash/api/routers/strategy.py` (add route + body model)
- Test: `tests/strategy/test_whatif.py`, `tests/contract/test_whatif_api.py`

**Body:** `{symbol, side: "buy"|"sell", shares, price, account_id?}`. If `account_id` omitted → the account holding the most shares of `symbol` (Q1 rule; reuse the `_q1_holding` idea from `symbol.py`); echo `account_id` back. Response per spec §3.2 (buy and sell branches). All money fields Decimal strings.

**Logic** (`compute_whatif(conn, *, now, reporting, symbol, side, shares, price, account_id) -> dict`):
- Build the book (`build_book` over the ledgers, like `symbol.py`) to get the current holding for `(account_id, symbol)`; resolve `account_id` if omitted.
- Resolve the account's `FeeRuleSet` via `config_seed.get_fee_rule_set(account.fee_rule_set)`; `is_etf` from the instrument. Call `compute_fees(rules, side, shares, price, is_etf=...)` → `fee`, `tax`. `fee_rule_desc`: a short human string (compose from the snapshot/rule fields).
- `amount = shares * price`.
- **buy:** `total_cost = amount + fee + tax`; `new_shares = held.shares + shares` (held may be 0); `new_original_avg`, `new_adjusted_avg` recomputed from the held totals + this buy (held.original_cost_total + total_cost) / new_shares (adjusted likewise). Use 0-cost base if unheld.
- **sell:** `oversell = shares > held.shares`; `proceeds_net = amount - fee - tax`; `adjusted_cost_removed = held.adjusted_avg * shares` (use `min(shares, held.shares)` for the cost-removed math but still report oversell true and full numbers — mirror write-path: report `realized = proceeds_net - adjusted_cost_removed`); `remaining_shares = held.shares - shares`.
- `new_weight`: reporting MV of the resulting position / resulting total reporting MV. Reuse `RateResolver` for the symbol's quote→reporting rate and `build_dashboard`'s total (or compute total reporting MV from valued holdings). Keep it honest: if the symbol has no current price, `new_weight=null`.
- Provide a pure core `compute_whatif_from(...)` testable without a DB if practical; otherwise unit-test through a seeded in-memory DB (like `tests/contract/test_export_tax.py`'s `_db_with_sells`).

- [ ] Standard TDD steps (failing test → implement → pass → gates → commit). Tests must cover: buy on an unheld symbol (new_adjusted_avg correct), sell within holdings (realized + remaining_shares), and `oversell=true` returning full numbers. Commit:

```bash
git add portfolio_dash/strategy/whatif.py portfolio_dash/api/routers/strategy.py tests/strategy/test_whatif.py tests/contract/test_whatif_api.py
git commit -m "feat(strategy): POST /api/whatif buy/sell sim (reuses fee engine; compute-no-write) (spec 03)"
```

---

### Task 4: POST /api/rebalance/preview

**Files:**
- Create `portfolio_dash/strategy/rebalance.py`
- Modify `portfolio_dash/api/routers/strategy.py` (add route + body model)
- Test: `tests/strategy/test_rebalance.py`, `tests/contract/test_rebalance_api.py`

**Body:** `{targets: {symbol: ratio-string}}` (ratios of reporting-ccy MV). Response per spec §3.3: `rows` (per symbol: current_weight, target_weight, side, integer shares to trade — MY market rounds to 100-unit lots, amount, ccy, fee, tax, new_weight) + `summary` (turnover_reporting, total_fees_reporting, cash_after, excluded[] for missing-price symbols).

**Logic** (`compute_rebalance(conn, *, now, reporting, targets) -> dict`):
- Build the dashboard once (current weights, reporting total MV, per-symbol market_price + quote_ccy + market_value, freshness). Use the same `RateResolver` spot rates.
- For each target symbol: `target_mv_reporting = target_ratio * total_reporting_mv`; current reporting MV of the position; delta in reporting → convert to quote-ccy shares at `market_price` (skip + add to `excluded` if no price). Round shares to an integer; **MY market → round to nearest 100-unit board lot** (`market.MY`). `side = buy|sell` by delta sign. Fee/tax via `compute_fees` on the account holding the symbol (Q1 account; sell does not split across accounts). `amount = shares * price` (quote ccy). `new_weight` = resulting reporting MV / total.
- `summary.turnover_reporting` = Σ|trade amount in reporting|; `total_fees_reporting` = Σ(fee+tax) converted to reporting; `cash_after` — net reporting cash delta from the trades (sells add, buys subtract, minus fees); `excluded` = symbols with no price.
- Targets reference symbols not currently held → treat current weight 0 (a buy). Symbols held but not in `targets` → not traded (left as-is) unless the spec implies full rebalance; v1: only act on symbols present in `targets` (document this).

- [ ] Standard TDD steps. Tests: a two-symbol target on the golden holdings produces sell/buy rows with integer shares + per-currency amounts; a target referencing a missing-price symbol lands in `excluded`; MY-market lot rounding (add an MY holding in a seeded DB to assert 100-lot rounding). Commit:

```bash
git add portfolio_dash/strategy/rebalance.py portfolio_dash/api/routers/strategy.py tests/strategy/test_rebalance.py tests/contract/test_rebalance_api.py
git commit -m "feat(strategy): POST /api/rebalance/preview (target-weight trades; MY 100-lot; excluded missing price) (spec 03)"
```

---

## CHANGELOG (end, before final review)

Add to `[Unreleased]` Added: the `strategy/` module + the five endpoints + dashboard `alerts` embed; record reconciliations: calib_gap/calibration_regression deferred to spec 04; quota_low threshold sourced from spec-16 LLM quota config; alerts single-sourced via `compute_alerts_from`; rebalance v1 acts only on symbols present in `targets`. Verify `grep -c "^## \[v" CHANGELOG.md` is unchanged (1).

## Self-review (controller, after all tasks)

1. Boundary: `strategy/` imports only portfolio/data_ingestion/shared; writes no ledger; routers thin.
2. Money: every wire cell `str(Decimal)`; FX only via `convert`/`RateResolver`; fee/tax only via `compute_fees`.
3. Single-source: `GET /api/alerts` and the dashboard `alerts` produce identical entries from one rule function.
4. Degradation: missing price/FX → excluded/blank/null, never fabricated; deferred AI rules absent, not stubbed-with-fake-data.
