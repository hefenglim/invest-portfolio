# Spec 04c — Self-Evolution Loops (Evaluate / Calibrate / Promote) Plan

> REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps `- [ ]`. Isolated worktree off `main`.

**Goal:** Loops 2–4 of spec 04 on top of 04a (composer) + 04b (generation): **Loop 2 自評分**
(`evaluate_insights` daily — objective quant_hit + master narrative_score, with pending_data anti-poison),
**Loop 3 自校正** (`generate_calibrations` weekly — master writes a new calibration version, validated),
**Loop 4 自升級** (shadow evaluation + auto-promote), the **master LLM role** completion path,
`/api/ai-score`, the calibration validator, and the `evolution_config` new fields.

**Authoritative contract:** `specs/04-ai-self-evolution.md` — **§4.10 (locked) first**, then §4.3 (master),
§4.4 (evaluate), §4.5 (calibrate), §4.6 (shadow/promote), §4.7 (ai-score API), §4.8 (safety/validator),
§4.9 R-reasons. Spec 03 alert ids `calib_gap` / `calibration_regression`.

**Architecture (layering — same lock as 04b):** `llm_insight/*` stay PURE (no pricing/data_ingestion):
deterministic scoring (`score_quant`), calibration binning, shadow/promote decision, and the master-LLM
calls live in `llm_insight`. The conn-bearing price reads for quant verification live in the **api service
layer** (`api/insight_service.py`, extends 04b) / the scheduler job, feeding actual values into the pure
`score_quant`. The evaluate/calibrate jobs are STATIC scheduler jobs (added to `JOBS`), scheduler→pricing
allowed; they call into `insight_service` for reads and `llm_insight` for logic.

**Tech:** sqlite3 + Pydantic (no ORM), LiteLLM (master role), APScheduler, mypy --strict, ruff,
pytest + pytest-socket (LLM seam monkeypatched, no network).

**Gates (repo `.venv`):** pytest · mypy --strict portfolio_dash · ruff check.
Baseline: **821 passed / 4 skipped, mypy clean (130 files), ruff clean.** Green per task.

---

## File Structure
**Create:** `llm_insight/evaluations_store.py` (insight_evaluations table + store + ai-score aggregation),
`llm_insight/scoring.py` (pure `score_quant`, calibration binning, miss decision),
`llm_insight/master.py` (master-role narrative scoring + calibration generation + validator — pure LLM calls),
`llm_insight/promote.py` (pure shadow/auto-promote decision), tests per module.
**Modify:** `llm_insight/composer_store.py` (evolution_config += defer_limit_days, horizon_basis,
shadow_on_alert), `shared/llm.py` + `shared/llm_config.py` (master-role selection + role param),
`api/insight_service.py` (quant actual-value loader; shadow generation hook), `api/routers/insights.py`
(GET /api/ai-score; calibrations/{id}/samples real), `scheduler/jobs.py` (evaluate_insights +
generate_calibrations JOBS), `app.py`, `conftest.py`.

---

## Task 1: evolution_config new fields + API
**Files:** `composer_store.py`, `api/routers/insights.py`; extend their tests.
- [ ] Failing test: `get_evolution_config` defaults now include `defer_limit_days:5`,
  `horizon_basis:"trading_days"`, `shadow_on_alert:false` (plus existing auto_promote/shadow_batches/
  min_samples/max_shadows/gap_alert_pp). PUT round-trips; bad `horizon_basis` (not in {trading_days,
  calendar_days}) → 400. Idempotent migration for existing single-row config.
- [ ] Implement. Commit `feat(llm_insight,api): evolution_config defer/horizon_basis/shadow_on_alert (spec 04.10)`.

## Task 2: Master LLM role completion path
**Files:** `shared/llm_config.py`, `shared/llm.py`; extend `tests/shared/test_llm.py`.
- [ ] Failing test: `select_role_models(conn, primary, fallback)` returns ordered enabled models for any
  role pair; `complete_structured(..., role=LLMRole.MASTER)` + `complete_text(..., role=...)` select the
  master chain (MASTER, MASTER_FALLBACK); master unset → `AINotActivated`. DEFAULT remains the default
  (back-compat: existing callers unchanged). Usage logged with the agent label.
