# Spec 04a — Insight Composer: design objects + CRUD Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development /
> executing-plans. Steps use `- [ ]`. Work in an isolated git worktree off `main`.

**Goal:** The static "design object" layer of spec 04 (AI self-evolution): the 5 composer tables
+ evolution config, their store, and the CRUD/schedule/calibration-selector/evolution-config API —
**no LLM calls, no insight generation, no evaluation** (those are 04b/04c). This is the foundation
04b (generation + R1–R8 gating) and 04c (backtest + calibration + shadow) build on.

**Architecture:** New `portfolio_dash/llm_insight/composer_store.py` owns 4 tables
(strategy_prompts, insight_types, insight_type_strategies, calibration_prompts) via `config_store`;
evolution config is a single-row config. The API router `portfolio_dash/api/routers/insights.py`
is thin (CRUD + validation + serialize). R1 (scope×variable-scope mismatch) is enforced at
create/update by REUSING `llm_insight.variables.validate_tokens` (06a) — no new validation core.
Schedule mount (4.2) writes a `schedule_config` row (kind=insight, payload=insight_type_id) via a
small helper in `scheduler/jobs.py`; the RUNTIME dispatch of kind=insight is 04b (04a only persists
the binding + returns job_id). Layering: `llm_insight` imports neither `pricing` nor
`data_ingestion`; the router may import `scheduler` helpers (api → scheduler is allowed).

**Tech Stack:** Python 3.12, sqlite3, pydantic v2, FastAPI, mypy --strict, ruff, pytest + TestClient.

**Gates (repo `.venv`):** `python -m pytest -q` · `python -m mypy --strict portfolio_dash` ·
`python -m ruff check`. Baseline: 660 passed / 4 skipped, mypy clean (121 files), ruff clean.

---

## File Structure

**Create:**
- `portfolio_dash/llm_insight/composer_store.py` — 4-table DDL + CRUD + 4.1 cascade + evolution config.
- `portfolio_dash/api/routers/insights.py` — composer API (insight-types, strategy-prompts,
  calibrations, evolution-config, schedule mount, active-calibration).
- `tests/llm_insight/test_composer_store.py`, `tests/contract/test_insights_composer_api.py`.

**Modify:**
- `portfolio_dash/api/app.py` — include the router; lifespan `composer_store.ensure_seeded`.
- `portfolio_dash/scheduler/jobs.py` — add `bind_insight_schedule` / `unbind_insight_schedule`
  helpers (write/delete a `schedule_config` row with kind=insight, payload). NO runtime dispatch yet.
- `tests/conftest.py` — `golden_db` calls `composer_store.ensure_seeded` (empty tables; prior suites stay green).

---

## Task 1: Composer tables + store (strategy_prompts, insight_types, links, calibrations)

**Files:** Create `composer_store.py`, `tests/llm_insight/test_composer_store.py`.

DDL (idempotent; INTEGER PK AUTOINCREMENT; booleans as INTEGER; timestamps ISO Asia/Taipei strings):
```sql
strategy_prompts(id, name, body, enabled DEFAULT 1, archived DEFAULT 0, created_at, updated_at)
insight_types(id, name, scope,                  -- 'per_symbol'|'portfolio'|'on_alert'
  use_system_prompt DEFAULT 1, self_correct DEFAULT 0, universe TEXT, alert_rules TEXT,
  enabled DEFAULT 1, archived DEFAULT 0, job_id TEXT, active_calibration_version INTEGER,
  created_at, updated_at)
insight_type_strategies(insight_type_id, strategy_prompt_id, position,
  PRIMARY KEY(insight_type_id, strategy_prompt_id))
calibration_prompts(id, insight_type_id, version, archived DEFAULT 0, body, cause, created_at)
```

- [ ] **Step 1: Failing tests** for: `ensure_seeded` idempotent; strategy CRUD (create→get→list (exclude
  archived by default)→update→archive); insight_type create with ordered strategies
  (`set_strategies(it_id, [(sp_id,pos)...])` → `get_strategies` returns ordered); calibration
  create (append version N+1) / list (include_archived flag) / archive; `active_calibration_version`
  set/clear. Use an in-memory conn fixture.
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3: Implement** pydantic models (`StrategyPrompt`, `InsightType`, `Calibration`) + CRUD.
  `universe`/`alert_rules` stored as JSON TEXT (parse on read to `dict|list|None`). `next_version(it_id)`
  = max(version)+1 or 1. Booleans round-trip via int. Exclude archived unless asked.
