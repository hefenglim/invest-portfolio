# Design: LLM Config Management + Token Budget Governance

- **Date:** 2026-06-09
- **Status:** Approved (design); pending spec review
- **Modules:** `portfolio_dash/shared/` (new `config_store.py` + `llm_config.py`; expands `llm.py`)
- **Depends on:** `shared/` (models, Decimal/money, db, Settings). Per `architecture.md`, `shared/`
  depends on nothing internal and is importable everywhere. The **settings page UI** that edits
  these values is `web_ui/` and is **out of scope here** (deferred), consistent with how the
  "AI cost-info page" was split: build the registry + governance + gate now, render the page later.

## Context & purpose

Today the LLM layer is a thin single-endpoint client: `shared/llm.py.complete_structured` reads one
global model/endpoint/key from env (`Settings.llm_endpoint/llm_api_key/llm_active_model`), logs each
call to `llm_usage`, and computes cost from a caller-supplied `ModelPricing`. There is **no** model
registry, no provider/vision concept, no fallback, no role-based selection, and **no budget control**.

This sub-project builds the **DB-backed LLM configuration + token-budget governance** the dashboard
needs to manage multiple models, route by role, support image (vision) input, and enforce a hard
spending cap — while establishing a **reusable DB-backed settings framework** that fee/account/prompt/
data-source config can migrate into later (scope option A).

## Decisions (settled 2026-06-09, human sign-off)

1. **Storage = DB, authoritative.** All LLM settings (models, defaults, budget, **including API keys**)
   live in dedicated SQLite tables. Code no longer reads model config from `.env`. **First launch
   seeds** the tables into a clean **AI-off** state (tables created, four role-defaults empty, no
   budget). Code-defined defaults serve as seed/restore values, not the runtime source of truth.
2. **Four global role-defaults, each nullable:** `default`, `default_fallback`, `vision`,
   `vision_fallback`. A null role = that role is **disabled**. When a feature needs a role that is
   null (or all relevant roles are null) → it gets an **"AI Agent 未啟動"** signal, not an error.
   Initial state = all four null = the whole AI layer is cleanly off until configured.
3. **Three distinct degradation signals** (each caught by callers; the dashboard never crashes, never
   fabricates — per `llm-insight.md`):
   - **Not activated** — required role unconfigured → surface "AI Agent 未啟動".
   - **Unavailable** (`LLMUnavailable`, exists) — provider errored or output unparseable.
   - **Budget exhausted** (`LLMBudgetExceeded`, new) — remaining budget < 0.
4. **Budget unit = USD ($).** A dollar cap is the true cross-model spend ceiling. (A token cap only
   bounds cost under a single model; deferred — not built now.)
5. **Budget = an append-only ledger, computed on read (no mutable counter).** Reset events are rows in
   `llm_budget_events`. **Remaining = (amount of the most recent reset) − (Σ cost of `llm_usage` rows
   dated at/after that reset).** "重置 token 金額" writes a **new reset event** (a fresh start line); it
   never edits a counter and never deletes history. Per-model usage + trend come from the **full**
   `llm_usage` history and are **never** reset. This mirrors the project invariant: ledgers are the
   source of truth, derived figures are recomputed on read (`average_cost`, 重算).
6. **Overshoot policy = block only when remaining < 0** (check before each call; actual cost is logged
   after and reflected in the next check). At most one small overshoot into the negative is tolerated.
   No pre-call estimation/blocking.
7. **Vision input** is supported (image content blocks). Its first concrete use is **broker
   statement / screenshot → structured transaction draft → preview → confirm** (the future Agent B),
   reusing the same `preview→confirm` pipeline as AI Agents Input. This spec builds the **vision
   plumbing + role routing**; the full Agent B statement parser is a `data_ingestion` follow-up.
8. **Scope = LLM config + budget, on a generic settings skeleton** (option A): build a reusable
   DB-backed settings framework (category registry, first-launch seed, code-defaults-as-restore,
   uniform read/write/restore); LLM is its first consumer. Fees/accounts/prompts/data-sources migrate
   into the same framework later, no rewrite.

## Data model / tables (Decimal as canonical TEXT, per `data-and-pricing.md`)

`shared/` owns these new tables (created idempotently on first launch). LLM-related tables are
consolidated here; the existing `llm_usage` table (currently created by `data_ingestion/schema.py`) is
**moved/owned here** so all LLM state lives together (a small, clearly-noted refactor).

