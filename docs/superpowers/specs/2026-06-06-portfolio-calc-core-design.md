# Design: Domain Models + `portfolio/` Calculation Core (sub-project ①)

- **Date:** 2026-06-06
- **Status:** Approved (design); pending spec review
- **Modules:** `portfolio_dash/shared/models/`, `portfolio_dash/portfolio/`
- **Author:** Claude Code (from human spec, brainstorming flow)
- **Depends on:** `shared/` foundation (enums, money, fx, config, db) — already shipped.

## Context & purpose

This is sub-project ① of the broader portfolio dashboard: the canonical **domain
models** plus the **calculation core** that turns ledgers into cost basis, P&L,
returns, XIRR, sector allocation, and a combined multi-currency view. It is built as
**pure functions over in-memory inputs** with fixed-fixture tests — no persistence, no
live pricing, no web layer (those arrive in later sub-projects). It is the correctness
foundation everything else renders.

## Decisions (settled in brainstorming)

1. **Scope = calc core only; defer full FX attribution to ② `forex/`.** This spec
   includes the reporting-currency XIRR/total return (every flow converted at trade-date
   FX via `shared.fx.convert`, so FX is *embedded*). The 換匯損益 decomposition
   (realized/unrealized FX P&L, per-account home-currency FX cost pools) is sub-project
   ②, not here.
2. **Cost basis = weighted-average only** (all markets). No FIFO. Interfaces need not
   pre-abstract other methods (YAGNI).
3. **Accounting model = adjusted-cost based** — see "Accounting model" below. **This
   OVERRIDES a locked decision** in `domain-ledger.md` (which had P&L use original cost
   with a separate dividend-income line). Human sign-off given 2026-06-06; the override
   must be recorded in `CHANGELOG.md` and `domain-ledger.md` updated (first plan task).
   The *no-double-count principle is preserved* — only the mechanism changes.
4. **Total return rate denominator = original invested cost** (not adjusted; adjusted
   would shrink as dividends arrive and diverge to infinity at full payback).
5. **XIRR via `pyxirr`** (new dependency, pin in `pyproject.toml`). XIRR is cashflow-
   based and is **unaffected** by the accounting model. XIRR uses `float` internally to
   solve for the rate — acceptable because the result is a rate, not money.
6. **Model placement:** input/ledger models → `shared/models/` (cross-layer:
   `data_ingestion` writes, `portfolio` reads — only `shared` satisfies the one-way
   dependency rule). Computed-result models → `portfolio/` (owned by the producer).
7. **Output set = full cohesive calc set** (per-holding, realized, unrealized, total
   return + rate, XIRR, sector allocation, combined reporting-currency view).
8. **Pure-function pipeline, `Decimal` throughout, no pandas.** Money is `Decimal`;
   pandas only holds Decimals as `object` dtype (error-prone) and vectorization is
   pointless at this scale (<2,400 rows/yr).

## Accounting model (the core of this spec)

Two cost numbers per holding (quote currency of the instrument):

- **`original_cost`** — weighted average of actual buy prices; **never overwritten**.
  Used for: the return-rate denominator, and the recoverable dividend/price split.
- **`adjusted_cost`** = `original_total − cumulative_cash_dividends`;
  `adjusted_avg = adjusted_total / shares`. **May be ≤ 0** when cumulative cash
  dividends exceed cost (long-held high-yield) — **never floored at 0** (flooring would
  drop dividends from total return).

P&L is computed against **adjusted cost**, and cash dividends are **not** a separate
income line (they are embedded as cost reduction):

- **Realized P&L** (on sell) = net proceeds (after fees + tax) − `adjusted_avg × shares_sold`.
- **Unrealized P&L** = (market price − `adjusted_avg`) × shares.
- **Total return** = realized + unrealized (both vs adjusted), **including realized from
  already-closed positions**. No separate dividend line.
- **Total return rate** = total return / **total original cost of all acquisitions in
  scope** (buys + opening inventory; matches the realized+unrealized numerator scope).
  This is a **cumulative, NON-annualized** figure — a quick "profit on capital deployed."
  For decisions, lead with **XIRR** (annualized, money-weighted); the simple rate is a
  secondary glance metric. (Caveat: with recycled capital — sell then re-buy — a
  gross-acquisition denominator can overstate the base; XIRR is the rigorous answer.)

