# Design: Data-Source Availability Probe (spike)

- **Date:** 2026-06-08
- **Status:** Approved (design); pending spec review
- **Kind:** Exploratory **spike** — produces evidence + a recommendation, not production code.
- **Feeds:** the future `pricing/` module spec (source selection, fallback chain, adapters).
- **Depends on:** nothing in the app core. Hits live external APIs / web pages directly.

## Context & purpose

`pricing/` must fetch quotes + FX from finance APIs into SQLite, with **graceful
degradation** and **per-row source provenance** (see `data-and-pricing.md`). Before
committing to sources, this spike empirically measures each candidate source per
(data type × market): availability, auth, rate limit/batch, latency, **data fidelity**
(esp. MY 3-dp), coverage against the user's real sample tickers, **history depth**, and
reliability. Output is a **comparison matrix + a ranked primary/fallback recommendation**
per (data type × market), plus **recorded raw-response fixtures** for later `pricing/`
mock tests. The spike does **not** implement `pricing/`.

## Decisions (settled in brainstorming, 2026-06-08, human sign-off)

1. **Quantitative scope only.** Probe covers ① quotes (latest EOD + historical daily),
   ② FX (USD/TWD, USD/MYR, MYR/TWD), ③ dividends / ex-dividend. Qualitative sources
   (Google Trends / FRED macro / news / analyst reports) are **catalogued only** here and
   validated later in the `llm_insight/` sub-project.
2. **Fees/taxes are NOT probed.** All fee/tax rates are **config-driven** (settings,
   versioned) — removed from the probe's API scope (supersedes Q11's "confirm in probe"
   for *rates*; the *structure* stays in `markets-and-fees.md`).
3. **Source priority is a recommendation, not a lock.** The probe ranks sources from
   measured results; the final order is **config-driven and runtime-adjustable** (a
   settings page later, where e.g. the FinMind token and per-source order live).
4. **Historical is in scope.** Measure each source's max historical daily-close depth
   (baseline test window: 5 years or the source's max). Rationale: future Apache ECharts
   equity/price charts overlay internal transaction history with market history.
   *Core calc needs only the latest quote; historical backfill is a `pricing/` concern.*
5. **Probe code is exploratory.** Lives in `scripts/probe/`, **not** in the typed
   `portfolio_dash/` package. It may be discarded after its report + fixtures are produced.

## Scope (what the probe tests)

Data types × markets, against the user's **sample tickers** (coverage/fidelity only;
not the real portfolio — formal positions arrive via a future CSV import):

- **US:** TSLA, AAPL, NVDA, IVV, VOO, RIVN, O, BEN, BABA, GOOGL, MSFT, MU, SNDK, ARKK,
  GGR, SE
