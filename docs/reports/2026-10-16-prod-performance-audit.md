# Prod performance audit — why the pages keep spinning

**Date:** 2026-10-16 · **Release under test:** v0.1.31 (`43730cd`) · **Branch of record:**
`perf/settlement-store`
**Owner request (2026-10-16):** 「很慢問題，我需要多一些資訊來評估，進行全站盤查分析每個頁面，抽屜的處理流程，點評出為什麼耗時？瓶頸要點…並給出需要優化的方向…在不換硬體與不影響運作功能的情形下。」
**Follow-up:** the owner's settlement-store proposal and the integrated plan are in
`docs/reports/2026-10-18-performance-plan.md`; the implementation spec is
`docs/spec/2026-10-18-settlement-store.md`.

Scope: 11 pages, 86 GET routes, 9 interactive previews, 23 scheduled jobs. Data scale: 757
transactions, 262 dividends, 86 instruments, ~74,000 daily price rows (prod-shaped). Host: GCP
e2-micro (shared core, 1 GB RAM). This document contains no tickers, amounts or host details.

## Verdict

**One cause: the full dashboard build.** It replays the ledger 463 times and values 2,181 days,
and takes 16–64 s on the prod VM. It is triggered far too often and nothing is kept:

- **8 pages** trigger one on load. The dashboard needs it; the other 7 trigger it through the
  top-bar **alert bell**, which fetches `/api/alerts` (a full build) to count alerts.
- **Opening a holding's drawer** triggers 2–3: the drawer's `/detail`, `/api/dashboard` (off the
  index page), and an automatic `POST /api/whatif` 300 ms after render. Every edit of a 試算 field
  triggers another.
- 績效比較, the rebalance preview, exports and the AI-task page each trigger their own.
- Builds are shared only **in flight** (`shared/flight.coalesce`); sequential requests rebuild.

The shared-core VM makes it worse: after two or three builds in a row the burst credit is gone
and the same build goes from 16–22 s to 64 s. Swap, uncompressed responses and 15–30 static
revalidations per page add to it. **Requests queue, and the top progress bar keeps spinning.**

## 1. Anatomy of one full build

Profiled locally on the prod-shaped ledger (cProfile total 14.3 s; unprofiled ≈ 7.7 s — the
ratios hold).

| Part | Time | Share |
| --- | ---: | ---: |
| Trend: replay the ledger from the first row to each event day (461 replays) | 7.9 s | 55% |
| Trend: cut the ledger to each day before replaying | 0.9 s | 6% |
| Trend: value 2,181 days; `net_invested` re-summed from zero each day (~2.4 M `counts_by`) | 3.3 s | 23% |
| Load price history: 86 symbols, full history, one pydantic `PriceRead` per row | 1.5 s | 10% |
| Everything else: holdings, XIRR, cash, FX P&L, benchmark | 0.8 s | 6% |

84% of the time is the trend chart. Today's holdings and returns take under a second. The trend
is expensive because every event day replays from the start (1 row on the first, 1,000+ on the
last, ~250,000 steps in total; `portfolio/timeseries.py:352`). v0.1.31 already cut 2,181 replays to
461 (26 s → 7.7 s locally); this chart is the remaining bottleneck.

## 2. Which routes are heavy

77 GET routes (the drawer measured for two symbols) and 3 preview POSTs were called locally on the
prod-shaped ledger, recording wall time, CPU time, SQL statements, RSS growth and the number of
calls into the core functions. 9 further GETs (lookups by id, diffs, run logs) were judged light
from the code and not timed. The machine was at 100% CPU from other work, so read the ratios.

