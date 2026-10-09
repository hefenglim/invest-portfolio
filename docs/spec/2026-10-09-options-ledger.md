# Spec — US equity options in the ledger (P3, approach ④)

**Status:** APPROVED 2026-10-09 — rulings R1–R7 and decisions OPT-D1..D10 confirmed by the owner in
chat (「10 項全部照建議」 — all ten as recommended). Implementation follows the plan
`docs/superpowers/plans/2026-10-09-options-ledger.md` on branch `feat/options-ledger` (off `main`
after `feat/corporate-actions` was merged, `aa5c80a`), and starts only after the owner approves the
plan.
**Builds on:** the corporate-action framework, the replay and the declared short sale (merged to
`main` in `aa5c80a`).
**Supersedes:** the P3 section of `docs/spec/2026-08-06-broker-import-backlog.md` as the plan of
record for options. That section's analysis (the multiplier site census, the "direction is not a
gap" finding) is still accurate and is the starting point here.

**Why now.** A real broker history cannot reconcile without its option legs: the premiums are cash
the account really received and paid, so a ledger that leaves them out ends with a cash balance the
broker never had. The owner's acceptance figures from that history live in the git-ignored
`sample-trade-data/charles-schwab/OPTIONS_ledger_proposals.md` (real data — never copied into a
committed file); this spec uses synthetic examples only.

## 1. Scope

**In:** US equity options (single-leg calls and puts, standard 100-share deliverable) in the US
accounts — `schwab` and the US market of `moomoo_my`. Open / close in both directions, expiry,
assignment, exercise, contract adjustments, valuation of an open contract, a separate 選擇權 section,
every import and export door.

**Out (v1):** TW and MY options (R6: not planned); multi-leg strategy
grouping, roll chains, collateral reservation, quotes / Greeks for open contracts, option-aware AI
insights and alerts — all deferred to approach ⑤ (§12). Index and non-equity options. Non-standard
deliverables (cash-in-lieu, mixed baskets) are refused, not approximated.

## 2. Owner rulings and the decisions this spec asks the owner to confirm

**Ruled 2026-10-09 (in chat):**

