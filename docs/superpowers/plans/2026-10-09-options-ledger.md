# Options ledger (P3, approach ④) — Implementation Plan

> **Spec:** `docs/spec/2026-10-09-options-ledger.md` (APPROVED 2026-10-09: R1–R7, OPT-D1..D19).
> **Branch:** `feat/options-ledger`, off `main` at `aa5c80a`. **Status:** PLAN for owner review —
> revised after review rounds 1–3 and round 4 (reviewers A + B, integrated by C; all Fable 5.1, max
> effort). No code until the owner approves (owner: approve the plan → compact the context → 開工).
> Execution: subagent-driven, **at most 3 parallel subagents**, worktree-isolated, disjoint files.
> Line numbers in the appendix were verified at `aa5c80a`; re-check before editing.

## 0. Goal and invariants

**Goal.** US equity options as the spec defines them. The owner's goal behind it: the app's Schwab
USD cash ends at the broker's figure once the whole real history is in.

**Invariants (a violation is a bug, not a choice):**

1. **Blast radius (D38, spec §10).** A ledger with no option is byte-identical in every API payload
   and export **except the additive changes spec §10 lists** (three export / template columns and the
   named wire keys, null / false / `"stock"` on a stock ledger), each re-pinned in the same commit
   wherever a test pins it. The golden payload (whole-payload equality) does not change: **no existing
   dashboard model gains a field**; every new dashboard field lives in `options`, popped when `None`;
   `Holding.unbookable_close` never reaches `HoldingRow`; every `+ options` addition is skipped when
   the block is `None` (never `+ Decimal(0)`); stock-only aggregates are computed by feeding the SAME
   functions (`combined_view`, `_holdings_subtotals`, `sector_allocation`) the stock-only list.
2. **Money.** Decimal end to end; the frontend only formats server strings; the multiplier and the
   OCC encoding live only in `shared/options.py` — the browser never composes a symbol or a premium
   total (compose endpoint, server preview fields).
3. **Layering.** `shared/options.py` imports nothing internal; `portfolio/options.py` is calc core;
   `portfolio/cost_basis.py` and `portfolio/results.py` stay leaves; the expiry detector lives in
   `api/` (`data_ingestion` never imports `portfolio` for it); the converter stays pure
   (`known_positions` is injected, a required argument).
4. **`prices` is never written for an option.**
5. **Stocks unchanged** — every new branch is gated on `is_option`, `close_reason` or `OPTION_ADJUST`.
6. **One definition per rule** — symbol parse / compose / display and the multiplier, `option_side`,
   the §6.1 vocabulary, `is_option`, last-trade price, the trend note, the close-row helper. The
   oracle transcribes from the spec.
7. **Project rules** — a GET never writes (the skip table and any table at boot); a state colour is a
   class; no native dialogs (`confirmDialog`); the DEF-056 sweep's seeded day is never an expiry.

**Process.** Fail-before tests for every behaviour. While fixing: ruff + targeted tests only. **Heavy
gates once, after the final review**, one at a time: ruff · mypy --strict (fresh `--cache-dir`) ·
pytest non-e2e (batch totals = `--collect-only`) · e2e in ≤ 11-test chunks · stress phase 1. Use
`C:\AI-Workspace\invest-portfolio\.venv\Scripts\python.exe` from the worktree root. Worktree subagents
come up on `main`: each first runs `git reset --hard <wave base sha>` and reports its merge-base.
Subagents never touch `CHANGELOG.md`, `api/app.py`, `tests/conftest.py`, `.claude/rules/*` or the
accounting manuals. Scoped `git add`; the session's commit trailer. Merge by cherry-pick; re-run the
seam tests after each.

## 1. Waves and dependencies

| Wave | Who | Packages | Base |
| --- | --- | --- | --- |
| 0 | main session | **W1** foundation | the branch tip the owner approves (≥ the commit carrying this plan) |
| 1 | 3 subagents | **A = W2** replay + valuation + trend + `options` block · **B = W3** doors + validators + fees + converter + cash lines + input wires · **C = W4** fetch scope + registration refusals + stock-reader exclusions | W1 tip |
| 2 | 3 subagents | **D = W5** web UI (section, entry, inbox panel, drawer, ledger, CA form, what's-new) · **E = W6** exports + the expiry inbox backend · **F = W7a** stress oracle + scenario ops | merged wave-1 tip |
| 3 | main session | integration (`app.py` wiring), cross tests, rule files, CHANGELOG, manual (W7b), final review, fixes, heavy gates, local real-data acceptance | merged wave-2 tip |

**Dependencies, stated so nobody waits inside a wave:**
- W1 defines every shared type and pure helper: `OptionTerms`, the parser / composer / display name,
  `contracts_to_shares` / `shares_to_contracts`, `is_standard_adjustment`, `OptionSide` + the
  classifier + the §6.1 vocabulary constants, `Instrument.is_option`, `Transaction.close_reason`,
  `RefusedClose`, `Book.refused_closes`, `Book.realized_close_reasons`, `OpenedPosition` /
  `Holding.unbookable_close`, `OptionsBlock` (shell) and `DashboardData.options`.
- B's validators read `holdings.shares_through` and the replay; their `OPTION_ADJUST` and
  refused-close behaviour is A's. B's tests do not cross an adjustment or a refused close until the
  wave-1 merge; the main session adds those cross tests.
- `unbookable_close` readers mirror `unbookable_action`'s: A (`pnl.py`, `position_aggregate.py`,
  `dividends.py`, `timeseries.py`, dashboard reasons, replay guard), B (`input_center.py` preview), D
  (`web/app.js`, `web/detail.js`).
