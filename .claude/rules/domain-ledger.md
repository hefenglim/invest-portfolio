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

- **Costs of trading and financing → they ARE return.** `REBATE` (+) · `INTEREST_EXPENSE` ·
  `BROKER_FEE` (−). These are P&L with no offsetting security flow, so `xirr_reporting` takes
  them as flows (the set is `portfolio/returns.py::XIRR_CASH_KINDS`; the sign comes from the
  kind's `credit` axis via `shared/cash_kinds.py::movement_sign`, each converted at its own
  date's FX). FE-D1's 77% rebate is one of them, and it is **0.229% of capital per round
  trip** — the 群益 charge-first model is the owner's normal billing, not an edge case.
- **Capital movements → they are NOT return.** `DEPOSIT` · `WITHDRAW` · `OPENING`. Admitting
  them would silently redefine XIRR from "the return on money put into securities" to "the
  return on the account" — a different metric, and not this one.
- **`INTEREST` (earned on idle cash) is NOT return either** — the principal that earns it never
  entered this metric's denominator, so crediting its yield to the numerator is asymmetric.
  (This bullet listed `INTEREST` among the flows until 2026-09-06 — a transcription error; the
  ruling text in `docs/spec/2026-08-16-ai-assistant.md` AI-D42, the manual §7.2 and the code
  all exclude it, and adding an `INTEREST` row leaves XIRR byte-identical, measured.)

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
- **A cash guard compares the pool DAY BY DAY, never lowest point against lowest point**
  (DEF-090, 2026-10-01; `shared/cash_dip.py`). A withdrawal / 換匯 is refused when it makes
  any day negative or more negative (`new_dip`); every ack-able `negative_cash` door — the
  import-batch undo, and since DEF-092 (owner ruling A, 2026-10-02) the single-row edit /
  delete and FX delete doors, and since DEF-093 (2026-10-03) the FX EDIT door for every pool
  it takes money from (its from-pool stays hard) — asks only when a day turns negative or
  drops below the pool's old low (`caused_dip`), so 「此筆會使…」 never quotes a dip the row
  did not cause. Comparing only the two lowest points let a back-dated withdrawal open a new
  negative stretch whenever an older, unrelated dip was deeper — the aggregate hid the event,
  the same class as the net-only sell check above. Every refusal names the FIRST short day
  and the lowest point (owner ruling 2B, DEF-091): 「自 D1 起為負，最低於 D2 降至 −N」. A door
  that changes a two-legged row checks EVERY pool it touches, not only the leg it spends.
- **Same-day trades replay in WRITE order — the ledger id — never buys-before-sells**
  (DEF-012, recorded 2026-09-23). A transaction has no time-of-day column, so the only
  evidence of intraday order is the row id: the order the owner entered the rows, or the
  order the statement listed them. `shared/ledger_events.py` therefore has ONE `TRADE`
  priority (`BUY`/`SELL` are aliases of it) and `build_book` sorts `(date, priority, id
  order)`; OPENING and CORPORATE_ACTION still precede the day's trades and DIVIDEND still
  follows them. Measured before the rule (functional test B-18): 2884 held 100 @ 9,320, then
  buy 200@44 → sell 100@45 → buy 100@46 on one day previewed the sell at realized −1,580 and
  booked −1,223 once the third row landed, cost 17,070 where the entry order gives 16,713.33.
  Consequences: **(1)** the manual preview appends its draft LAST and so agrees with the
  replay; **(2)** a CSV file's rows take ids in file order, so its preview counts a same-day
  sibling as cover only when it is listed ABOVE the sell (`validate.siblings_booked_before`)
  — a file with the sell above its buy is a 賣超 in both the preview and the replay, not a
  clean preview followed by a discarded basis; **(3)** a broker statement listed newest-first
  must be re-ordered chronologically WITHIN the day by its converter, or its ids encode the
  reverse of what happened; **(4)** the stress-audit oracle keeps its own transcription;
  **(5)** every correction door re-sorts its would-be ledger with `store.*_order_key` (each
  the Python twin of its `list_*` ORDER BY) before replaying it (CR-03, 2026-10-04): an edit
  replaced its row IN PLACE, so moving a back-dated buy onto its sell's day replayed
  buy-then-sell in the guard and sell-then-buy after the write — no question, sticky 賣超. Demo
  data is unaffected: the one same-day buy+sell pair on the demo ledger (tw_broker 2603
  2026-07-02) already had the buy's id below the sell's.
- **Every row counts from its OWN date** (DEF-016 for dividends; widened to trades, openings,
  FX conversions — and with them cash movements and corporate actions — by DEF-056; owner
  rulings 2026-09-24, option A). A confirmed dividend is stored on its payment date (the inbox
  confirms a payout the day it is announced) and a trade / opening / conversion may be entered
  ahead of its date (allowed with a warning since DEF-014), so a row can lie after the
  valuation date. Every VALUATION as at day D — the book / adjusted cost, holdings, 總報酬,
  XIRR, 已收股利, allocation, the B−A attribution, the FX pool's realized / unrealized FX, the
  drawer, 試算, the tax package, the sell hints, rebalance, the stress-audit oracle — reads
  `LedgerBundle.valued_as_of(D)` (FX conversions and cash movements, which the bundle does not
  carry: `valued_rows_as_of`), one predicate `counts_by(counts_on, D)` = `counts_on <= D`:
  a trade from its `trade_date`, an opening from its `build_date`, a dividend from its
  `effective_date` (payment date; the ex-date for a 配股 with one), a corporate action from its
  `date`. Actions are cut with the rest because only a date PREFIX is a state that exists: a
  future split applied to today's shares meets prices that are still pre-split (the stored
  price basis folds in only splits a fetch has seen), and a future action on a future buy
  would find no source. The cash pool (`as_of`, M5-06) and the trend (`through`, per day — it
  delegates to the same cut) already cut this way. VALIDATION replays (重算, correction-door
  replay guards, corporate-action reachability, the date-aware sell guard, the draft preview
  which replays as of the trade date since DEF-048) keep the WHOLE ledger — they check what
  will be stored, not what has happened. The validation SHARE path
  (`data_ingestion/holdings.py`: the walker, the naive sum, `_later_event_dates`) dates a 配股
  by the same `effective_date` (CR-05, 2026-10-04 — it used the payment date, so a sale between
  ex-date and pay date was refused as 賣超 while the replay booked it): one dating rule for every
  share counter. A ledger row dated after the SERVER's today carries
  `counts_from` and reads 「未來日期：YYYY-MM-DD 起計入」 on EVERY surface that lists it — the
  ledger tabs, the cash page's lists, the 現金收支明細 statement (which also draws the printed
  report's cut line and marks each future running balance 投影, because newest-first puts
  those rows directly under a 目前餘額 they are not part of), the drawer's 配息史, and the two
  reconciliation CSVs that mirror those tables (a trailing `counts_from` column). R4 badged a
  hand-picked list of tables and missed the statement (DEF-056 R5), so the guard is now a
  sweep: `tests/e2e/test_def056_r5_future_row_sweep_flow.py` fails on ANY visible row that
  prints a future date without the badge. The drawer's 交易明細 lists no future row at all
  (owner ruling ③, 2026-09-25). Measured before the rule (golden, day
  2026-06-11): an 18,200 TWD dividend paying in 15 days moved XIRR 0.5677 → 0.6703 and 總報酬
  111,600 → 129,800; a 1,000-share 2330 buy dated 2026-07-01 put 2,000 shares in today's holding
  (market value 639,600 → 1,239,600, XIRR 0.5677 → 0.6577); a 2-for-1 AAPL split dated 2026-07-01
  doubled the position at the unsplit price (XIRR → 0.8024, 總報酬 → 151,200) — each while the
  cash pool did not move.
