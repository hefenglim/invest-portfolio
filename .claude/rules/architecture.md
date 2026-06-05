# Rule: Architecture & Module Boundaries

Monolith, but internally layered. Boundaries are enforced by discipline (and mypy),
not by separate processes.

## Dependency direction (one-way — never violate)

```
web_ui  ─┐
         ├─►  portfolio  ──►  pricing  ──►  shared
strategy ─┘        │           │             ▲
                   └─►  data_ingestion  ──────┘
llm_insight ──► portfolio (reads computed results) ──► shared
scheduler  ──► pricing, llm_insight  (triggers only)
```

- `shared/` depends on nothing internal. Everything may import it.
- Lower layers (`shared`, `pricing`, `data_ingestion`) **never import `web_ui`**.
- The web layer **reads** computed results. It does not compute. No cost-basis or
  return math in routes or templates.
- `llm_insight` consumes the portfolio's *computed* numbers; it does not recompute
  them and does not produce numbers of its own (see `data-and-pricing.md` and
  `llm-insight.md`).

## Module responsibilities

- **shared/** — settings (env-driven), DB session/connection, Pydantic models used
  across layers, `Decimal`/currency helpers, FX-conversion helper. Pure, importable
  everywhere.
- **data_ingestion/** — manual transaction entry + CSV/broker import. Validates and
  normalizes into the canonical transaction model before persisting. Rejects bad
  input loudly; never silently coerces.
- **pricing/** — fetch quotes + FX from finance APIs into SQLite via idempotent
  upserts. Owns the refresh cadence. The only module allowed to write price/FX rows.
- **portfolio/** — the calculation core: cost basis (configurable FIFO / weighted
  average per holding), realized & unrealized P&L, return rates, sector allocation,
  USD/TWD-normalized combined view. Pure functions over inputs where possible →
  trivially unit-testable.
- **strategy/** — user-defined strategy logic as parameterized Python modules
  (condition params → signal/score). Pure and pytest-tested. **Not** a user-facing
  rule builder (see below).
- **llm_insight/** — LiteLLM orchestration. Assembles portfolio summary + fetched
  qualitative info → structured insight cards (Pydantic schema) → cache → render.
- **web_ui/** — FastAPI routes + Jinja2 templates + HTMX/Alpine/ECharts. Thin: it
  orchestrates calls into lower layers and renders.
- **scheduler/** — APScheduler job definitions only. Triggers pricing refresh and
  scheduled insight runs. Holds no business logic itself.

## "Strategy logic self-defined" — current form

Implement strategies as **Python modules/functions** authored by Claude Code from
the human's spec — parameterized conditions returning signals or scores. Do **not**
build a user-facing rule-builder / DSL now. That is the smallest error surface and
the easiest to test with pytest. Upgrade to a config-file or DSL **only** if a
non-engineer user later needs to edit rules themselves — and record that decision
first.

## Testability is a design constraint

Because implementation is AI-driven, every module must be self-verifiable:
- `portfolio/` and `strategy/` → pure-function unit tests with fixed fixtures.
- `pricing/` and `llm_insight/` → mock the external API/LLM; test parsing,
  upsert idempotency, cache behavior, and graceful degradation.
- `web_ui/` → httpx route tests; assert on rendered HTML fragments for HTMX endpoints.
