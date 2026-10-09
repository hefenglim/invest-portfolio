# QA Report R1 — Stock Position & Cost Management System (`portfolio_dash`)

- **Target**: working copy `/home/claude/work/invest-portfolio`, git baseline `402271e` (v0.1.28), served by `portfolio_dash.api.app:create_app` at `http://127.0.0.1:8300`.
- **Auditor**: QA Subagent (independent recomputation + Playwright Chromium E2E + scenario injection). All auditor code under `/home/claude/work/qa/audit/`; screenshots under `/home/claude/work/qa/evidence/`.
- **Authoritative contracts used**: `docs/accounting-formula-manual.en.md` (sections 1.3, 3.1–3.6, 4.1–4.4, 5.1–5.3, 6.1–6.5, 7.1–7.2, 8.1–8.4, 9.1–9.3, 10.2), `docs/reference/broker-fee-schedules-2026-07.md` (via manual §3 worked examples), corpus ground truths.
- **Pristine DB untouched**: `/home/claude/work/qa/portfolio.db` sha256 `cd3bf00735429ced115965fec3f0db54ce5837414e9475790c74018c80d063bd` at start == at end. All writes went to scratch copies (`scratch_demo.db`, `scratch_fees.db`, `scratch_xirr.db`, `scratch_schwab.db`).
- **Important environment fact**: the supplied "real-ledger" DB snapshot contains **zero ledger rows** (only 3 accounts + 4 account_market_rules + settings). Real-data reconciliation was therefore performed against `sample-trade-data/charles-schwab/` (real 1,214-row broker history + independently built ground truth) instead of the user's live ledger.

## 1. Datasets and pages covered

| Dataset | DB | What ran on it |
| --- | --- | --- |
| demo-corpus (16 fictional tickers, 167 events, EXPECTED_positions.csv) | `scratch_demo.db` | full HTTP import → EXPECTED comparison → independent oracle → cross-endpoint checks → Playwright page E2E |
| manual-entry scenario battery (TW/US/MY, 10 tickers) | `scratch_fees.db` | 16 fee-engine worked examples, §4.2 replay, §4.3 short sale, §5.3 oversell+sticky, §6.2b/6.3b dividends, §9 cash guards, §10.2 edit-recompute, batch undo, UI manual-entry flow |
| closed-form XIRR probe | `scratch_xirr.db` | buy −10,000, +10% after exactly 365 d → XIRR; perturbation to +15.5% |
| real Schwab history (TABLE_A, 1,214 rows → 662 equity/cash app rows) | `scratch_schwab.db` | generated app CSVs → HTTP import → oracle → TABLE_B / TABLE_A reconciliation → tax-package split |

Pages exercised with Playwright (Chromium headless, console + network capture, screenshots): `index.html` (dashboard + KPI band + holdings + FX card + realized card + `#sym=` detail drawer), `trades.html` (ledger + the real manual-entry form with clicks/fills/commit), `cash.html` (pools, statement after pool click, reporting total), `instruments.html`, `data-center.html`, `settings-accounts.html` (fee rules). Console-only sweep: `insights.html`, `news.html`, `dividend-inbox.html`, `pipeline-hub.html`, `settings-datasources/llm/scheduler/prompts`. (`input.html` and `ledger.html` are redirect stubs to `trades.html` — verified.)

## 2. Coverage matrix (domain × lens)

