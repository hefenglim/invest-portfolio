# portfolio-dash

Personal stock portfolio dashboard tracking cost basis, realized / unrealized P&L,
return rates, sector allocation, and FX gain/loss across **US, TW, and MY equities**
held in multiple broker accounts (TW broker · Charles Schwab · Moomoo MY) and
currencies (TWD / USD / MYR), for 1–2 users.

AI-implemented by Claude Code from human specifications (spec-first).

## Status

**v0.1.1 — first usable release** (see `CHANGELOG.md`). The calculation core (cost basis,
realized / unrealized P&L, returns / XIRR, sector allocation, FX attribution, per-account
dividend models), data ingestion, pricing + in-process scheduler, and LLM-insight are
implemented — served through a FastAPI JSON API + a static vanilla-JS dashboard, and
covered by unit + contract + Playwright E2E tests (incl. a spec-17 full-stack financial
regression). Self-hostable — see [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md). AI insights
are optional (off until an LLM is configured). Roadmap items are tracked in `CHANGELOG.md`
under `[Unreleased]` / Planned.

## Stack

Python 3.12 monolith — a **FastAPI JSON API** (`/api/*`) serving a **static vanilla-JS
frontend** (`web/`; no framework, no build step) with **ECharts** (CDN); **SQLite**;
`Decimal` money (never `float`); pandas / numpy + pyxirr (XIRR); **LiteLLM**;
**APScheduler**. Type-checked with mypy (strict); tested with pytest + httpx + Playwright.
(The web layer is JSON + static JS per decision (B); the earlier Jinja2 / HTMX / Alpine
plan was superseded — see `CHANGELOG.md`.) See `.claude/rules/stack.md`.

## Deployment

Self-host on a small VM (e.g. GCP `e2-micro` / Ubuntu): see
**[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)** — copy-paste setup + maintenance SOP.

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
2. Spec-first: the human provides the spec; Claude Code confirms understanding, then
   implements test-first (TDD).
3. Gate before delivery — ruff + `mypy --strict` + `pytest` (unit / contract) + Playwright
   E2E all green; `/ship-version` cuts the `CHANGELOG.md` version entry + an annotated tag.

## Privacy

This repository holds **code and conventions only**. The SQLite ledger (personal
financial data) and `.env` secrets are git-ignored — never commit them.
