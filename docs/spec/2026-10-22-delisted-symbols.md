# Delisted symbols: stop tracking, label, and value the holding days with no price history

| Field | Value |
| --- | --- |
| Status | REVISED after the accountant review (READY-WITH-FIXES, 2026-10-22); awaiting the owner's answers to §7 |
| Date | 2026-10-22 |
| Base | `fix/post-v0132` (v0.1.32 + the post-release fixes) |
| Owner rulings | 2026-10-22 (multiple choice, recorded in §2) |

## 1. The problem, measured

Several US symbols on the prod ledger have **no price history under their own ticker**: they
delisted years ago (SPAC mergers renamed into a successor ticker, a take-private) and the
providers dropped their history. The real list, with holding dates and the provider check below,
is in the git-ignored owner note `docs/human_noted/delisted-symbols-2026-10-22.md` (this
repository is public: no real trades, tickers or amounts here). Read from a local prod snapshot
(2026-10-13, read-only), every one of those positions was **closed by the ledger before the symbol
delisted**: either a sale at a real price, or an EXCHANGE into a successor ticker.

Realized P&L, cost basis and XIRR are complete and correct. What is missing is the **daily
valuation while the position was held**: `timeseries.daily_value_series` finds no stored close on
or before the day, leaves the holding out and marks the day `incomplete`. On prod the trend, B,
B − A, TWR, the benchmark and the month closes are incomplete over a multi-year range of holding
days — and TWR is worse than incomplete: `twr_index` carries the incomplete stretch flat and bridges
it in one step against the last complete day, i.e. against a base that excludes the unpriced
position, so the symbol's whole gain or loss is divided by the wrong base (accountant review M2).

**Provider check (2026-10-22, read-only):** for the positions that left by a 1:1 EXCHANGE, the
successor ticker's provider history DOES cover the predecessor's holding days (the SPAC was the
surviving issuer and only renamed); for the positions that were sold, no provider has any history.
So some symbols can be valued from real market closes, and the rest need an estimate.

Two side effects today: none of them is archived, so the quote jobs ask the providers for them every
day; nothing on the watchlist says they are delisted.

## 2. Owner rulings (2026-10-22)

1. Delisted symbols are marked 已下市, are no longer quoted, and are labelled on the watchlist.
2. After delisting the stock is "no longer computed, as if the position were cleared".
3. The holding days with no price history are valued at the ledger's last trade price, labelled
   估計 (chosen over: accepting the gap; typed month-end statement prices; other providers).
4. Process: this spec → independent accountant review (done: READY-WITH-FIXES, folded below) →
   owner answers §7 → implementation as its own change.

## 3. Design

### 3.1 Data: `instruments.delisted_on`, and the mark stops tracking by itself

- A nullable DATE column, added by an additive boot migration (`data_ingestion/schema.py`).
  NULL = listed. It is an **input** of the settlement triggers by default (the `instruments` spec
  exempts only display columns and `archived`), so setting or clearing it re-settles from the
  symbol's first ledger day. The `instruments` trail row records the changed input
  (`delisted_on` old → new) so the restatement reason can name it (§3.8).
- **The mark itself stops tracking; it never writes `archived`** (review B1). DEF-064 keeps
  持有 ⇒ 未封存 from both sides (the archive door refuses a held symbol; every booking un-archives),
  so coupling the mark to `archived` would be silently undone by the next booking.
  `shared/instrument_scope.TRACKED_SQL` becomes
  `COALESCE(archived,0)=0 AND delisted_on IS NULL AND asset_type='stock'`, and every one-symbol
  bypass refuses a marked symbol with 「已下市標的不再取得報價」 — quick-add, 恢復追蹤 and its gap
  backfill, `refresh_instrument_quote`, the Alpha Vantage held universe, held-only news (the same
  list the options spec refuses for contracts). 封存 stays the owner's separate choice.
