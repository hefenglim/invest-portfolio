# Spec 19 — Frontend Wiring (`api.js`) + Ops Hardening Implementation Plan

> REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps `- [ ]`. Isolated worktree off `main`.

**Goal:** Wire the static `web/` frontend to the real `/api/*` backend through a single fetch layer,
retire all mocks, and land spec-19 ops/保全 — folding in the 5 pre-wiring findings the analysis confirmed.
Backend is complete + green (1009 passed); this connects the UI to it.

**Architecture (locked, from the pre-wiring analysis + decision B + spec 19):**
- ONE fetch layer `web/api.js` (`window.pdApi`); **no page calls `fetch` directly** (spec 19.1).
- **Money model (point-5 rule):** the API delivers every **Decimal as a canonical string** (money / price /
  rate / ratio / cost). The backend is the ONLY place that computes; the frontend **never** does money math.
  Frontend consumes Decimal strings **display-only via `fmt.*`**; for a display-derived value (bar width)
  wrap each operand in `Number()` first; **never** `bare-string.toFixed()` / `+`-sum money on the client.
  Pure counts (`shares`, `tokens`, `confidence`, `n`, `days`, `year`) stay JSON numbers (safe to compute).
- **Dependency order** (do NOT reorder): Phase 0 fetch-layer + smoke harness + Makefile → Phase 1 backend
  completeness (ops, calib_gap, dashboard-insights embed) → Phase 2 page wiring (shell→dashboard→read
  pages→input→settings→insight/pipeline) → Phase 3 cleanup + green. Full Playwright E2E flows + property/
  mutation are **spec 17/18 (the NEXT phase, not here)**; spec 19 ships a per-page **smoke** harness so each
  wired page is guarded against the string-break TypeErrors immediately.

**Tech:** Python 3.12 backend (sqlite3/FastAPI/APScheduler), vanilla JS frontend (no build step),
Playwright (smoke only here), mypy --strict, ruff, pytest + TestClient.

**Gates (repo `.venv`):** pytest · mypy --strict portfolio_dash · ruff check (backend tasks);
Playwright smoke (frontend tasks). Baseline: **1009 passed / 4 skipped, mypy clean (136), ruff clean.**

---

# PHASE 0 — Fetch layer + smoke harness + tooling (the gate; everything depends on it)

## Task 0.1 — Makefile test-target fix (#3 / tooling) — DO FIRST (enables real regression)
**Files:** `Makefile`, `pyproject.toml` (register the `e2e` marker); (no app code).
- [ ] **Corrected premise (review F4):** `tests/unit/` DOES exist (`test_dividend_net.py`) and `tests/e2e/`
  DOES exist (`test_smoke.py`). The REAL bug: `make test` targets `tests/unit tests/contract`, so it **misses
  the bulk of the 1009 tests in `tests/<module>/`** (`tests/strategy/`, `tests/scheduler/`, `tests/llm_insight/`,
  `tests/portfolio/`, `tests/pricing/`, `tests/shared/`, `tests/data_ingestion/`, `tests/api/`).
- [ ] Fix targets so `make all` runs the FULL suite, excluding e2e by an EXPLICIT mechanism:
  `test:` → `$(PY) -m pytest tests --ignore=tests/e2e -q` (whole tree minus Playwright);
  `e2e:` → `$(PY) -m pytest tests/e2e -q`; `regress:` → `$(PY) -m pytest tests --ignore=tests/e2e -q` (+golden);
  register `markers = ["e2e: browser end-to-end (sockets enabled)"]` in `[tool.pytest.ini_options]` so the
  `-m`/path split is clean. `all: ruff mypy test`. Confirm `make all` runs all ~1009 (not just contract).
- [ ] **`make restore` (spec 19.3 §3 — was missing):** add `restore: ` target = stop scheduler → swap DB file
  from `FILE=...` → restart (documented, FILE arg required). Pairs with the `backup_daily` job (Task 1.1).
- [ ] Commit `fix(ops): Makefile runs full suite (excl e2e) + register e2e marker + make restore (spec 19)`.

