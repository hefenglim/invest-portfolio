# Remediation Blueprint — QA R1 (Master Orchestrator, 2026-08-29)

Target: `/home/claude/work/invest-portfolio` @ git `HEAD` (baseline v0.1.28 + staged `.claude/rules`).
Input: `/home/claude/work/qa/QA_REPORT_R1.md` (QA Subagent). Every bug below was independently re-verified by the Master before inclusion; two QA verdicts were corrected during the sanity check.

## 0. Master sanity-check verdicts

| QA ID | Master verdict | Notes |
| --- | --- | --- |
| BUG-01 | CONFIRMED (P2, corpus infra) | `build_fx_preview` signature drift crashes `smoke_import.py`; corpus `fx.csv` hard-rejected by FU-D34 (corpus predates the ruling). → FIX-C2 |
| BUG-02 | CONFIRMED (P2, docs) | Manual §9.3 (en+zh) still specifies ack-able `negative_cash` on withdraw/fx.convert; code+tests implement hard FU-D43a/FU-D34 (owner-signed, anchored by the very tests §9.3 cites). Manual is the stale side. → FIX-C1 |
| BUG-03 | **RECLASSIFIED** | QA's root cause is WRONG: `build_transaction_preview` IS sibling-aware (Master probe T1: buy100+sell60 one file → both `ok`, zero flags; corpus F1 same-file cure already landed — 7 flags then, 4 now). The 4 remaining corpus prompts are the DOCUMENTED cross-file residue (README locked order; actions cannot precede the trades that legalise them; one-click path already passes `pending_actions`). **However the sanity check exposed a REAL adjacent defect (Master probe T2)**: `transactions` is missing from the QA-01 select re-derivation → committing with the covering buy deselected returns **200 / written=1 / shares −60** — a silently written, never-confirmed oversold state. → FIX-A1 (P2) |
| BUG-04 | CONFIRMED (P3), root cause sharpened | The 2026-08-14 fix widened the ActionIndex with the pending batch, but the four book-derived rejections (E3/E22/E5/E18) replay `bundle`/`book_cache` from the STORED ledger only → a chain whose prerequisite action is same-batch still hard-rejects on pass 1 (`oversold_source` on the GGR shape); byte-identical pass 2 writes it. Owner's stated root-cure in corpus README F4: batch-aware validation, "和 F1 是同一個修法". → FIX-A2 |
| BUG-05 | CONFIRMED (P3, two parts) | (a) `dividend_import.py:100-106` leaks `str(exc)` → `[<class 'decimal.ConversionSyntax'>]` and other CPython English into the user-facing 原因 column. (b) Manual §6.3: MY single-tier records the NET amount; the door still hard-requires `gross`, so the spec's own number-of-record cannot be imported alone. → FIX-A3 |
| BUG-06 | CONFIRMED (P3) | `cash.py:474` `flat.sort(key=(date, delta<0), reverse=True)`: Python's stable sort with `reverse=True` does NOT flip equal-key rows, so same-day same-sign rows stay oldest-first inside the newest-first table — contradicting the comment two lines above it ("same-day rows show end-of-day balance on top") and §9.2. QA API evidence + screenshot; Master read confirms the mechanism. → FIX-B1 |

QA Observations below bug threshold (FU-D34 message prints sibling-adjusted 可用餘額; `code: null` on warn-tier wire rows; zero-FX book renders total 0): **no action this round** (Zero-Divergence) — recorded for the final report.

## 1. Global constraints — every repair agent

1. Before editing, read: `CLAUDE.md`, `.claude/rules/domain-ledger.md`, `.claude/rules/architecture.md`, `.claude/rules/engineering-process.md` (§ relevant parts), and for FIX-A3 also `.claude/rules/data-and-pricing.md`. House invariants: money is Decimal strings on the wire; no business logic in routers beyond orchestration; comments in English carrying the decision-reasoning style already used (cite FU-D43a/FU-D34/QA-01/E1a etc. where apt); user-facing door messages are vetted Traditional-Chinese sentences.
2. **Precision targeting**: modify ONLY what the assigned fixes require. No refactors, no formatting sweeps, no dependency or version changes, no CHANGELOG edits (the Master writes the consolidated entry), no `git commit` (Master commits after review).
3. Every fix ships with regression tests in the existing test tree, following neighbouring tests' style. `ruff check` and `mypy` (strict) must pass on touched files.
4. Return a **Repair Execution Report**: per fix — files/functions modified, what changed and why (short), tests added (node ids), verification commands run + results, plus `git diff --stat`.

