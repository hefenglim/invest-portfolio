# Design: `pricing/` — Market Data Layer (quotes + FX + history + dividend/ex-div)

- **Date:** 2026-06-08
- **Status:** Approved (design, A+B+C combined); pending spec review
- **Module:** `portfolio_dash/pricing/`
- **Depends on:** `shared/` (Decimal/money, `fx`, db session, models, Settings); consumes the
  data-source probe's recorded fixtures (`tests/pricing/fixtures/`) for tests. Per
  `architecture.md`, `pricing/` is the **only** module that writes price/FX rows; it **does not**
  write the ledger.

## Context & purpose

The market-data layer fetches **market data into SQLite** behind a **config-driven,
capability-aware provider chain** with **graceful degradation** and **per-row provenance**.
Delivered as **A + B + C combined, staged A→B→C**:

- **A — latest quotes (US/TW/MY) + FX (USD/TWD, USD/MYR, MYR/TWD).** Unblocks the portfolio ① /
  forex ② cores (current market value + current spot).
- **B — historical daily backfill.** Base data for analysis / backtest (incl. the `llm_insight/`
  self-backtest) and ECharts trend / equity curves.
- **C — dividend / ex-dividend reference data** (FinMind 除權息 + ex-div calendar). **Reference
  only:** stored for viewing, the ex-dividend calendar, and future *confirmed* auto-import; it
  does **not** write the ledger and does **not** enter P&L.

## Decisions (settled 2026-06-08, human sign-off)

1. **A+B+C combined, staged A→B→C.** Shared infra (provider protocol/registry/store/schema)
   built in A; B adds history; C adds the dividend-event table + the FinMind provider.
2. **Work-list source = caller passes the list** (instruments + FX pairs [+ history start date])
   as input. `pricing/` does not own an instruments registry (that is `data_ingestion/`'s job
   later). Tests pass sample lists.
3. **fetch → persist separation:** provider adapters return normalized in-memory rows and never
   touch the DB; a `store`/repository layer does the idempotent upserts (the only writer of
   price/FX/dividend-event rows).
4. **read / write decoupled:** dashboard and ① ② **read** from SQLite (last-known + staleness);
   **refresh writes**.
5. **Config-driven, capability-aware fallback chain.** v1 providers: `yfinance`, `twse`, `tpex`,
   and **`finmind`** (keyed) for C (and as a TW/FX fallback).
6. **C is reference data, not the ledger.** Fetched dividend/ex-div rows live in a dedicated
   `dividend_events` table; ① ② calc reads **only** the ledger, so there is **no double-count**.
   The "match holdings → prompt → confirmed auto-import → write ledger per accounting rules"
   workflow is **`data_ingestion/` + `web_ui/` (future)** and *consumes* this table.
7. **Unified auto-import principle** (cross-cutting, recorded for `data_ingestion/`): the manual
   ledger is source of truth; data-source data (FinMind dividends, Schwab transactions) is
   matched to holdings and offered for **user-confirmed** import following the accounting rules;
   manual entry is always retained; `original_cost` never overwritten.
8. **Designed for adjustability:** provider order in config (runtime-adjustable later), provider
   protocol + registry so new sources slot in without rewrites; YAGNI on features/scale per
   `stack.md`.

## Provider chain (config-driven · capability-aware · graceful)

- Each `(data_type[, market])` has a **config-defined ordered list of provider names**.
- A provider declares `supports(data_type, market)`; the chain **skips** unsupported providers.
- Try in order until one returns valid data; **record the winning `source` per row**.
- If all fail / none support → **do not raise to the caller.** Record the failure; the read path
  serves **last-known + `stale=True`** (or a "no data" marker if never fetched); `refresh`
  returns a per-item status summary. The dashboard degrades (staleness indicator), never crashes,
  never fabricates. (Bad *input* — unknown market, malformed pair — is still rejected loudly.)

## Data types

`QUOTE_LATEST`, `QUOTE_HISTORY`, `FX`, `DIVIDEND` (ex-div events).

## Inputs (write entrypoints)