## Task 0.2 — `web/api.js` single fetch layer (spec 19.1)
**Files:** create `web/api.js`; `tests/e2e/test_api_js_smoke.py` (Playwright, new) OR a jsdom unit test.
- [ ] Implement `window.pdApi = { get(path,params), post(path,body), put(path,body), del(path),
  download(path,body), abortable(key) }`:
  - non-2xx → parse the spec-08 error envelope `{error:{code,message,field,issues}}` → throw
    `PdApiError{status,code,message,field,issues}`; caller catches → `window.toast(message,'fail',code)`.
  - **401** (protected mode) → `window.location.replace('login.html')` (one place).
  - **402/409/503** (LLM degrade) → do NOT redirect; rethrow for the AI block to show degraded state.
  - **Decimal passthrough:** response money strings handed to callers untouched (no parseFloat).
  - `abortable(key)` → AbortController that cancels a prior same-key in-flight request.
- [ ] Test: a Playwright/jsdom smoke that pdApi parses an error envelope into PdApiError, 401 redirects,
  402 rethrows. (If Playwright infra isn't up yet, gate this task on Task 0.3.)
- [ ] Commit `feat(web): api.js single fetch layer — PdApiError, 401/402/409/503, abortable (spec 19.1)`.

## Task 0.3 — Playwright smoke harness (so each wired page is guarded)
**Files:** `tests/e2e/conftest.py` (Playwright + a seeded golden DB served by the app), `tests/e2e/
test_pages_smoke.py`; `pyproject`/`Makefile` Playwright dep already declared (stack.md).
- [ ] **Sockets (review F1 — CRITICAL):** `pyproject.toml` sets `--disable-socket --allow-unix-socket`
  globally (spec 17.1), which BANS the TCP loopback the harness needs (uvicorn port + headless Chromium →
  `localhost:PORT`). The existing `tests/e2e/test_smoke.py` only passes because it `pytest.skip`s at import.
  The harness MUST re-enable loopback for `tests/e2e` ONLY: add `--allow-hosts=127.0.0.1,localhost` (or a
  `socket_enabled`/`@pytest.mark.enable_socket` autouse fixture scoped to `tests/e2e/conftest.py`). Document
  this is the **spec-17-sanctioned exception** — real EXTERNAL network stays banned (LLM/pricing via the
  FakeProvider/FakeCompleter seams); only loopback to the app-under-test is allowed.
- [ ] Harness: start the FastAPI app (StaticFiles `web/` + `/api/*`) against a **seeded golden DB**
  (reuse the golden fixtures), drive a headless browser. A reusable `assert_page_ok(path)` =
  navigate, wait for the page's key root element, assert **zero console errors** (this is what catches the
  Decimal-string `.toFixed` TypeErrors). One passing baseline smoke (login or index) to prove the harness.
- [ ] Per-page smoke tests are ADDED by each Phase-2 task as it wires that page.
- [ ] Commit `test(e2e): Playwright smoke harness over the served app + golden DB (spec 17 seed; used by 19)`.

---

# PHASE 1 — Backend completeness (wire pages against a COMPLETE backend)

## Task 1.1 — Ops 保全: backup_daily + pre-write snapshot + integrity (spec 19.3)
**Files:** `portfolio_dash/scheduler/jobs.py` (+ a `portfolio_dash/ops/backup.py`), `app.py`; tests
`tests/scheduler/test_backup.py`.
- [ ] `backup_daily` job (registry, default 01:30): `sqlite3 .backup` API (not file copy) →
  `data/backups/portfolio_{YYYY-MM-DD}.db.gz`, keep 30, rotate; runs `PRAGMA integrity_check` (fail →
  error run + warn alert); 3-consecutive-fail → warn (spec 03 engine). Pre-write snapshot helper
  (`pre_import_`/`pre_migrate_` prefix) callable before CSV/AI commit + migrations.
- [ ] Tests: backup writes a gz, rotation keeps ≤30, integrity_check failure → error run. (Use a temp dir;
  no real schedule.) Commit `feat(scheduler,ops): daily SQLite backup + pre-write snapshot + integrity (spec 19.3)`.