- [ ] **Step 4:** run → PASS; mypy/ruff clean.
- [ ] **Step 5: Commit** `feat(llm_insight): composer store — strategy/insight-type/calibration tables (spec 04.0)`.

## Task 2: Delete/archive cascade (spec 4.1) + evolution config

**Files:** extend `composer_store.py` + its test.

- [ ] **Step 1: Failing tests** for spec 4.1 exactly:
  - delete strategy_prompt: if referenced by any `insight_type_strategies` → raise
    `StrategyInUseError(referencing_insight_type_ids)`; else if it has history (was ever referenced —
    approximate: archived flag or a usage marker) → archive; else hard-delete. (Keep the rule simple:
    referenced → error; otherwise archive when `archived`-eligible else hard delete — document the chosen
    "has history" proxy in a docstring.)
  - delete insight_type: set `archived=1` + clear its schedule binding (job_id→NULL) + archive its whole
    calibration chain; history rows (calibrations) retained (archived).
  - archive calibration version: always `archived=1`; if it was the active version →
    `active_calibration_version=NULL`.
  - evolution config: `get_evolution_config` returns defaults
    `{auto_promote:false, shadow_batches:3, min_samples:8, max_shadows:2, gap_alert_pp:"10"}`
    (gap_alert_pp as Decimal string); `set_evolution_config` upserts (single row via config_store).
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3: Implement** the cascade + a single-row `evolution_config` table (config_store category
  "evolution"). `gap_alert_pp` is a percentage-points Decimal stored/served as string.
- [ ] **Step 4:** run → PASS; mypy/ruff clean.
- [ ] **Step 5: Commit** `feat(llm_insight): composer cascade rules + evolution config (spec 04.1)`.

## Task 3: Schedule-binding helpers (spec 4.2) in scheduler

**Files:** modify `portfolio_dash/scheduler/jobs.py`; add `tests/scheduler/test_insight_binding.py`.

- [ ] **Step 1: Failing tests:** `bind_insight_schedule(conn, insight_type_id, cron, tz="Asia/Taipei")`
  upserts a `schedule_config` row with a deterministic `job_id=f"insight:{id}"`, `kind="insight"`,
  `payload=str(insight_type_id)`, the given cron/tz, enabled=1 — and returns the job_id.
  `unbind_insight_schedule(conn, insight_type_id)` deletes that row. Re-bind updates cron in place.
  (No APScheduler runtime here — pure schedule_config row writes; the kind=insight DISPATCH is 04b.)
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3: Implement** the two helpers writing `schedule_config(job_id, enabled, cron, timezone,
  kind, payload)`. Reuse `create_scheduler_tables`/`ensure` so columns exist. Do NOT add these to the
  static `JOBS` list (they are dynamic, payload-dispatched in 04b).
- [ ] **Step 4:** run → PASS; mypy/ruff clean.
- [ ] **Step 5: Commit** `feat(scheduler): insight-type schedule binding helpers (spec 04.2)`.

## Task 4: Composer API — insight-types + strategy-prompts CRUD + R1

**Files:** Create `portfolio_dash/api/routers/insights.py`; modify `app.py`, `conftest.py`;
create `tests/contract/test_insights_composer_api.py`.

