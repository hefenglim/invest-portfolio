# Backend Hardening — Import Atomicity (#1) + pricing→data_ingestion Layering (#2) Plan

> REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps `- [ ]`. Isolated worktree off `main`.

**Goal:** Two verified known backend issues, fixed before frontend wiring:
1. **#1 — CSV/broker import batch is not atomic.** `data_ingestion/preview.py::commit_preview` loops accepted
   rows calling `writer(conn, row)`, and each writer (the `data_ingestion/store.py` inserts) calls
   `conn.commit()` per row. An UNEXPECTED writer exception mid-batch leaves rows 1..k committed and k+1..n
   unwritten — a partial import that breaks ledger reproducibility (CLAUDE.md 重算/append-only). Make a batch
   **all-or-nothing on unexpected error**. (Intentional skips of hard-issue rows — `has_hard_issue` — stay
   as designed: they are contract-level partial success, NOT an error.)
2. **#2 — `pricing/datasources_store.py` imports `data_ingestion.config_seed.DEFAULT_ACCOUNTS`** — a
   cross-peer layering violation (architecture.md: pricing and data_ingestion are sibling lower layers; pricing
   must not import data_ingestion). The file ALREADY has a local `_ACCOUNT_MARKET` (the 4 account_id→market
   map); use it as the account source and drop the import. No behavior change.

**Tech:** Python 3.12, sqlite3 + Pydantic, mypy --strict, ruff, pytest.
**Gates (repo `.venv`):** pytest · mypy --strict portfolio_dash · ruff check.
Baseline: **1001 passed / 4 skipped, mypy clean (136 files), ruff clean.** Green per task.

---

## Task 1: Atomic batch import (#1)
**Files:** `portfolio_dash/data_ingestion/preview.py` (+ whatever the import writers / store inserts need to
defer their commit); `portfolio_dash/api/routers/input_center.py` (the import-commit endpoint, if it commits);
tests `tests/data_ingestion/test_preview_atomicity.py` (new) + the existing import contract tests.

- [ ] **Step 1: Failing test.** Build a small `ImportPreview` of N valid rows; inject a `writer` that succeeds
  for the first rows then raises on row k (simulating an unexpected DB error). Call `commit_preview`. Assert:
  (a) it raises / surfaces the error, AND (b) **NONE** of the batch's rows persisted (count in the target
  table is unchanged — full rollback). Also a happy-path test: all accepted, non-hard rows → all persisted +
  one effective commit. And confirm the existing "hard-issue rows are skipped, the rest written" contract
  still holds (skips are not a rollback trigger).
- [ ] **Step 2:** run → FAIL (today a mid-batch raise leaves a partial write).
- [ ] **Step 3: Implement** all-or-nothing. Pick the cleanest sqlite3-correct approach and state it:
  - Preferred: `commit_preview` runs the writer loop inside one transaction and commits ONCE at the end;
    on any exception it `conn.rollback()` and re-raises. This requires the batch writers to NOT commit
    mid-loop — thread a `commit: bool = True` parameter through the store insert(s) the import writers use
    (manual/single-row callers keep `commit=True`, unchanged; the batch writer passes `commit=False`), or
    refactor the import writer to use a non-committing insert. Do NOT change the single-row manual path's
    behavior. Ensure `conn` isn't left mid-transaction on the happy path (final commit) or error (rollback).
  - Keep the `ImportSummary(written, skipped)` contract; hard-issue rows still go to `skipped` (no rollback).
- [ ] **Step 4:** run → PASS; full suite green (the existing import contract tests must stay green).
- [ ] **Step 5: Commit** `fix(data_ingestion): atomic batch import — all-or-nothing on unexpected error (#1)`.

## Task 2: Drop the pricing→data_ingestion import (#2)
**Files:** `portfolio_dash/pricing/datasources_store.py`; tests `tests/pricing/test_datasource_tiers.py` /
`test_defaults.py` (whichever covers seed/fallbacks) + a small layering assertion.

- [ ] **Step 1: Failing test.** A layering test asserting `portfolio_dash.pricing.datasources_store` (and the
  `pricing` package broadly) imports nothing from `portfolio_dash.data_ingestion` (AST/source scan, like the
  04 `test_layering.py`). Plus: `seed()` + `account_chains()` still produce the SAME per-account fallback
  chains as before for the 4 default accounts (regression guard).
- [ ] **Step 2:** run → FAIL (import still present).
- [ ] **Step 3: Implement.** Remove `from portfolio_dash.data_ingestion.config_seed import DEFAULT_ACCOUNTS`.
  In `seed()` and `account_chains()` iterate the local `_ACCOUNT_MARKET` (`for account_id, market in
  _ACCOUNT_MARKET.items()`) instead of `DEFAULT_ACCOUNTS` — `_ACCOUNT_MARKET` already enumerates the 4
  account ids with their market, so this is byte-equivalent. Confirm `_ACCOUNT_MARKET` covers every account
  that `DEFAULT_ACCOUNTS` did (it does: tw_broker/schwab/moomoo_my_us/moomoo_my_my); if any default account
  lacks a market entry, add it to `_ACCOUNT_MARKET` rather than re-importing.
- [ ] **Step 4:** run → PASS; full suite green; mypy/ruff clean.
- [ ] **Step 5: Commit** `refactor(pricing): drop data_ingestion import; use local account→market map (#2 layering)`.

---

## Self-Review
- #1: a mid-batch unexpected error rolls the whole batch back (no partial ledger write); intentional hard-issue
  skips + single-row manual path unchanged; `ImportSummary` contract intact. ✓
- #2: `pricing` no longer imports `data_ingestion` (layering test guards it); fallback-chain seeding behavior
  byte-identical. ✓
- No money math, no schema changes, no new endpoints. Decimal discipline untouched. ✓
- Deferred (NOT in this batch, reported to the user): calib_gap alert wiring (03↔04 integration, not a bug —
  compute_alerts_from is pure over DashboardData; calibration_regression already emitted via 04c alert_events),
  spec-07 `_last_batch` timestamp-correlation robustness, removed_recently v1-empty, spec-04 #5/#6 relative/vol
  narrative-only (all intentional v1 scope, documented).
