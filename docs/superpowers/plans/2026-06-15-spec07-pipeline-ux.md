# Spec 07 — Insight Pipeline Hub UX (status / preflight / diagnose) Plan

> REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps `- [ ]`. Isolated worktree off `main`.

**Goal:** The observability layer over the spec-04 insight machinery — **no new business logic**
(03/04/06 reused). Three things: (1) a single "task status" API converging scattered status
derivation, (2) dry-run **preflight** that calls the SAME runtime gate as execution (spec 04 §4.9)
+ reuses the spec-06 assembled preview, zero-cost (no LLM, no job_runs), (3) a **diagnose** endpoint
("why didn't it run"). Plus the §7.0 route aliases and the task-view runs query.

**Authoritative contract:** `docs/design-handoff/ai-portfolio-watcher/project/specs/07-pipeline-ux.md`
(read it fully — §7.0–7.6). Decisions locked with the user (2026-06-15): freshness reuses the dashboard
computation; preflight supports a draft body; `/api/insight-tasks/*` is a **full alias of the same
resource** as `/api/insight-types/*` (both paths work; old routes kept).

**Architecture:** read-only convergence. `GET …/status` derivation = a PURE function
(`derive_node_states`) fed facts gathered in the api layer (schedule_config, resolved universe,
freshness, templates, budget/master, last_run). `preflight`/`diagnose` REUSE `llm_insight.gating.
evaluate_gates` (the 04 gate) + `assemble.assemble_layers` / the 06 preview — never re-deriving the gate.
`api/insight_service.py` already builds the gate context for generation; preflight builds the same and
calls the same gate (the §7.2 hard rule: no "preflight passed, run failed" double truth).

**Tech:** sqlite3 + Pydantic, FastAPI, mypy --strict, ruff, pytest + TestClient. No LLM in tests/preflight.

**Gates (repo `.venv`):** pytest · mypy --strict portfolio_dash · ruff check.
Baseline: **932 passed / 4 skipped, mypy clean (135 files), ruff clean.** Green per task.

---

## File Structure
**Create:** `portfolio_dash/llm_insight/pipeline_status.py` (PURE `derive_node_states` + aggregate level —
no conn), `tests/llm_insight/test_pipeline_status.py`, `tests/contract/test_pipeline_hub_api.py`,
`tests/contract/test_pipeline_preflight_api.py`.
**Modify:** `portfolio_dash/api/routers/insights.py` (alias routes + status/preflight/diagnose/runs),
`portfolio_dash/api/insight_service.py` (status fact-gathering + preflight context reuse), `app.py` if
needed. No new tables.

---

## Task 1: `/api/insight-tasks/*` route aliases (full mirror, same resource)
**Files:** `api/routers/insights.py`; `tests/contract/test_pipeline_hub_api.py`.
- [ ] Failing test: every existing `/api/insight-types*` route is reachable under `/api/insight-tasks*`
  with identical behavior (same handler, same resource) — GET list, POST, PUT, DELETE, schedule,
  active-calibration, runs. Old `/api/insight-types*` routes still work unchanged.
- [ ] Implement: register the same router functions under both prefixes (e.g. a second `APIRouter`
  prefix or duplicate `@router` decorators delegating to shared handler fns). No logic duplication.
- [ ] Commit `feat(api): /api/insight-tasks/* alias of insight-types resource (spec 07.0)`.

## Task 2: Node-state derivation (pure) + GET …/status
**Files:** create `llm_insight/pipeline_status.py`; `api/insight_service.py` (gather facts);
`api/routers/insights.py` (route); tests `tests/llm_insight/test_pipeline_status.py` +
`tests/contract/test_pipeline_hub_api.py`.
- [ ] Failing test (pure): `derive_node_states(facts) -> {nodes:{trigger,input,assemble,exec,output},
  level}` per §7.1.1 — trigger (manual→warn / disabled→idle), input (universe empty→fail / stale or
  missing price for THIS task's symbols→warn / R2-removed-within-7d→info), assemble (all templates
  off→fail / some off or R1 mismatch→warn / unapplied calibration→info), exec (quota 0→fail /
  quota<quota_low or (master unset & self_correct)→warn), output (last_run skipped|error→fail /
  partial→warn / never→idle). aggregate `level` = max severity; disabled task → idle.
- [ ] Failing test (contract): `GET /api/insight-tasks/status` → `{as_of, health:{master_ok,
  quota_remaining, last_batch:{at,cards,cost_usd}}, tasks:[{id,name,scope,enabled,level,nodes,last_run}]}`.
  Empty DB → `tasks:[]`, health from llm_config (master_ok=false, quota from budget_remaining).