| Domain | A. Independent recompute | B. UI E2E (Playwright) | C. Scenario injection |
| --- | --- | --- | --- |
| 1. Cost & positions | `oracle.py`: WAC replay incl. splits/exchanges/spinoff/shorts, per-holding shares & totals vs API on 3 DBs — 0 mismatches (demo 99 checks, fees 41, schwab 112) | holdings table, detail drawer, per-cell values (65 sh / 53.21 avg / 3,900.00 MV) verified against API | §4.2 worked example digit-exact (14,850.9090…); §4.3 short (−199,115, +7,115 unreal); demo EXPECTED_positions all match; Schwab: 47 per-ticker share counts vs TABLE_B all match (incl. 3-dp fractions) |
| 2. PnL | realized rows (count/per-symbol/total), unrealized per holding recomputed from raw SQLite — 0 mismatches | KPI band 已實現/未實現 rendering = API values; realized card | partial sells, sell-all-then-rebuy restart, short covers (1,701.999…996 28-digit tail exact), post-close dividend → `kind="dividend"` row; Schwab closed groups vs TABLE_B project-convention: Σdiff −0.0029 over the whole file |
| 3. Cash reconciliation | per-(account,ccy) pools recomputed from all 4 contributing ledgers — 0 mismatches on 3 DBs; statement running balance replay | cash cards, reporting total, statement rows incl. running balance after UI commit | deposits/withdraw/fx via UI+API; guards: withdraw hard-block, backdated block, fx hard-block, joint-overdraft REJECT file blocked with solo-row control passing; batch undo exact revert; Schwab USD pool vs TABLE_A net cash reconciled (diff = documented DRIP-rounding mapping artifact 9.29) |
| 4. Cross-currency / FX | fx pools: avg acquisition rate, covered_ratio, basis gap, unrealized fx stocks/cash, realized fx — recomputed, 0 mismatches; wire float sweep: **0 float leaves** across 14 endpoints | FX card values (29.37 → 32.50, +3.13, exposure amounts) = API | TWD/USD/MYR mixed book; unbased deposit → covered_ratio 0.9129… & `fx_basis_incomplete`; Moomoo US MY-stamp (fx 4.3) exact; TWD-funded conversion round trip |
| 5. XIRR / performance | own bisection solver over §7.2 flow rules + trade-date FX lookups vs `kpis.xirr` — agrees ≤1e-6 (demo: 0.0631849…) | KPI +6.32% / +10.00% rendering | closed-form: exactly 0.100000…/365 d, monotone to 0.155 after +5.5% price bump; simple USD rate exactly 0.10; oversold book → XIRR blanked with named reason; missing FX → named reason; TWR endpoint degrades honestly (no benchmark history in sandbox) |

Fee engine (manual §3.1–3.4, all 16 worked examples through the real preview door): TW floor+min20+ETF/daytrade tax ladder, Schwab SEC+TAF (incl. TAF cap 9.79), Moomoo US (incl. settlement cap + MY stamp in USD), MY comm/clearing/platform/SST + stamp (ETF exempt) + sub-RM1 3-dp price — **16/16 exact**, incl. §3.6 rebate forecast 658 and `fee_rule_snapshot` `engine=v2` persistence, and §10.2 edit-recompute (fee 20 → 997 on edit).

## 3. Bug tracking table

