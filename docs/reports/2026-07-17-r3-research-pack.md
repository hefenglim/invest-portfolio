# R3 Research Pack — Wave W-E (research only, no production code)

**Date:** 2026-07-17
**Scope:** Six design/research studies (R-1 … R-6) for `portfolio-dash`.
**Status:** Design and analysis only. Nothing here is implemented. Every codebase claim is
cited to `file:line` (verified against the working tree at this date). Stack is locked
(Python 3.12 · FastAPI · SQLite · vanilla JS + ECharts CDN) per `CLAUDE.md` and
`.claude/rules/stack.md`; no proposal may add a framework, a bundler, or a second DB engine
without owner sign-off recorded in `CHANGELOG.md`.

Effort key: **S** ≈ ≤ half a day · **M** ≈ 1–3 days · **L** ≈ ≥ 4 days / multi-wave.

---

## R-1 Multi-user extensibility study (design only — NOT to be implemented now)

### Verified starting facts

| Fact | Evidence |
| --- | --- |
| News is already a **separate SQLite file** opened on demand — the precedent | `portfolio_dash/news/store.py:26-46` (`news_db_path()` → `get_settings().db_path.parent / "news.db"`; `get_news_connection()` opens/creates it lazily) |
| `prices` / `fx_rates` / `dividend_events` have **no FK coupling** to `instruments` | `portfolio_dash/pricing/schema.py:4-20` (plain `TEXT` columns; PKs are `(instrument, as_of_date)` etc., no `REFERENCES`) |
| Main DB path is a **single global** setting | `portfolio_dash/shared/config.py:23` (`db_path: Path = Path("data/portfolio.db")`), read by `shared/db.py:16` and `api/deps.py:9,18` |
| Every request connection is per-request, single-thread, fresh | `portfolio_dash/shared/db.py:33-52` (`session()`); `api/deps.py:16-19` (`get_conn`) |
| `auth_users` / `auth_sessions` already exist (in the **main** DB) | `portfolio_dash/api/auth_store.py:39,42` |
| The **entire** DB-open surface is small: ~21 references to `db_path` / `get_connection()` / `session()` across **10 files** | `Grep` over `portfolio_dash/` — the two seams that matter are `shared/db.py` (request/background) and `news/store.py` (news) |
| No cross-DB `ATTACH DATABASE` is used anywhere today | `Grep "ATTACH"` — the 8 hits are English "attach"/artifact-attachment strings, not SQLite ATTACH |
| Pricing worklist is built from the whole `instruments` table (minus archived) | `portfolio_dash/scheduler/jobs.py:822-842` (`build_worklist`) |

### Personal vs shared classification (as supplied, cross-checked against schema)

- **PERSONAL (per-user ledger.db):** `transactions`, `dividends`, `fx_conversions`,
  `opening_inventory`, `cash_movements`, `accounts`, `instruments` (incl. `target_low`,
  `sector`, `archived` — `data_ingestion/schema.py:9-46`); AI records (`llm_usage`,
  `insights`, `insight_evaluations`, `calibration_prompts`, `llm_budget_events`,
  `insight_types`, `insight_type_strategies`, `strategy_prompts`); system records
  (`job_runs`, `action_log`, `ledger_audit`, `alert_events`, `alert_dispatch_log`,
  `pending_dividend_skips`, `rebate_skips`, `digests`, `whatsnew_seen`); `portfolio_snapshots`
  (personal-derived); nearly all config tables.
- **SHARED (one market.db, fetched once):** `prices`, `fx_rates`, `dividend_events`,
  `external_snapshots`, and the existing `news.db` (`organized_news`, `news_mentions`).
- **Grey — must be resolved before build:**
  - `auth_users` / `auth_sessions` are listed PERSONAL, but the **user registry is the
    routing key**: login must resolve *before* a ledger is selected, so `auth_*` belongs in
    a small **central control-plane DB** (or the shared file), *not* inside any user's
    ledger. This is a correction to the naive classification and is load-bearing.
  - `signal_states` is shareable only while rule *parameters* stay global; the moment a user
    can tune thresholds, signal state is personal. Keep it personal by default (cheapest
    reversible choice).
  - Config tables that gate **shared** fetching (`data_sources`, `schedule_config`, API keys,
    LLM model registry) are operationally shared even though "config" reads as personal —
    you do not want each user re-entering a FinMind key. Split config into *shared
    operational* vs *personal preference* at design time.

### Target architecture

```
                       ┌──────────────────────────┐
   login / routing ───►│  control.db (central)    │  auth_users, auth_sessions,
                       │  = the user registry     │  user→ledger path map, shared keys
                       └──────────┬───────────────┘
                                  │ resolve user → ledger path
      per request  ┌─────────────▼─────────────┐        ┌───────────────────────────┐
   user A ────────►│  ledger_A.db (PERSONAL)    │        │  market.db (SHARED, 1 copy)│
   user B ────────►│  ledger_B.db (PERSONAL)    │◄──────►│  prices, fx_rates,         │
                   │  txns, accounts, holdings, │  read  │  dividend_events,          │
                   │  AI records, system logs   │        │  external_snapshots        │
                   └───────────────────────────┘        └───────────────────────────┘
                                                          news.db (SHARED — already exists)
```

- **Reads that need both** (dashboard, allocation, XIRR) attach or open `market.db`
  read-only alongside the user's `ledger.db`. `portfolio/` already takes prices/FX as
  *inputs* (pure functions over passed data — `portfolio/allocation.py:16-40`,
  `portfolio/dividends.py:37-84`), so the split is a *data-access* change, not a calc
  change.
