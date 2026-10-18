# Integrated performance plan — the settlement store and four layers

**Date:** 2026-10-18 · **Base:** v0.1.31 (`43730cd`) · **Branch of record:** `perf/settlement-store`
**Builds on:** `docs/reports/2026-10-16-prod-performance-audit.md` (the audit and P1–P7).
**Implementation spec:** `docs/spec/2026-10-18-settlement-store.md`.

**Owner proposal (2026-10-18, summarised):** every chart, trend and ledger figure gets yearly and
monthly closing results. A completed year or month no longer changes — its market moves and trades
are final — so pages read the closed results instead of recomputing from the raw records. Only a
change (insert / edit / delete) to the underlying records of a closed period requires a
re-settlement, which runs as its own process with a visible processing status, a detailed progress
bar and text naming the item being processed; an interrupted run only leaves the store with an
old/new split, fixed by running it again. Can this replace the top-bar 重算 button, with editing an
old record prompting to start it? Is it the best approach, can it run together with P1–P7, and what
is the complete, all-at-once plan, given the ledger will double — with no hardware change but VM
tuning allowed?

## Verdict

The settlement-store direction is right and is the core of the plan. Measured on a prod-shaped
scratch ledger:

- Serving the history-dependent parts from a store cuts the dashboard build **8.47 s → 0.19–0.26 s**
  (30–45×), with the payload **byte-identical** to a full build.
- **"A closed period does not change" holds.** A row added or changed on day D changes no trend
  point before D (4 kinds of change, 0 points changed before D).
- **Doubling the ledger** makes today's build 2.5× slower (8.5 → 21.3 s) and the settled build
  barely moves (0.19 → 0.27 s).

The store fixes *computing too much*. Slowness has four more sources — the same page triggering
the same work several times, transport (no compression, no static caching), memory (swap), and
network round trips — so the plan keeps all of P1–P7 and adds five refinements to the proposal:
daily granularity with month/year rollups; detecting every source that can change the past, not
only the ledger; switching to a new version only once it is complete (never a mixed old/new
series); money figures always computed live, only history read from the store; and an automatic
invalidation when a release changes a calculation. P5 becomes the settlement engine. One release,
eight work packages, each gated on "settled == recomputed from scratch, byte for byte".

## 1. Measurements (2026-10-18)

Local, prod-shaped scratch ledger, network blocked. Other programs were using the CPU (wall time ≈
2 × CPU time), so read the ratios. Scripts are scratch-only (they point at a real-data DB):
`bench_settle_sim.py`, `bench_double.py`; WP1 turns them into a permanent harness.

| Measurement | Result |
| --- | --- |
| Settled simulation: the daily value series, the daily cash series and the benchmark comparison served from memory as if read from a store, the trend's full price-history loads skipped, everything else live | full build 8.47 / 8.45 s wall (CPU 3.69 / 3.53 s) → **0.19–0.26 s** wall (CPU 0.05–0.12 s); payload byte-identical |
| Doubled ledger (every transaction and dividend duplicated one day later; actions not duplicated) | full 8.47 → 21.31 s (CPU 3.66 → 9.84 s); settled 0.19 → 0.27 s (CPU 0.05 → 0.17 s) |
| Reading 2,179 settled days back from SQLite into `TrendPoint`s | 0.03 s, round trip identical |
| `/api/dashboard` payload | 439.1 KB, of which `trend` 348.5 KB (79%), `realized` 35.0 KB, `holdings` 20.3 KB, `options` 19.6 KB, `alerts` 9.7 KB |
| App import RSS | 252 MB (import 22.8 s); with `litellm` (163 MB alone) and `yfinance`→pandas (75 MB alone) stubbed out: **58 MB** (7.6 s); the calc core alone is 22 MB |

**Prefix property on the real-shaped ledger** — does a change dated D move any trend point before D?

| Change | D | Points before D changed | Points from D changed |
| --- | --- | ---: | ---: |
| a BUY dated today | 2026-10-13 | 0 / 2,178 | 1 |
| a cash dividend dated today | 2026-10-13 | 0 / 2,178 | 1 |
| a back-dated BUY | 2025-06-16 | 0 / 1,694 | 485 |
| one restated historical close | 2024-03-04 | 0 / 1,225 | 1 |

