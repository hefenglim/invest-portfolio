# Design: `data_ingestion/` — Ledger Input (manual + CSV + AI Agents Input)

- **Date:** 2026-06-09
- **Status:** Approved (design, complete 4-ledger scope); pending spec review
- **Module:** `portfolio_dash/data_ingestion/` (+ a small `shared/llm.py` AI-agent base)
- **Depends on:** `shared/` (models, Decimal/money, enums, db, Settings). Per `architecture.md`,
  `data_ingestion/` is the **only writer of the ledgers**; it validates/normalizes input, never
  computes P&L (that is `portfolio/`).

## Context & purpose

`data_ingestion/` turns human + data-source input into the four canonical **source-of-truth
ledgers** — every report rebuilds from these (重算). It owns the `instruments` registry that
`pricing/` consumes as its work-list. Delivered **complete (all four ledgers)** with three input
modes: **manual entry**, **CSV import (preview → confirm)**, and **AI Agents Input** (natural
language → draft → confirm). Fee + tax auto-compute from per-account config rules (editable),
with a per-row snapshot so 重算 reproduces history.

## Decisions (settled 2026-06-09, human sign-off)

1. **Complete scope:** all four ledgers — `transactions`, `dividends`, `fx_conversions`,
   `opening_inventory` — plus the `instruments` registry.
2. **CSV: account, not market.** The account determines the market (TW broker→TW, Schwab→US,
   Moomoo MY-US→US, Moomoo MY-MY→MY); the resolved instrument confirms it. Transaction CSV:
   `account, symbol(or name), side(BUY/SELL), date, shares, price [, fee, tax, note]`.
3. **Fee and tax are separate columns**, both **auto-computed** from the account's config
   fee-rule-set when left blank, and **editable/overridable**.
4. **Symbol resolution:** ticker → use directly; company name → **fuzzy lookup** against
   `instruments` → on miss, **"AI Agents 查詢中"** LLM resolution → **user confirms** → write to
   `instruments` (cached for next time).
5. **Import = two-step:** parse → **preview list** (shows each row + the auto-computed fee/tax)
   → user reviews/edits → **"開始匯入"** writes. Nothing is written before confirmation.
6. **AI Agents Input** (the feature name): natural language → LLM **draft** (structured) →
   user confirm/edit → same validation + fee/tax calc → write. The LLM **only drafts, never
   writes**.
7. **Config LLM endpoint + agent registry** (extensible AI-agent framework): a shared LiteLLM
   client (provider/endpoint/key from config) + an agent registry so agents A–F plug in by
   phase, and **future agents extend without rewrites**.
8. **Unified principle (recorded):** the manual ledger is source of truth; data-source data is
   matched + offered for **confirmed** import; manual entry is always retained; `original_cost`
   never overwritten; no double-count (calc reads only the ledger).

## Data model / tables (per `data-and-pricing.md`; reuse `shared/models`)

`data_ingestion/` creates + owns these SQLite tables (Decimal as canonical TEXT):
- `instruments` — `symbol, market, quote_ccy, sector, name` (PK `symbol`). The registry
  `pricing/` reads for its work-list.
- `transactions` — `account_id, symbol, side, quantity, price, fees, tax, trade_date`, **+ a
  fee/tax-rule snapshot** (the rates applied) so 重算 reproduces history after rules change.
  Append-only in spirit; corrections are new rows or explicit edits.
- `dividends` — `account_id, symbol, date, type(cash/stock/DRIP), gross, withholding, net,
  reinvest_shares, reinvest_price`.
- `fx_conversions` — `account_id, date, from_ccy, from_amount, to_ccy, to_amount`.
- `opening_inventory` — `account_id, symbol, shares, original_avg_cost, original_cost_total,
  build_date`.
- `accounts` — first-class entity (broker, settlement/funding ccy, fee-rule-set ref, dividend
  model). Seeded from config (the four known accounts).

## Fee / tax engine (config-driven, snapshotted)

- Each account references a **fee-rule-set in config** (structure from `markets-and-fees.md`;
  default values provided; editable in the settings page). Rates are config, not code.
- On entry (manual/CSV/AI) with blank fee/tax → compute per the account's rules:
  - **TW:** brokerage 0.1425%×discount, **min NT$20**; sell tax **現股 0.3% / ETF 0.1%**,
    **當沖 0.15%** via an optional per-row flag (default 現股; ETF detected from the instrument);
    fee+tax **四捨五入 to integer NT$**.
  - **US (Schwab):** ~US$0 commission + tiny sell-side reg fee (config, may be ~0).
  - **Moomoo MY-US:** commission + platform fee + FX spread (config).
  - **MY (Moomoo MY-MY):** brokerage + clearing 0.03% (cap RM1,000) + stamp duty + SST (config).
- The applied rates are **snapshotted onto the row**. Buy-side fee+tax fold into cost basis
  (per `domain-ledger.md`); user can override any computed value before confirming.

## Symbol resolution

`resolve(account, raw) -> InstrumentRef | Candidates`:
1. If `raw` is a known ticker/symbol in `instruments` → use it.
2. Else **fuzzy match** name/symbol against `instruments` (and a small alias map) → if a
   confident match, propose it.
3. Else → **LLM resolution** ("AI Agents 查詢中"): the LLM proposes `(symbol, market, name)`
   given the raw text + account/market hint.