- **Writes are cleanly partitioned:** only `pricing/` writes market rows
  (`data-and-pricing.md` invariant); only ledger flows write personal rows. This maps 1:1
  onto write-to-market.db vs write-to-ledger.db with **no shared writer contention**.

### Seams to prepare NOW (cheap, reversible, do not build the feature)

1. **Single choke-point for the personal connection.** Today `session()` /
   `get_conn` read the global `db_path`. Introduce (later) a `db_path_for(request)` resolver
   and route *all* personal opens through it. Preparation now = ensure **no module bypasses
   `session()` / `get_conn`** to hardcode a path. Audit shows the surface is already tiny
   (~21 refs / 10 files) — keep it that way; reject any new direct `sqlite3.connect(...)`.
2. **A separate accessor for market data.** `news/store.py` is the template: a module-local
   `market_db_path()` + `market_session()` that today points at the *same* file. Splitting
   the DDL for `prices`/`fx_rates`/`dividend_events`/`external_snapshots` into its own
   `create_tables` (mirrors `pricing/schema.py`) means the physical split later is a
   path change, not a query rewrite.
3. **Scheduler fan-out framing.** Fetch jobs must run **once** over the **union** of all
   users' instruments, not per-user. `build_worklist` (`scheduler/jobs.py:822-842`) already
   returns a de-duped instrument list from one `instruments` table — the future version
   unions every ledger's `instruments` into a shared worklist, fetches once into
   `market.db`, and every user's dashboard reads the shared rows. Personal jobs
   (snapshots, digests, alert scans, insight runs) fan out per user. Prepare now by keeping
   the worklist builder pure and parameterizable (it already is).
4. **Watchlist union for shared fetching.** The shared fetch scope = `⋃ user.instruments
   WHERE archived=0`. Because `prices` has no FK to any user's `instruments`
   (`pricing/schema.py:4-9`), a price row for a symbol only user B holds is transparently
   reused by user A the day A buys it — zero migration, instant cache hit.
5. **Config bifurcation marker.** When touching a config table, tag it in the db_stats
   registry (`api/routers/db_stats.py:72-92` already groups "設定") as *shared-operational*
   vs *personal-preference*. Costs nothing now; prevents a painful reclassification later.

### Phased migration path

- **Phase 0 (now, ~S):** connection-audit guardrail — assert every DB open goes through
  `session()`/`get_conn`/`news_session`; document the personal/shared/control split in one
  place. No behaviour change.
- **Phase 1 (~M):** physically split market tables into `market.db` (new accessor), still
  single-user. Dashboard opens both. This is the highest-value, lowest-risk step and is
  useful even for one user (smaller ledger, independent market-data backup/retention).
- **Phase 2 (~M):** stand up `control.db` with the user registry; move `auth_*` there;
  add `db_path_for(request)`. Still one ledger, but selected by resolver.
- **Phase 3 (~L):** multi-ledger — per-user `ledger_<id>.db`, scheduler union fetch +
  per-user personal fan-out, per-user backup rotation.
- **Phase 4 (~L):** hardening — cross-user cache-warming, per-user quota/budget, admin
  provisioning.

### Top 5 risks

1. **SQLite single-writer-per-file is an ADVANTAGE here, not a limit.** Each user's ledger
   is a single-writer world (1 user); `market.db` has exactly one writer (the scheduler).
   The classic multi-tenant "one hot table, many writers" contention **cannot arise** under
   this split. The risk is *forgetting* this and reaching for Postgres prematurely
   (`stack.md` explicitly parks Postgres). Verdict: file-per-user is the right primitive.
2. **ATTACH vs separate connections.** `ATTACH market.db` lets you `JOIN` across files in
   one query but ties both files' locking/WAL into one connection and complicates the
   "market.db is read-only for readers" guarantee. Separate connections (open ledger + open
   market, join in Python — which `portfolio/` already does, it takes prices as inputs)
   keep isolation crisp at the cost of no cross-file SQL JOIN. **Recommend separate
   connections**; the calc layer is already structured for it.
3. **Instrument registry is per-user but the fetch worklist must be the union.** A symbol's
   `sector`/`name`/`board` live on each user's `instruments` row and can legitimately differ
   (or drift) between users. The shared fetcher keys only on `(symbol, market, board)`, so
   metadata divergence is harmless for *prices*, but any shared surface that shows a name
   must pick one authority. Keep instrument *identity* (symbol/market/board) as the shared
   key and instrument *metadata* as per-user display.
4. **auth_* placement.** Putting the user registry inside a per-user ledger is a
   chicken-and-egg deadlock (you need the user to find the ledger to find the user). Must
   live in `control.db`. Getting this wrong is a re-architecture, not a patch — decide first.
5. **Backup/retention multiplication.** Today one file, 30 rotated backups. Multi-ledger =
   N ledgers × 30 + market.db + news.db + control.db. Retention (R-2) and backup rotation
   must become per-file and quota-aware before Phase 3, or a handful of users blow the small
   VM's disk.

**Recommendation ranking.** Do **Phase 0 + Phase 1 now** (they pay for themselves at one
user: cleaner seam, independent market-data backup, smaller personal DB) and stop there
until a real second user exists. Adopt **separate connections over ATTACH** (risk 2) and
**control.db for auth** (risk 4) as the two architectural commitments to lock in early,
since both are expensive to reverse. Everything past Phase 1 is design-on-paper until the
owner commits to the service.

