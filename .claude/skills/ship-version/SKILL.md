---
name: ship-version
description: "Run the pre-delivery checklist before shipping a version of portfolio-dash. Use this when a unit of work is complete and about to be delivered or committed as a version: it verifies tests, type-checking, CHANGELOG integrity (including the grep-c structural check), lessons captured, and a self-review pass. Invoke with /ship-version before declaring any version done."
---

# Ship Version

Do not declare a version done until every item passes. Stop and fix on any failure.

1. **Tests green** — run `pytest`; all pass.
2. **Types clean** — run `mypy --strict`; zero errors. Treat a type error as a build
   failure.
3. **Coverage** — new/changed behavior is covered by tests (pure calc in
   `portfolio/` and `forex/` has fixed-fixture unit tests; routes tested via httpx
   with HTML-fragment assertions for HTMX endpoints).
4. **CHANGELOG** — add/extend the entry for this version. Then verify structure:
   `grep -c "^## \[v" CHANGELOG.md` must equal the number of released version
   headings. Prefer a bounded-section rewrite over surgical edits. The version date
   is the **real delivery date**.
5. **Asset-version stamp** — after bumping `portfolio_dash/__init__.py.__version__`,
   run `.venv/Scripts/python scripts/stamp_asset_version.py` so every `web/*.html`
   local script/css tag carries `?v=<new version>` (stale-cache flush; the contract
   test `tests/contract/test_static_cache_discipline.py` fails if skipped).
   **5b. What's-new catalog** — every user-facing feature/adjustment in this version MUST get
   a `shared/whatsnew.py` `CATALOG` entry (zh-TW, phrased for the end user) with an accurate
   `area` AND both an `href` and a `target` (a stable in-page selector you verified exists), so
   「前往」 jumps to the right page and the arrival flash lands on the exact spot it changed.
   Also set its `VERSION_DATES` date. Both the ✦ 新功能 panel and the 版本發佈資訊 history
   browser then stay current. The catalog-integrity + bidirectional CHANGELOG-drift unit tests
   (`tests/shared/test_whatsnew.py`) fail if any shipped version (≥ v0.1.0, ≤ current) lacks a
   catalog entry, if its `VERSION_DATES` date is missing, if an `href` points at a non-existent
   page, or if any `href` lacks a `target` — so a shipped version MUST get an entry.
6. **Lessons** — update `LESSONS_LEARNED.md` if anything was learned the hard way.
7. **Self-review pass** — review the diff for: correctness; boundary adherence
   (`architecture.md` — calc stays in `portfolio/`/`forex/`, web layer thin);
   money discipline (`data-and-pricing.md` — Decimal, no float, correct precision);
   no double-counting of dividends or FX (`domain-ledger.md`).
8. **Bilingual protocol** — code/docs/commits/CHANGELOG in English; the summary to the
   human is in Traditional Chinese.
9. **Money-of-record stress audit** — if this version changes ANY money-of-record
   calculation (cost basis, realized/unrealized P&L, fee/tax, dividends, FX pool,
   returns/XIRR): extend `scripts/stress_audit` (the independent oracle in `oracle.py`
   **and** a `run_phase1.py` scenario op exercising the change), re-run phase 1 green
   (`.venv/Scripts/python scripts/stress_audit/run_all.py --phase 1` → `fail=0`), and
   update `docs/accounting-formula-manual.md` (the formula + a verification anchor). See
   the `/stress-audit` skill.

Report a concise pass/fail summary per item, then the version tag and one-line
description.