Endpoints (this task): strategy-prompts CRUD + insight-types CRUD with R1.
```
GET/POST /api/strategy-prompts ; PUT/DELETE /api/strategy-prompts/{id}
GET /api/insight-types            → [{id,name,scope,strategies:[{id,name,position}],self_correct,
                                       use_system_prompt,universe,alert_rules,enabled,schedule:{cron}|null,
                                       active_calibration_version, calib_summary:{...}|null}]
POST /api/insight-types ; PUT /api/insight-types/{id} ; DELETE /api/insight-types/{id}
```
- [ ] **Step 1: Failing tests:** CRUD happy paths; DELETE → archived (not gone) + schedule cleared;
  strategy DELETE while referenced → 409 with `referencing` list. **R1 (spec 4.9):** POST/PUT an
  insight_type with `scope="portfolio"` whose strategy set includes a strategy whose body uses a
  `per_symbol` variable → **422** `validation_error` (reuse `variables.validate_tokens(body,"portfolio")`
  → any `scope_violations` ⇒ reject; list offending tokens). `scope="per_symbol"` accepts both.
  `scope="on_alert"` new rows default `enabled=false` (R7). Unknown id → 404. Use the empty golden_db.
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3: Implement** the thin router. R1: for each referenced strategy, run
  `validate_tokens(strategy.body, scope)`; collect scope_violations across strategies; if any and
  scope!="per_symbol" → 422 via `error_body("validation_error", ..., issues=[...])`. Serialize per the
  GET shape (schedule read from the schedule_config row by `job_id=insight:{id}`; calib_summary may be
  null until 04c). Wire into `app.py` (include_router + lifespan ensure_seeded); `conftest.golden_db`
  ensure_seeded.
- [ ] **Step 4:** run → PASS; mypy/ruff clean; prior suites green.
- [ ] **Step 5: Commit** `feat(api): insight-types + strategy-prompts composer CRUD + R1 gate (spec 04.7/4.9)`.

## Task 5: Composer API — schedule, active-calibration, calibrations, evolution-config

**Files:** extend `insights.py` + its test.

```
POST   /api/insight-types/{id}/schedule   {cron}  → bind_insight_schedule → {job_id}
DELETE /api/insight-types/{id}/schedule           → unbind
PUT    /api/insight-types/{id}/active-calibration  {version:int|null}
GET    /api/calibrations?insight_type={id}&include_archived=true
POST   /api/calibrations/{id}/archive
GET    /api/calibrations/{id}/samples              → [] (real shape; populated by 04c)
GET/PUT /api/evolution-config
```
- [ ] **Step 1: Failing tests:** schedule POST creates the kind=insight schedule_config row (cron echoed
  in subsequent GET /api/insight-types `schedule`); DELETE removes it. `scope="on_alert"` insight_type →
  schedule POST **400** (R: on_alert is event-triggered, not scheduled). active-calibration PUT sets the
  version (and accepts null); setting a version that doesn't exist for that insight_type → 400.
  calibrations GET filters by insight_type + include_archived; archive POST soft-deletes + nulls active
  if it was active; samples GET returns `[]`. evolution-config GET returns defaults; PUT round-trips
  (gap_alert_pp as string; bad types → 400).
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3: Implement** the routes over `composer_store` + the scheduler binding helpers. Reject
  scheduling an on_alert insight_type (400). Validate active version exists (or null).
- [ ] **Step 4:** run → PASS; mypy/ruff clean; full suite green.
- [ ] **Step 5: Commit** `feat(api): insight schedule/active-calibration/calibrations/evolution-config (spec 04.2/4.6/4.7)`.

---

## Self-Review (against spec 04 — 04a scope only)
- 4.0 tables → Task 1/2. 4.1 cascade → Task 2/4. 4.2 schedule mount → Task 3/5. 4.6 active version
  selector + evolution-config → Task 2/5. 4.7 API (the CRUD subset) → Task 4/5. 4.9 R1 at create/update
  → Task 4. ✓ (4.3 master, 4.4 evaluate, 4.5 calibration-gen, 4.6 shadow/auto-promote, 4.8 validator,
  R2–R8 runtime, insight GENERATION + insights table → **04b/04c**, intentionally out of 04a.)
- Layering: `llm_insight/composer_store` imports only stdlib + `shared`/`config_store`; the router may
  import `scheduler` helpers (api→scheduler ok). R1 reuses `variables.validate_tokens` (no new core). ✓
- Degradation/non-regression: empty composer tables in `golden_db`; GET /api/insight-types = `[]`;
  prior suites unaffected. ✓
- No money/LLM here (pure CRUD). booleans as int; JSON cols parsed on read. ✓