Why it holds: each day is valued from `bundle.through(day)` (a date-prefix cut), the latest price
and FX on or before that day, and the splits in `(priced_on, day]`; the refused-dividend set is
decided from events before each dividend (`timeseries.py:327-334`); the benchmark counterfactual
accumulates units flow by flow and values them at the last close.

**Where one build's time goes** (cProfile cumulative; ratios matter):

| Part | Share | Settled? |
| --- | ---: | --- |
| `daily_value_series` | 81.7% | yes |
| ↳ `build_book` × 463 | 54.1% | (one forward pass in the engine) |
| ↳ `through` × 461 | 5.7% | (gone) |
| ↳ `counts_by` × 2,774,053 (`net_invested` re-summed daily) | 4.6% | (prefix sum) |
| ↳ `_entry_at_or_before` × 73,906 | 4.4% | (moving pointer) |
| `get_price_history` × 87 | 11.2% | yes (only the tail is read live) |
| `_benchmark_comparison` | 2.5% | yes (cumulative units) |
| `daily_cash_series` + `compose_net_worth` | 1.9% | yes |
| `xirr_reporting` and the rest of the live part | ≈ 2.7% | no — computed every time |

## 2. The proposal, point by point

| Proposal | Assessment | Plan |
| --- | --- | --- |
| Yearly and monthly closes, read directly | Right, but the trend, net-worth and TWR charts are daily | Store **daily** settled points; month and year closes are rollups of them; invalidation is per day (the back-dated BUY above re-settles 485 days and leaves 1,694 alone) |
| A closed period no longer changes | Holds for the ledger; six more sources change the past | Price/FX backfills and corrections, split reconciliation of the stored price basis, FX cross derivation, instrument changes (quote currency, type, market), benchmark configuration, and a release that changes a calculation all invalidate (§5 of the spec) |
| Re-settle only when underlying records change | Right; the system must *know* reliably | SQLite triggers record the earliest affected date in the same transaction as the write — app doors, imports, jobs, scripts and the sqlite3 CLI alike |
| A separate process with status, progress bar and current item | Right | Reuse `job_runs` and the in-flight progress mechanism; run in a child process at low priority so browsing stays fast |
| An interruption leaves an old/new split; rerun to fix | Improvable — a half-new chart is hard to notice | Write the next **generation** beside the current one and switch only when complete; an interruption keeps the old generation whole, the status says so, and the run resumes |
| Replace the 重算 button | Yes — today it is a 17–30 ms validation replay plus a page reload | 「重新結算」: the same validation first, then a full rebuild with progress; charts refresh in place |
| Prompt when an old record is edited | Feasible | Owner chose the prompt (decision PERF-D2) |
| Most tables and charts read only the store | History reads the store; money figures stay live | Trend, net worth, 月度成績, yearly figures, TWR, benchmark from the store; KPIs, holdings, realized, XIRR, cash live from one replay (0.05–0.12 s CPU), never stale |

**The existing 月度成績 is the proposal in miniature, without invalidation.** `api/snapshots.py`
upserts the current month's KPIs every evening; the row standing at month end is frozen and never
revisited. Prod imported its full ledger on 2026-10-13, so every earlier month is missing or wrong
and would stay so. The settlement store replaces it with month-ends rolled up from settled days.

## 3. The earlier directions inside this plan

P1 has the same spirit as the proposal (do not recompute when nothing changed), but it keeps the
whole result in memory for one day, drops it on any write, and is lost on restart. The proposal is
persistent, precise to the affected day, and survives days and restarts — it is the better core;
P1 stays as a thin layer above it.

| Earlier | Role now | Why still needed |
| --- | --- | --- |
| P1 result cache | kept — request layer | a settled build is 0.1–0.3 s, but bell + drawer + 試算 trigger 3–8 per page flow |
| P2 shared reads | kept — API layer | bell, drawer, 試算, rebalance, exports, AI-task page read one result; read-only POSTs stop disabling sharing |
| P3 transport | kept + ETag | the store cannot fix transport; an unchanged dashboard answers 304 |
| P4 pre-warm | folded into the settlement run | warm today's result after a settle or a write |
| P5 one-pass replay | becomes the settlement engine | a full re-settle of the whole history in an estimated 5–20 s on the VM |
| P6 queries | kept | faster settle runs; fixes the medium routes |
| P7 VM | kept + extended | lazy heavy imports (−194 MB measured), heavy jobs in child processes, prod over demo |

## 4. Architecture

