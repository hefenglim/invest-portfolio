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

### Planned
- Data-source availability probe: US / TW / MY quotes; USD/TWD, USD/MYR, MYR/TWD FX;
  ex-dividend calendar.

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
