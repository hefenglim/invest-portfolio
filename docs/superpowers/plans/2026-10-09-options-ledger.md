# Options ledger (P3, approach ④) — Implementation Plan

> **Spec:** `docs/spec/2026-10-09-options-ledger.md` (APPROVED 2026-10-09: R1–R7, OPT-D1..D12).
> **Branch:** `feat/options-ledger`, off `main` at `aa5c80a`. **Status:** PLAN for owner review —
> revised after review rounds 1 and 2 (Fable 5.1, max effort; round 2: READY-WITH-FIXES, folded). No code until the owner approves this plan
> (owner, 2026-10-09: approve the plan → compact the context → then build).
> Execution: subagent-driven, **at most 3 parallel subagents**, worktree-isolated, disjoint files.
> Every steps list is `- [ ]`. Line numbers in the appendix were verified at `aa5c80a`; re-check
> before editing.

## 0. Goal and invariants

**Goal.** US equity options in the ledger as the spec defines them. The owner's goal behind it: the
app's Schwab USD cash ends at the broker's figure once the whole real history is in.

**Invariants every task keeps (a violation is a bug, not a choice):**

1. **Blast radius (D38, spec §10).** A ledger with no option is byte-identical in every API payload
   and export **except the additive changes the spec §10 names** — raw transactions `close_reason`,
   the transaction template's `close_reason（選填）`, the realized CSV's `asset_type` / `contract`,
   and the wire keys (null / false / `"stock"` on a stock ledger) of cash statement rows (`short_sale`,
   `close_reason`), `/api/ledgers/transactions` rows (`close_reason`, `contracts`) and the instrument
   elements of `/api/instruments` and `/api/input/context` (`asset_type`) — each re-pinned in the
   same commit wherever a test pins it. The golden payload
   (`tests/golden/dashboard_full.json`, whole-payload equality) does not change: **no existing wire
   dashboard model gains a field**; every new dashboard field lives inside `options`, which the
   router removes when it is `None`; `Holding.unbookable_close` never reaches `HoldingRow` (no
   extra-field config — `HoldingRow(**h.model_dump())` drops it).
2. **Money.** Decimal end to end; the frontend only formats server strings; the multiplier lives in
   `shared/options.py`, never in JS.
3. **Layering** (`architecture.md`). New `shared/options.py` imports nothing internal (L0); new
   `portfolio/options.py` is calc core (L3). `portfolio/cost_basis.py` and `portfolio/results.py`
   **stay leaves** (`tests/architecture/test_layer_edges.py`). No new upward edge.
4. **`prices` is never written for an option** (the spinoff seed stays the only human-typed price).
5. **Stocks unchanged.** `EXCHANGE` / `SPLIT` / `SPINOFF`, the 賣超 guard, fees and the replay behave
   exactly as before; every new branch is gated on `is_option`, `close_reason` or `OPTION_ADJUST`.
6. **One definition per rule** — option-symbol parsing and the multiplier (`shared/options.py`), "is
   this an option" (`Instrument.is_option`), last-trade price (`portfolio/options.py`), the trend
   note (one constant), the close side/quantity rule (one validator helper). The oracle transcribes
   from the spec.

**Process (standing directives).** Fail-before tests for every behaviour. While fixing: ruff +
targeted tests only. **Heavy gates once, after the final review**, one at a time: ruff · mypy
--strict with a fresh `--cache-dir` · pytest non-e2e (batch totals = `--collect-only`) · e2e in
≤ 11-test chunks · stress phase 1. Run with `C:\AI-Workspace\invest-portfolio\.venv\Scripts\python.exe`
from the worktree root. Worktree subagents come up on `main`: each **first** runs
`git reset --hard <wave base sha>` and reports its merge-base. Subagents never touch `CHANGELOG.md`,
`api/app.py`, `tests/conftest.py`, `.claude/rules/*` or the accounting manuals — the main session
integrates those. Scoped `git add` only; commit trailer per the session's attribution lines. Merge
the worktrees by cherry-pick and re-run the seam tests after each.

## 1. Waves and dependencies

| Wave | Who | Packages | Base |
| --- | --- | --- | --- |
| 0 | main session | **W1** foundation: shared types, schema, store, registration, `OPTION_ADJUST` vocabulary, the result types other packages code against | `14dafce` + this plan's commit |
| 1 | 3 subagents | **A = W2** replay + valuation + trend + payload · **B = W3** doors + validators + fees + converter + cash lines · **C = W4** fetch scope + stock-reader exclusions | W1 tip |
| 2 | 3 subagents | **D = W5** web UI + drawer + what's-new · **E = W6** exports · **F = W7a** stress oracle + scenario ops | merged wave-1 tip |
| 3 | main session | integration, rule files, CHANGELOG, accounting manual (W7b), final review, fixes, heavy gates, local real-data acceptance | merged wave-2 tip |

**Cross-package dependencies, stated so no package waits on another inside its wave:**
- W1 defines every type the others code against: `OptionTerms`, `Instrument.is_option`,
  `Transaction.close_reason`, `RefusedClose`, `Book.refused_closes`, `Book.realized_close_reasons`,
  `OpenedPosition.unbookable_close`, `Holding.unbookable_close`, `OptionsBlock` (the wire model,
  empty shell) and `DashboardData.options`.
- B's validators read `holdings.shares_through` and the replay; their `OPTION_ADJUST` behaviour is A's
  deliverable. **B's tests do not cross an `OPTION_ADJUST` until the wave-1 merge**; the main session
  adds the cross tests (an expiry after an adjustment; a validator over an adjusted contract) at the
  merge.
- Readers of `unbookable_close` mirror today's readers of `unbookable_action`, split by file owner:
  A (`pnl.py` valuation, `position_aggregate.py`, `dividends.py`, `timeseries.py`, dashboard reasons,
  replay guard), B (the manual preview, `input_center.py`), D (`web/app.js` badges, `web/detail.js`
  drawer). Each implements its own readers in its wave.
