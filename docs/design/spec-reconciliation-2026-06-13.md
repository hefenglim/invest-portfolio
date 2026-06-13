# Spec Reconciliation & Build Sequence — design-handoff specs 01–19

- **Date:** 2026-06-13
- **Author:** Claude Code (reconciliation pass against locked architecture)
- **Source specs:** `docs/design-handoff/ai-portfolio-watcher/project/specs/` (01–19 + README + HANDOFF + SR-2026-06-13)
- **Verdict in one line:** Inner layers (calculation core, money discipline, ledger
  invariants, module boundaries, LLM-never-emits-numbers) conform **very well**. The
  **web layer diverges by design** — JSON API + static vanilla-JS frontend instead of the
  originally-locked Jinja2 + HTMX server-rendering. This divergence is **accepted as
  decision (B)** (recorded in `CHANGELOG.md`, 2026-06-13, human sign-off). Several feature
  expansions beyond the original scope are likewise human-approved and recorded below.

---

## 1. Locked-decision change: web layer → (B) JSON API + static vanilla-JS frontend

| | Originally locked (`CLAUDE.md` / `stack.md` / `design-handoff.md`) | Adopted (B), 2026-06-13 |
|---|---|---|
| Rendering | Jinja2 server-rendered HTML | Static HTML + client-side JS rendering |
| Front/back coupling | Single codebase, **no JSON contract between two codebases** | `web/` static frontend + `portfolio_dash/api/*` JSON; `mock-data.js` = the documented contract |
| Interactivity | HTMX server round-trips + Alpine | Unified `web/api.js` fetch wrapper (spec 19.1) |

**Why (B) is acceptable and not a stack-drift violation:** the guardrail in `design-handoff.md`
forbids introducing **a JS framework or a build step**. The export honors that — it is
**vanilla JS + ECharts CDN, no framework, no bundler**. It also pushes **all computation to
the backend** (spec 19.1 §4: "前端絕不 parseFloat 後運算，運算一律在後端"), so the web layer
still does not compute — the deeper intent of invariant #4 holds. What (B) trades away is
"single codebase / no contract to drift"; this is mitigated by (a) `mock-data.js` as the
explicit, version-controlled contract and (b) spec 18.4 §3 + spec 17.2 golden-snapshot tests
that assert the JSON contract round-trips exactly. **Decisive upside:** the JSON contract makes
the automated regression loop *stronger* (machine-diffable golden payload) than HTML-fragment
assertions would be.

`CLAUDE.md` and `stack.md` web-layer rows must be amended; `design-handoff.md` integration
rules (“convert to Jinja2 templates”) are superseded for this project. **The HANDOFF.md
template that overwrites `CLAUDE.md` is NOT applied verbatim — it is reconciled** (existing
locked decisions, accounting rules, and bilingual/process rules are preserved; the web-layer
rows and the new `api/` + test-harness rules are merged in).

## 2. Per-spec conformance

Legend: ✅ conforms / 🟦 additive new surface (approved) / ⚠ needs care during build.