- Before marking, the dialog offers the existing gap backfill once across the configured chain
  (FinMind for TW), because the mark stops every later fetch (review m8).

### 3.2 Door: 標記已下市 / 取消已下市標記

- `PUT /api/instruments/{symbol}` gains `delisted_on` (date or null); action-log labels 「標記已下市」
  / 「取消已下市標記」. An option contract is refused (options have their own expiry rows).
- **A date after today is refused** (review M3): only the past can be valued by the fallback, so
  no figure as at today can contain an estimate.
- **Default date: 帳上最後出場日** — offered only when the symbol is flat today and was held: the
  EXCHANGE that vacated it, or the SELL that emptied every account (a SPINOFF never vacates its
  source). No default when it is still held or was never held. The dialog says this is the
  ledger's exit, not necessarily the exchange's delisting date.
- **The door asks (an ack) when the ledger still holds the symbol after the chosen date**, naming
  the first such day and the shares (§3.5).
- The confirm text says: quotes stop; the symbol shows 「已下市（YYYY-MM-DD）」; the history from its
  first ledger day is re-settled and the closes that change are marked 已重述. On confirm the
  re-settlement follows PERF-D2 (prompt first).
- Clearing the mark re-settles the same range with its own reason (§3.8); tracking resumes unless
  the symbol is 封存.

### 3.3 Labels on lists

- Instrument list / watchlist: a marked, flat symbol reads 「已下市（YYYY-MM-DD）」 and is hidden by
  default with the archived rows (the toggle shows `archived OR delisted`).
- **A marked symbol still held after its date is never hidden**: 「已下市・帳上仍持有（待釐清）」
  (§3.5).
- Drawer header: the same badge.

### 3.4 The price of a marked symbol on a past day d (ONE owner)

A new pure module, `portfolio/price_fallback.py`, answers for a marked symbol on a past day `d`
(never for an unmarked symbol — a temporarily missing price stays missing, `data-and-pricing.md`
"never guess"; never for today — §3.5 governs a held marked symbol). It takes the **most recent
observation on or before `d`** among:

1. the symbol's own stored close (as today's carry-forward);
2. **the successor's stored close × the exchange ratio**, for days before an EXCHANGE out of the
   marked symbol, when the successor's provider history covers them (§7 Q4; review M6). The
   dialog then offers a one-off backfill of the successor from the predecessor's first ledger day.
   The rows go through the ordinary write seam under the successor's own symbol, so the split
   basis applies (a later split of the successor that the ledger records is folded out; one it
   does not record would show as a ratio mismatch — the dialog compares the successor-derived
   close with the predecessor's own trade prices on the trade days and warns when they differ by
   more than 25 %);
3. the symbol's own ordinary trade price (any account; `close_reason` rows excluded) or DRIP
   reinvest price — a real transaction, dated its day;

and, only when none exists, 4. the opening's cost per share (`original_cost_total / shares`, the
latest `build_date` ≤ `d`) — a COST, not a price (review m2).

Every observation is re-expressed into `d`'s share terms by `price_basis.price_in` for the splits in
`(observed on, d]`, exactly like a carried stored close. A position that arrived by EXCHANGE with no
own trade takes the source's last observation on or before the action date × the ratio, dated the
source's day (review m1, the options R1-M2 rule). Stored rows of a marked symbol dated AFTER
`delisted_on` are ignored and listed as suspect (a reused ticker; review m7).

**Basis and label of the value:**

| Observation used | Basis label | `estimated` |
| --- | --- | --- |
| own stored close or successor close, ≤ 4 days old | `market` / `successor` (「承接代號合併前報價」) | no (successor named in notes) |
| own stored close or successor close, older | as above | yes |
| own trade / reinvest price | `ledger` (「帳上成交價」) | yes |
| opening cost per share | `opening_cost` (「期初成本」) | yes |

The fallback is never written to `prices` and never appears in freshness (review m4).

