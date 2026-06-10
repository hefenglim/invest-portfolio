# Design: Dashboard Combiner (`portfolio/dashboard.py`)

- **Date:** 2026-06-10
- **Status:** Approved (design); pending spec review
- **Modules:** `portfolio/` (new `dashboard.py`, `dashboard_models.py`, `timeseries.py`),
  `pricing/store.py` (two new read helpers), `data_ingestion/store.py` (one new read helper).
- **Depends on:** `shared/`, `data_ingestion` (ledger reads), `pricing` (price/FX/dividend-event
  reads), `forex` (FX P&L) — introducing **one new one-way dependency edge: `portfolio → forex`**
  (forex imports only `shared`, so no cycle). Recorded here and in `CHANGELOG.md`.
- **Consumers:** `web_ui/` (next sub-project) renders it; `llm_insight/` (future) reads the same
  model as its computed-portfolio input. Both are allowed to import `portfolio`.

## Context & purpose

The web layer must read computed results, never compute (CLAUDE.md invariant; `architecture.md`).
This sub-project builds the **orchestration combiner**: one entry point that reads SQLite (ledgers +
prices + FX), calls the existing calculation cores (`portfolio`, `forex`), and assembles **one
complete dashboard data model** (`DashboardData`) — the data contract the UI binds to. The
Claude Design brief is written from this contract's shapes after it ships.

`forex.compute_fx_summary`'s docstring already anticipates this role: `foreign_exposure` is
"supplied by the orchestrator from the portfolio core's valued holdings". This module is that
orchestrator.

## Decisions (settled 2026-06-10, human sign-off)

1. **The combiner lives in `portfolio/`** (`portfolio/dashboard.py`), not `web_ui/` and not a new
   top-level module. Rationale: its output is computed results (weights, blended rates, daily
   valuation series are calculations, which live in `portfolio/`/`forex/` by rule); both future
   consumers may import `portfolio`; the architecture map already has `portfolio → pricing` and
   `portfolio → data_ingestion`. The only new edge is `portfolio → forex` (one-way, acyclic).
2. **Full section scope** ("完整版"): core sections (KPI summary, holdings table, realized P&L,
   sector allocation, currency combined view, FX P&L, freshness) **plus** ex-dividend calendar,
   dividend-history summary, and an insight-card placeholder shape (so the Design brief can lay
   out the card section before `llm_insight/` exists).
3. **Daily replay trend series included** ("含逐日重放序列"): portfolio total market value and
   cumulative net invested per day, computed by replaying the ledgers day by day against stored
   price/FX history. Correct (rebuild-from-ledgers invariant), and trivially fast at this scale
   (< 2,400 tx/year × ~365 replay dates).