- D needs A's `options` block and B's door responses; E needs A's block and B's `CashLine` fields; F
  needs only the spec — all are wave 2.

## 2. W1 — Foundation (wave 0, main session)

**Files:** create `portfolio_dash/shared/options.py`; modify `shared/symbol_format.py`,
`shared/models/assets.py`, `shared/models/ledger.py`, `shared/corporate_actions.py`,
`portfolio/results.py` (types only), `portfolio/dashboard_models.py` (types only),
`data_ingestion/schema.py`, `data_ingestion/store.py`, `data_ingestion/register.py`,
`api/routers/dashboard.py` (the `None` pop only), `tests/contract/test_i14_one_action_vocabulary.py`; new tests under `tests/shared/`,
`tests/data_ingestion/`.

- [ ] **`shared/options.py`** (module header: `TODO(options-⑤)` block → spec §12):
  `MULTIPLIER = 100`; `OPTION_PATTERN = ^[A-Z]{1,6}\d{6}[CP]\d{8}$` (letters-only root — a digit
  root is a non-standard deliverable, OPT-D11); `OptionTerms` (underlying, expiry `date`, strike
  `Decimal`, right `Literal["C","P"]`, multiplier `int`); `parse_option_symbol(sym) -> OptionTerms |
  None`; `option_symbol(terms)`; `option_display_name(terms)` 「XYZ 2026-12-18 50 Put」 (strike without
  trailing zeros); `from_broker_option(text) -> OptionTerms | None` for "XYZ 12/18/2026 50.00 P";
  `contracts_to_shares(n)` / `shares_to_contracts(q)` (the only multiplier arithmetic);
  `is_standard_adjustment(from_terms, to_terms, ratio_to, ratio_from) -> bool` (OPT-D11: both
  multiplier 100, letters-only destination root, same right, same expiry,
  `strike_from × ratio_from == strike_to × ratio_to`);
  `TREND_EXCLUDES_OPTIONS_NOTE` (spec §5 sentence).
- [ ] `symbol_format.py`: `MARKET_CODE_PATTERNS["US"]` unchanged; `matches_market_format(sym, US)`
  and `looks_like_market_code` also accept `OPTION_PATTERN`.
- [ ] Models: `Instrument.asset_type: Literal["stock","option"] = "stock"`, `option: OptionTerms |
  None = None`, property `is_option`; `Transaction.close_reason: Literal["expired","assigned",
  "exercised"] | None = None`.
- [ ] Result / wire types with defaults (no behaviour): `portfolio/results.py` `RefusedClose` (row
  id, account, symbol, date, reason), `Book.refused_closes = []`, `Book.realized_close_reasons:
  dict[int, str] = {}` (index into **`Book.realized.rows`** — the replay's full list in emission order —
  → `close_reason`; read only by `option_summary` before any filtering or sorting; `RealizedRow`
  unchanged, it carries no row id),
  `OpenedPosition.unbookable_close = False`, `Holding.unbookable_close = False`; `portfolio/dashboard_models.py` `OptionsBlock` (spec §7 fields) and
  `DashboardData.options: OptionsBlock | None = None`, and in the same commit
  `api/routers/dashboard.py` drops the `options` key from the wire when it is `None` — without the
  pop, `to_wire(data.model_dump())` would emit `"options": null` and the golden equality test would
  fail in W1 already. Grep every other whole-`DashboardData` dump (exports, snapshots, LLM variables)
  and confirm none serialises the new key for a stock ledger.
- [ ] Schema (additive, `_add_column_if_missing`, created at boot — a GET never writes):
  `instruments.asset_type TEXT NOT NULL DEFAULT 'stock'`; `transactions.close_reason TEXT`;
  `option_contracts` per spec §3.
- [ ] Store: `upsert_instrument` writes `asset_type` (explicit column list); `_row_to_instrument`
  reads it and attaches `option` from ONE `option_contracts` read per `list_instruments` call;
  `upsert_option_contract` / `get_option_contract`; transactions insert / update / list carry
  `close_reason`; the `*_order_key` functions unchanged.
- [ ] `register.py`: `register_option_contract(conn, symbol) -> Instrument` — parse, refuse (zh
  `Issue`) non-option / digit root / multiplier ≠ 100 / non-US; defaults per spec §3; idempotent;
  never fetches a quote.
- [ ] `corporate_actions.py`: `CorporateActionKind.OPTION_ADJUST`, `KIND_ZH` 「選擇權合約調整」,
  Pydantic rules (no `cost_carry`), `convert_stored` reads it. Until W2 lands, the replay must not
  let it fall into the SPINOFF arithmetic: W1 adds a refusing branch (「尚未支援」) right after the SPLIT
  branch, which W2 replaces. `test_i14` `_PAIR` regex widened to the fourth kind.
- [ ] Tests (fail first): symbol parse / format / display / broker-string round trip incl. 7.5
  strikes; digit root refused; `is_standard_adjustment` on a 1→3 split, a 1:1 rename, a 3-for-2
  (refused), a digit-root destination (refused), a put → call (refused), a changed expiry (refused); an old database migrates with every instrument
  `stock` and every `close_reason` NULL; registration idempotent with the spec §3 defaults; golden
  untouched.
- **Done when:** targeted tests green, ruff clean, mypy clean on touched modules; commit
  `feat(options): W1 foundation — option terms, schema, registration, OPTION_ADJUST vocabulary`.

## 3. W2 — Replay, valuation, trend, payload (wave 1, subagent A)

**Files:** `portfolio/cost_basis.py`, `portfolio/options.py` (new), `portfolio/dashboard.py`,
`portfolio/timeseries.py`, `portfolio/allocation.py`, `portfolio/{pnl,position_aggregate,dividends}.py`
(the `unbookable_close` readers), `portfolio/dashboard_models.py` (fill the
block), `data_ingestion/holdings.py` (`_delta_of`, `created_by`), `shared/models/ledger.py`
(`opened_by` / `position_opened_by`), `api/replay_guard.py`, `api/routers/performance.py`, `docs/spec/2026-08-06-corporate-actions.md` (§4.4 table row only),
`tests/portfolio/test_corporate_actions.py` (field-count list); tests under `tests/portfolio/`,
`tests/contract/`, `tests/api/`.

