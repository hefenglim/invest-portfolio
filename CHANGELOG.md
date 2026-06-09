# Changelog

All notable changes to this project are documented here. Format based on
*Keep a Changelog*; released versions use the heading `## [vMAJOR.MINOR.PATCH] - YYYY-MM-DD`.

**Integrity check** — after any edit to this file, run
`grep -c "^## \[v" CHANGELOG.md`; the count must equal the number of released version
headings. (`## [Unreleased]` is intentionally not counted.)

## [Unreleased]

### Changed
- **Accounting model decision (2026-06-06, human sign-off):** P&L now uses the
  adjusted-cost model — cash dividends fold into cost (no separate dividend-income line),
  realized/unrealized computed vs `adjusted_cost`; `original_cost` retained for the
  return-rate denominator and the capital-gain-vs-dividend split. Supersedes the prior
  original-cost-plus-separate-dividend rule in `domain-ledger.md`. The no-double-count
  principle is preserved (dividends still counted exactly once). Return-rate denominator
  stays original invested cost; cost basis is all-in (incl. buy fees+tax).

### Added
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