## Task 1.2 — `last_backup_at` on `/api/dashboard` freshness (spec 19.3)
**Files:** `portfolio_dash/portfolio/dashboard_models.py` (FreshnessReport += `last_backup_at: str|None`),
`portfolio_dash/portfolio/dashboard.py` or the dashboard router (fed, since backup state is ops/file —
read in the router, not the pure calc), `tests/contract/test_dashboard_api.py`.
- [ ] Add `last_backup_at` to the dashboard freshness payload (latest backup file mtime/name, fed by the
  router/ops reader — keep `build_dashboard` pure; the router attaches it like `llm_quota`). Golden updated.
- [ ] Commit `feat(api): dashboard freshness.last_backup_at (spec 19.3)`.

## Task 1.3 — Structured JSON-lines logging (spec 19.4)
**Files:** `portfolio_dash/shared/logging_config.py` (new), `app.py` (configure on startup).
- [ ] stdlib JSON-lines logging → `data/logs/app.log` (rotate 10MB×5); API 5xx include traceback; LLM calls
  log alias/tokens/cost (reconcile with `llm_usage`). No new dependency (stdlib `logging` + a JSON formatter).
- [ ] Tests: a log line is valid JSON with the expected keys; 5xx path logs traceback (caplog).
  Commit `feat(shared): structured JSON-lines logging (spec 19.4)`.

## Task 1.4 — calib_gap wired into the alert engine (#3 / I1)
**Files:** `portfolio_dash/strategy/rules_config.py` (**review F2: add the `calib_gap` rule — it does NOT
exist yet**), `portfolio_dash/strategy/alerts.py` (`compute_alerts_from` gains a fed `calib_gap: Decimal|None`),
`portfolio_dash/api/routers/dashboard.py` + `routers/strategy.py` (compute + pass in), `tests/strategy/
test_rules_config.py` + `test_alerts.py` + `tests/contract/test_alerts_api.py`.
- [ ] **F2 — add the rule first:** `AlertRules` (rules_config.py) currently has 7 rules and NO `calib_gap`
  (explicitly deferred). Add `calib_gap` to the `AlertRules` model + `RULE_META`/`RULE_IDS`
  (default `value="0.15"`, unit `pp`, min/max per spec 03 §3.1 line 43) with serialize/parse round-trip;
  extend `test_rules_config.py`. (Otherwise `rules.calib_gap.enabled` cannot compile.)
- [ ] Keep `compute_alerts_from` PURE: add a `calib_gap: Decimal | None` param; when
  `rules.calib_gap.enabled` and `calib_gap` is not None and `> threshold` → emit the `calib_gap` alert
  (spec 03 §3.1, warn).
- [ ] **F3 — correct gap source:** the gap is a SCALAR Decimal = `scoring.calibration_error(rows)` where
  `rows: list[tuple[int, bool]]` are scored (confidence, hit) pairs; `evaluations_store.ai_score()` returns
  a `calibration_bins` LIST (not a scalar) and ignores min_samples. The CALLER (dashboard + /api/alerts)
  gathers the scored rows, applies the `evolution_config.min_samples` gate (→ None when insufficient,
  per spec 04.10), computes the scalar via `scoring.calibration_error(...)`, and passes it in.
- [ ] **W3 — `calibration_regression` stays event-only:** it is emitted by 04c into `alert_events` (the
  event/bell feed); `/api/alerts` stays the rule-derived view and does NOT surface it. Document this in the
  router (no change to `compute_alerts_from`).
- [ ] Commit `feat(strategy,api): add calib_gap rule + wire from ai-score calibration error (spec 03/04 I1)`.

## Task 1.5 — Dashboard embeds latest N real insight cards (#4 / I3)
**Files:** `portfolio_dash/llm_insight/insights_store.py` (**W2: new `latest_cards(conn, n)` helper**),
`portfolio_dash/api/routers/dashboard.py`, `tests/llm_insight/test_insights_store.py` +
`tests/contract/test_dashboard_api.py`.
- [ ] **W2 — store helper:** `list_cards()` returns ALL cards with no shadow filter / no LIMIT. Add
  `latest_cards(conn, n)` → `WHERE is_shadow = 0 ORDER BY created_at DESC LIMIT n` (keep the SQL in the
  store, not the router).