| Tier | Route(s) | Caller | What it does | Local | Prod (est.) |
| --- | --- | --- | --- | ---: | ---: |
| Heavy | `GET /api/performance/twr` | 績效比較, every window/benchmark change | full build + TWR | 28.8 s | 16–64 s |
| Heavy | `GET /api/symbol/{s}/detail` | drawer, corporate-action form, option symbol on the trades page | full build + 1 replay (the trend is unused) | 22.6–24.8 s | 16–64 s |
| Heavy | `GET /api/dashboard` | index | full build + 400-day alert inputs + 30-day sparklines | 22.9 s | 16–64 s |
| Heavy | `GET /api/alerts` | the bell on 7 pages | full build to compute alerts | 21.3 s | 16–64 s |
| Heavy | `POST /api/whatif` | drawer (auto, 300 ms after render; then each edit) | 1 replay + full build (uses only total value and price) | 12.6–13.9 s | 16–64 s |
| Heavy | `POST /api/rebalance/preview` | each weight change | full build | 12.0 s | 16–64 s |
| Heavy | exports (holdings, realized, options, holdings report; rebalance report = 2 builds), `PUT /alert-rules`, AI-task status / diagnosis / preflight, prompt preview | their buttons; AI-task page load | 1 full build each (from the code, not timed) | — | 16–64 s+ |
| Medium | `/signals` 1.0 s, `/input/context` 0.8 s (1,073 SQL), `/instruments` 0.47 s, `/ledgers/transactions` 0.40 s, `/cash` 0.25 s, `/ledgers/dividends`, `/option-expiry/count` and 6 more | page bodies, badges | 1 replay, or many price reads, or hundreds of SQL statements | 0.15–1.0 s | 0.5–8 s |
| Light | the other 59 | settings, lists, badges | a few paginated queries | < 0.12 s | < 1 s |

Prod estimates: a full build was timed on prod at 22.7, 47.0 and 64.2 s (three in a row,
2026-10-13); one inside a scheduled job took 16–20 s. Medium/light = local × 3–8 plus network.

## 3. Page by page

Every page except login first runs the shell's 7 light requests (session, version, what's-new,
prefs, 3 inbox badges) and revalidates 15–30 static files.

| Page | Full builds on load | What the body waits for | Other issues |
| --- | ---: | --- | --- |
| Index | 1 | `/api/dashboard` (5 modules share it — correct) | 1 MB `echarts.min.js` is a synchronous `<head>` script; cash and 月度成績 wait for the dashboard although they do not need it; ~11 requests |
| Trades | 1 (bell) | `/input/context` → accounts → transactions, in sequence | `/input/context` and `/llm/config` fetched twice; ~18 requests, 615 KB JS |
| Cash | 1 (bell) | `/input/context` → `/cash` (1 replay) → FX, in sequence | switching tabs refetches cash and FX |
| Settings | 1 (bell) | 10 modules boot at once, ~35 requests, whichever tab is shown | prompts 6 steps and scheduler 3 steps in sequence; echarts in `<head>`; prefs fetched 3× |
| AI insights | 1 (bell) | 3 parallel requests | echarts in `<head>` |
| Watchlist | 1 (bell) | `/api/instruments` (~13 queries per symbol) | opening a drawer downloads echarts and builds twice more |
| Inbox | 1 (bell) | dividend, rebate, option-expiry lists | badge and list run the same detection; dividend detection queries per symbol × event × account |
| AI tasks | 1–2 | task status (itself a full build when tasks exist) | the 新增 wizard and 編輯標的 fetch the whole dashboard |
| Data center, News, Login | 0 | 1–3 requests | fast |

**Why the bar spins after the content is there:** the top progress bar stays while any request
is pending (`web/shell.js:695`). The body of 7 pages needs 1–3 s; the bell's full build needs
16–64 s. `web/api.js` sets no timeout, so a slow server shows 「載入中」 forever with no message.

## 4. Drawers and interactions