This is mathematically identical to the previous original-cost-plus-separate-dividend
model — `(price − adjusted_avg)×sh = (price − original_avg)×sh + cumulative_dividends` —
so the total is the same number; only the representation changes. **Trap (the original
double-count bug):** because dividends are hidden inside the cost, a separate dividend-
income line must **never** be added on top anywhere (dashboard, reports). Tests assert
the two formulations agree and that no separate line exists.

### Dividend handling per type

- **Cash (TW, MY):** reduce `adjusted_total` by **net** cash received — net = gross less
  any withholding and (TW) 二代健保補充保費 / dividend tax, captured at entry in
  `Dividend.net`. (TW quote TWD; MY MYR.) These are the only dividends that reduce
  adjusted cost.
- **DRIP (US — Schwab, Moomoo):** 30% withholding; net reinvested → repurchased shares
  recorded at **$0 cost**. This adds zero-cost shares (lowering both `original_avg` and
  `adjusted_avg` via the larger share count). DRIP does **not** reduce `adjusted_total`
  as a cash dividend (it was reinvested, not received) — otherwise it double-counts.
- **Stock dividend (配股):** add shares, no cash, no cost change.

### Decision-support split (kept visible)

Because `original_cost` is retained, the headline total (vs adjusted) is decomposed for
display:
- **Capital-gain portion** = (market price − `original_avg`) × shares
- **Dividend portion** = (`original_avg` − `adjusted_avg`) × shares  (= cumulative cash dividends still held)
- **回本進度 / 股利回收率** (display) = cumulative cash dividends / original_total.

### Reporting-currency aggregation & metric semantics (analyst review)

- **Per-currency first.** Per-holding P&L and per-currency subtotals are the primary
  truth, each in the instrument's quote currency (TWD/USD/MYR). The blended reporting-
  currency total is a derived headline, translated at **current spot** (`current_fx`).
  Showing per-currency avoids hiding where return and risk actually came from.
- **Three distinct metrics, never summed:** (a) asset total return (realized+unrealized,
  per ccy and blended at current spot); (b) the simple total-return **rate** (cumulative,
  not annualized — see above); (c) **XIRR** (annualized, money-weighted, FX-aware — flows
  at trade-date FX). Lead decisions with XIRR.
- **FX is embedded, not double-counted.** The blended total uses current spot; XIRR
  embeds trade-date FX. They are different lenses on the same portfolio and are never
  added together. The explicit asset-vs-FX *attribution* is sub-project ②, out of scope.
- **Transaction costs are captured:** realized P&L uses net-of-fee proceeds and XIRR buy
  outflows include fees + tax, so cost drag is never invisible.
- **Adjusted-model decision caveat:** a high-yield position can show a large unrealized
  "gain" that is mostly returned dividends, not price appreciation — which is exactly why
  the **capital-gain vs dividend-portion split is a first-class output**, not a footnote.

## Module layout

```
portfolio_dash/
  shared/
    models/
      __init__.py
      enums.py        # Side(BUY, SELL), DividendType(CASH, STOCK, DRIP)
      assets.py       # Account, Instrument
      ledger.py       # Transaction, Dividend, FXConversion, OpeningInventory
  portfolio/
    __init__.py
    results.py        # Holding, RealizedPnL, ReturnSummary, SectorAllocation, CombinedView
    cost_basis.py     # build_book() -> (holdings, realized), via one chronological replay
    pnl.py            # value_holdings() (valuation + unrealized)
    returns.py        # total_return(), xirr_reporting()
    allocation.py     # sector_allocation(), combined_view()
```

(`Currency`/`Market` already live in `shared/enums.py`; the new `shared/models/enums.py`
holds domain enums to keep the foundation enums separate from richer domain models.)

## Input models (`shared/models/`, Pydantic v2, `Decimal` fields)

