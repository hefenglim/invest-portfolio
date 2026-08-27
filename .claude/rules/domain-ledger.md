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
- `average_cost = total_cost / shares`, **computed on read** — applies to each of the two
  numbers (`original_avg = original_total / shares`, `adjusted_avg = adjusted_total /
  shares`); never store a rounded average as the authority (avoids cumulative rounding
  error across many lots).

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
- **Foreign cash movements are part of the pool (spec 2026-07-30, owner sign-off).** A
  deposit/opening/rebate **in a currency other than the account's funding currency** funds
  the pool and may carry `cash_movements.acq_home_amount` — the home-currency cost of that
  foreign amount. Consequences:
  - **Store the AMOUNT, never the rate** (F1). A rate is an average, and `data-and-pricing.md`
    forbids storing an average as the authority; `fx_conversions` likewise stores two
    amounts. The displayed acquisition rate is `acq_home_amount / amount`, computed on read.
  - **No cost recorded → the amount still funds the balance, but never the average** — a
    rate is never guessed, interpolated, or substituted with the current spot.
  - **`covered_ratio` = basis-known acquisitions / all acquisitions** (F2). Outflows are
    absorbed **pro rata** — cash is fungible and weighted-average tracks no lots. Never
    "balance − unbased amount": that goes negative once the balance drops below the unbased
    amount, which is the reversed-sign figure this rule exists to prevent.
  - **The ratio scales the WHOLE foreign exposure — cash *and* stocks** (F3), because
    `avg_rate` itself is derived from the covered population. Degrading only the cash leg
    leaves the larger error unflagged.
  - Sale proceeds and foreign cash dividends are **not** unbased acquisitions; they keep
    inheriting the pool average, so a ledger with no foreign movements has `covered_ratio`
    exactly 1 and is numerically unchanged by this rule.
- **N1 — a foreign WITHDRAW recognises no realized FX.** It reduces the pool's exposure;
  under weighted average a disposal changes neither the average nor the coverage, so the
  remaining exposure stays self-consistent. If the money was actually converted back to the
  home currency, the correct entry is an **fx_conversion**, not a withdrawal.
- **N2 — filling in an acquisition cost later re-computes history.** All reports rebuild
  from the ledgers (重算), nothing is snapshotted, so adding `acq_home_amount` to an old
  opening also changes previously displayed realized/unrealized FX. That is intended
  (`original_cost` is still never overwritten), but it is a visible change, not a no-op.