| # | Ruling |
| --- | --- |
| R1 | Approach ④: each contract is an instrument in the existing trade ledger; sell-to-open is the existing declared short sale |
| R2 | An open contract is valued at its **last trade price** from the ledger |
| R3 | The **historical trend excludes** option market value, with an explanatory note |
| R4 | Options are shown in their **own 選擇權 section** |
| R5 | On assignment / exercise the **premium stays option P&L** (not folded into the stock's cost) |
| R6 | **US only**; TW / MY options are not planned |
| R7 | Approach ⑤ (a dedicated sub-ledger) is kept as a planned TODO, started only on demand (§12) |

**Confirmed 2026-10-09 — every decision as recommended:**

| # | Decision | Confirmed answer | Why / rejected alternative |
| --- | --- | --- | --- |
| OPT-D1 | Quantity unit | **Share-equivalents** (contracts × multiplier); price = premium per share | Every `quantity × price` in the package (≥ 28 sites, backlog F-46) stays correct untouched; a multiplier field would have to be threaded through all of them |
| OPT-D2 | Expiry / assignment / exercise | A **price-0 closing trade** with `close_reason` ∈ {`expired`, `assigned`, `exercised`} | Brokers list them as transaction rows; a same-day buy-to-close then sorts by write order. As a corporate action it would run BEFORE that day's trades (actions sort first) and the action framework demands positive integer ratios and a `to_symbol` |
| OPT-D3 | Contract adjustment (underlying split, merger / ticker change) | New corporate-action kind **`OPTION_ADJUST`**, option → option, carrying long AND short lots by the ratio | Today's `EXCHANGE` refuses a short source in three places (replay E5, validator, share walker — D33). Widening it would change equity behaviour; a new kind leaves equities byte-identical (D38) |
| OPT-D4 | Which figures include an open option | **Point-in-time** figures include it at its last trade price: KPI 總市值 / 總報酬 / XIRR, total return A, the FX pool. **Trend-derived** figures exclude its value: trend chart, B (含匯兌總損益), TWR, benchmark, 月度成績 — one shared note | R3 + consistency: XIRR's final value must carry the open liability or a short put's premium counts as fully earned. Consequence (stated in the note): while a contract is open the trend's net worth carries its premium in cash without its value — above the truth for a sold contract, below it for a bought one |
| OPT-D5 | Where options appear | 持倉明細, allocation and weights **exclude** them; KPI totals **include** them with a 「其中選擇權」 breakdown line; the 已實現損益 section shows one aggregated 選擇權 line linking to the 選擇權 section | R4, without dropping option P&L from the totals |
| OPT-D6 | Fees | Per-account `option_per_contract` commission (config; Schwab 0.65) + SEC on sells (existing rule, notional basis) + optional `option_reg_per_contract` (default 0). A statement-supplied fee always wins. `moomoo_my`: no default — the fee is required until the owner sets the rate | Real statements show 0.65 + a few cents of regulatory fees; the engine must not guess a rate it was never given |
| OPT-D7 | Registration | An option instrument is **created from its symbol** (the terms are encoded in it) at every door — CSV, manual, broker converter — with no quote lookup; the underlying need not be registered | Today a CSV symbol must be pre-registered and `quick_register` demands a live quote, which no option has |
| OPT-D8 | Entry units | CSV `shares` = share-equivalents (must be a multiple of the multiplier; the preview shows 「= N 口」); the manual form takes **口數** and converts | The raw ledger CSV must round-trip (DEF-026); a typed "1" meaning one contract is refused as not a multiple of 100 instead of booking 1/100 of a contract |
| OPT-D9 | Assignment / exercise rows in the broker converter | **Not mapped blind**: an `Assigned` / `Exercise` row keeps raising `UnmappedRow`; the manual door and CSV support them now | Same rule as the missing wire-out mapping: no rule without a real row behind it |
| OPT-D10 | v1 exclusions | Options are out of: AI insights / signals / alerts / digest / news / fundamentals, the watchlist list (filterable), target weights, rebalance, what-if, the ex-dividend calendar | Each reader treats a held symbol as a stock; listed in §6 so none is missed |

## 3. Data model

**`instruments.asset_type`** `TEXT NOT NULL DEFAULT 'stock'` — `'stock'` | `'option'`. Additive
(`_add_column_if_missing`); every existing row reads `'stock'`, so equity ledgers are unchanged.

**`option_contracts`** (new table, owned by `data_ingestion/store.py`):

| Column | Type | Notes |
| --- | --- | --- |
| `symbol` | TEXT PK | = `instruments.symbol` |
| `underlying` | TEXT NOT NULL | e.g. `XYZ`; need not be registered |
| `expiry` | TEXT NOT NULL | ISO date |
| `strike` | TEXT NOT NULL | canonical Decimal (`money.to_db`) |
| `option_right` | TEXT NOT NULL | `C` / `P` |
| `multiplier` | INTEGER NOT NULL DEFAULT 100 | v1 accepts only 100 |

**Symbol** — the OCC form without root padding (the form yfinance uses):
`ROOT + YYMMDD + C|P + strike×1000 as 8 digits`, e.g. `XYZ261218P00050000`. `[A-Z0-9]` only, so
every route that carries a symbol in its path keeps working. `shared/symbol_format.py` gains an
`OPTION` pattern `^[A-Z][A-Z0-9]{0,5}\d{6}[CP]\d{8}$` and one parser `parse_option_symbol` → terms
(one home; the doors, the converter and the store read it). Display name: 「XYZ 2026-12-18 50 Put」.
Market is always `US`, quote currency `USD`.

**`transactions.close_reason`** `TEXT NULL` — `expired` | `assigned` | `exercised`; NULL on every
ordinary trade. Additive.

**`OPTION_ADJUST`** joins `CorporateActionKind` (`shared/corporate_actions.py`) with zh 「選擇權合約調整」.
No schema change (`kind` has no CHECK).

**Models:** `Instrument.asset_type` and `Instrument.option: OptionTerms | None`; `Transaction.close_reason`.

## 4. Ledger mechanics (replay — `portfolio/cost_basis.py`)

A contract is a position like any other, in share-equivalents. Internally the replay keeps a long lot
and a separate positive short lot (`short_shares`, `short_proceeds`); the signed `shares` is derived
on output. Nothing below changes how an equity is booked.

| Broker event | Ledger row | Replay effect | Realized row |
| --- | --- | --- | --- |
| Sell to Open | SELL, `short_sale=1` | sells any long lot first, rest opens / extends the short lot at net proceeds per share | none for the short part |
| Buy to Close | BUY | covers the short lot first (existing rule) | `short_cover` = (short avg − cover cost per share) × covered |
| Buy to Open | BUY | opens / adds to the long lot | — |
| Sell to Close | SELL | sells the long lot | `sale` |
| Expired (short) | BUY, price 0, fee 0, `close_reason=expired` | covers the short lot at 0 | `short_cover` = whole remaining premium |
| Expired (long) | SELL, price 0, fee 0, `close_reason=expired` | sells the long lot at 0 | `sale` = −remaining basis |
| Assigned (short put / call) | the option closes as above with `close_reason=assigned`, plus an ordinary stock BUY (put) / SELL (call) of the underlying at the strike | option P&L = premium (R5); the stock leg is a normal trade | option `short_cover`; stock per its own branch |
| Exercised (long call / put) | option SELL at 0 `close_reason=exercised`, plus a stock BUY (call) / SELL (put) at the strike | premium paid is the option's realized loss (R5) | option `sale`; stock per its own branch |
| Adjustment (e.g. 3-for-1 underlying split; merger ticker change) | `OPTION_ADJUST` old → new, `ratio_to:ratio_from` | moves the long lot (shares × ratio, totals carried) **and** the short lot (`short_shares` × ratio, `short_proceeds` carried) to the new contract | none |

- **Same-day order.** A `close_reason` trade is a TRADE (priority 20), so it sorts by write order; the
  converter writes a day's expiry after that day's trades. `OPTION_ADJUST` is an action (priority
  10): it applies at the start of its day, before trades in the adjusted contract.
- **Validation** (`data_ingestion/validate.py`):
  - price 0 is allowed **only** with a `close_reason` on an option instrument; `close_reason` is
    refused on a stock;
  - an `expired` close must be dated the contract's expiry and close the **whole** open position
    on that date (date-aware, `holdings.shares_through`); `assigned` / `exercised` must be dated on
    or before expiry and may close part of it;
  - a `close_reason` row carries no fee and no tax (the fee engine is not called);
  - a sell that would open a short on an option without `short_sale=1` is the ordinary 賣超
    (undeclared oversell) — never inferred;
  - quantity must be a multiple of the contract multiplier;
  - `OPTION_ADJUST`: both symbols are registered options, positive integer ratios, from ≠ to, a
    position must exist (long or short); SPLIT / EXCHANGE / SPINOFF on an option symbol are refused;
  - a dividend on an option is refused (`dividend_on_option`);
  - entering a SPLIT on an underlying with open options raises a soft finding 「此標的有未平倉選擇權，
    合約調整請另行登錄（選擇權合約調整）」 — the underlying's split never adjusts a contract by itself.
- **Assignment's two legs are entered as two rows** (the converter emits both when the mapping exists;
  the manual door offers 「被指派／履約」 that writes both in one commit). The stock leg is ordinary:
  fees, cash, 賣超 guard and preview all apply.

