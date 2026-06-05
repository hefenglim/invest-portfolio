# portfolio-dash

Personal stock portfolio dashboard tracking cost basis, realized / unrealized P&L,
return rates, sector allocation, and FX gain/loss across **US, TW, and MY equities**
held in multiple broker accounts (TW broker · Charles Schwab · Moomoo MY) and
currencies (TWD / USD / MYR), for 1–2 users.

AI-implemented by Claude Code from human specifications (spec-first).

## Status

Pre-implementation. Conventions, rules, and the specification scaffold are in place;
no application code yet. Next step: spec the `portfolio/` cost-basis & return core,
then run the data-source availability probe.

## Stack

Python 3.12 monolith — FastAPI + Jinja2 + HTMX + Alpine.js + ECharts + SQLite +
LiteLLM + APScheduler. Type-checked with mypy (strict), tested with pytest.
See `.claude/rules/stack.md`.

## Project conventions (for Claude Code)

- `CLAUDE.md` — root index: locked decisions, module map, core invariants.
- `.claude/rules/` — topic rules, loaded on demand: `stack`, `architecture`,
  `domain-ledger`, `markets-and-fees`, `data-and-pricing`, `llm-insight`,
  `engineering-process`, `design-handoff`.
- `.claude/skills/` — workflow skills: `/resume-dev` (session start),
  `/ship-version` (pre-delivery checklist).
- `docs/` — historical / reference documents.

## Workflow

1. `/resume-dev` at session start.
2. Human provides the spec; Claude Code confirms understanding, then implements (TDD).
3. `/ship-version` before delivery (tests green, mypy clean, CHANGELOG updated).

## Privacy

This repository holds **code and conventions only**. The SQLite ledger (personal
financial data) and `.env` secrets are git-ignored — never commit them.
