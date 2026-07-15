# Rule: Data, Pricing & Money

## Money & numeric precision model (non-negotiable)

**`Decimal` end to end — never `float` for money, quantity, price, or rate.**
The earlier "store everything at 2 dp" idea is refined: `Decimal` is exact at any
scale, so **store at full source precision and quantize only at settlement/display.**
Lossy 2-dp truncation at storage is forbidden because it breaks two real cases below.

**Storage precision (do not truncate):**
- **Prices** — store at the market's finest tick precision:
  - US: 2 dp · TW: up to 2 dp (tick-dependent) · **MY: up to 3 dp** (sub-RM1 shares
    tick at 0.005, ETFs at 0.001 — see `markets-and-fees.md`).
- **FX rates** — **high precision (4–6 dp)**. Rates are NOT money; the 2-dp rule never
  applies to them. Dedicated high-scale column.
- **Float-noise cap (decided 2026-07-03, human sign-off):** float-sourced providers
  (yfinance et al.) emit binary-float tails (e.g. `305.364990234375`) that are NOT
  source precision. At the single write seam (`pricing/store.upsert_prices/upsert_fx`)
  prices are **capped at 4 dp** (covers every market tick above) and FX rates at
  **6 dp**, ROUND_HALF_UP — cap only, never pad (clean values store byte-identical).
  This refines, not contradicts, "store at full source precision": the cap removes
  representation noise, not information.
- **Average cost** — never stored as an authoritative rounded value. Store
  `total_cost` + `shares`; compute `average = total_cost / shares` on read
  (see `domain-ledger.md`).

**Amount precision (per-currency minor unit, applied at settlement):**
- USD = 2 dp (cent) · TWD = 0 dp (whole NT$; **fee/tax 無條件捨去 — floor, ROUND_DOWN — to
  integer** per 財政部 FE-D3, owner sign-off 2026-07-15; supersedes the earlier 四捨五入) ·
  MYR = 2 dp (sen).
  - Note: `quantize_amount` (general amounts, e.g. proceeds) still uses ROUND_HALF_UP; the
    floor is specific to the TW **fee/tax** engine (`fees.py`, `rounding="floor"`). US/MY fee
    components quantize per-component ROUND_HALF_UP to the 2-dp minor unit.

**Mechanics:**
- Persist Decimals as **TEXT** (canonical string) or **scaled integers**; one
  convention per column, documented. Do not mix.
- All FX conversion goes through the single helper in `shared/`. No ad-hoc
  multiply-by-rate scattered across modules.
- Rounding is explicit (`Decimal.quantize` + stated rounding mode). Display formatting
  (decimals shown, thousands separators) is a presentation concern, decoupled from
  storage.

## SQLite schema conventions

Canonical tables (names indicative; finalize in the spec phase):

- `accounts` — first-class entity: broker, settlement currency, funding currency,
  fee-rule-set ref, dividend model (see `domain-ledger.md`).
- `instruments` — symbol, market (`US` / `TW` / `MY`), quote currency, sector, name.
- `transactions` — source of truth: account, instrument, side, quantity, price, fees,
  tax, trade date. **Append-only in spirit**; corrections are new rows or explicit
  edits, never silent mutation. Store a fee/tax/FX-rate **snapshot** per row so 重算
  reproduces history even after rules change.
- `dividends` — account, instrument, date, type (cash / stock / DRIP), gross,
  withholding, net, reinvest shares + price (see dividend models in `domain-ledger.md`).
- `fx_conversions` — account, date, from_ccy, from_amount, to_ccy, to_amount (Q12).
- `opening_inventory` — account, instrument, shares, original avg cost, original cost
  total, **build date** (not a trade flow, but feeds XIRR).
- `prices` — instrument, date, close (+ any OHLC), source. Idempotent upsert on
  (instrument, date).
- `fx_rates` — base/quote (USD/TWD, USD/MYR, MYR/TWD…), date, rate, source. Idempotent.
- `insights` — cached LLM output (see `llm-insight.md`).

Separate **raw** stored data (transactions, prices, fx) from **computed** results
(holdings, P&L, returns). Computed values are derived on read by `portfolio/`, not
stored as the source of truth (cache them only if profiling shows a need).

## Pricing sources — quotes come from finance APIs, never from an LLM

Candidate sources (validate availability/reliability in the spec phase before
committing to one):

- **US equities:** yfinance, or a keyed API (Finnhub / Alpha Vantage / Polygon) if
  yfinance proves unreliable. (Schwab + Moomoo US both hold US-listed instruments.)
- **TW equities:** yfinance with `.TW` suffix (e.g. `2330.TW`), FinMind, or the
  TWSE / TPEx open-data endpoints.
- **MY equities:** yfinance with `.KL` suffix (Bursa), or a MY data source — verify
  3-dp price fidelity for sub-RM1 counters in the probe.
- **FX:** USD/TWD, USD/MYR, and MYR/TWD (for the combined reporting-currency XIRR).

Rules:
- The LLM **never** supplies a price, quantity, or return number. Quantitative data
  is fetched, stored, and computed locally. The LLM only consumes already-computed
  numbers for narrative.
- Fetches are **idempotent upserts** — re-running a refresh must not duplicate rows
  or corrupt history.
- A failed/stale fetch degrades gracefully: serve last-known price with a clear
  staleness indicator; never crash the dashboard, never silently fabricate.
- Source is recorded per row, so data provenance is always auditable.

## Scheduling (pricing refresh)

- APScheduler job in `scheduler/`. Cadence set by config (e.g. post-market for each
  exchange's timezone — note US and TW differ).
- Refresh is decoupled from page load: the dashboard reads what is in SQLite.

## Returns & FX P&L

The authoritative definitions live in `domain-ledger.md` (cost basis, realized /
unrealized P&L, total return without double-counting dividends, XIRR cashflow signs,
and the FX-conversion ledger / 換匯損益 attribution). In short: XIRR is the primary
metric, single reporting currency, every flow converted at trade-date FX; FX gain/loss
is an **attribution breakdown** of that figure, never added on top.
