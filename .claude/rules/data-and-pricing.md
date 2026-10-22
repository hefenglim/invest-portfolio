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
- **FX cross rates are DERIVED through one base currency, never fetched (owner ruling
  2026-09-16, demo audit M1 option (b)).** `USD/TWD` and `USD/MYR` are the two pairs with a
  liquid direct market and are fetched; `MYR/TWD` is `USD/TWD ÷ USD/MYR`, written by
  `pricing/cross.py` right after every FX write (latest refresh and history backfill alike,
  derived over the whole stored history) with `source = "derived:USD"`. A day's row is WRITTEN
  only when it differs from the derivation of its legs — compared on the TEXT the write seam
  would store (`pricing/store.stored_rate_text`) and on the source: a day whose leg the run
  wrote or changed, a gap, or a row another writer left (a pre-M1 provider row, a seed's fixed
  series, a hand edit). An unchanged day keeps its row and its `fetched_at`, and a repeat run
  writes nothing (CR-14, 2026-10-04: every FX write had re-upserted and restamped ~1,800 rows,
  three quote jobs a day). Do not narrow the derivation to "the dates this run touched": the
  foreign rows sit on dates no run touches. Measured before the rule: the
  three independently fetched rates, all dated 2026-09-15 and none stale, disagreed by
  +0.0690% (USD/MYR × MYR/TWD = 31.856972 vs USD/TWD 31.834999), and a 4,000 MYR spot
  conversion moved the report-currency total by 40.88 TWD with no ledger change — the
  conversion path and the valuation path used different rates. The provider's `MYRTWD=X` is
  itself a USD cross sampled at another moment, so nothing real is lost. Consequences:
  **(1)** `freshness.fx_triangulation` is now a *guard*, not a warning — a non-zero gap means a
  row reached `fx_rates` by some path other than this module. It is judged on the three legs'
  latest COMMON date not after the valuation day (DEF-077, owner ruling ⑥ A, 2026-09-26) —
  never latest row against latest row, which reads a day's market move as a data error
  (J-01 measured −0.0938% with no bad row, USD/TWD a day ahead of USD/MYR). Legs on different
  latest dates carry `dates_differ` + `leg_dates` and the page says 「日期不同」; no common date
  within 30 days → `ok: null` + `reason` (「無法比較」), never `false`. The resolver reads each
  leg's history in the direction it resolved the rate; **(2)** a derived pair must
  never be handed to a provider (`pricing/cross.py::fetched_pairs` filters the worklist, so
  two writers can never own one row); **(3)** the freshness panel labels a derived row
  「推導」 so nobody reads it as a market quote; **(4)** the 6-dp cap applies to the derived
  value exactly as to a fetched one. Adding a fourth currency is a row in `CROSS_RATES`, not
  code. Rejected: base = TWD (derives away the one pair with a real market — and the pair the
  Moomoo FX pool's acquisition cost is booked in); read-time derivation in `shared/fx` (the
  stored row and the used rate would differ — two truths).
- **Average cost** — never stored as an authoritative rounded value. Store
  `total_cost` + `shares`; compute `average = total_cost / shares` on read
  (see `domain-ledger.md`). A correction never derives a total from an average: the 期初 edit
  door takes `total` or keeps the stored one, and refuses `avg` (CR-04, 2026-10-04 — the dialog
  sent the server's own 28-digit quotient back and a date-only edit stored
  99999.99999999999999999999999 for 100000).

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
  helper (`pricing/store.express_close`) and one window bound (`split_window_through`, below)
  so the two cannot drift. Idempotency, reversibility
  and order-independence then hold by construction: deleting a SPLIT restores every affected
  row **byte-identically**, which matters because `prices` is the only place this feature
  writes outside the ledgers and 重算 does not cover it.
- **Cap last, measured:** `cap(raw) × 20` stores `2.8340` where `cap(raw × 20)` stores
  `2.8333`; at `×3` it is `0.4251` vs `0.4250` — the sub-RM1 MY tick above.
