---
name: ship-version
description: "Run the pre-delivery checklist and the staged deploy before shipping a version of portfolio-dash. Use this when a unit of work is complete and about to be delivered or committed as a version: it verifies tests, type-checking, CHANGELOG integrity (including the grep-c structural check), lessons captured, a self-review pass, and then deploys the tag to the demo site first and prod second, leaving both sites on the same tag. Invoke with /ship-version before declaring any version done."
---

# Ship Version

Two parts, in order: the **quality gates** (1–9) decide whether the version may exist, and
the **staged deploy** decides whether it reaches users. Do not declare a version done until
every item passes. Stop and fix on any failure.

## Part A — quality gates

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
   update `docs/accounting-formula-manual.md` (the formula + a verification anchor) and
   regenerate `docs/accounting-formula-manual.en.md` in the same change. See
   the `/stress-audit` skill.

## Part B — staged deploy (demo → prod → aligned)

The two sites are a **queue, not a fork**. The same tag reaches the demo site first, is
verified there, and only then reaches prod. **The version is not shipped until BOTH sites
run that tag** — ending a session with the two on different commits is an unfinished
release, not a deferred chore.

Concrete host paths / URLs / ports / systemd units / backup locations live in the
git-ignored `docs/human_noted/` note. **Never write real host details into this file.**

**Run every remote command through `scripts/vm_exec.py`** (`--action "why" --cmd "…"`, add
`--write` when it changes state). It executes via `gcloud` *and* appends the entry to the
append-only VM operation log in one step, so the deploy trail cannot drift from what actually
happened — which is exactly what it did before this was automated.

10. **Cut the version.** Merge to `main`, tag `vX.Y.Z`, push both. Prod and demo are
    deployed from the **tag**, never from a branch.
11. **Back up every DB you are about to touch — demo included.** Prod's ledger is
    irreplaceable; demo's synthetic ledger is the accumulating stress-test corpus, and
    re-seeding it silently destroys coverage that took releases to build. **Then open each
    backup and check it** (`PRAGMA integrity_check` + a row count) — a backup nobody has read
    is not a backup. The host has no `sqlite3` CLI; use the instance's own venv python.
12. **Deploy the tag to the DEMO site.** Then verify, in this order:
    - the service is `active` and `/api/health` reports the expected `version` **and**
      `release: vX.Y.Z` (a stale `release: unreleased` means the checkout is still on a branch).
      **Poll for health; never `sleep <n>` and read once** — boot time varies with the
      instance and the change (measured 2026-08-04: prod took **40 s**, and a fixed 28 s wait
      returned an empty body that looks exactly like a failed deploy). Loop up to ~2 minutes,
      and only then treat silence as a fault and read the journal;
    - `scripts/verify_live.py <demo-url> --expect-version X.Y.Z` → **ALL PASS**
      (pass the version **bare**, no `v` prefix);
    - the accumulated demo data survived — compare row counts before/after (transactions,
      cash movements, holdings). A migration must extend the schema, never reseed or drop;
    - any flow this version changed, clicked through in a real browser.
13. **Only if demo is completely clean, deploy the same tag to PROD.** Same verification,
    plus: state how many existing rows the migration actually touched (usually **0** — say
    the number, do not assume it), and confirm the auth gate still answers 401 on the
    protected routes.
14. **External reachability — from outside, for BOTH sites.** Each public URL must resolve
    to an A record **and** answer HTTP 200 over HTTPS from the dev machine.
    A green `curl 127.0.0.1:<port>/api/health` on the host proves the *app*; it proves
    nothing about the *site*. On 2026-07-29 the demo node's funnel DNS registration vanished
    from the control plane while the node was online, the key valid, and the service healthy —
    every page was `000` from outside and nothing on the box showed it. Re-register the funnel
    per the host note. **The release is not complete until this check passes for both sites.**
15. **Close the loop.** Both sites report the same `version` / `commit` / `release`. The
    per-command entries are already in the operation log (step 10's wrapper wrote them);
    append one release summary — tag, both backup filenames, both verifications, the
    reachability result.

## Part C — abort protocol: data first, diagnosis second

Any anomaly at any step **stops the sequence immediately**. Do not try the next step to see
if it clears, do not roll forward through it, do not retry the deploy hoping it was transient.

Priority order — this order is the point:

1. **User data, prod above all else.** Before diagnosing anything, establish that the ledger
   is intact and recoverable: is the service still writing? does the pre-deploy backup exist
   and open? do row counts / a reconciliation run match the pre-deploy snapshot? An unshipped
   version costs nothing; a corrupted ledger is permanent.
2. **If data is at risk, restoring it outranks understanding the bug.** Stop the service,
   restore from the backup taken at step 11, verify the restore, *then* investigate.
3. **Prod may always move backwards to a validated tag.** Re-pinning prod to the previous
   released tag is a safe action and never needs deliberation; leaving prod on an unverified
   state does.
4. **Only once the data is proven safe:** investigate — journal, service logs, the diff,
   reproduce it on the demo site (which is what the demo site exists for).
5. Fix on a branch and **re-run the whole checklist from item 1.** A partially deployed
   version is never "finished later".

Never: skip the demo stage because the change looks small; deploy prod while demo is red;
deploy prod without a fresh prod backup; leave the two sites on different tags.

Report a concise pass/fail summary per item, then the version tag, a one-line description,
and the final deploy state of **both** sites including the external-reachability check.