- [ ] Implement: generalize `select_models` → `select_role_models`; add optional `role` param (default
  preserves vision/default behavior). Commit `feat(shared): master-role LLM selection + role param (spec 04.3)`.

## Task 3: insight_evaluations store + ai-score aggregation
**Files:** create `evaluations_store.py`, `tests/llm_insight/test_evaluations_store.py`.
- [ ] Failing test: DDL per §4.10 (status pending_data|scored|undetermined, quant_hit, narrative_score,
  miss, actual_value, defer_count, is_shadow, calibration_version). `add_evaluation`, `mark_pending`/
  `bump_defer`, `due_insights(now)` (insights with due_at<=now and no scored eval), version rollups:
  `combo_score(insight_type_id)` (n, avg narrative, miss rate, quant hit rate), `calibration_bins`
  (confidence bucket → claimed vs actual hit, calibration error pp), `ai_score()` (totals/by_combo/
  calibration_bins/rows). Shadow rows excluded from the displayed active score but kept for promote.
- [ ] Implement. Commit `feat(llm_insight): insight_evaluations store + ai-score aggregation (spec 04.4/4.7)`.

## Task 4: Pure scoring (quant_hit + miss + calibration)
**Files:** create `scoring.py`, `tests/llm_insight/test_scoring.py`.
- [ ] Failing hand-checked tests (Decimal, no float):
  `score_quant(prediction, actual) -> bool|None`: price_change (direction; +target_pct if set; e.g. pred
  up/+3%, actual +3.02% → hit; actual +1% → miss), volatility (regime match), relative (symbol vs
  benchmark return). `actual=None` → returns None (caller marks pending_data, NOT miss).
  `decide_miss(quant_hit, narrative_score, threshold)` → combined miss. `calibration_error(rows)` → pp.
- [ ] Implement pure functions. Commit `feat(llm_insight): pure quant scoring + calibration error (spec 04.4)`.

## Task 5: Master narrative scoring + calibration generation + validator
**Files:** create `master.py`, `tests/llm_insight/test_master.py`.
- [ ] Failing test (master LLM seam monkeypatched):
  `score_narrative(card, snapshot_then, actual_now, *, conn) -> {narrative_score:int, miss:bool, note}`
  via master role `complete_structured`. `generate_calibration(active_body, miss_samples, bins, *, conn)
  -> {body, cause}` via master role; the system prompt carries the §4.8 safety lock (append-only,
  reconstruct+trim, word cap, no vague/predictionless filler). `validate_calibration(body) ->
  (ok, reasons)`: keyword denylist (越權/幣別混算 phrases) + a single master LLM review pass; invalid →
  rejected (not written). Master unset → raises AINotActivated (pipeline pauses, cards still generate).
- [ ] Implement. Commit `feat(llm_insight): master narrative scoring + calibration generation + validator (spec 04.3/4.5/4.8)`.

## Task 6: Loop 2 — evaluate_insights daily job
**Files:** `scheduler/jobs.py`, `api/insight_service.py`; tests `tests/scheduler/test_evaluate_insights.py`.
- [ ] Failing test: `evaluate_insights(conn, *, now)` job: for each due insight → load actual value
  (insight_service reads pricing: price at create vs at due; missing/halted → None) → `score_quant`;
  actual None → status `pending_data` + `bump_defer`; defer_count > `defer_limit_days` (trading days) →
  status `undetermined` (excluded from calibration/score, never miss). Else master `score_narrative`
  (skipped/`narrative_score=None` if master unset) → `decide_miss` → status `scored` → write evaluation.
  Registered in JOBS (daily). Master-unavailable degrades gracefully (quant-only scored).
- [ ] Implement. Commit `feat(scheduler,llm_insight): evaluate_insights daily job w/ pending_data anti-poison (spec 04.4/4.10)`.

