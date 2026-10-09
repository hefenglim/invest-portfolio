# Options ledger (P3, approach ④) — Implementation Plan

> **Spec:** `docs/spec/2026-10-09-options-ledger.md` (APPROVED 2026-10-09: R1–R7, OPT-D1..D22).
> **Branch:** `feat/options-ledger`, off `main` at `aa5c80a`. **Status:** PLAN for owner review —
> revised after review rounds 1–4 (Fable 5.1) and the pre-build review (Opus 5.5, GO-WITH-FIXES,
> folded). No code until the owner approves (owner: approve → compact the context → 開工).
> Execution: subagent-driven, **at most 3 parallel subagents**, worktree-isolated, disjoint files.
> **This plan + the spec + the code are the whole brief** — the review history is not needed to build.
> Line numbers in the appendix were verified at `aa5c80a`; re-check before editing.

## 0. Goal and invariants

**Goal.** US equity options as the spec defines them. The owner's goal behind it: the app's Schwab
USD cash ends at the broker's figure once the whole real history is in.

**Invariants (a violation is a bug, not a choice):**

1. **Blast radius (D38, spec §10).** A ledger with no option is byte-identical in every API payload
   and export **except the additive changes spec §10 lists**, each re-pinned in the same commit
   wherever a test pins it. The golden payload does not change: no existing dashboard model gains a
   field; every new dashboard field lives in `options`, popped when `None`; `Holding.unbookable_close`
   never reaches `HoldingRow`; every `+ options` addition is skipped when the block is `None`;
   stock-only aggregates feed the SAME functions (`combined_view`, `_holdings_subtotals`,
   `sector_allocation`) the stock-only list.
2. **Money.** Decimal end to end; the frontend only formats server strings; the multiplier and the
   OCC encoding live only in `shared/options.py`; the browser never composes a symbol, a premium total
   or a 口 ↔ 股當量 conversion.
3. **Layering.** `shared/options.py` imports nothing internal; `portfolio/options.py` is calc core;
   `portfolio/cost_basis.py` and `portfolio/results.py` stay leaves; the expiry detector lives in
   `api/`; the converter stays pure (`known_positions` injected, a required argument).
4. **`prices` is never written for an option.**
5. **Stocks unchanged** — every new branch is gated on `is_option`, `close_reason` or `OPTION_ADJUST`;
   `pool_lines` / `cash_balances` (the guards' arithmetic) gain only an identity field.
6. **One definition per rule** — symbol parse / compose / display and the multiplier,
   `classify_option_rows`, the §6.1 vocabulary, `is_option`, last-trade price, the trend note, the
   close-row helper. The oracle transcribes from the spec.
7. **Project rules** — a GET never writes; a state colour is a class; no native dialogs; the DEF-056
   sweep's seeded day is never an expiry.

**Process.** Fail-before tests for every behaviour. While fixing: ruff + targeted tests only. **Heavy
gates once, after the final review**, one at a time: ruff · mypy --strict (fresh `--cache-dir`) ·
pytest non-e2e (batch totals = `--collect-only`) · e2e in ≤ 11-test chunks · stress phase 1. Use
`C:\AI-Workspace\invest-portfolio\.venv\Scripts\python.exe` from the worktree root. Worktree subagents
come up on `main`: each first runs `git reset --hard <wave base sha>` and reports its merge-base.
Subagents never touch `CHANGELOG.md`, `api/app.py`, `tests/conftest.py`, `.claude/rules/*` or the
accounting manuals. One owner per shared test registry: `tests/portfolio/test_settled_notional.py`
and `tests/contract/test_frontend_never_computes_money.py` belong to A; another package that needs an
entry asks the main session at the merge. Scoped `git add`; the session's commit trailer. Merge by
cherry-pick; re-run the seam tests after each.

## 1. Waves and dependencies

| Wave | Who | Packages | Base |
| --- | --- | --- | --- |
| 0 | main session | **W1** foundation | the branch tip the owner approves (≥ the commit carrying this plan) |
| 1 | 3 subagents | **A = W2** replay + valuation + trend + `options` block · **B = W3** doors + validators + fees + converter + cash lines + input wires · **C = W4** fetch scope + registration refusals + stock-reader exclusions | W1 tip |
| 2 | 3 subagents | **D = W5** web UI · **E = W6** exports + expiry inbox backend · **F = W7a** stress oracle + scenario ops | merged wave-1 tip |
| 3 | main session | `app.py` wiring, cross tests, inbox e2e, rule files, CHANGELOG, manual (W7b), final review, fixes, heavy gates, local real-data acceptance | merged wave-2 tip |

**Dependencies (nobody waits inside a wave):**
- W1 defines every shared type and pure helper (§2) — including `Transaction.id`, the pinned
  `RefusedClose`, `CashLine.txn_id` / `option_side` fields, `OptionsBlock` with its spec §7.1 keys,
  the classifier, and the inbox route contract is fixed by spec §7.2 (D and E build against it).
- B's validators read `holdings.shares_through` and the replay; their `OPTION_ADJUST` and
  refused-close behaviour is A's. B's tests do not cross an adjustment or a refused close until the
  wave-1 merge. C's reader tests that read `data.holdings` rely on A's stock-only split — C tests its
  own filters (registry / book) and the main session runs the `data.holdings` cases at the merge.
- `unbookable_close` readers mirror `unbookable_action`'s: A (`pnl.py`, `position_aggregate.py`,
  `dividends.py`, `timeseries.py`, dashboard reasons, replay guard), B (`input_center.py` preview), D
  (`web/app.js`, `web/detail.js`).
