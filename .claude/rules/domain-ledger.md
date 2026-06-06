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

- **Accounting model = adjusted cost (decided 2026-06-06, human sign-off).** P&L is computed against `adjusted_cost`; cash dividends are folded into cost
  (NOT a separate income line). `original_cost` is never overwritten and is retained for
  the return-rate denominator and the capital-gain-vs-dividend split.
  - `adjusted_total = original_total − cumulative cash dividends`; `adjusted_avg =
    adjusted_total / shares`; **may be ≤ 0** (high-yield payback) — never floored.
  - **Realized P&L** (on sell) = net proceeds (after fees+tax) − `adjusted_avg × shares_sold`.
  - **Unrealized P&L** = (market − `adjusted_avg`) × shares.
  - **Total return** = realized + unrealized (both vs adjusted), incl. realized from
    closed positions. Dividends enter exactly once (via cost reduction); **no separate
    dividend line** (the old double-count trap).
  - **Total return rate** = total return / **original invested cost** (cumulative, not
    annualized). **XIRR** is the annualized, money-weighted, FX-aware decision metric.
  - **Cost basis is all-in:** buy-side fees + tax are part of `original_total` (and thus
    adjusted), so every transaction cost is captured.
- **Dividend treatment:** TW/MY cash → reduce `adjusted_total` by net received. US DRIP →
  net reinvested as $0-cost shares (does NOT reduce `adjusted_total`). 配股 → add shares,
  no cost change. Display-only: 回本進度 / 股利回收率 = cumulative cash dividends /
  original_total.
- **XIRR cashflow signs:** buy −, sell +, cash dividend + (TW/MY), current market value +
  (final period). DRIP US dividends are **neutral** (not a + inflow; reinvest not a −
  outflow) (Q3). Opening inventory contributes a flow equal to its `original_cost_total`
  dated on its **build date** (so opening capital is counted). Single reporting currency;
  every flow converted at **trade-date FX** (Q7).

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