- [ ] **Price-0 closes**: prove with tests that the existing arithmetic books spec §4.1's rows (short
  expiry → `short_cover` = remaining premium; long expiry → `sale` = −basis; same-day BTC then expiry
  in write order; partial assignment).
- [ ] **§4.4 refusal**: in `build_book`, a `close_reason` row that would open or extend a lot (a BUY
  beyond the short lot, a SELL beyond the long lot, or a close against the opposite direction)
  books nothing, appends a `RefusedClose`, sets `_Position.unbookable_close` (new field,
  sticky, only when the position exists — the `RefusedClose` record is unconditional), the
  `OpenedPosition` and `Holding` flags; strict path raises `UnbookableLedgerError`. A partial
  `assigned` / `exercised` within the open lot is booked normally. The replay also fills
  `Book.realized_close_reasons` for every realized row it emits from a `close_reason` trade. Add the field's
  row to the corporate-actions spec §4.4 table (EXCHANGE / SPINOFF OR it into Q like
  `unbookable_action`) and to the field-count test. Readers in A's files, each mirroring its
  `unbookable_action` read: valuation withheld (`pnl.py`), `position_aggregate.py`, `dividends.py`,
  trend incomplete from that date (`timeseries.py`), XIRR and B withheld with a reason naming the
  row (`dashboard.py`), `api/replay_guard.py` hard refusal
  `refused_close_correction` naming the row (compared per row, like `orphan`). Test: delete the STO
  of an expired short → the guard refuses; a legacy ledger with the shape → flagged, never a 500.
- [ ] **`OPTION_ADJUST` branch** in `_apply_action` **immediately after the SPLIT branch, before
  the E5 block**, replacing W1's placeholder: spec §4.3 validation-at-replay (source long or short;
  destination flat or same direction, else `_reject`; E22) and the spec §4.3 field table, every
  `_Position` field.
- [ ] **Share walker**: `_delta_of` / `created_by` / `opened_by` move the signed count by the ratio,
  shorts included; opposite signs → skip + `note_negative_side_skip` on both symbols. Test: the same
  ledger through `build_book` and `shares_through` agrees after a 1→3 adjustment of a short.