- Wave 2 needs A's block (D, E), B's wires and close-row helper (D, E). The inbox router is wired into
  `app.py` only in wave 3, so D's inbox e2e runs in W7b; D unit-tests its panel against the §7.2
  contract with a stubbed fetch.

## 2. W1 — Foundation (wave 0, main session)

**Files:** create `portfolio_dash/shared/options.py`; modify `shared/symbol_format.py`,
`shared/models/assets.py`, `shared/models/ledger.py`, `shared/corporate_actions.py`,
`portfolio/results.py` (types), `portfolio/dashboard_models.py` (types), `portfolio/cash.py` (the two
`CashLine` fields only), `portfolio/cost_basis.py` (the placeholder branch only),
`data_ingestion/schema.py`, `data_ingestion/store.py`, `data_ingestion/register.py`,
`api/routers/dashboard.py` (`None` pop only), `tests/contract/test_i14_one_action_vocabulary.py`;
tests under `tests/shared/`, `tests/data_ingestion/`.

- [ ] **`shared/options.py`** (header `TODO(options-⑤)` → spec §12): `MULTIPLIER = 100`;
  `OPTION_PATTERN` (letters-only root); `OptionTerms` (underlying, expiry, strike, right, multiplier);
  `parse_option_symbol`; `option_symbol(terms)` (the only OCC composer); `option_display_name`;
  `from_broker_option("XYZ 12/18/2026 50.00 P")`; `contracts_to_shares` / `shares_to_contracts`;
  `is_standard_adjustment(from_terms, to_terms, ratio_to, ratio_from)` (OPT-D11 incl. `ratio_from ==
  1`); `OptionSide` (every spec §6.1 value) and `classify_option_rows(rows, actions) -> dict[int,
  OptionSide]` — rows `(id, account, symbol, trade_date, side, quantity, short_sale, close_reason)`,
  actions `(id, account, date, from_symbol, to_symbol, ratio_to)` in ledger-id order; ONE walk per
  account in replay order `(date, priority, id)` keeping a signed count and an oversold flag per
  (account, symbol); an adjustment moves count × `ratio_to`; the walk mirrors the replay's refusals
  (spec §6.1) — the ONE definition; the §6.1 zh constants; `TREND_EXCLUDES_OPTIONS_NOTE`;
  `PRINCIPAL_FX_OPTION_NOTE` (spec §5).
- [ ] `symbol_format.py`: `MARKET_CODE_PATTERNS["US"]` unchanged; `matches_market_format(sym, US)` and
  `looks_like_market_code` also accept `OPTION_PATTERN`.
- [ ] Models: `Instrument.asset_type` (default `"stock"`), `is_option = asset_type == "option"`,
  `option` = `parse_option_symbol(symbol)` when `is_option` (OPT-D22 — no table);
  `Transaction.id: int | None = None`, `Transaction.close_reason`.
- [ ] Types with defaults: `RefusedClose` (spec §3 fields), `Book.refused_closes`,
  `Book.realized_close_reasons`, `OpenedPosition` / `Holding.unbookable_close`; `CashLine.txn_id` and
  `CashLine.option_side` (both default `None`; `pool_lines` sets `txn_id` on trade lines);
  `OptionsBlock` with the spec §7.1 keys and `DashboardData.options = None`; `api/routers/dashboard.py`
  drops `options` from the wire when `None` (same commit — else the golden fails); grep that no other
  whole-`DashboardData` dump serialises it.
- [ ] Schema (additive, at boot): `instruments.asset_type`, `transactions.close_reason`.
- [ ] Store: `upsert_instrument` writes `asset_type`; `_row_to_instrument` reads it (terms from the
  symbol); transactions insert / update / list carry `close_reason`; **`load_ledger_bundle` passes
  `id` and `close_reason` into `Transaction`**; order keys unchanged.
- [ ] `register.py`: `register_option_contract(conn, symbol)` — parse, refuse non-option / digit root /
  non-US; spec §3 defaults; `asset_type='option'`; idempotent; no quote.
- [ ] `corporate_actions.py`: `OPTION_ADJUST`, `KIND_ZH` 「選擇權合約調整」, no `cost_carry`,
  `convert_stored`; `cost_basis.py`: a refusing placeholder branch right after SPLIT (「尚未支援」) until
  W2. `test_i14` `_PAIR` widened to the fourth kind.
- [ ] Tests (fail first): parse / compose / display / broker string incl. 7.5 strikes; digit root
  refused; `is_standard_adjustment` (1→3 ok, 1:1 ok; 3-for-2, reverse, `ratio_from > 1`, digit root,
  put→call, changed expiry refused); `classify_option_rows` over every §6.1 value incl. both over-
  cover / over-sell labels, a refused close, a refused adjustment, and STO 1 口 → 1→3 → BUY 3 口 =
  買進平倉; migration keeps every instrument `stock` and every `close_reason` NULL; registration
  defaults; `load_ledger_bundle` carries `id`; golden untouched.
- **Done when:** targeted tests, ruff, mypy on touched modules; commit `feat(options): W1 foundation`.

## 3. W2 — Replay, valuation, trend, the `options` block (wave 1, subagent A)