## 2. FIX-A1 (P2) — transactions door: QA-01 select re-derivation

**Files**: `portfolio_dash/api/routers/input_center.py` (`_resolve_builder`, `import_commit`, `_BATCH_GUARDED` docstrings), `portfolio_dash/data_ingestion/csv_import.py` (`build_transaction_preview` gains select-narrowing of its sibling `batch`).

**Defect (Master-proven)**: preview verdicts are derived against the WHOLE file's sibling batch; commit intersects `select` but re-derives only for `cash`/`fx`. Deselect a covering buy → the sell's clean verdict survives → written alone: 200, `written: 1`, holdings `shares: "-60"`, no 賣超 confirmation ever shown. The manual door answers the identical sell with the sticky needs_confirm.

**Required semantics** (mirror the QA-01 block in `import_commit`, extended to `transactions`):
- On commit with `select`, re-derive the transactions preview with the sibling batch narrowed to the rows that will actually be written (`accept`), keeping ALL rows in the preview output (index alignment + `row_hashes` must be untouched — mirror how `_cash_builder(select=…)` narrows the batch without dropping output rows).
- Shrink-only rule, adapted for soft issues: a row whose narrowed-batch validation surfaces an issue KIND that was NOT present on that row in the full-batch verdict must NOT be written under the earlier blanket `ack_warnings` (that specific confirmation was never shown). Drop it from `accept` so it lands in the summary's skipped count. A row whose issue kinds are unchanged (e.g. a genuinely-oversold sell the user acked) still writes — the ack was informed.
- `pending_actions_csv` widening must keep working and compose with the narrowing.
- Update the now-false docstring claims ("It is ignored by every other kind…", the QA-01 comment's "the two kinds").

**Tests** (place beside the existing QA-01 / import-commit tests; find them via `grep -rn "QA-01\|select" tests/`):
1. Full-file buy+sell commit → both written, zero flags (pins sibling-aware preview).
2. `select=[sell]` with covering buy deselected → sell NOT written as silent oversold; response reports it skipped; ledger has no unacked negative position.
3. `select` excluding an unrelated row → the sell still writes (narrowed batch keeps its cover).
4. Genuinely-oversold sell with `ack_warnings=True`, selected → still writes (ack honoured under narrowing).

**Acceptance**: Master's probe (`/home/claude/work/master_probe/probe_bug03.py` scenario) no longer produces the silent −60; full pytest green.

## 3. FIX-A2 (P3) — corporate actions: batch-aware book replay (completes corpus-F4's root cure)

**Files**: `portfolio_dash/data_ingestion/corporate_action_import.py` and/or `portfolio_dash/data_ingestion/validate.py` (`validate_corporate_action` + its `bundle`/`book_cache` plumbing). Investigate `data_ingestion/holdings.py` (`load_action_index`, book building) first; choose the minimal correct seam.

**Defect**: for action X in a batch, the four book-derived rejections replay the stored ledger only. A same-batch prerequisite action (the GGR shape: EXCHANGE `PPGH→GGR` then reverse SPLIT of `GGR`, trades already stored) is invisible → X rejects `oversold_source` on pass 1; identical pass 2 writes it. Two-pass is undocumented and the rejection message misdirects (「請先補登缺少的買進或期初庫存」 while the prerequisite is in the file).

**Required semantics**:
- When validating X, the replayed book at X's date must ALSO apply sibling batch actions sorting STRICTLY before X under the same cut the index already uses (`(action.date, EventPriority.CORPORATE_ACTION)`); a batch row never justifies itself; malformed rows exclude themselves (`convert_stored` failure → `unreadable`), exactly as the index does today.
- `test_a_lone_split_onto_an_empty_position_is_still_rejected` (named in the 2026-08-14 comment) must stay green, as must D15/E12 same-date-intersection behaviour.
- Mind `book_cache`: it is keyed per date and per call — ensure a cache entry cannot leak a book that ignores (or double-applies) pending siblings.
- Keep the rejection message unchanged — after this fix the same-batch case passes in one pass, and for a genuinely missing prerequisite the message is accurate.

**Tests**: GGR-shape regression (stored trades; one file with EXCHANGE + dependent reverse SPLIT) → single-pass, both written, zero rejected; a chain whose prerequisite action is absent entirely → still hard-rejected; existing corp-action suite untouched and green (`tests/data_ingestion/test_corporate_action_ledger.py`, `tests/contract/test_corporate_actions_api.py`, `tests/portfolio/test_corporate_actions.py`).

**Acceptance**: `sample-trade-data`'s real-history shape imports in one pass (Master re-verifies via the QA scripts); full pytest green.

## 4. FIX-A3 (P3) — dividend importer: vetted messages + §6.3 NET net-only

**Files**: `portfolio_dash/data_ingestion/dividend_import.py`; read `data_ingestion/dividend_model.py` + manual §6.2b/§6.3/§6.3b before touching anything; `data_ingestion/import_templates.py` if it declares the dividends template columns.

**Part (a) — message hygiene (defect class H-2)**: the parse block (`dividend_import.py:85-110`) answers `Issue(message=str(exc))`; blank `gross` renders `[<class 'decimal.ConversionSyntax'>]`, a bad date leaks CPython English, `unknown dividend type …` is English. Adopt the typed-cell-reader / vetted-sentence pattern already used by `csv_import.py` (`_CellError`, and its belt-and-braces zh fallback) so every parse failure yields a specific Traditional-Chinese sentence naming the column. No Python internals or English may reach `message`.

**Part (b) — spec conformance (manual §6.3)**: MY single-tier: "record the **net amount received**" — for `NET`, gross ≡ net by definition (the code itself notes "net would equal gross"). Change: a `NET` row may supply `net` only → derive `gross := net`, withholding 0. Constraints: applies to `NET` only (CASH/DRIP/STOCK column requirements unchanged); a `NET` row carrying a non-zero `withholding` is refused with a vetted sentence (single-tier has no withholding) unless `dividend_model` already defines stricter handling — align with the model, do not fork logic into the importer; `check_amounts` keeps guarding gross/net consistency when both are present.

**Tests**: the QA repro row (`moomoo_my,5225,2026-06-16,NET` with blank gross, net=120) now imports cleanly with gross=net=120 and correct ledger row; blank gross on a `CASH` row → vetted zh sentence, no internals; malformed date → vetted zh sentence; NET + nonzero withholding → vetted refusal (if that is the model-consistent behaviour). Extend `tests/data_ingestion/test_dividend_import*` style.

## 5. FIX-B1 (P3) — cash statement same-day ordering (separate agent, disjoint files)

**Files**: `portfolio_dash/api/routers/cash.py` (statement route only) + its tests. Frontend renders wire order — do NOT touch `web/cash.js`. `/api/export/cash-statement` (oldest-first CSV) must remain unchanged.

**Defect**: `flat.sort(key=lambda item: (item[1].date, item[1].delta < _ZERO), reverse=True)` — stable sort keeps equal-key rows in their original ascending order, so same-day same-sign rows display oldest-first inside the newest-first table; the day's end balance sits at the BOTTOM of that day's block, contradicting the comment above the sort and §9.2. Evidence: demo DB `schwab/USD` 2025-10-08 (`+13,529.30 → 33,565.52` shown above `+18.35 → 33,583.87`).

**Fix**: produce the exact reverse of the chronological display order — e.g. sort ascending by the existing chronological key (stable sort preserves `running_statement`'s `_ordered` sequence within ties) then `flat.reverse()`; or append a monotonic per-pool sequence number to the key. Preserve: each row keeps its own pool's running balance; pagination over `flat` (`offset`/`limit`); `balances`/`current_balance` computation; currencies still not interleaved into one running total. Update the comment so it describes what the code now actually does.

**Tests**: regression test (beside the existing statement tests — `grep -rn "statement" tests/api tests/contract` to locate) with ≥2 same-day same-sign rows in one pool asserting: first row of that day's block in the response carries the end-of-day balance; whole-table ordering is newest-first; CSV export ordering unchanged. Run the cash-related test files + `ruff`/`mypy` on touched files (Master runs the full suite afterwards).

## 6. FIX-C1 / FIX-C2 (docs & corpus) — dispatched AFTER Group A lands

**FIX-C1 (BUG-02)**: `docs/accounting-formula-manual.en.md` + zh mirror `accounting-formula-manual.md`, section §9.3 (+ §12.3 version history; check E16 wording stays accurate): rewrite the cash-gate guard paragraph to match the implemented, owner-signed rulings — withdraw = hard 422 `withdraw_insufficient_balance` (FU-D43a), fx.convert = hard 422 `fx_insufficient_balance` (FU-D34), NO ack, no financing, batch/sibling-aware, pre-existing-dip rule; `negative_cash` + `ack_negative` survives only on deposit/opening-side edits/deletes (date-aware `running_min`, all-affected-pools). Source of truth: `api/routers/cash.py` module header, `data_ingestion/fx_import.py` guard docstrings, and the §9.3-cited tests. Both language files change in lockstep, same content ("Mirror regenerated in the same change set" convention); add one §12.3 row in each (no formula change — guard-semantics documentation sync).

**FIX-C2 (BUG-01)**: make the demo corpus self-consistent with HEAD again: (1) `gen_demo_corpus.py` emits TWD funding deposits so every `fx.csv` conversion is covered under FU-D34 (QA-verified shape: 165,000 TWD before 2022-01-11 conversion, 198,000 TWD before 2025-03-19; derive amounts from the fx rows in the generator, don't hardcode magic constants); regenerate the corpus — `EXPECTED_positions.csv` must come out byte-identical (assert it), `EVENT_LOG.csv`/`import/cash.csv` gain the funding rows. (2) Fix `smoke_import.py` to the current `build_fx_preview(conn, text, pool=…)` signature (see how `input_center._fx_builder` wires the pool fn). (3) Run `smoke_import.py` + `verify_demo_corpus.py` end-to-end → all green; with FIX-A2 in place chains should commit in ONE pass — record what it prints. (4) Update corpus `README.md`: row counts, the locked-order results table (賣超提示 7 → the current measured count; note the same-file accumulation cure landed and the residue is the documented cross-file case with `pending_actions` on the one-click path), F1 section → fixed w/ residual note, F4 section → root cure landed (one-pass), refresh the "四道驗證全綠" claim to what is now true.

## 6b. FIX-A1b (P2, Round 2) — structurally-invalid rows must not cover siblings (Master finding during Stage-4 verification)

Agent A's residual observation, upgraded to a verified defect by Master probes V6/V7:
- **V6 (transactions, current HEAD)**: `buy 100 @ −50.00` (hard `non_positive_price`) + `sell 60` in one file → preview: buy `error`, sell `ok` (the un-writable buy still counted in the sibling batch) → commit with NO select → **200 / written 1 / ledger = lone SELL 60** — the silent unacked oversold state again, reachable by a mere typo.
- **V7 (cash, same shape)**: deposit `−500` (hard) + withdrawal `300` → the cash builder already EXCLUDES the structurally-bad row from its sibling pool, so the withdrawal is hard-blocked at preview and nothing writes. The QA-01 comment's premise ("a structurally invalid row was already out of the batch") is TRUE for cash/fx and FALSE for transactions.

**Fix direction**: in `build_transaction_preview`, exclude from the sibling `batch` any row whose ROW-LEVEL (ledger-independent) structural validation fails (the `non_positive_quantity` / `non_positive_price` / `negative_fee` / `negative_tax` / `amount_too_large` family) — mirror `cash_import`'s boundary exactly (find its mechanism first and cite it). A row that can never be written can never cover. Exclusion only shrinks availability, and share-side findings are soft (`needs_confirm`), so no hard-issue cascade; the select-narrowing from FIX-A1 composes on top. Regression tests: the V6 shape with no select (sell must surface 賣超 at preview and not write silently), with select, and cash-parity pin. Full suite green.

## 7. Sequencing & verification plan (Stage 4)

1. Group A (FIX-A1/A2/A3) and Group B (FIX-B1) in parallel — disjoint files.
2. Master review + full pytest + mypy/ruff; targeted re-probes (T1/T2, GGR one-pass, statement ordering, dividend rows).
3. Group C after A is green (C2 depends on A2 behaviour).
4. QA Subagent re-awakened for focused regression on the repaired modules + confirmation sweep.
5. Convergence declared only at: all fixes verified, full suite green, zero new findings.