- **All four OHLC columns are re-expressed** (W8, owner ruling 2026-08-27 — **supersedes
  D39b**). `close`, `open`, `high` and `low` each carry their own `*_raw` and are stored as
  `cap_4dp(raw × split_basis)`, so every one of them is restatable and reversible. D39b's
  objection was precisely that a factor applied to a column with no raw "could never be
  restated or reversed", and its own escape clause was "or add its own raw columns" — W8 took
  that clause. Until then a row could carry a post-split close beside pre-split highs and
  lows; harmless only because nothing read those columns, and a trap for the first
  candlestick chart drawn from this table.
- **`volume` is still never multiplied**, and for a STRONGER reason than the old OHLC one: it
  is a count, not a price. A 20-for-1 split multiplies the share count and divides the price,
  so giving `volume` the price factor would be actively **wrong**, not merely unrestatable.
  It gets no raw column and no factor.
- **The write multiplies; the read divides.** The write window is `(as_of, T]` —
  "which splits had the provider already folded in?" — and multiplies them back **out** to
  recover the as-traded value. `T` is the fetch's date on the EXCHANGE's calendar,
  `pricing/store.split_window_through(fetched_at, market)` with the zones in
  `shared/enums.MARKET_TIMEZONE` (owner ruling 2026-10-20), never the app clock's (Taipei)
  date: the US quote job fires at 16:30 New York, already the next day in Taipei, and a Taipei
  bound folded each split into its last pre-split session's as-traded close (the price-basis
  split edge, fixed after v0.1.32). The write seam and the reconcile both call that one helper.
  TW and MY rows stamped +08:00 (and naive stamps, which keep their own date) do not move; any
  other offset is converted, so a UTC-stamped row can move one day. The bound turns at the
  exchange's midnight, not its open, so two residuals are accepted, each healed by the next
  fetch: a pre-open fetch on the ex-date from a provider that has not restated yet, and a fetch
  before that midnight from a provider that restated early (for a Monday ex-date, the weekend
  `history_daily` runs). **Every restatement goes through ONE seam,
  `api/instrument_service.restate_price_basis`**, which also clears the symbols' derived
  `signal_history` / `signal_states` rows (W6 / AI-D27); its reconcile reads and writes in one
  `BEGIN IMMEDIATE` transaction when the caller holds none, so a concurrent quote write can
  never leave a close derived from the old raw beside the new one.
  `scripts/check_split_window.py` lists stored rows the window disagrees with (US and TW / MY
  apart, SUSPECT rows marked) and restates them with `--apply` after a verified backup. The read (`portfolio/price_basis.price_in`) window is
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
  - **A trade moves its SETTLED 價金, never the raw quantity × price** (owner 2026-09-30, method
    left to the developer: 「依券商實務」). `shared/money.py::settled_notional(qty, price, ccy)`:
    TWD drops everything below the dollar (TWSE computes an odd-lot 交割價金 per order and price
    with 元以下捨去, 2024-04-01; a board lot is always whole dollars), USD / MYR round the cent /
    sen half up. Every figure OF RECORD a trade moves reads it — the cash pool, the cost basis,
    realized proceeds, the XIRR and net-invested flows, the FX pool's foreign cash, the fee base,
    and every preview that mirrors them (manual door, drawer 試算) — so 0.5 AAPL at 191.23 is
    95.62 everywhere, never 95.615 in one place. A product already in the minor unit is returned
    AS IS (the identity short-circuit, as in the split-basis seams below), so whole-lot ledgers
    are byte-identical. A VALUATION (price × shares held), a projection or a tolerance check is
    not a settlement. `tests/portfolio/test_settled_notional.py` scans every quantity × price in
    the package; the stress-audit oracle transcribes the rule on its own (`oracle.settled`).

**Mechanics:**
- Persist Decimals as **TEXT** (canonical string) or **scaled integers**; one
  convention per column, documented. Do not mix. The canonical string is `money.to_db`
  (fixed-point), never `str(Decimal)`, which prints `5E-7` / `0E-24` for some values — and no
  export cell may print one either (`decimal_str` for a Decimal, `stored_decimal_str` for a
  stored TEXT). Owner 2026-09-30, after the llm-usage CSV passed a stored `5E-7` through;
  `tests/contract/test_exports_never_print_scientific_notation.py` builds every export and lists
  every bare `str()` in `export/`.
- All FX conversion goes through the single helper in `shared/`. No ad-hoc
  multiply-by-rate scattered across modules.
