# Spec 04b — Insight Generation Engine + Runtime Gating (Loop 1 Self-Run) Plan

> REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]`. Isolated worktree off `main`.

**Goal:** Loop 1 (自運作) of spec 04: assemble (system + strategies + active calibration) → R1–R8
runtime gating → LLM (default role, structured) → store an insight card; the scheduler `kind=insight`
dynamic dispatch; manual trigger (async + poll); and the `alert-scan` job + `alert_events` + on_alert
(R7) trigger. Builds on 04a (composer tables/API, schedule binding). 04c adds evaluate/correct/promote.

**Authoritative contract:** `docs/design-handoff/ai-portfolio-watcher/project/specs/04-ai-self-evolution.md`
— esp. **§4.10 locked decisions**, §4.0, §4.2, §4.9 (R1–R8), and the on_alert rule R7. Read §4.10 first.

**Architecture:** `llm_insight/` owns generation + gating PURELY over fed inputs (it imports neither
`pricing` nor `data_ingestion`; the API/scheduler layer feeds conn-bearing data via `VarContext` exactly
as 06a). The runtime gate is ONE function reused later by 07 preflight. Scheduler runs `kind=insight`
rows by dispatching `run_insight_type(conn, insight_type_id, now)`. Cards are structured JSON
(`complete_structured`, default role) cached by fingerprint.

**Tech Stack:** Python 3.12, sqlite3 + Pydantic (NO ORM), FastAPI, LiteLLM (response_format), APScheduler,
mypy --strict, ruff, pytest + TestClient + pytest-socket (no network; LLM seam monkeypatched).

**Gates (repo `.venv`):** pytest · mypy --strict portfolio_dash · ruff check.
Baseline: **717 passed / 4 skipped, mypy clean (123 files), ruff clean.** Keep green per task.

---

## File Structure
**Create:** `portfolio_dash/llm_insight/cards.py` (Pydantic card + prediction schema),
`portfolio_dash/llm_insight/insights_store.py` (insights table + fingerprint cache + due_at),
`portfolio_dash/llm_insight/assemble.py` (layer assembly: system+strategies+active calibration via
06a render_prompt), `portfolio_dash/llm_insight/gating.py` (R1–R8 pure gate → GateResult),
`portfolio_dash/llm_insight/generate.py` (run_insight_type orchestration — fed inputs),
`portfolio_dash/llm_insight/alerts_bridge.py` (alert_events table + R7 dispatch helpers),
+ tests per module under `tests/llm_insight/` and `tests/contract/`.
**Modify:** `portfolio_dash/llm_insight/composer_store.py` (insight_types += horizon_days, eval_prompt),
`portfolio_dash/llm_insight/variables.py` (date vars now/card_created_at/eval_date),
`portfolio_dash/shared/llm.py` (complete_structured response_format), `portfolio_dash/scheduler/jobs.py`
(kind=insight dispatch + alert_scan job), `portfolio_dash/scheduler/runtime.py` (schedule kind=insight
rows), `portfolio_dash/api/routers/insights.py` (manual run + list cards), `app.py`, `conftest.py`.

---

## Task 1: Composer migration — insight_types += horizon_days, eval_prompt
**Files:** modify `composer_store.py`; extend `tests/llm_insight/test_composer_store.py`.
- [ ] Failing test: `insight_types` gains `horizon_days INTEGER` (default 5) + `eval_prompt TEXT` NULL via
  additive idempotent migration (legacy DB opens); CRUD round-trips both; GET serializes them.
- [ ] Implement (PRAGMA-guarded ALTER, mirror scheduler `_add_column_if_missing`). Commit
  `feat(llm_insight): insight_types horizon_days + eval_prompt (spec 04.10)`.

## Task 2: Date/time variables in the 06a registry
**Files:** modify `variables.py`; extend `tests/llm_insight/test_variables.py`.
- [ ] Failing test: registry adds `now`, `card_created_at`, `eval_date` (category system, scope portfolio,
  available True). `value_for` renders ISO-8601 +08:00 strings from `VarContext` (fed: `now`, and for eval
  context `card_created_at`/`eval_date`); absent eval-context → render `now` only / null gracefully.
- [ ] Implement: extend `VarContext` with `card_created_at`/`eval_date` optional; `value_for` formats via
  the project tz helper. Commit `feat(llm_insight): date/time prompt variables ISO-8601+08:00 (spec 04.10)`.

## Task 3: `complete_structured` response_format enhancement
**Files:** modify `shared/llm.py`; extend `tests/` llm tests.
- [ ] Failing test: `complete_structured(..., schema)` passes `response_format` derived from the Pydantic
  schema to `litellm.completion` when the model supports it (assert kwarg via monkeypatched completion);
  on provider error / unsupported → falls back to current prompt+parse+retry; still logs usage + budget gate.
- [ ] Implement: build a json_schema `response_format` from `schema.model_json_schema()`; pass it; guard
  unsupported providers (try/except → retry without it). Keep failover + retry-once. Commit
  `feat(shared): complete_structured response_format enforcement w/ graceful fallback (spec 04.10)`.

## Task 4: Insight card + prediction schema
**Files:** create `cards.py`, `tests/llm_insight/test_cards.py`.
- [ ] Failing test: `Prediction{metric:Literal["price_change","volatility","relative"], direction, target_pct:Decimal|None, horizon_days:int}`;
  `InsightCard{title, summary, body_md, tags:list[str], symbol:str|None, confidence:int|None, prediction:Prediction|None}`;
  validation (confidence 0–100; confidence required when prediction present → validator); JSON round-trip
  (Decimal as string via to_wire). Commit `feat(llm_insight): insight card + prediction schema (spec 04.10)`.

## Task 5: insights store — table + fingerprint cache + due_at
**Files:** create `insights_store.py`, `tests/llm_insight/test_insights_store.py`.
- [ ] Failing test: DDL per §4.10 (insights table incl. is_shadow, calibration_version, fingerprint,
  prediction JSON, horizon_days, due_at, input_snapshot, model, cost_usd). `fingerprint(insight_type_id,
  assembled, snapshot_digest, prompt_version)` = sha256 hex. `find_by_fingerprint` → cached card or None
  (same-day identical inputs hit). `add_card` computes `due_at = created_at + horizon (trading days when
  horizon_basis=trading_days)`; narrative-only card → due_at NULL. `list_cards(insight_type_id?, symbol?)`.
  Trading-day add via a small helper (weekday skip; market holidays out of scope v1 — document).
- [ ] Implement. Commit `feat(llm_insight): insights store + fingerprint cache + due_at (spec 04.10)`.

## Task 6: Layer assembly (system + strategies + active calibration)
**Files:** create `assemble.py`, `tests/llm_insight/test_assemble.py`.
- [ ] Failing test: `assemble_layers(conn-fed inputs) -> [{kind:"system"|"template"|"calibration", name, rendered}]`
  in the HARD order: system (if use_system_prompt) + strategy1..n (ordered, enabled only) + active
  calibration (if self_correct and active version exists). Uses 06a `render_prompt` per layer with the fed
  `VarContext`. Disabled/archived strategies skipped. Returns the joined prompt + the layer list (for 07
  preview). Pure over fed inputs (no conn read of vars here — fed in). Commit
  `feat(llm_insight): layer assembly system+strategies+calibration (spec 04.0)`.

## Task 7: Runtime gating R1–R8 (the shared gate)
**Files:** create `gating.py`, `tests/llm_insight/test_gating.py`.
- [ ] Failing test: `evaluate_gates(ctx) -> GateResult{verdict:"blocked"|"degraded"|"clean", gates:[{id,lv,msg,reason?}]}`
  covering R1 (scope×var mismatch → blocked, reason R1_scope_mismatch, reuse validate_tokens), R2 (universe
  empty → blocked R2_universe_empty; auto-removed symbols → info), R3 (no live templates → blocked
  R3_no_live_templates), R4 (missing price per symbol → deterministic data-anomaly path flag, not LLM),
  R5 (var unavailable → degraded, proceed), R6 (budget exhausted → blocked R6_quota), R7 (on_alert filter +
  24h debounce key), R8 (one combo run = one card; per_symbol one per symbol). `master_missing` reason when
  self_correct + master unset (warn, cards still generate). Pure function over a fed context object.
  **This is the single gate code 07 preflight will reuse.** Commit
  `feat(llm_insight): R1–R8 runtime gate (shared with preflight) (spec 04.9)`.

## Task 8: Generation orchestration — run_insight_type
**Files:** create `generate.py`, `tests/llm_insight/test_generate.py` + `tests/contract/test_insight_generate.py`.
- [ ] Failing test: `run_insight_type(conn, insight_type_id, *, now)`:
  gate → for per_symbol iterate resolved universe (R8 one card/symbol; R4 missing price → deterministic
  "資料異常" card, zero LLM); fingerprint cache hit → reuse (no LLM); else assemble → `complete_structured`
  (default role, InsightCard schema, monkeypatched) → store card (is_shadow=0, calibration_version=active).
  R6 mid-iteration budget exhaustion → stop remaining, job_runs partial, produced cards kept. Every block
  writes job_runs(status=skipped, reason). Records cost. Conn-bearing inputs (dashboard data, snapshots,
  prices) are FED IN, not read here. **LOCKED layering (controller, per architecture.md):**
  `llm_insight/{assemble,gating,cards,variables,insights_store,composer_store,generate}` stay PURE
  (no pricing/data_ingestion import). `run_insight_type` receives FED per-symbol `VarContext` list +
  injected gate inputs; it only assembles → `complete_structured` → stores. The conn-bearing input loader
  (dashboard data + price history + external snapshots + fx → `VarContext`) lives in the **api service
  layer** `portfolio_dash/api/insight_service.py` (api MAY import pricing/portfolio/llm_insight, same as
  06a's prompts.py). Scheduler stays trigger-only: it exposes `register_insight_runner(fn)` + dispatches
  kind=insight to the registered runner; `app.py` wires `register_insight_runner(insight_service.run_for_id)`
  at startup (no scheduler→api import). So `generate.py` is pure; `insight_service.py` is the only seam that
  reads pricing/portfolio.
- [ ] Implement. Commit `feat(llm_insight): run_insight_type generation orchestration (spec 04.0/4.9 R4/R6/R8)`.

## Task 9: Scheduler kind=insight dispatch + manual run API
**Files:** modify `scheduler/jobs.py`, `scheduler/runtime.py`, `api/routers/insights.py`, `app.py`;
tests `tests/scheduler/test_insight_dispatch.py`, `tests/contract/test_insight_run_api.py`.
- [ ] Failing test: a `schedule_config` row with kind=insight, payload=insight_type_id is dispatched by
  running `run_insight_type(payload)` (not the static JOBS map); `runtime.py` schedules kind=insight rows.
  `POST /api/insight-types/{id}/run` → 202 + run_id (async daemon, own session, like spec 15 `/run`);
  `GET /api/insight-types/{id}/runs?limit=` returns job_runs filtered by payload with skipped reason enum.
  `GET /api/insights?insight_type=&symbol=` lists stored cards.
- [ ] Implement. Commit `feat(scheduler,api): kind=insight dispatch + manual run + card list (spec 04.2)`.

## Task 10: alert-scan job + alert_events + on_alert (R7)
**Files:** create `alerts_bridge.py`; modify `scheduler/jobs.py`; tests
`tests/llm_insight/test_alerts_bridge.py`, `tests/scheduler/test_alert_scan.py`.
- [ ] Failing test: `alert_events` table (id, rule_id, symbol, fired_at, consumed flags per task). An
  `alert_scan` job computes spec-03 alerts (fed from the dashboard/strategy alert computation — read in the
  job, not page load) → writes events. R7 dispatcher: for each new event, each ENABLED on_alert insight_type
  subscribing to that rule (alert_rules 'all' or contains rule_id) → run_insight_type (one card per
  (task,rule,symbol)), 24h debounce keyed (task,rule,symbol); alert card forces short horizon (≤3 trading
  days) via the on_alert system-prompt note. shadow_on_alert default false. Commit
  `feat(scheduler,llm_insight): alert-scan + alert_events + on_alert R7 dispatch (spec 04.9 R7/4.10)`.

---

## Self-Review (04b scope)
- §4.10 card/prediction/confidence → T4; date vars → T2; response_format → T3; fingerprint/due_at/trading-day
  → T5; assembly order → T6; R1–R8 (shared w/ 07) → T7; generation incl R4/R6/R8 + cache → T8; kind=insight
  dispatch + manual run → T9; alert-scan/events/R7 + short horizon + shadow_on_alert → T10. ✓
- Layering: keep `llm_insight/{assemble,gating,cards,variables,insights_store,composer_store}` import-clean;
  the ONE orchestration seam that reads pricing/portfolio is isolated (generate.py or api/) per architecture.md
  — state the decision. Reuse 06a render_prompt + validate_tokens (no new cores). ✓
- Degradation: missing price → deterministic card; var unavailable → degraded; budget → partial; empty DB →
  GET []; golden_db stays green. No money/float; cost recorded via llm_usage. ✓
- OUT of 04b (→ 04c): evaluate_insights, generate_calibrations, shadow/auto-promote, master role path,
  /api/ai-score, calibration validator, evolution_config new fields (defer_limit_days/horizon_basis/
  shadow_on_alert consumed here but the FIELDS added in 04c's config task — 04b reads them with safe defaults).
