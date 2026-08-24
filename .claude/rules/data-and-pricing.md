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

### The stored price basis — as-traded, in two columns (2026-08-10, owner D30)

Same class of rule as the float-noise cap above: a statement about what a stored price
**means**. A provider re-states its history after a split, so the number it delivers for a
given date depends on **when it was fetched** — while the ledger's share count for that
date does not. The stored close therefore fixes one canonical meaning:

> **A stored `close` is the price AS TRADED on its own `as_of_date`** (unadjusted for any
> later split), so it is always in the same share terms as the ledger's share count on that
> date.

`prices` carries three related columns to keep that true under an editable action ledger:

| Column | Meaning |
| --- | --- |
| `close_raw` | the provider's value **exactly as delivered**, **un-capped** — the only price column exempt from the 4-dp cap, because the close is *derived* from it and the derivation must not compound the cap's error |
| `split_basis` | the factor applied (canonical TEXT; the identity is the literal `"1"`, which is also the DDL default) |
| `close` | `cap_4dp(close_raw × split_basis)` — **the cap goes LAST, on the product** |

- **Two operations, one expression.** The write seam (`pricing/store.upsert_prices`, every
  fetch) and the reconcile (`pricing/reconcile.reconcile_prices`, every SPLIT
  insert/edit/delete) both compute `close := close_raw × target`, **recomputed from the raw
  column** — never rescaled in place, never by dividing the old basis back out. One shared
  helper (`pricing/store.express_close`) so the two cannot drift. Idempotency, reversibility
  and order-independence then hold by construction: deleting a SPLIT restores every affected
  row **byte-identically**, which matters because `prices` is the only place this feature
  writes outside the ledgers and 重算 does not cover it.
- **Cap last, measured:** `cap(raw) × 20` stores `2.8340` where `cap(raw × 20)` stores
  `2.8333`; at `×3` it is `0.4251` vs `0.4250` — the sub-RM1 MY tick above.
- **Only `close` is re-expressed** (D39b). `open` / `high` / `low` keep the provider's basis,
  like `volume`: they have no `*_raw` of their own, so a factor applied to them could never be
  restated or reversed. ⚠ A row's `close` and its `open`/`high`/`low` can therefore be on
  **different bases** — the first candlestick drawn from this table must divide them by
  `split_basis` first, or add its own raw columns.
- **The write multiplies; the read divides.** The write window is `(as_of, fetched_at]` —
  "which splits had the provider already folded in?" — and multiplies them back **out** to
  recover the as-traded value. The read (`portfolio/price_basis.price_in`) window is
  `(pd, d]` — "which splits happened between this price's date and the valuation day?" — and
  **divides**, so a price carried forward across a split meets a share count in the same
  units. Different windows, not a double application; the as-traded invariant sits between
  them. The read is never written back, so 重算 stays authoritative.
- **`Decimal(1)` is not `Decimal("1.0")`.** Multiplication sums exponents, so a
  non-integral identity factor rewrites `1.5` as `1.50` and `600` as `600.0` —
  value-preserving, TEXT-changing, invisible to `==`, and it would repaint every price row
  in the database on symbols with no corporate action at all. Both seams **short-circuit**
  past the identity rather than computing it.

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
  ⚠ **`fee_rule_snapshot` is PROVENANCE, not the replay's input** (recorded 2026-08-24, after a
  review claimed 重算 depends on it — it does not: `portfolio/cost_basis.py` reads `ev.fees` /
  `ev.tax` off the row). What it records is *which regime produced those two numbers*. It was
  therefore **empty on every broker-imported row**: `csv_import.py` only computes (and snapshots)
  a fee or tax it was **not** given, and a broker statement supplies both — the fee verbatim and
  a literal `"0"` tax. That silence is indistinguishable from "no rule applied". A supplied
  fee/tax now records `{"source": "supplied", …}` instead of `{}`, so a row's numbers are always
  attributable to either the engine or the statement. `data_ingestion/broker/convert.py`'s
  refusal to recompute is **deliberate and unchanged** — recomputing would yield a defensible
  number that does not match the money that actually left the account, which is the wrong kind
  of correct.
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

### Snapshot provenance — fundamentals are a UNION, not a fallback chain (AI-D4 / AI-D14)

**Owner ruling J, 2026-08-16, recorded in `docs/spec/2026-08-16-ai-assistant.md` §0; mechanism
revised by AI-D14 (W3-2, same day).** The default for a quote / FX / price fetch is the
**first-success-wins fallback chain** in `pricing/defaults.py` — one value is wanted, so the
chain picks the first provider that can give it and stops (and the `prices` table's
`(instrument, as_of_date)` key means a chart always reads exactly one close per day — the
union never touches charts). **Fundamentals are the deliberate, controlled exception.** Every
*enabled* fundamentals provider (yfinance / Finnhub / Alpha Vantage) writes its own
`external_snapshots` row — the table's primary key is already `(source, dataset, symbol,
as_of)`, so provenance is already in the data model and the rows coexist without a new table.

- **No merge layer — one block per source (AI-D14).** `fundamentals_json` is
  `{source: block}` — the block key IS the provenance (AI-D4's "every field carries its
  source" survives as "every block carries its as_of/currency", and the two-values-kept spirit
  is unchanged: two providers disagreeing simply produce two blocks). Field NAMES are
  normalized to one canonical set inside each block (renaming, not merging — the LLM must not
  be left to align `trailingPE` vs `peTTM`). **The never-average red line is enforced by the
  prompt**: the template requires different values across sources to be reported side by side
  with their sources, never averaged, never reconciled into a number no provider reported.
  That is the same red line as "the LLM never emits numbers of record" and "a stale price is
  labelled, never guessed".
- **Key-gated.** A provider whose key is absent reports `supports() == False`: it writes no
  snapshot and raises no error, so registering the key later plugs it in without a code change.
- ⚠ **This is scoped to fundamentals.** It is a deviation from the fallback convention, not a
  replacement for it — other snapshot chains (consensus, chips, sentiment, indices) keep
  first-success-wins unless the owner rules otherwise. An edge that exists in the code but not
  in this file is the next audit finding, which is why the exception is written here rather
  than left in the spec.

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