4. **Read-only**: `build_dashboard` never fetches; it reads what `pricing/` has stored
   (refresh is the scheduler's job — dashboard reads SQLite, per `data-and-pricing.md`).
5. **Cold-start degradation**: blended/reporting-level figures degrade to `None`/empty with
   explicit freshness reasons; per-position and per-currency-free data still renders. Never
   fabricate, never crash (details below).

## Contract: `portfolio/dashboard_models.py`

All money/quantity/rate fields are `Decimal` at full precision. The contract does **no** display
formatting (thousands separators, decimal places are template concerns). Models are Pydantic.

```
DashboardData
├ as_of: datetime
├ reporting_currency: Currency
├ kpis: KpiSummary
├ holdings: list[HoldingRow]
├ realized: RealizedPnL                      # existing portfolio model, attached as-is
├ returns: ReturnSummary | None              # existing model; None on cold start (FX missing)
├ allocation: SectorAllocation | None        # existing model; None on cold start
├ currency_view: CombinedView | None         # existing model; None on cold start
├ fx: FXSummary | None                       # existing forex model; None on cold start (see below)
├ dividends: DividendSummary
├ ex_dividend_calendar: list[ExDividendItem]
├ trend: TrendSeries
├ freshness: FreshnessReport
└ insights: list[InsightCardStub]            # always [] until llm_insight exists
```

New models:

- **`HoldingRow`** — flattened (template-friendly) enriched holding: all `Holding` fields
  (account_id, symbol, quote_ccy, shares, original_avg, adjusted_avg, original_cost_total,
  adjusted_cost_total, dividend_portion, payback_ratio, market_price, market_value,
  unrealized_pnl, capital_gain, price_stale) **plus** `account_name: str`, `name: str`,
  `market: Market`, `sector: str`, `board: str`, `price_as_of: date | None`,
  `weight: Decimal | None` (share of reporting total market value; `None` when the holding is
  unvalued or reporting FX is unavailable).
- **`KpiSummary`** — `reporting_currency` plus all-`Optional[Decimal]` blended figures:
  `total_market_value`, `total_return`, `total_return_rate` (= blended total return / blended
  original gross invested, both converted at current spot), `realized_total`, `unrealized_total`,
  `xirr`, `fx_realized`, `fx_unrealized`. `None` whenever the blend cannot be formed honestly.
  XIRR is surfaced **only** here; `ReturnSummary.xirr` stays `None` (single-sourced, no
  duplicate field to drift).
- **`DividendSummary`** — `by_year: list[DividendYearRow]` (ascending; each
  `DividendYearRow(year: int, by_currency: dict[Currency, Decimal])` = **native-currency net**
  totals from the dividend ledger — no FX conversion, hence exact) and
  `total_by_currency: dict[Currency, Decimal]`.
- **`ExDividendItem`** — `symbol`, `name`, `ex_date`, `pay_date: date | None`,
  `cash_amount: Decimal | None`, `stock_amount: Decimal | None` (TW 配股),
  `currency: Currency | None`, `source: str`. Built from
  `pricing.store.get_dividend_events` for **held** symbols, filtered to `ex_date >= as_of.date()`,
  ascending by ex_date.
- **`TrendSeries`** — `points: list[TrendPoint]`, `reporting_currency`, `available: bool`
  (False → `points == []` + freshness reason). `TrendPoint(date, total_value: Decimal,
  net_invested: Decimal, incomplete: bool)`.
- **`FreshnessReport`** — `prices: list[PriceFreshness]` (one per held symbol:
  `symbol, as_of: date | None, stale: bool`; `as_of None` = no stored price),
  `fx: list[FxFreshness]` (one per required pair: `base, quote, as_of: date | None, stale: bool`),
  `any_stale: bool`, `missing_prices: list[str]`, `missing_fx: list[str]` (e.g. `"USD/TWD"`),
  `xirr_unavailable_reason: str | None`, `trend_unavailable_reason: str | None`.
- **`InsightCardStub`** — `id: str`, `title: str`, `body: str`, `generated_at: datetime`.
  Placeholder shape for the Design brief; the combiner always returns `[]` for now.

## Entry point: `portfolio/dashboard.py`

`build_dashboard(conn, *, now: datetime, reporting: Currency) -> DashboardData`

Explicit arguments (no hidden settings read) — the caller (web_ui route / llm_insight run / test)
supplies `now` and the reporting currency from `shared.config`.

Assembly sequence:

1. **Read ledgers**: `list_transactions` / `list_dividends` / `list_fx_conversions` /
   `list_opening` (map `Stored*` → `shared.models.ledger` models), `list_instruments` (→
   `dict[str, Instrument]`), `list_accounts` (new; → `dict[str, Account]`).
2. **Prices**: `get_latest_price` per held symbol → `price_map: dict[str, Decimal]` + per-symbol
   freshness rows. (Held symbols are known after step 3's book — implementation may build the
   book first, then price its holdings; sequencing inside the function is free as long as the
   contract holds.)
3. **Current-FX resolver** (private helper in `dashboard.py`): `rate(base, quote)` → identity 1
   when `base == quote`; else latest stored `(base, quote)` via `get_fx`; else the **inverse** of
   latest `(quote, base)` (`1/rate`); else record the pair as missing and raise `KeyError`
   internally. Required pairs: every holdings/realized currency → reporting, plus each FX-exposed
   account's foreign→home. Staleness is reported, never blocking (last-known rate is used).
4. **Calculation cores**: `build_book` → `value_holdings` → (when all required reporting rates
   resolve) `total_return`, `sector_allocation`, `combined_view`. If any required reporting-pair
   has **no stored row at all** (cold start): `returns`/`allocation`/`currency_view` are `None`,
   blended KPI fields are `None`, and `freshness.missing_fx` lists the pairs.
5. **XIRR**: `xirr_reporting` with `fx_at` backed by the new `get_fx_on` (most recent rate **on or
   before** the flow date — never a later rate; "never guess backwards"). Any flow date with no
   on-or-before rate, or a missing current price for a held symbol → XIRR `None` +
   `xirr_unavailable_reason`. The whole XIRR step is wrapped so a `KeyError` from `fx_at` degrades
   to `None`, never propagates.
6. **FX P&L**: `foreign_exposure` = for each account with `settlement_ccy != funding_ccy`, the sum
   of that account's valued holdings' `market_value` (in settlement ccy) → `compute_fx_summary`
   (its own internal degradation — `None` unrealized on missing spot — is kept as-is). Its
   home→reporting rollup rate is allowed to raise on a missing pair (documented in forex); the
   combiner wraps the call, so a cold-start `KeyError` degrades to `fx = None` + `missing_fx`.
7. **Dividend summary** from the dividend ledger (group net by year × currency).
8. **Ex-dividend calendar** from `get_dividend_events` (held symbols, upcoming only).
9. **Weights**: each valued holding's market value converted to reporting / total; `None` for
   unvalued holdings or when reporting FX is unavailable.
10. **Trend** via `timeseries.daily_value_series` (below).
11. **insights = []**; assemble and return `DashboardData`.

## Trend: `portfolio/timeseries.py`

`daily_value_series(transactions, dividends, opening, instruments, price_history, fx_history,
reporting, *, end: date) -> TrendSeries` — a **pure function** (no DB handle); `dashboard.py`
bulk-loads inputs once via `get_price_history` (per held-ever symbol, from first event date) and
the new `get_fx_history` (per required pair).

- Day range: first ledger event date → `end` (every calendar day; weekends carry forward flat).
- Per day `d`: replay events with date ≤ `d` (reusing `build_book` on the filtered event lists —
  no new replay engine), value the resulting holdings at the **carry-forward** price (latest
  stored price ≤ `d`), convert at carry-forward FX ≤ `d` → `total_value` in reporting.
- `net_invested` (cumulative, reporting ccy, each flow converted at carry-forward FX of its flow
  date): + opening `original_cost_total` at build_date, + buy gross (qty×price+fees+tax),
  − sell net (qty×price−fees−tax), − cash dividend net. DRIP / stock dividends neutral —
  mirrors the XIRR sign conventions (negated), so the chart can plot market value vs invested
  capital.
- A held symbol with **no stored price ≤ d** contributes 0 that day and the point is flagged
  `incomplete=True` (flagged, never guessed).
- Any flow date with **no FX ≤ date** for a needed pair → the series is unreliable as a whole:
  `available=False`, `points=[]`, `trend_unavailable_reason` set (consistent with the XIRR rule).
- Complexity: O(days × events) with tiny constants — no caching, no incremental state.

## New read helpers (small, mechanical)

- **`pricing/store.py`**:
  - `get_fx_on(conn, base, quote, *, on: date) -> FxRead | None` — most recent stored rate with
    `as_of_date <= on`; `stale` always False (point-in-time read, staleness is a latest-quote
    concern, same convention as `get_price_history`).
  - `get_fx_history(conn, base, quote, start, end) -> list[FxRead]` — ascending, mirrors
    `get_price_history`.
- **`data_ingestion/store.py`**:
  - `list_accounts(conn) -> list[Account]` — reads the `accounts` table (seeded by
    `config_seed.seed_accounts`) into `shared.models.assets.Account`. (Today accounts have a
    seed-write but no read API.)

## Architecture / boundaries

- `portfolio/dashboard.py` imports: `shared`, `portfolio.*`, `forex` (new edge),
  `pricing.store`, `data_ingestion.store`. All one-way; nothing imports `web_ui`.
- `forex` continues to import only `shared` — the edge is strictly `portfolio → forex`.
- `web_ui` (next sub-project) and `llm_insight` (future) call `build_dashboard` and read the
  returned model; neither recomputes.
- The combiner derives only assembly-level figures (weights, blends, daily series); the
  authoritative math stays in the existing core functions.

## Error handling / degradation (never crash, never fabricate)

| Situation | Behavior |
| --- | --- |
| A held symbol has no latest price | Holding renders at cost with market fields `None` + `price_stale`; aggregates skip it (existing core behavior); listed in `missing_prices`. |
| A required reporting FX pair has no stored row ever (cold start) | `returns`/`allocation`/`currency_view`/`fx` = `None`; blended KPI fields `None`; pair listed in `missing_fx`; per-currency-free sections (holdings, realized rows, dividends, calendar) render normally. |
| Stale price/FX (old but present) | Used as last-known value; flagged `stale` in freshness — staleness informs, never blocks. |
| XIRR flow predates the earliest stored FX | `xirr = None` + `xirr_unavailable_reason`. (FX-history backfill is a future pricing job, out of scope.) |
| Trend day lacks a price for a held symbol | Point contributes 0 for that symbol + `incomplete=True`. |
| Trend flow lacks on-or-before FX | `trend.available=False`, `points=[]`, reason set. |
| `instruments` row missing for a ledger symbol | Existing core behavior (`KeyError`) — a data-integrity error, loud by design; ingestion guarantees instruments exist. |

## Testing strategy (no network; fixed fixtures; in-memory SQLite)

- **`timeseries`** (pure): fixed events + price/FX history dicts → exact expected daily values;
  carry-forward across gaps/weekends; `incomplete` flag days; `net_invested` accumulation incl.
  opening + cash dividend; missing-flow-FX → `available=False`.
- **`build_dashboard`** (integration over in-memory SQLite): seed accounts/instruments/ledgers +
  prices/FX via existing creators and stores → assert each section's values, holding enrichment
  (name/sector/board/account_name), weights sum ≈ 1, KPI blends, FX exposure wiring
  (only settlement≠funding accounts), calendar filtering (held + upcoming only).
- **Degradation paths** (one test each): no prices at all; missing reporting FX pair (cold
  start); XIRR flow before earliest FX; stale-but-present price (used + flagged).
- **New store reads**: `get_fx_on` (on the date / before / none exists), `get_fx_history`
  ordering and bounds, `list_accounts` round-trip against `seed_accounts`.
- **Contract**: `DashboardData.model_dump()` round-trips without error; Decimals preserved
  (no float coercion).

## Out of scope (deferred / other modules)

- FastAPI routes, Jinja2 templates, HTMX wiring (`web_ui/` — next sub-project, after the Design
  brief round-trip).
- The Claude Design brief itself (written immediately after this ships, from the contract).
- FX-rate **history backfill** job (needed before XIRR/trend cover old flows; a future `pricing/`
  + `scheduler/` job).
- Caching computed results (rules: derive on read; cache only if profiling shows need).
- `llm_insight` consumption (the contract is ready for it; nothing built here).
- Per-account filtered dashboards (the contract carries `account_id`/`account_name` on every row,
  so the UI can group/filter client-side; a server-side per-account `build_dashboard` variant can
  come later if needed).

## Designed-in flexibility

The contract is a Pydantic layer between calculation and presentation: UI iterations (Design
round-trips) change templates, not math; new sections (e.g. real insights) extend `DashboardData`
without touching existing fields. The FX resolver and the pure `daily_value_series` are injectable
/ independently testable. `build_dashboard` takes explicit `now`/`reporting`, so tests and future
multi-currency views need no settings monkey-patching.

## Staging (the plan will sequence)

1. New read helpers: `get_fx_on` + `get_fx_history` (pricing), `list_accounts` (data_ingestion).
2. Contract models (`dashboard_models.py`).
3. `timeseries.daily_value_series` (pure, fixture-tested).
4. `dashboard.build_dashboard` assembly: ledger reads → book → valuation → cores → FX summary →
   dividends/calendar/weights/freshness → trend → `DashboardData`.
5. Degradation-path tests + contract serialization test.
