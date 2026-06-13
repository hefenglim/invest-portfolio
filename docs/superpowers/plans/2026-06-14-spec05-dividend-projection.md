# Spec 05 — Annual Dividend Cash-Flow Projection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** Add a `dividend_projection` field to the dashboard payload: per-currency declared gross/net dividend cash flow for the current year, with net applying each holding account's dividend model. Single task — backend-only, no new endpoint.

**Architecture:** A pure function `portfolio/dividends.py::project_dividends(...)` over already-computed outputs (valued holdings + ex-dividend calendar + accounts + instruments), wired into `build_dashboard`. The dashboard router auto-serializes it (it's part of `DashboardData`). No ledger writes.

---

## Reconciliations
1. **v1 = `declared_only`** — only `ex_dividend_calendar` events for held symbols with `ex_date.year == current year` (the calendar is already upcoming-only / held-only). v2 (`declared_plus_estimated`) is out of scope.
2. **Net = withholding model only.** Map `account.dividend_model` → div_type for `apply_dividend_model`: `drip_us` → `"DRIP"` (30% US withholding); `cash_cost_reduction` / `cash` / `net` → `"cash"` (net = gross). The spec mentions a Moomoo-US "$0.99 platform fee" — it is NOT encoded anywhere (no per-dividend fee config; probe-pending per `markets-and-fees.md`), so v1 omits it and applies withholding only. Record this reconciliation in CHANGELOG.
3. **Per-currency, never summed across currencies** (locked invariant). `by_currency` keyed by the event's currency (fallback to the instrument's quote_ccy).
4. **Per-account aggregation:** a symbol held in multiple accounts (e.g. a US symbol in Schwab + Moomoo US) contributes one gross/net per holding (its own account's model), summed into the currency bucket; `events` counts distinct calendar events that contributed.
5. **`dividend_projection: DividendProjection | None = None`** on `DashboardData` (sibling-optional pattern) — `build_dashboard` ALWAYS populates it, so the live payload is never null; the Optional default just avoids breaking the two direct `DashboardData(...)` constructions in tests.

---

### Task 1: dividend projection (model + pure function + build_dashboard wiring)

**Files:**
- Modify `portfolio_dash/portfolio/dashboard_models.py` — add `DividendProjectionCurrency` + `DividendProjection`; add the field to `DashboardData`.
- Create `portfolio_dash/portfolio/dividends.py` — `project_dividends(...)`.
- Modify `portfolio_dash/portfolio/dashboard.py` — compute + set the field.
- Test: `tests/portfolio/test_dividend_projection.py` (unit), `tests/contract/test_dividend_projection_api.py` (payload).

**Models** (`dashboard_models.py`):
```python
class DividendProjectionCurrency(BaseModel):
    declared_gross: Decimal
    declared_net: Decimal
    events: int


class DividendProjection(BaseModel):
    year: int
    by_currency: dict[Currency, DividendProjectionCurrency]
    basis: str = "declared_only"
```
Add to `DashboardData`: `dividend_projection: DividendProjection | None = None`.

**Pure function** (`portfolio/dividends.py`):
```python
"""Annual declared-dividend cash-flow projection (spec 05). Pure over computed outputs;
no ledger writes. Net applies each holding account's dividend model (withholding only —
the Moomoo-US per-dividend platform fee is probe-pending and deferred)."""

from collections import defaultdict
from decimal import Decimal

from portfolio_dash.data_ingestion.dividend_model import apply_dividend_model
from portfolio_dash.portfolio.dashboard_models import (
    DividendProjection, DividendProjectionCurrency,
)
from portfolio_dash.portfolio.dashboard_models import ExDividendItem  # if defined there
from portfolio_dash.portfolio.results import Holding
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.assets import Account, Instrument

_ZERO = Decimal("0")
# account.dividend_model -> apply_dividend_model div_type
_MODEL_DIV_TYPE = {"drip_us": "DRIP", "cash_cost_reduction": "cash", "cash": "cash", "net": "net"}


def project_dividends(
    holdings: list[Holding],
    calendar: list[ExDividendItem],
    accounts: dict[str, Account],
    instruments: dict[str, Instrument],
    *,
    year: int,
) -> DividendProjection:
    gross: dict[Currency, Decimal] = defaultdict(lambda: _ZERO)
    net: dict[Currency, Decimal] = defaultdict(lambda: _ZERO)
    events: dict[Currency, int] = defaultdict(int)
    by_symbol: dict[str, list[Holding]] = defaultdict(list)
    for h in holdings:
        if h.shares > _ZERO:
            by_symbol[h.symbol].append(h)
    for ev in calendar:
        if ev.cash_amount is None or ev.ex_date.year != year:
            continue
        ccy = ev.currency or instruments[ev.symbol].quote_ccy
        contributed = False
        for h in by_symbol.get(ev.symbol, []):
            g = h.shares * ev.cash_amount
            div_type = _MODEL_DIV_TYPE.get(accounts[h.account_id].dividend_model, "cash")
            amounts = apply_dividend_model(div_type, gross=g)
            gross[ccy] += g
            net[ccy] += amounts.net
            contributed = True
        if contributed:
            events[ccy] += 1
    by_currency = {
        ccy: DividendProjectionCurrency(declared_gross=gross[ccy], declared_net=net[ccy],
                                        events=events[ccy])
        for ccy in gross
    }
    return DividendProjection(year=year, by_currency=by_currency, basis="declared_only")
```
NOTE: confirm where `ExDividendItem` is defined (it's in `dashboard_models.py`) — import correctly; the calendar passed in is `list[ExDividendItem]`. Confirm `Account` has a `.dividend_model: str` attribute (it does — `list_accounts` returns it). If `Account` lacks `dividend_model`, read it the way `accounts.py`/`build_dashboard` reads account metadata.

**Wire into `build_dashboard`** (`dashboard.py`): after the ex-dividend `calendar` is built and `valued` holdings exist, compute `project_dividends(valued, calendar, accounts, instruments, year=as_of.year)` and pass `dividend_projection=...` into the `DashboardData(...)` return.

- [ ] **Step 1: failing unit test** `tests/portfolio/test_dividend_projection.py`: build holdings (2330 in tw_broker 1000 sh; AAPL in schwab 10 sh), a calendar (2330 ex-div cash 5 TWD in `year`; AAPL ex-div cash 0.50 USD in `year`), the seeded accounts (use `config_seed.DEFAULT_ACCOUNTS` → build `{a.account_id: a}` Account map, or `list_accounts` over a seeded conn), instruments. Assert: `by_currency[TWD]` gross 5000 / net 5000 / events 1; `by_currency[USD]` gross 5.00 / net 3.50 (DRIP 30%) / events 1; `basis == "declared_only"`. Add a case: an event with `ex_date` in a DIFFERENT year is excluded; a `cash_amount=None` (stock) event is excluded.

- [ ] **Step 2:** run → fail. **Step 3:** implement models + function + wiring. **Step 4:** contract test `tests/contract/test_dividend_projection_api.py`: seed an ex-dividend event into `golden_db` via `pricing.store.upsert_dividend_events` for `2330` (held) with `ex_date` = 2026-12-01, `cash_amount=5`, then `GET /api/dashboard` and assert `body["dividend_projection"]["year"] == 2026`, `body["dividend_projection"]["basis"] == "declared_only"`, `by_currency["TWD"]["declared_net"] == "5000"`, money fields are strings, currency key is UPPER. Also assert the field is present even when no events (a second test on the plain `api_client` golden DB: `dividend_projection` present, `by_currency == {}`). NOTE: `upsert_dividend_events` signature — read `pricing/store.py:171` and `DividendEvent` shape before seeding.

- [ ] **Step 5: run full suite, fix breakage.** Adding the `DashboardData` field is Optional-default-None, so direct constructions in `tests/portfolio/test_dashboard_models.py` and `tests/strategy/test_alerts.py` should NOT break (they omit it → None). Confirm. Any contract test pinning the dashboard payload key-set must add `dividend_projection` (the existing `test_dashboard_api.py` uses targeted asserts — safe).

- [ ] **Step 6: gates** (`pytest -q`, `mypy --strict portfolio_dash`, `ruff check portfolio_dash tests`) clean. **Step 7: commit:**
```bash
git add portfolio_dash/portfolio/dashboard_models.py portfolio_dash/portfolio/dividends.py portfolio_dash/portfolio/dashboard.py tests/portfolio/test_dividend_projection.py tests/contract/test_dividend_projection_api.py
git commit -m "feat(portfolio): dividend_projection in dashboard payload (declared-only, per-account net) (spec 05)"
```

## CHANGELOG (controller, before review)
`[Unreleased]` Added: `dividend_projection` dashboard field (spec 05) — declared-only annual cash flow, per-currency (never summed), net via each holding account's dividend model; reconciliation: Moomoo-US per-dividend platform fee deferred (probe-pending), v1 net applies withholding only.

## Self-review
Money Decimal-strings; per-currency never summed; net only via `apply_dividend_model`; pure function, no writes; degrades to empty `by_currency` when no events.