**Files:** `portfolio/{cost_basis,options(new),dashboard,timeseries,allocation,dashboard_models,pnl,
position_aggregate,dividends}.py`, `data_ingestion/holdings.py` (`_delta_of`, `created_by`),
`shared/models/ledger.py` (`opened_by`), `api/replay_guard.py`, `api/routers/performance.py`, the
corporate-actions spec §4.4 row, `tests/portfolio/test_corporate_actions.py` (field list),
`tests/portfolio/test_settled_notional.py`, `tests/contract/test_frontend_never_computes_money.py`;
tests under `tests/portfolio/`, `tests/contract/`, `tests/api/`.

- [ ] Price-0 closes book spec §4.1's rows with the existing arithmetic (prove with tests).
- [ ] **§4.4 refusal** (spec §4.4): the test runs before the position is created or `_reopen`ed; a
  refused row books nothing and appends a `RefusedClose` (always, with `txn_id` from `Transaction.id`);
  sticky flags when the position exists; strict path raises. Fill `Book.realized_close_reasons`.
  XIRR and B withheld whenever `Book.refused_closes` is non-empty. Readers mirroring
  `unbookable_action`: `pnl.py`, `position_aggregate.py`, `dividends.py`, `timeseries.py`,
  `dashboard.py`. `api/replay_guard.py`: hard `refused_close_correction`, per row, its 422 listing
  every stranded close row (`txn_id`, date, contract, reason). CA spec §4.4 table + field-count test
  gain `unbookable_close`.
- [ ] **`OPTION_ADJUST`** branch after SPLIT, before the E5 block, replacing W1's placeholder: its own
  destination lookup / create, E22, opposite-direction `_reject`, the spec §4.3 field table
  (`× ratio_to`), then `_reopen(Q)`.
- [ ] **Share walker**: signed count × `ratio_to`, shorts included; opposite signs → skip +
  `note_negative_side_skip`; agreement test with `build_book` (on a ledger with no refused close —
  the spec §4.3 permitted divergence).
- [ ] **`portfolio/options.py`**: `last_trade_prices` (per symbol across accounts, ordinary trades,
  `trade_date ≤ as_of`, predecessor ÷ `ratio_to`); `option_summary(...) -> OptionsBlock` with every
  spec §7.1 key (`past_expiry` = expiry < valuation day while the book's position is non-zero;
  `underlying_price` from ONE `get_latest_price` read per distinct registered underlying, omitted
  when unpriced; `refused_closes`; `realized.rows` + aggregates; `trend_excludes_value` = at least one
  trend day skipped an option holding; `principal_fx_note`). Register the block's row models in the
  money scan; classify its money products for `test_settled_notional.py` (valuation) or allowlist.
- [ ] **Valuation seam**: options priced from `last_trade_prices` into `price_map`, not `price_reads`.
- [ ] **Split the lists** per invariant 1; KPI totals, XIRR terminal value, FX `foreign_stock_value` —
  stocks + options, skipped when `None`; KPI 總市值 = stock grand subtotal + `options.market_value`.
- [ ] **本金匯率效果 (OPT-D20)**: `principal_fx_effect = None` whenever an option position is open on
  the valuation day; `options.principal_fx_note` carries the reason; unchanged otherwise.
- [ ] **Trend**: skip `is_option` holdings without `incomplete`; TWR (`performance.py`) and the
  benchmark read `options.trend_excludes_value`.
- [ ] **Payload**: `options` set iff the uncut ledger has ≥ 1 transaction or action on an option.
- [ ] Tests (fail first): every §4.1 row; §4.3 table on long and short; opposite-direction merge
  refused; a refused close never creates a position nor clears `vacated_to`; last-trade price across
  accounts and an adjustment; KPI = stocks + options and weights sum to 100 %; B − A with an open sold
  put → `principal_fx_effect` None + note; trend excludes without `incomplete`; XIRR includes the open
  option; `past_expiry` after a partial assignment; golden unchanged; no `options` key on a stock
  ledger or for a registered-but-untraded contract.
- **Done when:** targeted tests, ruff, mypy, architecture tests; commit `feat(options): W2 replay,
  valuation, trend, options block`.

## 4. W3 — Doors, validators, fees, converter, cash lines, input wires (wave 1, subagent B)