- [ ] **`portfolio/options.py`**: `last_trade_prices(transactions, actions, as_of)` — per symbol
  across accounts, ordinary trades only, `trade_date ≤ as_of`, predecessor price × `ratio_from /
  ratio_to` across an `OPTION_ADJUST`; `option_summary(...) -> OptionsBlock` per spec §7 (open rows
  incl. 履約義務 and `unbookable_close`; `realized.rows` = the replay's option `RealizedRow`s, one per
  closing event, same shape as the stock `realized.rows`, plus `close_reason` (from
  `Book.realized_close_reasons`) and `contract`; aggregates `by_contract` (how each contract
  closed) / `by_year` / `by_underlying`; `realized_total`;
  `market_value` / `unrealized` per currency and in the reporting currency;
  `trend_excludes_value`, `note`). Add the block's row models to
  `tests/contract/test_frontend_never_computes_money.py`'s model scan. Name its money products so `tests/portfolio/
  test_settled_notional.py`'s scan classifies them (valuation, not settlement), or add reviewed
  allowlist entries.
- [ ] **Valuation seam** (`dashboard.py` `price_reads` / `price_map`): an option's price from
  `last_trade_prices`; options are left out of the freshness use of `price_reads`, so never in
  `freshness.prices`, `missing_prices` or `any_stale`.
- [ ] **Split the lists** (spec §6): `holdings`, `realized.rows` and the realized block's totals,
  sector allocation, weights (`weight = stock MV / Σ stock MV`), `holdings_subtotals`, the
  ex-dividend calendar and dividend projection — stock rows only. KPI 總市值 / 總報酬 / unrealized,
  XIRR's final value, the FX pool's `foreign_stock_value` — stocks + options. 月度成績 needs no change
  (it stores the KPI; OPT-D12). **Compute every stock-only aggregate by feeding the SAME functions
  (`combined_view`, `_holdings_subtotals`, `sector_allocation`) the stock-only `valued` list — never a
  second summation** — so a stock-only ledger takes the byte-identical path (D38: a short-circuit,
  not an equal-answer computation; `Decimal` text can differ where values are equal).
- [ ] **Trend** (`timeseries.py`): skip `is_option` holdings without setting `incomplete`; set
  `options.trend_excludes_value` when an option position existed in the window. TWR
  (`api/routers/performance.py`) and the benchmark comparison read the same flag from `options`.
- [ ] **Payload**: `DashboardData.options` set iff the uncut ledger has ≥ 1 transaction or action
  on an option instrument (the router's `None` pop is W1's).
- [ ] Tests (fail first): each spec §4.1 row through `build_book`; §4.3 transfer table on long and
  short incl. the residue; opposite-direction merge refused; walker agreement; last-trade price
  across accounts and across an adjustment; KPI = stocks + options while `holdings`,
  `realized.rows`, weights and subtotals exclude them (weights sum to 100 %); trend excludes the
  value without `incomplete`; XIRR includes the open option; golden unchanged; a no-option ledger's
  payload has no `options` key; a registered-but-untraded contract produces no block.
- **Done when:** targeted tests green, ruff, mypy on touched modules, architecture tests green;
  commit `feat(options): W2 replay (OPTION_ADJUST, refused closes), last-trade valuation, trend
  exclusion, dashboard options block`.

## 4. W3 — Doors, validators, fees, converter, cash lines (wave 1, subagent B)

**Files:** `data_ingestion/{validate,csv_import,corporate_action_import,opening_import,
import_templates,fees,config_seed,fee_overrides,agents}.py`, `data_ingestion/broker/{ir,schwab,
grouping,convert,reconcile}.py`, `portfolio/cash.py` (`CashLine` fields only),
`api/routers/{input_center,ledgers,cash,fee_rules}.py`, `api/wire.py`; tests under
`tests/data_ingestion/`, `tests/contract/`, `tests/api/`.

- [ ] **Validators** (`validate.py`) — spec §4.2 exactly, through ONE helper
  `option_close_issues(...)` used by every door: `close_reason` only on an option with price 0 / fee
  0 / tax 0 / `short_sale=0`; side derived from the signed *open* (`shares_through(..., on=date,
  pending=siblings booked before ∪ replacement)`, the row never counts) and refused when a row
  contradicts it; `expired` quantity = `|open|`, dated the expiry; `assigned` (open < 0) /
  `exercised` (open > 0) quantity ≤ `|open|`, dated ≤ expiry; any row after expiry refused; a
  same-day row ordered after an `expired` row refused; multiple of 100; the over-cover and the
  wrong-side soft findings; `dividend_on_option`; `validate_corporate_action`: `OPTION_ADJUST` per
  spec §4.3 incl. `is_standard_adjustment` (OPT-D11), SPLIT / EXCHANGE / SPINOFF with an option
  refused, the SPLIT-with-open-options soft finding. Opening import: option symbol refused
  「選擇權請以交易登錄」.
- [ ] **Registration at the doors** (OPT-D7): the CSV transaction and corporate-action doors and the
  manual door call `register_option_contract` for an unregistered OCC symbol, inside the commit's
  transaction; the preview says 「將建立選擇權合約 …」. `quick_register` is never used for options.
- [ ] **CSV**: `close_reason` joins `TRANSACTION_COLUMNS` (optional); template example rows (a
  sell-to-open, an expiry) commit clean; re-pin the template header test and the DEF-026 export /
  re-import decision for `close_reason`; the corporate-action CSV accepts `OPTION_ADJUST` /
  「選擇權合約調整」.
- [ ] **Fees** (OPT-D6): `FeeRuleSet.option_per_contract: Decimal | None`,
  `option_reg_per_contract: Decimal = 0`; `compute_fees(..., is_option=)` routes an option to a new
  `_us_option` branch that bypasses `_us` entirely (no share-based TAF / CAT / platform / settlement /
  MY stamp): per-contract × contracts (+ reg × contracts) + SEC on a sell (existing rule, notional
  basis); snapshot `{"option": true, "contracts": N, ...}`. `schwab` 0.65; `moomoo_my` None → an
  option trade without a supplied fee gets the hard 「此帳戶未設定選擇權手續費，請填入對帳單手續費」. Supplied
  fees win (existing path). Both fields owner-editable (`fee_overrides`, `api/routers/fee_rules.py`,
  `api/wire.py::fee_rules_wire`).
- [ ] **Manual door** (`input_center.py`): `ManualBody.contracts` for an option (the server converts
  with `contracts_to_shares`; `quantity` stays the stock field); `ManualBody.close_reason`; the four
  sides map to side + `short_sale`; the server derives an expiry's side; option mode refuses
  「買進平倉」 with no short 「無空頭部位可平倉，請改選買進開倉」 and 「賣出平倉」 with no long or beyond the
  long 「無多頭部位可平倉，請改選賣出開倉」 (CSV / edit rows keep the stock rules);
  `/api/input/context` instruments carry `asset_type`; the preview mirrors the
  replay branch and withholds its position math on `unbookable_close` (as on `oversold`); the
  two-leg 「被指派／履約」 commit: both rows or neither, one acknowledgement covers the pair, every
  finding names its leg; the stock leg's underlying registers through the existing auto-register
  path (quote gate applies), and a failure refuses the pair 「請先登錄標的 XYZ」.
- [ ] **Edit / delete doors** (`ledgers.py`): `GET /api/ledgers/transactions` rows gain
  `close_reason` and `contracts` (null on a stock); the transaction edit door runs the same helper
  (with `replacement`); the corporate-action create / edit / delete doors accept `OPTION_ADJUST` (the
  replay guard's new refusal is A's).
- [ ] **Cash lines**: `CashLine` gains `short_sale` and `close_reason` (`portfolio/cash.py` only
  copies them from the transaction); `api/routers/cash.py` puts them on the statement wire.
- [ ] **Schwab converter** (pure; the CSV door registers): option rows stop being dropped
  (`grouping.py` routing, `convert.py` `option_row_unsupported`); `from_broker_option` → OCC;
  Sell to Open → SELL `short_sale=1`; Buy to Close / Buy to Open → BUY; Sell to Close → SELL; fees
  verbatim, tax 0; `Expired` and TD 「REMOVAL OF OPTION DUE TO EXPIRATION」 (re-map `schwab.py`'s
  journal rule and fix `ir.py`'s comment) → price-0 `expired`, dated the contract's expiry, side from
  the running signed position in the file (no opening → `UnmappedRow`), same-day rows per contract
  merged, written after that day's trades; odd-split / position-change journal pairs → one
  `OPTION_ADJUST` when `is_standard_adjustment` holds, else `UnmappedRow` 「非標準交割，第一版不支援」;
  an unpaired half raises; platform transfers of an option → dropped with a stated reason;
  `Assigned` / `Exercise` stay `UnmappedRow`; the reconcile advisory text updated (the
  `web/broker-import.js` labels are D's — B exports the new reason codes).
- [ ] Tests (fail first): every validator rule with its zh message (side derivation, expiry
  quantity with same-day siblings above and below, after-expiry refusal, over-cover finding, the
  edit door with `replacement`); CSV round trip of the raw transactions export with option rows;
  template rows commit clean; fees per account; the two-leg commit writes both or neither; the
  converter on a synthetic Schwab file with every option row shape (STO / BTC / BTO / STC / Expired
  "as of" / TD removal / a standard odd-split pair / a non-standard pair that must raise / a
  position-change pair / a platform transfer / an Assigned row that must raise).
- **Done when:** targeted tests green, ruff, mypy on touched modules; commit `feat(options): W3 doors —
  validators, CSV + manual + converter, per-contract fees, option cash lines`.

## 5. W4 — Fetch scope and stock-reader exclusions (wave 1, subagent C)

**Files:** `shared/instrument_scope.py`, `api/instrument_service.py`, `scheduler/jobs.py`
(`refresh_instrument_quote` only), `api/fundamentals_service.py`, `api/news_service.py`,
`strategy/{rebalance,whatif,alerts}.py`, `api/{alert_inputs,signals_service,insight_service,
digest_service}.py`, `api/routers/{strategy,instruments,prompts,actions}.py`,
`llm_insight/variables.py`, `data_ingestion/dividend_inbox.py` (only if it reads its own universe);
new `tests/contract/test_options_excluded_from_stock_readers.py`.

- [ ] `TRACKED_SQL` / `tracked_instruments`: `asset_type = 'stock'`.
- [ ] The five bypass paths refuse an option with a stated reason, never a provider call:
  `quick_register`'s quote gate + history fetch, the gap backfill (`gap_backfill` and
  `_gap_backfill_inner`), `refresh_instrument_quote`, the Alpha Vantage held universe, held-only news.
- [ ] Readers (OPT-D10). **`data.holdings` is stock-only by construction (W2)**, so a reader that
  only reads `data.holdings` needs no filter — the scan test proves it rather than assuming it.
  Filter where the reader reads the registry or the book: target weights, rebalance, what-if,
  alerts (single weight, sector, drift), alert inputs, signals, insights, LLM variables, prompts
  preview, digest (incl. `_chores` staleness), news, fundamentals; `/api/instruments` elements gain
  `asset_type`; the watchlist list hides options unless `include_options=1`; archive / remove / purge unchanged (`holds_position`).
- [ ] **Scan test**: every module that iterates `book.holdings`, calls `held_among` /
  `holds_position` / `held_in_book`, reads `tracked_instruments`, or is `strategy/whatif.py` must
  apply the stock filter or be a reviewed allowlist entry with its reason.
- [ ] Tests (fail first): a ledger holding a stock and an option — each reader names the stock only;
  mocked providers assert no call for the option; the quote job's outcome stays `ok` with an option
  held.
- **Done when:** targeted tests green, ruff, mypy on touched modules; commit `feat(options): W4
  options out of every fetch scope and stock reader`.

## 6. W5 — Web UI, drawer, what's-new (wave 2, subagent D)

**Files:** `web/index.html`, `web/app.js` (or a new `web/options.js` + its script tag),
`web/charts.js`, `web/ledger.js`, `web/trades.html`, `web/input.js`, `web/corp-action-form.js`,
`web/broker-import.js`, `web/detail.js` (the drawer), `web/instruments.js`, `web/cash.js`, the what's-new
catalog, `api/routers/symbol.py`; tests under `tests/contract/`, `tests/e2e/`.

- [ ] Dashboard 選擇權 section after 持倉明細 (`data-screen-label="選擇權"`): 未平倉 and 已實現 (by year /
  by underlying, expandable rows), subtotals, the note; rendered only when the payload has `options`.
  KPI 「其中選擇權」 line from `options.market_value`; the 已實現損益 section's separate option line
  (not summed); the trend / B / TWR / benchmark note when `options.trend_excludes_value`.
- [ ] Ledger 交易 tab: contract display name, 口數 beside share-equivalents (both from the server), the
  seven chips; 公司行動 tab and form: `OPTION_ADJUST`.
- [ ] Manual entry option mode: OCC symbol or contract picker (underlying / expiry / strike / C-P →
  symbol), sends `contracts` (no multiplier in JS), the four sides, 「到期作廢」, 「被指派／履約」
  (two-leg), preview in 口 and share-equivalents.
- [ ] Drawer for a contract (`symbol.py` reads `options.open`): header, trades in 口, no 配息史,
  「選擇權不提供試算」, 待釐清 + remedy on `unbookable_close`; the reconciliation footer (`_reconcile`)
  takes the book's share count from the contract's `options.open` rows (signed share-equivalents),
  since `data.holdings` has no option rows — e2e: an open contract's footer is green. The 選擇權
  section badges `unbookable_close` as `web/app.js` badges `unbookable_action` on holdings.
- [ ] Watchlist 「顯示選擇權合約」 filter; broker-import labels for the new reason codes; cash page
  descriptions from `short_sale` / `close_reason`.
- [ ] What's-new catalog entry saying where to verify.
- [ ] Static tests stay green (`?v=` stamps, `names.js` before `api.js`, page lists,
  `ledger-count-ok`, state colours as classes). The DEF-056 future-row sweep: the e2e seed must not
  date an expiry on the sweep's seeded future day, and the sweep's docstring records that 到期日 is a
  contract term, not a future ledger row. e2e: the section renders from a seeded option ledger;
  option-mode entry STO → BTC end to end; the section is absent on the golden ledger; no horizontal
  scroll at phone width.
- **Done when:** targeted contract + e2e chunks green, ruff, mypy on `symbol.py`; commit
  `feat(options): W5 選擇權 section, ledger chips, option-mode entry, drawer, what's-new`.

