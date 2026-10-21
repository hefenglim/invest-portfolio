# Delisted symbols: stop tracking, label, and value the holding days with no price history

| Field | Value |
| --- | --- |
| Status | DRAFT for accountant review, then owner approval |
| Date | 2026-10-22 |
| Base | `fix/post-v0132` (v0.1.32 + the post-release fixes) |
| Owner rulings | 2026-10-22 (multiple choice, recorded below) |

## 1. The problem, measured

Several US symbols on the prod ledger have **no price history from any provider**: they delisted
years ago (SPAC mergers renamed into a successor ticker, a take-private) and the providers dropped
their history. The real list, with holding dates, is in the git-ignored owner note
`docs/human_noted/delisted-symbols-2026-10-22.md` (this repository is public: no real trades,
tickers or amounts here). Read from a local prod snapshot (2026-10-13, read-only), every one of
those positions was **closed by the ledger before the symbol delisted**: either a sale at a real
price, or an EXCHANGE into a successor ticker that has prices.

Realized P&L, cost basis and XIRR are therefore complete and correct. What is missing is the
**daily valuation while the position was held**: `timeseries.daily_value_series` finds no stored
close on or before the day, leaves the holding out and marks the day `incomplete`. So on prod the
trend, B, B − A, TWR, the benchmark and the month closes are incomplete over a multi-year range of
holding days.

Two side effects today:
- none of them is archived, so the quote jobs ask the providers for them every day;
- nothing on the watchlist says they are delisted.

## 2. Owner rulings (2026-10-22)

1. **Delisted symbols are marked 已下市, no longer quoted, and shown as delisted on the
   watchlist,** "as if it were not on the watchlist".
2. **After delisting (once that month is closed), the stock is no longer computed, as if the
   position were cleared.**
3. **The holding-period gap is valued at the ledger's last trade price, labelled 估計** (the owner
   first chose to accept the gap, then preferred this; recommended after comparing: accept the gap
   / monthly statement prices / ledger price / other providers).
4. Process: this spec → independent accountant review → owner approval → implementation, as its
   own change.

## 3. Design

### 3.1 Data: `instruments.delisted_on`

- A nullable DATE column, added by an additive boot migration (`data_ingestion/schema.py`).
  NULL = listed (every row today).
- It is an **input** of the settlement triggers by default (the `instruments` spec exempts only
  display columns and `archived`), so setting or clearing it marks history dirty from the symbol's
  first ledger day and the next run re-settles those days. That is intended: the valuation of
  those days changes.
- Setting it also sets `archived = 1` (the existing 停止追蹤, FU-D13): `shared/instrument_scope.
  TRACKED_SQL` then excludes the symbol from every quote, history, dividend, news, chip and
  fundamentals job, and the instrument list hides it behind the existing archived toggle.
  Clearing `delisted_on` leaves `archived` as it is (restoring tracking is the existing 恢復追蹤).

### 3.2 Door: 標記已下市

- On the instrument page, an archived-or-not row gets 「標記已下市」 with a date input. The
  default date is the symbol's last ledger exit (its last SELL that brought every account to zero,
  or the EXCHANGE / SPINOFF date that vacated it); the owner may change it.
- The confirm dialog states the consequences in plain words: quotes stop, the symbol shows
  「已下市（YYYY-MM-DD）」, history from its first ledger day is re-settled, and closes that change
  are marked 已重述.
- `PATCH /api/instruments/{symbol}` gains `delisted_on` (date or null). It is a mutating route with
  an action-log label (「標記已下市」 / 「取消已下市標記」).
- **Refusal:** an option contract (`asset_type = 'option'`) cannot be marked (options have their
  own expiry and close rows).

### 3.3 Label

- Instrument list / watchlist: a delisted row reads 「已下市（YYYY-MM-DD）」 in place of 持有 /
  觀察. The archived toggle still hides it by default.
- Drawer header: the same badge.

### 3.4 The valuation fallback: the ledger's last trade price (ONE owner)

A new pure function in `portfolio/` (e.g. `portfolio/price_fallback.py::ledger_price_on`) answers:
for a symbol with `delisted_on` set, valued on day `d`, when **no stored close exists on or before
`d`**, the price is the ledger's most recent ordinary trade price of that symbol on or before `d`:

- any account; `close_reason` rows excluded (they are not market prices);
- in the replay's order (an action before the day's trades; same-day trades by id);
- re-expressed for the splits in `(trade date, d]` by `portfolio/price_basis.price_in`, exactly
  as a carried-forward stored close is (`data-and-pricing.md` → the read divides);
