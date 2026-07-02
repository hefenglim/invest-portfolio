# CLAUDE.md

**Project:** Personal Stock Portfolio Dashboard (working name: `portfolio-dash`)

**Purpose:** Track cost basis, realized / unrealized P&L, return rates, sector
allocation, and FX gain/loss across **US, TW, and MY equities** held in **multiple
broker accounts** (TW broker · Charles Schwab · Moomoo MY) and **multiple currencies**
(TWD / USD / MYR), for **1–2 users**. A visual dashboard surfaces the metrics; the
human enters transactions, dividends, and currency conversions; the system fetches
market quotes and qualitative market/sector/news information; an LLM endpoint
synthesizes **batch-generated insight cards / reports** rendered into the dashboard.

**Scale assumptions (these justify the stack — do not over-engineer past them):**
1–2 users, < 200 transactions/month (< ~2,400 rows/year). Single instance.

---

## Locked decisions

These are settled. Do **not** relitigate without explicit human sign-off recorded
in `CHANGELOG.md`.

- **Python 3.12 backend monolith.** All business logic, calculation, and persistence
  in one Python package. No SPA framework, no build step.
- **Web layer (decision (B), 2026-06-13 — supersedes the prior Jinja2+HTMX rule;
  see CHANGELOG):** a **FastAPI JSON API** (`portfolio_dash/api/*`, all routes under
  `/api/*`) serving a **static vanilla-JS frontend** (`web/`, served via `StaticFiles`)
  + **ECharts** (CDN). Vanilla JS only — **no framework, no bundler, no build step**.
  The frontend never computes money or returns; all numbers come from the API as
  Decimal **strings** (`web/api.js` is the single fetch layer). `mock-data.js` is the
  documented JSON contract; spec-17 golden-payload + spec-18 round-trip tests guard it.
- **Storage:** **SQLite**. DuckDB is deferred — add it *only* if analytical query
  volume later justifies a second engine. Do not add it pre-emptively.
- **Financial math:** pandas / numpy / numpy-financial. **Money is never `float`** —
  use `Decimal` (or scaled integers). See `rules/data-and-pricing.md`.
- **LLM access:** **LiteLLM**, OpenAI-compatible interface. Providers
  (OpenRouter / OpenAI-compatible / Anthropic) are swappable by config, not code.
- **Scheduling:** APScheduler, in-process.
- **Type safety:** full type hints + Pydantic models + **mypy (strict)**.
- **Tests:** pytest + httpx.

## Core invariants (violating any of these is a bug, not a choice)

1. **Quote numbers come from finance APIs, never from an LLM.** The LLM handles
   qualitative synthesis only (news, sector context, narrative insight).
2. **LLM insight generation is batch only** — manual trigger or scheduler.
   Never called synchronously on dashboard page load. Results are cached.
3. **No money in floats.** `Decimal` everywhere; store at full source precision,
   quantize only at settlement/display. See `rules/data-and-pricing.md`.
4. **Module dependency direction is one-way** (see `rules/architecture.md`).
   Lower layers never import the web layer.
5. **`account` is a first-class entity.** Fee/tax/dividend rules bind to the account,
   not the market (the US market spans Schwab and Moomoo with different rules).
6. **No double counting.** Dividends enter total return exactly once (P&L uses
   original cost); FX gain/loss is an attribution breakdown of the reporting-currency
   XIRR, never added on top. See `rules/domain-ledger.md`.
7. **Original cost is never overwritten;** all reports rebuild from the ledgers.

---

## Module map

```
portfolio_dash/
  shared/        # config, db session, Pydantic models, currency/Decimal + FX helpers
  data_ingestion/# transaction/dividend/FX-conversion entry + import, validation
  pricing/       # market quotes + FX -> SQLite, scheduled refresh, idempotent upserts
  portfolio/     # CORE calc: cost basis, realized/unrealized P&L, returns, sector mix
  forex/         # currency-exchange ledger, FX cost basis, realized/unrealized FX P&L
  strategy/      # user-defined strategy logic as Python modules (parameterized)
  llm_insight/   # LiteLLM orchestration: portfolio + news -> structured cards (cached)
  api/           # FastAPI JSON API: routers (call core + serialize), no business logic
  web/           # static vanilla-JS frontend (HTML/CSS/JS + ECharts CDN); served by api/
  scheduler/     # APScheduler jobs (pricing refresh, scheduled insight runs)
```

Markets: **TW / US / MY**. Accounts: **TW broker · Charles Schwab (US) · Moomoo MY
(US) · Moomoo MY (MY)**. Calculation lives in `portfolio/` and `forex/`, never in
templates or routes. The web layer reads computed results; it does not compute.

---

## Rules — load the relevant file on demand, not all at once

| File | When to read |
| --- | --- |
| `.claude/rules/stack.md` | Choosing a library, or tempted to add/replace a tool |
| `.claude/rules/architecture.md` | Adding a module, route, or cross-module call |
| `.claude/rules/domain-ledger.md` | **Any calculation:** accounts, cost basis, dividends, P&L, returns, XIRR, FX P&L |
| `.claude/rules/markets-and-fees.md` | Market rules (tick/lot) or per-account fee/tax sets |
| `.claude/rules/data-and-pricing.md` | DB schema, quotes, FX, Decimal/precision handling |
| `.claude/rules/llm-insight.md` | Anything touching the LLM / LiteLLM / prompts |
| `.claude/rules/design-handoff.md` | Dashboard visuals or a Claude Design → Claude Code handoff |
| `.claude/rules/engineering-process.md` | Before committing, shipping, or editing CHANGELOG |

Do not load large reference files (specs, datasets, generated reports) in full —
read bounded sections only.

---

## Dev workflow

- **`/resume-dev`** at session start (`.claude/skills/resume-dev/`): read this file +
  `CHANGELOG.md` head + the relevant rule file(s). Do not re-read the whole repo.
- **`/ship-version`** before delivery (`.claude/skills/ship-version/`): tests green,
  mypy clean, CHANGELOG entry verified, lessons captured, self-review pass.
- **Spec-first.** The human provides requirements, plan, and spec; Claude Code
  confirms understanding, then implements. Implementation does not begin before
  the spec is acknowledged.
- **Loop-engineering (two environments).** The live deployment runs an isolated **test/demo**
  instance (its own checkout + venv + synthetic data) alongside **prod** (released tag, real
  data). Iterate and run the gate on the TEST site; promote to prod only when green — prod never
  runs untested code. Full flow + invariants: `.claude/rules/engineering-process.md` →
  "Two-environment loop-engineering". Concrete host settings (paths / URLs / ports / units) are
  in the git-ignored `docs/human_noted/` deployment note (never commit real host details).

## Repository layout (non-app files)

`README.md` · `CHANGELOG.md` (Keep-a-Changelog; `grep -c "^## \[v"` integrity check) ·
`LESSONS_LEARNED.md` (PEM) · `.gitignore` · `docs/` (historical/reference) ·
`.claude/rules/` (topic rules) · `.claude/skills/` (workflow skills). The SQLite
ledger and `.env` are git-ignored — personal financial data is never committed.

## Bilingual protocol

All repository artifacts — code, docstrings, comments, commit messages,
CHANGELOG, these rule files — are in **English**. Conversational summaries to the
human are in **Traditional Chinese**.

---

_Project initialized 2026-06-05._