| Action | Requests | Full builds | Note |
| --- | --- | ---: | --- |
| Open a holding's drawer | `/detail` + (off index) `/api/dashboard`; `POST /whatif` 300 ms after render | 2–3 | off index, 4 files (incl. echarts) load in sequence first; nothing is kept between openings |
| ←/→ to the next holding | the whole set again | 2 each | |
| Edit any 試算 field | `POST /whatif` after 300 ms | 1 each | a POST disables sharing while it runs (`api/app.py:315`) |
| Change a rebalance weight | `POST /rebalance/preview` after 250 ms | 1 each | |
| 績效比較 window / benchmark | `/performance/twr` | 1 each | |
| Corporate-action form field | may call `/detail` | 1 each | only to read an option's underlying and right, which the symbol already encodes |
| Option symbol on the trades page | `/detail` | 1 | the light `/input/option-symbol` exists |
| Export holdings / realized / options | export route | 1 | the rebalance report 2 |
| Manual-entry preview | `POST /input/manual/preview` | 0 | 3 single replays, 0.25 s locally — not a bottleneck |

## 5. Four amplifiers on the small VM

- **CPU.** The same build three times in a row: 22.7 → 47.0 → 64.2 s. An e2-micro sustains about a
  quarter of a vCPU and bursts to 2 for short periods; back-to-back builds exhaust the burst.
- **Memory.** 964 MB total, 234 MB available. The demo process had 234 MB in swap, prod 84 MB.
  The demo's first `/api/health` after idle took 0.82 s, then 5 ms (health is `lru_cache`d — the
  0.82 s is page-in). During heavy builds 31–64% of CPU time was I/O wait. The swap is on a
  network persistent disk.
- **Network.** The prod dashboard response is 408 KB raw (VM, 2026-10-13), ~65 KB gzipped, and
  gzip is off. The tunnel measured 0.5–0.74 MB/s. Static files are served `Cache-Control:
  no-cache` (`_NoCacheStaticFiles`, `api/app.py:131`), so each page revalidates 15–30 of them; a
  304 on a new connection costs 0.6–0.85 s; a new connection + TLS costs 0.4–0.8 s.
- **One process.** All requests and scheduled jobs share one Python process; pure-Python CPU work
  runs one thread at a time, so a build slows every other request, health included.

## 6. Scheduled jobs — not the cause, but the timing matters

`job_runs` on prod since the v0.1.31 deploy (read-only query):

| Job | Mean | Max | Time (TPE) | Main cost |
| --- | ---: | ---: | --- | --- |
| `fundamentals_daily` | 50.4 s | 55.5 s | 09:20 | per-symbol provider fetches (network) |
| `news_daily` | 20.2 s | 21.4 s | 06:00 | 1 full build + news + AI |
| `alert_scan` | 17.1 s | 19.0 s | 15:00 weekdays | ≥ 1 full build |
| `dividends_daily` | 16.5 s | 27.0 s | 03:00 | network |
| `snapshot_monthly` | 15.9 s | 16.4 s | 23:50 | 1 full build |
| dividend-inbox scan | 12.7 s | 13.6 s | 15:30 weekdays | per-symbol × event queries |
| 13 others | 0–9 s | ≤ 14 s | spread | quotes, backup, digest |

About 3 minutes a day in total, mostly network. Two notes: weekdays 14:00–15:30 TPE (after the TW
close) run 8 jobs back to back, three of them full builds or query-heavy; and on every restart
LiteLLM fetches its model cost map from GitHub (up to 5 s; 34 s observed once locally).

## 7. How the spinning happens (estimated from the measurements)

1. **0 s** — open the index → build 1. The shell's 7 requests queue in the same process.
2. **~20 s** — the index renders; open a holding → build 2; the auto 試算 → build 3.
3. **~1 min** — → to the next holding → builds 4 and 5. The burst credit is gone; ~20 s becomes ~60 s.
4. **~3 min** — switch to 交易紀錄 → the body is quick, the bell starts build 6 while earlier
   builds may still be running. The top bar spins.
5. **After** — every page change or reload adds one more. Requests pile up at low CPU speed.