- no trade on or before `d` (an opening only) → the opening's `original_cost_total / shares` on
  its `build_date` (the owner's "last buy price"), re-expressed the same way;
- the value it yields is **estimated**: every reader that uses it marks it.

It is used only when BOTH hold: the symbol is marked delisted, and the stored series has nothing
on or before `d`. A symbol with any stored close on or before `d` keeps the existing
carry-forward. A symbol that is not marked never gets a fallback — a temporarily missing price
stays missing (`data-and-pricing.md`: "Live price unobtainable → label clearly; never guess").

It mirrors the options rule (R2: an option is valued at its last ordinary trade price from the
ledger), so the app has one idea of "a price from the ledger" for instruments no provider quotes.

**Readers** (each must use the one function; none re-derives it):
1. the trend: `timeseries.daily_value_series` (live tail and the settle worker, which runs the
   same engine) — where `_entry_at_or_before` returns None today;
2. the point-in-time KPIs: the `PriceSource` of a close (`OnOrBefore`) and of the live build
   (`LatestStored`) — a held, marked symbol with no stored price gets the fallback;
3. the month / year close's holdings rows (`period_close`): price used, its date and its source.

### 3.5 After the delisting date (owner ruling 2)

`delisted_on` is the day the symbol stops existing as a quoted security. If the BOOK still holds
it on a day after `delisted_on` (no sale, exchange or write-off recorded), the ledger is
incomplete. Proposal (needs the owner's confirmation, §6 Q1): the position becomes 待釐清 with
「已下市但帳上仍持有：請登錄賣出、換股或註銷」, the day is `incomplete`, the holding's value is
left out (never a guessed disposal), and the drawer offers the remedy. None of today's marked
symbols reaches this branch (all were closed before delisting).

### 3.6 Labels of an estimated value

- `TrendPoint` gains `estimated: bool`, **serialized only when true** (so every payload of a
  ledger without a marked symbol stays byte-identical, including the v0.1.31 reference payloads
  and the golden payload). The trend tooltip adds 「含估計價（帳上成交價）」.
- `settled_day` stores the flag (a store schema change → `STORE_SCHEMA_VERSION` +1 → the calc
  version moves → full re-settle at first boot, as any `portfolio/` change already does).
- A close gains `notes.estimated_prices`: `[{symbol, price, priced_on, source: "ledger"}]`,
  shown as ⚠ 「估計：SYM 以 YYYY-MM-DD 帳上成交價估值」 on 月度成績. A close whose only defect was
  the missing price is no longer `incomplete`; it is estimated.
- A live holding row (only reachable via §3.5's refusal path or a marked symbol held today with no
  stored price) shows its price source 「帳上成交價（估計）」, like an option.
- B, B − A, TWR and the benchmark over estimated days are computed (not withheld), and the days
  carry the estimated mark. XIRR is unaffected (those positions are closed; XIRR reads flows).

### 3.7 What changes on prod

After the owner marks the symbols:
- quote attempts for them stop;
- the holding days become complete-but-estimated; the trend, B, TWR and the closes of those
  months change, and every close whose KPI changes gets a permanent 已重述 row whose reason names
  the instrument change (accountant MINOR #1's per-close rule);
- the realized P&L, the cost basis, XIRR, the tax package and every figure after the last such
  holding day are unchanged.

Known limit: a stock that moved a lot while held reads flat at its trade price on those days (one
of the symbols did); a SPAC before its merger usually trades near its trust value, so the estimate
is close there. The owner note quantifies it.

## 4. Tests

- The function: a stored close always wins; no stored close + marked → the ledger price; not
  marked → None; a split between the trade and `d` re-expressed; an opening-only position; a
  `close_reason` row ignored; same-day ordering.
- The trend: a marked symbol's days are valued and `estimated`, not `incomplete`; an unmarked
  symbol with no price stays `incomplete`; byte identity of every reference corpus (no marked
  symbol) and of the golden payload.
- The settle worker ≡ the live build on a corpus with a marked symbol (the prefix property).
- Closes: `notes.estimated_prices`, holdings rows' price source, KPIs as at D.
- §3.5: a marked symbol still held after `delisted_on` → 待釐清, `incomplete`, value omitted.
- The door: PATCH sets / clears; archived set; option refused; action-log label; the settle
  trigger marks the symbol's first ledger day dirty (and only an input change does).
- e2e: mark a symbol, see 「已下市（…）」, the confirm text, and the 估計 note on 月度成績.
- Stress audit: the oracle transcribes the fallback (it values from the fixture's own trades) and
  a phase-1 scenario op marks a no-history symbol; mutation check (remove the fallback → fails).

## 5. Docs

`data-and-pricing.md` (the fallback beside "never guess", and that only a marked symbol gets it),
`domain-ledger.md` (month closes: `estimated_prices`; §3.5), the accounting manual (zh + en) with a
verification anchor, the CHANGELOG, a whatsnew entry (instrument page → 標記已下市).

## 6. Open questions for the owner

- **Q1 (§3.5)** A marked symbol still held after its delisting date: 待釐清 and value omitted until
  the owner records the disposal (recommended) / value it at the ledger price as estimated /
  value it at 0.
- **Q2 (§3.1)** Marking delisted also archives (stops quotes) — recommended yes.
- **Q3 (§3.2)** The default date is the last ledger exit — recommended yes; the owner can change it.
