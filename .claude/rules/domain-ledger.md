# Rule: Ledger & Portfolio Domain Model

The financial core. Read this before touching `portfolio/`, `data_ingestion/`, or any
calculation. Every number must be reproducible from the ledger (rebuild / 重算).

## Accounts — a first-class entity

Three orthogonal dimensions: **market** (where it trades) · **account** (which broker
holds it) · **currency** (the instrument's quote currency). The same market can span
multiple accounts with different rules, so fee/tax/dividend rules bind to the
**account**, never to the market.

| Account | Market(s) held | Quote ccy | Funding ccy | Dividend model |
| --- | --- | --- | --- | --- |
| TW broker | TW | TWD | TWD | cash → cost-reduction (+ optional stock dividend) |
| Charles Schwab (Intl) | US | USD | TWD | DRIP, 30% US withholding, $0-cost repurchase |
| Moomoo MY (US) | US | USD | MYR | DRIP, 30% US withholding, $0-cost repurchase |
| Moomoo MY (MY) | MY | MYR | MYR | cash (net received) |

A transaction carries `account` + `instrument`; the instrument knows its market and
quote currency. Moomoo MY is one brokerage account holding USD-settled US stocks
(funded via MYR→USD conversion) **and** MYR-settled MY stocks.

## Cost basis

- **Weighted-average cost method**, all markets.
- Tracked in the instrument's **quote currency** (TW→TWD, US→USD incl. Moomoo, MY→MYR).
- Maintain **two numbers**: `original_cost` (永久保留, never overwritten) and
  `adjusted_cost` (after dividend adjustments).
- `average_cost = total_cost / shares`, **computed on read** — never store a rounded
  average as the authority (avoids cumulative rounding error across many lots).

## Dividend models (per account)

- **TW (cash):** reduces adjusted cost — `adjusted_total = original_total −
  cumulative_dividends`; `adjusted_avg = adjusted_total / shares`. Optional **stock
  dividend (配股)**: increases shares with no cash. Record gross & net.
- **US — Schwab & Moomoo (DRIP):** 30% US withholding; net dividend reinvested →
  repurchased shares recorded at **$0 cost** (lowers average via added zero-cost
  shares). Record gross, 30% withholding, net, reinvested shares + reinvest price.
- **MY (cash):** record **net cash received** (single-tier; confirm any high-income
  dividend tax in the data-source probe).

## P&L and returns — single source of truth, NO double counting

- **Realized P&L** (on sell) = net proceeds (after fees + tax) − `original_avg ×
  shares_sold`. Uses **original cost** (Q1).
- **Unrealized P&L** = (market price − `original_avg`) × shares. Uses **original cost**.
- `adjusted_cost` / `average_cost(adjusted)` / **股利回收率** are **display-only
  "回本進度"** — they are NOT inputs to total return.
- **Total return** = realized + unrealized + dividend income. Dividends are added
  **exactly once** here (because P&L uses original cost, not dividend-adjusted) — this
  is the fix for the original double-count (Q1).
- **Total return rate** denominator = **original invested cost** (Q2).
- **XIRR is the primary return metric** (Q2). Cashflow signs: buy −, sell +, cash
  dividend +, current market value + (final period). DRIP US dividends are **neutral**
  (not a + inflow; reinvest not a − outflow) (Q3). TW/MY cash dividends are + inflows.
  Opening inventory contributes a flow equal to its `original_cost_total` dated on its
  **build date** (so opening capital is counted). Single reporting currency; every
  flow converted at **trade-date FX** (Q7).

## FX / currency-exchange ledger (Q12)

- A dedicated ledger records **every actual conversion**: date, account, from_ccy,
  from_amount, to_ccy, to_amount → implied rate. (Schwab: TWD→USD; Moomoo: MYR→USD.)
- Each foreign-currency pool (per account) carries a **home-currency cost basis** =
  weighted-average acquisition rate. Schwab USD pool anchored in **TWD**; Moomoo USD
  pool anchored in **MYR**.
- **Realized FX P&L** on reconversion (foreign→home) = home received − (home cost of
  the foreign amount sold, at the pool's weighted-avg rate).
- **Unrealized FX P&L** = remaining foreign exposure marked to current spot vs. the
  weighted-avg acquisition rate.
- **CRITICAL — no double count:** the reporting-currency total / XIRR already embeds FX
  (flows converted at trade-date rates, final value at current rate). 換匯損益 is an
  **attribution breakdown** of that figure, **not** an extra gain added on top.
  Present it as decomposition (asset P&L vs. FX P&L), never additively.

## Data integrity (carried over from the human's spec)

- Permanent sources of truth: **opening inventory, transaction ledger, dividend ledger,
  FX-conversion ledger**. All reports are rebuilt from these (重算 mode).
- `original_cost` is never overwritten.
- Opening inventory is **not** a trade flow, but carries a **build date** + original
  cost total (needed for XIRR).
- **Sell qty > holdings → block direct deduction; require user confirmation** (input
  error vs. short sale).
- Modes: 試算 = compute, no write · 報告/更新/績效 = full report + live-price fetch ·
  重算 = rebuild all stats from ledgers.
- Live price unobtainable → label clearly; **never guess**.
- Data over narrative; thousands separators in all displayed tables.
