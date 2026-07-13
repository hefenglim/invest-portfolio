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
   **5b. What's-new catalog** — add/update this version's `shared/whatsnew.py` `CATALOG`
   entry (2-4 user-facing features, zh-TW, each with an accurate `href`/`area`) and its
   `VERSION_DATES` date so the ✦ 新功能 panel stays current. The catalog-integrity +
   CHANGELOG-drift unit tests (`tests/shared/test_whatsnew.py`) fail if a shipped version
   is missing or an `href` points at a non-existent page.
6. **Lessons** — update `LESSONS_LEARNED.md` if anything was learned the hard way.
7. **Self-review pass** — review the diff for: correctness; boundary adherence
   (`architecture.md` — calc stays in `portfolio/`/`forex/`, web layer thin);
   money discipline (`data-and-pricing.md` — Decimal, no float, correct precision);
   no double-counting of dividends or FX (`domain-ledger.md`).
8. **Bilingual protocol** — code/docs/commits/CHANGELOG in English; the summary to the
   human is in Traditional Chinese.

Report a concise pass/fail summary per item, then the version tag and one-line
description.