- **`llm_models`** — the model registry:
  - `id` TEXT PK (stable internal slug, distinct from display name and provider model id)
  - `model_alias` TEXT (UI display label)
  - `provider` TEXT — `openai` / `openrouter` / `anthropic` / `openai-compatible`
  - `model_name` TEXT — the provider's model id (e.g. `claude-opus-4-8`, `Qwen/Qwen3-...`)
  - `api_base` TEXT NULL — required for `openai-compatible`; for named providers, left null and the
    official base is implied by LiteLLM
  - `api_key` TEXT NULL — **stored in DB** (SQLite is gitignored; acceptable for 1–2 self-hosted users)
  - `vision` INTEGER (0/1) — explicit "model accepts image input" flag (do not rely on
    `litellm.supports_vision` for arbitrary openai-compatible endpoints)
  - `input_price_per_mtok` TEXT, `output_price_per_mtok` TEXT — Decimal, USD per 1M tokens
  - `context_window` INTEGER NULL, `max_output_tokens` INTEGER NULL
  - `timeout_seconds` INTEGER NULL, `max_retries` INTEGER NULL
  - `enabled` INTEGER (0/1) — keep a config but disable it
  - `notes` TEXT NULL
- **`llm_defaults`** — role → model binding (one row per role):
  - `role` TEXT PK ∈ {`default`, `default_fallback`, `vision`, `vision_fallback`}
  - `model_id` TEXT NULL → references `llm_models.id`; **NULL = role disabled**
- **`llm_budget_events`** — the reset/recharge ledger:
  - `id` INTEGER PK AUTOINCREMENT, `ts` TEXT (UTC ISO), `amount_usd` TEXT (Decimal), `note` TEXT NULL
- **`llm_usage`** (exists, moved here) — per-call log: `ts, model, agent, input_tokens,
  output_tokens, cost`. Source for both cost deduction and the per-model usage/trend chart.

### Generic settings framework (the reusable skeleton)

Not a single EAV table — a **pattern + small framework** in `shared/config_store.py`:
- A **category registry** (`llm`, and later `fees`, `accounts`, `prompts`, `data_sources`).
- **First-launch seeding**: on first run, ensure each registered category's tables exist and are
  seeded to its defined default state (for LLM: AI-off / empty).
- **Code-defaults-as-restore**: each category declares its default values in code; these are the
  seed values **and** the "restore to default" target — never the runtime authority once the DB holds
  values.
- A **uniform interface** (`ensure_seeded(conn)`, `restore_defaults(conn, category)`), so a new
  category is added by: define its tables + its default state + register it. No framework rewrite.
LLM provides typed relational tables (above) conforming to these conventions, rather than KV blobs.

## Budget governance (in `shared/llm_config.py`)

- `budget_remaining(conn) -> Decimal | None`:
  - If `llm_budget_events` is empty → **no cap set** → returns `None` (calls allowed, surfaced as
    "no budget cap"). *(Open item — confirm at review; alternative is "unset = blocked". Recommended:
    unset = no cap, because role-defaults already gate the AI off initially; the $ cap is an opt-in
    safety the user turns on by setting an amount.)*
  - Else: `latest = most recent reset (by ts,id)`; `remaining = latest.amount_usd − Σ Decimal(cost)
    for llm_usage where ts >= latest.ts`. Costs summed as `Decimal` in Python (low volume).
- `reset_budget(conn, amount_usd, note=None)` — appends a new `llm_budget_events` row (the "重置"
  action). History untouched.
- `check_budget(conn)` — the **gate**: if a cap is set and `remaining < 0` → raise
  `LLMBudgetExceeded`. Called **before** each LLM request inside `shared/llm.py`. Actual cost is
  logged after the call (existing behavior) and reflected in the next gate check.

## Role-based model selection + runtime fallback (in `shared/llm.py`)

`select_model(conn, *, vision: bool)` resolves the model for a call:
- **Text** call → try `default`; on a configured-but-erroring model, fail over to `default_fallback`.
  If both roles are null → raise `AINotActivated` ("AI Agent 未啟動").
- **Vision** call → try `vision`; fail over to `vision_fallback`. If both null → `AINotActivated`.
- Fallback therefore has real teeth: it covers **both** "primary role unconfigured" **and** "primary
  model errored at call time" (retry the request on the fallback model).
- Pricing for cost logging is read from the **selected model's** `llm_models` row — callers no longer
  pass `ModelPricing`.

`complete_structured` is expanded:
- New optional `images: list[bytes] | None`. When present → vision path: assemble an OpenAI-style
  multimodal `content` list (text block + `image_url` blocks as base64 data URLs) and route to the
  vision role; otherwise the existing text path.
- Per-call `timeout` / `num_retries` / `max_tokens` taken from the selected model's registry row and
  passed to `litellm.completion`.
- Order of operations per call: `check_budget` → `select_model` → `litellm.completion`
  (with fallback on provider error) → parse/validate (retry once) → `log_usage` (cost from registry).
- Exceptions: `AINotActivated`, `LLMUnavailable` (exists), `LLMBudgetExceeded` — all are caught by
  callers for graceful degradation.