The 2026-10-16 morning outage (prod unreachable from outside) was a Tailscale Funnel ingress
problem, unrelated to the slowness, and has been fixed.

## 8. Directions (by benefit)

None changes the hardware or any money / return calculation — the same code produces the same
numbers; work is simply not repeated or re-sent.

- **P1 — result cache.** Keep the first build's result; every reader uses it until data changes,
  the day changes or the reporting currency differs. Simulated locally: first 11.8 s, then the 6
  heavy routes 0.03–0.43 s; a deep copy 0.06 s, serialisation 0.02 s. Safe because every in-app
  write goes through `shared/db.session()` (a write counter exists); external writers (scripts,
  sqlite3) need a DB+WAL file signature (or `PRAGMA data_version`). The result depends on the clock
  only through `now.date()` (staleness is day-based, `pricing/store.py:316`); the few sub-day
  fields (`as_of`, backup time, scheduler status) are filled per request. Changes `flight.py`'s
  "keep nothing" principle — owner sign-off required. Limit: the first build after a change
  still costs 16–64 s.
- **P2 — drawers and previews read the shared result;** option terms via `/input/option-symbol`;
  read-only POSTs excluded from the write window. Drawer 2–3 builds → 0.
- **P3 — transport and frontend.** GZip (built into the framework); long-lived caching for the
  `?v=`-stamped static files (HTML still revalidates); defer echarts; parallelise the sequential
  chains (cash 3 steps, settings prompts 6, scheduler 3, the index's cash and 月度成績); remove
  duplicate requests; a request timeout with 「仍在計算中」 (owner decision).
- **P4 — background pre-warm** after writes settle and after each quote refresh (owner decision).
- **P5 — replay the trend once.** Walk the ledger forward once, record the book at each event
  day, accumulate `net_invested`, drop the redundant `replay_refusals` (`timeseries.py:334`).
  Estimated −60–70% of a cold build (local 7.7 → ~2.5 s; prod 16–64 → ~5–20 s). Calc core: must
  be byte-identical on the prod-shaped ledger, the demo corpus and the golden test, then pass the
  stress audit.
- **P6 — reads and queries.** Light price-history reads (no per-row model); read each FX history
  once; `(account, symbol)` indexes; batch the per-symbol queries in `/instruments` and
  `/input/context`; a cheap "already booked" check first in the dividend inbox.
- **P7 — operations.** zram swap; `LITELLM_LOCAL_MODEL_COST_MAP=True` on both units; optionally stop
  the demo between releases (frees ~280 MB; changes the release flow).

## 9. Suggested phasing (superseded)

The audit proposed phase 1 = P1+P2+P3, phase 2 = P5+P6, phase 3 = P4+P7. On 2026-10-18 the owner
proposed a persisted settlement store instead and asked for one integrated plan; see
`docs/reports/2026-10-18-performance-plan.md`.

## 10. Method and limits

- **Local runs** used a prod-shaped scratch ledger (the owner's import set run through the real
  import doors; same row counts as prod; git-ignored scratch, never committed). Network was
  blocked by a socket guard, so no provider was called. Recorded per route: wall and CPU time,
  SQL statements, RSS, core-function call counts (`sys.monitoring` PY_START counters).
- The local machine was at 100% CPU from other work: absolute seconds are inflated; conclusions
  rest on call counts and ratios. Quiet, the dashboard took ~7.7 s (2026-10-13).
- **Prod** was not load-tested (to spare its CPU credit). Prod figures come from three read-only
  timings on 2026-10-13, `job_runs`, and read-only memory/swap checks — every VM operation through
  `scripts/vm_exec.py`, logged.
- **Network** was measured from the dev machine, mostly against the demo; on prod only the static
  chart file.
- **Code analysis** by three read-only subagents (per-page requests, per-route computation, jobs);
  the key claims were re-checked in the source.