| ID | Sev | Module | Title | Expected (formula / citation) | Actual (source) | Repro |
| --- | --- | --- | --- | --- | --- | --- |
| BUG-01 | P2 | test assets / data_ingestion.fx_import | Demo-corpus ground truth is no longer importable by its own locked procedure; its own smoke tool crashes | README (`sample-trade-data/simulated/demo-corpus/README.md`) documents an all-green import incl. `fx.csv`; `smoke_import.py` must run | `fx.csv` both rows hard-rejected `fx_insufficient_balance` (no ack exists on this door, FU-D34); `smoke_import.py` crashes `TypeError: build_fx_preview() missing 1 required keyword-only argument: 'pool'` | fresh scratch DB → follow README order via `/api/import/commit`; or run `sample-trade-data/simulated/demo-corpus/smoke_import.py` |
| BUG-02 | P2 | docs (accounting manual §9.3) | Manual still specifies ack-able `negative_cash` for withdraw + fx.convert; the app implements hard, un-ackable FU-D43a/FU-D34 | §9.3: "Hard guard on cash gates (deposit/withdraw, fx.convert): … if `running_min < 0` and not `ack_negative` → 422 `negative_cash`" | app: 422 `withdraw_insufficient_balance` / `fx_insufficient_balance`, "NO ack override, no financing" (`api/routers/cash.py:6-9`, `fx_import.py:151-153`); strings `FU-D34`/`FU-D43` appear nowhere in either manual | POST `/api/cash/movements` withdraw over balance; POST `/api/cash/fx` from an unfunded pool |
| BUG-03 | P2 | data_ingestion/csv_import | Bulk transaction import still raises spurious STICKY-oversell confirmations for sells covered by buys earlier in the same file (corpus finding F1, still live) | cash/fx previews are sibling-aware ("The whole file is one batch", `cash_import.py`); a covered sell must not demand the one ack that permanently discards a cost basis (`domain-ledger`: 賣超 is STICKY) | 4 `sell_exceeds_holdings` needs_confirm rows (BRGN×2, NVSA, VRTA) on the corpus file, each covered by a same-file buy; `_BATCH_GUARDED = {"cash","fx"}` only (`input_center.py:978`) | fresh DB + instruments + cash/openings, then `build_transaction_preview` (or `/api/import/preview`) on corpus `transactions.csv` — see `qa/audit` F1 probe in §4 |
| BUG-04 | P3 | data_ingestion/corporate_action_import | Chained corporate action whose prerequisite is in the same batch hard-rejects on pass 1; rejection message misdirects; second identical upload (undocumented) is the actual fix | §4.4/E1a family; commit now reports `rejected_rows` (good) but message says 「請先補登缺少的買進或期初庫存」 while the data is already present in the batch | real-history import: GGR reverse split rejected `oversold_source` on pass 1; re-posting the same file wrote it (7 duplicates skipped, 1 written) | import `qa/audit/schwab_import/corporate_actions.csv` after transactions on `scratch_schwab.db` |
| BUG-05 | P3 | data_ingestion/dividend_import | User-facing parse error leaks raw Python internals: blank/malformed `gross` renders `[<class 'decimal.ConversionSyntax'>]` in the 原因 column | door messages are vetted zh sentences (H-2 posture, cf. `fx_import.py::_CellError`); §6.3 MY `NET`: "record the net amount received" (gross is not the number of record) | `/api/import/commit` kind=dividends row `moomoo_my,5225,2026-06-16,NET,,,120,,` → `rejected_rows: [{kind: parse_error, message: "[<class 'decimal.ConversionSyntax'>]"}]` (`dividend_import.py:100-106`) | one-row CSV as above on any scratch DB |
| BUG-06 | P3 | api/routers/cash.py (statement) + web/cash.js | Same-day same-sign statement rows are displayed oldest-first inside the newest-first table, so the day's end balance is NOT on top — contradicting the endpoint's own documented display rule | `cash.py:467-470`: "same-day rows show end-of-day balance on top"; §9.2 same-day ordering | 2025-10-08 (schwab/USD, demo DB): displayed top→bottom `+13,529.30 → 33,565.52` then `+18.35 → 33,583.87` (end-of-day 33,583.87 at the bottom). Root cause: `flat.sort(key=…, reverse=True)` at `cash.py:474` — Python's stable sort with `reverse=True` preserves (does not flip) equal-key order | GET `/api/cash/statement?account=schwab&ccy=USD` on `scratch_demo.db`; UI: `qa/evidence/cash_stmt_sameday_order.png` |

No P0 or P1 findings: across three datasets (including 1,214 rows of real broker history) every recomputed money figure — cost basis, realized/unrealized P&L, cash pools, FX pools, KPI rollups, XIRR — matched the API and the external ground truths within documented rounding.

## 4. Detailed findings

### BUG-01 (P2) — demo corpus broken by FU-D34; its verification tool crashes
- **Evidence**: import run log in this audit (fx step): `rejected_rows: fx_insufficient_balance — 換出金額 165000 TWD 超過 Charles Schwab 的 TWD 可用餘額 -198000 …`; `smoke_import.py` crash reproduced (`build_fx_preview()` now requires `pool`).
- **Analysis**: the corpus models TWD→USD conversions with no TWD cash movement (funding predates tracking). FU-D34 (hard, batch-aware, no-ack) post-dates the corpus (README verified 2026-08-12). The corpus is the project's own acceptance asset — "四道驗證全綠" is now false on HEAD, and the README's locked import order silently fails at step 3. Note also the guard's message prints "可用餘額 −198,000": with batch siblings deducted the figure is "pool minus your other rows", not the account's balance — worth a wording tweak while fixing.
- **Suspected files**: `sample-trade-data/simulated/demo-corpus/smoke_import.py:110` (fx builder call), `gen_demo_corpus.py` (needs TWD funding rows or fx removal + EXPECTED/README regen), `data_ingestion/fx_import.py::fx_balance_issues` (message wording).
- **Repair suggestion**: regenerate the corpus with TWD `DEPOSIT` rows funding each conversion (this audit proved that fixture change makes the whole corpus green again — every EXPECTED number still matches); update `smoke_import.py` to the new `build_fx_preview(conn, text, pool=…)` signature; re-verify and refresh README claims.
- **QA workaround used**: two TWD deposits (165,000 @ 2022-01-11; 198,000 @ 2025-03-19) — leaves every EXPECTED figure intact (TWD pool starts and ends at 0; verified).