- **TW:** 0050, 8299, 2454, 2330, 6488, 6531, 2543, 2317, 3005, 6139, 2308, 1519
  — the probe **resolves 上市 (TWSE, `.TW`) vs 上櫃 (TPEx, `.TWO`) per ticker**; several
  samples (e.g. 8299, 6488, 6531, 6139) are TPEx and need `.TWO` + the TPEx open-data
  endpoint (TWSE's API covers 上市 only).
- **MY:** 5212, 3182, 5347, 1155, 1818 — verify **3-dp fidelity** (sub-RM1 ticks 0.005,
  ETFs 0.001; never truncate to 2 dp).
- **FX:** USD/TWD, USD/MYR, MYR/TWD.

## Candidate sources (ranked input; probe re-ranks from data)

| Market / type | Candidates (initial order) | Form |
| --- | --- | --- |
| TW 上市 quote | yfinance(`.TW`) → TWSE open API → FinMind(token) → twstock | lib / API / API / lib |
| TW 上櫃 quote | yfinance(`.TWO`) → **TPEx open data** → FinMind → twstock | lib / API / API / lib |
| US quote | yfinance → stockprices.dev → Schwab API *(pending)* → AlphaVantage → Finnhub | lib / API / API / API / API |
| MY quote | **yfinance(`.KL`)** → klsescreener → Bursa → *(probe to discover: marketstack, eodhd, twelvedata, i3investor, Malaysiastock.biz)* | lib / web / web / API·web |
| FX | yfinance(`USDTWD=X`/`USDMYR=X`/`MYRTWD=X`) → FinMind FX → AlphaVantage FX → FRED | lib / API / API / API |
| Dividends / ex-div | yfinance dividends → FinMind 除權息 (TW) → *(probe to discover US/MY)* | lib / API |

No-key sources: yfinance, TWSE, TPEx, twstock, stockprices.dev, MY web pages.
Keyed (provided at execution time, in `.env`, gitignored): FinMind *(have)*, AlphaVantage,
Finnhub. Schwab = **pending** account/OAuth → marked pending, not tested this round.

## Methodology

For each `(source × data_type × market)` cell, attempt a real fetch over the sample
tickers (a few retries for reliability) and record one **probe result row**:

- `source`, `data_type` (quote_latest | quote_history | fx | dividend), `market`
- `requires_key` / auth method
- `batch_support` — max symbols per single call (drives rate-limit feasibility)
- `rate_limit` — observed/declared
- `latency_ms` — observed
- `coverage` — of the sample tickers, count resolved + list of misses
- `fidelity` — decimals preserved (MY 3-dp check), **raw vs adjusted close** availability,
  quote currency correct
- `history_depth` — earliest date / max range returned
- `freshness` — how delayed "latest" is vs market close
- `reliability` — errors/timeouts over N attempts
- `format` — JSON / CSV / scraped HTML
- `verdict` — primary | fallback | unusable, for that (data_type × market)
- `notes`

## Output artifacts

1. **Comparison report** — `docs/probes/2026-06-08-data-source-probe-results.md`:
   the full matrix + a **ranked primary/fallback recommendation per (data type × market)**,
   plus a "discovered MY sources" subsection and a "qualitative sources catalogue"
   (Trends/FRED/news — for the future `llm_insight/` probe).
2. **Recorded fixtures** — `tests/pricing/fixtures/<source>/<symbol|pair>.<ext>`: raw
   responses captured during the probe, for deterministic `pricing/` mock tests later.
3. **Architecture recommendation** (in the report; implemented later in `pricing/`):
   - a **provider protocol** (`fetch_quote_latest` / `fetch_quote_history` / `fetch_fx` /
     `fetch_dividends`), one adapter per source;
   - a **registry + config-driven ordered chain** per (data_type, market), tried in order
     until a valid result, **recording `source` per row** (provenance);
   - **graceful degradation** (serve last-known + staleness flag; never fabricate);
   - keys/tokens + source order in settings/`.env`, **runtime-adjustable**.

## Error handling (loud, never guess)

- A source that fails/owns no data for a cell → recorded as `unusable` with the error;
  **never fabricated**. Missing tickers are listed, not silently dropped.
- Network/transient errors → retried a few times; persistent failure is recorded honestly.
- Keyed sources without a key at run time → recorded as `skipped (no key)`, not failed.

## Out of scope (explicit)

`pricing/` implementation; `scheduler/`; the settings UI; the source registry/chain code
(the report *recommends* it, `pricing/` *builds* it); qualitative sources beyond
cataloguing (Trends/FRED/news → `llm_insight/`); fee/tax rates (config); CSV import +
its format (a future `data_ingestion/` task); persisting probe data into the app DB.

## Related / tracked elsewhere (not designed here)

- **Qualitative source validation** (Trends/FRED/news/analyst reports) → `llm_insight/`
  sub-project.
- **LLM prediction self-tracking / backtest / confidence index** — a newly raised
  `llm_insight/` feature (the LLM records each recommendation/forecast, later replays and
  scores its own past predictions, accumulating a confidence index and a corrective
  feedback loop). Recorded in `CHANGELOG.md` Planned; gets its own brainstorm at the
  `llm_insight/` stage. Out of scope here.