## 5. Valuation (R2, OPT-D4)

- **Price of an open contract** = the price of its **last ordinary trade** in the ledger (any side,
  `close_reason` rows excluded), dated that trade's date. If the contract has had no trade since an
  `OPTION_ADJUST`, the predecessor's last price re-expressed by the ratio. Read at the single
  valuation seam (`portfolio/dashboard.py` `price_reads` / `price_map`), **never written to
  `prices`** — the spinoff seed stays the only human-typed price row (`data-and-pricing.md`).
- Shown as 「最後成交價 YYYY-MM-DD」, never as a quote; an option is **not** counted in
  `missing_prices` / `any_stale` (a ledger price is not a failed fetch).
- **Point-in-time figures include it:** market value (signed — a sold contract is a liability),
  unrealized = (price − avg) × signed shares (existing formula), KPI totals with a 「其中選擇權」 line,
  total return A, XIRR final value, the FX pool's `foreign_stock_value`.
- **Trend-derived figures exclude its value** (R3): `portfolio/timeseries.py` skips option holdings
  in the daily value **without** marking the day incomplete (else B would be withheld whenever a
  contract is open), and the trend, B, TWR, benchmark and 月度成績 carry one note:
  「走勢不含未平倉選擇權的市值；權利金已計入現金，未平倉期間淨值高估（賣方）或低估（買方）該部位市值」.
  Option cash flows stay in `net_invested` and the cash series — they are real cash.