- Rounding is explicit (`Decimal.quantize` + stated rounding mode). Display formatting
  (decimals shown, thousands separators) is a presentation concern, decoupled from
  storage.

## SQLite schema conventions

Canonical tables (names indicative; finalize in the spec phase):

- `accounts` — first-class entity: broker, settlement currency, funding currency,
  fee-rule-set ref, dividend model (see `domain-ledger.md`).
- `instruments` — symbol, market (`US` / `TW` / `MY`), quote currency, sector, name,
  `asset_type` (`'stock'` | `'option'`, 2026-10-09: only `register.register_option_contract`
  writes `'option'`, from the OCC symbol's parse, and `upsert_instrument` never turns it back),
  and
  `aliases` (a JSON list of the other names a card may use — 大立光 for 3008 LARGAN; one
  writer, `store.set_instrument_aliases`; one name belongs to one instrument — item 8,
  2026-09-30).
- `transactions` — source of truth: account, instrument, side, quantity, price, fees,
  tax, trade date, and `close_reason` (2026-10-09: `expired` / `assigned` / `exercised` on a
  price-0 option closing row, NULL on every other row; v1's edit door keeps it — delete and
  re-enter to change it). **Append-only in spirit**; corrections are new rows or explicit
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
  withholding, net, reinvest shares + price (see dividend models in `domain-ledger.md`), and
  `ex_date` (an ex-date after `date` is ASKED about on every door — a warning the owner
  confirms, owner ruling ② 2026-10-05; CR-09 shipped it as a refusal). Every door that writes a dividend runs
  `validate_dividend` — the inbox confirm included — and the correction door edits `ex_date`
  (absent = keep, null = clear; a kept ex-date across a symbol change or a 配股's type / pay-date
  change asks first) (CR-09, 2026-10-04).
- `fx_conversions` — account, date, from_ccy, from_amount, to_ccy, to_amount (Q12).
- `opening_inventory` — account, instrument, shares, original avg cost, original cost
  total, **build date** (not a trade flow, but feeds XIRR).
- `prices` — instrument, date, close (+ any OHLC), source. Idempotent upsert on
  (instrument, date).
- `fx_rates` — base/quote (USD/TWD, USD/MYR, MYR/TWD…), date, rate, source. Idempotent.
- `insights` — cached LLM output (see `llm-insight.md`).

Separate **raw** stored data (transactions, prices, fx) from **computed** results
(holdings, P&L, returns). Computed values are derived on read by `portfolio/`, not
stored as the source of truth (cache them only if profiling shows a need). Profiling did show
a need for the history (v0.1.32), and the result is the settlement store below: a DERIVED
cache of settled days and closes, never a source of truth.

## The settlement store — derived, rebuildable (PERF, v0.1.32)

Spec `docs/spec/2026-10-18-settlement-store.md` (owner rulings PERF-D1..D10, approved
2026-10-18); owner `portfolio/settlement_store.py`. Measured before it: prod's dashboard took
16–64 s on the throttled host, because every page that shows history replayed the ledger once
per event day. The store keeps what does not change from day to day. **The ledgers stay the
only source of truth.** Dropping the store loses nothing, and a run rebuilds it.

**The tables** (spec §6; every money column is canonical Decimal TEXT, `money.to_db`):
- `settle_meta` — one row: the published `generation` (0 = never settled), `settled_through`,
  `held_through`, `first_day`, `calc_version`, `reporting_ccy`, the benchmark state, the ACTIVE
  run (`run_id`, `run_from`, `run_through`, the `run_dirty_from` it claimed) and
  `trigger_schema_version`;
- `settle_dirty` — one row: `dirty_from` (NULL = clean) and `seq`, which rises with every
  recorded change;
- `settle_change_log` / `settle_change_agg` — the change trail: one row per changed ledger or
  reference record, and one counter per symbol or pair for `prices` / `fx_rates`;
- `settle_run` / `settle_run_diff` — the run reports and every changed day of each run. The
  latest 400 are kept, pruned at a swap;
- `settled_day` — the trend point of each day;
- `settled_period`, `settled_period_holding` and `settled_period_cash` — the month and year
  closes, with the positions and the cash pools at the close;
- `settle_restatement` — every closed period that moved, and why. It is **never pruned**.

**What is settled, what stays live** (spec §5, §10):
- **From the store, for days before `live_from`:** the trend points (`total_value`,
  `net_invested`, `incomplete`, `net_worth`, which `/api/performance/twr` composes too), and the
  month and year closes ending before `live_from`.
- **From the store only under conditions:** the benchmark's stored state, used only when
  nothing is pending or claimed, no flow is dated after `settled_through`, and no corporate
  action on a benchmark storage key is.
- **Live on every build:** everything else — KPIs, holdings, subtotals, realized rows, XIRR,
  FX P&L, dividends, allocation, cash, the options block, freshness, the ex-dividend calendar
  and projection, B / B − A (B is the trend's LAST point, always live), and the alerts. That is
  one replay of the ledger cut at today — and one `iter_books` pass gives both the trend's books
  and today's book (`dashboard._replay_once`); a trend needing more than 64 books or ~8,000 held
  entries streams them and replays today's book apart. A served point's `pnl` (the tooltip's
  累計總損益（含匯兌）) is derived on serve (`_stored_point`, `with_trend_pnl`) from its
  `total_value` / `net_invested` / `incomplete`; it is never stored.

With the store clean, or dirty only inside the live window, `/api/dashboard` is
**byte-identical** to v0.1.31's code on the same database and clock, except the intended,
declared differences after v0.1.32 — PERF-F2 on the `unquoted_ccy_cash` corpus
(`tests/reference/perf_f2.py`) and the additive `TrendPoint.pnl` (`tests/reference/trend_pnl.py`).
That is checked against reference payloads generated by that code (`tests/reference/`), which are
never regenerated. The golden payload changed once, for the additive `pnl` (2026-10-22).

**The calc version** (PERF-S5) is `sha256(code digest + reporting currency +
STORE_SCHEMA_VERSION)`. The code digest covers every `.py` in `portfolio/`, `forex/`,
`pricing/`, `data_ingestion/` and `shared/`, with line endings normalised. It is computed once,
at import. A store built by other code, or in another reporting currency, is not served. So **a
release that touches any of those packages re-settles in full at its first boot.** The swap
stamps the version the child computed at its START, so a deploy during a run cannot stamp
new-formula rows over old ones.

**Invalidation: triggers, named `settle_trg_*`** (spec §7, `TRIGGER_PREFIX`):
- **The trigger tables:** the six ledgers (`transactions`, `dividends`, `opening_inventory`,
  `cash_movements`, `fx_conversions`, `corporate_actions`), plus `prices`, `fx_rates`,
  `instruments`, `accounts` and `account_market_rules`.
- **Kinds:** `AFTER INSERT`, `AFTER UPDATE … WHEN` and `AFTER DELETE` only, plus a
  `BEFORE INSERT` on the six ledgers that marks the row a `REPLACE` removes.
- **The `WHEN` clause** is generated from `PRAGMA table_info` minus each table's exclusion list
  (P8). So a column added to a ledger later is an input by default, and changes the trigger's
  text.
- **Both versions of a row:** an UPDATE marks from the earlier of the OLD and NEW day, and every
  symbol test reads the OLD and the NEW symbol.
- **A dividend's day** is its effective date (`DIVIDEND_EFFECTIVE_DAY_SQL`, the SQL twin of
  `Dividend.effective_date`).
- **The `prices` gate:** its trigger fires only for a symbol some ledger names (a
  `corporate_actions.to_symbol` included) or a benchmark storage key, and an unchanged restamp
  is silent.
- **The bound:** a change is recorded only when its day is ≤ the later of `settled_through`,
  `held_through` and the active run's `run_through`. A later day is in the live tail anyway.
- **What a recorded change does:** it lowers `dirty_from`, adds 1 to `seq`, and writes its trail
  row (or bumps its counter).
- **Not inputs:** fee rules (rows keep their own snapshot), alert rules, settings and
  `dividend_events`.
- **Every writer fires them:** the app, the import-batch undo, the moomoo merge, a script, the
  stress-audit harness writing SQLite directly. That is the reason for triggers rather than an
  application hook.

**Installed and verified at boot only, in one explicit transaction, after every migration.**
- `install_or_verify` compares each trigger's text in `sqlite_master` with its expected
  definition. It recreates only a missing or different one: an identical boot writes nothing and
  moves no `schema_version`.
- A recreated trigger, outside a calc-version change, on a store with a published generation,
  sets `dirty_from` to the first day, writes the trail line 「觸發器重建：<table>」, and makes the
  boot start a full run.
- `record_schema_version` runs **LAST** in the lifespan, after every boot DDL, and every claim
  re-verifies the set.
- **Every read** compares `trigger_schema_version` with `PRAGMA schema_version`. Any difference
  (a migration, a manual `DROP TRIGGER`) means the store is unverified, and it is not served
  until it is verified again.
- `PD_SETTLE=off` keeps the triggers, so switching it back on starts from a correct dirty state.
- **Never drop a settle table while its triggers exist:** every ledger write would then fail
  with "no such table". The documented reset (`drop_store`) drops the triggers first.

**The change trail and its actors.** A trail row names the table, the operation, the record
key, the old and new account and symbol, the old and new affected day, the import batch and
the write time. It is PENDING, CLAIMED by exactly one run, or SETTLED by one. Rows are tagged,
never moved, so a run that fails puts them back and no change is lost. Its `actor` is stamped
by a TEMP trigger the app installs (`shared/db.install_actor`, plan P14):
- `user:<login>` — a mutating request; shown as the login, or 「你」 to that user;
- `user:guest` — guest mode, shown as 「訪客」;
- `job:<id>` — a scheduled or manual job, shown as 排程;
- `system:boot` — the lifespan's own writes, shown as 系統（啟動）;
- NULL — anything the app did not open (the sqlite3 CLI, a raw-`sqlite3` script), shown as
  「外部程式／腳本」.

The main triggers call no application function, so external writers never fail.

**Generations and the swap** (spec §6, §9.4). A run writes generation `G + 1`, copying the rows
before its start day from `G`, and publishes in ONE transaction that moves `settle_meta` to
`G + 1` and deletes `G`. The swap is guarded by `settle_meta.run_id`, so an orphan child that
outlived a restart can never publish. Before it publishes, the swap asserts exactly one day
row per day in `[first_day, settled_through]`, and one close per month and year end from the
month of the ledger's first row. With `N` = the `dirty_from` found at the swap:
- **`N` empty → full publish:** `settled_through = held_through = run_through`.
- **`range_from < N ≤ run_through` → partial publish:** `settled_through = N − 1`. The held days
  `[N, previous held_through]` keep the previous generation's rows, and a chained run starts
  at `N`.
- **`N ≤ range_from` → `superseded`:** nothing is published.

`settled_through` can move BACKWARDS: a partial publish sets it to `N − 1` (and a self-check
with pending changes publishes `min(S, Dd − 1)`). `held_through` never does, and readers and the
trigger bound use it, so no served figure is affected — the panel's 已結算至 can read an earlier
day until the chained run finishes. Orphan generations of a dead run are deleted at boot and at
every claim.

**`live_from` and the stale range** (spec §10). `S` is `settled_through` and `H` is
`held_through`. `Dd` is the earlier of `dirty_from` and the `dirty_from` the active run CLAIMED
(`run_dirty_from`): a change a run has claimed stays dirty until its swap, but the rest of its
range does not — a full run on a clean store labels nothing stale (final senior review,
2026-10-20). The stored benchmark still waits for anything pending OR claimed (`claimed_day`). `W` is the 1st of the previous month. Then
**`live_from = min(H + 1, max(Dd, W))`**:
- **Clean:** every day before `S + 1` is from the store.
- **Dirty inside the window (`Dd ≥ W`):** every day from `Dd` is live, and every point is
  correct.
- **Dirty before the window (`Dd < W`):** the days `[Dd, live_from)` are the PREVIOUS
  generation's, the labelled stale range (PERF-D3). `/api/settlement/status` reports
  `stale_from` / `stale_to`, each stale close row carries `stale: true` and reads 「更新前版本」,
  and the chip and banner offer a run.

While history is stale, the drawdown alert is skipped with a note, an insight evaluation over
the range returns `pending_data`, and a period TWR across the join reads 待結算. The dashboard
payload gains no field.

The store is **usable** only when `generation > 0`, the calc version and the reporting currency
match, and the trigger set is verified. Otherwise, or with `PD_SETTLE=off`, every day is live:
v0.1.31's behaviour on the new engine, slower, never stale and never missing. A stored
placeholder day (a day a flow had no on-or-before rate) is served live
whenever the live series is available.

**Run lifecycle** (`settle_run.status`, spec §9.4 / §9.6). The parent
(`api/settlement_service.py`) creates the run's `job_runs` row (`job_id = 'settlement'`) and its
report as `preparing` (準備中) BEFORE it spawns the child. The child takes `settlement.lock`
first, then claims (`running`, 進行中). Each run ends in exactly one of these states:

| Status | zh | Meaning | `job_runs.status` |
| --- | --- | --- | --- |
| `published` | 完成 | every row of the range published, completeness asserted | `ok` |
| `partial` | 部分完成，接續處理 | published through `N − 1`; a chained run continues | `ok` |
| `superseded` | 已併入下一輪 | nothing publishable; the next run takes it over | `ok` |
| `cancelled` | 已取消 | terminated and waited for; the previous version stays | `skipped` |
| `failed` | 失敗 | an exception in a phase; the previous generation stays | `error` (`failed:<class>`) |
| `interrupted` | 中斷，已續跑 / 結算中斷（未續跑） | restart, deploy or crash; boot resumes the range unless it settles in full | `error` |

A self-check that found a difference is `job_runs` `partial`, reason `selfcheck_diff`. Every
end other than a publish runs one transaction: it puts the claimed changes back, sets
`dirty_from` to the earlier of itself and the dirty day the run CLAIMED (`run_dirty_from`; the
whole range only before anything was ever published), clears the run fields and deletes the
orphan generation. A cancelled full run on a clean store leaves it clean. **Liveness is the OS lock, never the heartbeat.** A status read probes the
lock, and a silent heartbeat only reads 「進行緩慢」 after 60 s. Boot ends every `preparing` /
`running` report whose lock is free as 中斷（未續跑）, together with its `job_runs` row. It first
terminates an orphan child that still holds the lock.

**What starts a run** (spec §9.1):
- **Boot:** a full run when the store is empty (and the ledger is not), when the calc version or
  the currency differs, or when a trigger was recreated. A resume after an interruption. Nothing
  on an identical boot.
- **The owner:** the prompt after an old edit, which runs from `dirty_from`; the panel's
  重新結算, a full run. Every successful write re-arms the prompt except the settlement's own
  controls (`POST /api/settlement/run`, `/cancel`): a cancel that puts the claimed dirty day
  back never asks again.
- **A chained run** follows only what the ended run itself left (PERF-D2, review 2026-10-22): a
  partial publish's hand-over, a superseded run's put-back claim, or a change the swap found
  committed while the run was active (`SwapResult.queued_from` → `report.state.chained_from`). A
  change committed AFTER the swap — while the waiter has not finalised yet and the status already
  reads idle — is nobody's follow-up: before the live window it takes the prompt, inside it it is
  live and waits for the nightly run.
- **The after-job hook:** after a job whose own changes reach before the live window.
- **`settlement_nightly` (06:40 Taipei):** settles what is dirty, then extends `settled_through`
  up to `final_through`. That is the day before the first day on which a due market quote job
  did not end `ok` (or yesterday), judged from `job_runs` and the schedule, never from the store.
  A day older than 4 days qualifies regardless, its close marked in `notes.unconfirmed_closes`.
- **`settlement_selfcheck` (Sundays 04:30):** a full recompute compared row by row with the
  published generation.

One run at a time per instance, and one heavy child at a time across the settlement and the job
workers (`architecture.md` → `shared/child_runner.py`).

**A GET never writes the store** (DEF-065). Triggers are created at boot only, and the actor's
TEMP trigger never exists on a GET or read-only-POST connection. The store is written only by a
run, at boot, or by a mutating request (through the triggers). `GET /api/settlement/status`
reads; it never starts, resumes or ends a run.

**The process flags** (`shared/flags.py`, read from `os.environ` on every call; unset = the prod
default; any other value stops the boot):

| Flag | Values | Prod | Meaning |
| --- | --- | --- | --- |
| `PD_SETTLE` | `on` / `off` | `on` | `off` is the kill switch: no run starts and every read is a full live build |
| `PD_SETTLE_MODE` | `child` / `inline` | `child` | where a run executes; `inline` is synchronous (hermetic tests, Windows dev) |
| `PD_JOB_MODE` | `child` / `inline` | `child` | where a scheduled job executes (`architecture.md` → scheduler/) |
| `PD_PREWARM` | `1` / `0` | `1` | pre-warm today's dashboard and alerts after a run and 3 s after the last write window |
| `PD_STATIC_IMMUTABLE` | `1` / `0` | `1` **on prod only** | serve a `?v=`-stamped asset `immutable` for a year. Unset means `0` everywhere. The demo never sets it: it runs WIP commits under an unchanged version, and an immutable copy would pin a browser to old JS |

Unit tests run `on` / `inline` / `inline` / `0`. The e2e servers run `PD_SETTLE=off` unless a
flow opts in with `flow_server(settle=True)`.

**`portfolio_snapshots` is no longer written** (plan P11). The 23:50 月度快照 job
`snapshot_monthly` is retired (`scheduler/jobs.py::RETIRED_JOB_IDS`), and its runner and its
boot DDL are gone, so a fresh database never has the table. An existing database keeps it as
the record of the old frozen values. The first run after go-live reads it once
(`settlement_store.cutover_snapshots`) and records each month whose freeze differs from the new
month close as a cutover restatement (上線重述, `domain-ledger.md` → month and year closes).
Nothing else reads it.

**Restore after a crash: delete `-wal` / `-shm`.** The database runs in WAL mode. On a clean
stop the lifespan closes the result cache (`result_cache.shutdown()`: it waits up to 15 s for a
running pre-warm, then closes its read-only connections) and then checkpoints
(`wal_checkpoint(TRUNCATE)`), so the folder holds ONE file. After a crash (an OOM kill, a power
loss), or a stop whose log says the checkpoint could not complete, `<db>-wal` and `<db>-shm` may
remain beside it. The `-wal` holds COMMITTED changes: fold it back before a backup by copy, and
delete both only when a restore replaces the file — otherwise SQLite replays the stale WAL into
the restored file. The store needs no restore of its own: boot settles in full when it is absent or does not
match the code. The procedure is in `docs/DEPLOYMENT.md`.

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
- **An option contract is never quoted and never has a `prices` row** (options spec R2 / §5,
  2026-10-09). It is valued at its last ordinary trade price from the ledger (computed on read
  in `portfolio/options.py::last_trade_prices`), outside `price_reads` and the freshness report.
  `shared/instrument_scope.py::TRACKED_SQL` reads `asset_type = 'stock'`, so no quote, history,
  dividend, news, chip or fundamentals job ever asks a provider for a contract, and every
  one-symbol bypass (quick-add, restore, gap backfill, `refresh_instrument_quote`, the Alpha
  Vantage held universe, held-only news) refuses one at entry (「選擇權合約由交易登錄建立」).
- **Exactly one price row is typed by a human: the SPINOFF child's seed** (D48b; DEF-040,
  owner ruling 2026-09-24, R4 2026-09-25). A spinoff creates a holding no provider has quoted
  yet, so the corporate-action form takes the child's opening price off the statement.
  `pricing/seed.py` is its only writer and remover (`pricing/` still owns every write to
  `prices`; the `api` layer only binds it). Three rules: **(1) a seed is written ONLY into an
  EMPTY `(instrument, as_of_date)` slot** — never over a provider quote or another seed; a
  refused write is reported (`child_price_skipped`), never silent. R3 wrote through
  `upsert_prices`' `ON CONFLICT` and so replaced a provider quote with no copy kept, and the
  delete then removed the replacement: the quote was gone while the API answered `restored`.
  **(2)** `source = "manual"` is RESERVED for seeds (no provider may use it) and the stamp is
  `fetched_at` = the action day 00:00; **(3)** each save records what it wrote
  (`corporate_actions.child_seed_json`), and deleting / re-dating the action takes back exactly
  that row and only while it is intact — a row a real quote has since replaced is the market's
  number and stays, with the reason in the confirm dialog and the toast. The provenance rule
  above is what makes (3) decidable from the row.

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