- **「持有」 has two readings, each with ONE owner** (DEF-075, owner ruling ② B, 2026-09-26:
  "a future-dated sale has not happened yet, so the position is still held"). The REGISTRY
  question — does the watchlist badge read 持有, may the symbol be archived / removed / purged,
  is it held for the target-weights badge, the signal `held` flag, the quote jobs' 部分
  threshold and the Alpha Vantage held universe — is `data_ingestion/holdings.py::
  holds_position` / `held_among`: a position in any account today or on any later ledger date
  (a declared short is a position; so is a symbol whose only rows are still ahead). The
  VALUATION question — which symbols today's book holds (insight / alert producers, the
  digest, the sell form's sellable shares) — is the book cut at the valuation day
  (`api/insight_service.py::held_in_book`, `valued_as_of`). The two differ only on a symbol
  whose every row is still ahead. `current_shares > 0` (all dates netted, long only) answers
  neither, and four readers used it until R6: the badge said 觀察 while 移除 answered 422
  「持倉中的標的不可移除」. `tests/contract/test_def075_registry_held_readers_agree.py` drives
  every registry reader through its public door against `holds_position`.
- **賣超 (undeclared oversell) is STICKY.** An acked oversell discards the position's cost
  basis and emits no realized row (待釐清). A later buy nets the position positive again but
  does **not** restore the discarded basis, so the flag must not be cleared by one either —
  it is raised on "was ever negative", not on the final sign. It is recorded on the BOOK
  (`Book.oversold_positions`, CR-11 2026-10-04), because `build_book` drops a 0-share position
  from `holdings`: a 賣超 netted back to exactly zero was invisible to every correction door,
  the XIRR gate and the drawer, and deleting its first buy erased the holding and its realized
  row with no question. Every reader of "is this position 賣超" reads the book's record. The
  book also records every position it OPENED, flat included, with both 待釐清 flags
  (`Book.opened_positions`: `oversold`, and the sticky `unbookable_action` a refused corporate
  action sets); the manual preview withholds its position math whenever that record flags the
  position — on today's book, the trade-date book and the book with the draft, for every branch
  (R4 / S1) — and the drawer tells 「部位已結清」 from 「從未持有」 by it (S9).