## 6. Every surface, and what it does with an option

| Surface | v1 behaviour |
| --- | --- |
| Quote jobs | `shared/instrument_scope.py::TRACKED_SQL` filters `asset_type='stock'` — one seam for quotes, history, dividends, inbox scan, FinMind, consensus, fundamentals. The five bypass paths each refuse an option: `quick_register`'s quote gate, `gap_backfill`, `refresh_instrument_quote`, the Alpha Vantage held universe, held-only news |
| Quote-job outcome | an option is never in the worklist, so it can never make a run 部分 |
| Dashboard | new **選擇權** section (§7); 持倉明細 / allocation / weights exclude options; KPI includes with breakdown; 已實現損益 shows one aggregated 選擇權 line |
| Drawer (`/symbol/{symbol}/detail`) | works for a contract: contract header (underlying, expiry, strike, right, multiplier), trades in 口 and share-equivalents, no 配息史, no 試算 (「選擇權不提供試算」) |
| Watchlist / 觀察清單 | contracts hidden by default, a 「顯示選擇權合約」 filter; archive / purge read `holds_position` as today |
| Target weights, rebalance, what-if | exclude options |
| Ex-dividend calendar, dividend projection | exclude options |
| Signals, alerts, alert inputs, insights, LLM variables, prompts, digest, news, fundamentals | exclude options (OPT-D10) |
| Cash page / 現金收支明細 | option trades settle like any trade; the description reads 「賣出開倉 1 口 XYZ 2026-12-18 50 Put」, 「到期作廢」 etc. |
| Ledger tabs (交易) | contract name, 口數 beside share-equivalents, chips 「賣出開倉」/「買進平倉」/「到期」/「被指派」/「履約」; 公司行動 tab lists `OPTION_ADJUST` |

## 7. The 選擇權 section (R4)

On the dashboard, after 持倉明細 (`data-screen-label="選擇權"`), rendered from a new
`options` block of `/api/dashboard` (server-computed Decimal strings only):

- **未平倉** — per account × contract: 標的, 到期日 (剩餘天數), 履約價, 買權/賣權, 方向 (買方/賣方), 口數,
  平均權利金, 最後成交價 (日期), 市值, 未實現損益, 履約義務 (sold put: strike × multiplier × contracts —
  the cash an assignment would take).
- **已實現** — by year and by underlying, with the contract rows behind each (expandable); total.
- Subtotals per currency (USD) and in the reporting currency; the R3 note under the section.
- Empty state when no option has ever been booked: the section is not rendered.

## 8. Import doors

- **Transaction CSV** (`csv_import.py`): new optional column `close_reason`; an option symbol that is
  not registered is registered from its terms (OPT-D7; the preview says 「將建立選擇權合約 …」);
  `shares` in share-equivalents (OPT-D8); fees per OPT-D6. Template gains an option example row.
- **Corporate-action CSV**: kind `OPTION_ADJUST` (alias 「選擇權合約調整」).
- **Manual entry** (`input_center.py`, `web/input.js`): an option symbol switches the form to option
  mode — 口數, the four sides 「買進開倉／賣出平倉／賣出開倉／買進平倉」 (mapping to side + `short_sale`;
  a mismatch with the resulting position is a soft finding), 「到期作廢」 and 「被指派／履約」 actions;
  the preview mirrors the replay's branch (existing rule).
- **Schwab converter** (`data_ingestion/broker/`): option rows stop being dropped. `option_symbol`
  "XYZ 12/18/2026 50.00 P" → OCC symbol; Sell/Buy to Open/Close → trades (`short_sale=1` on Sell to
  Open, fees verbatim); `Expired` and TD's 「REMOVAL OF OPTION DUE TO EXPIRATION」 → `close_reason=expired`
  dated the contract's expiry (the broker posts it a business day later, "as of" the expiry);
  「OPTION ODD SPLIT REORGANIZATION」 / 「OPTION POSITION CHANGE」 journal pairs → one `OPTION_ADJUST`;
  「TRANSFER OF SECURITY OR OPTION OUT」 between the broker's own platforms → nothing (no economic
  effect). `Assigned` / `Exercise` stay unmapped (OPT-D9).