- [ ] `/api/dashboard` `insights` = the latest **N (e.g. 3)** non-shadow cards, serialized to the dashboard
  shape `{id, title, summary, body_md, symbol, created_at, cost_usd}`. Empty table → `[]` (no LLM — pure read).
- [ ] **W4 — overwrite, don't shadow:** `build_dashboard` keeps `insights: list[InsightCardStub]` = `[]`
  (stays pure); the router **OVERWRITES** `payload["insights"]` AFTER `to_wire(data.model_dump())`, mirroring
  the existing `payload["alerts"]` / `payload["llm_quota"]` injection — so the empty stub list does not shadow
  the real read. Update the golden.
- [ ] Commit `feat(api): dashboard embeds latest N real insight cards via insights_store.latest_cards (spec 08/04 I3)`.

---

# PHASE 2 — Page wiring (each: mock → pdApi, fold money rules, retire mock, add page smoke)

> Per-task checklist (applies to every Phase-2 task): replace the page's `window.*_DATA`/mock-file read with
> `pdApi.*`; route every Decimal field through `fmt.*` (fix the named `.toFixed` break sites → `fmt`/`Number()`
> — **W5: the named list is NOT exhaustive; grep each page for `.toFixed(`/`+`-on-money and audit. Note:
> `(x*100).toFixed` and `Number(x).toFixed` are already string-safe via coercion (e.g. `settings-llm.js:78/368`,
> `insights.html:349`, `rebalance.js`); only BARE `field.toFixed()` and `+`-summation of money break.** Extra
> money sites to fmt: `pipeline-wizard.js:60` / `pipeline-preflight.js:76` quota/cost (Task 2.8));
> **delete the retired mock entirely** (no fallback branch — spec 19 §6; the two documented exceptions:
> detail.js feeTax offline mirror, alerts cache); add a Playwright **page smoke** (loads vs golden DB, zero
> console errors, key element renders). Backend contract tests already guard each endpoint shape.

## Task 2.1 — shell.js global scaffold (nav, quota chip, 401 tie-in)
- [ ] Wire the global shell (nav active state, the AI quota chip, login/lock state) through `pdApi`; the
  quota chip reads `/api/dashboard` `llm_quota`/`/api/llm` and renders via `fmt` (fix any bare `.toFixed`).
  Commit `feat(web): shell global scaffold via pdApi (spec 19)`.

## Task 2.2 — Dashboard (index / app.js) — the central page
- [ ] Wire `app.js` to `GET /api/dashboard` (replace `DASHBOARD_DATA`). **Fix C2 break site**
  `app.js:743 ins.token_cost_usd.toFixed(3)` → `fmt`. **W1 — the insight card read (app.js ~737-743) needs
  THREE field renames** to the Task-1.5 shape: `ins.body`→`ins.body_md` (or `summary`), `ins.generated_at`→
  `ins.created_at`, `ins.token_cost_usd`→`ins.cost_usd` (else the card renders `undefined`). Ensure
  kpis/holdings/returns/allocation/fx/dividends/trend all render via `fmt`; verify NO `+`-sum of money in
  bar/alloc calcs (use `Number()` only for display widths). Embedded `alerts` array (no second build) +
  insights from Task 1.5. Retire `mock-data.js`. Page smoke. Commit
  `feat(web): dashboard wired to /api/dashboard; money via fmt; mock retired (spec 19/08)`.

## Task 2.3 — Symbol detail (detail.js)
- [ ] Wire to `GET /api/symbol/{symbol}/detail` (replace `PD_HISTORY`); keep the feeTax offline mirror
  (spec 03 documented exception). Money via `fmt`. Page smoke. Commit
  `feat(web): symbol detail wired; feeTax mirror kept (spec 19/01)`.

## Task 2.4 — Ledger (ledger.js)
- [ ] Wire to `GET /api/ledgers/*`; money/rate via `fmt` (fix `ledger.js:234` rate display via `fmt.rate`/
  `Number`). Retire `LEDGER_DATA`. Page smoke. Commit `feat(web): ledger wired (spec 19/11)`.

## Task 2.5 — Instruments (instruments.js)
- [ ] Wire to `GET /api/instruments` (last/chg_pct/target_low via `fmt`). Retire `INSTRUMENTS_DATA`.
  Page smoke. Commit `feat(web): instruments wired (spec 19/10)`.