### BUG-02 (P2) — manual §9.3 vs implemented cash/fx guards
- **Evidence**: manual §9.3 (quoted in table); `api/routers/cash.py` header and `add_fx` docstring assert HARD/no-ack (FU-D43a / FU-D34); live behavior verified: withdraw-over-balance → 422 `withdraw_insufficient_balance` (both end-balance and date-aware variants), fx-over-balance → 422 `fx_insufficient_balance`; `ack_negative` remains only on edit/delete paths. Neither `FU-D34` nor `FU-D43` appears in either manual file; §12.3 version history carries no entry for the change.
- **Why it matters**: §1.1 makes the manual the arbitration authority; today an arbitration of a refused withdrawal would conclude the app is wrong. The rulings appear to be the owner's intent (they are documented in code as owner-signed), so the manual is the stale side — but per the audit charter the conflict itself is the defect.
- **Suspected files**: `docs/accounting-formula-manual.en.md` §9.3 (+ zh mirror), §12.3.
- **Repair suggestion**: fold FU-D43a/FU-D34 into §9.3 (hard, no-ack; `negative_cash`+ack retained for deposit-side edits/deletes), add a §12.3 version-history row.

### BUG-03 (P2) — transaction import preview not sibling-aware (F1 residual)
- **Evidence**: probe (fresh DB + corpus cash/openings, then preview corpus `transactions.csv`): 4 `sell_exceeds_holdings` needs_confirm rows, each with the covering buy earlier in the same file. `_BATCH_GUARDED` covers cash and fx only (`input_center.py:978`); a `pending_actions` widening exists for the broker one-click path but nothing accumulates same-file buys.
- **Impact**: numbers still come out right after commit (replay sees the whole ledger — verified), but a legitimate bulk import trains the user to click through the ONE confirmation whose acknowledgement permanently discards a cost basis (sticky 賣超, §5.3) — the exact risk `cash_import.py`'s own comment names as "the E1a class of failure".
- **Suspected files**: `data_ingestion/csv_import.py::build_transaction_preview` (per-row validation against stored ledger only).
- **Repair suggestion**: accumulate same-file earlier buys per (account, symbol) into the availability check, mirroring `cash_import.py`'s sibling batch (and re-deriving over `select` on commit as QA-01 did for cash/fx).

### BUG-04 (P3) — chained corporate actions: loud now, but two-pass is undocumented and the message misdirects
- **Evidence**: real-history import: pass 1 → `rejected_rows: [{row 8, oversold_source, "GGR 目前是賣超（待釐清）… 請先補登缺少的買進或期初庫存"}]`; pass 2 of the byte-identical file → `{written 1, duplicates 7}`. (Demo-corpus chains committed in one pass — the improvement since the F4 write-up is real; the residue hits chains whose prerequisite is same-batch.)
- **Repair suggestion**: either extend the sibling-batch treatment to corporate actions, or auto-re-preview skipped rows after a commit that wrote something (the loop `smoke_import.py` implements), or at minimum have the rejection message name the retry path when the prerequisite rows are in the same batch.
- **Suspected files**: `data_ingestion/corporate_action_import.py` (validation against stored book), `api/routers/input_center.py::import_commit`.

### BUG-05 (P3) — raw exception text in the dividend importer's user-facing reason
- **Evidence**: reproduced (§3 table). `dividend_import.py:100` `gross = Decimal(raw["gross"])` inside a `except (…, InvalidOperation) as exc → Issue(message=str(exc))` — for `InvalidOperation` that string is `[<class 'decimal.ConversionSyntax'>]`; a malformed date would similarly leak CPython English.
- **Repair suggestion**: adopt the `_CellError` vetted-sentence pattern from `fx_import.py` (H-2); decide whether `NET`/`CASH` rows may derive `gross` from `net` when only net is on the statement (MY single-tier: net is the number of record, §6.3).