- **Realized FX P&L** on reconversion (foreign→home) = home received − (home cost of
  the foreign amount sold, at the pool's weighted-avg rate).
- **Unrealized FX P&L** = remaining foreign exposure marked to current spot vs. the
  weighted-avg acquisition rate.
- **CRITICAL — no double count:** the reporting-currency total / XIRR already embeds FX
  (flows converted at trade-date rates, final value at current rate). 換匯損益 is an
  **attribution breakdown** of that figure, **not** an extra gain added on top.
  Present it as decomposition (asset P&L vs. FX P&L), never additively.

### Which figure actually embeds FX — A · B · B−A (AI-D41, owner ruling 2026-08-24)

The bullet above is true of **XIRR**. It is **NOT** true of `total_return`, and the manual's
invariant **I5 is corrected accordingly**: `portfolio/returns.py` computes
`Σ_ccy (realized + unrealized)_native × spot_today` — the rate is applied to the **gain**, never
to the **principal**. Meanwhile `portfolio/timeseries.py` builds `total_value − net_invested`
with every flow converted at **its own trade-date** rate; that IS the FX-complete lifetime
result, and it was labelled 「浮動損益」. Three figures, one decomposition:

| | figure | what it is |
| --- | --- | --- |
| **A** | `total_return` — 資產損益（不含本金匯率） | each currency's native P&L at today's spot |
| **B** | 含匯兌總損益 = `total_value − net_invested` | flows at trade-date FX, value at spot → FX-complete |
| **B − A** | 本金匯率效果 | `cost × (spot_now − acq_rate)` — the 換匯損益 card's content |

⚠ **Adding the 換匯損益 card to A double-counts the cross term** `(MV − C)(spot − acq)`. A, B and
B−A are presented **side by side as a decomposition, never summed** — the same red line as the
bullet above, applied to the figure the bullet does not cover. `total_return`'s definition is
**unchanged**; only its label and the presence of B alongside it are new.

### Which cash movements are return, and which are capital (AI-D42, 2026-08-24)

`shared/cash_kinds.py` already draws this line — reuse its table, never re-derive the predicate:

- **Pool income / cost → they ARE return.** `REBATE` · `INTEREST` (+) · `INTEREST_EXPENSE` ·
  `BROKER_FEE` (−). These are P&L with no offsetting security flow, so `xirr_reporting` takes
  them as flows (sign from `CASH_KIND_TABLE.credit`, each converted at its own date's FX).
  FE-D1's 77% rebate is one of them, and it is **0.229% of capital per round trip** — the 群益
  charge-first model is the owner's normal billing, not an edge case.
- **Capital movements → they are NOT return.** `DEPOSIT` · `WITHDRAW` · `OPENING`. Admitting
  them would silently redefine XIRR from "the return on money put into securities" to "the
  return on the account" — a different metric, and not this one.

## Data integrity (carried over from the human's spec)

- Permanent sources of truth: **opening inventory, transaction ledger, dividend ledger,
  FX-conversion ledger**. All reports are rebuilt from these (重算 mode).
- `original_cost` is never overwritten.
- Opening inventory is **not** a trade flow, but carries a **build date** + original
  cost total (needed for XIRR).
- **Sell qty > holdings → block direct deduction; require user confirmation** (input
  error vs. short sale). The check is **DATE-AWARE** (2026-07-31): the covering position is
  the one that exists **on the sell's own trade date** (`holdings.shares_through`), not the
  net across all dates. A net-only check let a back-dated sell through whenever a LATER buy
  covered it — and the replay then discarded that symbol's cost basis permanently. Mirrors
  the cash ledger's `running_min` guard (audit C3).
- **賣超 (undeclared oversell) is STICKY.** An acked oversell discards the position's cost
  basis and emits no realized row (待釐清). A later buy nets the position positive again but
  does **not** restore the discarded basis, so the flag must not be cleared by one either —
  it is raised on "was ever negative", not on the final sign.
- **A PREVIEW MUST MIRROR THE REPLAY'S BRANCHES, LITERALLY** (audit 2026-08-24, review §1.3).
  Every "what would this trade do" surface — the manual sell preview
  (`api/routers/input_center.py`), the drawer 試算 (`strategy/whatif.py`) — reproduces
  `portfolio/cost_basis.py`'s **branch structure**, not just its happy-path formula. The three
  sell branches are **declared short** (realized on `min(qty, max(shares,0))` only; the
  remainder opens/extends a short and emits **no** realized row), **undeclared oversell**
  (basis discarded → **no** realized row, 待釐清), and **ordinary**. Applying the ordinary
  formula to all three fabricated a `+500` realized on a short extension the ledger books
  nothing for — while the holdings column two rows above honestly printed 「—」, and the drawer
  gave a third answer for the same trade. **One app must not show three answers**, and a
  preview that disagrees with what will be written is worse than no preview.
  ⚠ **EVERY projected column, not just the realized one** (widened 2026-08-27, sweep F-01).
  The 2026-08-24 pin was written as 「`preview.realized` ≡ the booked realized row for all three
  shapes」 — and that is exactly what shipped, so the AVERAGE pair rendered beside it went on
  projecting a single happy path for another three days. A declared short leaves a SHORT lot
  based on the proceeds received; an undeclared oversell DISCARDS the basis; a full exit leaves
  no position at all (`build_book` drops `shares == 0`). The preview printed the PRE-trade
  average for all of them, so a card read 「240.00 → 240.00」 directly above its own warning that
  the basis was about to be permanently discarded. **A pin that names one field certifies one
  field.** The contract test is parametrised over every sell shape × every projected column and
  compares against `build_book`, so a new column or a fourth branch fails on its own
  (`tests/api/test_review_r1_preview_mirrors_replay.py`).
  ⚠ **Project the TOTALS, then divide on read** — never project an average directly. The replay
  moves `original_total` / `adjusted_total` and computes `avg = total / shares` on read
  (`data-and-pricing.md`); an average projected in one step re-divides and can differ in the
  last digit from the row that gets written.
  ⚠ **A surface that cannot know which branch applies must say so, not pick one.** The drawer
  has no 放空 declaration to read, so it states the fork and gives NO figure — for the averages
  exactly as it already did for the realized amount.

### Declared short sale (owner ruling 2026-07-31, spec option C)

The earlier "NOT short-position accounting" stance is **narrowed, not reversed**: short
accounting applies **only** to a sell the user explicitly declared, never to an oversell.

- A transaction carries `short_sale` (default **false**). Only a declared sell may exceed
  holdings without the 賣超 guard. It is never inferred — the system cannot distinguish a
  genuine short from a missing buy, and auto-applying short accounting would turn a
  data-entry slip into a plausible-looking realized loss (the dangerous failure mode: a
  wrong number that looks right).
- **Replay (weighted average, no lot tracking — consistent with the equity cost method):**
  a declared sell first sells the LONG lot (ordinary realized P&L), then opens/extends a
  SHORT lot holding the **net proceeds received**. A buy first **covers** the short, then
  adds to the long. Long and short are therefore mutually exclusive by construction, so a
  position is long / flat / short and carries ONE signed quantity.
- **Cover P&L (the owner's rule):** realized = `(short weighted-avg sale price − the
  covering buy's all-in per-share cost) × shares covered`, dated the **cover** date, and the
  leftover shares start their long life at that same per-share cost. Emitted as a realized
  row with `kind="short_cover"` — a capital gain/loss, so it belongs in the tax package.
- **Presentation:** an open short reports `shares < 0` with the proceeds as its (negative)
  basis, so `avg = total/shares` is the average sale price and
  `unrealized = (price − avg) × shares` profits when the price falls — every existing
  formula works unchanged on the signed quantity. Flagged `short_open`, which the UI renders
  **differently from `oversold`**: one is a real priced position, the other an unresolved
  data problem. Any **ratio** over the basis must divide by `abs(cost_total)`: the basis is
  negative, so the bare ratio flips sign and shows a profitable short as a loss (the audit-H1
  trap from the other direction). `fully_recovered` (已回本) must be gated on `not short_open`
  for the same reason. The trend / net-worth series **includes** an open short — its negative
  market value is a liability; excluding it while cash still holds the proceeds counts the
  two halves of one trade asymmetrically.
- **A dividend landing on an open short is NOT representable.** A short seller pays the
  dividend in lieu and this ledger has no debit row for that. Booking the recorded (positive)
  net as income, or adding DRIP/STOCK shares to the long lot, are both money-of-record
  errors — the latter also breaks long/short exclusivity, and a DRIP equal to the short nets
  the position to zero so the holding and its proceeds vanish from the report. Therefore:
  **raise** on the strict path, and on the dashboard path skip the event and flag the
  position `unbookable_dividend` (待釐清), never book it. Record such a payment as a cash
  movement instead.
- **`gross_invested` excludes short capital** (owner-accepted limitation): covering a short
  is funded by the proceeds already received, so a cover does not add to the denominator and
  the proceeds do not reduce it. Consequence: a currency whose only activity is a short has
  `gross = 0`, so its simple return `rate` is `None` even with realized profit — XIRR remains
  the rigorous metric. In a mixed portfolio the short's P&L rides the long position's
  denominator.
- **Known interpretive limits** (not defects): XIRR over a *pure* short round trip reports a
  borrowing rate (the flow pattern is a loan: proceeds in, cover out), and allocation weights
  use a net-exposure convention that can exceed 100% or sign-flip when the portfolio is net
  short. Both are honest readings of a degenerate input, not miscalculations.
- Modes: 試算 = compute, no write · 報告/更新/績效 = full report + live-price fetch ·
  重算 = rebuild all stats from ledgers.
- Live price unobtainable → label clearly; **never guess**.
- Data over narrative; thousands separators in all displayed tables.