## Task 2.6 — Input center (input.js)
- [ ] Wire to `/api/input/*` preview/commit (manual + CSV import). Form-field `parseFloat`/`Number` on
  USER INPUT stays (input side, sent to backend which computes). Render server-returned amounts via `fmt`.
  Retire `INPUT_DATA`/`input-mock-data.js`. Page smoke (preview round-trip). Commit
  `feat(web): input center wired to /api/input/* (spec 19/12)`.

## Task 2.7 — Settings group (accounts, datasources, llm, scheduler, prompts, alerts, users)
- [ ] Wire each settings page to its endpoint; **fix C2 bare-`.toFixed` sites**: `settings-llm.js:98/99/161/
  162` (remaining/threshold/price_in/price_out) and `settings-scheduler.js:186` (cost_usd) → `fmt`/`Number`.
  **alerts retire (I1):** `settings-alerts.js`/`alerts.js` stop client-computing alerts → read `/api/alerts`
  (+ `/api/alert-rules` for the rules page); calib_gap now comes from the backend (Task 1.4); keep the
  documented alerts cache only. Retire `LLM_DATA`/`DATASOURCES_DATA`/`SCHED_DATA`/`PROMPTS_DATA`/`PD_VARS`/
  `RULES`. Page smoke each. Commit per page or as a settings batch
  `feat(web): settings pages wired + alerts retired to backend (spec 19/13/14/16/15/06/03/09)`.

## Task 2.8 — Insights + AI Pipeline Hub (insights.html, pipeline*.js)
- [ ] Wire to `GET /api/insights`, `/api/ai-score`, and `/api/insight-tasks/{status,preflight,diagnose,
  runs}` (07). **Fold I2:** `'off'`→`'idle'` level vocab; map backend `fix:{kind,id}` → one-click buttons
  (enum→label→existing PUT/POST); recent_skips reason labels incl. `R7_rule_not_matched`/
  `unknown_insight_type`; preflight `fix.kind` buttons; calibration version chain via the calibrations API.
  Retire `PIPE`/`pipeline-data.js` + `vars.js`. Page smoke (status + preflight render). Commit
  `feat(web): insights + pipeline hub wired (spec 19/04/07); off→idle, fix buttons, reason labels`.

---

# PHASE 3 — Cleanup + green

## Task 3.1 — Mock sweep + direct-fetch guard + green
- [ ] Confirm every `window.*_DATA`/mock file is deleted (grep — only the 2 documented exceptions remain);
  add a guard test/grep asserting **no page calls `fetch(` directly** (all via `pdApi`). Run `make all`
  (now the full suite) + all page smokes green. Commit `chore(web): retire all mocks; assert pdApi-only fetch (spec 19 §6)`.

---

## Self-Review — 5-point coverage + dependency order
- **#1 spec-19 ops backend** → Tasks 1.1/1.2/1.3 (backup/last_backup_at/logging) + 0.1 (make). ✓
- **#2 Makefile fix** → Task 0.1. ✓
- **#3 calib_gap wiring** → Task 1.4. ✓
- **#4 dashboard insights embed latest N** → Task 1.5 + consumed in 2.2. ✓
- **#5 money-string frontend rule** → cross-cutting rule (architecture) + every Phase-2 task fixes its named
  `.toFixed`/sum sites via `fmt`/`Number`; counts stay numbers. ✓
- **07 watch-items (I2)** → Task 2.8. **Mock retirement (I4/§6)** → each Phase-2 task + 3.1. **api.js-only
  fetch** → 0.2 + 3.1 guard. ✓
- **Dependency order:** fetch-layer+smoke+make (0) → backend-complete (1) → pages (2, shell→dashboard→reads→
  input→settings→insight/pipeline) → cleanup (3). Full Playwright E2E flows + property/mutation = spec 17/18
  NEXT (not here); spec 19 ships the smoke harness so wiring is guarded. ✓
- Money discipline: frontend never computes money; Decimal strings display-only via fmt; backend unchanged
  (already canonical strings). No new backend money math. ✓