### BUG-06 (P3) — statement same-day ordering contradicts its own display rule
- **Evidence**: API rows + UI screenshot (`qa/evidence/cash_stmt_sameday_order.png`); my chronological replay (`cross_endpoints.py`, `out/cross_demo.txt`) flags the pool exactly at that tie. Per-row numbers are all correct; only intra-day ordering of same-sign rows is inverted relative to the table's newest-first direction.
- **Root cause**: `cash.py:474` `flat.sort(key=lambda item: (date, delta<0), reverse=True)` — stable sort + `reverse=True` keeps equal-key rows in ascending order; the comment above it believes they flip.
- **Repair suggestion**: sort ascending by the chronological key and reverse the list, or include a chronological sequence number in the key.
- **Frontend note**: `web/cash.js` renders rows in wire order, so the fix belongs server-side; the CSV export (`/api/export/cash-statement`) is oldest-first and unaffected.

### Verified-fixed leads (checked independently, no action needed)
- **F-01** (preview printed pre-trade averages): fixed — R6-E position preview shows `— → 602.00` old→new, server-computed (UI run).
- **F-02** (one acked oversell killed every drawer preview): fixed — other symbols preview normally; the oversold symbol itself degrades to `position_preview: null`.
- **F-03** (「確認寫入勾選列」 wrote everything): fixed — `select=[0]` wrote exactly 1 of 2 rows.
- **F-13** (fx-complete degradation blamed 缺價): fixed — reason now names 賣超.
- **F-16** (ledger-edit oversell warning cited final net position): fixed — message names the failing day and that day's holdings ("2026-04-10 … 5000 股，超過當日持股 110 股").
- **Corpus F2** (US cash dividends rejected): fixed by P1b — `CASH` on `drip_us` imports cleanly, reduces adjusted cost by net (1,500→1,430 verified), blank withholding raises the soft §6.2b ask with the exact documented wording; post-close path books `RealizedRow(kind="dividend")` (also observed on real data: SBUX 0.63, JNJ 7.59).
- **Corpus F4** (silent chain drop): the silence is fixed (`rejected_rows`), residue filed as BUG-04.

### By-design behaviors confirmed (not bugs; documented so repair agents don't "fix" them)
- Acknowledged oversell: whole sell emits **no realized row**, shares go negative, basis dropped, STICKY across later buys, valuation blanked (`market_value`/`unrealized_pnl` null with price present), XIRR + fx-complete + tax-package all blanked/refused **with reasons naming the row** (§5.3 family).
- `returns.xirr` is always null on the wire — XIRR is single-sourced in `kpis` (`dashboard_models.py:78`).
- KPI band's 「含匯兌總損益 / 本金匯率效果」 is the AI-D41 A·B·B−A decomposition (B−A verified = 73,116.0783), not an §8.4 double count.
- TW rebate forecast is display-only (658 = ⌊855×0.77⌋), never enters cost (§3.6 FE-D1).
- `INTEREST` credits don't dilute `covered_ratio`; broker fees are debits and not acquisitions (two-axis table §9.1.1) — exercised via the real-history import (38 INTEREST / 15 INTEREST_EXPENSE / 4 BROKER_FEE rows) with pools reconciling exactly.
- Tax package: `kind="dividend"` rows stay out of `realized_gains_{year}.csv` and appear via the dividends ledger (§6.3b separation, verified on real 2021 data).
- Import batch undo restores pools exactly (777-deposit probe: −615,418 → −614,641 → −615,418).

### Observations (below bug threshold)
- Import preview wire rows carry `status` + zh `reason` but `code: null` for warn-tier issues — the machine-readable issue kind is not exposed to the frontend.
- With **no** FX rate stored at all, `kpis.total_market_value` renders `0` while `realized_total` is null — a fully-unvalued book displaying "0" total value is arguably less honest than null (only reachable when every price is missing).
- On first import into a fresh ledger, the FU-D34 batch guard's message reports "可用餘額" figures with same-batch siblings deducted (can show a negative "available balance") — see BUG-01 note.

