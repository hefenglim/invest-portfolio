# Changelog

All notable changes to this project are documented here. Format based on
*Keep a Changelog*; released versions use the heading `## [vMAJOR.MINOR.PATCH] - YYYY-MM-DD`.

**Integrity check** — after any edit to this file, run
`grep -c "^## \[v" CHANGELOG.md`; the count must equal the number of released version
headings. (`## [Unreleased]` is intentionally not counted.)

## [Unreleased]

### Changed
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