- Wave 2 needs A's block (D, E), B's wires and close-row helper (D, E — the inbox confirm writes
  through B's helper), and only the spec (F).

## 2. W1 — Foundation (wave 0, main session)

**Files:** create `portfolio_dash/shared/options.py`; modify `shared/symbol_format.py`,
`shared/models/assets.py`, `shared/models/ledger.py`, `shared/corporate_actions.py`,
`portfolio/results.py` (types), `portfolio/dashboard_models.py` (types), `data_ingestion/schema.py`,
`data_ingestion/store.py`, `data_ingestion/register.py`, `api/routers/dashboard.py` (`None` pop only),
`tests/contract/test_i14_one_action_vocabulary.py`; tests under `tests/shared/`, `tests/data_ingestion/`.

- [ ] **`shared/options.py`** (header `TODO(options-⑤)` → spec §12): `MULTIPLIER = 100`;
  `OPTION_PATTERN` (letters-only root); `OptionTerms`; `parse_option_symbol`; `option_symbol(terms)`
  (the only OCC composer); `option_display_name`; `from_broker_option("XYZ 12/18/2026 50.00 P")`;
  `contracts_to_shares` / `shares_to_contracts`; `is_standard_adjustment(from, to, ratio_to,
  ratio_from)` — multiplier 100 both, letters-only destination root, same right, same expiry,
  **`ratio_from == 1`**, `strike_from × ratio_from == strike_to × ratio_to`; `OptionSide` and
  `classify_option_rows(rows, actions) -> dict[int, OptionSide]` — rows carry `(id, account, symbol,
  trade_date, side, quantity, short_sale, close_reason)`, actions `(account, date, from_symbol,
  to_symbol, ratio_to)`; ONE walk per account in replay order `(date, priority, id)` (an
  `OPTION_ADJUST` before the day's trades moves the signed count × `ratio_to` from → to), the ONE
  definition of spec §6.1; the §6.1 zh constants; `TREND_EXCLUDES_OPTIONS_NOTE`.
- [ ] `symbol_format.py`: `MARKET_CODE_PATTERNS["US"]` unchanged; `matches_market_format(sym, US)` and
  `looks_like_market_code` also accept `OPTION_PATTERN`.
- [ ] Models: `Instrument.asset_type` (default `"stock"`), `option`, `is_option = asset_type ==
  "option"`; `Transaction.close_reason`.
- [ ] Types with defaults: `RefusedClose`, `Book.refused_closes`, `Book.realized_close_reasons`
  (index into `Book.realized.rows` in emission order; `RealizedRow` unchanged), `OpenedPosition` /
  `Holding.unbookable_close`; `OptionsBlock` (spec §7.1 fields) and `DashboardData.options = None`;
  `api/routers/dashboard.py` drops `options` from the wire when `None` (same commit — otherwise the
  golden fails); confirm by grep that no other whole-`DashboardData` dump serialises it.
- [ ] Schema (additive, created at boot): `instruments.asset_type`, `transactions.close_reason`,
  `option_contracts`.
- [ ] Store: `upsert_instrument` writes `asset_type`; `_row_to_instrument` attaches `option` from ONE
  `option_contracts` read per `list_instruments`; `upsert_option_contract` / `get_option_contract`;
  transactions insert / update / list carry `close_reason`; order keys unchanged.
- [ ] `register.py`: `register_option_contract(conn, symbol)` — parse, refuse non-option / digit root /
  multiplier ≠ 100 / non-US; spec §3 defaults; sets `asset_type` from the parse; idempotent; no quote.
- [ ] `corporate_actions.py`: `OPTION_ADJUST`, `KIND_ZH` 「選擇權合約調整」, no `cost_carry`,
  `convert_stored`. A refusing placeholder branch right after SPLIT in `_apply_action` (「尚未支援」)
  until W2. `test_i14` `_PAIR` widened to the fourth kind.
- [ ] Tests (fail first): parse / compose / display / broker string incl. 7.5 strikes; digit root
  refused; `is_standard_adjustment` (1→3 ok, 1:1 ok, 3-for-2 / reverse / `ratio_from > 1` / digit
  root / put→call / changed expiry refused); `classify_option_rows` over every §6.1 value incl. the
  over-cover, and STO 1 口 → 1→3 adjustment → BUY 3 口 reads 買進平倉; migration keeps every instrument `stock` and every `close_reason` NULL; registration
  defaults; golden untouched.
- **Done when:** targeted tests green, ruff, mypy on touched modules; commit `feat(options): W1
  foundation — option terms, classifier, schema, registration, OPTION_ADJUST vocabulary`.

## 3. W2 — Replay, valuation, trend, the `options` block (wave 1, subagent A)

**Files:** `portfolio/{cost_basis,options(new),dashboard,timeseries,allocation,dashboard_models,pnl,
position_aggregate,dividends}.py`, `data_ingestion/holdings.py` (`_delta_of`, `created_by`),
`shared/models/ledger.py` (`opened_by`), `api/replay_guard.py`, `api/routers/performance.py`, the
corporate-actions spec §4.4 row, `tests/portfolio/test_corporate_actions.py` (field list); tests under
`tests/portfolio/`, `tests/contract/`, `tests/api/`.

- [ ] Price-0 closes book spec §4.1's rows with the existing arithmetic (prove with tests).
- [ ] **§4.4 refusal**: a `close_reason` row that would open or extend a lot (BUY beyond the short lot,
  SELL beyond the long lot, or against the opposite direction) books nothing and appends a
  `RefusedClose` (always); sets the sticky `_Position.unbookable_close` + `OpenedPosition` / `Holding`
  flags when the position exists; strict path raises. A partial `assigned` / `exercised` within the
  lot books normally. Fill `Book.realized_close_reasons` for every realized row emitted from a
  `close_reason` trade. XIRR and B withheld whenever `Book.refused_closes` is non-empty (reason names
  the row). Readers mirroring `unbookable_action`: `pnl.py` (valuation withheld), `position_aggregate.py`,
  `dividends.py`, `timeseries.py` (incomplete from that date), `dashboard.py`. `api/replay_guard.py`:
  hard `refused_close_correction`, per row. The corporate-actions spec §4.4 table and the field-count
  test gain `unbookable_close`. Tests: delete the STO of an expired short → refused by the guard; a
  legacy ledger with a refused close and no position → XIRR / B withheld, never a 500.
- [ ] **`OPTION_ADJUST`** branch after SPLIT, before the E5 block: spec §4.3 checks and field table
  (`× ratio_to`).
- [ ] **Share walker**: signed count × `ratio_to`, shorts included; opposite signs → skip +
  `note_negative_side_skip`. Agreement test with `build_book` after a 1→3 adjustment of a short.
- [ ] **`portfolio/options.py`**: `last_trade_prices` (per symbol across accounts, ordinary trades,
  `trade_date ≤ as_of`, predecessor ÷ `ratio_to`); `option_summary(...) -> OptionsBlock` with every
  spec §7.1 field: open rows (`underlying`, `underlying_price` + date from ONE `get_latest_price` read
  per distinct registered underlying — never added to `price_reads` / freshness; omitted when
  unpriced, `premium_total`, `past_expiry` = expiry < valuation
  day and no close row, `unbookable_close`, 履約義務), `obligation_subtotals`, `market_value`,
  `unrealized`, `refused_closes[]` (with the remedy text), `realized.rows` (the replay's option
  `RealizedRow`s + `close_reason` + `contract`), `by_contract` / `by_year` / `by_underlying`,
  `realized_total`, `trend_excludes_value`, `note`. Add the block's row models to
  `test_frontend_never_computes_money.py`'s scan; classify its money products for
  `test_settled_notional.py` (valuation) or allowlist with reasons.
- [ ] **Valuation seam**: options priced from `last_trade_prices`; left out of the freshness use of
  `price_reads`.
- [ ] **Split the lists** per invariant 1: `holdings`, `realized.rows` + totals, allocation, weights,
  `holdings_subtotals`, ex-dividend calendar / projection — stock-only via the same functions; KPI
  totals, XIRR final value, FX `foreign_stock_value` — stocks + options, skipped when `None`.
- [ ] **Trend**: skip `is_option` holdings without `incomplete`; `options.trend_excludes_value`; TWR
  (`performance.py`) and the benchmark read the same flag.
- [ ] **Payload**: `options` set iff the uncut ledger has ≥ 1 transaction or action on an option.
- [ ] Tests (fail first): every §4.1 row; §4.3 table on long and short; opposite-direction merge
  refused; walker agreement; last-trade price across accounts and an adjustment; KPI = stocks +
  options, `holdings` / weights / subtotals stock-only (weights sum to 100 %); trend excludes without
  `incomplete`; XIRR includes the open option; `past_expiry`; `underlying_price` present only when
  priced; golden unchanged; no `options` key on a stock ledger or for a registered-but-untraded
  contract.
- **Done when:** targeted tests, ruff, mypy, architecture tests green; commit `feat(options): W2
  replay (OPTION_ADJUST, refused closes), last-trade valuation, trend exclusion, options block`.

## 4. W3 — Doors, validators, fees, converter, cash lines, input wires (wave 1, subagent B)

**Files:** `data_ingestion/{validate,csv_import,corporate_action_import,opening_import,import_templates,
fees,config_seed,fee_overrides,agents}.py`, `data_ingestion/broker/{ir,schwab,grouping,convert,
reconcile}.py`, `portfolio/cash.py` (`CashLine` field only), `api/routers/{input_center,ledgers,cash,
fee_rules,broker_import}.py`, `api/wire.py`; tests under `tests/data_ingestion/`, `tests/contract/`,
`tests/api/`.

- [ ] **Structural + close-row helper** (`validate.py`): `TxnInput` gains `close_reason`, `contracts`;
  `transaction_structural_issues` accepts `price == 0` only with `close_reason` (fail-first: a price-0
  row without it is still refused); ONE helper `option_close_issues(...)` (spec §4.2: derived side via
  `shares_through(..., pending=siblings before ∪ replacement)`, quantity, dates, after-expiry and
  same-day-after-expiry refusals, multiple of 100 with the 口數 hint) used by every door and by the
  inbox confirm; `dividend_on_option`; opening import refuses option symbols; `validate_corporate_action`
  for `OPTION_ADJUST` (spec §4.3, `is_standard_adjustment`, `shares_before_action_on` non-zero
  signed, destination direction), option symbols refused for SPLIT / EXCHANGE / SPINOFF, the
  SPLIT-with-open-options soft finding; `unknown_action_kind` lists the four kinds from `KIND_ZH`.
- [ ] **Fees**: every door sets fee = tax = 0 on a `close_reason` row without the engine (snapshot
  `{"source": "close_reason"}`); a supplied non-zero fee / tax refused 「到期作廢／被指派／履約列不收手續費與稅」;
  `FeeRuleSet.option_per_contract` / `option_reg_per_contract`; `compute_fees(..., is_option=)` →
  `_us_option` (bypasses `_us`; 0 / 0 at notional 0; per contract + reg + SEC on sells; snapshot
  `{"option": true, "contracts": N}`); schwab 0.65, moomoo_my None → the spec §8 refusal text with the
  settings link; `fee_rule_label` 「每口 0.65＋SEC（賣出）」; both keys editable in `fee_overrides`
  (`_RATE_FIELDS` / `_AMOUNT_FIELDS`, `EDITABLE_FIELD_ORDER`), on `/api/fee-rules` `fields` via
  `fee_rules.py` / `wire.py::fee_rules_wire` — re-pin `tests/api/test_r7_fee_rule_readonly_wire.py` and
  the DEF-010 test in the same commit.
- [ ] **Registration at the doors** (OPT-D7): CSV transaction / corporate-action doors and the manual
  door call `register_option_contract` inside the commit's transaction; the AI door's option rows
  commit through the CSV path (`agents.py` edits only if a test shows it needs one).
- [ ] **CSV + template**: `close_reason` column; hints and the example note (spec §8); preview rows'
  `data` gains `contracts`, `contract_name` (re-pin the preview-shape tests in the same commit); soft `option_premium_exceeds_strike` on puts; re-pin the template and
  DEF-026 tests; the corporate-action CSV accepts `OPTION_ADJUST`.
- [ ] **Manual door** (`input_center.py`): compose endpoint `GET /api/input/option-symbol`;
  `ManualBody.contracts` / `close_reason`; option-mode side mapping, OPT-D14 refusals, server-derived
  expiry side; preview `share_equivalents`, `contract_name`, withholding on `unbookable_close`; the
  two-leg 「被指派／履約」 (`legs` preview, one transaction, `ack_oversell` as the pair's only ack,
  underlying auto-register or refusal 「請先登錄標的 XYZ」; the commit answers `txn_ids: [option,
  stock]` + per-leg totals); `/api/input/context` instruments carry
  `asset_type`; **`/api/input/holdings`: an option with a non-zero signed count is `held`** with
  `asset_type`, `direction`, `contracts`, signed `shares` (stock rows unchanged).
- [ ] **Ledger doors** (`ledgers.py`): `GET /api/ledgers/transactions` rows gain `close_reason`,
  `contracts`, `option_side` (ONE `classify_option_rows` call per request over the stored option rows
  and `OPTION_ADJUST` rows of every account on the page — the classifier keys by (account, symbol)),
  `refused_close` (from the replay's `Book.refused_closes`); the
  edit door runs the close-row helper with `replacement`; the corporate-action doors accept
  `OPTION_ADJUST` (a row with `ratio_from ≠ 1` refused 「選擇權合約調整以 1 口為基準，請填 1 → N」); the CA
  preview returns `strike_products` / `conserves`.
- [ ] **Cash lines**: `CashLine.option_side: str | None = None`. **`account_statement(...)`** (the
  statement wrapper, `portfolio/cash.py`) takes a required `option_side_of: Mapping[int, str]` and sets
  the field inside it (`dataclasses.replace` — `CashLine` is frozen); **`pool_lines` and
  `cash_balances` are unchanged** (the guards' pool arithmetic never sees a label, and A's
  `networth.py` is untouched). Its callers — the statement route (`api/routers/cash.py`, B) and the
  statement export (E) — compute it once with `classify_option_rows`; B updates the two direct calls in
  `tests/portfolio/test_cash_running.py`; the route puts `option_side` on the statement wire.
- [ ] **Schwab converter + broker import**: option rows stop being dropped; `from_broker_option` →
  OCC; **quantity = `contracts_to_shares(Quantity)`**, price per share (test the reconciler's
  qty × price = Amount on an option row); the four sides; `Expired` and the TD removal journal
  (re-map `schwab.py`'s rule, fix `ir.py`'s comment) → price-0 `expired` dated the expiry, side from
  the running position across every file of the request **plus `known_positions`** — a required
  argument of `convert(...)`, supplied by `api/routers/broker_import.py`: the ledger's signed
  share-equivalents per (account, contract) through the day before the request's earliest row
  (OPT-D19); same-day merge, written after the day's trades; standard journal pairs →
  `OPTION_ADJUST` with `ratio_to = |to| ÷ |from|` only when a whole number (else
  `option_nonstandard_adjust`); the conversion report adds no key (display name in the `symbol` cell,
  「2 口（200 股當量）」 in the `shares` cell); 「…OPTION OUT」 and new 「…OPTION IN」 rules → dropped with a reason; `UnmappedRow`
  carries `reason` (`option_assigned_unmapped` / `option_nonstandard_adjust` /
  `option_expiry_no_opening`) and the line; `broker_import.py`'s 422 body carries both.
- [ ] Tests (fail first): every rule with its zh message; CSV round trip of the raw export with option
  rows; template rows commit clean; fees per account and the fee-free close; the two-leg commit both
  or neither; `/api/input/holdings` lists a short contract as held; the compose endpoint (ok, digit
  root, malformed strike); `option_side` on ledger rows and cash lines equals `classify_option_rows`;
  the converter on a synthetic Schwab file with every option row shape, units vs Amount, an expiry
  whose opening is only in `known_positions` (and a request that overlaps already-imported rows,
  caught at the CSV door), a 2 → 6 journal pair read as 1 → 3, and each `UnmappedRow` reason.
- **Done when:** targeted tests, ruff, mypy; commit `feat(options): W3 doors — validators, fees,
  CSV + manual + converter, option sides, input wires`.

## 5. W4 — Fetch scope, registration refusals, stock readers (wave 1, subagent C)

**Files:** `shared/instrument_scope.py`, `api/instrument_service.py`, `scheduler/jobs.py`
(`refresh_instrument_quote`), `api/{fundamentals_service,news_service,alert_inputs,signals_service,
insight_service,digest_service}.py`, `strategy/{rebalance,whatif,alerts}.py`,
`api/routers/{strategy,instruments,prompts,actions}.py`, `llm_insight/variables.py`; new
`tests/contract/test_options_excluded_from_stock_readers.py`.

- [ ] `TRACKED_SQL` / `tracked_instruments`: `asset_type = 'stock'` (the dividend inbox is covered).
- [ ] `quick_register` and `restore_archived` refuse an OCC-shaped symbol **at entry, regardless of
  `force`** 「選擇權合約由交易登錄建立」; the gap backfill, `refresh_instrument_quote`, the Alpha Vantage
  held universe and held-only news refuse an option — each with a mocked-provider test.
  `resolve.py`'s register-first path meets the entry refusal (test; no edit expected).
- [ ] Readers (OPT-D10): `data.holdings` is stock-only by construction — the scan proves it; filter
  where a reader reads the registry or the book (target weights, rebalance, what-if, alerts, alert
  inputs, signals, insights, LLM variables, prompts, digest incl. `_chores`, news, fundamentals);
  `/api/instruments` elements gain `asset_type`; the watchlist hides options unless
  `include_options=1`.
- [ ] Scan test: every module iterating `book.holdings`, calling `held_among` / `holds_position` /
  `held_in_book`, reading `tracked_instruments`, or `strategy/whatif.py` applies the stock filter or is
  an allowlist entry with its reason.
- **Done when:** targeted tests, ruff, mypy; commit `feat(options): W4 options out of every fetch scope,
  registration path and stock reader`.

## 6. W5 — Web UI (wave 2, subagent D)

**Files:** `web/{index.html,app.js,charts.js,ledger.js,trades.html,input.js,sym-picker.js,
corp-action-form.js,broker-import.js,detail.js,instruments.js,cash.js,shell.js,dividend-inbox.html,
settings-fees.js}`,
new `web/options.js` (if the section is split out) and `web/expiry-inbox.js`, `shared/whatsnew.py`,
`api/routers/symbol.py`; tests under `tests/contract/`, `tests/e2e/`.

- [ ] **選擇權 section** (spec §7.1 layout): sort, columns, 標的現價 beside 履約價, 履約義務合計, expander,
  ≤ 640 px cards, empty state, 已實現 tabs and reason chips, the `past_expiry` badge + 「登錄到期作廢」 →
  `window.pdPrefillOptionClose` (the `pdPrefillManualSell` pattern), the R3 note; the KPI 「其中選擇權」
  line; the 已實現損益 option line linking to the section; the trend / B / TWR / benchmark note; the
  待釐清 banner from `options.refused_closes` with 「前往該筆交易」 (`openDeepLink` gains
  `?ledger=tx&txn_id=`).
- [ ] **Manual entry**: picker footer 「＋選擇權合約」 (US accounts) → builder → compose endpoint; held
  contracts in 已持有 as 「XYZ 2026-12-18 50 Put・賣方 2 口」, hint 「可平倉 N 口」; option mode labels,
  four sides with the position default, 口數 default, preview labels, 「到期作廢」, 「被指派／履約」 legs
  card, the inline 「轉倉：賣出開倉同標的新合約」 button after a 買進平倉 commit, the fee refusal link.
- [ ] **Ledger**: §6.1 chips from `option_side`, 口數 beside share-equivalents, 「未入帳（待釐清）」 on
  `refused_close`, keyword filter on the display name; the delete dialog pre-lists later close rows of
  the same contract; the edit dialog shows 「N 口（＝ 股當量）」 and the reason chip (rows stay editable).
- [ ] **公司行動 form**: the fourth kind, 來源 = held contract fixed at 1 口, 目的 = underlying + new
  strike → compose endpoint + 目的 口數 (= `ratio_to`), `strike_products` / `conserves` line; tab hint; `input.js` CSV hint lists
  four kinds.
- [ ] **Broker import page**: `BLOCK_ZH` per-reason remedies with the line number; `TYPE_ZH` §6.1
  labels; 「2 口（200 股當量）」.
- [ ] **Drawer** (`detail.js`, `symbol.py`): contract header, chips, reason chips, 試算 note, 待釐清, the
  footer from `options.open` (unit 股當量); the stock drawer's 未平倉選擇權 line.
- [ ] **Expiry inbox panel**: `web/expiry-inbox.js` + its tag in `dividend-inbox.html`; items, 確認到期作廢,
  改登錄被指派／履約 (deep link), 略過 / 取消略過, 全部確認 via `confirmDialog`, per-item results; an item
  on a contract with a refused close shows 待釐清 + remedy instead of the confirm button; the sidebar
  badge (`shell.js`) adds the inbox count.
- [ ] **Fee settings** (`settings-fees.js`): labels 「選擇權每口手續費」／「選擇權每口監管費」 and their field
  order; shown for US rule sets only.
- [ ] **Watchlist** filter; cash page descriptions from `option_side`; the what's-new entry with
  spec §8's title, href, target `#m-sym-foot` and area.
- [ ] Static tests (`?v=` stamps, `names.js` before `api.js`, page lists, `ledger-count-ok`, state
  colour classes, no native dialogs, no horizontal scroll); the DEF-056 seed rule; e2e: section from a
  seeded option ledger, option-mode STO → BTC, the roll button, the inbox confirm and confirm-all, an
  open contract's drawer footer green, the section absent on the golden ledger.
- **Done when:** targeted contract + e2e chunks green, ruff, mypy on `symbol.py`; commit
  `feat(options): W5 選擇權 section, option entry, inbox panel, drawer, ledger, what's-new`.

## 7. W6 — Exports and the expiry inbox backend (wave 2, subagent E)

**Files:** `export/{holdings,holdings_report,realized,tax,ledgers_report,cash_statement,symbol_detail,
rebalance_report}.py`, `api/routers/export.py` (if a signature changes), new `api/option_expiry.py`
and `api/routers/option_expiry.py`; tests under `tests/export/`, `tests/contract/`, `tests/api/`.

- [ ] Exports per spec §9 (持股 block only when `options` present; realized CSV rows +
  `asset_type` / `contract` with the header re-pinned; 稅務包 contract name + subtotal; 帳本報告 chips,
  口數, refused-close marks, `OPTION_ADJUST`; 現金收支明細 from `option_side` (the export computes
  `option_side_of` once with `classify_option_rows` and hands it to the statement-line builder); 個股明細 for a contract;
  再平衡 excludes options; raw zip round trip). Every other export of a stock ledger byte-identical.
- [ ] **Expiry inbox backend** (spec §7.2): `api/option_expiry.py` — `ensure_tables` (`option_expiry_skips`),
  `pending_expiries(conn, today)` (compute-on-read from the book: non-zero signed option positions
  with expiry < today, no close row, not skipped; zh item text, server strings), `confirm(...)`
  (writes the price-0 row through B's close-row helper and the manual door's write path), `skip` /
  `unskip`; `api/routers/option_expiry.py` — list, count, confirm, confirm-all (per-item results),
  skip, skipped, unskip; every GET read-only. The main session wires `ensure_tables` and the router in
  `api/app.py`.
- [ ] Tests (fail first): detector over expired / live / closed / skipped / short and long contracts;
  confirm writes exactly the §4.1 row; confirm-all reports per item and stops nothing on one failure;
  count 0 on a stock ledger; the DEF-065 GET sweep covers the new GETs.
- **Done when:** targeted tests, ruff, mypy; commit `feat(options): W6 exports + expiry inbox backend`.

## 8. W7 — Oracle, manual, acceptance

**W7a — oracle (wave 2, subagent F; `scripts/stress_audit/*` only).** From the spec, never importing
the app (`grep -E '^\s*(import|from)\s+portfolio_dash' oracle.py` prints nothing):
- [ ] `load_facts_from_db` reads `close_reason`; option terms parsed from the symbol by its own parser.
- [ ] `oracle.py`: price-0 closes, §4.4 refusal, `OPTION_ADJUST` (`_KINDS`, branch, `ratio_from == 1`,
  `replay()`, `integrity_findings`), an option branch in `fee_tax` (0 / 0 on a close; per contract +
  SEC on sells), last-trade valuation, the trend exclusion.
- [ ] Phase-1 ops: STO→BTC · STO→expire · BTO→STC · BTO→expire · 1→3 on a short · 1:1 rename · sold-put
  assignment (both legs) · same-day BTC + expiry · cash ties per pool. Assertions: realized per
  contract, open position, cash, KPI = stocks + options, trend excludes the option value.
- **Done when:** phase 1 green; the new baseline recorded; commit `test(stress-audit): options`.

**W7b — wave 3, main session:**
- [ ] `api/app.py`: the inbox router and `ensure_tables` at boot.
- [ ] Cross tests at the merges: validators over an adjusted contract; an expiry after an adjustment;
  the manual preview over a refused close; the 交易 tab marks a refused close; `option_side` identical
  on the ledgers wire, cash lines and exports; `/api/input/holdings` direction agrees with
  `options.open`; the inbox confirm through B's helper; the §6.1 vocabulary guard.
- [ ] Accounting manual zh + en: an options section with its anchor; version bump + history row.
- [ ] Rule files: `domain-ledger.md`, `data-and-pricing.md`, `markets-and-fees.md`, `architecture.md`
  (the new modules, the reader-exclusion seam, `known_positions` injection, the inbox in `api/`).
- [ ] CHANGELOG `[Unreleased]`: R1–R7, OPT-D1..D19 and the feature; `grep -c "^## \[v"` = 30.
- [ ] Final review (xhigh on `main..feat/options-ledger`), fixes with fail-before tests, heavy gates
  once.
- [ ] **Real-data acceptance** (local, throw-away database): the owner's Schwab option rows through
  the converter (all exports in one request, and split across requests to exercise `known_positions`)
  and the CSV door — option realized, the open contract, the option cash total equal the git-ignored
  `OPTIONS_ledger_proposals.md`.
- [ ] Report to the owner. Demo deploy on the owner's word (additive migration pre-authorised); back up
  and verify the demo DB first; prod only on 「上」.

## 9. File ownership (no two subagents in one file)

| Wave 1 — A (W2) | Wave 1 — B (W3) | Wave 1 — C (W4) |
| --- | --- | --- |
| `portfolio/{cost_basis,options,dashboard,timeseries,allocation,dashboard_models,pnl,position_aggregate,dividends}.py`, `data_ingestion/holdings.py`, `shared/models/ledger.py`, `api/replay_guard.py`, `api/routers/performance.py`, CA spec §4.4 row, `tests/portfolio/test_corporate_actions.py` | `data_ingestion/{validate,csv_import,corporate_action_import,opening_import,import_templates,fees,config_seed,fee_overrides,agents}.py`, `data_ingestion/broker/*`, `portfolio/cash.py`, `api/routers/{input_center,ledgers,cash,fee_rules,broker_import}.py`, `api/wire.py`, `tests/portfolio/test_cash_running.py` | `shared/instrument_scope.py`, `api/instrument_service.py`, `scheduler/jobs.py`, `api/{fundamentals_service,news_service,alert_inputs,signals_service,insight_service,digest_service}.py`, `strategy/*`, `api/routers/{strategy,instruments,prompts,actions}.py`, `llm_insight/variables.py` |

| Wave 2 — D (W5) | Wave 2 — E (W6) | Wave 2 — F (W7a) |
| --- | --- | --- |
| `web/*`, `shared/whatsnew.py`, `api/routers/symbol.py` | `export/*`, `api/routers/export.py`, `api/option_expiry.py`, `api/routers/option_expiry.py` | `scripts/stress_audit/*` |

Main session only: W1's files, `CHANGELOG.md`, `api/app.py`, `tests/conftest.py`, `.claude/rules/*`,
the accounting manuals, worktree merges, cross tests. `api/snapshots.py` needs no change (OPT-D12).

## 10. Risks

| Risk | Mitigation |
| --- | --- |
| A close row refused by the structural price check | W3's first bullet + its fail-first test |
| An engine fee on a close row | doors skip the engine; `_us_option` 0 / 0 at notional 0 |
| A stock reader nobody listed sees an option | W4's scan test; `data.holdings` stock-only by construction |
| A new wire field changes the golden | invariant 1; new dashboard fields only inside `options` |
| A close left behind books a phantom lot | §4.4 refusal + record-based XIRR gate + replay guard + delete pre-list |
| Registration of an OCC symbol as a stock | entry refusal in `quick_register` / `restore_archived` regardless of `force` |
| Fractional contracts from an adjustment | `ratio_from == 1` (OPT-D16) in the validator, the converter and the oracle |
| Expiries forgotten, positions stale | `past_expiry` badge + the expiry inbox |
| 100× quantity error | multiple-of-100 + the 口數 hint at every door; `contracts` on the manual door; converter multiplies |
| A browser-side money or symbol computation | compose endpoint; server preview fields; the money-scan test |
| Demo data | backup + verify before the deploy; prod only on 「上」 |

## Appendix — code map (verified at `aa5c80a`; re-check line numbers before editing)

**Replay** — `portfolio/cost_basis.py`: `_reject` / `UnappliedAction` :225-263 (the record, not the
flag, gates :240-246); `_apply_action` def :266; E1 :298, E2 :304, E3 :310; SPLIT :315-324; **E5
block :326-331**; E18 :338-341; E22 :346; SPINOFF fall-through :391-415. `_Position` (17 fields)
:70-155; cover :714-751 (per-share 0 at price 0 :720-742); declared SELL :752-785; dividend on a short
:860-880; signed output :1007-1009; `opened_positions` :1014-1016; 0-share skip :1029-1030;
`short_open` :1058; qty guards :643-710; sort key :592-603. `ledger_events.py` :46-53. `results.py`
`Holding.unbookable_action` :105, `RealizedRow.kind` :166. Field-count test
`tests/portfolio/test_corporate_actions.py` :352; CA spec §4.4 table :532-585.

**Action vocabulary** — `shared/corporate_actions.py` :36-52, `is_ratio_term` :68-78, validator
:111-131, `convert_stored` :240; `schema.py` :63/:65. Kind registration: `validate.py`
`unknown_action_kind` :1145-1150 (text :1148), E-codes :1161-1549, `shares_before_action_on` :1265;
`corporate_action_import.py` :61-64/:71-76/:379/:389; `api/routers/ledgers.py` :1101,
:1754/:1887/:2041/:2069, `_holding_accounts` :1242-1245; `symbol.py` :345-350; `store.py` :1452/:1474;
`import_templates.py` :88/:154-169; `broker/grouping.py` :502-508; web `corp-action-form.js` :133-140
/:686-703, `ledger.js` :1020, `input.js` :1115, `broker-import.js` :82-88, `trades.html` :448-449;
`test_i14` `_PAIR` :35.

**Share walker and validators** — `holdings.py` `_delta_of` :287-352 (skip :328-333, SPLIT exempt
:324-327), `shares_through` :450-474, `created_by` :676; `ledger.py` `opened_by` :319. `validate.py`
`TxnInput` :117, `pending_share_flows` :475 (→ `row_cannot_be_written` :489-497), `siblings_booked_before`
:511, `_replacement_flows` :534 (call sites :803-832), structural price / qty :629-636 (:633-634), 賣超
guard :778-873, `_dividend_findings` :2231-2440.

**Replay guard** — `api/replay_guard.py` codes :78, `_replay_block` :287-363, orphan 422 :394-396.

**`unbookable_action` readers** — `pnl.py` :39; `timeseries.py` :391; `position_aggregate.py` :172 (→
`symbol.py` :259); `dividends.py` :85; `input_center.py` :228/:252; `web/app.js` :702/:733 (banner
`renderUnappliedBanner` reads `D.unapplied_actions` :147); `web/detail.js` :558-583.

**Instruments and registration** — `assets.py` :38-70; `schema.py` :9-20, `_add_column_if_missing`
:104-109; `store.py` :162-227/:580/:623/:634; `register.py` :33-79, `autoregister_spinoff_child` :82,
`spinoff_child_draft` :119-135; `instrument_service.py` `_gap_backfill_inner` :417-435, `gap_backfill`
:438, `quick_register` :462-598 (quote gate `if not quote_ok and not force` :558), `restore_archived`
:348; `routers/instruments.py` `_element` :91-135, POST :543-617 (`force=True` :600-604, backfill
:613), quick :630-660, `{symbol}` :663/:730/:761/:801; `symbol_format.py` :25-29; `resolve.py` :81;
CSV unregistered `csv_import.py` :289-306; opening `opening_import.py` :210-217; manual auto-register
`input_center.py` :1124-1179.

**Fetch scope** — `instrument_scope.py` :38/:50-71; `jobs.py` :470-477/:480-530/:1565-1585;
`api/app.py` :243 → `actions.py` :34-57; `pricing/ingest.py` :55-80; `fundamentals_service.py`
:24-37; `news_service.py` :141/:164; `api/dividend_inbox.py` :55/:236 (`tracked_instruments`);
`yfinance_provider.py` :39-53.

**Valuation and payload** — `dashboard.py` `build_dashboard` :624-1200; price reads :713-735; XIRR
reason :844-845; FX stock value :858-868; ex-div :940-957; `combined_view` → :961/:1121; weight
:967-973/:1137-1150; `HoldingRow(**data)` :986-997; cash series :1046-1068; `RealizedPnL(rows=…)`
:1078; `total_return` → :1078-1089; freshness `prices` :1151-1156, flags :1173-1174; subtotals
:317-390; `fx_complete_return` :489-533; benchmark :539. `pnl.py` :55-66; `returns.py` :39-87,
flows :361-365, XIRR :388-396; `allocation.py` :16-65. `dashboard_models.py` `TrendPoint` :211-221,
`exclude=True` :427-443. `api/routers/dashboard.py` `to_wire(data.model_dump())` :47. Golden
`test_spec17_financials.py` :613. 月度成績 `web/app.js` :1018-1025 → `api/snapshots.py` :44-63. Money
scan `test_frontend_never_computes_money.py` :34-70; settlement scan `test_settled_notional.py`
:122-173.

**Trend** — `timeseries.py` :282-445 (:382-433; short :405-409; no price :410-413), flows :198-204;
`web/charts.js` :68-70/:121-122; `index.html` :81.

**Doors** — `csv_import.py` :55-58/:66/:177-200/:319-387 (compute when fee or tax None :319-345)/:614;
`fees.py` `supplied_snapshot` :103-134, `_us` :208-255 (notional ≤ 0 return :214-215, SEC floor :241),
`compute_fees` :297-336; `config_seed.py` :42-54/:134-143; `fee_overrides.py` :46-60/:80-86;
`wire.py` `fee_rules_wire` :163-185; `input_center.py` `/api/input/context` :139-167, `/api/input/
holdings` `held` = `shares > 0` :387, `ManualBody` :402-443 → `TxnInput` :446-454,
`_cash_overdraft_issue` :560-595, `manual_preview` :1026-1102 (`fee_rule_label` :1094), commit :1106+
(only `ack_oversell` gates :1186-1190), import commit `ack_warnings` :1589; `ledgers.py`
transaction rows :262-290; cash `portfolio/cash.py` :283-289, `routers/cash.py` :615-640,
`web/cash.js` :345-346; manual UI `trades.html` :100-114, `input.js` :193-211/:252/:284-318/
:451-534 (`openManualQuickAdd` :451), `pdPrefillManualSell` :1002, `onManualWritten` :1046,
`CSV_HINTS` :1108; `sym-picker.js` footer :24; `shell.js` toast :748, inbox badge :908-931.

**Converter** — `broker/ir.py` :76-86/:117-120/:170-172; `schwab.py` :95-100, journal rules :108-122
(OUT :116), :298-321; `grouping.py` :699-700; `convert.py` `UNCONVERTIBLE` :63-69, `RowDetail` :75,
trade rows :160-192, `option_row_unsupported` :349-354, CSV out :435; `reconcile.py`
`_check_priced_rows` :182, advisory :453-459; `api/routers/broker_import.py` :15, `_opening_gaps`
:127, parse / convert :336-348, 422 `broker_row_unmapped` :340-345; `web/broker-import.js`
`TYPE_ZH` :82, `BLOCK_ZH` :106, 「請回報」 :404-407.

**Inbox pattern** — `api/rebates.py` (compute-on-read; `rebate_skips` DDL :55, `ensure_tables` :180,
skip insert :380); `api/app.py` boot ensures :205-211; `api/routers/dividend_inbox.py` routes
:21/:48/:61/:71/:82/:95; `web/dividend-inbox.html`, `web/inbox.js`, `web/rebate-inbox.js`;
`shared/whatsnew.py` :77-85.

**Exports** — `export/holdings.py` :61-63; `holdings_report.py` :147/:203/:282; `realized.py` :25-45
(:33), header `test_export_realized.py` :28; `tax.py` :103-116 (:116); `ledgers_report.py` :110/:185/
:379; `cash_statement.py` :198; `symbol_detail.py` :58; `rebalance_report.py` :322/:337;
`ledger_registry.py` `LEDGER_TABLES`; `test_def026_ledger_export_reimports.py` :291. DEF-056 sweep
`tests/e2e/test_def056_r5_future_row_sweep_flow.py` :34/:90/:96-147.

**Oracle** — `scripts/stress_audit/common.py` :501-565; `oracle.py` `fee_tax` :218-235, `RealizedRow`
:505-520, `EventPriority` :559-577, `_Pos` :581-596, `_apply_action` :709-861 (`_KINDS` :633-634,
E-kind :769-770), `replay()` :947-1070, `integrity_findings` :1574+; README :244.