---

## R-2 Data-growth capacity study

### Method & measured inputs

Row payloads were **measured** from the synthetic stress-audit fixture
(`scripts/stress_audit/evidence/phase1.db`) as average text bytes/row (SQLite has no
`dbstat` compiled in this build, so on-disk sizing adds a b-tree + index overhead factor):

| Table | measured text B/row | disk estimate B/row (text × ~1.5–2 + PK index) | note |
| --- | --- | --- | --- |
| `prices` | 59.6 | ~110 | PK `(instrument, as_of_date)` index |
| `fx_rates` | 59.3 | ~110 | only 3 reporting pairs |
| `transactions` | 171.3 | ~230 | grows with **user** activity, not market |
| `dividends` | 43.9 | ~90 | user activity |
| `action_log` | 71.4 | ~120 | **pruned** to 5000 |
| `external_snapshots` | not in fixture; JSON `payload` col (`pricing/snapshots_store.py:22-30`) | ~300 | est. 250 B payload + index |
| `organized_news` | not in fixture; title+2–4-sentence summary+links (`news/store.py:63-78`) | ~650 | separate `news.db` |
| `job_runs` | — | ~120 | never pruned |
| `llm_usage` | — | ~130 | never pruned; **feeds quota** |

Trading days/yr = **252** (US ~252, TW ~242, MY ~245; 252 used as a round upper bound).
Initial history backfill on registration = **5 years** (`history_backfill_days = 1825`,
`shared/config.py:34`), so each new symbol writes ~1,260 price rows *immediately*.

### Scheduler-driven fixed load (independent of watchlist size)

Counted from the `JOBS` registry (`scheduler/jobs.py:708-807`, 21 static jobs):

| Cadence | jobs | runs/job/yr | subtotal |
| --- | --- | --- | --- |
| Daily (`* * *`) — history, dividends, snapshot, sentiment, consensus, evaluate, backup, news | 8 | 365 | 2,920 |
| Mon–Fri — quotes_tw/us/my, dividend_inbox_scan, finmind_chips, finmind_valuation, index_quotes, signal_scan, alert_scan, digest_daily | 10 | 260 | 2,600 |
| Weekly (Sun) — generate_calibrations, digest_weekly | 2 | 52 | 104 |
| Monthly — finmind_fundamentals_monthly | 1 | 12 | 12 |
| **Total static `job_runs` rows/yr** | | | **5,636** |

(kind=insight dynamic runs add more — `scheduler/jobs.py:1049-1078` — but are user/budget
bounded.) So `job_runs` ≈ **5,636 rows/yr ≈ 0.68 MB/yr**, never pruned, watchlist-independent.

### Per-table annual growth, three watchlist scenarios

`prices` = symbols × 252/yr. `external_snapshots` is dominated by `consensus_daily` which
runs for **all** instruments **daily** (`scheduler/jobs.py:526-536`), plus TW-subset chips
(~40% of symbols, mon-fri × 2 datasets), sentiment (2/day), index (3/day mon-fri).

| Table | 20 symbols | 50 symbols | 100 symbols | notes / arithmetic |
| --- | --- | --- | --- | --- |
| `prices` (rows/yr) | 5,040 | 12,600 | 25,200 | = symbols × 252 |
| `prices` (MB/yr) | 0.55 | 1.39 | 2.77 | × 110 B |
| `prices` one-time backfill (MB) | 2.8 | 6.9 | 13.9 | symbols × 1,260 × 110 B |
| `external_snapshots` (rows/yr) | ~13,000 | ~30,400 | ~59,300 | consensus symbols×365 + chips/val + sentiment/index |
| `external_snapshots` (MB/yr) | 3.9 | 9.1 | 17.8 | × 300 B |
| `llm_usage` (MB/yr) | ~2.4 | ~2.4 | ~3+ | budget/`per_symbol_cap=5` bounded (`news/pipeline.py:50`) |
| `job_runs` (MB/yr) | 0.68 | 0.68 | 0.68 | fixed 5,636 rows |
| `organized_news` (news.db, MB/yr) | ~3.5 | ~7.1 | ~11.9 | ~15/30/50 organized/day × 650 B, de-duped |
| other (alerts, dividend_events, insights) | ~1 | ~1.5 | ~2 | small |
| **portfolio.db total (MB/yr, steady)** | **~8.5** | **~15** | **~25.6** | excludes one-time backfill |
| **combined incl. news.db (MB/yr)** | **~12** | **~22** | **~37.5** | |

### Verdict

- **Years of headroom at every scenario.** At 50 symbols, portfolio.db grows ~15 MB/yr; after
  **10 years** it is ~150 MB + ~7 MB backfill — trivial for SQLite (practical multi-GB is
  routine; the format limit is 281 TB). Even 100 symbols for 10 years ≈ ~260 MB portfolio +
  ~120 MB news.
- **The real constraints are NOT file size or query speed but:**
  1. **Backup rotation disk.** 30 rotated gzipped full copies (task-confirmed). At 50
     symbols/5 yr the ~75 MB DB gzips ~4:1 → ~18 MB × 30 ≈ **~540 MB** of backups; ~1 GB
     after a decade. On a small VM this is the **first** operational pressure — before RAM,
     before query latency.
  2. **RAM is essentially unaffected.** SQLite is page-cache bounded (~2 MB default), not
     file-size bound — a 500 MB DB does not load into the 1 GB host's RAM. The only RAM
     risk is a *query pattern* that materializes a huge result set (e.g. a naive
     `SELECT * FROM prices` for a chart). Query shape matters more than table size.