4. **User confirms** the chosen instrument → upsert into `instruments` (so it is cached).
Resolution never silently guesses; ambiguous/low-confidence → surfaced for confirmation.

## Input modes

- **Manual entry** — one record at a time, per ledger (transaction / dividend / fx-conversion /
  opening-inventory), through the validation + fee/tax pipeline.
- **CSV import** — bulk; transaction CSV per §Decisions 2; `opening_inventory` has its own CSV
  (`account, symbol, shares, original_avg_cost, build_date`); dividends/fx-conversions get their
  own CSV shapes too. Flow: parse → resolve symbols → compute fee/tax → **preview list** →
  user edits/confirms → write. Blank fee/tax auto-filled; bad rows flagged in the preview, not
  silently dropped.
- **AI Agents Input** — free text (e.g. "在元大買 10 股 2330 @ 600", or several lines) → the
  shared LLM client returns a **structured draft** (Pydantic) of one or more records →
  preview/confirm/edit → same validation + fee/tax → write. The LLM call is **batch/structured**,
  JSON-only, validated against the schema; on failure, retry once then fall back to manual.

## AI-agent framework (`shared/llm.py` + registry)

- **`shared/llm.py`** — a thin **LiteLLM** client: OpenAI-compatible call, provider/endpoint/key
  from `Settings` (swappable by config, no code change), a **structured-output helper** (prompt
  → validated Pydantic), prompt-versioning, and **graceful degradation** (endpoint down →
  feature unavailable, never crashes; per `llm-insight.md`). New dependency: `litellm`.
- **Agent registry** — agents are named units (prompt + input/output schema + the LLM client).
  **A. AI Agents Input** (this module) is the first. The registry + config endpoint are built so
  **B (statement/screenshot parsing), C (portfolio Q&A), D (corporate-action reconcile), E
  (data-quality), F (insight/strategy)** plug in at their phases (`data_ingestion` next: B/D;
  `llm_insight`/`strategy`: C/E/F) without reworking the base.

## Validation rules (loud, never silently coerce)

- **Sell qty > current holdings → block + require explicit confirmation** (input error vs short
  sale, per `domain-ledger.md`).
- Account must exist; symbol must resolve (or be confirmed); date valid; quantity/price positive;
  side ∈ {BUY, SELL}.
- Bad input is rejected loudly with the offending row/field; the preview surfaces issues.
- Edits/corrections are explicit (new rows or edited rows), never silent mutation;
  `original_cost` is never overwritten.

## Architecture / boundaries

- `data_ingestion/` imports only `shared/*` (incl. `shared/llm.py`); it is the **only writer of
  the ledgers + instruments**. It does **not** compute P&L (that is `portfolio/`), fetch prices
  (that is `pricing/`), or render UI (that is `web_ui/`).
- The future **confirmed auto-import** of FinMind dividends / Schwab transactions (Planned)
  consumes `pricing/`'s `dividend_events` + the unified-import principle, and lands here later.

## Error handling

- Validation failures → raise/flag loudly with context; nothing partially written (import is
  all-preview-then-commit).
- LLM (resolution / AI Agents Input) unavailable or invalid → no fabrication; fall back to manual
  entry; surface a clear message.

## Testing strategy

- **Pure unit tests:** fee/tax calc per account (fixed fixtures incl. TW min-fee/rounding, ETF
  tax, 當沖 flag, MY clearing cap); validation (sell>holdings block, bad rows); CSV parsing →
  records; symbol fuzzy-resolution.
- **Store/repository:** idempotent-ish writes, read-back, the rule snapshot persists.
- **LLM-backed (AI Agents Input + LLM resolution):** **mock the LLM** — test prompt assembly,
  JSON parsing/validation, retry-once, graceful degradation, and that a draft is never written
  without confirmation.
- No live network/LLM in the suite.

## Out of scope (deferred / other modules)

- P&L/returns compute (`portfolio/`), live price fetch (`pricing/`), UI rendering (`web_ui/`).
- **Confirmed auto-import** from FinMind/Schwab (Planned) — a `data_ingestion` follow-up that
  reuses this module's validation + fee/tax + confirm pipeline.
- Agents **C/E/F** (portfolio Q&A, data-quality, insight/strategy) — later phases; the framework
  is built now so they slot in.

## Designed-in flexibility (per human directive)

Config-driven fee rules + LLM endpoint, an agent registry, decoupled resolve/validate/persist
layers, and the four ledgers behind a uniform validation+confirm pipeline mean new input sources,
agents, accounts, and fee rules are **config + small additions, not rewrites**. Concrete specifics
(extra CSV shapes, more agents) deferred until real use (YAGNI on scale per `stack.md`).

## Staging (one spec; the plan will sequence)

1. `instruments` table + resolution (fuzzy) + accounts seeding.
2. `transactions` ledger: validation + fee/tax engine + manual + CSV (preview→confirm).
3. `opening_inventory` (manual + CSV).
4. `dividends` ledger (manual + CSV; per-account dividend models).
5. `fx_conversions` ledger (manual + CSV).
6. `shared/llm.py` (LiteLLM client + structured output) + agent registry.
7. **AI Agents Input** (NL → draft → confirm) + LLM symbol-resolution fallback.
