# Research: Charles Schwab API (Trader API — Individual) — capabilities & system fit

- **Date:** 2026-06-08
- **Status:** research note (no implementation; Schwab integration deferred to a future provider)
- **Scope:** what data Schwab exposes, and how it should feed `pricing/`, `data_ingestion/`,
  `llm_insight/`, and the LLM self-backtest loop. Reading-only research (no Schwab account
  needed to catalogue; an account + OAuth app approval is needed to actually call it).

## 1. The two Schwab API products

| Product | Base URL | Purpose |
| --- | --- | --- |
| **Market Data API** | `https://api.schwabapi.com/marketdata/v1` | quotes, price history, option chains, movers, market hours, instruments/fundamentals |
| **Trader API (Accounts & Trading)** | `https://api.schwabapi.com/trader/v1` | account balances, **positions**, **transactions**, orders, user preferences |

**Auth:** OAuth2. Refresh token **expires every 7 days** → a weekly re-authorization is
required (operational friction for a self-hosted app — see §6). API calls use an **account
hash** (from `GET /accounts/accountNumbers`), never the raw account number. Requires a
Schwab brokerage account + a developer app that must be **approved** (days). "Trader API —
Individual" is the personal-account variant. Market Data rate limit ≈ **120 req/min**
(ample for 1–2 users).

## 2. Market Data API — capabilities

- **Quotes** — `quotes` (multi-symbol, customizable fields) / `quote` (single). Latest
  price, bid/ask, day OHLC, volume, 52-week range, etc.
- **Price history** — daily/weekly **back to ~1985** (e.g. AAPL); intraday at 1/5/10/15/30-min
  (1-min ~48 days, others ~9 months). Extended-hours + previous-close flags. Single symbol +
  date range per call.
- **Option chains** — full chains (calls/puts, strikes, expirations) with strategy analytics
  (incl. **implied volatility**), plus expiration chain.
- **Movers** — top movers per index (DJI/COMPX/SPX/NYSE/NASDAQ…), sortable by
  volume/trades/%-change.
- **Market hours** — per market type (EQUITY/OPTION/BOND/FUTURE/FOREX).
- **Instruments** — symbol/description search (regex), CUSIP lookup, and a **`FUNDAMENTAL`
  projection** returning per-instrument fundamentals (P/E, EPS, market cap, dividend yield,
  52-week high/low, …).

**Gap:** Schwab does **not** provide news, analyst reports, or deep financial statements /
earnings transcripts. For US qualitative/fundamental depth we still need other sources
(deferred to the `llm_insight/` source probe).

## 3. Trader API — capabilities (the differentiator vs every other source)

- **Accounts** — `accountNumbers` (→ hashes), `account`/`accounts` with **balances,
  positions (incl. cost basis & unrealized P&L), and orders**.
- **Transactions** — filterable by type, symbol, date (**60-day window per call** → paginate
  for history). Types include **TRADE** (buy/sell), **DIVIDEND_OR_INTEREST**, ACH/wire,
  JOURNAL, MONEY_MARKET, MARGIN_CALL, etc.
- **Orders** — get/place/cancel/replace; rich status enum. (Out of scope — this app does not
  place trades.)
- **User preferences** — linked accounts, etc.

## 4. Mapping to `portfolio-dash` modules

### `pricing/` (US numbers of record)
- US **quotes** (latest) and **history** (daily back to 1985 → excellent for the ECharts
  equity/price curves) → a strong US provider alongside yfinance. As a **keyed, account-bound,
  OAuth** source it is heavier than yfinance; slot it as a **high-quality US fallback/cross-check**
  in the provider chain, not necessarily the everyday primary.
- **Fundamentals** via `instruments?projection=FUNDAMENTAL` (P/E, EPS, market cap, div yield,
  52w) → decision indicators + `llm_insight/` inputs.
- **Market hours** → `scheduler/` uses it to time the US post-close refresh correctly.
- Same money discipline as everywhere: parse to `Decimal(str(x))`, quantize at display.

### `data_ingestion/` — the highest-value Schwab use
- Trader API can **auto-import the Schwab (US) account's positions and transactions**, turning
  manual entry into reconciliation. `TRADE` rows → transaction ledger; `DIVIDEND_OR_INTEREST`
  → dividend ledger (DRIP repurchases visible as trades). This dramatically cuts manual entry
  for the US/Schwab account.
- **Guardrails (per `architecture.md` / `domain-ledger.md`):** the canonical ledger stays the
  source of truth — imported rows are **normalized, deduped, and user-confirmed**, never
  silently written; reject malformed rows loudly. Schwab's reported cost basis is a
  **cross-check only** — our model rebuilds from the ledger and **never overwrites
  `original_cost`**. Mind the 60-day transaction window (paginate) and account-hash handling.
- Note: only the **Schwab** account benefits; TW broker / Moomoo have no such API → those stay
  manual/CSV.

### `llm_insight/` (qualitative context, never numbers of record)
- Fundamentals (P/E, EPS, div yield, 52w position), **movers** (market context), option-chain
  **implied volatility** (sentiment/risk) → narrative inputs. News/analyst depth must come
  from elsewhere.

## 5. LLM self-backtest / self-improvement (powers the `llm_insight/` prediction-tracking feature)
The deferred `llm_insight/` mechanism (LLM records each forecast, later scores itself) needs an
**outcome oracle** + **feature snapshots**. Schwab supplies both for US names:
- **Outcome oracle:** `price_history` (daily back decades) gives the realized return over any
  prediction window → score each past prediction → update its confidence index.
- **Feature snapshot at prediction time:** fundamentals + movers + IV recorded alongside the
  forecast → later analysis of *which signals preceded good vs bad calls* → corrective feedback
  into future prompts/weights. (Same pattern as FinMind for TW — see that note.)

## 6. Considerations / risks before adopting Schwab
- **7-day OAuth refresh expiry** → needs a weekly re-auth UX (or a documented manual refresh).
  This is the main operational cost for a self-hosted 1–2-user app.
- **App approval + brokerage-account requirement** → lead time; can't be probed without it.
- Fits the locked **provider registry**: register as a US **market-data provider** (quotes/
  history/fundamentals) and, separately, an **account-sync provider** for `data_ingestion/`.
  Config-driven, runtime-orderable like every other source.
- Keep keys/tokens in settings/`.env` (gitignored), never in code (matches `llm-insight.md`
  / data rules).

## 7. Recommended sequencing
Defer until after `pricing/` + `data_ingestion/` exist with the provider-registry + import
pipeline. Then add Schwab in two slices: (a) market-data provider (low risk), (b) account-sync
importer (higher value, needs the dedupe/confirm pipeline). Validate live only once the account
+ approved app + OAuth flow are in place.

## Sources
- [Charles Schwab Developer Portal](https://developer.schwab.com/)
- [Trader API — Individual specifications](https://developer.schwab.com/products/trader-api--individual/details/specifications/Retail%20Trader%20API%20Production)
- [schwab-py client documentation](https://schwab-py.readthedocs.io/en/latest/client.html)
- [The (Unofficial) Guide to Charles Schwab's Trader APIs](https://medium.com/@carstensavage/the-unofficial-guide-to-charles-schwabs-trader-apis-14c1f5bc1d57)
- [Schwabdev — Market Data Methods (DeepWiki)](https://deepwiki.com/tylerebowers/Schwabdev/3.3-market-data-methods)