- `refresh_quotes(instruments, fx_pairs) -> RefreshSummary` (A).
- `refresh_history(instruments, start: date | None) -> RefreshSummary` (B); `start` per the
  caller (the ledger's first trade date later); default a configured window (e.g. 5y).
- `refresh_dividends(instruments) -> RefreshSummary` (C).
- `InstrumentRef = (symbol, market, board/suffix)` (e.g. `("2330","TW","TWSE")`,
  `("8299","TW","TPEx")`, `("3182","MY",".KL")`, `("AAPL","US","")`); `FxPair = (base, quote)`.
- Source order from config; keyed providers (FinMind) skipped when no token configured.

## Module layout (`portfolio_dash/pricing/`)

```
pricing/
  __init__.py
  results.py    # PriceRow, FxRow, DividendEvent (fetch output); PriceRead/FxRead (with stale); RefreshSummary
  providers/
    __init__.py
    base.py            # Provider protocol: name, supports(data_type, market), fetch_quote_latest/_history/_fx/_dividends
    yfinance_provider.py  # US/TW/MY quotes (latest+history), FX, dividends (fallback)
    twse_provider.py      # TW 上市 latest (string close)
    tpex_provider.py      # TW 上櫃 latest (string close)
    finmind_provider.py   # keyed: TW dividends/除權息 (primary), TW price/FX fallback
  registry.py   # config-ordered, capability-aware chain; fallback; provenance; per-item status
  schema.py     # CREATE TABLE IF NOT EXISTS prices, fx_rates, dividend_events
  store.py      # idempotent upsert + read-latest/history/events + staleness (only writer of these tables)
  refresh.py    # orchestrators (write entrypoints) -> RefreshSummary
```

Providers re-express the probe's exploratory adapters as **typed** providers, tested against the
recorded fixtures. `pricing/` imports only `shared/*` and its own submodules.

## Schema (Decimal as canonical TEXT strings; FX high precision)

- `prices(instrument, market, as_of_date, close, open?, high?, low?, volume?, source, fetched_at)`
  — **PK `(instrument, as_of_date)`** (a time series; A writes the latest row, B backfills many).
  Latest read = max `as_of_date` per instrument.
- `fx_rates(base, quote, as_of_date, rate, source, fetched_at)` — **PK `(base, quote, as_of_date)`**.
- `dividend_events(instrument, market, ex_date, pay_date?, cash_amount?, stock_amount?, currency,
  source, fetched_at)` — **PK `(instrument, ex_date)`**. **Reference only**; never read by ① ②.
- Idempotent upsert via `INSERT ... ON CONFLICT(...) DO UPDATE`; `source` + `fetched_at` per row.

## Read API

- `get_latest_price(instrument) -> PriceRead(value, as_of, source, stale)`
- `get_price_history(instrument, start, end) -> list[PriceRead]` (for ECharts/analysis)
- `get_fx(base, quote) -> FxRead(rate, as_of, source, stale)`
- `get_dividend_events(instrument) -> list[DividendEvent]` (reference; for the calendar + future
  auto-import — not for ① ② calc)
- `stale = True` when no fresh row / `as_of` older than a configured threshold.

## Config

`Settings` holds the per-`(data_type[, market])` ordered provider names + the FinMind token
(`.env`, gitignored). v1 defaults from the probe ranking — quotes: US `yfinance`; TW
`twse`/`tpex` then `yfinance`; MY `yfinance`; history: `yfinance`; FX: `yfinance` (FinMind
fallback); dividends: `finmind` (TW) then `yfinance`. Structure supports reorder + adding
providers later (the settings page).

## Staging (A → B → C in one plan)

1. **A:** provider protocol + registry + schema(`prices`,`fx_rates`) + store + `refresh_quotes`
   + yfinance/twse/tpex quote-latest & FX. ← ① ② can now read real prices.
2. **B:** add `fetch_quote_history` + `refresh_history` + `get_price_history`; backfill into
   `prices`.
3. **C:** add `dividend_events` table + `DIVIDEND` data type + `finmind_provider` (keyed) +
   `refresh_dividends` + `get_dividend_events`.

## Error handling (loud where it should be; graceful where it matters)

- Bad **input** (unknown market, malformed pair) → reject loudly (programming error).
- External fetch failure / unsupported → skip + fallback; final none → graceful (last-known +
  stale + summary), never crash/fabricate.
- All money `Decimal`; non-finite rejected at model boundaries; FX rates stored, not converted
  here (conversion uses `shared.fx`).

## Testing strategy (mocks/fixtures — no live network)

- **providers** parse the probe's recorded fixtures (`tests/pricing/fixtures/{yfinance,twse,tpex,
  finmind}`): normalized `Decimal`, `source`, `as_of`; TW string-tick fidelity; FinMind dividend
  schema (cash + ex-div + pay dates); history rows.
- **registry:** config order honored; unsupported skipped; fallback on failure; provenance.
- **store:** idempotent upsert (re-run → no dupes) for prices/fx_rates/dividend_events;
  read-latest/history/events; staleness.
- **refresh:** end-to-end (A/B/C) with mocked providers; per-item summary; graceful all-fail.

## Out of scope (deferred / other modules)

- **Confirmed auto-import to the ledger** (match holdings → prompt → write ledger per accounting
  rules) — **`data_ingestion/` + `web_ui/` (future)**; consumes `dividend_events` (and, later,
  Schwab transactions). `CHANGELOG.md` Planned.
- `scheduler/` (APScheduler triggers the `refresh_*` functions) — separate; `pricing/` only
  exposes them.
- instruments registry / "what I hold" — `data_ingestion/` later (this layer takes the list as
  input).
- `web_ui/` rendering, `llm_insight/`.

## Designed-in flexibility (per human directive)

Config-driven provider order, a provider protocol + registry, decoupled fetch/persist/read, and
the reference-vs-ledger split mean adapting to real-world needs is **config edits + small
additions, not rewrites** — new providers (stockprices.dev, AlphaVantage, Finnhub, klsescreener,
Schwab) register and join the chain via config when needed. Concrete specifics deferred until real
use (YAGNI on scale/features per `stack.md`).
