# Design: `scheduler/` — In-process Job Scheduling (pricing refresh, extensible)

- **Date:** 2026-06-10
- **Status:** Approved (design); pending spec review
- **Module:** `portfolio_dash/scheduler/`
- **Depends on:** `shared/` (config_store, db), `pricing/` (`refresh_*` orchestrators + `default_registry`).
  Per `architecture.md`: **scheduler triggers `pricing` / `llm_insight` only — it holds no business
  logic and computes nothing.**
- **New dependency:** `APScheduler` (locked in `stack.md`; not yet in `pyproject.toml`).

## Context & purpose

`scheduler/` runs in-process (APScheduler) and **triggers** existing work on a cadence: today the
`pricing/` refresh orchestrators (`refresh_quotes`, `refresh_history`, `refresh_dividends`), later the
`llm_insight/` batch run and other jobs — added through an extensible **job registry** without rewrites.
Refresh is decoupled from page load (`data-and-pricing.md`); the dashboard reads what is in SQLite.

A future **"Scheduler 設定頁面"** (`web_ui/`, merged with the config page) will edit each job's cadence /
enabled flag and show last-run / next-run / status. This spec builds the backend that page sits on; the
**page UI is deferred to `web_ui/`** (same split as the LLM cost-info page).

## Decisions (settled 2026-06-10, human sign-off)

1. **DB-backed schedule config** via the reusable `config_store` create-always / seed-once framework, so
   the future settings page edits cadence/enabled directly.
2. **Per-market post-close cadence**, where each exchange's close time is an **auto-filled recommended
   default that the user can override** (custom time per job). Plus a daily history/dividend sweep. Plus
   an always-available **manual ad-hoc trigger**.
3. **`job_runs` history table** (started / finished / status / detail) to feed the settings page's
   last/next/status and aid debugging.
4. **Extensible job registry**: jobs are named units; a new job = add a `JobSpec` + it auto-seeds its
   default schedule row. Future jobs (`insight`, confirmed auto-import checks, data-quality sweeps) slot
   in without reworking the base.
5. **APScheduler in-process, triggers-only.** No business logic in `scheduler/`.

## Data model / tables (`config_store` category `"scheduler"`, Decimal n/a)

- **`schedule_config`** — one row per registered job:
  - `job_id` TEXT PK
  - `enabled` INTEGER (0/1)
  - `cron` TEXT — an APScheduler-style cron expression (e.g. `0 14 * * mon-fri`)
  - `timezone` TEXT — IANA tz (e.g. `Asia/Taipei`)
- **`job_runs`** — append-only run log:
  - `id` INTEGER PK AUTOINCREMENT, `job_id` TEXT, `started_at` TEXT (UTC ISO),
    `finished_at` TEXT NULL, `status` TEXT (`ok` / `error`), `detail` TEXT NULL (summary or error message)

**Seeding (extensible):** `config_store.ensure_seeded` creates the tables; an idempotent
`ensure_job_rows(conn)` runs at every startup and `INSERT OR IGNORE`s a default row per registered
`JobSpec` — so first-run seeds all jobs **and** a newly-registered job auto-gets its default row on the
next startup, while existing rows keep the user's edits.

## Job registry (code)

```
JobSpec(id, func, default_cron, default_timezone, default_enabled, description)
JOBS: list[JobSpec]   # the registry; new jobs are appended here
```

`func` signature: `func(conn, *, now: datetime) -> str` — does the trigger+wiring and returns a short
run summary (stored in `job_runs.detail`). It assembles its own inputs (registry, work-list) and calls
the lower-layer orchestrator; it never computes.

### v1 registered jobs

| job_id | func | default cadence (editable) | tz |
| --- | --- | --- | --- |
| `quotes_tw` | refresh TW quotes+FX | after TW close, weekdays | `Asia/Taipei` |
| `quotes_us` | refresh US quotes+FX | after US close, weekdays | `America/New_York` |
| `quotes_my` | refresh MY quotes+FX | after MY close, weekdays | `Asia/Kuala_Lumpur` |
| `history_daily` | `refresh_history` (all markets) | once daily | reporting tz |
| `dividends_daily` | `refresh_dividends` (all markets) | once daily | reporting tz |