## 5. Unable to verify (with reasons)
1. **Live market-quote / FX provider paths** (`pricing/` fetchers, FinMind/others): the sandbox has no market-data network access. All prices/FX in tests were injected deterministically at the documented write seams. Provider parsing, staleness refresh, and scheduler-driven pricing are untested here.
2. **TWR series arithmetic** (`/api/performance/twr`): requires benchmark index history, which cannot be fetched; verified only that it degrades honestly (`available: false` with a named reason) instead of fabricating.
3. **The user's actual portfolio numbers**: the provided DB snapshot contains no ledger rows (accounts/config only), so no reconciliation against the owner's live figures was possible; the real-data mandate was satisfied with the Schwab export corpus instead.
4. **檢視清單 ① exact four figures** (`demo_schwab_raw.csv`): that 12-row file lives outside the repo. Equivalent converter behaviors were verified on `tests/golden/broker/schwab_2024.csv` via `/api/broker/convert`: broker sell fee preserved (0.07, not recomputed), DRIP `reinvest_shares` recomputed from net/price to 8 dp (1.81366934), INTEREST / INTEREST_EXPENSE kinds mapped. The interest-NRA netting case (4.20 → 2.94) has no row in the golden file and was not directly reproduced.
5. **Options** (79% of real-record realized P&L): out of app scope by declared spec gap C; excluded from all comparisons on both sides.
6. **Google-webfont load**: blocked by the sandbox egress proxy on every page (`fonts.googleapis.com` → ERR_TUNNEL_CONNECTION_FAILED). Per `CLAUDE.md` the webfont deliberately stays on the CDN, and pages render fine on fallback stacks — environment artifact, not an app defect; noted so console captures are read correctly.
7. **Scheduler/notifications/LLM content**: out of scope by charter; pages passed the console sweep (no errors beyond the webfont).

## 6. Appendix — auditor artifacts

Scripts (`/home/claude/work/qa/audit/`):
- `setup_demo_db.py` · `import_demo.py` · `inject_prices.py` — demo-corpus DB build + locked-order HTTP import + deterministic quotes/FX.
- `oracle.py` — the independent recomputation engine (shares no code with the app): five-ledger replay per manual §4/§5/§6 (incl. corporate actions, declared shorts, sticky oversell), cash pools §9.1, FX pools §8.1–8.3, KPI rollups §7.1, own XIRR bisection solver §7.2; diffs against `/api/dashboard` + `/api/cash` (+ EXPECTED_positions.csv on the corpus).
- `cross_endpoints.py` — dashboard vs cash vs statement vs exports (`/export/holdings`, `/export/realized`) vs symbol detail vs ledgers row counts vs allocation/subtotals/currency_view.
- `e2e_pages.py` · `e2e_manual_entry.py` — Playwright page sweep and the real manual-entry form flow (fills, preview fee asserts, commit, cross-page propagation).
- `setup_scen_db.py` · `scenario_run.py` — the §3/§4/§5/§6/§9/§10 scenario battery (fee worked examples et al.).
- `xirr_closed_form.py` — closed-form XIRR probe (+10%/365 d, perturbation).
- `wire_types.py` — Decimal-string wire sweep (float-leaf detector) over 14 endpoints.
- `gen_schwab_import.py` · `setup_schwab_db.py` · `compare_schwab.py` — TABLE_A → app-CSV mapping (documented in-file), real-history import, TABLE_B/TABLE_A reconciliation. Generated CSVs in `qa/audit/schwab_import/`.

Run logs (`/home/claude/work/qa/audit/out/`): `oracle_demo.txt` (99 OK/0), `oracle_fees.txt` (41 OK/0), `oracle_schwab.txt` (112 OK/0), `cross_demo.txt` (118 OK/1 = BUG-06 evidence), `cross_fees.txt` (69 OK/0), `compare_schwab.txt` (68 OK; 2 logged aggregate deltas decomposed in-report: cash Δ9.27 = broker DRIP rounding folded by the mapping + 0.02 CIL price quantization; realized Δ2,376.04 = FIFO-vs-WAC on OPEN positions — closed-group Σdiff −0.0029), `e2e_pages_demo.txt`, `console_log.json`, `dashboard_demo.json`, `cash_demo.json`.

Screenshots (`/home/claude/work/qa/evidence/`): `index_dashboard.png`, `detail_drawer_brwn.png`, `trades_ledger.png`, `cash_page.png`, `cash_statement_tw.png`, `cash_stmt_sameday_order.png` (BUG-06), `manual_preview_tw_buy.png`, `manual_preview_tw_sell.png`, `manual_commit_tw_buy.png`, `instruments.png`, `data_center.png`, `settings_fees.png`.

QA fixture deviations (documented, deliberate): demo corpus imported with two added TWD funding deposits (BUG-01 workaround; EXPECTED figures unaffected — verified); Schwab mapping decisions listed in `gen_schwab_import.py`'s docstring.
