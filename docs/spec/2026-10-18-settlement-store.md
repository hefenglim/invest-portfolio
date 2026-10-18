# Spec — Settlement store, result cache and the performance program (PERF)

**Status:** DRAFT 2026-10-18 — owner rulings PERF-D1..D10 confirmed in chat on 2026-10-18;
accountant review (§22) done and folded — three reviewers, a fold verification, a review of the
owner's run-report addition, a fresh-eyes final pass, a second fold verification and a final check
(no blocker, no money-formula regression; its text-level fixes folded); awaiting the
owner's answers to Q1–Q7 (§21) and acknowledgement. **No implementation before the owner
acknowledges this spec** (spec-first, `CLAUDE.md`).
**Branch:** `perf/settlement-store` (off `main` at `43730cd`, v0.1.31).
**Builds on:** `docs/reports/2026-10-16-prod-performance-audit.md` (the audit, P1–P7) and
`docs/reports/2026-10-18-performance-plan.md` (the owner's proposal, the measurements, the plan).
**Changes locked text:** `architecture.md` (the `flight.py` "keeps nothing" principle — PERF-D1),
`data-and-pricing.md` ("computed values are derived on read … cache them only if profiling shows a
need" — this spec is that profiling), the 月度成績 panel's meaning (PERF-D7). Recorded in
CHANGELOG with the release.

**Revision log**

| Date | Change |
| --- | --- |
| 2026-10-18 | First draft from the owner's settlement proposal, rulings PERF-D1..D10, three read-only code surveys (write paths; jobs/progress/UI; consumers/transport) and the 2026-10-18 measurements |
| 2026-10-18 | Self-review: the dirt is claimed by a run and compared against `run_through` (a change dated inside the range being published is never lost); strict validation reports instead of blocking (an acked 賣超 must not stop the history being settled); the drawer keeps its automatic first 試算; weekly self-check |
| 2026-10-18 | **Accountant review** — three independent reviewers (Opus 5.5, max effort), briefed as an investment accountant with 30 years of securities-trading bookkeeping, all READY-WITH-FIXES, folded in full. **A — number sources** (3 blockers): readers treat a claimed range as dirty (`Dd` = earlier of `dirty_from` and `run_from`), else the 指數對照 差額 mixes two ledgers during a run; every UPDATE marks from the earlier of OLD and NEW dates on every ledger, and "fires only when" became an exclusion list; the cache keys on `PRAGMA data_version` from a dedicated connection instead of a file signature; plus corporate-action symbols in the triggers, carry-forward seeds on every bounded load, stale history withheld from the drawdown alert and insight scoring, an order-independent exactness bound, the benchmark state voided by a benchmark-key action after `settled_through`, distinct names for the trend value and the KPI value, live past months when the store is unusable. **B — month / year closes** (1 blocker): a missing price understates 總市值 / 總報酬 (only XIRR is all-or-nothing) and every close now carries `notes` shown as ⚠ on 月度成績; which month rows come from the store follows §5/§10 (a correction last month shows at once); period activity uses the tax package's definitions, lists every cash kind, FX conversions and realized FX, cash per account × currency at period end, and states that its lines are not additive; period TWR = I(D) / I(previous period end) − 1; 暫定 before a month's US close arrives; 累計 labels; restatement explained; period tests (year ≡ December, Σ months ≡ year ≡ tax package, Π TWR); the price-basis split edge recorded as a risk to verify in WP2. **C — invalidation** (2 blockers): every non-publishing end restores the claimed range; cancel waits and never deletes a published generation; the swap is guarded by `run_id`, never writes `settle_dirty`, stamps the start-time calc version and asserts completeness; a run starts at `min(dirty_from, settled_through + 1)`; an OS lock marks a live child; dividends dirty from their effective date (no prompt on routine inbox confirmations); NULL-safe and `IS NOT`-guarded trigger SQL; trigger-set integrity check; calc version hashes the calculation sources; restatement log + 已重述; push on a failed automatic run; indexes for the trigger's symbol lookups; "system-handled" defined |
| 2026-10-18 | **Fold verification** (fourth reviewer, same brief; FOLDS NEED FIXES → folded): the tax-package tie-out stated sheet by sheet, DRIP with gross / withholding / net, Σ months ≡ year for flow lines only; `period_twr` = `None` (待結算) across a stale join; `xirr_reason` / `period_twr_reason` / `restated` on `/api/snapshots`; `dividends_receivable` excludes 配股; month-end cash fed with booked dividends only; §5 points at §10's three benchmark conditions; the prefix test asserts `dirty_from ≤` (never later); `total_market_value − options ≡ trend_total_value`; AFTER triggers only (an upsert's DO UPDATE never fires AFTER INSERT); instruments use the dividends' effective-date expression and exclude `target_set_at` / `board_status`; phase 1's validation moves into a pure `portfolio/` function so the worker never imports `fastapi`; boot terminates an orphan child that still holds its lock before cleaning up; dropping the store requires dropping its triggers first. One claim was checked and rejected: the tax package cannot list a refused payout, because it replays strictly and answers 422 on any refusal |
| 2026-10-18 | **Owner addition** (「點開展示所有重新結算的細節資訊完整化，並且完成結算後留下上一次的資訊，隨時點開即可查看前一次的結算資訊與結果」): the progress file carries every detail (changes being settled down to the record, validation, per-phase detail lines and rates, the estimate's basis, versions, what cancel does) (§9.5); every run leaves a persisted report — summary, range, versions, changes settled, validation, steps, result (history changes from a G vs G+1 diff, restated closes, state) — kept for the latest 400 runs (§9.6, new tables `settle_change_log`, `settle_run`); the panel shows the live run in full, opens on the last run's full report when idle, and browses every kept run (§14.2); `GET /api/settlement/runs`, `/runs/{id}` (§15) |
| 2026-10-18 | **Accountant review of the addition + final pass** (two more reviewers, same brief; both READY-WITH-FIXES, folded). **Run report** (3 blockers): a partial publish now settles only records dated before the new `dirty_from`, keeps G's rows beyond it (`held_through`) as the labelled stale range and the next comparison base, restates only the published range, and a run that can publish nothing ends `superseded`; the G-vs-G+1 result compares only days both generations hold in the same currency, lists newly settled days apart, treats value ↔ none and incomplete flips as their own category, and writes no diff or restatement across a currency change; the change trail is tagged (pending / claimed / settled), never moved or truncated, with price / FX counters per symbol / pair merged on put-back; plus: reports of runs that did not publish say so and the next run marks 「曾於 #411 嘗試」; phase 1 lists every 待釐清 record of the tolerant replay in non-blocking wording; `on_cancel` / failure sentences derived from the actual state; `started_by` and a per-record actor (a TEMP trigger on the application's mutating connections only; NULL = 外部程式／腳本); restatement rows carry label, unit and notes and are never pruned; liveness from the OS lock, not the heartbeat; tokens for old / new accounts; import batch grouping; the current item is the day and positions count; diff computed before the small swap transaction; calc version before → after; a quieter 上線重述 mark; job_runs status mapping per DEF-067. **Final pass** (no blocker, 3 majors): 暫定 and the nightly extension follow `final_through` (every market's quote job for the day ended `ok`), not the store; period closes carry `options_value_excluded` (R3) and `options_past_expiry`; a trigger recreated outside a calc-version change marks everything dirty, the trigger set is verified by `schema_version` on every read, and `PD_SETTLE=off` keeps the triggers; plus: only live points are composed with cash; the options OR uses stored days before `live_from`; key columns of `prices` / `fx_rates` fire; `missing_fx` / `stale_fx` notes; a per-row `stale` flag; the benchmark state is not published on a partial swap; the self-check publishes through the ordinary swap; the epilogue flag is applied to a copy (not moved into the loop); each FX consumer keeps its own pair set, and a suspected pre-existing trend defect is recorded as PERF-F2; period-test summation order; schema facts corrected (`opening_inventory` has no provenance columns, `fx_conversions` no note); FX P&L at D stated as not part of a close; the example range is 489 days |
| 2026-10-18 | **Fold verification 2** (same brief; FOLDS NEED FIXES → folded; no money formula regressed): `final_through` counts only quote jobs that were DUE on a day (a day with none due qualifies; a day older than 4 days qualifies with the missing close in its notes), so weekends, pre-go-live days and the scheduler-off demo never freeze settlement, and 暫定 gets its own note; boot recreates only a missing or different trigger (comparing `sqlite_master` first), so an ordinary boot re-settles nothing; the parent creates the run report as 準備中 before spawning and the child locks before claiming, so start-up is never shown as 中斷; interrupted runs read 已續跑 / 未續跑 from their state; **every changed day of a run is kept** (`settle_run_diff`, including new and dropped days) so the owner can reopen exactly which days changed and by how much; a partial publish keeps `held_through = max(previous, N − 1)`, puts records back per record only, and compares the last published day both generations hold; a calc-version change writes a diff and restatements (計算規則更新), only a currency change suppresses them, and the cutover restates only rows in the configured currency; the ledger check runs on the snapshot the run settles (phases 1 and 2 swapped) and failures cover every phase; `cancelled_by`; boot writes stamped `system:boot`; a self-check difference is `job_runs` `partial` / `selfcheck_diff`; the unused `series_flags` dropped; live-computed closes OR their live days for the options note; consistent `changes` URL; calc version before → after in the live panel |
| 2026-10-18 | **Final check** (same brief; no blocker, no money formula regressed; 3 majors, all text-level, folded): `final_through` is the day before the earliest non-qualifying day, "due on D" is judged in each job's own cron timezone, and a day passed by the 4-day rule lists its closes in a new `notes.unconfirmed_closes` (shown as 「收盤價未經確認」); the triggers also log changes on the held days of a partial publish (`held_through` in the bound); boot finalises any `preparing` / `running` report whose lock is free as 中斷（未續跑）, and `preparing` is in the status enum; plus a paged `/runs/{id}/diff` route, a recreated trigger starts an automatic full run with a 「觸發器重建」 trail line, the §7 opening sentence, 系統（啟動） in every actor list, before values in the progress example, and the `_quote_outcome` citation |

---

## 1. Summary

A full dashboard build costs 16–64 s on the prod VM and is triggered by 6+ routes on 8 pages,
2–3 times per drawer opening, and never kept. 84% of a build and 79% of its payload is history —
the daily trend, net worth and the benchmark — which a closed day never changes. This spec:

1. **Settles history** into a persisted, rebuildable **settlement store**: one row per day, plus
   month and year closes, for the configured reporting currency. Pages read it; only today (and
   at most the current and previous month after a recent change) is computed live.
2. **Never lets money figures go stale.** KPIs, holdings, realized P&L, XIRR, cash, FX P&L and the
   options block stay live — one ledger replay (0.05–0.12 s CPU locally).
3. **Detects every change that can alter the past** with SQLite triggers that record the earliest
   affected date, for every writer including scripts.
4. **Re-settles in a separate, low-priority child process** with a visible status chip on every
   page, a clickable detail panel (what it is doing, what remains, time spent, ETA), a prompt when
   an old record is edited, and generation swapping so an interruption never leaves a half-new
   series.
5. Adds the **result cache** (P1), **shared reads** (P2), **transport** fixes (P3), the **one-pass
   engine** (P5), **query fixes** (P6) and **process / VM tuning** (P7).

Measured (local, prod-shaped scratch ledger): a settled build 8.47 s → 0.19–0.26 s with a
byte-identical payload; the ledger doubled: 8.5 → 21.3 s today versus 0.19 → 0.27 s settled.

## 2. Owner rulings (2026-10-18) and spec decisions

**Rulings** — owner, in chat: 「我決定的三件事 1同意修改原則，2可跳提示後再開始，3改由結算資料提供。其餘決策按照建議」
plus the progress-detail requirement:

| # | Ruling |
| --- | --- |
| PERF-D1 | Approve the settlement store and the result cache; change `flight.py`'s "keeps nothing" principle; record it in CHANGELOG |
| PERF-D2 | When a user's edit of an old record needs a re-settlement: **prompt first**, start on confirmation |
| PERF-D3 | While a re-settlement is pending or running, history charts show the previous generation, labelled 更新中 / 待結算 |
| PERF-D4 | Heavy work runs in short-lived child processes (the settlement run, scheduled AI generation and provider fetches); APScheduler still triggers in-process |
| PERF-D5 | The dashboard wire format is unchanged; compression yes; splitting the trend into its own immutable route is deferred |
| PERF-D6 | A request that takes long shows 「仍在計算中」 (15 s) |
| PERF-D7 | 月度成績 is served from the settlement store; past months are restated from today's ledger; `portfolio_snapshots` is kept read-only for reference |
| PERF-D8 | VM: zram and prod-over-demo priority; stopping the demo while idle is not adopted |
| PERF-D9 | The two real-data scratch DBs stay until WP8's before/after comparison, then are deleted |
| PERF-D10 | The top-bar 重新結算 status is **clickable** and opens full detail: what it is doing now, what remains, time spent, estimated time remaining |

**Spec decisions** (derived; the owner acknowledges them with the spec — the ones that refine a
ruling are also listed as questions in §21):

| # | Decision | Why |
| --- | --- | --- |
| PERF-S1 | **Daily** rows are the unit of settlement; month and year closes are computed from the same state at each period end | the trend, net-worth and TWR charts are daily; invalidation per day re-settles only what changed (a back-dated buy on 2025-06-16 re-settled 485 days and left 1,694 untouched) |
| PERF-S2 | Settle through **yesterday** (`settled_through ≤ as_of − 1`); today is always live | today's prices move during the day; a day is closed once it is over |
| PERF-S3 | **Live window** = from the 1st of the previous month to today. A change dated inside it takes effect immediately (computed live, no prompt); only an older change needs a re-settlement (and, for a user's edit, the prompt) | the owner's "old record" (舊交易記錄); ≤ 62 live days cost well under a second; refines PERF-D2 → Q1 |
| PERF-S4 | **Generations**: a run writes generation G+1 beside G and switches in one transaction at the end | an interruption leaves G whole; never a mixed series |
| PERF-S5 | **Calc version** = sha256 over the source of every `.py` file in `portfolio_dash/portfolio`, `forex`, `pricing`, `data_ingestion` and `shared` (computed once at import) + the reporting currency + the store schema version; a mismatch means the store is not served and a run starts at boot | a release, a test-site WIP deploy, or an uncommitted edit that changes a calculation must never show old-formula history (a build commit would miss uncommitted edits — review C #8); a Python change outside those packages does not re-settle |
| PERF-S6 | **Who starts a run:** a user confirming the prompt or pressing 重新結算; automatically — at boot (empty store, calc-version change, interrupted run), after a scheduled job that dirtied the settled range, and nightly at 06:40 TPE; a change made while a run is active is chained | system-originated changes have nobody to prompt; refines PERF-D2 → Q2–Q4 |
| PERF-S7 | **Period-end valuation** is "as at the end of the period's last calendar day D": the ledger cut `valued_as_of(D)`, each symbol's latest stored close **on or before D** (re-expressed for splits in `(priced_on, D]`), each FX rate on or before D | identical to the trend's valuation of day D; see §11 for why the live dashboard function cannot simply be called with a past `now` |
| PERF-S8 | Only the **configured** reporting currency is settled | `Settings.reporting_currency` is fixed for the life of the process (`shared/config.py:26`, `lru_cache`); a change is a restart and changes the calc version |
| PERF-S9 | Run progress goes to a **sidecar JSON file**, not the database | progress writes every 0.5 s would churn the WAL, invalidate the result cache and contend for the write lock |
| PERF-S10 | `get_conn` commits **before** the response is sent (`Depends(get_conn, scope="function")`) | today a write left to the dependency's teardown commits after the response (FastAPI 0.136.3 request-scoped teardown; `tests/portfolio/test_dashboard_shared_build.py:172-201`), so the browser's next request can read the pre-write snapshot — the settlement status and the cache must see the write |
| PERF-S11 | No ETag on `/api/dashboard` in this release; gzip only | its body carries the request time (`as_of`, shown at `web/app.js:63`), so a 304 would show an older time; gzip already takes 439 KB to ~70 KB (≈0.1 s on the tunnel) → Q6 |
| PERF-S12 | The settlement run, the result cache and every heavy reader read inside **one read transaction**, opened explicitly (`BEGIN`) **after** the cache / flight key is taken — `_flight_key` returns `None` for a connection already in a transaction (`dashboard.py:687`) | a consistent snapshot (today `_build_dashboard` issues separate autocommit SELECTs); the run records the dirty sequence number from the same snapshot |
| PERF-S13 | Interactive heavy calls (AI door, LLM ping, manual quote refresh, quick-add backfill, manual news fetch) stay in the web process, importing their libraries lazily on first use | a child per click would add the library import (litellm 15 s locally) to every click; residency is measured in WP8 → Q7 |

## 3. Scope, non-goals, preserved invariants

**In:** the settlement store and run; the one-pass engine; the read path; month and year closes and
the 月度成績 change; the result cache and shared reads; read-only POSTs; transport (gzip, static
caching, deferred echarts); frontend (progress bar, dedupe, 15 s notice, status chip, detail panel,
prompt, banner, 重新結算); the medium routes; lazy imports and child workers; VM tuning; tests,
harness, docs.

**Non-goals:** new hardware; any change to a money-of-record formula, rounding or ordering; a new
dependency; DuckDB / pandas; splitting the dashboard payload (PERF-D5); stopping the demo while idle
(PERF-D8); new report pages beyond the 月度成績 月／年 toggle (Q5); the live window applies to the
settlement store only — validation replays (重算's check, correction-door guards) are untouched.

**Preserved (each has a test in §18):**

1. Every number on every page is produced by the same calculation code it is produced by today;
   with the store clean the dashboard payload is **byte-identical** to a full build.
2. The ledgers remain the only source of truth. The store is derived: dropping its tables (after
   dropping its triggers, §7) loses nothing; a run rebuilds it. "Original cost is never overwritten; all reports rebuild from the
   ledgers" is unchanged.
3. **A GET never writes** (DEF-065): the store is written only by the run (a child process), at
   boot, or by a mutating request; triggers are created at boot only.
4. Money stays `Decimal`, stored as canonical TEXT (`money.to_db`).
5. Layering (`architecture.md`): calculation in `portfolio/`; the run's orchestration and the
   composition in `api/`; jobs only trigger.
6. LLM rules unchanged (batch only, cached, never on page load).
7. 待釐清 / refusal semantics unchanged: a refused dividend, a 賣超, an unapplied action or a
   refused option close marks the same days incomplete and withholds the same figures.

## 4. The correctness basis — the prefix property

**Property.** For every day *d*, every settled figure for *d* is a function only of: ledger rows
that count by *d* (`counts_by(counts_on, d)` — a trade's `trade_date`, an opening's `build_date`, a
dividend's `effective_date`, a corporate action's `date`, FX conversions and cash movements by
`date`); price rows with `as_of_date ≤ d`; FX rows with `as_of_date ≤ d`; reference data
(instruments, accounts, account market rules); and the code.

**Why it holds today** (the code, not the new design):

- the trend values day *d* from `build_book(bundle.through(d))`, a date-prefix cut
  (`timeseries.py:360-361`; `shared/models/ledger.py:216-269`);
- prices and FX are the latest on or before *d* (`_entry_at_or_before`, `_fx_at`), re-expressed for
  splits in `(priced_on, d]` (`price_in`, `timeseries.py:451`);
- a dividend's refusal is decided by the events before it in the replay order
  (`timeseries.py:327-334`; the vacated-ticker check reads the position's state when the dividend
  is processed, `cost_basis.py:1036`; a later trade clears `vacated_to` only for later events,
  `cost_basis.py:186-203`) — so the refused set restricted to rows ≤ *d* is the same whether the
  replay stops at *d* or at today;
- every 待釐清 flag on the book is date-scoped the same way (`timeseries.py:375-401`);
- the cash series carries each pool's end-of-day balance forward and converts it at that day's FX
  (`networth.py:124-143`);
- the benchmark counterfactual accumulates units flow by flow and values them at the last close
  (`benchmark_counterfactual.py:154-184`).

**Measured** on the prod-shaped scratch ledger (`bench_settle_sim.py`): a BUY dated today and a cash
dividend dated today changed 0 of 2,178 earlier points; a back-dated BUY on 2025-06-16 changed 0 of
1,694 earlier points and 485 from that day; one restated close on 2024-03-04 changed 0 of 1,225
earlier points and 1 point.

**Series-level aggregates are not prefix figures** and are therefore never settled:

| Aggregate | Rule today | Handling |
| --- | --- | --- |
| `TrendSeries.available` | false if ANY flow up to today lacks an on-or-before FX rate (`build_reporting_flows` returns `None`, `timeseries.py:214-227`) | evaluated live on every build over all flows (cheap); when false the response is the unavailable series, exactly as today |
| `TrendSeries.options_value_excluded` | true if any day had an open option | stored per day; the series flag is the OR of stored days and live days |
| the series start | `min(event_dates)` | stored `first_day`; a change dated before it dirties from that date (§7) |

**Decimal order — byte identity.** `Decimal` addition is exact while the result fits the context
precision (28 digits) and order-dependent once it rounds. Two sums matter:

- `net_invested` re-sums the flow list from zero each day, in the list's order
  (`timeseries.py:458-461`), and that list is **not date-sorted** (openings, then trades, then
  dividends, then cash kinds — `timeseries.py:195-229`). A running sum in date order equals it only
  when no partial sum in EITHER order rounds. The engine checks an **order-independent bound**
  once per build: with *e* the smallest exponent among the converted amounts, require
  `Σ|amount| < 10^(28 + e)` — then every partial sum in every order is exact. When the bound
  fails (possible with USD / MYR reporting, where an inverse rate carries 28 digits) the engine
  uses the reference per-day sum, recomputed on flow days only. Either way the result is the
  reference result. (Trapping `Inexact` on the date-ordered sum alone would not prove the
  list-ordered one exact — review A #7a.)
- the benchmark's `units += amount / level` is a quotient and is inexact by nature. Stored
  cumulative units are used only when **no flow is dated after `settled_through`**, nothing is
  pending or being re-settled, and **no corporate action on a benchmark storage key is dated after
  `settled_through`** (`series_in` re-expresses every benchmark level into the valuation day's
  terms, and `0050` is both a benchmark and a stock the owner can hold); otherwise the
  counterfactual is computed live in reference order (§10).

## 5. What is settled and what stays live

| Figure | Source | Note |
| --- | --- | --- |
| Trend points: `total_value`, `net_invested`, `incomplete`, `net_worth` | store for days < `live_from`; live from `live_from` | `live_from` and the stale range as defined in §10 step 3 (a range claimed by a run counts as dirty) |
| Benchmark comparison | stored state only under the three conditions of §10 step 5, else live | terminal value always uses today's last close |
| `/api/performance/twr` | `twr_index` on the composed daily series | benchmark overlay series cached per (key, window) |
| B (含匯兌總損益), B − A, 交易與融資成本 | live | B is the trend's LAST point, which is always live |
| Month and year closes (KPIs + period activity + holdings at period end) | store for periods ending before `live_from`; live otherwise | §11 |
| 月度成績 rows | past months: month closes; current month: live KPIs | PERF-D7 |
| KPIs, holdings, subtotals, realized rows, returns, XIRR, FX P&L, dividends summary (incl. by_year / TTM), allocation, cash, options block, freshness, ex-dividend calendar, projection | **live, every build** | one replay of the ledger cut at today |
| Alerts | live, from the (cached) build | `max_drawdown` reads the composed trend (`strategy/alerts.py:526-532`) |

## 6. Data model

All tables are owned by `portfolio/settlement_store.py` (new), created at boot after every
borrowed table exists (`api/app.py` lifespan, after `seed_accounts` at :183; also in
`tests/conftest.py:149-162`). Borrowed tables are named in the docstrings (the
direct-SQL convention, `architecture.md`). Money columns are canonical Decimal TEXT.

```sql
CREATE TABLE IF NOT EXISTS settle_meta (            -- exactly one row
    id INTEGER PRIMARY KEY CHECK (id = 1),
    generation INTEGER NOT NULL DEFAULT 0,          -- 0 = never settled
    settled_through TEXT,                           -- ISO date; NULL when generation = 0
    first_day TEXT,                                 -- the series start of this generation
    calc_version TEXT,                              -- PERF-S5
    reporting_ccy TEXT,
    bench_state TEXT,                               -- JSON: counterfactual state through settled_through
    built_at TEXT, run_id INTEGER,                  -- the run that produced this generation
    run_from TEXT, run_through TEXT,                -- the ACTIVE run's range; NULL when idle (§9.4)
    held_through TEXT,                              -- last day this generation holds rows for (§9.4 partial)
    trigger_schema_version INTEGER                  -- PRAGMA schema_version when the trigger set was verified (§7)
);
CREATE TABLE IF NOT EXISTS settle_dirty (           -- exactly one row; triggers raise it, a run claims it
    id INTEGER PRIMARY KEY CHECK (id = 1),
    dirty_from TEXT,                                -- earliest affected day; NULL = clean
    seq INTEGER NOT NULL DEFAULT 0                  -- +1 on every recorded change
);
-- The change trail (§9.6). A row is PENDING (claimed_run_id and settled_run_id NULL), CLAIMED by a
-- run, or SETTLED by one; rows are tagged, never moved, so nothing is lost when a run fails.
CREATE TABLE IF NOT EXISTS settle_change_log (      -- one row per changed ledger / reference record
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seq INTEGER NOT NULL,                           -- settle_dirty.seq this change produced
    table_name TEXT NOT NULL,                       -- not prices / fx_rates (settle_change_agg)
    op TEXT NOT NULL CHECK (op IN ('insert', 'update', 'delete')),
    row_key TEXT NOT NULL,                          -- id; account|symbol for openings; symbol; account_id
    old_account_id TEXT, new_account_id TEXT,       -- kept apart, rendered as {account:<id>} tokens
    old_symbol TEXT, new_symbol TEXT,               -- for a corporate action: from_symbol (old/new) + to_symbol in detail
    detail TEXT,                                    -- JSON: e.g. to_symbol, kind, for the panel line
    old_day TEXT, new_day TEXT,                     -- the affected day before / after (§7)
    import_batch_id TEXT,                           -- so an import or a batch undo groups as one line
    actor TEXT,                                     -- 'user:<name>' | 'job:<id>' | 'system:boot' | NULL = 外部程式／腳本 (§7)
    written_at TEXT NOT NULL,                       -- the statement's time (UTC), shown as 寫入時間
    claimed_run_id INTEGER, settled_run_id INTEGER,
    attempted_run_ids TEXT NOT NULL DEFAULT ''      -- runs that claimed it and did not publish it
);
CREATE TABLE IF NOT EXISTS settle_change_agg (      -- prices and fx_rates: one counter per symbol / pair
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT NOT NULL, key TEXT NOT NULL,    -- symbol, or 'USD/TWD'
    changes INTEGER NOT NULL, earliest TEXT NOT NULL, latest TEXT NOT NULL,
    claimed_run_id INTEGER, settled_run_id INTEGER, attempted_run_ids TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS settle_change_agg_pending   -- the trigger's upsert target
    ON settle_change_agg (table_name, key) WHERE claimed_run_id IS NULL AND settled_run_id IS NULL;
CREATE TABLE IF NOT EXISTS settle_run (             -- §9.6: one report per run, kept after it ends
    run_id INTEGER PRIMARY KEY,                     -- = its job_runs id
    status TEXT NOT NULL CHECK (status IN ('preparing', 'running', 'published', 'partial',
                                           'superseded', 'cancelled', 'failed', 'interrupted')),
    resumed INTEGER NOT NULL DEFAULT 0,             -- interrupted AND a resume actually started
    reason TEXT NOT NULL,                           -- prompt|button|boot|job:<id>|nightly|chained|resume|selfcheck
    started_by TEXT,                                -- 'user:<name>' for prompt / button; else NULL
    cancelled_by TEXT,                              -- 'user:<name>' when cancelled
    trigger_text TEXT NOT NULL,                     -- rendered per viewer (「你」 only for started_by)
    started_at TEXT NOT NULL, finished_at TEXT,
    range_from TEXT, range_to TEXT, days INTEGER,
    generation_before INTEGER, generation_after INTEGER,
    settled_through_before TEXT, settled_through_after TEXT,
    calc_version_before TEXT, calc_version_after TEXT,
    reporting_ccy_before TEXT, reporting_ccy_after TEXT,
    eta_initial_s REAL, duration_s REAL,
    previous_run_id INTEGER, next_run_id INTEGER,   -- the chain (resume / chained follow-up)
    report TEXT NOT NULL                            -- JSON, §9.6 — Decimal values as canonical strings
);
CREATE TABLE IF NOT EXISTS settle_run_diff (        -- §9.6: every changed day of a run, kept with its report
    run_id INTEGER NOT NULL, day TEXT NOT NULL,
    field TEXT NOT NULL,                            -- total_value | net_invested | net_worth | incomplete
    kind TEXT NOT NULL CHECK (kind IN ('changed', 'value_to_none', 'none_to_value',
                                       'flag_flip', 'new_day', 'dropped_day')),
    before TEXT, after TEXT,                        -- canonical Decimal strings (NULL where none)
    flags_before TEXT, flags_after TEXT,            -- incomplete / cash_incomplete / options_open
    PRIMARY KEY (run_id, day, field)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS settled_day (
    generation INTEGER NOT NULL, day TEXT NOT NULL,
    total_value TEXT NOT NULL, net_invested TEXT NOT NULL,
    net_worth TEXT, cash_total TEXT, cash_incomplete INTEGER NOT NULL,
    incomplete INTEGER NOT NULL, options_open INTEGER NOT NULL,
    PRIMARY KEY (generation, day)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS settled_period (
    generation INTEGER NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('M','Y')),
    period TEXT NOT NULL,                           -- 'YYYY-MM' or 'YYYY'
    end_day TEXT NOT NULL,                          -- the period's last calendar day (PERF-S7)
    kpi TEXT NOT NULL,                              -- JSON: §11.2 KPI block
    activity TEXT NOT NULL,                         -- JSON: §11.3 period activity
    PRIMARY KEY (generation, kind, period)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS settled_period_holding (
    generation INTEGER NOT NULL, kind TEXT NOT NULL, period TEXT NOT NULL,
    account_id TEXT NOT NULL, symbol TEXT NOT NULL,
    asset_type TEXT NOT NULL, quote_ccy TEXT NOT NULL,
    shares TEXT NOT NULL, original_cost_total TEXT NOT NULL, adjusted_cost_total TEXT NOT NULL,
    price TEXT, price_as_of TEXT, market_value TEXT, unrealized TEXT,
    flags TEXT NOT NULL DEFAULT '{}',               -- oversold / unbookable_* / short_open
    PRIMARY KEY (generation, kind, period, account_id, symbol)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS settle_restatement (     -- §14.6: a closed period that moved, and why — never pruned
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL, period TEXT NOT NULL,
    field TEXT NOT NULL, label TEXT NOT NULL,       -- e.g. total_market_value / 總市值
    unit TEXT NOT NULL,                             -- 'TWD' | 'USD' | 'MYR' (reporting or native) | 'ratio'
    old_value TEXT, new_value TEXT,
    notes_before TEXT, notes_after TEXT,            -- the close's notes, so a jump explains itself
    run_id INTEGER NOT NULL, reason TEXT NOT NULL, source TEXT, at TEXT NOT NULL,
    cutover INTEGER NOT NULL DEFAULT 0              -- 1 = the one-time restatement at go-live (§11.4)
);
CREATE TABLE IF NOT EXISTS settled_period_cash (    -- §11.3: cash ties to the broker statement
    generation INTEGER NOT NULL, kind TEXT NOT NULL, period TEXT NOT NULL,
    account_id TEXT NOT NULL, ccy TEXT NOT NULL,
    balance TEXT NOT NULL, reporting_value TEXT, fx_rate TEXT, fx_as_of TEXT,
    PRIMARY KEY (generation, kind, period, account_id, ccy)
) WITHOUT ROWID;
```

Size for today's ledger: ~2,180 day rows, ~72 month + 6 year rows, a few thousand holding rows
— well under 1 MB. Doubled data adds rows linearly.

**Indexes on the borrowed ledgers** (the `prices` trigger asks "does this symbol appear in a
ledger?" — a 5-year backfill fires it ~100,000 times): `transactions(symbol)`,
`dividends(symbol)`, `opening_inventory(symbol)`, `corporate_actions(from_symbol)`,
`corporate_actions(to_symbol)`, created by their owner `data_ingestion/schema.py` (P6 also asks for
`(account_id, symbol)`).

**Generations.** Readers use `settle_meta.generation`. A run writes G+1 rows (copying rows before
its start day from G with one `INSERT … SELECT`), then in one transaction: updates `settle_meta`
to G+1 and deletes generation G's rows. Orphan rows of a dead run (generation > current) are
deleted at boot and before every run.

## 7. Invalidation — triggers

Created at boot (lifespan): each trigger's text in `sqlite_master` is compared with its expected
definition first, and only a missing or different one is (re)created with `DROP TRIGGER IF EXISTS`
+ `CREATE TRIGGER` (integrity paragraph below), so a definition change in a release replaces the old
one and an ordinary boot changes nothing. Never created by a GET (`tests/contract/test_def065_get_never_writes.py`
compares `sqlite_master`), in **one explicit transaction after every migration** (the legacy
`sqlite3` mode runs DDL in autocommit, `shared/db.py:38-43`). Each trigger lowers
`settle_dirty.dirty_from` to the earlier of its current value and the affected day, adds 1 to
`seq`, and records the change in the trail (§9.6): for the ledger and reference tables one
`settle_change_log` row naming the record (old and new account, symbol and affected day, the
import batch, `written_at`); for `prices` / `fx_rates` — where one backfill writes tens of thousands
of rows — an upsert of the PENDING `settle_change_agg` counter of that symbol or pair (count +1,
earliest / latest day widened). It does so **only when the affected day is ≤ the latest of
`settled_through`, `held_through` and the active run's `run_through`** (a later day is in the live
tail anyway; `run_through` means a change dated inside the range a run is about to publish is never
lost; `held_through` means a change on the held days of a partial publish is logged even while the
chained run is still queued, has failed, or `PD_SETTLE=off` — final check #2) and only when a value
that the store depends on actually changed.

**Who made the change.** `actor` is stamped by a **TEMP trigger** that the application installs
only on the connections it opens for a mutating request (`user:<login name>`, from the session) or
a job (`job:<id>`), together with an application function returning that name; a TEMP trigger may
read the connection's own state and fire on the main table, and it never exists on a GET
connection (DEF-065) or on a connection the application did not open. A row written by anything
else — the sqlite3 CLI, a script with raw `sqlite3` — keeps `actor = NULL`, shown as
「外部程式／腳本」. The application's own boot writes (`seed_accounts`, migrations) run on a connection
stamped `system:boot` (shown as 「系統（啟動）」), so they are never mistaken for a script. The main
triggers call no application function, so external writers never fail.

**NULL safety** (review C #4d, verified on SQLite 3.49): `min()` / `max()` with a NULL argument
return NULL, so every comparison is written with `CASE` / `COALESCE` — "the later of
`settled_through`, `held_through` and `run_through`" is `MAX(COALESCE(settled_through, ''),
COALESCE(held_through, ''), COALESCE(run_through, ''))`, and a NULL `dirty_from` is replaced, never
compared. **`UPDATE OF col` fires even when the
value is unchanged** (review C #4e — every instrument edit upserts `market` and `quote_ccy`,
`store.py:207-225`), so each UPDATE trigger carries a `WHEN` clause comparing OLD and NEW of every
included column with `IS NOT`. **Trigger integrity:** boot and every run compare the trigger set in
`sqlite_master` with the expected definitions and record `PRAGMA schema_version` in
`settle_meta.trigger_schema_version`; every read compares it with the current `schema_version`
(one cheap pragma) and treats any difference as "unverified" — the store is not served until the
set is verified again (§10 step 2 includes this check). Boot compares each trigger's text in
`sqlite_master` with the expected definition **before** recreating anything: an identical trigger
is left alone (recreating it would move `schema_version` on every boot — measured 7 → 9 — and an
unconditional rule would make every boot a full re-settle; fold verification 2 #2); only a
**missing or different** trigger (a table-rebuild migration, a manual `DROP`, a release that
changes the definitions) is recreated, and — outside a calc-version change — it also sets
`dirty_from = first_day`, because edits made while it was missing never reached `settle_dirty`
(final pass #3). `schema_version` is recorded after the recreation. `PD_SETTLE=off` keeps the triggers installed, so switching
it back on starts from a correct dirty state. The settle tables are never dropped while their
triggers exist — a ledger write would then fail with "no such table"; the documented reset drops
the triggers first.

**Trigger kinds:** `AFTER INSERT`, `AFTER UPDATE … WHEN`, `AFTER DELETE` only. An upsert that
takes its `DO UPDATE` branch fires the UPDATE triggers and the BEFORE INSERT triggers, never the
AFTER INSERT ones (verified on SQLite 3.49.1 — fold verification #9) — so with AFTER triggers a
`seed_accounts` boot rewrite, a `upsert_prices` restamp, an instrument edit or the opening upsert
is judged by the UPDATE trigger's `WHEN` clause, which is what keeps them silent when nothing
changed.

**Two general rules for every trigger** (review A #2):

1. **An UPDATE marks from the earlier of the OLD and NEW dates** — on every table, because every
   ledger has a door that can move a row's date (transactions and dividends; the opening upsert,
   `data_ingestion/store.py:1022-1030`; the cash-movement edit, `:2145`; the FX-conversion edit,
   `:1956`; the corporate-action edit, `:1597`). Marking only NEW would leave the row's old range
   settled with the row still counted in it — a 3,000 BROKER_FEE moved from 2024-03-05 to
   2026-10-10 would stay in every day and month-end XIRR from 2024-03-05 on, with the chip saying
   已結算.
2. **"Fires only when" is an exclusion list** ("any column except …"), and every symbol test checks
   both the OLD and NEW symbol — a column added to a ledger later is then an input by default, not
   a silently ignored one.

| Table | Events | Affected day (UPDATE: the earlier of OLD and NEW) | Fires on UPDATE when any column changed except |
| --- | --- | --- | --- |
| `transactions` | INSERT / UPDATE / DELETE | `trade_date` | `fee_rule_snapshot`, `note`, `import_batch_id`, `source_row_hash` |
| `dividends` | INSERT / UPDATE / DELETE | the replay's **effective date**: `ex_date` for a STOCK dividend that has one, else `date` (the payment date) — `CASE WHEN type = 'STOCK' AND ex_date IS NOT NULL THEN ex_date ELSE date END`, the SQL twin of `Dividend.effective_date` (`shared/models/ledger.py:100-111`, a test asserts they agree for every type). Not "the earlier of `date` and `ex_date"`: a cash dividend changes nothing before its payment date, and a TW payout confirmed from the inbox on its pay date with an ex-date 6–8 weeks back would otherwise prompt on the most routine entry there is (review C #4f). The ex-date's only other use, the `dividends_receivable` note of a period close, is computed when the close is read (§11.2), so it needs no invalidation | `import_batch_id`, `source_row_hash` |
| `opening_inventory` | INSERT / UPDATE / DELETE | `build_date` | none — every column is an input (the table has no provenance columns, `schema.py:40-45`), so an `account_id` rewrite by `moomoo_merge.py:191` fires |
| `cash_movements` | INSERT / UPDATE / DELETE | `date` | `note`, `import_batch_id`, `source_row_hash` |
| `fx_conversions` | INSERT / UPDATE / DELETE | `date` | `import_batch_id`, `source_row_hash` (it has no `note`, `schema.py:34-39`) |
| `corporate_actions` | INSERT / UPDATE / DELETE | `date` | `child_seed_json`, `note`, provenance (the seed's price row fires `prices`) |
| `prices` | INSERT / UPDATE / DELETE | `as_of_date` | everything except `close` and the key columns `instrument`, `as_of_date` — i.e. it fires when `close` changed or the row moved to another symbol or day (INSERT, DELETE, or `OLD.close IS NOT NEW.close OR OLD.instrument IS NOT NEW.instrument OR OLD.as_of_date IS NOT NEW.as_of_date`); restamping an unchanged row (`upsert_prices` rewrites every column, `pricing/store.py:276-285`) is silent. Also only when the instrument (OLD or NEW) appears in `transactions`, `dividends`, `opening_inventory`, or `corporate_actions` as **`from_symbol` or `to_symbol`** — a SPINOFF child appears in no other ledger (`dashboard.py:1109-1118`, audit F-50), so its real history backfilled after the seed must fire — or is a benchmark storage key |
| `fx_rates` | INSERT / UPDATE / DELETE | `as_of_date` | everything except `rate` and the key columns `base`, `quote`, `as_of_date` |
| `instruments` | INSERT / DELETE; UPDATE | the earliest date of that symbol (OLD and NEW) in the ledgers — trades' `trade_date`, openings' `build_date`, dividends by the effective-date expression above, **and `corporate_actions` by `date` where it is `from_symbol` or `to_symbol`** (registering an unregistered `to_symbol` brings its action back from its date, `ledger.py:394-395`); none if it has no ledger rows | `name`, `sector`, `industry`, `aliases`, `board`, `board_status`, target bands and `target_set_at` (every band edit moves it — an alert-band change must not prompt a re-settle), `archived`, `etf_flag_unknown`, `is_etf` (fees are stored per row), and `_unarchive_on_booking`'s flag |
| `accounts`, `account_market_rules` | INSERT / DELETE; UPDATE | the first ledger day (full re-settle) | `name` / display labels — `seed_accounts` rewrites every row at boot unconditionally (`config_seed.py:298-339`) and the comparison keeps it silent when nothing changed |

Benchmark storage keys (`0050`, `^GSPC`, `^KLSE` — `pricing/benchmarks.py:66-115`) are written into
the `prices` trigger's `WHEN` clause at boot; benchmarks are code constants, so a change is a release
and the triggers are re-created.

**The calc version** (PERF-S5) covers everything that is code: formulas, benchmark definitions,
the reporting currency, the store schema.

**Not inputs** (no trigger): `dividend_events` (ex-dividend calendar and projection — live),
fee rules (each row keeps its own snapshot, `fee_rules.py:5`), alert rules, settings.

**Ledger rows added by the import-batch undo, the moomoo merge and every script** fire the same
triggers — the reason for triggers instead of an application hook (the stress-audit harness itself
writes prices and actions straight into SQLite from another process, `scripts/stress_audit/common.py:375-405`).

## 8. The engine (WP2) — one pass, same answers

1. **`cost_basis.iter_books(bundle, days)`** — a generator in `portfolio/cost_basis.py` (which must
   stay a leaf, `tests/architecture/test_layer_edges.py`). It sorts the events exactly as
   `build_book` does (`cost_basis.py:718-729`), applies them once, and at the end of each requested
   day yields a `Book` built by the same finalisation `build_book` runs (`cost_basis.py:1150-1240`),
   composed so that it equals `build_book(bundle.through(day), allow_oversell=True)`:
   - the prologue's unreadable actions (`cost_basis.py:697-705`) enter at their own dates, in list
     order, ahead of the event-loop refusals — as the prefix bundle would hold them;
   - the finalisation reads the replay state and never mutates it: the epilogue's
     `touched.unbookable_action = True` (`cost_basis.py:1148-1151`) is applied to a **copy** of the
     positions at each yielded day. It is not moved into the event loop: the epilogue also flags a
     position opened AFTER the unreadable row's date, which an in-loop flag at that date would miss
     (final pass #8);
   - lists in the yielded `Book` are copies.
   `build_book` itself becomes "run `iter_books` to the last day" only if WP2 proves that changes no
   output; otherwise it stays as it is and remains the reference.
2. **`timeseries.value_series(…, start, end, books)`** — the per-day valuation loop of
   `daily_value_series` (`timeseries.py:355-464`), unchanged in arithmetic and order, fed by
   `iter_books`, valuing only days in `[start, end]`, with:
   - `net_invested` from the exactness-guarded running sum (§4);
   - price and FX lookups by a moving index per symbol / pair instead of a bisect from the start;
   - no per-day `through()`.
   `daily_value_series` keeps its signature and becomes a thin wrapper (start = first event day),
   so every existing caller and test is unchanged.
3. **`networth.daily_cash_series(…, start=None)`** — a new optional `start`: days before it are not
   emitted. The balances are still built from every line (exact sums), so a day's value does not
   depend on `start`.
4. **Benchmark state** — `counterfactual` gains a function returning its accumulation state
   (`units`, `invested`, `uncovered_gross`, `gross_all`, `uncovered`) for a flow list, and a
   function finishing a state with the last closes; `counterfactual(flows, closes)` is their
   composition, unchanged.
5. **Light reads (P6)** — `pricing/store.py::get_close_series(conn, symbol, start, end) ->
   list[tuple[date, Decimal]]` (no `PriceRead` per row). Every lookup takes the latest row **on or
   before** a day (`timeseries.py:68-107`, `benchmark_counterfactual.py:115-127`), so every bounded
   load **starts at the last row on or before its boundary**, never at the boundary itself
   (review A #5): the trend's closes from the last close on or before the first ledger date;
   benchmark closes from the last close on or before the first flow date instead of 1990 / 1900
   (`_BENCH_EPOCH`, `dashboard.py:570`; `_EPOCH`, `performance.py:38`, used at `:126-136`); the live tail from the last close / rate on or
   before `live_from`; FX in both directions. Otherwise an opening built on 1 January, a first flow
   on a holiday, or a tail starting on a Saturday loses the carried-forward value. FX histories
   are read once per build into one cache (today one build issues **1,059 `fx_rates` queries** —
   measured with a SQL trace), but **each consumer keeps exactly the pair set it reads today**
   (the trend: the ledger's quote currencies, `dashboard.py:1132-1138`; the cash series: the cash
   pools' currencies; XIRR's `fx_at`; the benchmark): handing the trend a pair it does not read
   today could flip `available` and break byte identity. Tests cover each boundary on a weekend and
   a holiday, not only on weekdays.
   ⚠ **Suspected pre-existing defect PERF-F2** (final pass #9): because the trend loads only the
   ledger's quote currencies, a cash movement of an XIRR kind (REBATE, INTEREST_EXPENSE,
   BROKER_FEE) in a currency no held instrument is quoted in finds no rate in the trend's FX map
   (`timeseries.py:225-227`) and makes the whole trend unavailable, although the rate is stored.
   WP2 reproduces it; if confirmed it is reported to the owner and fixed as its own change, since
   the fix changes output (the trend becomes available).
6. **Equality obligation:** for every corpus in §18, every event day, `iter_books` ≡
   `build_book(bundle.through(day))` (`model_dump` equality); the series ≡ `daily_value_series`'s
   reference output; the dashboard payload ≡ today's. The reference implementations stay in the
   test tree as the oracle.

## 9. The settlement run (WP3)

### 9.1 Starting a run

| Trigger | Range | Prompt |
| --- | --- | --- |
| user confirms the prompt (§14.3) | from `dirty_from` | is the prompt |
| user presses 重新結算 (§14.4) | full: from the first ledger day | the panel's button is the confirmation |
| boot: store empty, calc version differs, the last run was interrupted, or a missing / different trigger was recreated (§7) | full / full / resume its range / full | none (automatic — nobody made these changes, Q2); a recreated trigger also adds one trail line 「觸發器重建：<table>」 so the run's change list explains its diff |
| after a scheduled job whose run raised `seq` with `dirty_from < live window start` | from `dirty_from` | none (automatic) |
| nightly `settlement_nightly` (06:40 TPE, after the US quotes at 16:30 New York) | from `dirty_from` if dirty; then extend `settled_through` up to **`final_through`** (below) and no further, so a day whose final close may be missing stays live and the period it closes stays 暫定 (final pass #1) | none (automatic) |
| a change committed while a run is active | chained: a follow-up run from the new `dirty_from` starts when the active one finishes | none — the panel lists it as 「完成後接著處理」 |
| weekly self-check `settlement_selfcheck` (Sunday 04:30 TPE) | full recompute in a child, compared row by row with the published generation (skipping the pending range); it publishes through the ordinary swap of §9.4 — the `run_id` guard, the start-time calc version, never writing `settle_dirty`, a partial publish below a new `dirty_from`, restatement rows for any close it changes | none; any difference is recorded in its report, a `job_runs` row naming the first differing day and table, AND a chip state 自檢發現差異 with the detail in the panel — a missed invalidation path, to be fixed as a defect |

**`final_through`** — **the day before the earliest non-qualifying day, or yesterday if there is
none** (computed from `job_runs` and the schedule, never from the store; a qualifying weekend never
hides a failed Friday such as month-end 2026-07-31). A day D qualifies when **every market quote
job that was due on D** ended `ok` — "due on D" meaning the fire, by the job's cron, enabled, with
the scheduler on, **whose date in the job's own cron timezone is D** (`quotes_us` fires at 16:30
`America/New_York`, i.e. 04:30 / 05:30 Taipei on D+1, and still judges D). The verdict is the
quote jobs' `_quote_outcome` (`jobs.py:480-530`, DEF-067: `partial` or `error` means a held
symbol's final close may be missing, and `history_daily` at 02:00 TPE may have stored a still-open
US session as D's bar). A day on which no quote job was due qualifies (weekends; days before
go-live; every day on the demo, whose scheduler is disabled; a market job the owner switched off).
A day **older than 4 days** (the live staleness age, `pricing/store.py:22`) qualifies regardless,
and its close lists the symbols of that run's `held_failed` list, with the price date actually
used, in `notes.unconfirmed_closes` — so a held symbol the provider no longer quotes, which makes
its job `partial` every day, never blocks settlement for more than four days and never passes
unmarked. On a weekday holiday the market's job finds no bar and ends `ok`; the rule needs no
calendar.

At most one run at a time per instance (a lock file in the data folder plus `settle_meta.run_id`);
at most one heavy child at a time across the settlement run and the job workers (§16), queued in
order. With `PD_SETTLE=off` nothing starts and every read is a full live build (the kill switch,
today's behaviour with the new engine).

### 9.2 The child process

`python -m portfolio_dash.portfolio.settle_worker --run <id>` started by
`api/settlement_service.py` (`subprocess.Popen`, `start_new_session=True`). In the child, first:
`os.nice(19)` and, on Linux, `os.sched_setscheduler(0, os.SCHED_IDLE, …)` when available. It
imports only `portfolio/`, `pricing/`, `forex/`, `data_ingestion/` and `shared/` — never `litellm`,
`yfinance`, `pandas` or `fastapi` (a test inspects `sys.modules` after the worker's imports).
`PD_SETTLE_MODE=inline` runs the same code in-process for hermetic tests and Windows development.

### 9.3 Phases (also what the panel shows)

| # | Phase (zh label) | Work | Unit for progress |
| --- | --- | --- | --- |
| 1 | 讀取帳本與行情 | one read transaction: ledgers, instruments, accounts; close series and FX histories from the first needed day; `settle_dirty.seq` read in the same snapshot | symbols loaded / total |
| 2 | 檢查帳本 | on phase 1's bundle (the ledger this run settles — fold verification 2 #7), a pure `portfolio/` function `ledger_findings(bundle) -> list[Finding]` (the worker may not import the 重算 route, which imports `fastapi` and `scheduler.jobs` — fold verification #10) that runs the dashboard's **tolerant** replay (`allow_oversell=True`) and lists **every** record that replay keeps as 待釐清 — `Book.oversold_positions`, `orphan_dividends`, `unbookable_dividends`, `unapplied_actions`, `refused_closes`, and unregistered symbols (excluded exactly as the dashboard excludes them) — each dated, in **non-blocking** wording (「以待釐清處理：…」, one owner per sentence beside today's strict ones, e.g. a non-blocking twin in `shared/oversold.py`). The strict 重算 replay stops at its first refusal and says 「無法重算」, which would list one item and claim the run stopped (run-report review #5). Findings never stop the run; the step's detail counts the events the tolerant replay actually processed | findings |
| 3 | 重播帳本 | `iter_books` over the whole ledger, keeping books for the days to value | events / total |
| 4 | 逐日估值 | `value_series` + `daily_cash_series(start=…)` for the run's days; written to G+1 in chunks of one month, each its own short write transaction | days / total; the current item is the day being valued and the number of positions valued on it (every account's holdings are valued each day, so naming one account or FX pair would be arbitrary) |
| 5 | 月結與年結 | §11 for every period end in range | periods / total |
| 6 | 基準比較 | the counterfactual state through the run's last day — published only on a full publish (on a partial one it would describe days that are not published) | — |
| 7 | 比對與切換 | before the swap transaction: the G-vs-G+1 result (§9.6) and the restatement rows; inside it (kept small): copy rows before the start day, apply the cut-off of §9.4 (trimming the result to the published range if a new `dirty_from` arrived), swap, delete G | — |
| 8 | 預熱 | in the web process: invalidate and pre-warm today's result (§12) | — |

### 9.4 Changes during a run, cancel, interruption, failure

- **The run's range.** A run starts at `min(dirty_from, settled_through + 1)` (the first ledger
  day for a full run) and ends at the last day the nightly rule of §9.1 allows, so its new
  generation never has a gap. Before it writes `published` or `partial`, the swap asserts the
  generation holds exactly one `settled_day` row per day in `[first_day, settled_through]` **and** one
  `settled_period` row per month end and year end ≤ `settled_through`.
- **Starting.** The parent (the settlement service) allocates the run id and creates the
  `settle_run` row with status `preparing` (準備中) **before** it spawns the child, so every run —
  even one whose child dies at once — has a report. The child takes its OS lock **first**, then
  claims; a status read that finds `preparing` shows 準備中, never 已中斷 (fold verification 2 #3).
- **Claiming the dirt.** The child's first write is one short transaction that copies
  `settle_dirty.dirty_from` into `settle_meta.run_from`, sets `run_through`, records its `run_id`,
  sets `dirty_from = NULL`, moves its `settle_run` row to `running`, and
  **tags** every PENDING `settle_change_log` / `settle_change_agg` row with `claimed_run_id` = this
  run — rows are tagged, never moved, so a change is always pending, claimed by exactly one run, or
  settled by exactly one, and none is ever lost or truncated (run-report review #3). Only then does
  it open its read snapshot (phase 1). A change committed after the claim is a new PENDING row and
  lowers `dirty_from` afresh; one committed between the claim and the snapshot is in both, which
  costs at most one extra run and never a missed change. **Readers keep treating the claimed range
  as dirty** (`Dd` = the earlier of `dirty_from` and `run_from`, §10), so a run never makes a
  correction that is already shown live disappear while it computes.
- **Publishing (the swap).** Only if `settle_meta.run_id` is still this run's id (an orphan child
  that outlived a restart can never publish over boot's cleanup). With *N* = the `dirty_from` found
  at the swap (a change committed during the run):
  - ***N* is NULL — full publish:** `settled_through = held_through = run_through`; every claimed
    row gets `settled_run_id` = this run; the benchmark state is published; status `published`.
  - **`range_from < N ≤ run_through` — partial publish:** `settled_through = N − 1` (valid by the
    prefix property). The days from *N* on are **not** published: G+1 keeps generation G's rows for
    `[N, previous held_through]` unchanged — they are what the owner was last shown, are served only
    as the labelled stale range (§10), and are the next run's comparison base — and
    `held_through = max(previous held_through, N − 1)` (a run that also extended new days must not
    lose the ones it published). The result and the restatements are computed over
    `[range_from, N − 1]` only. Claimed records whose affected day is ≥ *N* (their effect was not
    published at all) are put back with **the per-record steps below only** — claim cleared, run
    appended to `attempted_run_ids`, price / FX counters merged — never the end transaction's
    `dirty_from := min(dirty_from, run_from)`, which would mark the range just published dirty
    again (`dirty_from` is already *N*); the others are settled by this run for the days up to
    *N* − 1, and the chained run, which starts at *N*, completes their later days — its report says
    「延續 #412 的變更於 *N* 起的影響」. The benchmark state is not published (set to NULL: computed
    live until the chained run publishes). Status `partial`.
  - ***N* ≤ `range_from` — nothing publishable:** the run ends through the path below with status
    `superseded` (已併入下一輪); `settled_through` never moves backwards.
  The swap writes `calc_version` as the value the child computed when it started (never re-read at
  swap time — a deploy during the run must not stamp new-formula rows on old ones), clears
  `run_from` / `run_through` / `run_id`, and **never writes `settle_dirty`**. The G-vs-G+1 result
  (§9.6) and the restatement rows are computed BEFORE the swap transaction, which stays small and
  only trims them to the published range when *N* arrived meanwhile.
- **Every other end** — failure, cancel, crash, boot cleanup, `superseded` — runs one transaction:
  `dirty_from` = the earlier of `dirty_from` and `run_from` (never an overwrite: an earlier change
  that arrived during the run is kept), clear `run_from` / `run_through` / `run_id`, delete only
  generations above `settle_meta.generation`, and **put the claimed changes back**: each claimed
  `settle_change_log` row gets `claimed_run_id = NULL` and this run appended to
  `attempted_run_ids`; each claimed `settle_change_agg` row is **merged** into the PENDING counter of
  the same table and key (count added, earliest = min, latest = max, attempts kept) and removed — a
  plain re-insert would collide with a pending counter for the same symbol written during the run.
  The report is finalised with its status, what it got done, and its changes shown as
  「本次未生效，已退回待結算」; the next run's report marks them 「曾於 #411 嘗試」. Then:
  - **Cancel** (panel button 取消（保留目前版本）) terminates the child and **waits for it to exit**
    before that transaction, and does nothing if the run already published; state returns to
    待結算.
  - **Interruption** (restart, deploy, crash): the child holds an OS lock file in the data folder
    for its whole life (`fcntl` / `msvcrt`) and writes its pid into the progress file; the
    service's waiter thread sees an abnormal exit at once, and boot sees a non-NULL `run_id`. If
    that run's lock is **still held** (an orphan child that outlived the web process), boot
    terminates it by its pid and waits for the lock to free **before** the end transaction — the
    `run_id` guard on the swap then makes a late publish impossible. The same range then resumes
    automatically (reason `resume`). **Liveness is the OS lock, never the heartbeat** (run-report
    review #9): a status read tries the lock without blocking (not a database write); if it can take
    it, the child is gone and the state is 已中斷. A SCHED_IDLE child on a throttled VM can easily
    go 15 s without a heartbeat, so a silent heartbeat only shows 「進行緩慢」 after 60 s without
    progress. An interrupted run is shown as 「中斷，已續跑（#413）」 only when a resume actually
    started (`resumed = 1`); otherwise — under `PD_SETTLE=off`, or when boot starts a full run
    instead because the calc version changed — as 「結算中斷（未續跑）」 with the reason. The chip text
    is derived from that state, never fixed. Status reads never start or resume anything.
  - **Failure** (an exception in any phase): G stays, the run's `job_runs` row is `error` with a
    zh sentence (DEF-073), the panel shows it and offers 重試; a failed **automatic** run also sends
    one push notification (`ops/notify.py`, system topic), because on the demo (scheduler disabled)
    and at night nobody is looking at the chip.
- When a child exits, the parent calls `flight.note_write()`, so in-flight sharing never joins a
  build that started before the child's last commit.
- Every run has a `job_runs` row (`job_id = 'settlement'`, the existing table and history UI) with a
  zh `detail` like 「重新結算 2025-06-16 起 489 天，耗時 14 秒」 (DEF-073). Its status follows
  DEF-067's meanings: `published`, `partial` and `superseded` → `ok` (nothing was lost — 部分 means
  lost work there, and a partial publish loses none; the detail says what was published and what
  follows); `cancelled` → `skipped`; `failed` and `interrupted` → `error`; a **self-check that found
  a difference** → `partial` with reason `selfcheck_diff`, so the digest and the 排程中心 report it as
  the chip does. `job_runs.reason` keeps
  its existing meaning — an outcome code (`cancelled`, `interrupted`, `failed:<class>`), never the
  trigger; the trigger (`prompt`, `button`, `boot`, `job:<id>`, `nightly`, `chained`, `resume`,
  `selfcheck`) lives in `settle_run.reason`.

### 9.5 Progress file (PERF-S9, PERF-D10)

`<data folder>/settlement-progress.json`, replaced atomically (write temp + `os.replace`) at most
every 0.5 s and at every phase change. It carries everything the panel shows **while** a run is
active (§14.2); the same structure, frozen, becomes the run's report at its end (§9.6):

```json
{"run_id": 412, "pid": 2211, "heartbeat": "2026-10-18T10:32:14+08:00",
 "status": "running", "reason": "prompt", "started_by": "user:owner",
 "trigger_text": "{started_by} 於 10:32 啟動（交易更正）",
 "range": {"from": "2025-06-16", "to": "2026-10-17", "days": 489,
           "kept_before": 1694, "kind": "dirty"},
 "versions": {"generation_before": 41, "settled_through_before": "2026-10-17",
              "calc_version_before": "a1b2c3d", "calc_version": "a1b2c3d",
              "reporting_ccy_before": "TWD", "reporting_ccy": "TWD"},
 "started_at": "2026-10-18T10:32:02+08:00", "elapsed_s": 12.4,
 "percent": 58, "eta_s": 8, "eta_initial_s": 18,
 "eta_basis": "逐日估值實測速度 35 天/秒；其餘階段依上次結算（10-18 06:41）的耗時",
 "priority": "背景低優先序",
 "changes": {"tables": [{"table": "transactions", "label": "交易", "changes": 2,
                         "earliest": "2025-06-16", "latest": "2025-06-18"},
                        {"table": "prices", "label": "價格", "keys": 3, "changes": 14,
                         "earliest": "2025-07-01", "latest": "2025-07-21"}],
             "records_url": "/api/settlement/changes?state=run:412"},
 "validation": {"status": "done", "findings": []},
 "phases": [{"key": "load", "label": "讀取帳本與行情", "status": "done", "elapsed_s": 1.8,
             "detail": "86 檔 · 價格 74,393 筆 · 匯率 3 組 4,653 筆"},
            {"key": "check", "label": "檢查帳本", "status": "done", "elapsed_s": 0.1,
             "detail": "重播 1,030 筆事件，未發現待釐清項目"},
            {"key": "replay", "label": "重播帳本", "status": "done", "elapsed_s": 0.1,
             "detail": "1,030 筆事件 · 461 個事件日"},
            {"key": "valuate", "label": "逐日估值", "status": "running", "done": 281,
             "total": 489, "unit": "天", "elapsed_s": 7.9, "rate": "35 天/秒",
             "detail": "已寫入 2025-06 至 2026-03（10 個月）"},
            {"key": "close", "label": "月結與年結", "status": "pending", "total": 17,
             "unit": "個期間"}, …],
 "current": {"day": "2026-03-24", "positions": 41},
 "queued": [{"from": "2026-09-03", "changes": 1, "reason": "結算期間新增的變更"}],
 "on_cancel": "保留第 41 版（結算至 2026-10-17）；這 2 筆交易與 14 筆價格變更回到待結算"}
```

`on_cancel` and every failure sentence are **derived from the actual state**, never fixed text
(run-report review #6): generation 0 → 「目前尚無結算版本，歷史照常即時計算」; a calc-version or
currency change → 「目前沒有可用的結算版本（計算規則已更新），歷史照常即時計算」; a run with no
claimed change (a nightly extension) → 「保留第 41 版（結算至 2026-10-16），今天這段照常即時計算」.
Account ids travel as `{account:<id>}` tokens, resolved by the frontend like every other backend
text (`web/api.js:43-70`), old and new apart; symbols are shown as the instrument's registered code
and name; `{started_by}` renders as 「你」 only for the user who started the run, else that user's
login name. The live-only fields (`pid`, `heartbeat`, `eta_s`, `current`) are not frozen into the
stored report.

**ETA.** Each phase's expected duration is the same phase's duration in the last successful run,
scaled by its unit count; during phase 4, after ≥ 3 s, by its measured rate (exponentially smoothed).
First run ever: weights from the 2026-10-18 profile. Shown as 「預估還要 N 秒」, 「即將完成」 under 5 s,
「估算中」 while there is no basis; `eta_basis` says in words what the estimate rests on, and the
report records the first estimate beside the actual duration.

### 9.6 The run report — kept after every run

Every run leaves one `settle_run` row, created by the parent as 準備中 before the child is spawned
(§9.4), updated at the end of each phase (so an interrupted run keeps the part it finished), and
finalised at its end. **Boot finalises every `preparing` or `running` row whose OS lock is free** —
including one whose web process restarted before its child claimed (systemd's cgroup kill takes the
child too, and `settle_meta.run_id` is still NULL) — as 中斷（未續跑）, together with its `job_runs`
row; such a row never counts as an active run (final check #3). Nothing in it is computed
for display only: each figure is read from the store or the ledger at the moment it describes.

| Section | Contents |
| --- | --- |
| Summary | status (準備中 / 進行中 / 完成 / 部分完成，接續處理 / 已併入下一輪 / 已取消（by whom） / 失敗 / 中斷，已續跑 / 結算中斷（未續跑）) — 完成 only when every row of the range was published and the completeness assertion passed; the trigger sentence (who — 「你」 only for the viewer who started it, otherwise the login name; a job; boot; the nightly rule — when, and why); start, end, duration; the first ETA beside the actual duration; the previous and next run of a chain (a link to a pruned report reads 「已超過保留期限」) |
| Range | from – to, days re-valued, days kept from the previous generation, `dirty` or `full`; on a partial publish, the published part and the part handed to the chained run |
| Versions | generation, settled-through, calc version and reporting currency, each before → after (so a formula-driven change is attributable) |
| Changes settled | per table: count, earliest and latest affected day; prices and FX per symbol / pair (count and days); every changed ledger record — table, 新增 / 修改 / 刪除, record key, account and symbol (old and new), the affected day before → after, the import batch (an import or a batch undo groups as one line), the actor (使用者 / 排程 / 系統（啟動） / 外部程式／腳本), the write time — **all of them**, paged in the panel (`GET /api/settlement/changes`), never truncated; records this run took over from a run that did not publish them are marked 「曾於 #411 嘗試」; on a run that did not publish, the same list reads 「本次未生效，已退回待結算」 |
| Validation | phase 2's findings — every 待釐清 record of the tolerant replay, dated, non-blocking wording — or 「未發現待釐清項目」 |
| Steps | every phase with start, end, duration and its own detail line (events replayed, symbols / prices / FX rows read, days valued and the rate, periods closed, rows written) |
| Result — history | the G-vs-G+1 comparison, defined so it never reports a non-change as a change (run-report review #2): **only days held by both generations, with the same reporting currency, inside the published range**. **Every changed day is kept** — one `settle_run_diff` row per day and field (securities value, net invested, net worth, the incomplete flag) with before → after and the day's flags on both sides — and the panel pages through them, so which days changed and by how much can always be reopened after G is gone (fold verification 2 #4). On top: counts per field; value ↔ none changes and incomplete-flag flips as their own categories; the first and last changed day; the largest net-worth change with its date and its before → after; and the before → after of **the last published day that both generations hold** (never "yesterday vs today"). Days G did not hold are listed as 「新結算 N 天」 (`new_day`, no before value); days G held that G+1 no longer has (the series start moved later) as 「移除 N 天」 (`dropped_day`). A **calc-version** change is compared like any other (its reason reads 計算規則更新, the cause of every difference); only a **reporting-currency** change suppresses the value comparison (「報告幣別由 TWD 改為 USD：全部重算，不逐日比較前後值」). Reporting currency; labelled 「歷史走勢的變動（帳戶即時數字一直是最新的）」 |
| Result — closes | the period closes whose KPIs changed (the run's `settle_restatement` rows): period, the field's zh label (總市值 for the KPI, 證券市值 for the trend value — never one word for both), unit (currency or ratio), before → after, and the notes before → after (so a filled-in missing price explains a 總市值 jump); same comparison rules as above (both generations, same currency, published range); 「月結與年結無變動」 when none |
| Result — state | the stale range cleared or still open (and why); the 待釐清 positions on the book at the last settled day; whether a chained run follows and from which day |
| Failure / cancel / interruption | the phase it stopped in, the zh reason, who cancelled (`cancelled_by`), and the consequence derived from the actual state (§9.5 `on_cancel`) |

Retention: the latest 400 runs (about a year of nightly runs plus every other run); older reports,
their `settle_run_diff` rows and the change-log rows they settled are deleted together at a swap. **Restatement rows are never
pruned** — 月度成績's 已重述 marks must always be explainable. `job_runs` still gets its
one-sentence row per run (DEF-073), so the 排程中心 history is unchanged.

## 10. The read path (WP4)

`build_dashboard(conn, now, reporting)` keeps its signature and payload. Inside, in one read
transaction:

1. Load the ledger cut and compute the live part exactly as today (steps 1–8, 10–11 of
   `_build_dashboard`).
2. Read `settle_meta` and `settle_dirty`. The store is **usable** when `generation > 0`,
   `calc_version` matches, `reporting_ccy` matches, and the trigger set is verified
   (`trigger_schema_version` equals the current `PRAGMA schema_version`, §7); otherwise every day
   is live (step 4 with `live_from = first day`).
3. With `S = settled_through`, **`Dd` = the earlier of `dirty_from` and `run_from`** (∞ only when
   both are NULL — a range a run has claimed is still dirty until its swap publishes it; review A
   #1), `H` = `held_through` (= `S` except after a partial publish, §9.4) and `W` = the 1st of the
   previous month: **`live_from = min(H + 1, max(Dd, W))`**. (With `H = S` this is the original
   `min(S + 1, max(Dd, W))`; after a partial publish `Dd = S + 1`, so inside the window the days
   from `Dd` are live, and before it the held days `(S, H]` are the labelled stale range rather than
   a long live recompute.)
   - clean → `live_from = S + 1` (today, or a few days after a gap in runs);
   - dirty inside the live window (`Dd ≥ W`) → `live_from = min(S + 1, Dd)`: every point correct;
   - dirty before the window (`Dd < W`) → `live_from = min(S + 1, W)`: days `[Dd, live_from)` are
     the previous generation's (**stale**, PERF-D3) and the status says so.
4. Points before `live_from` from `settled_day`, **with their stored `net_worth`**; points from
   `live_from` to today from `value_series` + `daily_cash_series(start=live_from)` over books from
   one `iter_books` pass, and **only these live points** go through `compose_net_worth` — composing
   the whole series against a cash map that starts at `live_from` would read every stored day's cash
   as 0 (`networth.py:162-166`, final pass minor 1). `available` per §4; `options_value_excluded` is
   the OR of the stored days' `options_open` **before `live_from`** and the live days' flag (never a
   series-level stored flag, which would break byte identity in the inside-window case — final pass
   minor 5; `settle_meta` keeps none). A period close computed live (§11.4) ORs its live days the
   same way.
5. Benchmark: stored state iff `Dd` is ∞ (nothing pending, nothing claimed by a run), no flow is
   dated after `S`, and no corporate action on a benchmark storage key is dated after `S` (§4);
   otherwise the live counterfactual (reference order). The last close is always today's. This is
   what keeps the KPI band's 指數對照 差額 (= B − benchmark return, `dashboard.py:635-638`, shown at
   `web/app.js:480-490`) from mixing two versions of the ledger while a run is in progress.
6. **Internal consumers of history** get the stale range through an internal `DashboardData`
   field (`history_state`, `exclude=True` like `options_value_excluded` — never on the wire, so the
   payload stays byte-identical; the browser reads the same facts from `/api/settlement/status`).
   While `[stale_from, stale_to]` is set, `strategy/alerts.py`'s `portfolio_drawdown` (peak over the whole series,
   `:526-550`, which pushes a notification) is skipped with a note, and
   `insight_service._portfolio_return` (`:662-688`, which writes `insight_evaluations` and feeds
   promotion) returns its existing `pending_data` result when `[created, due]` overlaps the stale
   range (review A #6).
7. The router extras (spark_30d, alerts, llm quota, backup time, scheduler flag) are unchanged.

**Payload identity:** in the clean and inside-window cases the payload equals a full live build,
byte for byte (§18). In the stale case it intentionally differs on `[Dd, live_from)` and the status
(§15) carries `stale_from` / `stale_to`; the dashboard payload gains no field (PERF-D5).

Cost on a cold cache: the live part (0.05–0.12 s CPU locally) + 0.03 s to read the days + one
`iter_books` pass (≈ the cost of one `build_book`) + ≤ 62 days of valuation.

## 11. Month and year closes (WP4, PERF-D7)

### 11.1 Why the dashboard cannot simply be run "as of" a past day

`_build_dashboard` values holdings with `get_latest_price` and the FX resolver with `get_fx`, both
of which return **the most recent stored row regardless of `now`** (`pricing/store.py:291-316`,
`:414-436`); only staleness uses `now`. The existing 月度成績 snapshot is correct only because the
job runs on the real evening (`api/snapshots.py:36-71`) — and then it freezes at 23:50 on the last
day of the month, before the US close of that day exists (US quotes arrive at 16:30 New York, the
next morning in Taipei). So a period close needs an explicit "as at D" price source.

### 11.2 The KPI block of a period close (PERF-S7)

The KPI computation of `_build_dashboard` — step 1's cut of cash movements and FX conversions at
the valuation day (`dashboard.py:713-722`) and steps 2–4 and 10 (book, `price_map`, valuation,
returns, XIRR, the KPI blend, the currency view) — is extracted into one function
`dashboard.kpis_as_of(bundle, …, prices, fx, as_of)` called by the live dashboard (sources:
"latest stored", as today — payload unchanged, golden test) and by the run (sources: "on or before
D"). For a period ending on calendar day D:

| Field | Definition |
| --- | --- |
| book | `build_book(bundle.valued_as_of(D), allow_oversell=True)` (from `iter_books`); cash movements and FX conversions cut by `valued_rows_as_of(…, D)` |
| price of a held stock | latest stored close with `as_of_date ≤ D`, re-expressed by `price_in` for splits in `(priced_on, D]` |
| a held stock with no such price | exactly today's rule: its market value is `None` (`pnl.py:60-71`), it is left out of 總市值 and of the unrealized part of 總報酬 while its cost stays in the rate's denominator, and XIRR is `None` because XIRR alone is all-or-nothing (`returns.py:389-392`) — and the close **says so** (`notes` below) |
| option contract | its last ordinary trade price on or before D (R2, `last_trade_prices(bundle, D)`), refused adjustments excluded (`options.py:138-143`) |
| FX | latest rate on or before D (direct, else inverse — `_fx_at`); the rates used and their dates are stored |
| `total_market_value` | the stock grand subtotal at those prices + the options' value, as the live KPI |
| `total_return` (A), `total_return_rate` | as the live KPI, at D's FX — **since inception** (累計), not the period's return |
| `xirr`, `xirr_window_days`, `xirr_reason` | `xirr_reporting` over flows counting by D, terminal value at D, `as_of = D` — since inception |
| `by_currency_value` | `currency_view.by_currency_value` at D |
| `period_twr`, `period_twr_reason` | `I(D) / I(previous period end) − 1` on the full-series TWR index `/api/performance/twr` uses (`twr.py:64-123`); the first period from the index's anchor; `None` (with the reason) when either end day is incomplete, and `None` with reason 待結算 while history is stale for any period whose `(previous end, D]` contains `live_from` or overlaps `[stale_from, stale_to]` — the join between stale and live days would book a back-dated position's whole gain as one day's return (fold verification #2); excludes options like the trend (R3) |
| `notes` | stored: `incomplete` (bool), `missing_prices` (symbols), `stale_prices` (symbol + price date, older than the live 4-day maximum age before D, `pricing/store.py:22`), `unconfirmed_closes` (symbol + the price date used, for a day settled by the 4-day rule of §9.1 —
a close that is not missing and not 4 days stale, yet never confirmed by an `ok` quote run),
`missing_fx` and `stale_fx` (pair + rate date — the live dashboard flags both, `dashboard.py:1289-1302`; without a rate 總市值 and 總報酬 go blank and the reason must say so), `pending_positions` (the 待釐清 positions on the book at D: 賣超, unapplied action, refused dividend, refused close), `options_value_excluded` (the OR of `settled_day.options_open` over `(previous end, D]`, with the R3 sentence `TREND_EXCLUDES_OPTIONS_NOTE`, `shared/options.py:280`, in the tooltip — the trend leaves an open contract's value out while its premium is a `net_invested` flow, so 當期報酬 can count a whole premium as return; final pass #2) and `options_past_expiry` (contracts still open after their expiry at D); computed when the close is read: `dividends_receivable` (non-STOCK dividend rows with `ex_date ≤ D < date` — shown, never counted; a 配股 counts from its ex-date and is already in D's shares; on a stale row it is not shown — it would come from today's ledger beside KPIs from the previous generation — and the row's 更新前版本 mark stands instead) |
| FX P&L at D | not part of a period close in this release: the cumulative realized / unrealized 換匯損益 at D is left out (the KPI block covers the four 月度成績 figures; the FX pool stays a live figure) — stated so its absence is not read as zero |
| `trend_total_value`, `net_invested`, `net_worth` | the settled day D — the trend's figures (stocks only, a partial sum on an incomplete day — the trend excludes options, R3); named apart from the KPI `total_market_value` (options included, all-or-nothing on FX) so the two are never confused (review A #7c) |
| all JSON values | Decimal canonical strings (`money.to_db`), never floats |

A non-trading D (weekend, holiday, one market closed while another trades) carries the last close
forward, as every chart does today. Cash and DRIP dividends count on their payment date and a
配股 on its ex-date (`shared/models/ledger.py:100-111`) — the ruled convention the trend follows —
so a month ending between an ex-date and its payment date shows the price drop without the
dividend; `dividends_receivable` discloses it.

### 11.3 Period activity

Every line is in native currency **and** in the reporting currency at each row's own-date FX (the
trend's flow conversion). Rows count inside the period (first day … D) by the same dates the ledger
cut uses. **The lines are not additive**: under the adjusted-cost model a sale's realized P&L
already contains the cash dividends folded into that position's cost (`domain-ledger.md`), so
「已實現」 + 「股利」 is not "what the period earned", and no total of them is shown.

**Tie-out with the tax package, sheet by sheet** (fold verification #1): realized sales and short
covers (both the adjusted and the original column) ≡ `realized_gains_<year>.csv`; booked CASH /
NET / DRIP dividends ≡ `dividends_<year>.csv`; realized FX ≡ `fx_realized_<year>.csv`. The other
lines (fees, cash movements, FX conversions, openings, cash at D) have no tax-package counterpart.
The tax package replays **strictly** (`build_book(bundle)`, `export/tax.py:100`): every refusal —
no position, an open short, a vacated ticker — raises `UnbookableLedgerError` there and the export
answers 422 with the sentence (`cost_basis.py:1010-1050`), so a refused payout can never reach its
dividends sheet. The tie-out is therefore defined on every ledger the tax package can be built
for; on a ledger with a refused payout there is no package to tie to, and the period close lists
the refusal as 未入帳（待釐清）. (The fold verification read `export/tax.py:164-172` as listing
refused payouts as income; the strict replay above makes that unreachable — checked, not a
defect.)

| Line | Definition (the tax package's, `export/tax.py:121-200`, where it has one) |
| --- | --- |
| realized — sales and short covers | the book's realized rows of kind `sale` / `short_cover` dated in the period, vs **adjusted** cost (`realized`, ties to 總報酬, DEF-001) and vs **original** cost (`proceeds_net − original_cost_removed`, the capital gain); split by `asset_type` (stock / option) |
| realized — dividends after a full close | the rows of kind `dividend` (audit H2), listed separately because the same payout is also on the dividends line |
| dividends | booked rows only, by type: CASH / NET as cash (gross, withholding, net); DRIP as gross, withholding, net + reinvested shares; STOCK as shares only (no amount) |
| dividends not booked | refused payouts (`Book.refused_dividends` in the period): count and net amount, labelled 未入帳（待釐清） |
| trade fees and tax | `transactions.fees + tax` dated in the period, by account |
| cash movements | every kind in `shared/cash_kinds.py:66-72` — DEPOSIT, WITHDRAW, OPENING, REBATE, INTEREST, INTEREST_EXPENSE, BROKER_FEE — by account × currency |
| FX conversions | the period's conversions by account (from / to amounts) and the period's realized FX (`realized_fx_rows_as_of`, the 換匯損益 owner) |
| opening inventory | rows with `build_date` in the period |
| cash at D | per account × currency, the end-of-day balance at D (`cash_balances(…, as_of=D)` fed with **booked** dividends only — `booked_dividends(…, book.refused_dividends)`, as the dashboard does at `dashboard.py:1154`, so a refused payout never lands in month-end cash) and its reporting value, so cash ties to each broker's month-end statement as positions do |

Holdings at D (`settled_period_holding`): per account × symbol, shares, original and adjusted cost
totals, the price used and its date, market value and unrealized P&L in native currency, and the
待釐清 flags — stocks and options both, tagged by `asset_type` (OPT-D5 keeps them apart wherever
they are listed). Cash at D gets its own table, `settled_period_cash(generation, kind, period,
account_id, ccy, balance, reporting_value, fx_rate, fx_as_of)`.

### 11.4 What changes on the page

- **Which rows come from the store** follows §5 and §10: a period whose end day D is before
  `live_from` comes from `settled_period`; a period whose end day is on or after `live_from` (the
  month just ended, before the nightly run; a month touched by a change inside the live window) is
  computed live with the same `kpis_as_of` — so a correction dated last month changes last month's
  row at once (a test pins it).
- **暫定** marks a past period whose end day D is after `final_through` (§9.1, the same rule that
  caps the nightly extension), with its own note 「月底收盤價尚未確定：<market> 報價工作未完成」. It
  depends on the price data, not on the store: a month is not 暫定 merely because the store is
  unusable, `PD_SETTLE=off`, or a partial publish moved `settled_through` back. A month whose US
  close failed to arrive stays 暫定 for up to four days instead of being settled on an intraday
  bar; after that it is settled with the close in `notes.unconfirmed_closes` and the row's ⚠ says
  「收盤價未經確認：…」 (final pass #1, fold verification 2 #1, final check #1).
- **Stale rows are marked per row.** A row whose end day lies in `[stale_from, stale_to]` carries
  `stale: true` on `/api/snapshots` and `/api/settlement/periods*` (year rows included) and shows
  「更新前版本」 in the row itself, not only in the panel banner.
- `GET /api/snapshots` keeps its row fields (`month, as_of, reporting_ccy, total_value,
  total_return, total_return_rate, xirr, by_currency`) and gains **optional** ones (`kind`,
  `provisional`, `stale`, `notes`, `xirr_reason`, `period_twr`, `period_twr_reason`, `restated`) — additive
  on this route only (PERF-D5 concerns the dashboard payload); a blank XIRR or TWR always shows its
  reason in the tooltip. Rows: the current month from live KPIs (`as_of` = now), past months as above
  (`as_of` = D). `total_value` keeps its meaning (`kpis.total_market_value`, options included).
  When the store is unusable (calc-version mismatch before the boot run finishes, `PD_SETTLE=off`)
  the past months shown are computed live with `kpis_as_of` — slower, never stale and never
  missing (review A #7d).
- A row whose `notes` is not empty shows a ⚠ whose tooltip names the reason (「缺價格：…」,
  「待釐清：…」, 「已除息、股利未入帳：…」).
- The panel's columns become 月份 · 總市值 · 累計總報酬 · 累計報酬率 · 累計 XIRR · 當期報酬（TWR）, so
  a year row is never read as that year's own return.
- The subtitle 「每晚快照當月 KPI，月底定格 — 查表即得，不重算歷史」 (`web/index.html:226`) becomes
  「過去月份為月底結算值：帳本、價格或匯率更正，或計算規則更新並重新結算後會跟著更新；本月為即時值」.
  The empty state no longer mentions 23:50. When today's book carries a 待釐清 position, one note
  under the table says earlier months may change once it is resolved.
- A 月／年 toggle (Q5) switches between the 12 latest month rows and the year rows (current year
  live).
- The `snapshot_monthly` job leaves the registry; `settlement_nightly` takes its place.
  `portfolio_snapshots` is no longer written or served; it stays in the database only, as the
  record of the old frozen values. Tests that ran `snapshot_monthly`
  (`tests/e2e/test_scheduler_status_flow.py` and the scans listed by the jobs survey) move to
  another job.
- **Restatement is expected, and happens once at the cutover.** Every past month moves to the
  value today's ledger gives for its last day: the 23:50 freeze used the previous day's US close,
  and prod's months before the 2026-10-13 import reflect an empty ledger. The whatsnew entry and
  the release note say so.

## 12. The result cache (P1) and shared reads (P2) (WP5)

### 12.1 The cache

`shared/result_cache.py` (the generalisation of `flight.py`; in-flight sharing stays):

- **Key** `(name, db_path, write_generation, data_version, now.date(), reporting, extra)`, where
  `write_generation` is `flight.write_generation()` and `data_version` is `PRAGMA data_version`
  read on **one dedicated, never-writing connection per database** held by the cache (lock-guarded).
  SQLite changes that value whenever any OTHER connection commits — another request, a job worker,
  the settle worker, a script, the stress-audit harness writing SQLite directly — so no write is
  missed. (A file signature `(st_mtime_ns, st_size)` was rejected: after a checkpoint the `-wal` is
  reused at the same size, and two commits inside one timer tick leave the mtime unchanged; a quote
  refresh commits prices, FX and the derived MYR/TWD rate milliseconds apart,
  `pricing/refresh.py:151-159` — review A #3.) The version is read BEFORE the computation's read
  snapshot opens, so a commit between the two can only make an entry newer than its key, never
  older. `None` (no caching) while a write window is open, when the connection is in a
  transaction, or for an in-memory database — the rules of `_flight_key` today
  (`dashboard.py:678-696`).
- **What is cached:** the `DashboardData` of `build_dashboard`; the alerts result of
  `compute_alerts_full` (including its 400-day inputs); the TWR overlay per `(benchmark, window)`;
  the whole-ledger dividend refusals `symbol.py:879` replays.
- **Entries:** the latest one per name; the key is recorded at compute START, so a write or an
  external commit during the computation makes the entry unreachable.
- **Copies:** every reader receives a deep copy (0.06 s locally for `DashboardData`), as `flight`
  does today. Sub-day fields are set per request after the copy (`as_of = now`).
- **Pre-warm:** after a settlement swap and ~3 s after the last write window closes, a background
  thread computes today's dashboard (and alerts) into the cache.

### 12.2 Routes

| Route | Change |
| --- | --- |
| `GET /api/dashboard` | cached build + cached alerts |
| `GET /api/alerts` (the bell on 7 pages) | cached alerts |
| `GET /api/symbol/{s}/detail` | cached build + cached refusals; no second replay |
| `GET /api/performance/twr` | cached build; cached overlay per (benchmark, window); benchmark closes from the first flow date |
| `POST /api/whatif` | **read-only POST**; reads the cached build instead of `build_dashboard` in `_new_weight` (`whatif.py:486`); one `RateResolver` (`:502`, `:512`); the drawer keeps its automatic first 試算 300 ms after render — it now costs a cache hit, not a build |
| `POST /api/rebalance/preview` | read-only POST; cached build |
| `POST /api/prompts/preview` | read-only POST; cached build (`/api/prompts/test` calls the LLM and writes usage — unchanged) |
| `POST /api/insight-tasks/{id}/preflight` | read-only POST if the survey in WP5 confirms it writes nothing; cached build |
| `GET /api/insight-tasks/status` | cached build |
| exports, `PUT /api/alert-rules`, digests, insight runs, news | unchanged code; they get a settled build (or a cache hit when outside a write window) |
| corporate-action form, trades-page option symbol | read option terms from `/api/input/option-symbol` instead of `/detail` |

A **read-only POST** is declared by a route marker; `_write_window_middleware` (`app.py:315-325`)
skips it, so it neither opens a write window nor disables sharing. A contract test runs every
marked route under a held write lock with the DEF-065 SQL trace: any write fails it.

## 13. Transport and frontend (WP6)

- **GZip** — Starlette's `GZipMiddleware` (installed starlette 1.3.1; no new dependency),
  `minimum_size=1024`; ZIP downloads (`application/zip`, `export/artifact.py:89`) excluded.
- **Static caching** — a request for a local asset whose `?v=` equals `__version__` gets
  `Cache-Control: public, max-age=31536000, immutable`; HTML and unversioned requests keep
  `no-cache` (`_NoCacheStaticFiles`, `app.py:131-153`). The stamper already versions every local
  reference (`scripts/stamp_asset_version.py`); `tests/contract/test_static_cache_discipline.py`
  is updated to assert both policies.
- **echarts** — `defer` on `index.html:13`, `insights.html:13`, `settings.html:14`; chart init
  waits for it; the drawer's lazy load (`shell.js:209`) unchanged.
- **Parallel chains** — cash (3 steps), settings prompts (6), scheduler (3), the index's cash and
  月度成績 (no longer wait for the dashboard).
- **Duplicates** — trades (`/input/context`, `/llm/config` twice), settings (prefs 3×), inbox badge
  vs list.
- **`web/api.js`** — (a) a `{background: true}` option excluded from the `pd-net` counter
  (`api.js:112-126`), used by the bell, status polls and pre-fetches, so the top bar tracks only the
  page's own content; (b) identical concurrent GETs share one promise; (c) a request still pending
  after 15 s shows 「仍在計算中…」 in the top bar (PERF-D6) — no abort; (d) after every successful
  non-GET it dispatches `pd-mutated` (the one seam the prompt listens to — no path list).

## 14. User experience (WP6)

### 14.1 The status chip (every page)

Inserted after `.header-spacer` (`shell.js:375`) so it shows on every page regardless of
`data-chips`. States: 已結算 (muted, ✓) · 待結算 · 2025-06-16 起 (amber) · 結算中 58% (accent, pulse)
· 準備中 (accent) · 結算中斷 — then 「已續跑」 or 「未續跑」 with the reason, derived from the run's
state (amber) · 結算失敗 (red) · 自檢發現差異 (red, §9.1 weekly self-check). No chip text is fixed;
each is derived from the status read. A state colour is a class with a stylesheet rule,
never an inline style (DEF-077).

### 14.2 The detail panel (PERF-D10)

A click on the chip opens the panel (desktop: a side panel; phone: a full-height sheet). Its
sections are collapsible and remember their state per browser; nothing in it is a summary of
something it does not also show.

**While a run is active** — every field of the progress file (§9.5), live:

1. **Header** — 「重新結算：2025-06-16 至 2026-10-17，共 489 天」 · 「由你於 10:32 啟動（交易更正）」
   (「你」 only for the user who started it; otherwise the login name, a job, boot or the nightly
   rule) · 「已用 12 秒 · 預估還要 8 秒」 · the overall bar and percent · 「背景低優先序」.
2. **Why it runs — the changes being settled** — per table (「交易 2 筆，2025-06-16 至 2025-06-18」;
   prices and FX per symbol / pair, 「價格 3 檔 14 筆，2025-07-01 至 2025-07-21」) and **every**
   changed record (table, 新增 / 修改 / 刪除, record, account and symbol old → new, 日期 2025-06-18 →
   2025-06-16, import batch, 使用者 / 排程 / 系統（啟動） / 外部程式／腳本, 寫入時間), paged 50 at a time, never
   truncated; 「曾於 #411 嘗試」 where a run that did not publish had claimed it.
3. **Steps** — the 8 phases, each with ✓ / ▸ / ·, start time, elapsed, its count
   (「逐日估值 281 / 489 天 · 35 天/秒」) and its detail line (§9.5).
4. **Current item** — the day being valued and the number of positions valued on it.
5. **Still to do** — the pending phases with their expected durations, and 「完成後接著處理：
   2026-09-03 起的變更（1 筆）」 when a change is queued.
6. **Validation** — phase 2's findings as they appear.
7. **Estimate** — the first estimate, the current one, and `eta_basis` in words.
8. **Versions** — 第 41 版 → 第 42 版（切換後生效）; settled through before → after; calc version and
   reporting currency, each before → the run's (marked when they differ).
9. **Actions** — 取消（保留目前版本）, with the consequence spelled out (`on_cancel`).

**When no run is active** — the panel opens on **the last run's full report** (§9.6), every
section expanded: summary, range, versions, changes settled, validation, steps with durations,
result (history changes, restated closes, state), and its failure reason if it failed. When the
last run settled no change (at 09:00 it is usually the uneventful nightly extension), a second card
right under it shows **the last run that settled changes**, also in full, so the most recent
correction and its effect are one click away. Above it, the
current state: 「已結算至 2026-10-17 · 第 42 版」, or 待結算 with the pending changes listed exactly as
in section 2 above and the 立即重新結算 button, or the stale note 「歷史走勢與月度成績尚未包含
2025-06-16 起的變更」, or 重試 after a failure. The 重新結算 button (§14.4) is always there.

**History** — below the last report, every kept run (§9.6 retention, newest first, paged 20 at a
time) with date, trigger, range, duration and result; a click opens that run's full report in the
same layout, with 「上一筆 / 下一筆」 to step through and the chain links (「接續自 #411」,
「接著處理 #413」; 「已超過保留期限」 for a pruned one). A filter hides the nightly runs that settled
nothing. The panel reads the reports from the database, so they survive a reload, a restart and a
deploy.

Polling: once at page boot; every 1 s while the panel is open and a run is active; every 3 s while
only the chip shows a run; none when idle (the scheduler centre's pattern,
`web/settings-scheduler.js:406-430`). All status requests are `{background: true}`.

### 14.3 The prompt (PERF-D2)

On `pd-mutated` (debounced 800 ms) and at page boot, the shell reads the status in the background.
`needs_prompt` is computed by the server: true when a change older than the live window is pending
(`dirty_from < W`) and no run is active or queued for it — a change is **system-handled** exactly
when an automatic run (§9.1) is active or queued, and then the chip shows that run instead. When
`needs_prompt` is true and this browser has not been asked about this `seq` yet (`sessionStorage`),
the shell shows `confirmDialog`:

> **要重新結算歷史資料嗎？**
> 這筆變更影響 2025-06-16 起的歷史結算（共 489 天；待結算：交易 2 筆）。重新結算約需 10 秒，以背景
> 低優先序執行，期間可以繼續操作。選「稍後」會先顯示更新前的歷史走勢與月度成績，並標示「待結算」；帳戶數字（總市值、報酬、持倉）
> 已是最新。
> [立即重新結算] [稍後]

「稍後」 leaves 待結算 on the chip (and the nightly run settles it, Q4). A change inside the live
window shows no prompt — it is already reflected.

### 14.4 重新結算 replaces 重算

The top-bar actions menu item 重算（重建統計） (`shell.js:544-561`) becomes 重新結算（驗證並重建歷史）,
and the panel carries the same button. It starts a full run, whose phase 2 (§9.3) lists every
待釐清 record of the tolerant replay; without reloading the page, charts refresh when the swap is
announced. A finding is shown as a warning toast and in the panel and **no longer stops anything**
— today's strict check stops at its first refusal on a replay that changes nothing; with a store it
would leave the history unsettled for a state the dashboard already values as 待釐清. `POST
/api/actions/recompute` stays unchanged for its existing callers and tests; the menu no longer
calls it.

### 14.5 The stale banner (PERF-D3)

On the trend, net-worth, 績效比較 and 月度成績 panels, while `stale_from` is set: 「更新前版本 ·
尚未包含 2025-06-16 起的變更 · [查看結算]」. On 績效比較 the text adds 「期間報酬可能失真」, because
the join between the stale days and the live days can book a back-dated position's whole gain as a
one-day return (review C #2). The KPI band never shows it — it is always live.

### 14.6 Restated closes

A swap that changes a published period close records each changed KPI field in
`settle_restatement` — its zh label and unit (`by_currency_value` is in each currency's own units,
the totals in the reporting currency, XIRR and the rates are ratios), old and new value, the notes
before and after, the run, its reason, the earliest change source — under the comparison rules of
§9.6 (both generations hold the period, same reporting currency, inside the published range; a
currency change writes no restatement rows; a calc-version change does, with reason 計算規則更新).
月度成績 marks such a row 「已重述」; its tooltip shows
the previous value, the date and why (「2025-06-16 交易更正後重新結算」). The cutover itself (§11.4)
is recorded once per row with `cutover = 1` — its old values come from `portfolio_snapshots`, and
only for snapshot rows whose `reporting_ccy` is the configured one (a row in another currency is not
compared, by the same currency rule) — and shown with a distinct, quieter mark 「上線重述」, so it does not make
every month look corrected forever. Restatement rows are never pruned.

## 15. API

| Route | Purpose |
| --- | --- |
| `GET /api/settlement/status` | `{state: clean|pending|preparing|running|interrupted|failed|selfcheck_diff, generation, settled_through, calc_ok, dirty_from, live_from, stale_from, stale_to, needs_prompt, prompt: {from, days, est_s}, pending: {tables, records_total}, final_through, run: <progress file or null>, last_run: <its report summary>, last_change_run: <the last run that settled changes>}` — reads only; the records themselves come from `/api/settlement/changes` |
| `GET /api/settlement/runs?limit=&offset=&only_changes=` | the kept run reports' summaries, newest first, `{rows, total_count}` |
| `GET /api/settlement/changes?state=pending|run:<id>&limit=&offset=` | the change trail — pending changes, or the changes a run claimed / settled / handed back — every record, paged, `{rows, total_count}` |
| `GET /api/settlement/runs/{run_id}` | one run's full report (§9.6); 404 when pruned |
| `GET /api/settlement/runs/{run_id}/diff?field=&kind=&limit=&offset=` | the run's per-day changes (`settle_run_diff`), paged — a calc-version run can write ~8,700 rows |
| `POST /api/settlement/run` | body `{scope: "dirty"|"full"}` → 202 `{run_id}`; 409 `already_running` (the chained queue is automatic) |
| `POST /api/settlement/cancel` | 200; 409 when nothing runs |
| `GET /api/settlement/periods?kind=M|Y&limit=` | the period closes (KPI + activity), newest first |
| `GET /api/settlement/periods/{kind}/{period}` | one close with its holdings |

Every mutating route has an action-log label (`tests/contract/test_action_log_labels.py`): 「重新結算」,
「取消重新結算」. zh error messages; `PdApiError` shape unchanged.

## 16. Process and VM (WP7, WP8)

- **Lazy imports.** `shared/llm.py` imports `litellm` inside the functions that call it;
  `pricing/providers/yfinance_provider.py`, `sentiment_source.py`, `index_source.py`,
  `consensus_source.py`, `fundamentals_source.py` import `yfinance` inside their fetchers. Measured:
  the app's import 252 MB / 22.8 s → 58 MB / 7.6 s. A test boots the app and walks every route in the
  contract suite with stub providers: `litellm`, `yfinance` and `pandas` stay out of `sys.modules`
  except in the tests that exercise the interactive heavy calls (PERF-S13).
- **Job workers (PERF-D4).** Scheduled jobs run in a child process
  (`python -m portfolio_dash.api.job_worker <job_id> <run_id>`), which binds the same injection
  seams as the app — moved from the lifespan into one `api/composition.py::bind_runtime()` called by
  both — and runs the job through `run_job_outcome`, so `job_runs`, `usage_tally` and JobOutcome are
  unchanged. Progress goes to a per-run sidecar the web process merges into `running_progress()`. A
  child that dies without finishing its row is finalised by the parent as `error`
  (「工作子程序異常結束」). At most one heavy child at a time (§9.1). A test runs every job in the
  registry in a child against a temp database and compares its outcome with the inline run.
- **Environment** (both units): `LITELLM_LOCAL_MODEL_COST_MAP=True`, `MALLOC_ARENA_MAX=2`.
- **systemd:** prod `CPUWeight=1000`, demo `CPUWeight=100` and `MemoryHigh=` (value set in WP8 from
  measurements) so the demo is reclaimed first; journald `SystemMaxUse=50M`; `python -m compileall`
  at deploy.
- **zram** (PERF-D8): a zram device as the first swap (lz4/zstd, ~50% of RAM), the disk swap kept
  at lower priority; `vm.swappiness` set for zram (WP8 measures).
- **Off-peak heavy work:** `settlement_nightly` at 06:40 TPE; the weekday 14:00–15:30 cluster
  unchanged in time, cheaper by the store.
- Every VM change goes through `scripts/vm_exec.py` (logged) and is written into the git-ignored
  host note; `docs/DEPLOYMENT.md` gets the placeholders.

## 17. Rule and document changes (with the release)

- `architecture.md`: the `flight.py` paragraph becomes the result cache; new module
  responsibilities (`portfolio/settlement_store.py`, `portfolio/settle_worker.py`,
  `api/settlement_service.py`, `api/composition.py`, `api/job_worker.py`); the read-only POST
  marker; `get_conn` commits before the response.
- `data-and-pricing.md`: a "settlement store" section (derived, rebuildable, triggers, generations,
  calc version, the live window, PERF-S7's valuation).
- `domain-ledger.md`: the period-close definition (§11.2–11.3) as a computed-figure rule.
- `docs/accounting-formula-manual.md` (+ `.en.md` regenerated): the month / year close formulas and
  a verification anchor (ship-version item 9).
- `shared/whatsnew.py`: entries for the status chip and panel, the prompt, 重新結算, 月度成績's new
  meaning and the 月／年 toggle.
- CHANGELOG: the owner's sign-off on PERF-D1 and PERF-D7; every behaviour change.

## 18. Verification

| Layer | Test |
| --- | --- |
| Engine | for every corpus (golden fixture, demo synthetic corpus, the stress-audit scenarios, the doubled synthetic ledger), every event day: `iter_books` ≡ `build_book(bundle.through(day))`; `value_series` ≡ `daily_value_series`; the exactness guard's fallback forced and equal |
| Prefix property | random mutation sequences (insert / update / delete on every trigger table, back-dated and current, price restatements, splits, unregistered → registered): `dirty_from` is **≤** the earliest day whose fresh value changed (never later — the exclusion lists over-fire on purpose); no fresh point before `dirty_from` changed |
| Store | run → read ≡ full live build (byte-identical payload); interrupted run (kill the child in every phase) → old generation intact → resume ≡ fresh; change committed before the claim, between claim and snapshot, during the computation and during the swap → partial swap + chained run ≡ fresh, never a stale day published as settled; while a run computes, readers still serve the claimed range live or labelled (a correction shown live never reverts); failure, cancel and boot cleanup restore `dirty_from` to the earlier of it and `run_from`; cancel after the swap leaves the published generation intact; an orphan child (outliving a restart) cannot publish; calc-version change; completeness assertion at the swap |
| Read path | the three `live_from` cases; the stale case reports `stale_from/stale_to` and differs only there; `available = false` and `options_value_excluded` preserved; each bounded load (first ledger day, first flow day, `live_from`) on a weekend, a holiday and a suspended / illiquid symbol keeps the carried-forward value; the benchmark falls back to live after a flow, a pending range, **a range claimed by a running run** (the A1 case), or a corporate action on a benchmark key dated after `settled_through`; internal consumers (drawdown alert, insight scoring) defer while stale; `period_twr` is `None` (待結算) across a stale join |
| Period closes | each month-end KPI ≡ an independent "as at D" computation in the test tree (the reference engine with on-or-before price/FX sources); on a complete day `total_market_value − options value ≡ trend_total_value` at D (by design it may differ from the live KPI seen that evening, §11.1); the year row's KPIs ≡ the December row's; for **flow** lines, year activity ≡ Σ its months (balances — cash at D, holdings — are compared with the December row instead); the sheet-by-sheet tie-out with the tax package on every corpus it builds for (§11.3); year TWR ≡ Π(1 + month TWR) − 1 when every inner month end is complete (otherwise Π is undefined while the year TWR still is — the test skips it); realized amounts are quotients, so Σ months ≡ year is asserted in the replay's own row order or within one minor unit per row; a quote job ending `partial` for the month-end day keeps the row 暫定; an open option in the period sets `options_value_excluded`; a missing or stale FX rate at D appears in `notes`; a missing price, a stale price, a 待釐清 position and a dividend between ex-date and pay date each appear in `notes`; a correction dated last month changes last month's row at once; a split dated the first trading day after month-end (and one dated on a weekend D, and on D) leaves the month-end value unchanged; the stress-audit oracle (`scripts/stress_audit/oracle.py`) gains month-end values and its phase 1 compares them |
| Triggers | every writer in the survey (store functions, reconcile, cross-rate derivation, seed, child seed, batch undo, moomoo merge, scripts with raw `sqlite3`) fires them; unchanged restamps, unchanged `UPDATE OF` columns and `seed_accounts` at boot do not; a row moved to a LATER date on every ledger marks from its OLD date; OLD and NEW symbols both checked; a SPINOFF child's backfilled prices and a registered `to_symbol` fire; the dividend trigger's date ≡ `Dividend.effective_date` for every type; NULL `settled_through` / `run_through` / `ex_date` / `dirty_from` handled; an UPDATE moving a price or FX row to another symbol, pair or day fires; a dropped trigger makes the store unusable (via `schema_version`) and its recreation outside a calc-version change marks everything dirty; `PD_SETTLE=off` keeps the triggers; the actor TEMP trigger exists only on mutating-request and job connections (never on a GET connection) |
| Run report | every end (published, partial, superseded, cancelled, failed, interrupted → resumed) leaves a report whose sections match §9.6, and 完成 appears only with every row published and the completeness assertion passed; an interrupted run keeps the phases it finished and the resumed run links to it; the change trail is never truncated (an import of 1,000 back-dated rows interrupted by a restart: all 1,000 are pending again and named by the resumed run) and every change ends settled by exactly one run; a partial publish settles only records dated before *N*, hands the rest back, keeps G's rows for `[N, held_through]` and restates only inside `[range_from, N − 1]`; a superseded run moves nothing; the put-back of a price counter merges with a pending counter of the same symbol; the history result equals an independent diff restricted to days both generations hold in the same currency — a nightly extension reports 「新結算 1 天」 and no "change"; value ↔ none and incomplete flips are their own category; a currency change reports no value diff and writes no restatement; the restated closes equal the run's `settle_restatement` rows with label, unit and notes; `on_cancel` / failure sentences match the actual state (generation 0, unusable store, no claimed change); 「你」 renders only for the starter; an actor is stamped for requests and jobs and NULL for a raw `sqlite3` writer; liveness follows the OS lock (a live but silent child is never 已中斷); reports survive a restart; pruning keeps the latest 400 and never a restatement; the panel opens on the last report (and the last change-settling one) after a reload and steps through the history (e2e) |
| Final-day and lifecycle rules | `final_through`: a weekend, a day before go-live, every day on a scheduler-off instance and a day with the market's job disabled qualify; a weekday whose US job ended `partial` does not, until it is 4 days old (then settled with the missing close in `notes`); a permanently unquoted held symbol never blocks settlement for more than 4 days; boot with an identical trigger set re-settles nothing and leaves `schema_version` unchanged, a missing or altered trigger is recreated and marks everything dirty; a run whose child dies before claiming still has a report (準備中 → 失敗) and a status read during start-up shows 準備中; `cancelled_by` recorded; the boot connection's writes carry `system:boot`; a self-check difference maps to `job_runs` `partial` / `selfcheck_diff`; every changed day of a run is in `settle_run_diff` (incl. `new_day` / `dropped_day`) and pages in the panel; a calc-version change writes a diff and restatements (reason 計算規則更新), a currency change neither; the cutover restates only snapshot rows in the configured currency; `held_through = max(previous, N − 1)` after a partial publish that also extended days |
| GET never writes | DEF-065 test extended to `/api/settlement/*` (status, runs, one run, changes, periods); read-only POST test |
| Cache | a commit from another process (job worker, settle worker, script, the stress-audit harness writing SQLite directly), including two commits within one millisecond and after a WAL checkpoint → miss (`data_version`); write window → bypass; a commit between the version read and the snapshot never yields an entry older than its key |
| UI (Playwright, ≤ 11-test batches) | chip states; panel content and polling; prompt on an old edit, none on a recent one; 稍後 → 待結算 + banner; 立即重新結算 → progress → swap → charts refresh; cancel; 重新結算 with 待釐清 findings lists them and still completes; 月／年 toggle; top bar not held by the bell; 15 s notice |
| Process | worker `sys.modules` lean; every job in a child ≡ inline; a dying child finalises `error` |
| Budgets (harness, WP1) | on the prod-shaped scratch ledger: settled cold build ≤ 0.5 s CPU; cache hit ≤ 0.1 s; full run ≤ 5 s CPU; per-route call-count ceilings (≤ 1 build per page flow) |
| Stress audit | phase 1 `fail=0` with the store on (the harness runs a settlement after its writes and also asserts the stale state before it) |

## 19. Work packages (one release, `perf/settlement-store`)

| WP | Content | Gate |
| --- | --- | --- |
| WP1 | Measurement harness in `scripts/perf/` (from the scratch benches, DB path as an argument, never committed data); baseline numbers; the reference implementations moved into the test tree as oracles | harness reproduces the 2026-10-18 numbers |
| WP2 | Engine (§8) + P6 light reads + FX history once | engine equality on every corpus; stress audit phase 1 green |
| WP3 | Store, triggers (with the change log), generations, calc version, the run (child, phases, progress file, run reports, cancel, resume, chain), `PD_SETTLE` kill switch, `get_conn` scope | prefix-property, store and run-report tests |
| WP4 | Read path (§10), `kpis_as_of`, period closes, `/api/snapshots` from the store, `settlement_nightly`, `snapshot_monthly` retired | golden unchanged; period-close tests; stress-audit oracle extended |
| WP5 | Result cache, shared reads, read-only POSTs, pre-warm | cache tests; call-count ceilings |
| WP6 | Transport, `api.js` options, chip, panel (live detail, last run's report, run history), prompt, banner, 重新結算, 月／年 toggle, 15 s notice | e2e batches; static-cache contract |
| WP7 | Medium routes (`/signals`, `/input/context`, `/instruments`, `/ledgers/*`, `/cash`, inbox detection), lazy imports, job workers, composition root | SQL-count ceilings; lean-import test; jobs-in-child test |
| WP8 | VM tuning, staged deploy (backups, demo then prod), VM before/after, docs, CHANGELOG, whatsnew, lessons; then delete the two scratch DBs (PERF-D9) | ship-version Part A–C; before/after report to the owner |

Heavy gates (full pytest, mypy, e2e) run once per round after the final review, e2e in foreground
batches of ≤ 11 tests.

## 20. Risks

| Risk | Mitigation |
| --- | --- |
| A stale number that looks right | money figures always live; triggers at the database level; calc version; generation swap; byte-identity tests; the stale state labelled on every history panel and in the status |
| An input the triggers miss | the 12 tables a build reads were taken from a SQL trace of a real build; the trigger test scans every writer; the weekly `settlement_selfcheck` recomputes everything and records any difference in `job_runs` |
| Engine divergence from `build_book` | per-event-day equality on every corpus; the reference kept as oracle; the kill switch |
| Child processes on 1 GB | one heavy child at a time; lean worker imports; nice / SCHED_IDLE; measured in WP8 |
| SQLite contention (child writes) | month-sized chunks, each a short transaction; the swap is one small transaction; WAL |
| `get_conn` scope change | no route streams a response and the two background tasks open their own session (`instruments.py:620`, `:639`, `:782` — `gap_backfill`); the full suite runs |
| The prompt annoys | only for changes older than the live window, once per change sequence per browser session |
| A pre-existing price-basis edge surfaces in month closes (review B #7, unverified) | `upsert_prices` takes the split window `(as_of, fetched_at]` on the Taipei date of the fetch (`pricing/store.py:260-263`, `shared/clock.py:20-22`); a fetch made on the ex-date's Taipei date before the provider restates its history might multiply D's as-traded close by the ratio until the next night's sweep refetches it. WP2 reproduces it with a fixture; if confirmed it is filed and fixed as its own defect (it affects today's trend too), with the period-close split tests in §18 |
| A pre-existing trend edge (final pass #9, unverified — PERF-F2) | the trend loads FX only for the ledger's quote currencies, so an XIRR-kind cash movement in another currency could make the whole trend unavailable although the rate is stored; WP2 reproduces it; if confirmed it is reported to the owner and fixed as its own change (the fix changes output) |
| A US month-end close arrives the next morning in Taipei | the month row is 暫定 until `final_through` passes D (§9.1, §11.4); `history_daily` at 02:00 TPE may store the still-open US session as D's bar, which the 05:30 quote job and the trigger then correct; a close still missing after four days is settled with a note |

## 21. Questions for the owner (with the recommended answer)

| # | Question | Recommended |
| --- | --- | --- |
| Q1 | Changes dated in the current or previous month take effect immediately without a prompt; only older changes prompt (PERF-S3, refines PERF-D2) | Yes |
| Q2 | Changes that no person made — a price backfill or correction, a release that changes a calculation, resuming an interrupted run — re-settle automatically without a prompt | Yes |
| Q3 | A change made while a run is active is chained automatically after it | Yes |
| Q4 | A change you deferred with 「稍後」 is settled by the nightly run at 06:40 | Yes |
| Q5 | 月度成績 gets a 月／年 toggle showing the year closes | Yes |
| Q6 | No ETag on the dashboard this release (its 資料時間 would show an older time on a 304); gzip only | Yes |
| Q7 | Interactive AI and quote buttons keep loading their libraries into the web process on first use; WP8 measures how long the memory stays used before deciding anything further | Yes |

## 22. Review before owner acknowledgement

Per the owner (2026-10-18): independent subagents, briefed as an investment accountant with 30
years of securities-trading experience, check that the cost, P&L, dividend, FX and return figures
keep their sources and their calculation logic under this spec — practical bookkeeping, not
accounting standards. Their findings and the folds are recorded in the revision log.

## Appendix A — measurements (2026-10-18, local, prod-shaped scratch ledger)

| Item | Value |
| --- | --- |
| Full build | 8.47 / 8.45 s wall, 3.69 / 3.53 s CPU |
| Settled simulation | 0.19–0.26 s wall, 0.05–0.12 s CPU; payload byte-identical |
| Doubled ledger | full 21.31 s wall / 9.84 s CPU; settled 0.27 s / 0.17 s |
| Read 2,179 settled days | 0.03 s |
| Payload | 439.1 KB: trend 348.5, realized 35.0, holdings 20.3, options 19.6, alerts 9.7 |
| Profile (cProfile share) | `daily_value_series` 81.7% (`build_book` × 463 54.1%, `through` × 461 5.7%, `counts_by` × 2,774,053 4.6%, `_entry_at_or_before` × 73,906 4.4%); `get_price_history` × 87 11.2%; `_benchmark_comparison` 2.5%; cash + net worth 1.9%; `xirr_reporting` 1.0% |
| Tables read by one build (SQL trace) | `fx_rates` 1,059 queries, `prices` 105, `dividend_events` 17, `transactions` 2, `dividends` 2, `fx_conversions` 2, `opening_inventory`, `corporate_actions`, `instruments`, `accounts`, `account_market_rules`, `cash_movements` 1 each |
| Import RSS | app 252 MB / 22.8 s; without litellm + yfinance 58 MB / 7.6 s; litellm alone 163 MB; yfinance alone 75 MB; calc core 22 MB |