- **AI input door**: an OCC-shaped symbol no longer draws `symbol_format_mismatch`.

## 9. Export doors

| Export | Change |
| --- | --- |
| Ledgers zip / per-tab ledger CSV (raw) | unchanged mechanism; `transactions.close_reason` rides the `SELECT *` dump. The zip carries ledger tables only (`instruments` is not in it), so neither `asset_type` nor `option_contracts` is exported: on re-import an option's terms re-derive from its symbol (OPT-D7), and the CSV round-trips |
| 持股 CSV / 持股報表 (print) | equities as today; a separate 選擇權 block (same columns as §7) and the 「其中選擇權」 KPI line |
| 已實現損益 CSV | new column `asset_type` (+ the contract's display name) |
| 稅務包 | option closes already land in capital gains (`sale` / `short_cover`); add the contract name and a 「其中選擇權」 subtotal |
| 帳本報告 (print) | 口數 and the option chips in the transactions section; `OPTION_ADJUST` in the actions section |
| 現金收支明細 CSV / print | option descriptions (§6) |
| 個股明細 CSV | works for a contract |
| 再平衡報告 | excludes options |

## 10. Verification

- **Hermetic tests** per mechanism (§4 table row by row, both directions, partial assignment,
  adjustment on a short, same-day BTC + expiry order, price-0 refusal on a stock, multiple-of-100,
  dividend refusal), per surface (§6 / §9 — a scan of held-symbol readers, so a new reader cannot
  forget the exclusion), and the e2e flow for the 選擇權 section and option-mode manual entry.
- **Stress-audit oracle** (`scripts/stress_audit/oracle.py`) transcribes, without importing the app:
  price-0 `close_reason` closes, `OPTION_ADJUST` on long and short lots, last-trade valuation, the
  trend exclusion. Phase-1 scenario ops: STO→BTC, STO→expire, BTO→STC, BTO→expire, adjustment 1→3
  on a short, assignment of a sold put (option + stock legs), cash ties.
- **Accounting manual** (`docs/accounting-formula-manual.md` + `.en.md`): a new options section with
  each formula and its verification anchor.
- **Real-data acceptance** (local only, git-ignored): the owner's Schwab history through the doors —
  option realized, the open contract, and the account's USD cash on its last statement date equal
  the figures in `OPTIONS_ledger_proposals.md`.
- **Blast radius (D38):** a ledger with no option is byte-identical in every API payload and export;
  the golden payload does not change.

## 11. Build order (proposed)

`W1` model + schema + symbol parser + registration → `W2` replay (close_reason, OPTION_ADJUST) +
valuation seam + trend exclusion → `W3` doors (CSV, corporate-action CSV, manual, converter, fees) →
`W4` exclusions (§6) → `W5` 選擇權 section + ledger chips + drawer → `W6` exports → `W7` oracle +
manual + real-data acceptance. Heavy gates once, after the final review.

## 12. Deferred — approach ⑤ (dedicated options sub-ledger), TODO plan

Started only when the owner asks. When implementation starts, the options module header
(`shared/options.py`) carries a `TODO(options-⑤)` block pointing here. The triggers and the work:

- **Triggers:** multi-leg strategies (spreads, straddles), frequent rolls the owner wants analysed as
  chains, assignment volume that makes the two-row entry a burden, or a need for live option quotes.
- **Roll chains:** link a buy-to-close and a same-day sell-to-open on the same underlying into a
  chain with its cumulative premium.
- **Collateral:** reserve a sold put's strike × multiplier against the cash pool (「可用現金」 vs
  「帳戶現金」).
- **Quotes and Greeks:** a provider path for open contracts; mark-to-market history so the trend can
  include option value (would retire R3's exclusion).
- **Assignment linkage:** an optional per-assignment choice to fold the premium into the stock's cost
  (the US tax-basis convention), which R5 declines for v1.
- **Option-aware AI and alerts:** expiry reminders, assignment-risk alerts, option context in insight
  prompts.
- **Strategy views:** P&L by strategy, annualised return on collateral.