- [ ] Implement: fact-gathering in `insight_service` (schedule_config kind/cron/enabled; resolved
  universe via the 04b resolver; freshness via the **dashboard freshness computation** for the task's
  symbols; template enabled counts; `budget_remaining` + `get_alert_threshold` (quota_low) +
  `get_role_model_id(MASTER)`; last_run from job_runs excluding is_shadow). `derive_node_states` pure.
  `last_batch` = most recent non-shadow insight run.
- [ ] Commit `feat(api,llm_insight): insight-tasks status API + pure node-state derivation (spec 07.1)`.

## Task 3: Preflight (shares the 04 gate; reuses 06 preview; zero-cost)
**Files:** `api/insight_service.py` (preflight builder), `api/routers/insights.py` (route);
`tests/contract/test_pipeline_preflight_api.py`.
- [ ] Failing test: `POST /api/insight-tasks/{id}/preflight` (and with a draft `body` for an unsaved
  task) → `{gates:[{id,name,lv,msg,fix?}], verdict, assembled_preview:{layers,est_tokens,est_cost_usd}}`.
  Gates IN ORDER: G0 (task enabled), G1 (trigger: manual→fail "won't auto-run"), R1–R6 (from the SAME
  `gating.evaluate_gates` as execution — assert the gate fn is the shared one), G7 (calibration: master
  unset→warn, unapplied version→info). `verdict` = blocked(any fail)|degraded(any warn)|clean.
  `assembled_preview` reuses the 06 assemble/preview (layers system/template/calibration + est_tokens +
  est_cost_usd from the model price). **No LLM call, no job_runs row** (assert neither happens).
  `fix.kind` ∈ {enable_task, create_schedule, enable_template:{id}, edit_universe, edit_templates,
  set_active_calibration}.
- [ ] Implement: build the same gate context generation uses; wrap with G0/G1/G7; assemble preview via
  existing 06 path; est cost = est_tokens × default-model price (no spend). Draft body path: validate +
  assemble a transient task without persisting.
- [ ] Commit `feat(api): insight-tasks preflight — shared 04 gate + 06 preview, zero-cost (spec 07.2)`.

## Task 4: Diagnose + task-view runs
**Files:** `api/routers/insights.py`; extend the contract tests.
- [ ] Failing test: `GET /api/insight-tasks/{id}/diagnose` → same `gates` as preflight (read-only) +
  `first_blocker` (id of first fail gate, or null) + `recent_skips` (last 5 job_runs with status=skipped
  → `[{at, reason}]`, reason is the single enum from 04b). `GET /api/insight-tasks/{id}/runs?limit=20` →
  job_runs filtered by payload=task id, **is_shadow excluded** (04b), skipped rows carry the reason enum.
- [ ] Implement: diagnose = preflight builder (no preview needed, but reuse gates) + job_runs query;
  runs = the existing 04b runs query under the alias.
- [ ] Commit `feat(api): insight-tasks diagnose + task-view runs (spec 07.3/7.4)`.

## Task 5: §7.6 acceptance scenarios (3 failure demos reproducible)
**Files:** `tests/contract/test_pipeline_hub_api.py`.
- [ ] Failing tests reproducing the 3 spec demos end-to-end:
  1. task disabled + unscheduled → diagnose `first_blocker=G0`, fixes enable_task + create_schedule;
     status trigger=idle.
  2. only template disabled → preflight R3=fail + enable_template fix; a real run writes
     job_runs `skipped`/`R3_no_live_templates`; status assemble=fail.
  3. custom universe emptied → status input R2 (auto-removed info / empty→fail); task auto-disabled when
     list empties.
- [ ] Implement any small gaps surfaced. Commit `test(api): spec 07.6 pipeline acceptance scenarios`.

---

## Self-Review (07 scope)
- §7.0 aliases → T1. §7.1/7.1.1 status + node derivation → T2. §7.2 preflight (shared gate + preview,
  zero-cost) → T3. §7.3 diagnose + §7.4 runs (reason enum, is_shadow excluded) → T4. §7.6 demos → T5. ✓
- Hard rule: preflight calls the SAME `gating.evaluate_gates` as generation (no second gate) — asserted. ✓
- No new tables, no LLM calls, no business logic; all derivation read-only over existing data. Reuses
  04 gate + 06 preview + 15 job_runs + 16 budget/master + dashboard freshness. Decimal for cost est. ✓
- Layering: pure derivation in `llm_insight/pipeline_status.py` (no pricing/api import); fact-gathering +
  routes in api (api may read pricing/portfolio/composer). ✓
- After 07: the 04→07 chain backend is COMPLETE → cross-module senior review (light, it's read-only) →
  CHANGELOG → push. Then: foundation-hardening + frontend wiring (spec 19) + spec-17 E2E regression LAST.