## Task 7: Loop 3 — generate_calibrations weekly job
**Files:** `scheduler/jobs.py`; tests `tests/scheduler/test_generate_calibrations.py`.
- [ ] Failing test: `generate_calibrations(conn, *, now)` job: per self_correct insight_type with resolved
  samples ≥ `min_samples` AND a trigger (≥3 consecutive miss / miss-rate > gap_alert_pp / output-rule
  violation) → gather active body + miss samples + bins → master `generate_calibration` → `validate_
  calibration` → on ok append `calibration_prompts` version+1 (append-only). min_samples not met or no
  trigger → no-op. Master unset → job logs paused (no crash). Registered in JOBS (weekly).
- [ ] Implement. Commit `feat(scheduler,llm_insight): generate_calibrations weekly job (spec 04.5/4.8)`.

## Task 8: Loop 4 — shadow generation + auto-promote
**Files:** create `promote.py`; `api/insight_service.py` (shadow hook), `scheduler/jobs.py`; tests
`tests/llm_insight/test_promote.py`, `tests/scheduler/test_shadow_promote.py`.
- [ ] Failing test: when active_calibration_version ≠ latest non-archived version → latest is the SHADOW.
  During a run, if a shadow exists (and not on_alert when shadow_on_alert=false, and within max_shadows),
  `insight_service` generates BOTH active card (shown) and shadow card (is_shadow=1, hidden) — shadow goes
  through evaluation like active. `decide_promotion(active_scores, shadow_scores, cfg) -> "promote"|"hold"`
  (pure): shadow batches ≥ shadow_batches AND shadow not worse than active → promote. In evaluate/promote
  step: promotion → set active_calibration_version=shadow (if auto_promote) else flag for UI. Active rolling
  score worsens (n≥8) → emit `calibration_regression` (spec 03 info) via alerts_bridge.
- [ ] Implement (pure decision in promote.py; orchestration in insight_service/job). Commit
  `feat(llm_insight,api,scheduler): shadow eval + auto-promote + regression alert (spec 04.6)`.

## Task 9: /api/ai-score + calibration samples
**Files:** `api/routers/insights.py`; tests `tests/contract/test_ai_score_api.py`.
- [ ] Failing test: `GET /api/ai-score` → {totals, by_combo[], calibration_bins[], rows[]} from
  `evaluations_store.ai_score`. `GET /api/calibrations/{id}/samples` now returns the miss samples that drove
  that version (real, replacing 04a's `[]`). Empty DB → zeroed/`[]`. (CSV export is frontend — note only.)
- [ ] Implement. Commit `feat(api): /api/ai-score + calibration samples (spec 04.7)`.

## Task 10: Wiring + app/conftest + degrade audit
**Files:** `app.py`, `conftest.py`.
- [ ] Failing test: app lifespan creates `insight_evaluations` (golden_db too, EMPTY → all prior suites
  green); evaluate/generate jobs registered + appear in scheduler config; with no master + no data the whole
  loop degrades (cards still generate, evaluations stay pending/quant-only, no crash). Commit
  `feat(app): wire evolution jobs + tables; degrade audit (spec 04.3/4.10)`.

---

## Self-Review (04c scope)
- §4.3 master role → T2/T5. §4.4 evaluate (quant+narrative, pending_data) → T3/T4/T6. §4.5 calibration gen
  → T5/T7. §4.6 shadow/auto-promote/regression → T8. §4.7 ai-score/samples → T3/T9. §4.8 validator/safety
  → T5. §4.10 evolution_config fields + defer/horizon_basis/min_samples gating → T1/T6/T7. ✓
- Layering: pure logic (scoring/promote/master-calls/binning) in `llm_insight`; pricing reads in
  `api/insight_service`; jobs in `scheduler` (→pricing ok); `llm_insight` imports no pricing/data_ingestion. ✓
- Anti-poison: missing actual → pending_data → undetermined after defer cap; min_samples gates calib. ✓
- Degradation: master unset → narrative skipped + calibration paused, cards still generate; budget exhaust →
  pause; empty DB green. Decimal end-to-end; no float; no network in tests (LLM seam monkeypatched). ✓
- After 04c: **cross-module senior review (Opus Max)** over all of 04 (a→c) end-to-end mechanism, then
  update-docs (CHANGELOG) + push (04 fully complete).
