# Changelog

All notable changes to this project are documented here. Format based on
*Keep a Changelog*; released versions use the heading `## [vMAJOR.MINOR.PATCH] - YYYY-MM-DD`.

**Integrity check** — after any edit to this file, run
`grep -c "^## \[v" CHANGELOG.md`; the count must equal the number of released version
headings. (`## [Unreleased]` is intentionally not counted.)

## [Unreleased]

### Planned
- **Unified auto-import principle:** the manual ledger is the source of truth; data-source data
  (FinMind dividend/ex-div, Schwab transactions) is matched to holdings and offered for a
  **user-confirmed** auto-import into the ledger following the account's accounting rules —
  cutting manual entry, never bypassing confirmation, never double-counting (calc reads only the
  ledger), `original_cost` never overwritten; **manual entry always retained**.
- `data_ingestion/` confirmed auto-import (future): match `pricing/`'s fetched dividend/ex-div
  events (and Schwab transactions) to the holdings list → prompt "new distribution detected —
  auto-import?" → on confirm, write a ledger entry per the account's dividend model (TW cash →
  cost reduction, US DRIP $0-cost, MY cash). `web_ui/` provides the prompt UI.
- `llm_insight/` prediction self-tracking + backtest loop (future sub-project): the LLM
  records each recommendation/forecast, later replays and scores its own past predictions
  against realized outcomes, accumulating a per-prediction confidence index and a
  corrective feedback loop that informs future advice. Gets its own brainstorm at the
  `llm_insight/` stage.
- `llm_insight/` insight inputs & per-stock prompt (future): per-holding decision signals from
  FinMind (財報 / 月營收 / 法人 / 融資券 / PER-PBR / news URL) plus **US sentiment indicators —
  CNN Fear & Greed Index and VIX** — as buy/sell context. **Prompt architecture (decided
  2026-06-08):** one editable **default system prompt** (ships as a Claude-recommended best prompt; user
  fine-tunes in config) holds the output contract + invariants (JSON schema, no
  numbers-of-record, batch-only) and is immutable by overrides; reusable, named
  **Strategy Prompts** (the library ships with several Claude-generated optimized templates;
  users can add their own) add a per-type analytical focus, and each stock's Strategy is **blank by
  default**, optionally **selecting 0..1** from the library (per-stock assignment — option A; data model pre-reserves tag/category binding for a
  later upgrade). All prompts live in the settings (config) page, versioned and folded into the
  cache fingerprint + self-backtest attribution (per `llm-insight.md`).
- **User authentication / access control** (`web_ui/`, future): basic login + permission gating
  so the self-hosted instance (1–2 users) is not publicly exposed on the network — kept minimal.
- `web_ui/` dashboard UI/UX (future): as strategy info, data, and ECharts charts grow, the page
  can get long — evaluate clear categorization + non-cluttered tabs/sections (avoid endless
  scroll) for this AI-stock-strategy / position-management / watchlist assistant. Optimize the
  human-computer interface then, not pre-emptively.
- **AI cost-info + LLM settings page** (`web_ui/`, future): the **backend is now built** (model
  registry, four role-defaults, USD budget governance, `llm_usage` log + cost calc, vision plumbing —
  see Added). Remaining is the `web_ui/` page: usage stats + history-trend + per-model cost charts;
  model add/edit (provider / endpoint / key / vision / pricing); role-default pickers; budget
  set/reset; and the screenshot-upload widget for vision (statement → draft → confirm).
- **Design principle (all modules):** invest in adjustable structure — config-driven behavior,
  provider/strategy protocols + registries, swappable adapters, decoupled layers — so future
  changes are config edits + small additions, not rewrites; keep YAGNI on features/scale (per
  `stack.md`), deferring concrete specifics until real use surfaces them.

## [v0.1.0] - 2026-06-19

### Added
- **Frontend wiring foundation — spec 19 Phase 0 (2026-06-16):** the static `web/` frontend's single
  fetch layer + a Playwright smoke harness, landed ahead of per-page wiring.
  - **`web/api.js` (`window.pdApi`, spec 19.1):** the ONE fetch seam — `{get, post, put, del, download,
    abortable}` + `window.PdApiError`. Parses the `api/errors.py` envelope `{error:{code,message,field,
    issues}}` into a structured `PdApiError`; **401** → `window.location.replace('login.html')` (the single
    redirect site) then throws; **402/409/503** → rethrow with NO redirect (the AI block renders a degraded
    state); response Decimal **strings pass through untouched** (no `parseFloat`/`Number`/`+` — the frontend
    never computes money); `credentials:'same-origin'` (carries `pd_session`); `abortable(key)` cancels a
    prior same-key in-flight request. No page calls `fetch` directly.
  - **Playwright smoke harness (`tests/e2e/conftest.py`, reuses the spec-17 golden seed):** a subprocess
    uvicorn serves the real `create_app()` (StaticFiles `web/` + `/api/*`) against an on-disk golden DB
    (DRY-reuse of `tests/conftest.py::_seed_golden`); headless chromium drives it; reusable
    `assert_page_ok(page, base_url, path, root_selector="body")` asserts **zero console errors + zero uncaught
    pageerrors** (catches Decimal-string `.toFixed` TypeErrors once pages bind to `/api`). The global
    `--disable-socket` ban is re-enabled **for loopback only, scoped to `tests/e2e`** (autouse
    `_e2e_loopback_socket`, restored on teardown) — external network stays banned. Baseline smokes for
    `login.html` + `index.html`; per-page smokes are added by Phase 2. (`playwright>=1.44` was already a
    declared dep; raw `playwright.sync_api` used — no `pytest-playwright` added.)