(Exact default times are config defaults chosen to fall after each exchange's close; the user overrides
per job in the settings page. FX is refreshed alongside each market's quote job since FX has no market.)

## Work-list builder

`build_worklist(conn, market: Market | None) -> tuple[list[InstrumentRef], list[FxPair]]`:
- Reads the `instruments` table (SQL read of a shared DB table; no `data_ingestion` code import, keeping
  the dependency direction clean), filtered by `market` when given.
- Maps rows → `InstrumentRef`: board is `""` (US) / `".KL"` (MY) / **`"TWSE"` (TW, v1 default)**.
- `fx_pairs`: the reporting-currency pairs needed for the combined view (USD/TWD, USD/MYR, MYR/TWD),
  derived from the configured reporting currency + the account funding/quote currencies.

**Known v1 limitation (documented):** the `instruments` table has no `board` column, so TW board defaults
to `TWSE`. For **quotes** this is harmless — the registry order `twse → tpex → yfinance` falls through to
the right source. For **history** (which needs the exact `.TW`/`.TWO` yfinance suffix), a TPEx-listed TW
stock may fail its backfill (recorded in the run summary, never crashes). Precise TW board resolution
(an `instruments.board` column or a resolver) is a **`pricing/` / `data_ingestion/` follow-up**, out of
scope here.

## Runtime

- `run_job(conn, job_id, *, now) -> None` — the shared execution path: write a `job_runs` start row →
  call the `JobSpec.func` → update the row with `finished_at` + `status` + `detail`. A job exception is
  **caught, logged as `status="error"` with the message, and swallowed** so one failing job never crashes
  the scheduler or other jobs (graceful degradation, `data-and-pricing.md`).
- `trigger_job(job_id)` — manual ad-hoc run: opens a connection and calls `run_job` immediately. The
  future settings page (and any manual-trigger route) calls this.
- `build_scheduler() -> BackgroundScheduler` — reads `schedule_config`, and for each **enabled** job adds
  an APScheduler `CronTrigger` (from the row's `cron` + `timezone`) whose action is `run_job(job_id)`.
- `start()` / `shutdown()` lifecycle wrappers.

## Architecture / boundaries

- `scheduler/` imports `pricing` (`refresh_*`, `default_registry`) and `shared` (`config_store`, `db`).
  It reads the `instruments` table via SQL. It does **not** compute, write ledgers, fetch prices itself,
  or render UI.
- The `llm_insight` job and other future jobs register here when their modules exist (triggers only).

## Error handling / degradation

- A job failure → `job_runs.status="error"` + message; never propagates to crash the scheduler.
- A pricing fetch partial outage is already handled inside `pricing` (failed keys recorded in the
  `RefreshSummary`); the job records the summary in `detail`.

## Testing strategy (mock the lower layers; no real timing, no live network)

- **Seeding:** `ensure_scheduler_seeded` creates tables; `ensure_job_rows` is idempotent and seeds a row
  per registered job; a newly-appended `JobSpec` adds its row on re-run while existing edited rows persist.
- **run_job logging:** success writes `ok` + summary; an exception in `func` writes `error` + message and
  does **not** raise.
- **Job wiring:** inject a fake `refresh_*` / `default_registry` / work-list and assert the per-market job
  passes the correct (market-filtered) work-list and FX pairs.
- **work-list builder:** instruments rows → correct `InstrumentRef` board per market; market filter works.
- **Scheduler construction:** only **enabled** jobs get a trigger; the trigger reads `cron`/`timezone`
  from `schedule_config`. (Test the construction/registration, not wall-clock firing.)
- **Manual trigger:** `trigger_job` runs the job and logs a run.
- No real network; APScheduler timing is not slept-on in tests.

## Out of scope (deferred / other modules)

- The **Scheduler 設定頁面** UI + last/next/status display + manual-trigger button (`web_ui/`).
- The `llm_insight` scheduled job (lands with `llm_insight/`; the registry slot is ready).
- Confirmed auto-import checks + data-quality sweeps (future jobs).
- Precise TW board resolution for history (a `pricing/` / `data_ingestion/` follow-up).

## Designed-in flexibility (per human directive)

The job registry + DB-backed `schedule_config` (on the shared `config_store` framework) + the idempotent
per-job seeding mean **new schedules and new job types are config edits + a `JobSpec` append, not
rewrites** — and the future settings page already has its backend (config rows + `job_runs`). YAGNI holds:
no distributed scheduler, no extra broker (`stack.md` — APScheduler in-process is enough for 1–2 users).

## Staging (the plan will sequence)

1. Add `APScheduler` dep; `scheduler/` package skeleton.
2. `schedule_config` + `job_runs` tables via `config_store`; `JobSpec` registry + `ensure_job_rows`
   (idempotent per-job seeding) + default cron/tz constants.
3. `build_worklist(conn, market)` (instruments → InstrumentRef + FX pairs).
4. The pricing-refresh job functions (`quotes_<mkt>`, `history_daily`, `dividends_daily`) over
   `pricing.refresh_*` + `default_registry`.
5. `run_job` (job_runs logging + swallow-and-log on failure) + `trigger_job` (manual).
6. `build_scheduler` (enabled rows → CronTrigger) + `start`/`shutdown`.