- **First table to matter:** by **rows**, `external_snapshots` (all-instrument daily
  consensus) overtakes `prices` from ~20 symbols up; by **bytes**, `organized_news` (text,
  in the separate `news.db`) is the single largest grower. Both are **shared/market** data —
  which is exactly why the R-1 split lets you retire them on a different schedule than the
  irreplaceable ledger.

### Prioritized retention plan

1. **`llm_usage` — prune carefully; it feeds quota.** Verified: `budget_remaining()` =
   `Σ llm_budget_events.amount_usd − Σ llm_usage.cost` (`shared/llm_config.py:284-290`,
   and `quota_remaining` delegates to it, `:371-376`). **Deleting old `llm_usage` rows
   deletes their cost from the subtraction and silently INFLATES remaining budget** (the
   memory-flagged trap). Safe retention = roll pruned rows into a **monthly aggregate row
   that preserves total `cost`**, OR post a compensating negative `llm_budget_events` entry
   equal to the pruned cost sum. Never a naive `DELETE`.
2. **`external_snapshots` — highest row growth, fully reproducible.** Keep last N days
   (e.g. 400) per `(source, dataset, symbol)`; older daily snapshots are pure history that
   the dashboard does not read. Biggest single win, zero ledger risk.
3. **`job_runs` — never pruned today.** Apply the `action_log` pattern (`api/action_log.py:118-132`,
   cap 5000) — keep newest N per job_id or globally. 5,636/yr makes this the clearest
   "copy an existing, proven prune" task.
4. **`alert_events` / `alert_dispatch_log`** — TTL on `fired_at` / `dispatched_at`; these
   are operational history, not money-of-record.
5. **`organized_news`** (news.db) — TTL on `news_date` (e.g. 12–18 months). Largest bytes,
   but summaries only (never full bodies — `news/store.py:5-7`), and stale news has no
   analytic value.

Money-of-record ledgers (`transactions`, `dividends`, `fx_conversions`,
`opening_inventory`) are **never** pruned (rebuild/重算 invariant, `domain-ledger.md`); they
are also the slowest-growing (<200 rows/month per `CLAUDE.md`).

---

## R-3 Sector taxonomy normalization — 5 proposals

### Verified problem

- `instruments.sector` is a **nullable free-text** column (`data_ingestion/schema.py:11`),
  and the edit UI is a bare `<input>` with `value = i.sector || ''` (`web/instruments.js:273-275`)
  — no vocabulary, no dropdown, so "Tech" and "Technology" are two distinct strings, EN/中文
  synonyms coexist, and TW stocks can be blank.
- The allocation donut groups **directly on the raw string**: `by_sector[inst.sector] += value`
  (`portfolio/allocation.py:32`) → every spelling variant is its own slice; a `None`/blank
  sector becomes its own bucket.
- The **`sector_weight` concentration alert** iterates the *same* raw-keyed weights
  (`strategy/alerts.py:174-181`, `for sector, w in data.allocation.weights.items()`), so
  fragmentation both dilutes the concentration signal (a truly 30% tech position split
  "Tech"/"Technology" reads as two 15% slices and never trips the threshold) **and** spams a
  per-variant alert title. Registration default sector is empty string
  (`api/instrument_service.py:66`).

### Proposals

**P1 — Read-time canonical mapping (config dict, applied in calc).** A `SECTOR_CANONICAL`
map in config (`{"tech":"Technology", "科技":"Technology", "半導體":"Semiconductors", …}`);
`sector_allocation` and the alert group by `canonical(inst.sector)` instead of the raw
string; blanks map to a single `"未分類"` bucket. zh-TW display is a second lookup
(`DISPLAY_ZH[canonical]`). **Touchpoints:** `portfolio/allocation.py:32`, `strategy/alerts.py:176`.
No DB write, no migration; existing rows keep their raw text. **Effort:** S.
*Pros:* smallest change; fully reversible; fixes donut + alert together; zero data risk.
*Cons:* raw text stays messy in the DB and edit form (only *display/grouping* is fixed);
mapping table needs maintenance as new variants appear; two users could still enter new
un-mapped spellings.

**P2 — Canonical vocabulary + one-time migration + registration-time normalization.** Define
a fixed canonical set (a `SECTOR` enum or a `sectors` reference table, zh-TW label + EN key);
one-time migration rewrites existing `instruments.sector` to canonical values; the edit UI
becomes a **dropdown** (replace the free `<input>` at `web/instruments.js:273-275`);
registration writes only canonical values. **Touchpoints:** schema/migration, `instrument_service`,
`web/instruments.js`, allocation/alert unchanged (already correct once data is clean).
**Effort:** M. *Pros:* root-cause fix — the data itself becomes clean, so *every* downstream
(export, LLM prompts, future per-user shared views) benefits with no per-surface mapping;
donut/alert need no logic change. *Cons:* migration must decide every legacy value
(needs a human pass); a rigid vocabulary can misfit odd instruments; touches golden fixtures
if any assert sector strings.

