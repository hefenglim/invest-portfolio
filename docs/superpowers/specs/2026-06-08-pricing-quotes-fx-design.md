# Design: `pricing/` — Quotes + FX into SQLite (v1)

- **Date:** 2026-06-08
- **Status:** Approved (design); pending spec review
- **Module:** `portfolio_dash/pricing/`
- **Depends on:** `shared/` (Decimal/money, `fx`, db session, models, Settings); consumes the
  data-source probe's recorded fixtures (`tests/pricing/fixtures/`) for tests. Per
  `architecture.md`, `pricing/` is the **only** module that writes price/FX rows.

## Context & purpose

v1 fetches **latest quotes (US/TW/MY) + FX (USD/TWD, USD/MYR, MYR/TWD)** into SQLite via
**idempotent upserts**, behind a **config-driven, capability-aware provider chain** with
**graceful degradation**. It unblocks the portfolio ① / forex ② cores (current market value +
current spot). **Historical backfill (B)** and **dividend / ex-dividend fetching (C)** are
explicitly deferred (recorded in `CHANGELOG.md` Planned).

## Decisions (settled 2026-06-08, human sign-off)

1. **Scope v1 = latest quotes + FX only.** B (historical) and C (dividend/ex-div) deferred.
2. **Work-list source = caller passes the list** (instruments + FX pairs as input). `pricing/`
   does **not** own an instruments registry (that is `data_ingestion/`'s job later). Tests pass a
   sample list; in production the holdings/ledger supplies it.
3. **fetch → persist separation:** provider adapters return normalized in-memory rows and
   **never touch the DB**; a `store`/repository layer does the idempotent upserts (the only
   writer of price/FX rows).
4. **read / write decoupled:** the dashboard and ① ② **read** from SQLite (last-known +
   staleness); **refresh writes**.
5. **Config-driven, capability-aware fallback chain** (below). v1 providers: `yfinance`, `twse`,
   `tpex`.
6. **Designed for adjustability** (per human directive): provider order in config
   (runtime-adjustable later), a provider protocol + registry so new sources slot in without
   rewrites, decoupled layers. YAGNI on features/scale per `stack.md` — flexible seams, deferred
   specifics.

## Provider chain (config-driven · capability-aware · graceful)

- Each `(data_type[, market])` has a **config-defined ordered list of provider names**.
- A provider declares `supports(data_type, market)`. The chain **skips** providers that don't
  support the requested `(data_type, market)`.
- Try in order until a provider returns valid data; **record the winning `source` per row**
  (provenance).
- If all fail / none support → **do NOT raise to the caller.** Record the failure; the read path
  serves **last-known + `stale=True`** (or a "no data" marker if never fetched). `refresh`
  returns a per-item status summary. The dashboard degrades (staleness indicator), never
  crashes, never fabricates. (Bad *input* — unknown market, malformed pair — is still rejected
  loudly; that's a programming error, not a degraded fetch.)

## Inputs (v1)

`refresh(instruments: list[InstrumentRef], fx_pairs: list[FxPair]) -> RefreshSummary`
- `InstrumentRef = (symbol, market, board/suffix)` (e.g. `("2330","TW","TWSE")`,
  `("8299","TW","TPEx")`, `("3182","MY",".KL")`, `("AAPL","US","")`).
- `FxPair = (base, quote)` for USD/TWD, USD/MYR, MYR/TWD.
- Source order comes from config; token-keyed providers are skipped when no key (N/A for v1's
  no-key providers).

## Module layout (`portfolio_dash/pricing/`)

```
pricing/
  __init__.py
  results.py    # PriceRow, FxRow (normalized fetch output), PriceRead/FxRead (with stale), RefreshSummary
  providers/
    __init__.py
    base.py     # Provider protocol: name, supports(data_type, market), fetch_quote_latest(...), fetch_fx(...)
    yfinance_provider.py   # US/TW/MY quotes + FX
    twse_provider.py       # TW 上市 latest (string close)
    tpex_provider.py       # TW 上櫃 latest (string close)
  registry.py   # config-ordered, capability-aware chain; fallback; provenance; per-item status
  schema.py     # CREATE TABLE IF NOT EXISTS prices, fx_rates (establishes the app's SQLite DDL pattern)
  store.py      # idempotent upsert + read-latest + staleness (only writer of prices/fx_rates)
  refresh.py    # orchestrator (write entrypoint): work-list -> chain -> store -> RefreshSummary
```

Providers re-express the probe's exploratory adapters as **typed** providers, tested against the
recorded fixtures. `pricing/` imports only `shared/*` and its own submodules.

## Schema (Decimal as canonical TEXT strings; FX high precision)

- `prices(instrument, market, as_of_date, close, open?, high?, low?, volume?, source, fetched_at)`
  — **PK `(instrument, as_of_date)`**; latest read = max `as_of_date` per instrument.
- `fx_rates(base, quote, as_of_date, rate, source, fetched_at)` — **PK `(base, quote, as_of_date)`**.
- Idempotent upsert via `INSERT ... ON CONFLICT(...) DO UPDATE`. `source` + `fetched_at` recorded
  per row for provenance/staleness. Decimals stored as canonical strings (shared money/fx
  conventions); FX rate column high-precision.

## Read API

- `get_latest_price(instrument) -> PriceRead(value, as_of, source, stale)`
- `get_fx(base, quote) -> FxRead(rate, as_of, source, stale)`
- `stale = True` when no fresh row / `as_of` older than a configured threshold. Consumed by ① ②
  and the dashboard.

## Config

`Settings` holds the per-`(data_type[, market])` ordered provider names. v1 default from the probe
ranking — US: `yfinance`; TW: `twse`/`tpex` then `yfinance`; MY: `yfinance`; FX: `yfinance`. The
structure supports reordering + adding providers later (the settings page).

## Testing strategy (mocks/fixtures — no live network)

- **providers** parse the probe's recorded fixtures (`tests/pricing/fixtures/{yfinance,twse,tpex}`);
  assert normalized `Decimal`, `source`, `as_of`, and (TW) string-tick fidelity.
- **registry:** config order honored; unsupported providers skipped; fallback on provider failure;
  provenance recorded; per-item status correct.
- **store:** idempotent upsert (re-run → no dupes, updates in place), read-latest, staleness flag.
- **refresh:** end-to-end with mocked providers; per-item summary; graceful all-fail (no raise).

## Out of scope (deferred / other modules)

- **(B) historical daily backfill**, **(C) dividend / ex-dividend fetching** — `CHANGELOG.md`
  Planned; later `pricing/` iterations.
- `scheduler/` (APScheduler triggers `refresh`) — separate; `pricing/` only exposes the function.
- instruments registry / "what I hold" — `data_ingestion/` later (v1 takes the list as input).
- `web_ui/` rendering, `llm_insight/`.

## Designed-in flexibility (per human directive)

Config-driven provider order, a provider protocol + registry, and decoupled fetch/persist/read
mean adapting to real-world needs is **config edits + small additions, not rewrites** — new
providers (FinMind, stockprices.dev, AlphaVantage, Finnhub, klsescreener, Schwab) register and
join the chain via config when needed. Concrete specifics deferred until real use (YAGNI on
scale/features per `stack.md`).
