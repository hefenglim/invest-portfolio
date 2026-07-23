# Rule: Technology Stack

The stack is locked. This file exists so the stack is **not** relitigated every
session. If a change seems warranted, propose it to the human and record the
decision in `CHANGELOG.md` before acting.

## The stack

| Concern | Choice | One-line rationale |
| --- | --- | --- |
| Language | Python 3.12 | Most "native" language for LLM code-gen; one language = smallest error surface |
| Web framework | FastAPI | Async, Pydantic-native, minimal boilerplate |
| Frontend (decision (B), 2026-06-13) | Static vanilla JS + ECharts (CDN) | No framework, no bundler, no build step; served by FastAPI `StaticFiles`. Supersedes Jinja2/HTMX/Alpine — see CHANGELOG. |
| Front/back contract | JSON over `/api/*` | Money as Decimal **strings**; frontend never computes; `web/api.js` single fetch layer; **spec-17 golden payload** (`tests/golden/dashboard_full.json`) = documented contract (`mock-data.js` retired under spec-19 §6; `test_web_pdapi_only` asserts it stays deleted) |
| Charts | ECharts (CDN) | Visual quality lives in the chart lib + CSS, not in a JS framework |
| Storage | SQLite | Tiny data volume; zero-ops; one file |
| DataFrames / math | pandas, numpy | Idiomatic financial computation |
| Returns (IRR/XIRR) | numpy-financial (+ XIRR helper) | Periodic IRR built-in; irregular cashflows need XIRR |
| Money type | `decimal.Decimal` | Never float for currency |
| LLM gateway | LiteLLM | One OpenAI-format call across all providers |
| Scheduling | APScheduler | In-process; no extra service |
| Validation / models | Pydantic v2 | Shared between API, DB layer, and LLM I/O |
| Type checking | mypy (strict) | Compile-time guardrail for an AI-implemented codebase |
| Tests | pytest + httpx + Playwright | Unit + FastAPI TestClient contract tests (JSON shape) + Playwright E2E; golden-payload regression; pytest-socket network ban |
| Container | single Docker image | Small footprint; runs on 1 GB VM or a NAS |

## Why NOT (settled trade-offs)

- **Not React / Next.js / any SPA.** The dashboard is read-heavy with periodic
  refresh, form input, and batch-generated insight cards — not a real-time trading
  terminal. "Visually rich" comes from ECharts + CSS, which need no JS framework.
  Re-open this only if the LLM feature becomes a **streaming chat as the primary
  surface** (it is not — insights are batch, manual or scheduled).
- **Not DuckDB (yet).** SQLite covers the transactional data at this volume. Adding
  DuckDB now is a spare part, not a feature. Revisit only on real analytical load.
- **Not a separate task queue (Celery/Redis).** APScheduler in-process is enough for
  1–2 users. No broker, no extra container.
- **Not Postgres.** Single instance, tiny data, single writer. SQLite wins on ops.

## Adding a dependency

Default answer is no. Before adding any library, confirm it cannot be done with the
stack above. If it is added, pin the version and note why in `CHANGELOG.md`.