**P3 — Settings-page alias mapping UI (user-managed).** A `sector_aliases` config table
(`raw → canonical`) with settings CRUD; applied at read like P1 but *user-editable* without a
code change. **Touchpoints:** new config table + `api/routers` + a settings panel;
allocation/alert read through the alias resolver. **Effort:** M. *Pros:* non-engineer can fix
a new synonym instantly; aligns with the config-driven-adjustability principle; no migration
required. *Cons:* more moving parts than P1 for a 1–2-user app; UI to build and test; still
lets messy raw values accumulate (only remapped, not prevented).

**P4 — Provider-side enrichment with suggestion.** On registration/probe, fetch the sector
from the price provider (e.g. yfinance `sector` field) → normalize to the canonical set →
pre-fill/suggest it to the user, who confirms. **Touchpoints:** `instrument_service` probe
path, provider adapter, edit UI shows a suggested value. **Effort:** M/L. *Pros:* fills the
blank-sector gap automatically and consistently (provider taxonomy is already normalized);
reduces manual entry. *Cons:* provider sector coverage for TW/MY is patchy (the exact market
where blanks occur today); adds a network dependency to registration; provider taxonomy
(GICS-ish, English) still needs a zh-TW display map; must degrade gracefully when the provider
has no sector.

**P5 — LLM-assisted one-time cleanup with human confirm.** A batch job sends the *distinct*
raw sector strings (a handful, not per-row) to the LLM, which proposes a `raw → canonical
(+ zh-TW label)` mapping; the human confirms in a review screen; the confirmed map drives a
one-time migration. Cached, batch-only, **never a number source** (`llm-insight.md`).
**Touchpoints:** one-off ops job + a confirm UI + migration. **Effort:** M. *Pros:* handles
messy multilingual synonyms far better than a hand-written dict; one pass cleans years of
drift; respects the batch/cache LLM invariants. *Cons:* needs AI activated + budget; overkill
for a small vocabulary; must not run unattended (human-confirm gate is mandatory); doesn't
*prevent* future drift on its own (pair with P2's dropdown).

**Recommendation ranking.** Ship **P1 now** (it stops the donut and the concentration alert
from lying, in an afternoon, with zero data risk) and **fold its map into P2's canonical set**
as the durable fix: convert the edit field to a dropdown and migrate once, so new instruments
can only be canonical. Use **P5** as the *tool* to generate P2's migration map cheaply if the
existing raw values are numerous or multilingual. **P4** is a nice auto-fill enhancement on top
of P2 but shouldn't gate the fix. **P3** is over-built for 1–2 users — defer until the
multi-user service (R-1) makes user-editable taxonomy actually valuable. Every option must carry
a zh-TW display label (`產業` is already the UI term — `web/instruments.js:275`,
`web/settings-alerts.js:23-24`).

---

## R-4 Account display-naming unification — 5 proposals

### Verified problem

The same account is rendered three different ways:

1. **API English `account_name`** — the golden dashboard payload carries
   `"account_name": "TW Broker"`, `"Charles Schwab"`, `"Moomoo MY (MY)"`
   (`tests/golden/dashboard_full.json:18,128,184`), sourced from `accounts.name`
   (`data_ingestion/schema.py:4`, `Account.name` `shared/models/assets.py:14`).
2. **Frontend zh chips via a duplicated `ACCOUNT_ZH` map** — the *same literal object*
   `{tw_broker:'台灣券商', schwab:'嘉信 Schwab', moomoo_my_us:'Moomoo 美股',
   moomoo_my_my:'Moomoo 馬股'}` is copy-pasted into **three** files:
   `web/app.js:13-18`, `web/detail.js:29-32`, `web/ledger.js:49-54`. (Even the trailing-comma
   style differs — classic drift.)
3. **`/api/input/context`** serves yet another shape — `{id, name, ccy, …}` with the English
   `accounts.name` (`api/routers/input_center.py:70-80`) — used to populate selects, which
   therefore show English while chips show 中文.

Fallback chains differ per surface too: `ACCOUNT_ZH[id] || h.account_name`
(`web/app.js:388`) vs `ACCOUNT_ZH[id] || a.account_name` (`web/detail.js:205`) vs
`ACCOUNT_ZH[id] || id` (`web/app.js:237`). Same account, up to three labels in one session.

### Proposals

**P1 — Single server-side `display_name`.** Add `accounts.display_name` (zh-TW), serialize it
everywhere `account_name` appears, and delete all three `ACCOUNT_ZH` maps. **Touchpoints:**
schema + migration, `api/serialize.py` / dashboard + `/input/context`, remove client maps.
**Effort:** M — **and it changes the golden payload** (`tests/golden/dashboard_full.json` +
the spec-17 contract test `tests/contract/test_spec17_financials.py` + spec-18 round-trip
must be re-baselined). *Pros:* one source of truth; every surface (incl. exports, LLM prompts,
future shared views) is automatically consistent; the frontend stops carrying business naming.
*Cons:* golden/contract re-baseline is unavoidable; zh naming becomes a backend concern
(fine, but a philosophical shift — the API currently emits English record names).

**P2 — Shared frontend name-resolver module.** One `web/accounts.js` exporting
`accountName(id, fallback)` backed by a single fetch of `/api/input/context`
(`input_center.py:61`, already serves the account list); the three duplicated `ACCOUNT_ZH`
literals are replaced by imports of that one module. **Touchpoints:** new JS module + 3 call
sites; **no API change, no schema change, no golden change.** **Effort:** S. *Pros:* kills the
drift (one map, not three) with the least risk; no test re-baseline; ships today. *Cons:* the
canonical zh names still live in the frontend (not available to exports/LLM/other clients);
still an overlay on top of the English API value rather than a true single source.

**P3 — Short-code + full-name two-tier.** Add `accounts.short_code` (e.g. `嘉信`) +
`display_name` (e.g. `嘉信 Schwab (美股)`); dense tables use the short code, headers/drawers use
the full name, a documented rule says which surface uses which. **Touchpoints:** schema (2 cols)
+ serialize + a small style convention in the resolver. **Effort:** M (golden re-baseline like
P1). *Pros:* fits the "dense, data-first" design language — narrow columns get a compact label,
detail views get the full one, and both are canonical; solves the real UX tension (chips vs wide
selects). *Cons:* two fields to keep in sync; more serialization surface; golden re-baseline.

**P4 — Per-surface style guide + one canonical zh constant module (dedupe only).** Keep naming
client-side but define **one** `ACCOUNT_NAMES` constant (imported everywhere) plus a short
written guide ("chips → zh short; tables → zh full; selects → zh full"). Essentially P2's dedupe
minus the API fetch — a pure refactor of the three literals into one. **Effort:** S. *Pros:*
lowest risk; no network, no schema, no tests touched; enforces consistency by construction.
*Cons:* naming still frontend-only and hardcoded (new account = code edit); a "guide" is
discipline, not enforcement.

**P5 — User-editable aliases in settings.** An `account_aliases` config table + a settings panel
so the owner renames accounts without a deploy; a resolver (server or client) applies them.
**Touchpoints:** config table + `api/routers` + settings UI + resolver. **Effort:** M/L (+golden
re-baseline if server-applied). *Pros:* future-proof for the multi-user service (each user names
their own accounts); config-driven. *Cons:* heavy for 1–2 users with 4 fixed accounts; most
build cost of any option for the least near-term payoff.

**Recommendation ranking.** **P2 now** — it removes the actual defect (three diverging copies of
one map) in an afternoon with **no golden/contract churn**, which is the cheapest correct fix.
Schedule **P1 (or P3 if the density tension is real)** as the durable answer *bundled with the
next change that already re-baselines the golden payload*, so the `dashboard_full.json` +
spec-17/18 update is paid once, not twice. **P4** is a strictly-worse P2 (keeps naming
hardcoded) — only pick it if a `/input/context` fetch on those pages is undesirable. **P5** waits
for R-1 multi-user; it has no standalone value at today's scale.

---

## R-5 Dividend income surfaces — 5 design proposals

### Verified data & the crucial "already-built" finding

**The backend already computes both owner-approved surfaces** — this is mostly a frontend job:

- **Historical received-dividends, per year, per currency** already exists as
  `DividendSummary.by_year` (list of `DividendYearRow{year, by_currency}`) +
  `total_by_currency`, built from the `dividends` ledger and **already in the dashboard
  payload** (`portfolio/dashboard_models.py:67-76`; assembled `portfolio/dashboard.py:239-246`;
  wired into `DashboardData.dividends` at `:390`). Native currency, **never summed across
  currencies** (correct per `domain-ledger.md`).
- **Forward/estimated declared income** already exists as `DividendProjection` (current-year
  declared gross/net per currency, `basis="declared_only"`) — `portfolio/dividends.py:37-84`,
  models at `dashboard_models.py:92-105`, wired at `dashboard.py:263-265`. Net already applies
  each account's dividend model incl. 30% US withholding (`dividends.py:70-73`).
- **Ex-dividend calendar** already exists — `ex_dividend_calendar: list[ExDividendItem]`
  (upcoming, held symbols only), `dashboard.py:248-259`, `:391`.
- Source ledger: `dividends(account_id, symbol, date, type[CASH/STOCK/DRIP/NET], gross,
  withholding, net, reinvest_shares, reinvest_price)` (`data_ingestion/schema.py:23-27`);
  announced events in `dividend_events` (`pricing/schema.py:15-20`).

**Invariant that constrains every proposal:** dividends already fold into `adjusted_cost`
(`domain-ledger.md` — TW/MY cash reduces adjusted cost; US DRIP = $0-cost shares). So **any
income surface is DISPLAY-ONLY attribution and must never be added into returns** (the
double-count trap). Forecasts must be labelled forecast-only, mirroring the rebate-forecast
precedent (`markets-and-fees.md` FE-D1).

### Proposals

**P1 — Trailing-12M actuals card + yearly bar chart (dashboard).** A dashboard card showing
TTM net dividends received (per currency) + an ECharts **stacked/grouped bar** of
`DividendSummary.by_year`. **Data:** already served (`DashboardData.dividends`); TTM is a thin
new aggregation over the `dividends` ledger (or client-side sum of the trailing 12 months).
**Endpoints:** none new (reuse the dashboard payload) or a small `/api/dividends/history`.
**Chart:** grouped bar, one series per currency (never a single summed bar — currencies stay
separate). **Effort:** S/M (mostly frontend). *Pros:* highest value / lowest cost — the data
exists; directly delivers the owner's "cumulative historical received-dividends chart"; no
calc/invariant risk (pure display of ledger `net`). *Cons:* TTM needs a clear as-of label;
multi-currency bars need thoughtful color/legend (see the dataviz skill) to avoid implying a
cross-currency total.

**P2 — Forward-estimate from held shares × trailing per-share dividend (with confidence).**
A card estimating *next-12-month* income = `Σ held_shares × trailing-12M per-share dividend`
per symbol, net of each account's model, with an explicit **forecast-only** badge and a
confidence tag (e.g. "based on last 4 payments" vs "1 payment, low confidence").
**Data:** new compute extending `project_dividends` — the current one is `declared_only` (only
counts *already-announced* ex-dates, `dividends.py:63-66`), which understates forward income for
payers who haven't announced yet. **Endpoints:** `/api/dividends/forecast`. **Chart:** none
required (KPI card) or a per-symbol contribution bar. **Effort:** M. *Pros:* answers "what will
I earn?" which declared-only cannot; reuses the account-model net logic already in
`dividends.py`. *Cons:* genuinely a forecast — must be unmistakably labelled and never touch
returns/XIRR; trailing-per-share needs enough history; DRIP accounts complicate "income" (it's
reinvested, not cash) and need a clear treatment.

**P3 — Ex-dividend calendar surface (upcoming timeline).** Render the existing
`ex_dividend_calendar` as an upcoming-events widget/timeline (symbol, ex-date, pay-date, cash
amount). **Data:** already served (`dashboard.py:248-259`). **Endpoints:** none. **Chart:** a
timeline/agenda list, optionally a month calendar. **Effort:** S. *Pros:* near-free (data is in
the payload, unused by the UI today); operationally useful (know when cash/DRIP lands, pairs
with the 待確認匯入 dividend inbox). *Cons:* not "income" per se (timing, not totals); depends on
`dividend_events` fetch coverage per market.

**P4 — Per-symbol yield-on-cost / 回本進度 table.** A table: per symbol, cumulative net cash
dividends received ÷ `original_cost` = 股利回收率 / 回本進度 (a **display-only** metric explicitly
sanctioned by `domain-ledger.md`: "回本進度 / 股利回收率 = cumulative cash dividends /
original_total"), plus yield-on-cost = TTM dividend ÷ original avg cost. **Data:** aggregate
`dividends.net` by symbol + `original_cost` from `portfolio/` (already computed). **Endpoints:**
`/api/dividends/yield-on-cost`. **Chart:** sortable table (design language is table-first).
**Effort:** M. *Pros:* the metric is already blessed by the rules and avoids any double-count
worry (explicitly display-only); very "data-first"; surfaces high-yield-payback positions.
*Cons:* yield-on-cost across currencies must not be blended; needs `original_cost` per symbol
wired to the dividend aggregation; DRIP vs cash accounts define "received" differently.

**P5 — Combined "Dividend Center" page.** A dedicated `/dividends` page bundling P1 (history
bar + TTM card) + P2 (forecast) + P3 (calendar) + P4 (yield-on-cost table) with account/currency
filters. **Data:** union of the above. **Endpoints:** one `/api/dividends/overview` composing the
sections. **Chart:** yearly bar + calendar + tables. **Effort:** L. *Pros:* one coherent home for
everything dividend; matches how the app already has focused pages (cash, ledger, instruments);
best long-term IA. *Cons:* largest build; premature to commit before the individual surfaces
prove their worth; more endpoints/tests.

**Recommendation ranking.** **P1 first, then P3** — both are essentially frontend work over data
already in the dashboard payload, and together they deliver the two owner-approved surfaces (the
yearly received-dividends chart and, as a bonus, the upcoming calendar) at S effort with **zero
money-of-record risk**. Add **P4** next: it's the most analytically valuable dividend view and the
rules already sanction 回本進度 as display-only, so it sidesteps the double-count trap cleanly. Hold
**P2** until the actuals surfaces land — a forecast needs the "forecast-only" framing done right
(rebate precedent) and is the one most likely to be mistaken for return. **P5** is the eventual
home once P1/P3/P4 exist; build it as an *assembly* of proven parts, not up front.

---

## R-6 Holdings notes / buy-thesis field — 5 proposals

### Verified facts

- `transactions.note` **already exists** (`data_ingestion/schema.py:20`, `note TEXT`), as does
  `cash_movements.note` (`:45`). Per-transaction notes are already capturable at the ledger
  level — the question is surfacing them, and whether a *position-level* thesis (distinct from
  a per-trade note) is wanted.
- `instruments` has **no** note/thesis column today (`data_ingestion/schema.py:9-15`).
- The alerts/digest machinery that a "review reminder" would hook into exists
  (`strategy/alerts.py`, digest jobs `scheduler/jobs.py:799-806`).

### Proposals

**P1 — Simple per-instrument note.** Add `instruments.note TEXT`; show/edit it as a textarea in
the symbol drawer and the instrument edit modal (`web/instruments.js` edit form, alongside the
existing 名稱/產業 fields). **Touchpoints:** additive migration (mirrors the existing
`_add_column_if_missing` pattern, `schema.py:58-63`), instrument serialize, drawer + edit UI.
**Effort:** S. *Pros:* dead simple; one note per position is what most people mean by
"buy thesis"; no new table; reuses the instrument edit surface. *Cons:* single free-text blob —
no history, no review dates, no structure; overwritten on edit (loses the original thesis unless
the user appends).

**P2 — Surface the existing `transactions.note` as a per-symbol timeline.** No schema change —
render each symbol's trade notes chronologically in the detail drawer (a "why I traded"
timeline). **Touchpoints:** a `/api/.../notes` read (or include notes in the existing detail
payload) + drawer UI. **Effort:** S. *Pros:* zero new storage; ties rationale to the actual
decision point (the trade); the field is *already being written* by the import/manual flows.
*Cons:* per-trade, not per-thesis — a standing "why I hold this" doesn't map to a single trade;
notes are entered at trade time and rarely revisited.

**P3 — Dedicated journal table.** `journal_entries(id, symbol, entry_date, kind[thesis/review/
exit], body, review_date)` — multiple dated entries per symbol, so the thesis has history and
scheduled reviews. **Touchpoints:** new table + CRUD router + a journal panel in the drawer/a
small page. **Effort:** M. *Pros:* the "real" answer — captures thesis evolution, review cadence,
and exit criteria; append-only history (matches the ledger philosophy); feeds P5's reminder
cleanly. *Cons:* most storage/UI of the note options; arguably over-structured for 1–2 users who
may just want a textarea.

**P4 — Tag-based convictions with filters.** `instrument_tags(symbol, tag)` (e.g. `#core`,
`#high-conviction`, `#trim`, `#watch`) + filter chips on the holdings table. **Touchpoints:** new
table + tag CRUD + filter UI (the table already has chip filtering, `web/app.js:237`).
**Effort:** M. *Pros:* fast to scan/sort a portfolio by conviction; complements rather than
replaces a note; pairs naturally with allocation/rebalance views. *Cons:* tags aren't a thesis
(no "why"); taxonomy sprawl without discipline; more of an organizing tool than a journal.

**P5 — Markdown notes + review-reminder hook into alerts/digest.** P1/P3's note plus a
`review_date`; when a review comes due, a digest/alert item fires ("TSMC thesis review due")
via the existing alert-compute + digest pipeline (`strategy/alerts.py`, `scheduler/jobs.py:799-806`).
**Touchpoints:** note + `review_date` storage + one new alert rule + digest line. **Effort:** M/L.
*Pros:* turns a passive note into an active discipline (the highest behavioural value — it makes
you *revisit* theses); reuses proven alert/digest infra. *Cons:* most surface area; a new alert
rule needs config + tests; risk of notification noise if review cadence is mis-set.

**Do-nothing assessment.** Declining this loses **only** the *position-level* thesis surface —
and only partly, because `transactions.note` already exists and captures per-trade rationale
(`schema.py:20`), so the owner is not starting from zero. What's kept: the whole money-of-record
system is unaffected (notes are pure annotation, never a calc input, so there is no correctness,
invariant, or double-count risk either way). What's lost: a durable "why I hold this / when to
re-check" record — which for a long-horizon multi-year portfolio is genuinely useful for avoiding
thesis drift, but is a *behavioural nicety, not a system requirement*. At 1–2 users the honest
call is that **P2 (surface the notes that already exist) captures ~70% of the value for almost no
cost**, and building P3/P5 should wait for an explicit owner "yes, I will use a journal" — a
journal nobody writes in is pure carrying cost. Do-nothing is a defensible choice; do-P2 is the
better one because it unlocks existing-but-hidden data.

---

## Executive summary

| Section | Recommended option | Effort |
| --- | --- | --- |
| **R-1** Multi-user extensibility | Do **Phase 0 + Phase 1** now (connection-audit guardrail + split market tables into `market.db`); lock in **separate connections over ATTACH** and **`control.db` for auth**; defer Phases 2–4 until a real 2nd user | Phase 0 **S**, Phase 1 **M** (rest **L**, deferred) |
| **R-2** Data-growth capacity | 10+ yr headroom at all scenarios; act on retention in order: **llm_usage (quota-safe roll-up) → external_snapshots TTL → job_runs prune (copy `action_log`) → alert_events TTL → organized_news TTL**; watch backup-rotation disk, not RAM | **S–M** per table |
| **R-3** Sector normalization | **P1 read-time canonical map now**, graduate to **P2 canonical vocab + dropdown + one-time migration**; use **P5 LLM cleanup** as the migration-map generator | P1 **S**, P2 **M** |
| **R-4** Account naming | **P2 shared frontend resolver now** (kills the 3-copy drift, no golden churn); bundle **P1 server-side `display_name`** (or **P3** two-tier) with the next change that already re-baselines the golden payload | P2 **S**, P1/P3 **M** |
| **R-5** Dividend surfaces | **P1 TTM card + yearly bar** + **P3 ex-div calendar** first (backend data already in the dashboard payload), then **P4 yield-on-cost / 回本進度**; hold **P2 forecast** until framing is right; **P5 Dividend Center** last | P1 **S/M**, P3 **S**, P4 **M** |
| **R-6** Holdings notes | **P2 surface existing `transactions.note` as a per-symbol timeline** (zero new storage); add **P1 per-instrument note**; build **P3 journal / P5 reminders** only on an explicit owner commitment | P2 **S**, P1 **S**, P3/P5 **M/L** |

---

*Senior-review note: line citations were re-verified against the working tree on 2026-07-17.
Key load-bearing corrections vs. the brief: (a) `auth_*` must live in a central control-plane
DB, not a per-user ledger (R-1 risk 4); (b) R-5's two owner-approved surfaces are already
computed and served in the dashboard payload (`DividendSummary.by_year`, `DividendProjection`,
`ex_dividend_calendar`) — the work is overwhelmingly frontend; (c) the `llm_usage` prune trap is
confirmed at the source: `budget_remaining()` subtracts `Σ llm_usage.cost`
(`shared/llm_config.py:284-290`), so a naive delete inflates quota.*