## 7. W6 — Exports (wave 2, subagent E)

**Files:** `export/{holdings,holdings_report,realized,tax,ledgers_report,cash_statement,
symbol_detail,rebalance_report}.py`, `api/routers/export.py` if a builder signature changes; tests
under `tests/export/`, `tests/contract/`.

- [ ] 持股 CSV / 持股報表: stock rows unchanged; a 選擇權 block and the 「其中選擇權」 line only when
  `options` is present.
- [ ] 已實現損益 CSV: rows from `realized.rows` + `options.realized.rows` (same columns, one per
  closing event — the tax package's granularity); `asset_type`, `contract` appended; re-pin `tests/contract/test_export_realized.py`'s header.
- [ ] 稅務包: contract name on option rows; 「其中選擇權」 subtotal; pin that option closes land in
  capital gains (`export/tax.py` kind filter).
- [ ] 帳本報告: 口數 + chips; `OPTION_ADJUST` in the actions section.
- [ ] 現金收支明細 CSV / print: option descriptions from `CashLine.short_sale` / `close_reason`.
- [ ] 個股明細 CSV for a contract; 再平衡報告 excludes options.
- [ ] Raw zip / per-tab CSV: `close_reason` rides `SELECT *`; re-import round trip.
- [ ] Every other export of a no-option ledger byte-identical.
- **Done when:** targeted tests green, ruff, mypy; commit `feat(options): W6 exports`.

## 8. W7 — Oracle, manual, acceptance

**W7a — stress-audit oracle (wave 2, subagent F; `scripts/stress_audit/*` only).** Transcribe from
the **spec**, never import the app (`grep -E '^\s*(import|from)\s+portfolio_dash' oracle.py` prints
nothing):
- [ ] `common.load_facts_from_db` reads `close_reason`; option terms parsed from the symbol (its own
  parser, per spec §3), not from `option_contracts`.
- [ ] `oracle.py`: price-0 closes and the §4.4 refusal; `OPTION_ADJUST` (`_KINDS`, its own branch,
  `replay()`, `integrity_findings`); an option branch in `fee_tax` (per-contract + SEC on sells);
  last-trade valuation; the trend exclusion.
- [ ] Phase-1 scenario ops: STO→BTC · STO→expire · BTO→STC · BTO→expire · 1→3 adjustment on a short
  · 1:1 rename · sold-put assignment (option + stock legs) · same-day BTC + expiry · cash ties per
  pool. Assertions: realized per contract, the open position, cash, KPI = stocks + options, trend
  excludes the option value.
- **Done when:** phase 1 green; the new baseline (ops / pass / fail) recorded; commit
  `test(stress-audit): options — oracle transcription + scenario ops`.

**W7b — wave 3, main session:**
- [ ] Cross tests at the wave-1 merge (B's validators over an adjusted contract; an expiry after an
  adjustment).
- [ ] Accounting manual zh + en: an options section (each spec §4.1 row's formula, valuation, trend
  exclusion, fees) with its verification anchor = the phase-1 run; version bump + history row.
- [ ] Rule files: `domain-ledger.md` (options mapping, `close_reason`, `OPTION_ADJUST` vs
  `EXCHANGE`, §4.4 refusal, R5), `data-and-pricing.md` (option terms, last-trade valuation never
  written to `prices`, share-equivalent quantity), `markets-and-fees.md` (option fees),
  `architecture.md` (the two new modules, the stock-reader exclusion seam).
- [ ] CHANGELOG `[Unreleased]`: rulings R1–R7, OPT-D1..D12 and the feature; integrity count
  unchanged (`grep -c "^## \[v"` = 30).
- [ ] Final review (code review at xhigh on `main..feat/options-ledger`), fixes with fail-before
  tests, then the heavy gates once.
- [ ] **Real-data acceptance** (local, throw-away database, real data never committed): the owner's
  Schwab option rows through the converter and the CSV door — option realized, the open contract and
  the option cash total equal the git-ignored `OPTIONS_ledger_proposals.md`. The full-history cash
  target follows the trade / dividend / opening conversion (the next work item).
- [ ] Report to the owner. Demo deploy on the owner's word; the owner has pre-authorised its additive
  schema migration (2026-10-09). Back up and verify the demo DB first; prod only on 「上」.

## 9. File ownership (no two subagents in one file)

| Wave 1 — A (W2) | Wave 1 — B (W3) | Wave 1 — C (W4) |
| --- | --- | --- |
| `portfolio/{cost_basis,options,dashboard,timeseries,allocation,dashboard_models,pnl,position_aggregate,dividends}.py`, `data_ingestion/holdings.py`, `shared/models/ledger.py`, `api/replay_guard.py`, `api/routers/performance.py`, corporate-actions spec §4.4 row, `tests/portfolio/test_corporate_actions.py` | `data_ingestion/{validate,csv_import,corporate_action_import,opening_import,import_templates,fees,config_seed,fee_overrides,agents}.py`, `data_ingestion/broker/*`, `portfolio/cash.py`, `api/routers/{input_center,ledgers,cash,fee_rules}.py`, `api/wire.py` | `shared/instrument_scope.py`, `api/instrument_service.py`, `scheduler/jobs.py`, `api/{fundamentals_service,news_service,alert_inputs,signals_service,insight_service,digest_service}.py`, `strategy/*`, `api/routers/{strategy,instruments,prompts,actions}.py`, `llm_insight/variables.py`, `data_ingestion/dividend_inbox.py` |

| Wave 2 — D (W5) | Wave 2 — E (W6) | Wave 2 — F (W7a) |
| --- | --- | --- |
| `web/*`, `api/routers/symbol.py`, what's-new catalog | `export/*`, `api/routers/export.py` | `scripts/stress_audit/*` |

Main session only: `CHANGELOG.md`, `api/app.py`, `tests/conftest.py`, `.claude/rules/*`, the
accounting manuals, worktree merges, cross tests. `api/snapshots.py` needs no change (OPT-D12).

## 10. Risks and how the plan meets them

| Risk | Mitigation |
| --- | --- |
| A stock reader nobody listed still sees an option | W4's scan test; `data.holdings` stock-only by construction |
| A new wire field changes the golden payload | invariant 1: new fields only inside `options`; the router pops `None` |
| A close left behind by an edit / delete books a phantom lot | spec §4.4 refusal in the replay + the replay guard's hard refusal |
| The share walker and the replay disagree on `OPTION_ADJUST` | A owns both; an agreement test |
| Same-day order (BTC then expiry) | price-0 closes are trades in write order; the after-expiry and same-day rules; the converter writes expiries last |
| A non-standard adjustment booked at the wrong deliverable | OPT-D11 in the validator, the converter and the symbol pattern |
| 100× quantity error | multiple-of-100 at every door; `contracts` on the manual door; 口 shown beside share-equivalents |
| An option symbol reaches a provider | one fetch-scope seam + five guarded bypasses + mocked-provider tests |
| The oracle copies the app | independence grep; transcribed from the spec, own fee branch |
| Demo data | backup + verify before the deploy; prod only on 「上」 |

## Appendix — code map (verified at `aa5c80a`; re-check line numbers before editing)

**Replay** — `portfolio/cost_basis.py`: `_reject` / `UnappliedAction` :225-263; `_apply_action`
def :266; E1 :298, E2 :304, E3 :310; SPLIT scales both lots :315-324; **E5 block :326-331** (short
source refused), E18 :338-341, E22 :346; every other kind falls into the SPINOFF arithmetic
:391-415. `_Position` (17 fields) :70-155 — long lot + separate positive short lot :83-89; BUY covers
first, `kind="short_cover"` :714-751; declared SELL :752-785; dividend on an open short refused
:860-880; signed output :1007-1009; `opened_positions` :1014-1016; 0-share skip :1029-1030;
`short_open` :1058; qty < 0 raises / 0 skipped :643-710; sort key :592-603.
`shared/ledger_events.py` priorities OPENING 0 · CORPORATE_ACTION 10 · TRADE 20 · DIVIDEND 40
:46-53. `portfolio/results.py` `RealizedRow.kind` :166. Field-count test
`tests/portfolio/test_corporate_actions.py` :352; the corporate-actions spec §4.4 table :532-585.

**Action vocabulary** — `shared/corporate_actions.py` `CorporateActionKind` + `KIND_ZH` :36-52,
`is_ratio_term` :68-78, validator :111-131, `convert_stored` def :240 (reads at :266/:276);
`schema.py` `kind` comment :63, `to_symbol NOT NULL` :65. Registration points: `validate.py`
`unknown_action_kind` :1145-1150 and E-codes :1161-1549; `corporate_action_import.py` columns
:61-64, `_KIND_ALIASES` :71-76, dispatch :379/:389; `api/routers/ledgers.py` `_KIND_ALIASES` :1101,
dispatch :1754/:1887/:2041/:2069; `api/routers/symbol.py` role :345-350; `store.py`
`insert_corporate_action` :1452/:1474; `import_templates.py` :88/:154-169; `broker/grouping.py`
`_ACTION_FAMILY` :502-508; web `corp-action-form.js` :133-140/:686-703, `ledger.js` :1020,
`input.js` :1115, `broker-import.js` :82-88, `trades.html` :448-449;
`tests/contract/test_i14_one_action_vocabulary.py` `_PAIR` :35.

**Share walker** — `data_ingestion/holdings.py` `_delta_of` :287-352 (negative side skipped for
EXCHANGE / SPINOFF :328-333, SPLIT exempt :324-327), `shares_through` :450-474, `created_by` :676;
`shared/models/ledger.py` `opened_by` :319. Validator helpers `validate.py` defs
`pending_share_flows` :475, `siblings_booked_before` :511, `_replacement_flows` :534 (call sites
:803-832); the 賣超 guard :778-873.

**`unbookable_action` readers (mirror for `unbookable_close`)** — `Holding.unbookable_action`
`portfolio/results.py` :105; `portfolio/pnl.py` :39; `timeseries.py` :391; `position_aggregate.py`
:172 (→ drawer `api/routers/symbol.py` :259); `dividends.py` :85; `input_center.py` :228/:252;
`web/app.js` :702/:733; `web/detail.js` :558-583. Drawer reconciliation `symbol.py` `_reconcile`
:507-553. Money-arithmetic scan `tests/contract/test_frontend_never_computes_money.py` :34.

**Replay guard** — `api/replay_guard.py` block codes :78 (`oversell` | `orphan`), `_replay_block`
:287-363, orphan → 422 `orphan_correction` :394-396.

**Validators** — `validate.py` `transaction_structural_issues` price / qty ≤ 0 :629-636;
`whole_share_issue` TW/MY only :431-441 (US not checked :420-421); declared short exempt from the
oversell guard :778; `_dividend_findings` :2231-2440 (no instrument-type check).

**Instruments** — model `shared/models/assets.py` :38-70; DDL `schema.py` :9-20,
`_add_column_if_missing` :104-109; `store.upsert_instrument` :162-227, `_row_to_instrument` :580,
`get_instrument` :623, `list_instruments` :634; `register.py` `register_instrument` :33-79,
`autoregister_spinoff_child` :82, `spinoff_child_draft` :119-135; `quick_register`
`api/instrument_service.py` :462-598 (quote gate :544-558); routes `api/routers/instruments.py`
POST :543-617 (gap backfill scheduled :613), quick :630-660, `{symbol}` paths :663/:730/:761/:801;
`symbol_format.py` :25-29; CSV refuses unregistered symbols `csv_import.py` :289-306
(`opening_import.py` :210-217); manual auto-register `input_center.py` :1124-1179.

**Fetch scope** — `shared/instrument_scope.py` `TRACKED_SQL` :38, `tracked_instruments` :50-71;
`scheduler/jobs.py` held hook :470-477, `_quote_outcome` :480-530, `build_worklist` :1565-1582,
`refresh_instrument_quote` :1585; `api/app.py` :243 → `api/routers/actions.py` :34-57;
`pricing/ingest.py` :55-80; bypasses: `instrument_service.py` `_gap_backfill_inner` :417-435,
`gap_backfill` :438, quote gate + history :544-598; `api/fundamentals_service.py` :24-37;
`api/news_service.py` :141/:164; `yfinance_provider.py` :39-53.

**Valuation and payload** — `portfolio/dashboard.py` `build_dashboard` :624-1200; price reads
:713-735 (`pricing/store.get_latest_price` :291-316); XIRR reason :844-845; FX stock value :858-868
→ `forex/fx_pnl.py` :275/:329 → `compute_account_fx` :138-217; ex-dividend calendar :940-957;
`combined_view` → :961/:1121; row weight :967-973 (+ :1137-1150); stale row :992-993; cash series
:1046-1068; `total_return` → :1078-1089; freshness :1173-1174; subtotals :317-390;
`fx_complete_return` :489-533; benchmark :539. freshness `prices` :1151-1156; `pnl.py` no price :55-66; `returns.py`
`total_return` :39-87, XIRR all-or-nothing :388-396; `allocation.py` :16-65.
`portfolio/dashboard_models.py` `TrendPoint` :211-221, `exclude=True` precedent :427-443.
`api/routers/dashboard.py` `to_wire(data.model_dump())` :47. Golden equality
`tests/contract/test_spec17_financials.py` :613. 月度成績 `web/app.js` :1018-1025 →
`api/snapshots.py` :44-63 (KPI `total_market_value`).

**Trend** — `portfolio/timeseries.py` :282-445 (per holding :382-433, short counted :405-409, no
price → incomplete :410-413), `net_invested` flows :198-204; frontend note `web/charts.js`
:68-70/:121-122, `web/index.html` :81.

**Stock readers** — target weights `api/routers/strategy.py` :179-200; rebalance
`strategy/rebalance.py` :178-265; what-if `strategy/whatif.py` :473-481; alerts
`strategy/alerts.py` :288-300/:438-441; `api/alert_inputs.py` :130/:156; signals
`api/signals_service.py` :178-205; insights `api/insight_service.py` :124-167 (`held_in_book`
:137)/:1385/:1606; `llm_insight/variables.py` :590/:789-791/:901/:970-975; digest
`api/digest_service.py` :205-223/:507/:618-628; watchlist `api/routers/instruments.py` :61-78,
`data_ingestion/holdings.py` :564-620; prompts `api/routers/prompts.py` :419.

**Doors** — `csv_import.py` `TRANSACTION_COLUMNS` :55-58, import-only :66, unknown columns
:177-200/:587-589, `short_sale` :614, fee compute vs supplied :319-387; `fees.py`
`supplied_snapshot` :103-134, `_us` :208-255, `compute_fees` :297-336, settled notional :322-327;
`config_seed.py` `FeeRuleSet` :42-54, schwab :134-143; `fee_overrides.py` :46-60/:80-86;
`api/wire.py` `fee_rules_wire` :163-185; `/api/input/context` `input_center.py` :139-167;
`/api/ledgers/transactions` rows `ledgers.py` :262-290; `/api/instruments` elements
`instruments.py` :91-135; `input_center.py` `ManualBody` :402-443 → `TxnInput`
:446-454, `_cash_overdraft_issue` :560-595, `manual_preview` :1026-1102, commit :1106+; cash lines
`portfolio/cash.py` :283-289, `api/routers/cash.py` :615-640, `web/cash.js` :345-346; UI
`trades.html` :102-114, `input.js` :193-211/:252/:284-318/:451-534. Converter: `broker/ir.py`
`OPT_*` :76-86, `OPTION_KINDS` :117-120, `option_symbol` :170-172; `schwab.py` option rules
:95-100, journal rules :120-122, option detection :305-321, `UnmappedRow` :298-303; `grouping.py`
:699-700; `convert.py` `UNCONVERTIBLE` :63-69, trade rows :160-192, `option_row_unsupported`
:349-354, CSV out :435; `reconcile.py` :453-459; `api/routers/broker_import.py` :15.

**Web** — nav `web/shell.js` :9-18; `web/index.html` 持倉明細 :126-138, 已實現損益 :183; `web/app.js`
`renderHoldingsHead` :522, `renderHoldings` :659, dashboard fetch :1624; ledger tabs `trades.html`
:369-468, `LEDGER_TABS` :498; `ledger.js` `renderTx` :837-900, `renderActions` :1290, `renderCash`
:1350, keyword filter :105-110. Enumerating tests: `test_static_cache_discipline.py` :43-54,
`test_names_js_before_api_js.py` :63-77, `test_def044_account_tokens_every_page_flow.py`,
`test_no_horizontal_scroll.py` `_ALL_PAGES`, `test_p3_batch3_wave3.py` `_TOOLBAR_PAGES`,
`test_whatsnew_api.py` :77, `test_ledger_count_not_hardcoded.py`, `test_ledgers_cash_tab.py`
:79-89, DEF-056 sweep `tests/e2e/test_def056_r5_future_row_sweep_flow.py` :96-147.

**Exports** — `export/holdings.py` :61-63; `holdings_report.py` :147/:203/:282; `realized.py`
:25-45 reads `data.realized.rows` :33, header pinned `tests/contract/test_export_realized.py` :28;
`tax.py` per-event realized rows :103-116 (capital-gains kind filter :116); `ledgers_report.py` `_side_chip` :110,
`_transactions_section` :185, `_actions_section` :379; `cash_statement.py` `_describe` :198;
`symbol_detail.py` :58; `rebalance_report.py` :322/:337; raw zip = `shared/ledger_registry.py`
`LEDGER_TABLES` (no `instruments`); export → import decisions
`tests/contract/test_def026_ledger_export_reimports.py` :291.

**Oracle** — `scripts/stress_audit/common.py` `load_facts_from_db` :501-565; `oracle.py`
`fee_tax` :218-235, `RealizedRow` :505-520, `EventPriority` :559-577, `_Pos` :581-596,
`_apply_action` :709-861 (`_KINDS` :633-634, unknown → "E-kind" :769-770), `replay()` cover
:947-960 / declared short :972-993 / emit :1059-1070, `integrity_findings` :1574+ (short walk
:1658-1664); README independence rule :244. Settlement scan `tests/portfolio/test_settled_notional.py`
:122-173.