- **Backend completeness — spec 19 Phase 1 (2026-06-16):** ops/observability + dashboard-completeness so
  Phase-2 pages wire against a complete backend.
  - **Ops 保全 (spec 19.3):** new leaf `portfolio_dash/ops/backup.py` — `backup_database` (sqlite3 online
    `.backup` API → gzip → `data/backups/portfolio_{YYYY-MM-DD}.db.gz`, keep-30 rotation), `check_integrity`
    (`PRAGMA integrity_check`), `pre_write_snapshot` (prefixed one-off snapshots for CSV/AI commit + migrations).
    `scheduler/jobs.py` `backup_daily` job (default 01:30 Asia/Taipei): integrity-fail → error run + structured
    warn; logs recovery after a 3-consecutive-fail streak. Pairs with the Phase-0 `make restore` target.
  - **`/api/dashboard` freshness `last_backup_at` (spec 19.3):** `ops.backup.latest_backup_at()` (newest backup
    mtime as a UTC ISO string, or None); `FreshnessReport.last_backup_at`; router-fed after `to_wire`
    (build_dashboard stays pure).
  - **Structured JSON-lines logging (spec 19.4):** new leaf `shared/logging_config.py` (`JsonLinesFormatter`
    + idempotent `configure_logging`, RotatingFileHandler 10 MB×5 → `data/logs/app.log`), configured in the app
    lifespan; a catch-all `Exception` handler in `api/errors.py` logs the traceback + returns the generic 500
    envelope (no detail leak); one `llm_usage` structured log point in `shared/llm.py` (alias/tokens/cost,
    reconciled with the `llm_usage` row). stdlib only.
  - **`calib_gap` alert rule (spec 03/04 I1):** `AlertRules.calib_gap` (default **15 pp**, not a ratio); the
    pure `compute_alerts_from`/`compute_alerts` gain a fed `calib_gap: Decimal | None`; `evaluations_store.
    scored_confidence_hits` + the SINGLE-SOURCE `api/insight_service.calibration_gap(conn)` (global `min_samples`
    gate → `scoring.calibration_error`, in pp) feed BOTH the dashboard embed and `GET /api/alerts` (they cannot
    diverge). `calibration_regression` stays an `alert_events` event, not surfaced here. (`evolution_config.
    gap_alert_pp` is the separate spec-04c regression threshold — NOT this rule's threshold.)
  - **Dashboard embeds latest N real insight cards (spec 08/04 I3):** `insights_store.latest_cards(conn, n)`
    (`is_shadow=0`, newest-first, LIMIT n); the router overwrites `payload["insights"]` after `to_wire` with the
    latest 3 as `{id, title, summary, body_md, symbol, created_at, cost_usd}` (cost_usd stays the canonical
    Decimal string; empty table → `[]`). NOTE the field names differ from the older `web/mock-data.js` insight
    shape — reconciled when Phase 2 wires the dashboard page.
- **spec-17 full-stack regression — financial golden verification + E2E user flows (2026-06-17):**
  the final acceptance pass over the wired full stack.
  - **Multi-stock financial verification (`tests/contract/test_spec17_financials.py`, spec-17 §17.2):**
    a rich 8-instrument / 4-account / 3-currency scenario (`seed_full`) seeded through the REAL write paths
    and driven through `GET /api/dashboard`, asserted against **independent first-principles oracles** derived
    from `rules/domain-ledger.md` (NOT by re-calling the calc core). Covers weighted-average cost (2330), TW
    cash-dividend cost-reduction (2330), partial-sell realized P&L (0056), 配股 stock dividend (2603),
    missing-price degradation + XIRR all-or-nothing (00919), US DRIP $0-cost reinvest with 30% withholding
    (AAPL), age-stale-but-valued price (MSFT), MY cash dividend + 3-dp price fidelity (1155.KL), the
    cross-currency reporting blend at spot, and **invariant #6 — FX gain/loss is an attribution of the
    reporting total, never added on top** (`total_return == realized + unrealized`; realized FX 2,000 TWD
    hand-verified). A frozen `tests/golden/dashboard_full.json` snapshot (regenerated deliberately via
    `scripts/regen_golden_full.py`) pins the whole payload for regression (spec-17 §17.6.1). New reusable
    `tests/conftest.py::dashboard_client_factory` (+ extracted `init_golden_base`) builds a TestClient over a
    fresh, custom-seeded golden-base DB; the fixed subset `golden_db` (and the 1067 tests on it) are untouched.
  - **E2E user flows (`tests/e2e/test_flows_e1_e10.py`, spec-17 §17.5):** Playwright against per-flow ISOLATED
    uvicorn subprocesses (new `tests/e2e/conftest.py::flow_server` factory + `fresh_page` isolated context) so
    write/auth flows are order-independent. E1 dashboard (golden KPIs + 00919 缺價 badge + asof/stale chip), E2
    manual buy commit (form → preview → confirm 201 → position grows 1000→2000 in the API), E4 oversell soft
    warning (ack gates the confirm button, then writable), E6 login loop (protected mode: wrong pass 401 stays
    on /login.html → correct → dashboard). Expect-polling only, no sleeps (§17.7.4). Harness robustness
    added during a senior full-stack review (the suite is green — exit 0 — every run; these prevent rare
    real infra races, NOT a failing assertion): 60s readiness + Playwright ceilings (not 30s) absorb
    Windows subprocess cold-start contention (one genuine TimeoutError seen under review load);
    `flow_server` retries the spawn with a fresh port on early-exit (the `_free_port` bind→release→spawn
    TOCTOU race, amplified by spawning a server per flow); best-effort `fresh_page` / `_terminate`
    teardown so a passed test never errors on Playwright/subprocess cleanup. NOTE: the benign captured
    log `asyncio: Task was destroyed but it is pending!` (Playwright `Page._on_route` GC at close) is
    NOT a failure and only shows under `-rA`/`-rE`, never under the `-q` gate (see LESSONS_LEARNED).

### Fixed
- **Deterministic `/api/dashboard` freshness ordering (spec-17 regression, 2026-06-17):** `freshness.fx` and
  `freshness.missing_fx` iterated `RateResolver.reads`, whose order derives from set iteration over quote
  currencies (PYTHONHASHSEED-dependent across processes) — so the API list order was non-deterministic and a
  golden snapshot flapped between runs. `portfolio/dashboard.py` now sorts both by `(base, quote)`. (`prices`
  was already stable via `sorted(held_symbols)`.)
- **Oversold (賣超) ledger no longer 500s the dashboard (2026-06-18, human sign-off — lightweight, NOT short
  accounting):** an acked oversell (`POST /api/input/manual/commit` `side=sell` qty>held + `ack_oversell=true`)
  writes a sell exceeding holdings; the NEXT `GET /api/dashboard` then crashed (`build_book` raised
  `OversellError`, uncaught → 500). Surfaced by the spec-17 regression. Fix: `build_book(allow_oversell=True)`
  (the dashboard path) DEGRADES GRACEFULLY — nets the position to negative shares, drops its now-undefined cost
  basis, emits no realized row; the holding is flagged `oversold` with 待釐清 (null) value/P&L and is **excluded
  from portfolio aggregates** (auto via the existing `market_value is not None` gates). XIRR degrades to None
  with a reason when any position is oversold. The 重算/rebuild action (`actions.py`) and all input-time
  oversell warnings (preview/whatif detect it independently) are unchanged — `build_book` still raises by
  default. `Holding`/`HoldingRow` gain an `oversold` flag; the holdings table renders a **賣超** badge +
  tooltip prompting the user to record the missing opening inventory / buy. New
  `tests/contract/test_oversell_graceful.py` + an e2e display flow; full short-position accounting is
  deliberately out of scope (it would invert cost basis, dividend direction, weights/allocation/XIRR — over
  scope for a 1–2-user long-only tracker; revisit only if real short trades are needed).
- **`/api/health` exempt from the protected-mode auth gate (2026-06-17, human-approved):** the liveness probe is
  added to `auth_store._OPEN_PATHS` (alongside `/api/auth/login` + `/api/auth/session`). It returns only
  `{"status":"ok"}` (no data), so it must answer regardless of login — previously, once ≥1 user existed (protected
  mode) an unauthenticated Docker/k8s/monitoring liveness probe got a 401. Every OTHER `/api/*` path still requires a
  session in protected mode (regression test pins protected `/api/health`→200 AND `/api/dashboard`→401).
- **Makefile runs the full suite (spec 19 Phase 0, 2026-06-16):** `make test`/`make regress`/`make all` now
  run `pytest tests --ignore=tests/e2e` (the whole tree minus browser e2e) — previously `make test` targeted
  only `tests/unit tests/contract`, collecting **266 of 1012** tests, so `make all` was not real regression.
  `make e2e` is the explicit Playwright gate; the `e2e` pytest marker is registered in `pyproject.toml`.
  Added a guarded `make restore FILE=... [DB=...]` ops target (copies a backup over the live SQLite DB at
  `data/portfolio.db`).
- **Atomic batch import (#1 backend hardening, 2026-06-15):** CSV/broker batch import is now
  all-or-nothing on an unexpected error. `data_ingestion/preview.commit_preview` previously looped
  accepted rows calling writers that each `conn.commit()` per row, so a mid-batch unexpected exception
  left a partial ledger (rows 1..k committed, the rest not) — breaking 重算/append-only reproducibility.
  Now the writer loop runs in ONE transaction (a `commit: bool` param threaded through the four batch
  store inserts; batch passes `commit=False`), commits once at the end, and `rollback()`s + re-raises on
  any exception. The single-row/manual path is unchanged (default `commit=True`); intentional skips of
  hard-issue rows stay contract-level partial success (not a rollback trigger). New
  `tests/data_ingestion/test_preview_atomicity.py`.
- **pricing→data_ingestion cross-peer import removed (#2 layering, 2026-06-15):**
  `pricing/datasources_store.py` no longer imports `data_ingestion.config_seed.DEFAULT_ACCOUNTS`
  (architecture.md: pricing and data_ingestion are sibling lower layers). It now iterates the file's own
  local `_ACCOUNT_MARKET` map (already enumerating the 4 accounts) — byte-equivalent fallback-chain
  seeding. New `tests/pricing/test_layering.py` AST-guards that `pricing/**` imports no `data_ingestion`.

### Changed
- **Renamed `web/AI Pipeline Hub.html` → `web/pipeline-hub.html` (2026-06-19):** the only frontend page
  whose filename had spaces + Title Case, out of step with the lowercase-hyphenated convention
  (`index.html`, `settings-scheduler.html`, …). `git mv` + updated all LIVE references —
  `web/shell.js` (sidebar nav), `web/alerts.js` (`/pipeline` href map), `web/settings-prompts.html`
  (cross-link), and the e2e smoke (`/pipeline-hub.html`, dropping the `%20` URL-encoding). The frozen
  `docs/design-handoff/` export bundle (its own `AI Pipeline Hub.html` + shell.js + spec-07 reference)
  is left untouched — it is a self-consistent historical snapshot, not the served app.
- **spec 19 deferred follow-ups resolved (2026-06-16):** ① the 自我進化設定 panel is wired to `GET/PUT
  /api/evolution-config` (read-then-PUT preserves the non-panel knobs `horizon_basis`/`defer_limit_days`/
  `shadow_on_alert`; `gap_alert_pp` sent as a Decimal string; the `localStorage pd_evolution_cfg` path removed);
  ② removed the dead `window.PD_HISTORY` trend trade-marker code in `charts.js` (the E8 large-trade markers had no
  backend source for the portfolio-level trend after the mock deletion); ③ `rebalance.js` now derives trades/fees via
  the authoritative `POST /api/rebalance/preview` (debounced + `pdApi.abortable`) instead of a client-side estimate —
  the module computes NO money (`FX_TWD`/`pdFeeTax`-call/lot-snapping/turnover removed); ④ `api.js` `download()`'s
  401-redirect now carries the same `!endsWith('login.html')` guard as `_handle`; ⑤ `prompts.py` registry docstring
  26→29; ⑥ added `web/favicon.svg` (+ a `shell.js`-injected `<link>` and a login.html `<link>`) to retire the app-wide
  `/favicon.ico` 404. Each fix shipped with a per-change senior review + page smoke + an E2E Playwright flow
  (evolution-config round-trip, trend-chart mount, rebalance-preview round-trip, favicon presence). Suite now
  **1067 passed / 3 skipped + 33 e2e**.
- **Frontend wired to the live API — spec 19 Phase 2 (page wiring) + Phase 3 (cleanup) (2026-06-16):** every
  static `web/` page now consumes the real `/api/*` through the single `window.pdApi` fetch layer; ALL mock-data
  globals are retired and the mock FILES deleted. No framework, no build step (decision B). Per page (each: mock →
  `pdApi`, money via `fmt.*` [Decimal strings, never client-computed], async boot, Playwright page-smoke):
  - **shell.js** — async `GET /api/auth/session` guard (guest / signed-in / signed-out→`login.html`), replacing the
    localStorage guard; sync globals (`toast`/`confirmDialog`/`pdOpenSymbol`/search/nav) preserved; logout/lock via pdApi.
  - **dashboard** (index/app.js + charts.js + alerts.js) — one shared `window.pdDashboard = pdApi.get('/api/dashboard')`
    promise consumed by all three; sparkline from `spark_30d`; insight cards from the real `{summary,body_md,created_at,
    cost_usd}` shape; alert `href` mapped to static routes; the embedded `alerts`/`llm_quota` rendered (no client recompute).
  - **symbol detail drawer** — `GET /api/symbol/{symbol}/detail` + the shared dashboard promise; feeTax offline mirror
    kept (documented exception); 合計 consumes backend `unrealized_pnl` (no client money-sum).
  - **ledger** — `GET /api/ledgers/*` (implied_rate from the backend; account filter keys on `account_id`).
  - **instruments** — `GET /api/instruments` + probe/register/edit (`POST /probe`, `POST/PUT /instruments`).
  - **input center** — `GET /api/input/context` + manual/CSV/AI preview+commit (oversell + warnings ack-confirm flows);
    manual dividend/FX/opening forms are design-stage (no single-entry endpoint — CSV import is the path).
  - **settings** — LLM (`/api/llm/config`), scheduler (`/api/scheduler/jobs`+`/runs`), datasources (`/api/datasources`),
    prompts + vars (`/api/system-prompt`, `/api/prompt-vars`, `/api/prompts/{preview,test}`), users (`/api/users`),
    alert-rules editor (`GET/PUT /api/alert-rules`). Fixed the C2 bare-`.toFixed` money sites + war-game Finding 8
    (`cost_usd == null` nil-check). Retired the shell `setSession` transitional shim.
  - **alerts.js (I1)** — off-dashboard pages now read `GET /api/alerts` (bell) + `GET /api/llm/config` (quota chip);
    the client-side rule-compute orphan removed.
  - **login.html** — `POST /api/auth/login` (cookie session); api.js's 401-redirect is suppressed ON `login.html` so a
    wrong-password 401 surfaces in the form instead of self-reloading.
  - **insights + AI Pipeline Hub** — `/api/insights`, `/api/ai-score`, `/api/insight-tasks/{status,preflight,diagnose,
    runs}`, `/api/calibrations`; folded the 07 watch-items (`'off'`→`'idle'`, `fix.kind`→one-click buttons,
    `recent_skips` reason labels, calibration version chain).
  - **Phase 3 cleanup** — wired rebalance.js to the shared `/api/dashboard`; DELETED the 4 mock files
    (`mock-data.js`/`history-mock.js`/`input-mock-data.js`/`pipeline-data.js`); added `tests/contract/test_web_pdapi_only.py`
    asserting **no `web/*.js` except `api.js` calls `fetch(` directly** (single-fetch-layer guardrail, spec 19 §6).
  - **Backend fix exposed by the real-server e2e:** `shared/db.py` now opens the SQLite connection with
    `check_same_thread=False` — FastAPI runs the sync `get_conn` dependency in an anyio threadpool, so a per-request
    connection can be created on one worker thread and closed on another (no concurrent use); the default same-thread
    guard wrongly raised on close → a 500 under the real subprocess server (the in-process TestClient never hit it).
    Guarded by a cross-thread regression test.
  - **Test harness:** the Playwright smoke harness (spec 17 seed) now guards every wired page + key interactions
    (drawer, account filter, input preview, instrument probe, rebalance drawer, alert bell, login) — 29 e2e smokes,
    zero console/page errors per page. Suite: 1009 → **1067 passed / 3 skipped**.
  - **Deferred (tracked follow-ups, none ship-blocking for 1–2 users):** wire the 進化設定 panel to `GET/PUT
    /api/evolution-config` (backend already implemented; panel still uses localStorage); the dashboard trend's
    trade-event markers no longer render (`charts.js` `window.PD_HISTORY` is now dead code after the mock deletion —
    remove or source from `/api/dashboard` trend); rebalance.js authoritative result could use `POST /api/rebalance/preview`
    (currently a documented client what-if estimate); `prompts.py` docstring says "26 variables" (registry is 29).
- **Money/Decimal wire-string unification (#2c/M1 foundation hardening, 2026-06-15):** every Decimal
  now serializes to the JSON wire in ONE canonical form — `format(d, "f")` (fixed-point, full source
  precision, trailing zeros preserved, **never scientific notation**) — identical to the DB form
  (`money.to_db`). New `shared/wire.decimal_str`; `to_wire`'s Decimal branch routes through it (was
  `str(Decimal)`, which could emit `1E-7`-style sci-notation); `money.to_db` delegates to it
  (byte-identical, float/non-finite guards kept). All direct `str(<Decimal>)` wire bypasses migrated to
  the canonical encoder across `api/wire.py` + routers (dashboard `spark_30d`/`llm_quota`, input_center
  [**`_money_str`/`normalize()` removed**], symbol, ledgers, llm_settings, instruments, strategy,
  prompts, insights) and `api/insight_service.py`; `str()` on ints/ids/enums left untouched. Done
  **before frontend wiring** so the UI binds to a stable money format and formats for display itself
  (full precision stays on the wire; quantize only at display, per `data-and-pricing.md`). One spec-17
  golden value changed (a trailing zero now preserved: `612500.0`, not `612500`); spec-18 round-trip +
  a no-scientific-notation guard added. (+21 tests; 980 → 1001 passed.)
- **LLM budget model — single topup-cumulative (2026-06-13, human sign-off; senior-review
  finding I-1):** the USD budget is now one number — `budget_remaining = Σ top-ups − Σ usage`
  (`shared/llm_config`). `remaining <= 0` blocks (`check_budget` raises `LLMBudgetExceeded`),
  so an unfunded/$0 account is blocked even when fully configured; exhaustion coincides exactly
  with `Σ top-ups == Σ usage`. Top-ups ADD cumulatively (no reset). The gate, settings page
  (`GET /api/llm/config` `quota.remaining_usd`), dashboard chip (`GET /api/dashboard`
  `llm_quota.remaining_usd`), and the spec-16 `quota_remaining` alias all read this single value
  (`reset_budget` removed; `quota_remaining` delegates to `budget_remaining`). **Supersedes the
  earlier append-only "reset ledger" model** (remaining = latest reset − Σ usage since that reset;
  unset = no cap). End-to-end reconciliation proof: `tests/contract/test_quota_accounting.py`.
- **Web-layer architecture decision — option (B) (2026-06-13, human sign-off):** the web
  layer is now a **FastAPI JSON API (`portfolio_dash/api/*`) + a static vanilla-JS frontend
  (`web/`)**, superseding the originally-locked **Jinja2 + HTMX server-rendering** (CLAUDE.md
  locked decision #1 "no frontend/backend split / no JSON contract"; `stack.md`;
  `design-handoff.md` "convert to Jinja2 templates"). Rationale: the Claude-Design export is
  vanilla JS + ECharts CDN with **no framework and no build step** (the stack-drift guardrail
  is honored) and pushes **all computation to the backend** (the web layer still does not
  compute — invariant #4 intent preserved). The trade-off ("single codebase / no contract to
  drift") is mitigated by `mock-data.js` as the version-controlled contract and spec-17 golden
  payload + spec-18.4 string-serialization round-trip tests. Net upside: the JSON contract makes
  the automated regression loop machine-diffable (stronger than HTML-fragment assertions).
  `CLAUDE.md`/`stack.md` web rows to be amended; the HANDOFF.md CLAUDE.md template is
  **reconciled, not applied verbatim** (locked accounting/ledger/process rules preserved).
  Full reconciliation: `docs/design/spec-reconciliation-2026-06-13.md`.
- **Scope expansions adopted from design-handoff specs 01–19 (2026-06-13, human sign-off):**
  a new `api/` HTTP layer (08/19); `strategy/` alerts rule-engine + what-if + rebalance as
  pure functions (03, with config-row editable thresholds — a narrow, bounded step toward
  user-editable rules, explicitly NOT a DSL); a full `llm_insight/` self-evolution system —
  insight composers, calibration version chains, backtest scoring, a new `master` LLM role
  (04, far beyond the prior "batch insight cards"; invariant #1 preserved — quant hits are
  code, the LLM only writes narrative/calibration text); external-data ingest + an append-only
  `external_snapshots` store (06: FinMind chips/fundamentals/valuation, VIX, Fear&Greed,
  indices); auth/users via stdlib `hashlib.scrypt` (09, no new dependency); a full test/
  regression harness — `make all`, golden dataset, FastAPI TestClient contract tests,
  Playwright E2E, hypothesis/mutmut, pytest-socket network ban (17/18); SQLite backup/restore
  + structured logging (19). Schema migrations (additive, via `_add_column_if_missing` /
  `config_store`): `instruments += target_low/board_status/is_etf`, `transactions +=
  fee_snapshot`, `schedule_config += kind/payload`, `job_runs += payload/reason/cost_usd`, plus
  new tables for auth/datasources/external-snapshots/insight. Enum extensions: `DividendType +=
  NET`, `LLMRole += MASTER/MASTER_FALLBACK`. `FeeRuleSet` structural fixes (flat_fee, US/MY
  min_fee, stamp_duty_rate+cap) and US/MY fee-rate backfill (spec 18.0, pending real-statement
  confirmation). Build order in the reconciliation doc §6.
- **Accounting model decision (2026-06-06, human sign-off):** P&L now uses the
  adjusted-cost model — cash dividends fold into cost (no separate dividend-income line),
  realized/unrealized computed vs `adjusted_cost`; `original_cost` retained for the
  return-rate denominator and the capital-gain-vs-dividend split. Supersedes the prior
  original-cost-plus-separate-dividend rule in `domain-ledger.md`. The no-double-count
  principle is preserved (dividends still counted exactly once). Return-rate denominator
  stays original invested cost; cost basis is all-in (incl. buy fees+tax).

### Added
- **Insight pipeline-hub UX — status / preflight / diagnose (spec 07, Phase 4 — the observability
  layer):** read-only convergence over the spec-04 machinery — NO new tables, NO LLM calls, NO new
  business logic (03/04/06 reused). `GET /api/insight-tasks/status` returns a single source of truth:
  health (master_ok, quota_remaining, last_batch) + per-task 5-node states (trigger/input/assemble/exec/
  output, §7.1.1 derivation) aggregated to a level — the pure `llm_insight/pipeline_status.py`
  `derive_node_states` over facts gathered in `api/insight_service.py` (schedule_config, resolved
  universe, **reused dashboard freshness**, templates, budget/quota_low/master, last non-shadow run).
  `POST /api/insight-tasks/{id}/preflight` (also a draft `body` for the wizard's check-before-create) is
  a zero-cost dry run that calls the **SAME `gating.evaluate_gates` as execution** (the §7.2 hard rule —
  no "preflight passed, run failed"; asserted via a spy + an end-to-end demo) wrapped with G0/G1/G7,
  returns ordered gates + verdict (blocked/degraded/clean) + the spec-06 assembled preview + `fix.kind`
  one-click hints — never calls the LLM, never writes job_runs/llm_usage. `GET …/diagnose` adds
  first_blocker + recent_skips (single-enum reasons); `GET …/runs` is the task-view job_runs (is_shadow
  excluded). §7.0 naming: `/api/insight-tasks/*` is a full **alias of the same resource** as
  `/api/insight-types/*` (one `_dual` route registration, no logic duplication; old routes + table names
  kept). Senior review: APPROVE-WITH-NITS → fixed the R6 (quota) gate emitting a wrong `create_schedule`
  fix (quota has no one-click action in the enum). The 3 §7.6 failure demos reproduced. **This completes
  the 04→07 insight chain backend.** (+48 tests; 932 → 980 passed.)
- **AI self-evolution / Loop Engineering (spec 04, Phase 4 — the four-self loop):** the
  insight-composer + generation + backtest + calibration + shadow-promote system, built in three
  sub-phases (04a design/CRUD, 04b generation, 04c evolution) under the §4.10 locked decisions
  (mechanism reviewed + human-signed-off 2026-06-14). **04a** — composer tables
  (`strategy_prompts`/`insight_types`/`insight_type_strategies`/`calibration_prompts`) +
  `evolution_config`, CRUD/cascade (4.1)/schedule-binding (4.2 kind=insight)/active-calibration/
  evolution-config API, R1 create-time gate reusing `validate_tokens`. **04b (Loop 1 自運作)** —
  `InsightCard`+`Prediction` schema (confidence required with a prediction), `insights` table with
  fingerprint cache + trading-day `due_at`, layer assembly (system+strategies+active calibration via
  06a `render_prompt`), the single **R1–R8 runtime gate** (shared with spec-07 preflight),
  `run_insight_type` generation (default role, R4 zero-LLM anomaly card, R6 partial, cache hits),
  scheduler `kind=insight` dynamic dispatch via an injected `register_insight_runner` (no scheduler→api
  cycle), date variables (`now`/`card_created_at`/`eval_date`, ISO-8601 +08:00),
  `complete_structured` `response_format` enforcement w/ graceful fallback, and the `alert-scan` job +
  `alert_events` + on_alert (R7) trigger (24h debounce, ≤3-day horizon). **04c (Loops 2–4)** — the
  **master LLM role** completion path, `insight_evaluations` store + `/api/ai-score` aggregation,
  pure `score_quant`/calibration-binning/`decide_promotion`, the daily `evaluate_insights` job
  (objective quant_hit + master narrative_score, **pending_data anti-poison** → `undetermined` after
  `defer_limit_days`), the weekly `generate_calibrations` job (master writes a validated new version,
  `min_samples`-gated, append-only), shadow evaluation + auto-promote + `calibration_regression`
  alert, and the §4.8 calibration validator (keyword denylist + one master review). **Layering held:**
  `llm_insight/*` import no `pricing`/`data_ingestion`/`api` (the only price-reading seam is
  `api/insight_service.py`; the wire encoder moved to `shared/wire.py` to kill a pre-existing
  `llm_insight→api` import). LLM emits no numbers of record (quant_hit is code; master writes only
  narrative/calibration text); single budget governs all roles. Cross-module senior review:
  APPROVE-WITH-NITS → fixed insights.model provenance, the reverse import, shadow `job_runs`
  distinction (`is_shadow` column, excluded from user-facing runs), and single-enum skip reasons.
  Deferred v1 watch-items: `relative`/`volatility`/`portfolio_return` quant metrics (narrative-only for
  now, anti-poison-safe). New tables: `insights`, `insight_evaluations`, `alert_events`,
  `alert_dispatch_log` + the four composer tables; `job_runs += is_shadow`; `insight_types +=
  horizon_days/eval_prompt`. (+265 tests; 667 → 932 passed.)
- **Data-source catalog, provider expansion & external-snapshot ingest (spec 20, Phase 4 —
  absorbs the planned 06b):** the data layer that makes the chips/sentiment prompt variables
  live. **Two seams** (control plane = spec 14 settings/keys/health/fallback; data plane =
  spec 20): the existing `pricing/` registry + providers stays the single interface — adding a
  source = one adapter + one catalog row + one probe adapter. New `pricing/snapshots_store.py`
  (append-only `external_snapshots`: source/dataset/symbol/as_of/payload/fetched_at, latest
  `fetched_at` wins; created EMPTY in `golden_db` so every external var degrades and prior
  suites stay green); `pricing/finmind_datasets.py` (FinMind Free-tier client for
  institutional/margin/PER/monthly-revenue/financials, **always per-`data_id` → Free tier**);
  `pricing/sentiment_source.py` (VIX via yfinance `^VIX` + CNN Fear&Greed free JSON) +
  `index_source.py` (yfinance `^TWII`/`^GSPC`/`^KLSE`); 4 free quote fallbacks
  (`twstock`/`stockprices_dev`/`klsescreener`/`malaysiastock`) wired into
  `DEFAULT_PROVIDER_ORDER`; `portfolio/external_signals.py` (pure Decimal derivations —
  consecutive-buy-days, net-buy-sum, chg/yoy/mom with None on denom≤0, percentile, vix_zone —
  numbers of record stay out of `llm_insight`); `pricing/ingest.py` + 5 scheduler ingest jobs
  (TW universe via direct SQL — `scheduler` imports no `data_ingestion`; 3-consecutive-fail
  warn → `data_source_health`). Catalog (`datasources_store.SOURCE_INFO`) expanded to the full
  ~15-source matrix with `provides`/`status` (`live`/`pending`/`blocked`); token-gated adapters
  (alphavantage/finnhub/fred) catalogued `pending` + key-gated `supports` (inert until a key is
  entered — not in the fallback order); the 7 chips/sentiment variables flipped `available=true`,
  served from snapshots via `VarContext` (router-fed; `llm_insight` imports neither `pricing`
  nor `data_ingestion`), degrading to `{"unavailable": true}` when a snapshot is missing.
- **FinMind auth & tier-awareness (spec 20.15, per the official AI-agent manual):** both
  FinMind callers switched to `Authorization: Bearer {token}` (token still DB-resolved), added
  optional `end_date`. Per-source token tier marking — `data_sources.tier` (additive idempotent
  migration), `SourceInfo.tiers`, `TIER_ORDER`, `PUT /api/datasources/{id}/tier` (400 unknown
  tier / `auth:"none"`; 404 unknown id), `tier`/`tiers` on the GET wire. `DATASET_TIER` (all 5
  = `free`) + a **local tier preflight** that raises `FinMindTierError` BEFORE any network call
  when the marked token tier is too low; HTTP 402 / JSON `status==402` → `FinMindQuotaError`
  carrying FinMind's message; `fetch_quota` reads `user_info` (`user_count`/`api_request_limit`).
  `GET /api/prompt-vars` now carries `required_tier`/`tier_ok`/`tier_label` so the frontend greys
  out variables/panels needing a higher plan; ingest catching tier/quota errors writes no
  snapshot and records `data_source_health` (status=error, reason) → the variable degrade payload
  carries the `reason` (router-fed). Non-regression: under a free/unset token the 5 chips vars
  stay `tier_ok=true`. Probe harness extended (Bearer, `fetch_quota`, tier-from-limit) +
  bounded `docs/probes/` refresh; full source matrix authored in
  `docs/design-handoff/.../specs/20-data-source-catalog.md`.
- **Data-variable & prompt-rendering foundation (spec 06a, Phase 4 — the AI brain's base):**
  the prompt "Lego-block" layer that specs 04/07 build on. New module `portfolio_dash/llm_insight/`
  (`variables.py` = a **26-variable / 8-category registry** mirroring `web/vars.js` + `render_prompt`
  + `validate_tokens` — the SINGLE reusable validation core that spec 04 §4.9 R1 runtime gating and
  spec 07 §7.2 preflight will also call; `system_prompt.py` = one editable global system prompt via
  `config_store`, default seeded). New `portfolio_dash/portfolio/technicals.py` (pure Decimal: MA
  20/60/120 + deviation, sample-stdev annualized volatility via `Decimal.sqrt`, max drawdown,
  price-vs-cost) — the **LLM emits no numbers of record**, so every numeric variable value is
  computed by the calc core and only ASSEMBLED into JSON here. Endpoints (`api/routers/prompts.py`):
  `GET /api/prompt-vars`, `GET/PUT /api/system-prompt`, `POST /api/prompts/preview` (diagnostic —
  ALWAYS 200, lists `unknown_tokens`/`scope_violations`, REAL computed values, **never calls the
  LLM**, `est_tokens` heuristic), `POST /api/prompts/test` (execution path — **422** on unknown
  token or a `per_symbol` var in a `portfolio`-scope body = R1; else real LiteLLM via a new
  `shared/llm.complete_text`, records `llm_usage` agent=`prompt_test`, budget exhausted → 402,
  returns `quota_remaining`). Money/price/rate are Decimal **strings**.
  - **Availability:** position+price+dividend+fx+system (17 vars) are live now; chips+sentiment
    (7) are `available=false` until spec 06b external ingest; backtest/calibration (2) until spec 04
    (`web/vars.js` mislabels the `ai` category `ready` — corrected to `false`). Unavailable vars
    render `{"unavailable": true}`. (Reconciliation: the spec prose says "24" variables; its own
    table and `web/vars.js` enumerate **26** — the authoritative count.)
  - **Senior-review (Opus, APPROVE-WITH-NITS) fixes folded in before merge:** `fx_rates_json` now
    emits the real spot rate (was as_of/stale only — `freshness.fx` carries no rate; the router
    resolves it via `get_fx`); `dividends_json` is the per-event ledger list with currency (was a
    yearly summary, contradicting its contract); `price_vs_cost` returns each ratio independently so
    a non-positive `adjusted_avg` (high-yield payback, allowed by `domain-ledger.md`) no longer
    drops the valid original ratio; `to_wire` now transforms Mapping keys (defensive); +coverage
    (all available tokens render valid JSON, fx rate present, per-event dividends). Conn-bearing
    reads (FX rates, dividend rows) are resolved in the api router and fed into `VarContext` —
    `llm_insight` imports only `portfolio`/`shared`/`api.serialize` (one-way deps intact).
  - **Deferred to spec 06b** (intentional split): `external_snapshots` table + 5 ingest jobs
    (FinMind chips/fundamentals/valuation, VIX/Fear&Greed, indices) + derivations + flipping
    chips/sentiment vars to available. **Global system-prompt CRUD lands here** (neither spec 06
    nor 04 assigned the endpoint; it is foundational to rendering).
- **Scheduler management API (spec 15, Phase 3):** `portfolio_dash/api/routers/scheduler.py` over the
  existing in-process scheduler. `GET /api/scheduler/jobs` (config + latest run + next fire),
  `PUT /api/scheduler/jobs/{id}` (cron/tz/enabled subset-merge with live reschedule), `POST
  /api/scheduler/jobs/{id}/run` (async **202** + a daemon thread that opens its own `session()`;
  `409 already_running` when the latest run is unfinished), `GET /api/scheduler/runs` (history;
  `limit>500 → 400`). Cron/tz validated via `CronTrigger.from_crontab` — invalid → **400
  `invalid_cron`** with the `field` pointing at the real offender (tz checked separately from cron),
  and **no DB write**. Every route degrades gracefully when `app.state.scheduler` is `None`
  (`PD_DISABLE_SCHEDULER=1`, e.g. tests): `next` = null, reschedule is a no-op. `cost_usd`/`reason`
  are Decimal-string/null, never stringified. New `scheduler/runtime.py::reschedule_job` (None-safe)
  + `scheduler/jobs.py` helpers (`start_job_run`/`finish_job_run`/`latest_run_unfinished`/
  `run_job_func`). **§15.0 schema columns (SR 2026-06-13; specs 04/07 depend on these):**
  `schedule_config += kind ('system'|'insight'), payload`; `job_runs += payload, reason, cost_usd`,
  added idempotently in `create_scheduler_tables` via a **local** `_add_column_if_missing` (no
  `scheduler → data_ingestion` dependency). v1 lists `kind='system'` jobs only (no insight jobs yet).
- **Sessions & authorized users (spec 09, Phase 3):** stdlib-only auth (`hashlib.scrypt` +
  `secrets`; no new dependency). `portfolio_dash/api/auth_store.py` (table DDL, scrypt
  hash/verify with `hmac.compare_digest`, user/session CRUD, mode check) + routers `auth.py`
  (`POST /api/auth/login` sets an `HttpOnly; SameSite=Lax; Path=/` `pd_session` cookie; `GET
  /api/auth/session`; `POST /api/auth/logout`/`lock` → 204) and `users.py` (`GET/POST/DELETE
  /api/users`; 201 create / 409 `duplicate_username` / 400 short-or-empty). **Guest vs protected
  mode:** `auth_users` empty → everything open; ≥1 user → a global `require_session` dependency
  (wired into `create_app`, sharing `Depends(get_conn)` so it is test-overridable — NOT middleware)
  gates all `/api/*` except `login`/`session` → 401 without a valid, unlocked cookie. `golden_db`
  seeds no user (guest), so the entire pre-existing suite stays green. Stores only salted scrypt
  hashes; `password_hash` is never returned or logged; bad-username and bad-password are
  indistinguishable in status, body, **and timing** (a dummy scrypt verify equalizes the
  missing-user path — no username enumeration).
  - **`GET /api/auth/session` shape (additive to the spec's two literal examples):** not protected
    → `{"mode":"guest"}`; protected + valid/known cookie → `{"mode":"user", username, name, locked}`
    (a locked-but-known session reports `locked:true`); protected + absent/unknown cookie →
    `{"mode":"user", username:null, name:null, locked:false}` so the shell shows the login screen.
  - **Senior-review fixes folded in before merge:** equalized login timing (closes the
    username-enumeration side-channel); `PUT /scheduler/jobs` 400 `field` attribution (valid tz +
    bad cron now blames `cron`); `require_session` treats a missing `auth_users` table as guest
    (defensive, no 500 before lifespan); non-empty `username` validation; +coverage (authenticated
    request through the gate, `/api/users` gated when protected, valid-tz/bad-cron field).
    **Deferred (low risk for the 1–2-user localhost threat model, filed as follow-ups):** `/run`
    check-then-insert TOCTOU; cookie `Secure` flag (HTTPS only); `run_job_func` outer-except
    logging; last-user deletion silently reverting to guest mode.
- **Dividend projection in dashboard payload (spec 05, Phase 2):** `DashboardData.dividend_projection`
  — annual declared-dividend cash flow `{year, by_currency: {<ccy>: {declared_gross, declared_net,
  events}}, basis: "declared_only"}`, computed by the pure `portfolio/dividends.py::project_dividends`
  over the ex-dividend calendar + valued holdings. Net applies each holding account's dividend model
  via `apply_dividend_model` (drip_us → 30% US withholding; cash/cash_cost_reduction → net=gross).
  **Per-currency, never summed across currencies.** v1 is `declared_only` (events with `ex_date.year ==
  current year`); v2 `declared_plus_estimated` deferred. **Reconciliation:** the Moomoo-US per-dividend
  platform fee mentioned in the spec is NOT encoded (no per-dividend fee config; probe-pending) — v1 net
  applies withholding only.
  - **Account model: `dividend_model` is now a first-class field** (`shared/models/assets.py` +
    `list_accounts` SELECT). `project_dividends` reads it from the DB-sourced `accounts` param (single
    source of truth; fail-loud KeyError on an unknown account_id), resolving the prior split where the
    projection read config-as-code while `accounts.py` read the DB (senior-review finding).
- **strategy/ module: alerts, what-if, rebalance (spec 03, Phase 2):** a new
  `portfolio_dash/strategy/` consumer layer (pure functions over computed outputs; writes
  no ledger) + five endpoints. **Alert engine** — `compute_alerts_from(data, rules, *,
  quota_remaining, quota_threshold)` is the single source for both the dashboard payload's
  embedded `alerts` and `GET /api/alerts` (the dashboard path reuses its already-built
  `DashboardData`, no second build); six v1 rules (single_weight, sector_weight, stale_price,
  missing_price, fx_drift, exdiv_upcoming, quota_low — `quota_low` escalates warn→risk at
  remaining 0). `GET/PUT /api/alert-rules` — editable thresholds in a single-row JSON config
  (`alert_rules_config`), Decimal-as-string, bounds-validated (out-of-bounds → 400). **what-if**
  `POST /api/whatif` — buy/sell trade sim reusing the real `compute_fees` (compute, no write);
  `account_id` defaults to the most-shares account and is echoed; `oversell=true` still returns
  full numbers. **rebalance** `POST /api/rebalance/preview` — target-weight trades with integer
  shares (MY market rounds to 100-unit board lots), per-row fee/tax + `new_weight`, and a summary
  (turnover/fees in reporting ccy, cash_after, excluded). Missing-price symbols are excluded and
  missing FX leaves `new_weight` null — never fabricated.
  - **Reconciliations (recorded):** (R1) `calib_gap` / `calibration_regression` rules DEFERRED
    to spec 04 (their AI-calibration data source does not exist yet) — absent, not stubbed with
    fake data; (R2) `quota_low` threshold is sourced from spec-16's `llm_config.get_alert_threshold`
    (single source of truth), NOT stored in alert-rules; (R3) alerts single-sourced via
    `compute_alerts_from`; (R4) rebalance v1 acts only on symbols present in `targets` (held
    symbols absent from `targets` are left untouched).
- **Fixed — quota alert threshold default (spec-03 §3.1 SR):** `llm_config._DEFAULT_THRESHOLD`
  changed `0 → 1.00` so `quota_low` fires when remaining < 1.00 until the user sets their own
  threshold, matching the SR ("預設值 1.00"). Spec 16's contract is unaffected (it asserts the
  key's presence, not the default value).
- **Export endpoints (spec 02, Phase 2):** a new consumer-layer module `portfolio_dash/export/`
  + `POST /api/export/{holdings,ledgers,llm-usage,job-runs,tax-package}`. All output is
  reconciliation-grade: **raw `Decimal` strings** (no rounding/thousands separators), **UTF-8
  with BOM**, **CRLF**, `Content-Disposition: attachment`. holdings → 21-column snapshot CSV
  (incl. `reporting_ccy_value` via the promoted public `RateResolver`; blank on missing FX,
  never fabricated) + `# as_of/fx_rates/generated` footer; ledgers → zip of the four raw ledger
  CSVs + `fee_rules_snapshot.json` (Decimals as strings via `to_wire`) + `manifest.json`
  (counts/as_of/schema_version); llm-usage/job-runs → range-filtered raw CSV (`from>to` → 400
  `validation_error`); tax-package → annual zip (`realized_gains`/`dividends`/`fx_realized`/
  `summary.md`), **year-cut by trade date**, **per-currency never summed**, realized converted
  at **trade-date FX** with the rate recorded (blank when no stored rate). Each endpoint writes
  one `job_runs` audit row.
  - **Calc-core enrichment:** `RealizedRow.sell_date` (the sell transaction's trade date), so
    realized gains can be cut by tax year. Domain-model enrichment only — no accounting-semantics
    change.
  - **DRY:** `forex.fx_pnl.realized_fx_rows` is the single source of the realized-FX formula;
    `_realized_fx` now sums over it.
  - **Reconciliation — audit `kind`:** spec 02 §3 says the audit row carries `kind=export`, but
    `job_runs` has no `kind` column and spec 15.0 places `kind` on `schedule_config` (not
    `job_runs`). Implemented instead as a namespaced `job_id=export:<type>` via
    `scheduler.jobs.log_export_run`.
  - **Reconciliation — module map:** `portfolio_dash/export/` added as a consumer layer
    (`web_ui → export → {portfolio, forex, pricing, data_ingestion, scheduler, shared}`; nothing
    lower imports it; the router stays thin and computes no numbers of record).
- **Review fixes I-2 / I-3 (2026-06-13):** a single shared secret-masking helper
  `shared/masking.py::mask_secret` (`prefix•••suffix`, with a short-key guard that fully masks
  keys too short to safely reveal a prefix/suffix) — now the one masker for `api_key_masked` and
  data-source key views (I-2); and `default_registry(conn)` wiring the FinMind token from the
  `data_sources` DB into the provider chain (env/ctor fallback retained) so the configured key is
  actually used at runtime (I-3).
- **Instruments API (spec 10, Phase 1):** `GET /api/instruments` (list + held flag + latest
  price + `chg_pct` + target_low; TW board serialized `null` until confirmed),
  `POST /api/instruments/probe` (TW board probe via `probe_tw_board`),
  `POST/PUT /api/instruments` (register/update through `register_instrument`, with
  `duplicate_symbol` 409 / `validation_error` 400 / `not_found` 404 envelopes). Schema/model:
  `instruments += target_low/board_status/is_etf` (idempotent migration); `target_low`/`is_etf`
  on the `Instrument` model, `board_status` a registration-only column set by
  `register_instrument`; `is_etf` is the single source of truth for ETF (no `sector=="ETF"`).
- **Ledgers read API (spec 11, Phase 1):** `GET /api/ledgers/{transactions,dividends,fx,openings}`
  read-only over the four append-only ledgers — account-name join, account/symbol/date-range
  filters, desc pagination (`limit`/`offset`/`total_count`), the buy/sell `total` sign convention,
  `implied_rate`, and the **lowercase wire format** for `side`/`type` (Currency stays uppercase).
  Reuses the existing `transactions.fee_rule_snapshot` column (mapped to API `fee_snapshot`) — no
  new column; `openings` gets a synthetic display id (its PK is account_id+symbol). No write routes.
- **Input center — context + manual entry (spec 12a, Phase 1):** `GET /api/input/context`
  (accounts + mapped `div_model`, fee-rule serialization with label, instruments + `etf`,
  current holdings) and `POST /api/input/manual/{preview,commit}` over `enter_transaction`.
  New `api/wire.py` shared mappers: lowercase `side` in/out (`parse_side`), `Issue` →
  `{sev,code,text,field}` (`issue_wire`), `fee_rules_wire` (reused by spec 13), `div_model`
  mapping (`cash_cost_reduction→tw`/`drip_us→drip`/`cash→net`). Commit is **ack-gated**: hard
  issues → 400, unacked oversell → 422 `oversell_unacknowledged`, else append. (Known follow-up:
  unify API money-string formatting — `_money_str` trims trailing zeros in manual preview/commit
  while `to_wire`/ledgers use raw `str`; cosmetic, deferred to the frontend-wiring phase.)
- **Input center — CSV import + AI input (spec 12b, Phase 1):** `POST /api/import/{preview,commit}`
  (4 ledger kinds; preview → `{rows:[{n,status,reason,data}],summary}`; **commit re-derives from
  `csv_text`** and re-validates vs the current ledger, ack-gating warn rows → 422
  `warnings_unacknowledged`) and `POST /api/input/ai/preview` (LLM text → preview + `meta` +
  `csv_text`; degradation mapped `budget_exceeded`→402 / `ai_not_activated`→409 /
  `llm_unavailable`→503). `ai_agents_input` now returns `AiInputResult{preview, meta, csv_text}`
  (meta from the `llm_usage` row; `completer` default resolved at call time). Also fixed
  `build_transaction_preview` to catch `decimal.InvalidOperation` (a malformed number now yields a
  `parse_error` row instead of crashing — matching its siblings + docstring). Senior review added a
  soft `fuzzy_resolved` (ack-gated) issue so a fuzzy symbol match surfaces + writes the resolved
  symbol (no silent phantom-symbol writes), in both `txn_preview_row` and `enter_transaction`.
- **Top-bar actions (spec 08 §8.2–8.3, Phase 1 close-out):** `POST /api/actions/refresh-quotes`
  (triggers the per-market `quotes_*` jobs synchronously, returns their `job_runs` ids; unknown
  market → 400) and `POST /api/actions/recompute` (re-runs `build_book` over the ledgers to validate
  consistency, `OversellError` → 422; append-only, writes nothing). `run_job` now returns its run id.
  (Sync 200 instead of the spec's 202-background — the `GET /api/scheduler/runs` poll endpoint is
  spec 15, not yet built; `run_job` swallows provider errors so a failed fetch is a logged run, not a
  500. Revisit when spec 15 lands.) **Phase-1 core data flow (specs 08 / 10 / 11 / 12) backend complete.**
- **Settings batch — accounts/fees + datasources + LLM settings (specs 13 / 14 / 16, Phase 2; built as
  3 parallel worktree-isolated sub-projects):**
  - **spec 13:** `GET /api/accounts` (read-only) — accounts + dividend model + fee-rule serialization
    (reusing `api/wire.py`); `version.seeded_at` is `null` (accounts aren't recorded in `settings_meta`).
  - **spec 14:** data-source management — new `pricing/datasources_store.py` (config_store tables
    `data_sources` / `data_source_health` / `data_source_fallbacks`); `GET /api/datasources`,
    `PUT …/{id}/key`, `POST …/{id}/test`, `PUT …/fallbacks`; API keys write-only (masked
    `prefix•••suffix`); `FinMindProvider` reads its token from the DB via an injected getter
    (env/ctor fallback retained).
  - **spec 16:** `GET /api/llm/config` + model CRUD (`POST/PUT/DELETE /api/llm/models/{alias}`,
    api_key write-only, `model_in_use` 422) + `PUT /api/llm/roles` + quota topup/threshold + model
    connection-test; `LLMRole += MASTER / MASTER_FALLBACK` (spec 04 overlay); usage aggregation reads
    (`shared/llm_usage_reads.py`: by-model / by-agent / 30-day daily series).
  - Routers mounted in `api/app.py`; `golden_db` seeds the data_sources tables.
- `shared/` foundation layer: `Currency`/`Market` enums; `Decimal` money primitives
  (canonical TEXT persistence via `to_db`/`from_db`, per-currency `quantize_amount`
  with ROUND_HALF_UP, float + non-finite guards); single pure `fx.convert` helper
  (rejects non-positive / non-finite rates); env-driven `Settings` + cached
  `get_settings`; stdlib `sqlite3` `get_connection`/`session` (WAL, foreign keys on).
- Package + tooling bootstrap: `pyproject.toml` (pydantic, pydantic-settings; dev:
  mypy strict, ruff, pytest, pytest-asyncio; strict `asyncio_mode`); `portfolio_dash/`
  package with `py.typed`; `tests/` layout.
- `portfolio/` calculation core: chronological ledger replay (`build_book`) →
  holdings + realized P&L; `value_holdings` (unrealized vs adjusted, capital-gain vs
  original, stale-price flagging); `total_return` (per-currency + reporting blended);
  reporting-currency `xirr_reporting` (pyxirr); `sector_allocation`; `combined_view`.
- `shared/models/`: canonical domain models (`Account`, `Instrument`, `Transaction`,
  `Dividend`, `FXConversion`, `OpeningInventory`) + `Money` finite-Decimal type.
- Dependency: `pyxirr` (irregular-cashflow XIRR).
- `forex/` FX (換匯) P&L: per-account foreign-currency pool (weighted-avg acquisition
  rate from home→foreign conversions), reconstructed foreign cash balance, realized FX on
  reconversions, unrealized FX (stocks + cash) marked to spot; reporting-currency
  `FXSummary` rollup. Presented as an attribution decomposition of the portfolio return
  (asset + FX), never additive.
- Data-source availability probe (spike) under `scripts/probe/`: typed harness
  (`ProbeResult` model, `run_probe` runner + fixture recorder, markdown report renderer)
  + live adapters (yfinance, TWSE, TPEx, twstock, stockprices.dev, klsescreener; FinMind /
  AlphaVantage / Finnhub keyed). Produced a ranked primary/fallback recommendation per
  (data type × market) and recorded raw fixtures under `tests/pricing/fixtures/` for
  `pricing/` mock tests. Results + `pricing/` architecture recommendation:
  `docs/probes/2026-06-08-data-source-probe-results.md`. Key findings: yfinance is the
  US/MY/FX workhorse primary; TW latest quotes from TWSE/TPEx string sources for true tick
  precision; MY 3-dp verified via klsescreener (yfinance is float64 — convert via
  `Decimal(str(...))`); TW board (上市/上櫃) must be resolved per instrument; keyed sources
  (FinMind/AlphaVantage/Finnhub) and Schwab await keys/OAuth.
- FinMind **validated** (2026-06-08, trial token, 600/hr): 6 datasets confirmed (price,
  dividend/除權息, FX, financial statements, institutional, margin) with fixtures under
  `tests/pricing/fixtures/finmind/`. Added capability research notes under `docs/research/`
  for **Schwab Trader API** (enables US account/transaction auto-import for `data_ingestion/`)
  and **FinMind** — both feeding `pricing/` source selection, `llm_insight/` fundamentals, and
  the LLM self-backtest loop.
- `pricing/` market-data layer (A+B+C): config-driven, capability-aware provider chain
  (yfinance / TWSE / TPEx / FinMind-keyed) writing idempotent SQLite rows
  (`prices`/`fx_rates`/`dividend_events`) — the only writer of those tables. (A) latest quotes +
  FX, (B) historical daily backfill, (C) dividend/ex-dividend **reference** data (FinMind 除權息
  + yfinance fallback). Graceful degradation (last-known + staleness; never raises/fabricates),
  per-row source provenance, `Decimal(str())` precision, per-instrument TW board resolution.
  Read API (`get_latest_price`/`get_fx`/`get_price_history`/`get_dividend_events`) + orchestrators
  (`refresh_quotes`/`refresh_history`/`refresh_dividends`). Providers tested against the probe's
  recorded fixtures (no live network). Dividend events are reference-only — never the ledger,
  never in P&L. Plan: `docs/superpowers/plans/2026-06-08-pricing-market-data-layer.md`.
- `data_ingestion/` ledger input (the only ledger writer): SQLite schema for the four
  source-of-truth ledgers (`transactions`/`dividends`/`fx_conversions`/`opening_inventory`) +
  `instruments` registry + `accounts`/fee-rule/LLM-model config seed. Per-account **fee/tax
  engine** (config rules + per-row snapshot; TW 0.1425% / 0.3% / 0.1% / 0.15%, min NT$20, integer
  rounding; US/MY structures). Three input modes through one resolve→fee/tax→validate→
  **preview→confirm** pipeline: **manual**, **CSV import**, and **AI Agents Input** (natural
  language → LLM structured draft → confirm; the LLM never writes directly). Symbol resolution
  fuzzy → LLM-fallback → confirm; sell>holdings blocks until confirmed; per-account dividend
  models (TW cash / US DRIP 30% / MY cash). New `shared/llm.py` (LiteLLM client + structured
  output + model registry + `llm_usage` token/cost log + graceful degradation; `litellm` dep).
  Spec/plan: `docs/superpowers/{specs,plans}/2026-06-09-data-ingestion*`.
- LLM config management + token-budget governance (`shared/`): DB-backed model registry
  (`llm_models`; per-model provider / endpoint / key / `vision` flag / pricing / context-window /
  timeout / retries / enabled). Four **nullable** role-defaults (`default` / `default_fallback` /
  `vision` / `vision_fallback`) — all empty = AI cleanly **off** (first-launch seed). `complete_structured`
  now: budget gate → role selection → **runtime failover** to the fallback model on provider error →
  **image (vision)** input → cost logged from the *selected* model's registry pricing. Three
  degradation signals — `AINotActivated` / `LLMUnavailable` / `LLMBudgetExceeded` (all subclass
  `LLMError`) — surfaced to callers (mapped to issue `kind`), never crash or fabricate. **USD budget**
  as an append-only reset ledger (`llm_budget_events`): remaining = latest reset amount − Σ usage cost
  since that reset; **unset = no cap**; **remaining < 0 blocks** ("額度用盡"); per-model usage/trend from
  `llm_usage` is never reset (a reset is a fresh start line, not a counter overwrite). Reusable
  `config_store` create-always / seed-once settings framework; package-root `portfolio_dash/bootstrap.py`
  composition root (so `shared/` keeps importing nothing internal); `llm_usage` ownership moved from
  `data_ingestion/` to `shared/llm_config`. AI Agents Input rewired to the registry API (no
  caller-supplied pricing). The settings-page UI stays deferred to `web_ui/`. Spec/plan:
  `docs/superpowers/{specs,plans}/2026-06-09-llm-config-and-budget*`.
- `scheduler/` in-process job scheduling (APScheduler, **triggers-only**): an extensible `JobSpec`
  registry + DB-backed `schedule_config` (on the `config_store` framework; idempotent per-job seeding,
  so a newly-registered job auto-gets a default row while user edits are preserved) + a `job_runs` log.
  v1 jobs trigger `pricing.refresh_*`: per-market post-close quotes + FX (`quotes_tw` / `quotes_us` /
  `quotes_my`, editable cron defaults in each exchange's tz), plus daily `history_daily` +
  `dividends_daily` sweeps; a manual `trigger_job` shares the same `run_job` path (job_runs logging; a
  job failure is logged as `error`, never crashes the scheduler). `build_worklist` reads the
  `instruments` table — a new nullable **`instruments.board`** column (idempotent migration) carries the
  resolved TW board, falling back to the market default (US `""` / MY `.KL` / TW `TWSE`) when unset.
  New dependency: `APScheduler` (locked in `stack.md`), confined to `scheduler/runtime.py`. The
  Scheduler settings-page UI is deferred to `web_ui/`. Spec/plan:
  `docs/superpowers/{specs,plans}/2026-06-10-scheduler*`.
- TW board resolution at instrument registration (`data_ingestion/` + `pricing/` + `shared/`):
  `Instrument` gains a persisted **`board`** attribute (`store.py` reads/writes it). `pricing.probe_tw_board`
  guesses a TW instrument's board by trying TWSE then TPEx (injectable providers, graceful on a network
  error). `data_ingestion.register_instrument` fills the board — US `""` / MY `.KL` deterministic; TW via
  an **injected** prober (keeping `data_ingestion` decoupled from `pricing`) — and upserts on confirm,
  raising a soft `board_unresolved` flag (never blocking) when a TW probe finds nothing. Resolves the
  board once so the scheduler work-list picks the right `.TW`/`.TWO` source; the listing/confirm UI is
  deferred to `web_ui/`. Spec/plan: `docs/superpowers/{specs,plans}/2026-06-10-tw-board-resolution*`.
- `portfolio/dashboard.py` — the orchestration combiner: `build_dashboard(conn, now,
  reporting)` assembles one complete `DashboardData` (KPIs, enriched holdings, realized
  P&L, returns, sector allocation, currency view, FX P&L, dividend summary, ex-dividend
  calendar, daily-replay trend series, freshness report, insight placeholders) from the
  ledgers + stored prices/FX; the contract `web_ui` (and later `llm_insight`) binds to.
  Introduces the one-way dependency edge `portfolio -> forex` (spec
  2026-06-10-dashboard-combiner-design).
- `portfolio/timeseries.py` — pure daily ledger-replay valuation series (market value
  vs cumulative net invested, carry-forward prices/FX, honest `incomplete`/unavailable
  flags).
- `pricing/store.py` — `get_fx_on` (on-or-before point-in-time rate) and
  `get_fx_history` reads; `data_ingestion/store.py` — `list_accounts` read.
- **Phase 0 — web API foundation (decision B):** `portfolio_dash/api/` FastAPI app
  factory (lifespan boots DB + scheduler; serves static `web/` via StaticFiles; routers
  under `/api/*`), the common error envelope (incl. LLM 402/409/503 mapping), the
  Decimal→string wire serializer (`to_wire`), per-request `get_conn`/`get_now`/`get_reporting`
  dependencies, and `GET /api/health` + `GET /api/dashboard` (serialized `build_dashboard` +
  `spark_30d` + `llm_quota`). Spec-17 test harness: `golden_db` fixture (seeded via the real
  write paths), injected clock (`GOLDEN_NOW`), `api_client`, `pytest-socket` network ban, and
  a `Makefile` (`make all`). Fee engine (spec 18): `FeeRuleSet` gains `flat_fee` /
  `stamp_duty_rate` / `stamp_duty_cap` and US/MY `min_fee`; MY stamp duty books to `tax`;
  worked examples W1–W9; US/MY rates backfilled from the spec-18.0 truth table (pending
  real-statement confirmation). `DividendType += NET` (MY single-tier).

## [v0.0.0] - 2026-06-05

### Added
- Project bootstrap: `CLAUDE.md`; `.claude/rules/` (stack, architecture,
  domain-ledger, markets-and-fees, data-and-pricing, llm-insight, engineering-process,
  design-handoff); `.claude/skills/` (resume-dev, ship-version); README, this
  changelog, LESSONS_LEARNED, .gitignore.
- Locked technology selection (Python 3.12 monolith: FastAPI + Jinja2 + HTMX +
  Alpine + ECharts + SQLite + LiteLLM + APScheduler; mypy strict; pytest).
- Domain model: `account` as a first-class entity (TW broker · Charles Schwab US ·
  Moomoo MY US · Moomoo MY); three markets (TW / US / MY); multi-currency
  (TWD / USD / MYR) with a single-reporting-currency combined XIRR (trade-date FX)
  and a currency-exchange ledger.
- Numeric precision model: `Decimal` end to end; store at full source precision
  (MY prices up to 3 dp), quantize amounts per currency minor unit at settlement.

_No application code yet — conventions and specification scaffold only._
