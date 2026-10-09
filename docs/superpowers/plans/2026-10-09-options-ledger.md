# Options ledger (P3, approach ④) — Implementation Plan

> **Spec:** `docs/spec/2026-10-09-options-ledger.md` (APPROVED 2026-10-09: R1–R7, OPT-D1..D10).
> **Branch:** `feat/options-ledger`, off `main` at `aa5c80a`. **Status:** PLAN for owner review —
> no code until the owner approves this plan (owner, 2026-10-09: approve the plan → compact the
> context → then build).
> Execution: subagent-driven, **at most 3 parallel subagents**, worktree-isolated, disjoint files.
> Every steps list is `- [ ]`. Line numbers in the appendix were verified at `aa5c80a`; re-check
> before editing.

## 0. Goal and invariants

**Goal.** US equity options in the ledger: each contract an instrument, Sell to Open = the declared
short sale, Buy to Close its cover, expiry / assignment / exercise a price-0 closing trade with
`close_reason`, contract adjustments a new action kind `OPTION_ADJUST`; last-trade valuation; a
separate 選擇權 section; every import and export door. The owner's goal behind it: the app's Schwab
USD cash ends at the broker's figure once the whole real history is in.

**Invariants every task must keep (a violation is a bug, not a choice):**

1. **Blast radius (D38).** A ledger with no option is byte-identical in every API payload and export.
   The golden payload `tests/golden/dashboard_full.json` does not change; the `options` block is
   **omitted** (not `null`) when no option was ever booked.
2. **Money.** Decimal end to end; the frontend only formats server strings
   (`tests/contract/test_frontend_never_computes_money.py`).
3. **Layering** (`architecture.md`). New `shared/options.py` imports nothing internal (L0); new
   `portfolio/options.py` is calc core (L3). `portfolio/cost_basis.py` and `portfolio/results.py`
   **stay leaves** (`tests/architecture/test_layer_edges.py`). No new upward edge.
4. **`prices` is never written for an option** — the last-trade price is a read-path value; the
   spinoff seed stays the only human-typed price row.
5. **Equities unchanged.** `EXCHANGE` / `SPLIT` / `SPINOFF` semantics, the 賣超 guard, fees and the
   replay for stocks behave exactly as before; every new branch is gated on `asset_type == 'option'`
   or on the new kind / column.
6. **One definition per rule** — option-symbol parsing in `shared/options.py`, "is this an option"
   as `Instrument.is_option`, last-trade price in `portfolio/options.py`, the trend note text in one
   constant. No second copy in a door, a reader or the oracle (the oracle transcribes from the spec).

**Process (standing directives).** Fail-before tests for every behaviour. While fixing: ruff +
targeted tests only. **Heavy gates once, after the final review**, one at a time: ruff · mypy
--strict with a fresh `--cache-dir` · pytest non-e2e (batch totals = `--collect-only`) · e2e in
≤ 11-test chunks · stress phase 1. Run with `C:\AI-Workspace\invest-portfolio\.venv\Scripts\python.exe`
from the worktree root. Worktree subagents come up on `main`: each **first** runs
`git reset --hard <wave base sha>` and reports its merge-base. Subagents never touch `CHANGELOG.md`,
`api/app.py`, `tests/conftest.py` or rule files — the main session integrates those. Scoped
`git add` only; commit trailer per the session's attribution lines.

## 1. Waves and ownership