| Spec | Scope | Conformance | Notes |
|---|---|---|---|
| **08** app-shell + dashboard API | new `api/` layer; `GET /api/dashboard` | 🟦 | New HTTP layer (expected under B). Router = call core + serialize, no compute (honors inv #4). `now`/Registry/completer injectable (test determinism). |
| **17** test/regression harness | pytest + TestClient + Playwright + golden | ✅🟦 | Exactly the closed-loop we want. `mock-data.js` = golden expected values. pytest-socket blocks network. New dev infra. |
| **18** calculation correctness | fee truth-table, identities, property tests | ✅ | Strengthens our rules (Decimal AST check, single `compute_fees`, no cross-ccy sums). Backfills US/MY fee rates into `config_seed` (were placeholders). |
| **19** frontend wiring + ops | `api.js`, layout, backup/restore, logging | 🟦 | New `web/` static dir + `/api/*`. SQLite backup job. All additive. |
| **10** instruments | registry/probe/register routes | ⚠🟦 | Wraps existing `register_instrument`/`probe_tw_board`. **Schema migration:** `instruments += target_low, board_status, is_etf`. |
| **11** ledgers read | 4 read endpoints | ⚠ | Wraps existing `list_*`. **Schema migration:** `transactions += fee_snapshot`. **enum:** `DividendType += NET`. |
| **12** input center | manual/CSV/AI preview+commit | ✅ | Wraps existing `enter_transaction`/`build_*_preview`/`commit_preview`/`ai_agents_input`. Two-phase preserved; append-only preserved. Vision entry adds a completer path. |
| **01** symbol detail | `GET /api/symbol/{s}/detail` | ✅ | Pure read over existing price history + ledgers + holdings. `spark_30d` added to dashboard payload. |
| **02** export endpoints | reconciliation-grade CSV/zip | ✅🟦 | Pure read → file. New `api/export` surface; each export logs a `job_runs` row. |
| **03** strategy/alerts/rebalance | `strategy/` rule engine, what-if, rebalance | ⚠🟦 | Lands in the planned `strategy/` module as **pure functions over computed outputs** (honors architecture.md). **BUT** adds editable thresholds (`PUT /api/alert-rules`) — edges toward the "user-facing rule builder" architecture.md said *not* to build yet. Approved as config-row thresholds (not a DSL). what-if/rebalance reuse `compute_fees` (inv: single engine). |
| **04** llm_insight self-evolution | strategy prompts, insight types, calibration chains, master role, backtest | ⚠🟦 | **Largest expansion.** Far beyond locked "batch insight cards." Respects inv #1 (§4.4 quant-hit is code, not LLM; §4.8 LLM only writes narrative/calibration text; numbers from injected vars). New tables + `master` LLM role. Human-approved 2026-06-12. |
| **05** dividend projection | dashboard `dividend_projection` | ✅ | Pure calc reusing dividend models; native-ccy, never summed across ccy. |
| **06** data variables + external snapshots | prompt-var registry, preview/test, FinMind/sentiment ingest | ⚠🟦 | Honors inv #1 ("LLM 不自行計算; 變數由計算核心組裝"). New `external_snapshots` table (append-only, reproducible). New ingest jobs expand `pricing`/scheduler scope. |
| **07** pipeline UX | insight-task status/preflight/diagnose | ✅🟦 | Preflight shares the runtime gate code (no double-truth). Route aliases only; no table rename. |
| **09** auth/users | session + users CRUD | 🟦 | New domain (was absent). stdlib `hashlib.scrypt` + `secrets` — **no new dependency**. Guest mode (no users) = open; protected mode once a user exists. |
| **13** accounts/fees | read-only routes | ✅ | Wraps `list_accounts` + `FeeRuleSet`. Shares serializer with spec 12.1 (no double-write). |
| **14** datasources | keys/health/fallback chain | ⚠🟦 | **New tables:** `data_sources`, `data_source_health`, `data_source_fallbacks`. FinMind token moves from env/ctor → DB (key masked in API). Registry chain becomes DB-driven (fallback to hardcoded default). |
| **15** scheduler | jobs/cron/run/history routes + dynamic reschedule | ⚠🟦 | Wraps existing `scheduler/`. **Schema migration:** `schedule_config += kind, payload`; `job_runs += payload, reason, cost_usd`. New `runtime.reschedule/pause/resume`. |
| **16** llm settings | model/role/quota/usage routes | ⚠ | Wraps existing `llm_config` four tables. **enum:** `LLMRole += MASTER, MASTER_FALLBACK` (spec 04). New `usage` aggregation reads (were write-only) — fills the gap I flagged earlier. |

## 3. Inventory of changes that touch locked surfaces (build must honor)

**Schema migrations** (all via existing `data_ingestion.schema._add_column_if_missing` or `config_store`):
- `instruments += target_low TEXT NULL, board_status TEXT DEFAULT 'resolved', is_etf INTEGER DEFAULT 0` (spec 10)
- `transactions += fee_snapshot TEXT NULL` (spec 11)
- `schedule_config += kind TEXT DEFAULT 'system', payload TEXT NULL` (spec 15)
- `job_runs += payload TEXT NULL, reason TEXT NULL, cost_usd TEXT NULL` (spec 15)
- New tables: `auth_users`, `auth_sessions` (09); `data_sources`, `data_source_health`, `data_source_fallbacks` (14); `external_snapshots` (06); `strategy_prompts`, `insight_types`, `insight_type_strategies`, `calibration_prompts`, `insight_evaluations` (04); alert-rules config row (03)

**Enum extensions** (core layer, API maps to lowercase wire format):
- `DividendType += NET` (spec 08/11) — and `apply_dividend_model` must support it.
- `LLMRole += MASTER, MASTER_FALLBACK` (spec 04/16).

**Model extensions:**
- `Instrument += target_low: Decimal | None, is_etf: bool` (spec 10).
- `FeeRuleSet += flat_fee, min_fee on US/MY branches, stamp_duty_rate + stamp_duty_cap` (spec 18.0.1) — the fee-engine structural gaps.

**New modules / scope expansions (human-approved):**
- `portfolio_dash/api/` — the whole HTTP layer (decision B).
- `strategy/` — alerts rule engine + what-if + rebalance (spec 03), as pure functions.
- `llm_insight/` — full self-evolution system (spec 04), far beyond "batch cards".
- External-data ingest (FinMind chips/fundamentals/valuation, VIX, Fear&Greed, indices — spec 06) + `external_snapshots`.
- Dev infra: `Makefile`, `make all`, hypothesis/mutmut/freezegun/pytest-socket/playwright, SQLite backup/restore + structured logging (specs 17/19).

**Fee truth-table (spec 18.0) — backfills `config_seed.FEE_RULES`** US/MY placeholders with the design's values. ⚠ flagged "待使用者最終確認" against real broker statements (SEC fee rate, MY stamp-duty cap, Moomoo platform fee buy/sell). Build against the table; user confirms numbers later.

## 4. Conflicts already resolved by the spec authors (SR-2026-06-13)

The spec set self-reviewed and fixed 8 front/back conflicts (enum case, `DividendType.NET`,
LLM-exception→HTTP mapping 402/409/503, preview-always-200, `usage.daily` shape,
`quota_low` single-source, `whatif.account_id`, scheduler schema). Q1 (multi-account same
symbol) closed: default = account holding the most of that symbol, echoed in responses. No
open blockers. These are accepted as-is.

## 5. Residual risks / watch-items during build

1. **`alert-rules` editable thresholds** (spec 03) is the one genuine brush against
   architecture.md's "no user-facing rule builder yet". Accepted narrowly: it is **config
   rows with min/max bounds**, not a DSL/expression builder. Do not let it grow into one
   without a new recorded decision.
2. **`llm_insight` (spec 04) scope** is large; build it last (P1–P2) and keep inv #1 a hard
   test (a property test: no LLM call output ever becomes a number of record).
3. **External ingest rate limits** (FinMind 600/hr) — jobs must batch + backoff (spec 06.3).
4. **CLAUDE.md reconciliation, not overwrite** — preserve locked accounting/ledger/process
   rules; merge in web-layer (B) + `api/` + test-harness rules.
5. **Secrets:** `data_sources.api_key` stored plaintext in the local DB by design (single-user);
   API responses mask it. The DB + `.env` stay git-ignored (unchanged rule). FinMind token
   never committed.

## 6. Build sequence (sub-projects)

Follows the spec authors' recommended order, grouped into our sub-project flow. Each
sub-project = brainstorm-light (specs already are the design) → writing-plans → subagent-driven
→ gates (`make all`) → finish. Each ends green per spec 17.6.

| Phase | Sub-project | Specs | Why first / depends on |
|---|---|---|---|
| **0 Foundation** | API skeleton + test harness + ops + fee truth-table | 08 §8.0, 17, 19, 18 | Everything hangs on the FastAPI app, the test harness, and the fee structural fixes. Includes layout (`web/`, `specs/`), CLAUDE.md reconciliation, `pyproject`/`Makefile`, schema migrations groundwork, `DividendType.NET`. |
| **1 Core data flow** | dashboard + instruments + ledgers + input center | 08 §8.1+, 10, 11, 12 | The P0 read+write spine; replaces the biggest mocks; unblocks E2E E1–E4. |
| **2 Symbol + export + strategy** | symbol detail, exports, alerts/what-if/rebalance | 01, 02, 03 | Build on the data spine; `strategy/` pure functions. |
| **3 Settings & admin** | auth, accounts/fees, datasources, scheduler, llm-settings | 09, 13, 14, 15, 16 | Operational surfaces; mostly wrap existing backends. |
| **4 AI insight system** | data variables + ingest, llm_insight self-evolution, pipeline UX | 06, 04, 07 | Largest + most novel; built last on a proven base. |

Cross-cutting throughout: spec **17** (every sub-project ends `make all` green) and spec **18**
(calculation-correctness identities + worked examples are permanent regression anchors).

## 7. Immediate next step

Begin **Phase 0**: physical handoff layout (copy `web/`, place `specs/`), reconcile
`CLAUDE.md` + `stack.md` web rows for (B), add `pyproject`/`Makefile` deps, scaffold
`portfolio_dash/api/app.py` (lifespan + StaticFiles + `/api`), and stand up the spec-17 test
harness (`tests/conftest.py` golden_db + frozen clock + fake providers/completer). Then
`GET /api/dashboard` (spec 08 §8.1) as the first real endpoint, proving the golden-payload
regression loop end to end.