## Migration off the current single-endpoint client

- `Settings.llm_endpoint/llm_api_key/llm_active_model` are **removed** from the model-config path (DB
  is authoritative). They may be retained only as an optional one-time bootstrap seed source, or
  deleted — decided in the plan; default is **delete** (clean DB-only).
- `data_ingestion/config_seed.py`'s `ModelPricing` + empty `DEFAULT_LLM_MODELS` are **superseded** by
  `llm_models`; removed/migrated.
- Existing callers (`data_ingestion/agents.py` AI Agents Input, `data_ingestion/resolve.py` LLM
  symbol resolution) are rewired to the new `complete_structured` (no `pricing` arg; pricing now
  comes from the registry). Behavior preserved; on `AINotActivated`/`LLMBudgetExceeded`/`LLMUnavailable`
  they degrade exactly as today (a clear issue row, never a crash, never a silent write).
- `FinMind` and other **data-source** keys stay in `.env` for now (the `data_sources` settings
  category is a later migration, not this spec).

## Architecture / boundaries

- All of this lives in `shared/` (importable everywhere; depends on nothing internal). It does not
  compute P&L, fetch prices, write ledgers, or render UI.
- The **settings page** (model CRUD, role-default pickers, budget set/reset, usage & trend charts,
  screenshot upload for vision) is **`web_ui/`, deferred**. This spec delivers the registry,
  governance, gate, role selection, and vision plumbing the page will sit on.

## Error handling / degradation

- `AINotActivated` / `LLMUnavailable` / `LLMBudgetExceeded` are the only ways the layer refuses work;
  each carries a clear message the caller maps to a user-facing state. No fabrication, no crash.
- Budget gate and reset run in a single SQLite writer; the gate's read + the call's later log are
  serialized by the connection (1–2 users, no real concurrency).

## Testing strategy (mock LLM; no live network)

- **Budget ledger math** (pure): remaining with no events (→ None), one reset, multiple resets (only
  the latest counts), and usage driving remaining negative.
- **Gate**: blocks at remaining < 0, allows at ≥ 0, allows when unset (per decision/open item).
- **Role selection + fallback**: primary null → uses fallback; primary errors (mock `litellm`
  raising once then succeeding) → fails over; all roles null → `AINotActivated`; vision routing picks
  the vision role.
- **Vision content assembly**: image bytes → correct multimodal message structure (assert on the
  assembled `messages`, no network).
- **Cost from registry**: `log_usage` uses the selected model's pricing; row persisted.
- **Degradation trichotomy**: each exception surfaces and callers degrade without crashing.
- **Framework**: first-launch seed produces the AI-off empty state; `restore_defaults` re-applies
  code defaults; registry CRUD round-trips (incl. Decimal-as-TEXT prices).

## Out of scope (deferred / other modules)

- The settings **page UI** + usage/trend charts + screenshot upload widget (`web_ui/`).
- Migrating **fees / accounts / prompts / data-sources** config into the DB framework (later, via the
  same skeleton).
- The full **Agent B** broker-statement parser (a `data_ingestion` follow-up; this spec enables the
  vision input path it will use).
- A **token-unit** budget cap (only USD now), pre-call cost **estimation/blocking**, and per-feature
  per-agent model overrides (four global roles only for now; data model leaves room to add overrides).
- Agents C/E/F (portfolio Q&A, data-quality, insight/strategy) — later phases.

## Designed-in flexibility (per human directive)

The category-registered settings framework, the nullable role-defaults, runtime fallback, an explicit
`vision` flag, and a ledger-based budget mean new models, providers, roles, budget resets, and future
config categories are **config edits + small additions, not rewrites**. Concrete extras (token cap,
per-feature overrides, more categories, more agents) are deferred until real use surfaces them (YAGNI
per `stack.md`).

## Staging (the plan will sequence these)

1. Generic DB-backed settings framework (`config_store.py`): category registry, first-launch seed,
   code-defaults-as-restore, uniform interface + table bootstrap.
2. `llm_models` registry table + CRUD + Decimal-as-TEXT pricing + restore-to-default (AI-off seed).
3. `llm_defaults` four nullable role rows + `select_model` with runtime fallback + `AINotActivated`.
4. `llm_budget_events` + `budget_remaining` + `reset_budget` + `check_budget` gate +
   `LLMBudgetExceeded`.
5. Expand `shared/llm.py`: role-based selection, budget gate, pricing-from-registry, per-model
   timeout/retries/max-tokens.
6. Vision: image content assembly + vision-role routing + `complete_structured(images=...)`.
7. Migrate off `.env`/`config_seed` LLM bits; rewire existing callers (AI Agents Input, symbol
   resolution) to the new API; move `llm_usage` ownership into `shared/`.