- **A dividend needs a position that exists by its effective date** (CR-02, 2026-10-04). Every
  door refuses one without it (`dividend_without_position`: no opening, non-zero trade or
  EXCHANGE / SPINOFF into the symbol in that account by then); a stored one is refused on the
  book (`Book.orphan_dividends`, also in `refused_dividends`), named, and withholds XIRR — it is
  never income and never a ghost holding, and it never answers 500 (it did: `build_dashboard`
  caught only `ArithmeticError`). A payout after a full close still books as realized income
  (H2). One rule, two forms: `holdings.position_exists_by` (SQL, the doors) and
  `LedgerBundle.position_opened_by` (the replay guard's fallback); both count an action only as
  the replay reads it (convertible, both symbols registered), and their one stated difference
  from the replay is a readable action the replay refuses (S8). Correction doors compare
  stranded dividends per ROW, never per (account, symbol) — a legacy orphan on the key masked a
  new one (R5) — and 股利總覽 skips every dividend the book refuses (R6), so one payout never
  gets three answers.
- **An EXCHANGE's `vacated_to` describes the position's CURRENT life** (CR-06, 2026-10-04). A
  trade, an opening, or an EXCHANGE / SPINOFF landing shares on a vacated ticker clears it, so
  a reused ticker's dividends book; while it stays vacated, a dividend on it is still refused
  (E24) and flagged on the live successor.
- **A refused dividend moves no money and counts nowhere** (owner rulings ⑪ / K2 / K3 / X1 / Y1 /
  Y3, 2026-10-05; manual §6.6). The replay refuses three kinds — no position by the effective
  date (`Book.orphan_dividends`), an open declared short and a vacated ticker
  (`Book.unbookable_dividends`, with cause, carrier and the strict sentence) — all in
  `Book.refused_dividends`. Such a row is excluded from EVERY money figure: cash pools and the
  statement balance, the guards' pool, the FX pool's foreign cash, net worth, `net_invested` /
  XIRR / B / TWR / the benchmark flows, realized income, shares and every listing total — through
  ONE decision, `returns.DividendRefusals` over the replay's own refusals, never a re-derivation.
  A surface that LISTS dividend rows lists it marked 「未入帳（待釐清）」 with that sentence; the
  raw re-importable ledger CSV stays raw (DEF-026). The 待釐清 is on the BOOK, so it does not die
  with a flat carrier: every trend day from it is incomplete and B is withheld with the reason,
  while a HELD carrier keeps its value (its shares and price are real — only a 賣超 or an
  unapplied action drops a value). The remedy wording has one owner
  (`cost_basis.ORPHAN_DIVIDEND_REMEDY` / `CASH_MOVEMENT_REMEDY`, unified to 「…並刪除該筆股利」),
  and the drawer offers the remedy of each cause from server facts. An unreplayable ledger counts
  every payout, as before (X2).
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

### US equity options (owner rulings R1–R7, OPT-D1..D22, 2026-10-09)

Spec of record: `docs/spec/2026-10-09-options-ledger.md` (approach ④). US only (R6); single-leg
calls and puts with the standard 100-share deliverable; anything else is refused (OPT-D11).

- **A contract is an instrument in the trade ledger** (R1): `instruments.asset_type = 'option'`,
  its terms parsed from the OCC symbol (`XYZ261218P00050000`) — never stored a second time
  (OPT-D22). `shared/options.py` is the ONE owner of the symbol, the multiplier, the standard-
  adjustment check and the §6.1 vocabulary; nothing re-derives them.
- **Quantity is in SHARE-EQUIVALENTS** (contracts × 100) and the price is the premium per share
  (OPT-D1), so every `quantity × price` in the package stays correct untouched. The manual and
  edit doors take 口數 (`contracts`) and the server converts; the CSV takes share-equivalents.
- **Sell-to-open IS the declared short sale** above; buy-to-close covers it (`short_cover`).
- **Expiry / assignment / exercise is a price-0 closing trade with `close_reason`** (OPT-D2),
  fee = tax = 0 without the engine. Its side and quantity are DERIVED from the position (an
  expiry closes the whole position, on the expiry date). R5: on assignment / exercise the
  premium stays OPTION P&L — the stock leg is an ordinary trade at the strike, never re-costed.
- **A close row never opens a lot (§4.4).** The replay refuses a `close_reason` row that is a
  BUY beyond the short lot or a SELL beyond `max(long, 0)` (no position at all included): it
  books nothing, is recorded in `Book.refused_closes` (and sets the sticky `unbookable_close`
  when the position exists), and XIRR and B are withheld while any is recorded — the
  `orphan_dividends` posture. A correction that would strand a close row is refused hard
  (`refused_close_correction`). `classify_option_rows` mirrors exactly this rule, so the side
  label 未入帳（待釐清） and the record cannot disagree.
- **`OPTION_ADJUST`** (OPT-D3) moves a contract — long or short — onto its adjusted successor,
  1 → N contracts only (`ratio_from == 1`, OPT-D16; strike × contracts conserved, same right and
  expiry). It sits after SPLIT in `_apply_action` and has its own guards (E22, opposite
  direction); EXCHANGE's short refusal (E5) does not apply to it.
- **Valuation (R2, OPT-D4):** an open contract is valued at its LAST ordinary trade price from
  the ledger (close rows excluded; after an `OPTION_ADJUST`, the LATER of the destination's own
  last trade and the predecessor's last price ÷ ratio, and nothing from an adjustment the replay
  refused — final review R1-M2, 2026-10-10; on a 待釐清 contract no price, value or obligation)
  — never a quote,
  never written to `prices`, never in freshness. POINT-IN-TIME figures include it (KPI 總市值 /
  總報酬 / XIRR's terminal value / total return A / the FX pool / 月度成績); TREND-DERIVED figures
  exclude its value (the trend, B, TWR, the benchmark) with one note (R3). 本金匯率效果 (B − A) is
  withheld while an option is open (OPT-D20) — B leaves the option out while A carries its
  unrealized, so their difference would read the option's value as FX.
- **Stock-only lists (OPT-D5):** `holdings`, weights, allocation, subtotals and `realized.rows`
  are stocks only; the options live in `DashboardData.options` (absent on a ledger with no
  option, so a stock ledger's payload is byte-identical). KPI 總市值 = the stock grand subtotal +
  `options.market_value`. AI insights, signals, alerts, digest, news, fundamentals, target
  weights, rebalance and what-if exclude options (OPT-D10).

## Month and year closes (月結／年結, PERF-D7; spec 2026-10-18 §11, as built 2026-10-20)

A close is a COMPUTED figure, never a source of truth. It is rebuilt from the ledgers like
every other report, and the settlement store only keeps it (`data-and-pricing.md` → the
settlement store). The 23:50 月度快照 it replaces froze the dashboard's KPIs on the last evening
of the month. That was before the US close of that day existed, and the freeze was never
corrected afterwards. Owner `portfolio/period_close.py`; the worker, the read path
(`api/snapshots.py`) and the stress oracle all compute the same definition.

- **A close is valued AS AT its end day D**, the period's last CALENDAR day (a weekend or a
  holiday carries the last close forward, as every chart does). Its KPI block is the
  dashboard's own computation (`dashboard.kpis_as_of`) with the sources "on or before D":
  - **the ledger** — the book of `valued_as_of(D)`, with cash movements and FX conversions cut
    by `valued_rows_as_of(…, D)`. Every row counts from its own date;
  - **a held stock** — the latest stored close with `as_of_date ≤ D`, re-expressed by
    `price_in` for the splits in `(priced_on, D]`. A split dated ON a weekend D halves Friday's
    close and doubles the shares, so the value at D is unchanged;
  - **an option contract** — its last ordinary trade price on or before D (R2);
  - **FX** — the latest rate on or before D (direct, else inverse). The rates used and their
    dates are stored in `fx_used`.

  A held stock with no such price follows exactly the live rule. Its value is `None`, so it is
  left out of 總市值 and of the unrealized part of 總報酬 while its cost stays in the rate's
  denominator. XIRR is `None`, because XIRR is all-or-nothing. And the close says so.
- **Every KPI is SINCE INCEPTION, as at D:**
  - 總市值, stocks + options;
  - 累計總報酬 A and 累計報酬率;
  - 累計 XIRR with `xirr_window_days` / `xirr_reason`, under the same gates as the live KPI
    (manual §7.2.1, owner ruling 2026-10-20);
  - `by_currency_value`.

  None of these is the period's own return. A year row is never "that year's return".
- **The period's own return is `period_twr` = `I(D) / I(previous end) − 1`**, on the full-series
  TWR index that `/api/performance/twr` uses. That index is the trend's, so options are excluded
  (R3).
  - The first period starts from the index's anchor (100).
  - The TWR is `None`, with its reason, when either end day is incomplete or has no trend point.
  - While history is stale, a period whose `(previous end, D]` contains `live_from` or overlaps
    the stale range reads `None` with 「待結算：此期間包含尚未重新結算的日子，當期報酬暫不計算」. A
    return across the join of stale and live days would book a back-dated position's whole gain
    as one day's return.
- **The trend figures at D** are `trend_total_value` (證券市值: stocks only, a partial sum on an
  incomplete day), `net_invested` and `net_worth`, all from the settled day D. They are named
  apart from KPI 總市值 (options included, all-or-nothing on FX), so the two are never
  confused. **FX P&L at D is not part of a close** in this release, and its absence is not a zero.
- **`notes`, every reason a figure is absent or unconfirmed:**
  - `incomplete`;
  - `missing_prices`, and `stale_prices` (older than the live 4-day age before D);
  - `unconfirmed_closes` — a close settled by the 4-day rule that no `ok` quote run confirmed,
    matched by the PRICE date, so a Saturday month end carries Friday's partial run;
  - `missing_fx` / `stale_fx`;
  - `pending_positions` — the 待釐清 on the book at D: 賣超, an unapplied action, a refused
    dividend, a refused close;
  - `options_value_excluded`, and `options_past_expiry`;
  - `trend_unavailable` — no trend point at D. Either a flow has no on-or-before rate (「部分投入
    流量缺少當日匯率，該日沒有走勢資料」), or the series has not started yet (「走勢自第一筆交易、股
    利或期初庫存起算，此日尚無走勢資料」);
  - `missing_cash_fx` — a non-empty cash pool at D with no rate to the reporting currency;
  - `dividends_receivable` — computed when the close is READ, never stored. It lists every
    non-STOCK row with `ex_date ≤ D < payment date`: shown, never counted. A refused one is
    marked 「未入帳（待釐清）」 (X1). It is hidden on a stale row.
- **The closes start at the month of the ledger's FIRST ROW in any table.** A deposit or a
  conversion may precede the first trade. So every period's activity lies inside its own
  calendar period, and each year ties out with that year's tax sheets. A month before the
  series has its cash, its activity and its KPIs, with zero holdings, no trend point
  (`trend_unavailable`) and XIRR 「尚無持倉，XIRR 不計」.
- **Activity in the period** (spec §11.3). Each line is in native currency AND in the reporting
  currency at each row's OWN date's FX (the trend's flow conversion; `None` when a row has no
  rate). Rows count inside `[the period's first day, D]` by the dates the ledger cut uses. The
  lines:
  - **realized — sales and short covers** (`kind` `sale` / `short_cover`), split by
    `asset_type`. Each is shown vs ADJUSTED cost (`realized`, ties to 總報酬) and vs ORIGINAL
    cost (`proceeds_net − original_cost_removed`, the capital gain);
  - **realized — dividends after a full close** (audit H2), listed apart because the same payout
    is also on the dividends line;
  - **dividends** — BOOKED rows only, by type, account and currency. CASH / NET carry gross,
    withholding and net; DRIP adds the reinvested shares; STOCK is shares only;
  - **dividends not booked** — the period's refused payouts: count and net, 「未入帳（待釐清）」;
  - **trade fees and tax**, by account;
  - **every cash-movement kind**, by account × currency;
  - **FX conversions** (from / to amounts) and **realized FX** (`realized_fx_rows_as_of`, the
    換匯損益 owner);
  - **openings** with `build_date` in the period.

  ⚠ **The lines are NOT additive.** Under the adjusted-cost model a sale's realized already
  contains the cash dividends folded into that position's cost, so 已實現 + 股利 is not "what
  the period earned". No total of them is formed or shown.
- **Tie-out with the tax package, sheet by sheet.**
  - Realized sales and covers, both columns, ≡ `realized_gains_<year>.csv`.
  - Booked CASH / NET / DRIP dividends ≡ `dividends_<year>.csv`.
  - Realized FX ≡ `fx_realized_<year>.csv`.

  It holds by construction: `period_close.realized_rows` / `dividend_rows` / `fx_realized_rows`
  are the ONE row selection both `export/tax.py` and a close read. The tax package replays
  STRICTLY, so on a ledger with a refused payout it answers 422 and there is no package to tie
  to. The close then lists the refusal as 未入帳（待釐清）. Fees, cash movements, conversions,
  openings and cash at D have no tax counterpart.
- **Holdings and cash at D.**
  - **Holdings:** every position, per account × symbol, stocks AND options tagged `asset_type`
    (OPT-D5). Each row carries the shares, the original and adjusted cost totals, the price used
    and its date, the market value and the unrealized P&L (`None` on a 待釐清 position), and the
    待釐清 flags.
  - **Cash:** every pool's END-OF-DAY balance at D (`cash_balances(…, as_of=D)`), fed with
    BOOKED dividends only (`booked_dividends(…, book.refused_dividends)`, ⑪), so a refused payout
    never lands in month-end cash. Its reporting value is at D's rate. An empty pool needs no
    rate. So cash ties to each broker's month-end statement, as positions do.
- **The year close ≡ the December close.** They share the same D (12-31), so they have the
  same KPI block, holdings and cash. Two things differ. A year's `period_twr` runs from the
  previous year's 12-31, and its activity covers the whole year: every FLOW line is the sum of
  its months, while balances are never summed. The current month and year are live, as at
  today.
- **Where a close comes from.** A past close whose D is before `live_from` is the store's.
  Every other one is computed live with the same functions, so a correction dated last month
  changes last month's row at once. A store row inside the stale range is served with
  `stale: true` and reads 「更新前版本」.
- **暫定 (provisional)** marks a past period whose D is after `final_through`: a market quote job
  due on D has not ended `ok`. The note reads 「月底收盤價尚未確定：<市場>報價工作未完成」
  (「年底…」 for a year). It depends on the price data, never on the store. After 4 days the
  close settles anyway, with `notes.unconfirmed_closes`.
- **Restatements.**
  - **已重述:** when a run changes a published close's KPI, it writes a `settle_restatement` row.
    The row records the field's zh label (總市值 / 累計總報酬 / 累計報酬率 / 累計 XIRR /
    當期報酬（TWR） / 證券市值 / 淨投入 / 淨資產 / `<ccy> 市值`), the unit, before → after, the
    notes before and after, the reason (e.g. 「計算規則更新」, or a dated correction) and the
    time. Restatement rows are never pruned, so the row's 已重述 mark always explains itself.
  - **上線重述 at go-live:** on a month's FIRST publish, its close is compared with the retired
    `portfolio_snapshots` freeze, but only when that freeze was written on the month's last day
    (the go-live month's row was never a close). Every difference is recorded with `cutover = 1`
    and the reason 「上線重述：月底定格值改為月底結算值」. The 23:50 freeze used the previous
    day's US close, and months before a ledger import reflect the empty ledger: a one-time
    restatement is expected.

  Activity lines and holdings are recomputed on every run but not restated.
- **Two defects found during the store work, fixed after v0.1.32** (ruling 2026-10-19:
  v0.1.32 kept its byte identity against v0.1.31, and each was fixed as its own change after
  the release; their former `xfail(strict=True)` tests in `tests/portfolio/test_engine_repro.py`
  are plain regression tests now):
  - **PERF-F2.** The trend's FX pair set held only the ledger's QUOTE currencies, so an XIRR-kind
    cash movement (REBATE / INTEREST_EXPENSE / BROKER_FEE) in a currency no instrument is quoted
    in made the WHOLE trend unavailable (net worth, B, B − A, 交易與融資成本, the benchmark),
    although its rate was stored and XIRR counted it. The set now has ONE owner,
    `portfolio/timeseries.py::trend_fx_currencies`: the quote currencies of
    `trend_symbols(bundle)` plus the currency of every movement the trend converts, by
    `returns.is_return_movement` (XIRR's own predicate). The live build and the settlement
    worker both read it. Only a rate genuinely missing on or before a flow's date still makes
    the trend unavailable (「帳本中有交易日期缺少匯率資料」); on such a ledger a past close
    computed live has no trend figure at all. The worker's placeholder widening is now defense
    in depth with no known production trigger; its test reaches it through v0.1.31's pair set.
    `tests/reference/perf_f2.py` owns the intended payload difference on the
    `unquoted_ccy_cash` corpus (the v0.1.31 reference files are never regenerated).
  - **The price-basis split edge.** The write window's bound was the TAIPEI date of the fetch,
    so every scheduled US fetch of a split's last pre-split session (16:30 New York, already
    the ex-date in Taipei) stored its as-traded close × the ratio until `history_daily`
    re-fetched it about 20 hours later. The bound is now the fetch's date on the exchange's
    calendar (owner ruling 2026-10-20; `data-and-pricing.md` → the stored price basis).