**Readers**, each through the one module: the trend (`timeseries.daily_value_series`, live tail and
settle worker alike) and a close's point-in-time prices (`OnOrBefore` for a past D). `LatestStored`
never returns it: no figure as at today contains an estimate (review M3, with a test).

### 3.5 A marked symbol still held after its date (owner ruling 2)

"As if cleared" is already true for every position a ledger row closed — all of prod. For a
position still held after `delisted_on` the ledger is incomplete. Proposed (§7 Q1): a
**valuation-layer** 待釐清 (never a replay refusal, so 重算 and the tax package are untouched):
`pending_positions` kind `delisted_held` and a 檢查帳本 finding, the holding's value omitted, the day
`incomplete`, XIRR and B withheld with the reason named — and the drawer offers the remedies that
match what the statement shows:

- **sale / cash-out** (take-private, SPAC redemption, liquidation, ETF liquidation): a SELL dated by
  the statement, with fee and tax **as supplied** — pre-filled 0, editable — never the engine's
  (a TW tender offer bears 證交稅; a 企併法 cash merger is exempt) (review M4);
- **ticker change / OTC continuation / stock merger**: an EXCHANGE at the ratio;
- **write-off** (bankruptcy, lapsed rights): a new stock close row, `close_reason = 'worthless'`
  (§7 Q5): price 0, fee 0, tax 0, quantity the whole position, dated by the statement (it decides
  the realized loss's tax year), never opening a lot — the options §4.4 rules. Today a price-0
  stock SELL and a `close_reason` on a stock are both refused, so without it a worthless holding
  stays 待釐清 forever.

### 3.6 Labels of an estimated value (review M1, m4)

- `TrendPoint.estimated: bool`, serialized only when true; `TrendSeries.estimated_ranges`
  `[{symbol, from, to, basis}]`, serialized only when non-empty — so every payload of a ledger
  without a marked symbol stays byte-identical (v0.1.31 references, golden). The tooltip names the
  symbol, the price, its date and its basis.
- **The exit day is estimated too**: the day a position last valued at an estimate is sold,
  exchanged or reduced, because the whole holding-period move lands there.
- A close carries `notes.estimated_prices` at D — 「估計（非市價）：SYM 以 YYYY-MM-DD 帳上成交價 10.00
  估值（距期末 176 天）」 — and `notes.estimated_in_period` `[{symbol, from, to}]`, the OR over
  `(previous end, D]` (the rule the worker already uses for `options_value_excluded`), shown as
  「當期報酬含估計期間：SYM YYYY-MM-DD～YYYY-MM-DD」. One note per estimate: an estimated price is
  never also in `missing_prices` or `stale_prices`.
- `/api/performance/twr` adds `estimated_note` whenever its window contains an estimated day
  (additive, like `options_note`).
- `settled_day` stores `estimated` (store schema version +1 → full re-settle at first boot, as
  any `portfolio/` change already does).
- **The drawdown alert excludes estimated days** from both its peak and its trough
  (`strategy/alerts.py::max_drawdown` skips `incomplete` today), with a test: marking must never
  send a push measured from an estimated peak (review M2b).
- A day with an estimated price but no FX rate stays `incomplete`; both flags may be set and
  incomplete governs (review m10).

### 3.7 What changes on prod (review M2)

**Unchanged:** realized P&L; the cost basis; today's 總市值, A, B, B − A and XIRR; the lifetime
benchmark comparison; the tax package; every close after the last exit month.

**Changed:** the trend and net-worth curves over the holding days; the closes of the holding months
and of each exit month (KPIs, 累計 XIRR — from 不計 to a figure — and 當期報酬); every cumulative TWR
window that spans the stretch (it will move, most likely a lot, and the new figure is the correct
one: the old bridge divided the position's move by a base that excluded it); the drawdown peak
(estimated days excluded). Expect about 6–7 permanent 已重述 rows per changed holding month plus the
exit month.

### 3.8 Restatement reason (review M5)

The reason of a close moved by a mark reads 「標記已下市：SYM（下市日 YYYY-MM-DD），持有期間改以帳上成交價／
承接代號報價估值」, and by an un-mark 「取消已下市標記：SYM」. With the per-close rule of accountant MINOR
#1 (`fix/accountant-minors`), a run that claimed several marks names each one whose symbol the
close's period held. The trail row's affected day is the symbol's first ledger day; the reason does
not print it as if it were an old dated correction.

### 3.9 Markets and instruments

- TW / MY: the same rule. A TW share that delisted but sits in 集保 with no market is §3.5 plus the
  write-off row. A TW ETF liquidation payout is a disposal at NAV with supplied fee and tax.
- Options: refusing to mark a contract is right; a contract whose underlying is marked keeps R2; the
  underlying gets no fallback, so its obligation figures stay missing rather than estimated (m9).
- Multi-account: the most recent trade across accounts is used; internal transfers booked as trades
  at a book price would distort it — the owner note says whether any exist.

## 4. Tests

- `price_fallback`: the ladder (market close, successor × ratio, own trade, reinvest price, opening
  cost), most-recent-observation, the 4-day carry → estimated, splits re-expressed, an
  EXCHANGE-arrival without a trade, `close_reason` ignored, rows after `delisted_on` ignored, an
  unmarked symbol → None, today → never.
- The trend: marked days valued and `estimated` (not `incomplete`), the exit day estimated, ranges,
  unmarked unchanged; byte identity of every reference corpus and the golden payload.
- Settle worker ≡ live build with a marked symbol (prefix property).
- Closes: `estimated_prices`, `estimated_in_period`, basis per holding row, KPIs as at D, 累計 XIRR,
  當期報酬; the TWR note; the drawdown alert never fires from an estimated peak.
- §3.5: still held after the date → `delisted_held`, value omitted, XIRR/B withheld, strict replay
  (重算, tax package) unchanged; the three remedies (supplied fee/tax; EXCHANGE; the write-off row
  and its refusals: never opens a lot, fee/tax 0).
- The door: set / clear, future date refused, default date rules, the held-after ack, option
  refused, the trail row's changed input, TRACKED_SQL and every bypass refusal, the list toggle.
- e2e: mark, see the badge, the confirm text, the 估計 notes on 月度成績 and the tooltip.
- Stress audit: the oracle transcribes the ladder from the fixture's own data; a phase-1 op marks a
  no-history symbol; a mutation (remove the fallback) fails.

## 5. Docs

`data-and-pricing.md` (the ladder beside "never guess"; only a marked symbol; never today),
`domain-ledger.md` (closes: the two notes; §3.5; the write-off close row), `architecture.md`
(TRACKED_SQL, `price_fallback`), the accounting manual (zh + en) with a verification anchor, the
CHANGELOG, a whatsnew entry.

## 6. Out of scope

Typing month-end statement prices; a provider other than the configured chain; re-keying a reused
ticker (flagged, not fixed).

## 7. Questions for the owner

- **Q1** A marked symbol still held after its date: 待釐清, value omitted, until the statement's
  outcome is recorded (recommended) / keep valuing at the last trade price, 估計 / value it at 0.
- **Q2** How quotes stop: the 已下市 mark itself stops them, 封存 stays separate (recommended) / also
  set 封存 (undone by the next booking) / keep quoting.
- **Q3** Default date: the ledger's last exit, editable; future dates refused; an ack when the
  ledger holds the symbol after it (recommended) / no default / today.
- **Q4** Successor pre-merger closes: use them where the provider has them, ranked above the ledger
  price and labelled (recommended — the check found them for every EXCHANGE exit) / ledger price only.
- **Q5** Build the stock write-off close row (`worthless`) in this change: yes (recommended; Q1's
  remedy needs it) / later.