**Files:** `data_ingestion/{validate,csv_import,corporate_action_import,opening_import,import_templates,
fees,config_seed,fee_overrides,agents,manual}.py`, `data_ingestion/broker/{ir,schwab,grouping,convert,
reconcile}.py`, `portfolio/cash.py` (`account_statement` + `pool_lines`' `txn_id` only),
`api/routers/{input_center,ledgers,cash,fee_rules,broker_import}.py`, `api/wire.py`,
`export/cash_statement.py` (the two `account_statement` call sites only — E owns the rest in wave 2),
`scripts/schwab_convert.py`, `tests/scripts/test_schwab_convert.py`,
`tests/portfolio/test_cash_running.py`; tests under `tests/data_ingestion/`, `tests/contract/`,
`tests/api/`.

- [ ] **Validators** (`validate.py`): `TxnInput.close_reason`; `transaction_structural_issues` accepts
  `price == 0` only with `close_reason` (fail-first: a price-0 row without it is refused); ONE helper
  `option_close_issues(...)` used by every door and the inbox confirm (spec §4.2: derived side,
  quantity, dates, after-expiry, same-day-after-expiry, the stored-`expired`-row protection,
  multiple of 100 with the 口數 hint); `dividend_on_option`; opening import refuses option symbols;
  `validate_corporate_action` for `OPTION_ADJUST` (spec §4.3: `is_standard_adjustment`, `ratio_from
  ≠ 1` refusal, `shares_before_action_on` non-zero signed, destination direction, bypass E5 / E18,
  skip N3-price, E10 via registration, own zh); option symbols refused for SPLIT / EXCHANGE / SPINOFF;
  the SPLIT-with-open-options soft finding; `unknown_action_kind` lists four kinds from `KIND_ZH`.
- [ ] **Fees**: close rows fee = tax = 0 without the engine (snapshot `{"source": "close_reason"}`;
  a supplied non-zero fee / tax refused); `FeeRuleSet.option_per_contract: Decimal | None`,
  `option_reg_per_contract: Decimal = 0`; `compute_fees(..., is_option: bool = False)` →
  `_us_option` (bypasses `_us`, 0 / 0 at notional 0, per contract + reg + SEC on sells, no MY stamp;
  snapshot `{"option": true, "contracts": N}`); `schwab` 0.65, `moomoo_us` None → issue
  `option_fee_unset` only when the **fee** is unsupplied; an option-label helper for the preview
  (`fee_rules_wire()["label"]` unchanged); a nullable field class in `fee_overrides` for the two keys
  (+ `EDITABLE_FIELD_ORDER`), on `/api/fee-rules` via `fee_rules.py` / `wire.py::fee_rules_wire`;
  re-pin `tests/api/test_r7_fee_rule_readonly_wire.py`, the DEF-010 test and the zip's fee snapshot.
- [ ] **Manual door** (`input_center.py` + `data_ingestion/manual.py`): `ManualBody.option_intent`,
  `contracts` (then `shares` is optional and set by `contracts_to_shares`), `close_reason`,
  `stock_price`; `enter_transaction` resolves an OCC symbol through `register_option_contract`, skips
  the engine on a close row, writes `close_reason`, and writes the two-leg pair in one transaction
  (rollback on a failed second leg); option-mode refusals (spec §4.2 incl. OPT-D14 / D21); the close
  row's preview (realized figure, position after); `legs`, `txn_ids`, per-leg totals; preview
  `share_equivalents`, `contract_name`, withholding on `unbookable_close`; the compose endpoint `GET
  /api/input/option-symbol`; `/api/input/context` instruments `asset_type`; `/api/input/holdings`
  rows `asset_type`, `direction`, `contracts` (a non-zero signed option is `held`, a flat one
  `closed`).
- [ ] **CSV + template**: `close_reason` column; hints and the example note; preview `data`
  `contracts`, `contract_name`; soft `option_premium_exceeds_strike`; re-pin the template, preview-
  shape and DEF-026 tests; the corporate-action CSV accepts `OPTION_ADJUST`.
- [ ] **Ledger doors** (`ledgers.py`): `GET /api/ledgers/transactions` rows `close_reason`,
  `contracts`, `option_side` (ONE `classify_option_rows` call per request over the stored option rows
  and `OPTION_ADJUST` rows of every account on the page), `refused_close` (from the replay's
  `Book.refused_closes`); the edit door (`TxEditBody.contracts`; the `price <= 0` gate relaxed for a
  stored close row; `_recompute_edit_fees` skips the engine on it; `close_reason` absent = keep, not
  editable in v1) runs the close-row helper with `replacement`; the corporate-action doors accept
  `OPTION_ADJUST`; the CA preview returns `strike_products` / `conserves`.
- [ ] **Cash lines**: `account_statement(...)` takes the required `option_side_of: Mapping[int, str]`
  and sets `option_side` by `txn_id` (`dataclasses.replace`); B updates both callers it owns now —
  `api/routers/cash.py` (statement wire gains `option_side`, `contracts`) and the two
  `export/cash_statement.py` call sites (E renders the descriptions in wave 2) — and the direct calls
  in `tests/portfolio/test_cash_running.py`.
- [ ] **Converter + broker import**: option rows stop being dropped; `RowDetail.cells` carry the OCC
  symbol and share-equivalents (`contracts_to_shares(Quantity)`), `RowDetail.symbol` / `shares` the
  display text; price per share; the four sides; `Expired` and the TD removal journal (re-map
  `schwab.py`'s rule, fix `ir.py`'s comment) → price-0 `expired` per spec §8; `known_positions` a
  required argument of `convert(...)`, built in `broker_import.py` (ledger through the day before the
  request's earliest row); `ratio_to = |to| ÷ |from|` whole-number journal pairs → `OPTION_ADJUST`;
  OPTION OUT / IN → dropped; option symbols excluded from the derived openings; `UnmappedRow.reason` /
  line; `broker_import.py` catches it around `parse_export` **and** `convert` → 422; the CLI passes
  `known_positions={}` (help text says so) and its test is updated.
- [ ] Tests (fail first): every rule with its zh message; the edit door on a close row; a back-dated
  trade against a stored `expired` row; CSV round trip of the raw export with option rows; template
  rows commit clean; fees per rule set and `option_fee_unset` with fee supplied / not supplied; the
  two-leg commit both or neither (incl. a failed second leg); `/api/input/holdings` lists a short as
  held; the compose endpoint; `option_side` on ledger rows and cash lines equals the classifier; the
  converter on a synthetic Schwab file with every option row shape, `cells` committed through the CSV
  door, units vs Amount, `known_positions` (and an overlapping request), a 2 → 6 pair read as 1 → 3,
  each `UnmappedRow` reason incl. one raised inside `convert`.
- **Done when:** targeted tests, ruff, mypy; commit `feat(options): W3 doors, fees, converter, input
  wires`.

## 5. W4 — Fetch scope, registration refusals, stock readers (wave 1, subagent C)

**Files:** `shared/instrument_scope.py`, `api/instrument_service.py`, `scheduler/jobs.py`
(`refresh_instrument_quote`), `api/{fundamentals_service,news_service,alert_inputs,signals_service,
insight_service,digest_service}.py`, `strategy/{rebalance,whatif,alerts}.py`,
`api/routers/{strategy,instruments,prompts,actions}.py`, `llm_insight/variables.py`; new
`tests/contract/test_options_excluded_from_stock_readers.py`.

- [ ] `TRACKED_SQL` / `tracked_instruments`: `asset_type = 'stock'`.
- [ ] `quick_register` and `restore_archived` refuse an OCC-shaped symbol at entry regardless of
  `force` 「選擇權合約由交易登錄建立」; the gap backfill, `refresh_instrument_quote`, the Alpha Vantage held
  universe and held-only news refuse an option — each with a mocked-provider test; `resolve.py`'s
  register-first path meets the refusal (test only).
- [ ] Readers (OPT-D10): filter where a reader reads the registry or the book; `/api/instruments`
  elements gain `asset_type`; the watchlist hides options unless `include_options=1`. Readers that only
  read `data.holdings` need no filter (stock-only by A) — the main session tests them at the merge.
- [ ] Scan test: every module iterating `book.holdings`, calling `held_among` / `holds_position` /
  `held_in_book`, reading `tracked_instruments`, or `strategy/whatif.py` applies the filter or is an
  allowlist entry with its reason. `compute_fees` callers in `strategy/` pass no `is_option` (default
  False) — options never reach them.
- **Done when:** targeted tests, ruff, mypy; commit `feat(options): W4 fetch scope and stock readers`.

## 6. W5 — Web UI (wave 2, subagent D)

**Files:** `web/{index.html,app.js,charts.js,ledger.js,trades.html,input.js,sym-picker.js,
corp-action-form.js,broker-import.js,detail.js,instruments.js,cash.js,shell.js,dividend-inbox.html,
settings-fees.js}`, new `web/options.js` (if split out) and `web/expiry-inbox.js`,
`shared/whatsnew.py`, `api/routers/symbol.py`; tests under `tests/contract/`, `tests/e2e/`.

- [ ] **選擇權 section** (spec §7.1): sort, columns, 標的現價, 履約義務合計, expander, ≤ 640 px cards, empty
  state, 已實現 tabs and chips, the `past_expiry` badge + 「登錄到期作廢」 → `window.pdPrefillOptionClose`;
  the KPI 「其中選擇權」 line; 本金匯率效果 「—」 + `options.principal_fx_note`; the 已實現損益 option line;
  the trend / B / TWR / benchmark note; the 待釐清 banner with 「前往該筆交易」 (`openDeepLink` gains
  `?ledger=tx&txn_id=`).
- [ ] **Manual entry**: picker footer 「＋選擇權合約」 (US accounts) → builder → compose endpoint; held
  contracts in 已持有; option mode sends `option_intent` + `contracts` (no conversion in JS); four
  sides with the position default; preview labels; 「到期作廢」; 「被指派／履約」 legs card (`stock_price`
  editable); the inline 「轉倉」 button; the `option_fee_unset` link.
- [ ] **Ledger**: §6.1 chips from `option_side`; 口數 from the server's `contracts`; 「未入帳（待釐清）」;
  keyword filter on the display name; the delete dialog pre-lists later close rows (`GET
  /api/ledgers/transactions?account_id&symbol`) and renders the `refused_close_correction` 422 list;
  the edit dialog edits 口數 (`TxEditBody.contracts`) and shows the reason chip.
- [ ] **公司行動 form**: fourth kind, 來源 fixed at 1 口, 目的 underlying + strike → compose endpoint +
  目的 口數; `strike_products` / `conserves` line; tab hint; `input.js` CSV hint lists four kinds.
- [ ] **Broker import page**: per-reason remedies with the line; labels from `RowDetail.symbol` /
  `shares`.
- [ ] **Drawer** (`detail.js`, `symbol.py`): contract header, chips, reason chips, 試算 note, 待釐清, the
  footer from `options.open`; the stock drawer's 未平倉選擇權 line.
- [ ] **Expiry inbox panel** against the spec §7.2 contract: `web/expiry-inbox.js` + its tag in
  `dividend-inbox.html`; confirm, deep link, skip / unskip, 全部確認 via `confirmDialog`, per-item
  results, 待釐清 items; the sidebar badge (`shell.js`) adds `/api/option-expiry/count`. Unit-tested
  with a stubbed fetch; its e2e runs in W7b.
- [ ] **Fee settings** (`settings-fees.js`): the two option fields (US rule sets; unset ≠ 0).
- [ ] **Watchlist** filter; cash page descriptions from `option_side` and `contracts`; the what's-new
  entry (spec §8).
- [ ] Static tests stay green; DEF-056 seed rule; e2e: section from a seeded option ledger, option-mode
  STO → BTC, the roll button, an open contract's drawer footer green, the section absent on the golden
  ledger.
- **Done when:** targeted contract + e2e chunks, ruff, mypy on `symbol.py`; commit `feat(options): W5
  web UI`.

## 7. W6 — Exports and the expiry inbox backend (wave 2, subagent E)

**Files:** `export/{holdings,holdings_report,realized,tax,ledgers_report,cash_statement,symbol_detail,
rebalance_report}.py`, `api/routers/export.py` (if a signature changes), new `api/option_expiry.py`
and `api/routers/option_expiry.py`; tests under `tests/export/`, `tests/contract/`, `tests/api/`.

- [ ] Exports per spec §9 (realized CSV header re-pinned; 現金收支明細 computes `option_side_of` once
  and renders descriptions — B already updated the call sites). Every other export of a stock ledger
  byte-identical.
- [ ] **Expiry inbox backend** per spec §7.2 (routes, payload keys, item rules, `account_ref` text,
  short / long wording, confirm through B's `option_close_issues` + the manual write path, per-item
  confirm-all, `ensure_tables` for `option_expiry_skips`). The main session wires it in `app.py`.
- [ ] Tests (fail first): detector over expired / live / closed / partially-assigned / skipped / short
  and long; confirm writes exactly the §4.1 row; confirm-all per item; count 0 on a stock ledger; a
  refused-close item is not confirmable; the DEF-065 GET sweep covers the new GETs.
- **Done when:** targeted tests, ruff, mypy; commit `feat(options): W6 exports + expiry inbox backend`.

## 8. W7 — Oracle, manual, acceptance

**W7a — oracle (wave 2, subagent F; `scripts/stress_audit/*` only).** From the spec, never importing
the app:
- [ ] `load_facts_from_db` reads `close_reason`; terms parsed from the symbol by its own parser.
- [ ] `oracle.py`: price-0 closes, §4.4 refusal, `OPTION_ADJUST` (`_KINDS`, branch, `ratio_from ==
  1`, `replay()`, `integrity_findings`), an option branch in `fee_tax`, last-trade valuation, the trend
  exclusion, OPT-D20's withheld 本金匯率效果.
- [ ] Phase-1 ops: STO→BTC · STO→expire · BTO→STC · BTO→expire · 1→3 on a short · 1:1 rename ·
  sold-put assignment · same-day BTC + expiry · cash ties per pool.
- **Done when:** phase 1 green; new baseline recorded; commit `test(stress-audit): options`.

**W7b — wave 3, main session:**
- [ ] `api/app.py`: the inbox router + `ensure_tables` at boot.
- [ ] Cross tests: validators over an adjusted contract; an expiry after an adjustment; the manual
  preview over a refused close; the 交易 tab marks a refused close; `option_side` identical on the
  ledgers wire, cash lines and exports and agreeing with the replay's realized rows; `/api/input/
  holdings` direction agrees with `options.open` (no refused close); `data.holdings` readers see no
  option; the inbox confirm through B's helper; the inbox e2e (confirm, confirm-all); the §6.1
  vocabulary guard; KPI 總市值 = stock grand subtotal + `options.market_value` (update
  `tests/api/test_dashboard.py`'s grand == KPI pin for option ledgers only).
- [ ] Accounting manual zh + en; rule files (`domain-ledger.md`, `data-and-pricing.md`,
  `markets-and-fees.md`, `architecture.md`); CHANGELOG `[Unreleased]` (R1–R7, OPT-D1..D22; `grep -c
  "^## \[v"` = 30).
- [ ] Final review (xhigh on `main..feat/options-ledger`), fixes with fail-before tests, heavy gates
  once.
- [ ] **Real-data acceptance** (local, throw-away database): the owner's Schwab option rows through
  the converter (one request, and split across requests) and the CSV door — option realized, the open
  contract, the option cash total equal the git-ignored `OPTIONS_ledger_proposals.md`.
- [ ] Report to the owner; demo deploy on the owner's word (additive migration pre-authorised; back
  up and verify first); prod only on 「上」.

## 9. File ownership (no two subagents in one file)

| Wave 1 — A (W2) | Wave 1 — B (W3) | Wave 1 — C (W4) |
| --- | --- | --- |
| `portfolio/{cost_basis,options,dashboard,timeseries,allocation,dashboard_models,pnl,position_aggregate,dividends}.py`, `data_ingestion/holdings.py`, `shared/models/ledger.py`, `api/replay_guard.py`, `api/routers/performance.py`, CA spec §4.4 row, `tests/portfolio/{test_corporate_actions,test_settled_notional}.py`, `tests/contract/test_frontend_never_computes_money.py` | `data_ingestion/{validate,csv_import,corporate_action_import,opening_import,import_templates,fees,config_seed,fee_overrides,agents,manual}.py`, `data_ingestion/broker/*`, `portfolio/cash.py`, `api/routers/{input_center,ledgers,cash,fee_rules,broker_import}.py`, `api/wire.py`, `export/cash_statement.py` (call sites), `scripts/schwab_convert.py`, `tests/scripts/test_schwab_convert.py`, `tests/portfolio/test_cash_running.py` | `shared/instrument_scope.py`, `api/instrument_service.py`, `scheduler/jobs.py`, `api/{fundamentals_service,news_service,alert_inputs,signals_service,insight_service,digest_service}.py`, `strategy/*`, `api/routers/{strategy,instruments,prompts,actions}.py`, `llm_insight/variables.py` |

| Wave 2 — D (W5) | Wave 2 — E (W6) | Wave 2 — F (W7a) |
| --- | --- | --- |
| `web/*`, `shared/whatsnew.py`, `api/routers/symbol.py` | `export/*`, `api/routers/export.py`, `api/option_expiry.py`, `api/routers/option_expiry.py` | `scripts/stress_audit/*` |

Main session: W1's files, `CHANGELOG.md`, `api/app.py`, `tests/conftest.py`, `.claude/rules/*`, the
manuals, merges, cross tests. `api/snapshots.py` needs no change (OPT-D12).

## 10. Risks

| Risk | Mitigation |
| --- | --- |
| Converted option rows refused at the CSV door | `cells` keep OCC + share-equivalents; a converter → CSV commit test |
| An option's value read as 本金匯率效果 | OPT-D20 withholds it while an option is open; B − A test |
| A close row refused by the structural / edit price gates | W3's first bullets + fail-first tests |
| An engine fee on a close row | doors skip the engine; `_us_option` 0 / 0 at notional 0 |
| A back-dated trade strands an `expired` row | the stored-`expired`-row protection (spec §4.2) |
| A close left behind books a phantom lot | §4.4 refusal + record-based gate + replay guard + listing marks |
| Two packages disagree on a shared shape | W1 defines the types; spec §7.1 / §7.2 pin the keys and routes |
| A stock reader nobody listed sees an option | W4's scan test; `data.holdings` stock-only |
| A new wire field changes the golden | invariant 1 |
| Fractional contracts | OPT-D16 in validator, converter and oracle |
| 100× quantity error | multiple-of-100 + 口數 hint; `contracts` on the manual / edit doors; the converter multiplies |
| Demo data | backup + verify before the deploy; prod only on 「上」 |

## Appendix — code map (verified at `aa5c80a`; re-check line numbers before editing)

**Replay** — `portfolio/cost_basis.py`: `_reject` / `UnappliedAction` :225-263 (the record gates
:240-246); `_apply_action` def :266; E1 :298, E2 :304, E3 :310; SPLIT :315-324; E5 block :326-331;
E18 :338-341; E22 :346; SPINOFF fall-through :391-415. `_Position` (17 fields) :70-155; position
create / `_reopen` before the trade branches :711-713; cover :714-751; declared SELL :752-785;
dividend on a short :860-880; signed output :1007-1009; `opened_positions` :1014-1016; 0-share skip
:1029-1030; sort key :592-603. `ledger_events.py` :46-53. `results.py` `Holding.unbookable_action`
:105, `RealizedRow.kind` :166. Field-count test `tests/portfolio/test_corporate_actions.py` :352; CA
spec §4.4 table :532-585.

**Models and store** — `shared/models/ledger.py` `Transaction` :28-44 (no `id` today), `opened_by`
:319; `store.py` `load_ledger_bundle` builds `Transaction` without `id` :1681; `upsert_instrument`
:162-227, `_row_to_instrument` :580.

**Action vocabulary and CA validator** — `shared/corporate_actions.py` :36-52, `is_ratio_term`
:68-78, validator :111-131, `convert_stored` :240; `validate.py` `unknown_action_kind` :1145-1150,
E-codes :1161-1549 (fallback wording :1175 / :1518, `short_source` :1436, `short_destination` :1453,
N3-price `to_symbol_unpriced` :1515), `shares_before_action_on` :1265; `corporate_action_import.py`
:61-64/:71-76/:379/:389; `ledgers.py` :1101, :1754/:1887/:2041/:2069, `_holding_accounts` :1242-1245;
`store.py` :1452/:1474; `import_templates.py` :88/:154-169; web `corp-action-form.js` :133-140 /
:686-703, `ledger.js` :1020, `input.js` :1115; `test_i14` `_PAIR` :35.

**Share walker and validators** — `holdings.py` `_delta_of` :287-352, `shares_through` :450-474,
`created_by` :676. `validate.py` `TxnInput` :117, `pending_share_flows` :475 (→
`row_cannot_be_written` :489-497), `siblings_booked_before` :511, `_replacement_flows` :534, structural
:629-636, 賣超 guard :778-873, `_dividend_findings` :2231-2440.

**Replay guard** — `api/replay_guard.py` codes :78, `_replay_block` :287-363, orphan 422 :394-396.

**`unbookable_action` readers** — `pnl.py` :39; `timeseries.py` :391; `position_aggregate.py` :172
(→ `symbol.py` :259); `dividends.py` :85; `input_center.py` :228/:252; `web/app.js` :702/:733,
banner :147; `web/detail.js` :558-583.

**Manual door** — `data_ingestion/manual.py` `enter_transaction` (resolve → `symbol_unresolved` for an
unregistered symbol; fee seam :116 / :143; commits each row); `input_center.py` `/api/input/context`
:139-167, `/api/input/holdings` (`held` = `shares > 0`) :387, `ManualBody` :402-443 → `TxnInput`
:446-454, `_cash_overdraft_issue` :560-595, `_position_preview` returns None at price 0 :783,
`manual_preview` :1026-1102 (`fee_rule_label` :1094), commit :1106+ (`ack_oversell` only
:1186-1190), auto-register :1124-1179; import commit `ack_warnings` :1589.

**Edit door** — `ledgers.py` transaction rows :262-290, `edit_transaction` price gate :666,
`_recompute_edit_fees` :545-631.

**Instruments and registration** — `assets.py` :38-70; `schema.py` :9-20, `_add_column_if_missing`
:104-109; `register.py` :33-79; `instrument_service.py` `_gap_backfill_inner` :417-435,
`gap_backfill` :438, `quick_register` :462-598 (quote gate :558), `restore_archived` :348;
`routers/instruments.py` `_element` :91-135, POST :543-617 (`force=True` :600-604, backfill :613);
`symbol_format.py` :25-29; `resolve.py` :81; `csv_import.py` :289-306; `opening_import.py` :210-217.

**Fetch scope** — `instrument_scope.py` :38/:50-71; `jobs.py` :470-477/:480-530/:1565-1585;
`app.py` :243 → `actions.py` :34-57; `pricing/ingest.py` :55-80; `fundamentals_service.py` :24-37;
`news_service.py` :141/:164; `api/dividend_inbox.py` :55/:236.

**Valuation and payload** — `dashboard.py` `build_dashboard` :624-1200; price reads :713-735; XIRR
reason :844-845; FX stock value :858-868; ex-div :940-957; `combined_view` → :961/:1121; weight
:967-973/:1137-1150; `HoldingRow(**data)` :986-997; cash series :1046-1068; `RealizedPnL` :1078;
`total_return` → :1078-1089; **`principal_fx = fx_complete − total_return_blended − trading_cost`
:1100-1108**; freshness :1151-1156 / :1173-1174; subtotals :317-390; `fx_complete_return` :489-533;
benchmark :539. `returns.py` :39-87, flows :361-365, XIRR terminal value `price_map × shares`
:388-394. `dashboard_models.py` `TrendPoint` :211-221, `exclude=True` :427-443. `routers/dashboard.py`
:47. Golden `test_spec17_financials.py` :613; grand == KPI pin `tests/api/test_dashboard.py` :451.
月度成績 `web/app.js` :1018-1025 → `api/snapshots.py` :44-63. Money scan
`test_frontend_never_computes_money.py` :34-70; settlement scan `test_settled_notional.py` :122-173.

**Trend** — `timeseries.py` :282-445 (:382-433; short :405-409; no price :410-413), flows :198-204;
`web/charts.js` :68-70/:121-122; `index.html` :81.

**Fees** — `csv_import.py` compute when fee or tax None :319-345; `manual.py` :116; `fees.py`
`supplied_snapshot` :103-134, `_us` :208-255 (notional ≤ 0 :214-215, SEC floor :241), `compute_fees`
:297-336; `config_seed.py` `FeeRuleSet` :20-54, rule sets `schwab` :134, `moomoo_us` :146;
`fee_overrides.py` :46-60/:80-86/:131; `wire.py` `fee_rules_wire` :163-185; zip fee snapshot
`export/ledgers.py` :86-91.

**Cash** — `portfolio/cash.py` `_TxRow` :81-90, trade lines :283-289, `CashLine` (frozen) :178-210,
`pool_lines` :236, `account_statement` :359; callers `routers/cash.py` :234/:236/:311/:694,
`export/cash_statement.py` :141/:351, `tests/portfolio/test_cash_running.py` :134/:144;
`web/cash.js` :345-346.

**Converter** — `broker/ir.py` :76-86/:117-120/:170-172; `schwab.py` :95-100, journal rules
:108-122, :298-321; `grouping.py` :699-700; `convert.py` `UNCONVERTIBLE` :63-69, `RowDetail` :75
(`cells` = the CSV row :80, :95), trade rows :160-192, `option_row_unsupported` :349-354, CSV out
:435; `reconcile.py` `_check_priced_rows` :182, advisory :453-459; `broker_import.py` :15,
`_opening_gaps` :127, `UnmappedRow` caught around `parse_export` only :336-345; CLI
`scripts/schwab_convert.py` :287; `web/broker-import.js` `TYPE_ZH` :82, `BLOCK_ZH` :106, :404-407.

**Inbox pattern** — `api/rebates.py` (`rebate_skips` :55, `ensure_tables` :180, skip insert :380);
`app.py` boot ensures :205-211; `routers/dividend_inbox.py` :21/:48/:61/:71/:82/:95;
`web/dividend-inbox.html`, `web/inbox.js`, `web/rebate-inbox.js`; `shared/account_ref.py`;
`shared/whatsnew.py` :77-85; `web/sym-picker.js` :24; `web/shell.js` toast :748, badge :908-931.

**Exports** — `export/holdings.py` :61-63; `holdings_report.py` :147/:203/:282; `realized.py` :25-45,
header `test_export_realized.py` :28; `tax.py` :103-116; `ledgers_report.py` :110/:185/:379;
`cash_statement.py` :198; `symbol_detail.py` :58; `rebalance_report.py` :322/:337;
`ledger_registry.py`; `test_def026_ledger_export_reimports.py` :291. DEF-056 sweep
`tests/e2e/test_def056_r5_future_row_sweep_flow.py` :34/:90/:96-147.

**Oracle** — `scripts/stress_audit/common.py` :501-565; `oracle.py` `fee_tax` :218-235, `RealizedRow`
:505-520, `EventPriority` :559-577, `_Pos` :581-596, `_apply_action` :709-861 (`_KINDS` :633-634),
`replay()` :947-1070, `integrity_findings` :1574+; README :244.