| Layer | What | Effect |
| --- | --- | --- |
| Browser | KPIs and tables first, charts after; background requests do not hold the top bar; the settlement status chip | shortest perceived wait |
| L5 transport | GZip, ETag/304, long-lived caching of `?v=` assets, deferred echarts | 439 KB → ~70 KB; 15–30 fewer revalidations per page |
| L4 shared reads | bell, drawer, 試算, rebalance, exports from one result; the drawer sends one request | 3–8 builds per flow → 1 |
| L3 result cache | key = data write generation + settled generation + day + reporting currency | repeats 0.03–0.4 s |
| **L2 calc** | point-in-time money live (one replay); history from the store; today's tail live | 8.47 → 0.19–0.26 s |
| **L1 settlement run** | triggers → child process at low priority → generation swap → pre-warm | page speed independent of history length |
| Data | SQLite ledgers, prices, FX — the only source of truth; the store can be dropped and rebuilt | "every report rebuilds from the ledgers" unchanged |
| L0 VM / process | lazy imports, zram, prod over demo, allocator, journald caps | about ⅔ less resident memory |

## 5. Owner decisions (2026-10-18)

Owner, in chat: 「我決定的三件事 1同意修改原則，2可跳提示後再開始，3改由結算資料提供。其餘決策按照建議」,
plus a requirement for the progress chip. Recorded as rulings in the spec:

| # | Question | Decision |
| --- | --- | --- |
| PERF-D1 | Settlement store + result cache; change `flight.py`'s "keeps nothing" principle (architecture.md), recorded in CHANGELOG | **Approved** |
| PERF-D2 | After a user edits an old record: re-settle automatically, or prompt first | **Prompt first, then start** |
| PERF-D3 | Charts during a re-settle | previous generation, labelled 更新中 (as recommended) |
| PERF-D4 | Heavy jobs (settlement, AI generation, provider fetches) in short-lived child processes; APScheduler still triggers in-process | **Yes** (as recommended) |
| PERF-D5 | Dashboard wire format | gzip + ETag only, format unchanged; splitting the trend into its own immutable route deferred (as recommended) |
| PERF-D6 | 「仍在計算中」 after a long request | **Yes**, 15 s (as recommended) |
| PERF-D7 | 月度成績 served from the settlement store; past months restated from today's ledger | **Yes**; `portfolio_snapshots` kept read-only for reference |
| PERF-D8 | VM tuning | zram + prod-over-demo priority: **yes**; stopping the demo while idle: not adopted (owner's call, status quo) |
| PERF-D9 | The two real-data scratch DBs | kept until WP8's before/after comparison, then deleted |
| PERF-D10 | (new requirement) the top-bar 重新結算 status | **clickable**, opening full detail: what it is doing now, what remains, time spent per step, estimated time remaining |

## 6. Delivery

One release, eight work packages on `perf/settlement-store`, then one staged deploy (demo → prod):
WP1 measurement baseline and the "settled == fresh" harness → WP2 the one-pass engine (P5+P6) →
WP3 the store, triggers, generations and the run → WP4 the read path and month/year closes → WP5
the request layer (P1, P2, P4) → WP6 transport and frontend (P3, status chip, prompt) → WP7 medium
routes and process slimming → WP8 VM tuning, deploy, before/after. Details and gates in the spec.

## 7. Expected results on the prod VM

| Situation | Now | After | Basis |
| --- | ---: | ---: | --- |
| Dashboard, cold (just after a write) | 16–64 s | ~0.5–2 s | 30–45× measured locally, scaled by the VM ratio |
| Dashboard / page change, warm | 16–64 s | < 1 s | cache hit + gzip or 304 + no static requests |
| The bell on 7 pages | a full build per page | reads the cache | WP5 |
| Open a holding's drawer | 2–3 full builds | < 1 s | one request, cached |
| Each 試算 edit | 1 full build | ~0.2–0.5 s | delta on the cached book |
| Re-settle after an old edit | — | 5–20 s in the background | estimated from the profile; nearer dates are faster |
| Pages after the ledger doubles | 2.5× slower | about the same | 0.19 → 0.27 s measured |
| Resident memory of the web process | 253 MB + 84 MB swapped | est. < 100 MB | import 252 → 58 MB measured |
| Service start | ~40 s | est. ~15 s | import 22.8 → 7.6 s measured |

Local figures are measured; VM figures are projections from earlier VM timings and the local
ratios. WP8 re-measures on the VM and reports before/after.