| Wave | Who | Packages | Base |
| --- | --- | --- | --- |
| 0 | main session | **W1** foundation (shared types, schema, store, registration, the new action kind's vocabulary) | `aa5c80a` + spec/plan commit |
| 1 | 3 subagents in parallel | **A = W2** replay + valuation + trend + dashboard payload · **B = W3** input doors + validators + fees + converter · **C = W4** fetch scope + reader exclusions | W1 tip |
| 2 | 3 subagents in parallel | **D = W5** web UI + drawer + what's-new · **E = W6** exports · **F = W7a** stress oracle + scenario ops | merged wave-1 tip |
| 3 | main session | integration, rule files, CHANGELOG, accounting manual (W7b), final review, fixes, heavy gates, local real-data acceptance | merged wave-2 tip |

File ownership is disjoint inside a wave (§9 lists it). A file two packages need is assigned to one;
the other only calls it.

## 2. W1 — Foundation (wave 0, main session)

**Files:** create `portfolio_dash/shared/options.py`; modify `shared/symbol_format.py`,
`shared/models/assets.py`, `shared/models/ledger.py`, `shared/corporate_actions.py`,
`data_ingestion/schema.py`, `data_ingestion/store.py`, `data_ingestion/register.py`; tests under
`tests/shared/`, `tests/data_ingestion/`.

- [ ] **`shared/options.py`** — `OptionTerms` (underlying, expiry `date`, strike `Decimal`, right
  `Literal["C","P"]`, multiplier `int` = 100); `parse_option_symbol(sym) -> OptionTerms | None`
  (OCC without root padding: `^[A-Z][A-Z0-9]{0,5}\d{6}[CP]\d{8}$`, strike = 8 digits / 1000);
  `option_symbol(terms) -> str`; `option_display_name(terms)` 「XYZ 2026-12-18 50 Put」 (strike
  printed without trailing zeros); `from_broker_option(text) -> OptionTerms | None` for
  "XYZ 12/18/2026 50.00 P"; `TREND_EXCLUDES_OPTIONS_NOTE` (the spec §5 sentence, the one copy).
  Module header carries the `TODO(options-⑤)` block → spec §12 (owner R7).
- [ ] `symbol_format.py`: an `OPTION` shape recognised by the same helper the US pattern uses, so
  `symbol_format_mismatch` stops firing on an OCC symbol.
- [ ] `Instrument`: `asset_type: Literal["stock","option"] = "stock"`, `option: OptionTerms | None`,
  property `is_option`. `Transaction`: `close_reason: Literal["expired","assigned","exercised"] | None = None`.
- [ ] Schema (additive, `_add_column_if_missing`): `instruments.asset_type TEXT NOT NULL DEFAULT
  'stock'`; `transactions.close_reason TEXT`; `CREATE TABLE IF NOT EXISTS option_contracts (symbol
  TEXT PRIMARY KEY, underlying TEXT NOT NULL, expiry TEXT NOT NULL, strike TEXT NOT NULL,
  option_right TEXT NOT NULL, multiplier INTEGER NOT NULL DEFAULT 100)`. Created at boot (a GET
  never writes — DEF-065).
- [ ] Store: `upsert_instrument` writes `asset_type` (explicit column list); `_row_to_instrument`
  reads it and attaches `option` from one `option_contracts` read per `list_instruments` call (no
  N+1); `upsert_option_contract` / `get_option_contract`; transactions insert / update / list
  carry `close_reason`; the transaction order keys are unchanged.
- [ ] `register.py`: `register_option_contract(conn, symbol) -> Instrument` — parse, refuse
  (zh `Issue`) when not an option symbol / multiplier ≠ 100 / market not US; idempotent; name =
  display name; market `US`, ccy `USD`, `asset_type='option'`; never fetches a quote.
- [ ] `corporate_actions.py`: `CorporateActionKind.OPTION_ADJUST`, `KIND_ZH` 「選擇權合約調整」,
  Pydantic rules (no `cost_carry`), `convert_stored` reads it. Update
  `tests/contract/test_i14_one_action_vocabulary.py` if it pins the kind list. **The replay branch is
  W2's**; until then a stored `OPTION_ADJUST` must not reach the SPINOFF fall-through — W1 adds a
  refusing branch (「尚未支援」) that W2 replaces.
- [ ] Tests (fail first): symbol parse / format / display round trip incl. 7.5 strikes and digit
  roots; an old database migrates with every instrument `stock` and every `close_reason` NULL;
  register idempotency and refusals; a no-option golden ledger is byte-identical (golden test
  stays green untouched).
- **Done when:** targeted tests green, ruff clean, mypy clean on touched modules; commit
  `feat(options): W1 foundation — option terms, schema, registration, OPTION_ADJUST vocabulary`.

## 3. W2 — Replay, valuation, trend, dashboard payload (wave 1, subagent A)

**Files:** `portfolio/cost_basis.py`, `portfolio/options.py` (new), `portfolio/dashboard.py`,
`portfolio/timeseries.py`, `portfolio/allocation.py` (only if the exclusion cannot sit in
`dashboard.py`), `shared/models/ledger.py` (`opened_by` / `position_opened_by`),
`data_ingestion/holdings.py` (`_delta_of`, `created_by`), `api/dashboard_models.py` (or wherever
`TrendPoint` / the payload models live), `api/wire.py` for the options block; tests under
`tests/portfolio/`, `tests/contract/`.

- [ ] **Replay — price-0 closes need no new arithmetic**: prove it with tests (short expiry →
  `short_cover` = remaining premium; long expiry → `sale` = −basis; same-day BTC then expiry in
  write order; partial assignment). Fix only what a test proves wrong.
- [ ] **Replay — `OPTION_ADJUST` branch** in `_apply_action`, placed before the SPINOFF
  fall-through: source must hold a long or a short position (else the existing E1/E2 refusal path);
  long lot → destination `shares × ratio`, totals carried; short lot → `short_shares × ratio`,
  `short_proceeds` carried; source flat afterwards; destination may be new or hold the SAME
  direction (lots add); opposite directions → refuse (`_reject`, 待釐清). No realized row. Priority
  CORPORATE_ACTION (start of day).
- [ ] **Share walker** (`holdings.py::_delta_of`, `created_by`; `opened_by`): `OPTION_ADJUST` moves
  the signed count by the ratio (shorts included — unlike EXCHANGE, which skips a negative side),
  so a sell-to-close / buy-to-close on the new contract is covered on its date.
- [ ] **`portfolio/options.py`**: `last_trade_prices(transactions, actions, as_of) -> {symbol:
  (price, date)}` (ordinary trades only — `close_reason` rows skipped; across an `OPTION_ADJUST`
  the predecessor's price × `ratio_from / ratio_to`); `option_summary(book, instruments, prices,
  rates, reporting) -> OptionsBlock` (open rows per spec §7 incl. 履約義務 for a sold put =
  strike × multiplier × contracts; realized by year / by underlying with contract rows; subtotals
  per currency and in the reporting currency).
- [ ] **Valuation seam** (`dashboard.py` `price_reads` / `price_map`): an option's price comes from
  `last_trade_prices`, `price_as_of` = that trade's date, a `price_source="last_trade"` marker; it
  is never added to `missing_prices` / `any_stale`.
- [ ] **Split the rows**: holdings table rows, sector allocation, weights, subtotals and the
  ex-dividend calendar use stock rows only; KPI totals (總市值, 總報酬, unrealized) include option
  rows and expose `options_market_value` for the 「其中選擇權」 line; XIRR's final value and the FX
  pool's `foreign_stock_value` include options; the realized section exposes an aggregated option
  line (its rows live in the options block).
- [ ] **Trend** (`timeseries.py` per-holding loop): skip `is_option` holdings **without** setting
  `incomplete`; the series carries `excludes_option_value: bool` (true only when an option position
  existed in the window) and the note constant. B (`fx_complete_return`), TWR, benchmark and 月度成績
  carry the same flag. Option cash flows stay in `net_invested` and the cash series.
- [ ] **Payload**: `/api/dashboard` gains `options` (omitted when no option was ever booked) and the
  flags above; all money as Decimal strings.
- [ ] Tests (fail first): each §4 spec-table row through `build_book`; `OPTION_ADJUST` 1→3 on a
  short and 1:1 rename; opposite-direction merge refused; walker covers a post-adjust close;
  last-trade price incl. across an adjustment; KPI = stocks + options while holdings rows exclude
  options; trend skips the option value without `incomplete`; XIRR includes the open option's
  value; golden unchanged; a no-option ledger produces no `options` key.
- **Done when:** targeted tests green, ruff clean, mypy clean on touched modules, architecture tests
  green; commit `feat(options): W2 replay (OPTION_ADJUST), last-trade valuation, trend exclusion,
  dashboard options block`.

## 4. W3 — Input doors, validators, fees, broker converter (wave 1, subagent B)

**Files:** `data_ingestion/validate.py`, `csv_import.py`, `corporate_action_import.py`,
`import_templates.py`, `fees.py`, `config_seed.py`, `fee_overrides.py`, `agents.py`,
`data_ingestion/broker/{ir,schwab,grouping,convert,reconcile}.py`, `api/routers/input_center.py`,
`api/routers/ledgers.py` (transaction edit door + corporate-action door), `api/routers/fees.py` if the
fee editor lists fields there; tests under `tests/data_ingestion/`, `tests/contract/`, `tests/api/`.

- [ ] **Validators** (`validate.py`):
  - `transaction_structural_issues`: price 0 allowed only with `close_reason` on an option;
    `close_reason` on a stock refused; fee / tax on a `close_reason` row refused;
  - `expired`: date = contract expiry, quantity = the whole open position on that date
    (`holdings.shares_through`, date-aware); `assigned` / `exercised`: date ≤ expiry, quantity ≤ open;
  - option quantity a multiple of the multiplier (zh: 「選擇權數量須為 100 的倍數（1 口 = 100 股當量）」);
  - a sell that opens a short on an option without `short_sale` = the ordinary 賣超 (no inference);
  - `dividend_on_option` (hard);
  - `validate_corporate_action`: `OPTION_ADJUST` — both symbols registered options (or registrable
    from their symbols at the same door), positive integer ratios, from ≠ to, a position exists;
    SPLIT / EXCHANGE / SPINOFF with an option symbol refused; a SPLIT on an underlying with open
    options → soft finding (spec §4 sentence).
- [ ] **Registration at the doors** (OPT-D7): CSV transactions and corporate actions, the manual
  door and the broker converter call `register_option_contract` for an unregistered OCC symbol (the
  preview says 「將建立選擇權合約 …」; the commit registers in the same transaction as the write).
  `quick_register` is not used for options.
- [ ] **CSV**: optional column `close_reason` (template header `(選填)`, example rows: a sell-to-open,
  an expiry); `shares` in share-equivalents with the preview's 「= N 口」; the corporate-action CSV
  accepts `OPTION_ADJUST` / 「選擇權合約調整」.
- [ ] **Fees** (OPT-D6): `FeeRuleSet.option_per_contract: Decimal | None`,
  `option_reg_per_contract: Decimal = 0`; `compute_fees(..., is_option=...)`: for an option, fee =
  per-contract × contracts (+ reg × contracts) + SEC on a sell (existing rule, notional basis), no
  share-based TAF / CAT / platform / MY stamp; `schwab.option_per_contract = 0.65`; `moomoo_my`
  None → an option trade with no supplied fee gets a hard 「此帳戶未設定選擇權手續費，請填入對帳單手續費」.
  A supplied fee always wins (existing path). Both fields owner-editable (`fee_overrides`). The
  per-row `fee_rule_snapshot` records `option: true`.
- [ ] **Manual door** (`input_center.py`): `ManualBody.close_reason`; an option symbol puts the form
  in option mode server-side (quantity arrives in share-equivalents — the UI converts 口數); preview
  mirrors the replay branch; a soft finding when a 「買進平倉」/「賣出平倉」 does not match the position;
  a two-leg **assignment / exercise** commit (option close + stock trade at the strike, one
  transaction, both validated, the stock leg through the ordinary doors' rules).
- [ ] **Edit / delete doors** (`ledgers.py`): the transaction edit door validates `close_reason` rows
  like the create doors; the corporate-action create / edit / delete doors accept `OPTION_ADJUST`
  (replay guards as for any action).
- [ ] **Schwab converter**: option rows stop being dropped (`grouping.py` routing, `convert.py`
  `option_row_unsupported`); `from_broker_option` → OCC symbol; Sell to Open → SELL `short_sale=1`,
  Buy to Close / Buy to Open → BUY, Sell to Close → SELL, broker fees verbatim, tax 0; `Expired`
  and TD 「REMOVAL OF OPTION DUE TO EXPIRATION」 → price-0 `close_reason=expired` dated the contract's
  expiry, written after that day's trades; 「OPTION ODD SPLIT REORGANIZATION」 / 「OPTION POSITION
  CHANGE」 same-day journal pairs → one `OPTION_ADJUST` (ratio = |to qty| : |from qty|; an unpaired
  half raises loudly); 「TRANSFER OF SECURITY OR OPTION OUT/IN」 of an option between the broker's own
  platforms → dropped with a stated reason; `Assigned` / `Exercise` still `UnmappedRow` (OPT-D9);
  reconcile advisory and `web/broker-import.js` labels updated by **D** (W5) — B exports the new
  reason codes only.
- [ ] **AI door**: an OCC symbol does not raise `symbol_format_mismatch`.
- [ ] Tests (fail first): every validator rule with its zh message; CSV round trip of the raw
  transactions export with option rows; template example rows commit clean; fee engine per account
  (schwab 0.65 × contracts + SEC on sell; moomoo_my hard issue; supplied wins); manual two-leg
  assignment writes both rows or neither; converter on a synthetic Schwab file covering every option
  row shape (STO / BTC / BTO / STC / Expired "as of" / TD removal / odd-split pair / position-change
  pair / platform transfer / an Assigned row that must raise).
- **Done when:** targeted tests green, ruff, mypy on touched modules; commit
  `feat(options): W3 doors — validators, CSV + manual + converter, per-contract fees`.

## 5. W4 — Fetch scope and reader exclusions (wave 1, subagent C)

**Files:** `shared/instrument_scope.py`, `api/instrument_service.py`, `scheduler/jobs.py`
(`refresh_instrument_quote` only), `api/fundamentals_service.py`, `api/news_service.py`,
`strategy/{rebalance,whatif,alerts}.py`, `api/{alert_inputs,signals_service,insight_service,
digest_service}.py`, `api/routers/{strategy,instruments,prompts,actions}.py`,
`llm_insight/variables.py`, `data_ingestion/dividend_inbox.py` if it reads its own universe; a new
scan test `tests/contract/test_options_excluded_from_stock_readers.py`.

- [ ] `TRACKED_SQL` / `tracked_instruments`: `asset_type = 'stock'` — one seam for every scheduled
  fetch (quotes, history, dividends, inbox scan, FinMind, consensus, fundamentals).
- [ ] The five bypass paths refuse an option with a stated reason, never a provider call:
  `quick_register`'s quote gate + history fetch, `gap_backfill`, `refresh_instrument_quote`, the
  Alpha Vantage held universe, held-only news.
- [ ] `held_among` keeps answering for options (the registry question — archive / purge use it);
  the quote-outcome hook can never see an option (it is not in the worklist) — pin with a test.
- [ ] Readers exclude options (OPT-D10): target weights, rebalance, what-if, alerts (single weight,
  sector, drift), alert inputs, signals, insights (`held_in_book`), LLM variables, prompts preview,
  digest (incl. `_chores` staleness), news, fundamentals; the watchlist list hides options unless
  `include_options=1` (new query param), archive / remove / purge rules unchanged.
- [ ] **Scan test**: find every module that iterates `book.holdings`, calls `held_among` /
  `holds_position` / `held_in_book`, or reads `tracked_instruments`; each must apply the stock
  filter or be a reviewed allowlist entry with its reason (dashboard rows are W2's and listed there).
  The test is what stops the next reader from forgetting.
- [ ] Tests (fail first): a ledger holding a stock and an option — each reader's output names the
  stock only; no provider is called for the option (mocked providers assert no call); the quote
  job's outcome stays `ok` with an option held.
- **Done when:** targeted tests green, ruff, mypy on touched modules; commit
  `feat(options): W4 options out of every fetch scope and stock reader`.

## 6. W5 — Web UI, drawer, what's-new (wave 2, subagent D)

**Files:** `web/index.html`, `web/app.js` (or a new `web/options.js` + its script tag), `web/charts.js`,
`web/ledger.js`, `web/trades.html`, `web/input.js`, `web/corp-action-form.js`,
`web/broker-import.js`, the drawer's JS, `web/instruments.js` (watchlist filter), `web/cash.js` if
it builds row descriptions, the what's-new catalog, `api/routers/symbol.py` (contract header in the
drawer payload); tests under `tests/contract/`, `tests/e2e/`.

- [ ] Dashboard **選擇權** section after 持倉明細 (`data-screen-label="選擇權"`): 未平倉 table and 已實現
  (by year / by underlying, expandable rows), subtotals, the trend note; hidden when the payload has
  no `options` key. KPI 「其中選擇權」 line; the 已實現損益 section's aggregated option line linking to
  the section. Trend chart note when `excludes_option_value` (replacing nothing — the incomplete
  note keeps its own text).
- [ ] Ledger 交易 tab: contract display name, 口數 beside share-equivalents, chips
  「賣出開倉」/「買進平倉」/「買進開倉」/「賣出平倉」/「到期」/「被指派」/「履約」; 公司行動 tab and form: `OPTION_ADJUST`.
- [ ] Manual entry option mode: detects an OCC symbol (or a contract picker: underlying / expiry /
  strike / C-P → symbol), 口數 input (converted to share-equivalents before sending), the four
  sides, 「到期作廢」 and 「被指派／履約」 (two-leg) actions, preview showing 口 and share-equivalents.
- [ ] Drawer for a contract: header (underlying, expiry, strike, right, multiplier), trades in 口,
  no 配息史, 試算 replaced by 「選擇權不提供試算」.
- [ ] Watchlist: 「顯示選擇權合約」 filter. Broker-import page: the new option row labels.
- [ ] What's-new catalog entry saying where to verify (standing directive).
- [ ] Static contract tests stay green (`?v=` stamps, `names.js` before `api.js`, page lists,
  `ledger-count-ok`, state colours as classes, secret inputs). e2e: the section renders from a seeded
  option ledger; option-mode manual entry (STO → BTC) end to end; the section is absent on the
  golden ledger; no horizontal scroll at phone width.
- **Done when:** targeted contract + e2e chunks green, ruff, mypy on `symbol.py`; commit
  `feat(options): W5 選擇權 section, ledger chips, option-mode entry, drawer, what's-new`.

## 7. W6 — Exports (wave 2, subagent E)

**Files:** `export/{holdings,holdings_report,realized,tax,ledgers_report,cash_statement,
symbol_detail,rebalance_report}.py`, `api/routers/export.py` if a builder signature changes; tests
under `tests/export/`, `tests/contract/`.

- [ ] 持股 CSV / 持股報表: stock rows unchanged; a separate 選擇權 block (spec §7 columns) and the KPI
  「其中選擇權」 line — only when an option exists (blast radius).
- [ ] 已實現損益 CSV: columns `asset_type`, `contract` (display name) appended at the end.
- [ ] 稅務包: contract name on option rows; 「其中選擇權」 subtotal in the summary; option closes are
  already `sale` / `short_cover` — pin that they land in capital gains.
- [ ] 帳本報告: 口數 + option chips; `OPTION_ADJUST` in the actions section.
- [ ] 現金收支明細 CSV / print: option descriptions (「賣出開倉 1 口 XYZ 2026-12-18 50 Put」, 「到期作廢」…).
- [ ] 個股明細 CSV for a contract; 再平衡報告 excludes options.
- [ ] Raw ledgers zip / per-tab CSV: `close_reason` rides `SELECT *`; re-import round trip test.
- [ ] Every export of a no-option ledger byte-identical (existing export tests unchanged).
- **Done when:** targeted tests green, ruff, mypy; commit `feat(options): W6 exports`.

## 8. W7 — Oracle, manual, acceptance (F in wave 2; the rest in wave 3)

**W7a — stress-audit oracle (subagent F; `scripts/stress_audit/*` only).** Transcribe from the
**spec**, never import the app (`grep -E '^\s*(import|from)\s+portfolio_dash' oracle.py` must print
nothing):
- [ ] `common.load_facts_from_db` reads `close_reason` and the option terms; `oracle.py`: price-0
  closes, `OPTION_ADJUST` (`_KINDS`, its own `_apply_action` branch, `replay()`,
  `integrity_findings`), last-trade valuation, trend exclusion.
- [ ] Phase-1 scenario ops: STO→BTC · STO→expire · BTO→STC · BTO→expire · adjustment 1→3 on a short ·
  rename 1:1 · sold-put assignment (option + stock legs) · a same-day BTC + expiry · cash ties per
  pool. Assertions: realized per contract, open position, cash, KPI = stocks + options, trend
  excludes the option value.
- **Done when:** phase 1 green; the new baseline (ops / pass / fail) recorded; commit
  `test(stress-audit): options — oracle transcription + scenario ops`.

**W7b — wave 3, main session:**
- [ ] Accounting manual zh + en: new options section (formulas for each §4 row, valuation,
  trend exclusion, fees) with its verification anchor = the phase-1 run; version bump + history row.
- [ ] Rule files: `domain-ledger.md` (options: the mapping, close_reason, OPTION_ADJUST vs EXCHANGE,
  R5), `data-and-pricing.md` (option terms table, last-trade valuation never written to `prices`,
  share-equivalent quantity), `markets-and-fees.md` (option fees), `architecture.md` (the two new
  modules, the stock-reader exclusion seam).
- [ ] CHANGELOG `[Unreleased]`: owner rulings R1–R7 + OPT-D1..D10 and the feature; integrity count
  unchanged (`grep -c "^## \[v"` = 30).
- [ ] Final review (code review at xhigh on `main..feat/options-ledger`), fixes with fail-before
  tests, then the heavy gates once.
- [ ] **Real-data acceptance (local, throw-away database, real data never committed):** convert the
  owner's Schwab option rows through the new converter and import them through the app's doors:
  option realized, the open contract, and the option cash total equal the figures in the git-ignored
  `sample-trade-data/charles-schwab/OPTIONS_ledger_proposals.md`. The full-history cash target
  (the broker's cash on its last statement date) is met when the whole history is imported — that
  needs the trade / dividend / opening conversion, the next work item after this branch.
- [ ] Report to the owner; **demo deploy only on the owner's word** — the demo currently runs the
  owner's own real-data test on a blank database, and this branch migrates its schema (additive).

## 9. File ownership by wave (no two subagents in one file)

| Wave 1 — A (W2) | Wave 1 — B (W3) | Wave 1 — C (W4) |
| --- | --- | --- |
| `portfolio/cost_basis.py`, `portfolio/options.py`, `portfolio/dashboard.py`, `portfolio/timeseries.py`, `portfolio/allocation.py`, `data_ingestion/holdings.py`, `shared/models/ledger.py`, payload models + `api/wire.py` | `data_ingestion/{validate,csv_import,corporate_action_import,import_templates,fees,config_seed,fee_overrides,agents}.py`, `data_ingestion/broker/*`, `api/routers/{input_center,ledgers}.py`, fee router | `shared/instrument_scope.py`, `api/instrument_service.py`, `scheduler/jobs.py`, `api/{fundamentals_service,news_service,alert_inputs,signals_service,insight_service,digest_service}.py`, `strategy/*`, `api/routers/{strategy,instruments,prompts,actions}.py`, `llm_insight/variables.py` |

| Wave 2 — D (W5) | Wave 2 — E (W6) | Wave 2 — F (W7a) |
| --- | --- | --- |
| `web/*`, `api/routers/symbol.py`, what's-new catalog | `export/*`, `api/routers/export.py` | `scripts/stress_audit/*` |

Main session only: `CHANGELOG.md`, `api/app.py`, `tests/conftest.py`, `.claude/rules/*`, the
accounting manuals, merging the worktrees (cherry-pick; re-run the count / seam tests after each).

## 10. Risks and how the plan meets them

| Risk | Mitigation |
| --- | --- |
| A stock reader nobody listed still sees an option | W4's scan test fails on any new reader that skips the filter |
| The share walker and the replay disagree on `OPTION_ADJUST` | W2 owns both; a test drives the same ledger through `build_book` and `shares_through` |
| Same-day order (BTC then expiry) | price-0 closes are trades in write order; the converter writes expiries last; a test pins it |
| Golden / no-option exports drift | invariant 1; existing golden + export tests must stay untouched |
| 100× quantity error | multiple-of-multiplier validation at every door; 口 shown beside share-equivalents everywhere |
| Option symbol reaches a provider | one fetch-scope seam + five guarded bypasses + mocked-provider tests |
| Oracle copies the app | independence grep; transcribed from the spec |
| Demo data | no deploy without the owner's word (real-data test running there) |

## Appendix — code map (verified at `aa5c80a`; re-check line numbers before editing)

**Replay** — `portfolio/cost_basis.py`: `_Position` long + separate positive short lot (`short_shares`,
`short_proceeds`) :83-89; signed output :1007-1009; `short_open` :1058; BUY covers first, emits
`kind="short_cover"` :714-751; declared SELL sells long first, rest to the short lot, no realized
row for the short part :752-785; 0-share position kept in `opened_positions` :1014-1016, skipped in
holdings :1029-1030; qty < 0 raises, 0 skipped :643-710; dividend on an open short refused
:860-880; sort key `(date, priority, ledger seq)` :592-603; `_apply_action` :298-415 (E1 :298,
E2 :304, E3 :310, SPLIT scales both lots :315-324, EXCHANGE/SPINOFF from a short refused E5
:327-331, short destination E18 :338-341, **every other kind falls into the SPINOFF arithmetic
:391-415** — the new branch goes before it); `_reject` / `UnappliedAction` :248-263.
`shared/ledger_events.py` priorities OPENING 0 · CORPORATE_ACTION 10 · TRADE 20 · DIVIDEND 40
:46-53. `portfolio/results.py` `RealizedRow.kind` :166.

**Action vocabulary** — `shared/corporate_actions.py` `CorporateActionKind` + `KIND_ZH` :36-52,
validator :111-131, `is_ratio_term` :68-78, `convert_stored` :266/:276; `schema.py` actions
`to_symbol NOT NULL` :65, no CHECK on `kind` :63. Registration points of a kind: `validate.py`
`unknown_action_kind` :1145-1150 and E-codes :1161-1549; `corporate_action_import.py`
`_KIND_ALIASES` :71-76, columns :61-64, dispatch :379/:389; `api/routers/ledgers.py` `_KIND_ALIASES`
:1101, dispatch :1754/:1887/:2041/:2069; `api/routers/symbol.py` role :345-350; `store.py`
`insert_corporate_action` :1452/:1474; `import_templates.py` :88/:154-169;
`broker/grouping.py` `_ACTION_FAMILY` :502-508; web `corp-action-form.js` :133-140/:686-703,
`ledger.js` :1020, `input.js` :1115, `broker-import.js` :82-88, `trades.html` :448-449;
`tests/contract/test_i14_one_action_vocabulary.py`.

**Share walker** — `data_ingestion/holdings.py` `_delta_of` :287-352 (EXCHANGE/SPINOFF with a
negative side skipped :328-333, SPLIT exempt :324-327), `created_by` :676;
`shared/models/ledger.py` `opened_by` :319.

**Validators** — `validate.py` `transaction_structural_issues` price/qty ≤ 0 :629-636;
`whole_share_issue` TW/MY only :431-441 (US not checked :420-421); declared short exempt from the
oversell guard :778; dividend `_dividend_findings` :2231-2440 (no instrument-type check).

**Instruments** — model `shared/models/assets.py` :38-70; DDL `schema.py` :9-20,
`_add_column_if_missing` :104-109; `store.upsert_instrument` explicit columns :162-227,
`_row_to_instrument` :580, `get_instrument` :623, `list_instruments` :634; `register.py`
`register_instrument` :33-79, `spinoff_child_draft` :82-135; `quick_register`
`api/instrument_service.py` :462-598 (quote gate :544-558); routes `api/routers/instruments.py`
POST :543-617 (gap_backfill :613), quick :630-660, `{symbol}` paths :663/:730/:761/:801;
`symbol_format.py` US pattern :25-29; CSV refuses unregistered symbols `csv_import.py` :289-306
(`opening_import.py` :215); manual auto-register `input_center.py` :1124-1179.

**Fetch scope** — `shared/instrument_scope.py` `TRACKED_SQL` :38, `tracked_instruments` :50-71;
`scheduler/jobs.py` `build_worklist` :1565-1582, `_quote_outcome` :480-530, held hook :470-477
(wired `api/app.py` :243 → `api/routers/actions.py` :34-57), `refresh_instrument_quote` :1585;
`pricing/ingest.py` universes :55-80; bypasses: `instrument_service.py` :417-435 (`gap_backfill`),
:544-598; `api/fundamentals_service.py` :24-37; `api/news_service.py` :141/:164;
`yfinance_provider.py` sends US symbols unchanged :39-53.

**Valuation** — `portfolio/dashboard.py` `build_dashboard` :624-1200; price reads :713-735
(`pricing/store.get_latest_price` :291-316); no price → `pnl.py` :55-66, stale row :992-993,
freshness `missing_prices` :1174 / `any_stale` :1173; XIRR all-or-nothing `returns.py` :388-396,
reason :844-845; `combined_view` `allocation.py` :43-65 → :961/:1121; `total_return`
`returns.py` :39-87 → :1078-1089; `sector_allocation` :16-40; row weight :967-973; subtotals
:317-390; ex-dividend calendar :940-957; FX stock value :858-868 → `forex/fx_pnl.py` :275/:329 →
`compute_account_fx` :138-217; `forex/pools.py` counts trades' settled notional :194/:241.

**Trend** — `portfolio/timeseries.py` :282-445 (per-holding :383-433, declared short counted
:405-409, no price → incomplete :410-413); `TrendPoint` `api/dashboard_models.py` :211-221;
`fx_complete_return` `dashboard.py` :489-533; frontend note `web/charts.js` :68-70/:121-122,
`web/index.html` :81; `net_invested` flows `timeseries.py` :198-204; cash series
`dashboard.py` :1046-1068.

**Stock readers to exclude** — allocation / weights / totals `allocation.py` :16-65,
`dashboard.py` :317-390/:967-973; ex-div :940-957; target weights `api/routers/strategy.py`
:179-200; rebalance `strategy/rebalance.py` :178-265; what-if `strategy/whatif.py` :473-481;
alerts `strategy/alerts.py` :288-300/:438-441; `api/alert_inputs.py` :130/:156; signals
`api/signals_service.py` :178-205; insights `api/insight_service.py` :124-167/:1385/:1606;
`llm_insight/variables.py` :590/:789-791/:901/:970-975; digest `api/digest_service.py`
:205-223/:507/:618-628; watchlist `api/routers/instruments.py` :66-78,
`data_ingestion/holdings.py` :564-620; prompts `api/routers/prompts.py` :419.

**Doors** — `csv_import.py` `TRANSACTION_COLUMNS` :55-58, import-only :66, unknown columns
:177-200/:587-589, `short_sale` parse :614, fee compute vs supplied :319-387; `fees.py`
`supplied_snapshot` :103-134, `_us` :208-255, `compute_fees` :297-336, settled notional :322-327;
`config_seed.py` `FeeRuleSet` :42-54, schwab :134-143; `fee_overrides.py` fields :46-60/:80;
`input_center.py` `ManualBody` :402-443 → `TxnInput` :446-454, `manual_preview` :1026-1102,
`_cash_overdraft_issue` :560-595; UI `trades.html` :102-114, `input.js` :193-211/:252/:284-318/
:451-534. Converter: `broker/ir.py` `OPT_*` :76-86, `OPTION_KINDS` :117-120, `option_symbol`
:170-172; `broker/schwab.py` option rules :95-100, journal rules :120-122, option detection
:305-321, `UnmappedRow` :298-303; `grouping.py` routes options :699-700; `convert.py`
`option_row_unsupported` :349-354, trade rows :177-192, `UNCONVERTIBLE` :63-69; `reconcile.py`
advisory :453-459.

**Web** — nav `web/shell.js` :9-18; dashboard sections `web/index.html` (持倉明細 :126-138,
已實現損益 :183); `web/app.js` `renderHoldingsHead` :522, `renderHoldings` :659, dashboard fetch
:1624; ledger tabs `trades.html` :369-468, `LEDGER_TABS` :498; `ledger.js` `renderTx` :837-900,
`renderActions` :1290, `renderCash` :1350, keyword filter :105-110. Page/section-enumerating tests:
`test_static_cache_discipline.py` :43-54, `test_names_js_before_api_js.py` :63-77,
`test_def044_account_tokens_every_page_flow.py` (≥ 11 pages), `test_no_horizontal_scroll.py`
`_ALL_PAGES`, `test_p3_batch3_wave3.py` `_TOOLBAR_PAGES`, `test_whatsnew_api.py` :77,
`test_ledger_count_not_hardcoded.py`, `test_ledgers_cash_tab.py` :79-89.

**Exports** — `export/holdings.py` :61-63; `holdings_report.py` :147/:203/:282; `realized.py`
columns; `tax.py` capital gains = `sale` + `short_cover` :47-59; `ledgers_report.py`
`_side_chip` :110, `_transactions_section` :185, `_actions_section` :379; `cash_statement.py`
`_describe` :198; `symbol_detail.py` :58; `rebalance_report.py` :322/:337; raw zip =
`shared/ledger_registry.py` `LEDGER_TABLES` (ledger tables only; `instruments` not exported).

**Oracle** — `scripts/stress_audit/common.py` `load_facts_from_db` :501-565; `oracle.py`
`EventPriority` :559-577, `_Pos` :581-596, `_apply_action` :709-861 (`_KINDS` :633-634, unknown →
"E-kind" :769-770), `replay()` cover :947-960 / declared short :972-993 / emit :1059-1070,
`integrity_findings` :1574+ (short walk :1658-1664), `RealizedRow` :505-520; README independence
rule :244.