- **`Account`** — `account_id: str`, `name: str`, `broker: str`,
  `settlement_ccy: Currency`, `funding_ccy: Currency`. (Dividend behavior is driven by
  each `Dividend` record's fields, not re-derived from the account here.)
- **`Instrument`** — `symbol: str`, `market: Market`, `quote_ccy: Currency`,
  `sector: str`, `name: str`.
- **`Transaction`** — `account_id`, `symbol`, `side: Side`, `quantity: Decimal`,
  `price: Decimal`, `fees: Decimal`, `tax: Decimal`, `trade_date: date`. Carries the
  fee/tax/FX **snapshot** taken at entry; the calc reads these as-is (never recomputes
  fees).
- **`Dividend`** — `account_id`, `symbol`, `date`, `type: DividendType`, `gross: Decimal`,
  `withholding: Decimal`, `net: Decimal`, `reinvest_shares: Decimal | None`,
  `reinvest_price: Decimal | None`.
- **`FXConversion`** — `account_id`, `date`, `from_ccy: Currency`, `from_amount: Decimal`,
  `to_ccy: Currency`, `to_amount: Decimal`. (Defined now; primarily consumed by ② forex.)
- **`OpeningInventory`** — `account_id`, `symbol`, `shares: Decimal`,
  `original_avg_cost: Decimal`, `original_cost_total: Decimal`, `build_date: date`.

All `Decimal` money fields validate as finite (reuse the spirit of `money` guards).

## Result models (`portfolio/results.py`)

- **`Holding`** — `account_id`, `symbol`, `shares`, `original_avg`, `adjusted_avg`,
  `original_cost_total`, `adjusted_cost_total`, `market_price: Decimal | None`,
  `market_value: Decimal | None`, `unrealized_pnl: Decimal | None`,
  `capital_gain: Decimal | None`, `dividend_portion: Decimal`,
  `payback_ratio: Decimal`, `price_stale: bool`.
- **`RealizedPnL`** — per-sale rows + aggregate (each: symbol, account, shares_sold,
  proceeds_net, adjusted_cost_removed, realized).
- **`ReturnSummary`** — `total_return`, `total_return_rate` (denominator =
  original invested cost), `xirr: Decimal | None`, reporting `Currency`.
- **`SectorAllocation`** — sector → (value, weight) in reporting currency.
- **`CombinedView`** — per-currency totals + reporting-currency normalized totals.

## Calculation pipeline (pure functions)

1. **`build_book(transactions, dividends, opening_inventory, instruments) -> tuple[list[Holding], RealizedPnL]`**
   One **chronological** replay (events sorted by date), maintaining per-(account,symbol)
   `shares` / `original_total` / `adjusted_total`. Seeds from opening inventory; applies
   buys/sells (weighted average); applies dividends (cash → reduce `adjusted_total`;
   DRIP → add $0-cost shares; stock → add shares); allows `adjusted_total ≤ 0`. Emits the
   open **holdings** (cost basis: `original_avg`, `adjusted_avg`, `dividend_portion`,
   `payback_ratio`; market fields `None` until valued) and **realized** rows produced on
   each sell (net proceeds after fees+tax − `adjusted_avg × shares_sold`, using the
   adjusted average *at the moment of that sale*). One replay is required precisely
   because the adjusted average evolves as dividends/buys occur over time.
2. **`value_holdings(holdings, price_map) -> list[Holding]`**
   Fills `market_price`, `market_value`, `unrealized_pnl = (price − adjusted_avg)×shares`,
   `capital_gain = (price − original_avg)×shares`. Missing price → leave value fields
   `None`, set `price_stale = True` (never fabricate).
3. **`total_return(realized, valued_holdings) -> partial ReturnSummary`**
   `total_return = realized + Σ unrealized`; `rate = total_return / original_invested_cost`.
4. **`xirr_reporting(transactions, dividends, opening_inventory, fx_at, current_prices, current_fx, as_of) -> Decimal | None`**
   Builds reporting-currency cashflows: buy − (**gross cash out, incl. fees + tax**),
   sell + (**net proceeds after fees + tax**), **cash dividend +** (net received),
   **DRIP neutral**, opening inventory − (`original_cost_total` at `build_date`), final
   market value + at `as_of`. Each flow converted at its **trade-date FX** via
   `shared.fx.convert`; final value at `current_fx`. Solve with `pyxirr`. No sign change /
   non-convergence → `None` (surface, never fake). Note: opening inventory is compressed
   to a single flow at `build_date` (its pre-history is not modeled) — XIRR for opening
   positions is therefore approximate by design.
5. **`sector_allocation(valued_holdings, instruments, current_fx) -> SectorAllocation`**
   Reporting-currency value per sector + weights.
6. **`combined_view(valued_holdings, realized, current_fx) -> CombinedView`**
   Per-currency and reporting-currency normalized totals.

### External inputs (supplied, never fetched here)

- `price_map: dict[symbol, Decimal]` — current prices (quote ccy).
- `fx_at(date, from, to) -> Decimal` — trade-date FX lookup for flow conversion.
- `current_fx(from, to) -> Decimal` — current spot for valuation.
- `as_of: date` — valuation date.

Later sub-projects (`pricing/`) supply these from SQLite; here they come from fixtures.

## Error handling (loud, never guess)

- **Sell quantity > holdings** → raise a specific error (e.g. `OversellError`). The
  orchestration layer turns this into the "input error vs short sale — confirm" flow.
- **Missing current price** for a held instrument → `market_value`/`unrealized` = `None`,
  `price_stale = True`. Do not fabricate; totals expose staleness.
- **Missing required FX rate** (flow or valuation) → raise (a reporting-currency total
  cannot be computed without it).
- **XIRR non-convergence / no sign change** → `xirr = None` (surfaced, not faked).
- Non-finite Decimals rejected at the model boundary (consistent with `shared/money`).

## Testing strategy (TDD, fixed fixtures)

Per-scenario fixtures: TW cash-dividend cost reduction (+ 配股); US DRIP $0-cost
reinvest (30% withholding); MY cash; multi-lot weighted average; partial sells (realized
vs adjusted); opening inventory; multi-currency flows with trade-date FX; sector
allocation; combined view. **Invariant assertions:**
- Adjusted-model total == original-model total (`(price−adj)·sh == (price−orig)·sh + cum_div`) — proves no double count and equivalence.
- No separate dividend-income line is ever added.
- `original_cost` never mutated across the run.
- `adjusted_cost` allowed ≤ 0 (high-yield payback case) and dividends not lost.
- Return rate uses original invested cost (not adjusted).
- DRIP is XIRR-neutral; cash dividends are + inflows; opening inventory flows at build date.
- Oversell raises; missing price flags stale (no fabrication); missing FX raises.
- A known-value XIRR fixture matches `pyxirr` to a tolerance.

## Out of scope (explicit)

Full FX attribution (②); `pricing/` + scheduler (live quotes/FX); `data_ingestion`
(entry/import — fixtures here); persistence (DB read/write); `web_ui`; `llm_insight`;
`strategy`; mode orchestration (試算 / 報告 / 重算).

## Locked-decision override (process)

Adopting the adjusted-cost model changes `domain-ledger.md` (which currently states P&L
uses original cost and adjusted is display-only). First plan task will:
- Update `domain-ledger.md` to the adjusted-cost model (P&L vs adjusted; dividends as
  cost reduction, no separate line; rate denominator = original; adjusted may be ≤ 0;
  no-double-count principle retained).
- Add a `CHANGELOG.md` `[Unreleased]` note recording the decision change with the
  2026-06-06 human sign-off.
- Add `pyxirr` to `pyproject.toml` dependencies (pinned).

## Follow-ups flagged in final review (for the consuming layers)

Deferred from ① on purpose — they are design decisions best made when `forex/` (②),
`pricing/`, and the dashboard consume these results (and they need the human's UX call
on how to present incomplete data):

- **Uniform staleness / coverage contract.** Today the aggregators degrade three
  different ways when prices are missing: `total_return`, `sector_allocation`,
  `combined_view` produce *partial* totals; `xirr_reporting` returns `None` if **any**
  held symbol lacks a price (all-or-nothing). None of the aggregate result models
  (`ReturnSummary`, `SectorAllocation`, `CombinedView`) carry a portfolio-level coverage
  signal — staleness lives only on each `Holding.price_stale`. Before the dashboard
  renders these, add a coverage indicator (e.g. `stale_symbols` / covered-vs-total) to
  the aggregate models and decide whether `xirr_reporting` should report *which* symbols
  are unpriced (e.g. a typed `MissingPriceError`) rather than a bare `None`.
- **Simple-rate scope mismatch under staleness.** `total_return`'s rate excludes a stale
  holding's unrealized from the numerator while its cost remains in the denominator, so
  the rate understates returns when stale positions are present (documented in the
  docstring; fold into the coverage signal above when built). XIRR remains the rigorous
  metric.
