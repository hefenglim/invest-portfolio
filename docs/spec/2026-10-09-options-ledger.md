# Spec — US equity options in the ledger (P3, approach ④)

**Status:** APPROVED 2026-10-09 — rulings R1–R7 and decisions OPT-D1..D12 confirmed by the owner in
chat (「10 項全部照建議」 — the first ten as recommended; OPT-D11 / OPT-D12 answered after review
round 1). Implementation follows `docs/superpowers/plans/2026-10-09-options-ledger.md` on branch
`feat/options-ledger` (off `main` after `feat/corporate-actions` was merged, `aa5c80a`) and starts
only after the owner approves the plan.
**Builds on:** the corporate-action framework, the replay and the declared short sale (`main`).
**Supersedes:** the P3 section of `docs/spec/2026-08-06-broker-import-backlog.md` as the plan of
record for options. That section's multiplier census and its "direction is not a gap" finding still
hold and are the starting point here.

**Revision log**

| Date | Change |
| --- | --- |
| 2026-10-09 | First draft; owner rulings R1–R7; OPT-D1..D10 confirmed as recommended |
| 2026-10-09 | **Review round 1** (independent reviewer, Fable 5.1, max effort; verdict NOT READY, all fixes document-level). Folded in: B1 → OPT-D11 (owner: standard adjustments only); B2 → the replay refuses a close that would open a lot (§4.4); M1 → every new wire field lives in the `options` block, omitted when absent (§7); M2 / M10 → `holdings`, `realized.rows`, weights and subtotals are stock-only, KPI totals include options (§6); M3 / M4 → the `OPTION_ADJUST` branch placement and its complete field table (§4.3); M5 → invariant 1 names its three additive columns (§10); M6 → OPT-D12 (owner: 月度成績 includes options); M7 / M8 / M12 → exact close rules, side derivation and the over-cover finding (§4.2); M9 → file ownership (plan); M11 → the share walker skips an opposite-direction adjustment (§4.3); m5–m18 → wording, defaults and the oracle's fee branch |

**Why now.** A real broker history cannot reconcile without its option legs: the premiums are cash
the account really received and paid, so a ledger that leaves them out ends with a cash balance the
broker never had. The owner's acceptance figures live in the git-ignored
`sample-trade-data/charles-schwab/OPTIONS_ledger_proposals.md` (real data — never copied into a
committed file); this spec uses synthetic examples only.

## 1. Scope

**In:** US equity options — single-leg calls and puts with the standard 100-share deliverable — in
the US accounts (`schwab`, and the US market of `moomoo_my`). Open / close in both directions,
expiry, assignment, exercise, **standard** contract adjustments, valuation of an open contract, a
separate 選擇權 section, every import and export door.

**Out (v1):** TW and MY options (R6). **Non-standard deliverables** (OPT-D11): a contract whose OCC
root carries a digit (the exchange's mark of an adjusted deliverable, e.g. `XYZ1`) is refused at
every door, and an adjustment that does not conserve strike × contracts is refused. Multi-leg
grouping, roll chains, collateral reservation, quotes / Greeks for open contracts, option-aware AI
and alerts — deferred to approach ⑤ (§12). Index and non-equity options.

## 2. Owner rulings and confirmed decisions

**Rulings (in chat, 2026-10-09):**

| # | Ruling |
| --- | --- |
| R1 | Approach ④: each contract is an instrument in the existing trade ledger; sell-to-open is the existing declared short sale |
| R2 | An open contract is valued at its **last trade price** from the ledger |
| R3 | The **historical trend excludes** option market value, with an explanatory note |
| R4 | Options are shown in their **own 選擇權 section** |
| R5 | On assignment / exercise the **premium stays option P&L** (not folded into the stock's cost) |
| R6 | **US only**; TW / MY options are not planned |
| R7 | Approach ⑤ (a dedicated sub-ledger) is kept as a planned TODO, started only on demand (§12) |

**Confirmed decisions:**

| # | Decision | Confirmed answer | Why / rejected alternative |
| --- | --- | --- | --- |
| OPT-D1 | Quantity unit | **Share-equivalents** (contracts × 100); price = premium per share | Every `quantity × price` in the package (≥ 28 sites, backlog F-46) stays correct untouched. The premium multiplier is 100 for every listed equity option |
| OPT-D2 | Expiry / assignment / exercise | A **price-0 closing trade** with `close_reason` ∈ {`expired`, `assigned`, `exercised`} | Brokers list them as transaction rows; a same-day buy-to-close sorts by write order. As a corporate action it would run BEFORE that day's trades (actions sort first), and the action framework demands positive integer ratios and a `to_symbol` |
| OPT-D3 | Contract adjustment | New corporate-action kind **`OPTION_ADJUST`**, option → option, carrying long AND short lots by the ratio | `EXCHANGE` refuses a short source in the replay (E5), the validator and the share walker (D33); widening it would change equities. A new kind leaves equities byte-identical (D38) |
| OPT-D4 | Which figures include an open option | **Point-in-time** figures include it at its last trade price: KPI 總市值 / 總報酬 / XIRR, total return A, the FX pool, **月度成績** (OPT-D12). **Trend-derived** figures exclude its value: the trend chart, B (含匯兌總損益), TWR, the benchmark comparison — one shared note | R3 + consistency: XIRR's final value must carry the open liability, or a sold contract's premium counts as fully earned. Consequence, stated in the note: while a contract is open, the trend's net worth carries its premium in cash without its value |
| OPT-D5 | Where options appear | `holdings` rows, sector allocation, weights and subtotals are **stock-only**; KPI totals **include** options, with a 「其中選擇權」 line; the 已實現損益 section lists stock rows and shows one separate option line (not summed into the stock total) linking to the 選擇權 section | R4, without dropping option P&L from the totals |
| OPT-D6 | Fees | Per-account `option_per_contract` commission (config; Schwab 0.65) + SEC on sells (notional basis) + optional `option_reg_per_contract` (default 0). A statement-supplied fee always wins. `moomoo_my`: no default — the fee is required until the owner sets a rate | Real statements show 0.65 + a few cents of regulatory fees; the engine must not guess a rate it was never given |
| OPT-D7 | Registration | An option instrument is **created from its symbol** at the CSV door and the manual door (the terms are encoded in the symbol), with no quote lookup; the underlying need not be registered | A CSV symbol must otherwise be pre-registered, and `quick_register` demands a live quote, which no option has |
| OPT-D8 | Entry units | CSV `shares` = share-equivalents (a multiple of 100; the preview shows 「= N 口」); the manual form sends **口數** (`contracts`) and the server converts | The raw ledger CSV must round-trip (DEF-026); a typed "1" meaning one contract is refused as not a multiple of 100 instead of booking 1/100 of a contract. The multiplier lives in one place (`shared/options.py`), never in JS |
| OPT-D9 | Assignment / exercise rows in the broker converter | **Not mapped blind**: `Assigned` / `Exercise` keep raising `UnmappedRow`; the manual door and CSV support them now | Same rule as the missing wire-out mapping: no rule without a real row behind it |
| OPT-D10 | v1 exclusions | Options are out of: AI insights / signals / alerts / digest / news / fundamentals, the watchlist list (filterable), target weights, rebalance, what-if, the ex-dividend calendar and dividend projection | Each reader treats a held symbol as a stock (§6) |
| OPT-D11 | Non-standard deliverables (owner, after review) | **Standard only**: an `OPTION_ADJUST` is accepted iff both contracts parse with multiplier 100, the destination root has no digit, and `strike_from × ratio_from == strike_to × ratio_to`; otherwise refused at the doors and `UnmappedRow` in the converter | A 3-for-2 split or a reverse split changes what one contract delivers; v1 has no deliverable model. The owner's two real adjustments conserve strike × contracts |
| OPT-D12 | 月度成績 (owner, after review) | **Includes options** — it stores the KPI 總市值 (`api/snapshots.py` from `build_dashboard`), a point-in-time figure; no note | It is not a trend reading |

## 3. Data model

- **`instruments.asset_type`** `TEXT NOT NULL DEFAULT 'stock'` — `'stock'` | `'option'`. Additive
  (`_add_column_if_missing`); every existing row reads `'stock'`.
- **`option_contracts`** (new table, owned by `data_ingestion/store.py`): `symbol TEXT PRIMARY KEY`
  (= `instruments.symbol`), `underlying TEXT NOT NULL`, `expiry TEXT NOT NULL` (ISO),
  `strike TEXT NOT NULL` (canonical Decimal, `money.to_db`), `option_right TEXT NOT NULL` (`C`/`P`),
  `multiplier INTEGER NOT NULL DEFAULT 100` (v1 accepts only 100).
- **Symbol** — the OCC form without root padding (yfinance's form): letters-only root, `YYMMDD`,
  `C`/`P`, strike × 1000 as 8 digits — `^[A-Z]{1,6}\d{6}[CP]\d{8}$`, e.g. `XYZ261218P00050000`. A
  digit in the root is refused (OPT-D11). `[A-Z0-9]` only, so every route carrying a symbol in its
  path keeps working. One parser, `shared/options.py::parse_option_symbol`. Display name:
  「XYZ 2026-12-18 50 Put」. Market `US`, currency `USD`.
- **Registration defaults** for an option instrument: `name` = display name, `sector=""`,
  `board=""`, `is_etf=False`, `etf_flag_unknown=False`, `asset_type='option'`.
- **`transactions.close_reason`** `TEXT NULL` — `expired` | `assigned` | `exercised`; NULL on every
  ordinary trade. Additive.
- **`OPTION_ADJUST`** joins `CorporateActionKind` (`shared/corporate_actions.py`) with zh
  「選擇權合約調整」. No schema change (`kind` has no CHECK).
- **Models:** `Instrument.asset_type`, `Instrument.option: OptionTerms | None`,
  `Instrument.is_option`; `Transaction.close_reason`; `_Position.unbookable_close` and
  `OpenedPosition.unbookable_close` (sticky, §4.4); `Book.refused_closes: list[RefusedClose]`
  (row id, account, symbol, date, reason); `DashboardData.options: OptionsBlock | None` (§7).

## 4. Ledger mechanics

A contract is a position like any other, in share-equivalents. Internally the replay keeps a long lot
and a separate positive short lot (`short_shares`, `short_proceeds`); the signed `shares` is derived
on output. Nothing below changes how a stock is booked.

### 4.1 Event mapping

| Broker event | Ledger row | Replay effect | Realized row |
| --- | --- | --- | --- |
| Sell to Open | SELL, `short_sale=1` | sells any long lot first; the rest opens / extends the short lot at net proceeds per share | none for the short part |
| Buy to Close | BUY | covers the short lot first (existing rule); any excess opens a long lot (§4.2 finding) | `short_cover` = (short avg − cover cost per share) × covered |
| Buy to Open | BUY | opens / adds to the long lot | — |
| Sell to Close | SELL | sells the long lot | `sale` |
| Expired, short | BUY, price 0, fee 0, tax 0, `close_reason=expired` | covers the whole short lot at 0 | `short_cover` = the whole remaining premium |
| Expired, long | SELL, price 0, fee 0, tax 0, `close_reason=expired` | sells the whole long lot at 0 | `sale` = −remaining basis |
| Assigned (sold put / call) | option BUY at 0 `close_reason=assigned` + an ordinary stock BUY (put) / SELL (call) of the underlying at the strike, shares = contracts × 100 | option P&L = premium (R5); the stock leg is a normal trade | option `short_cover`; the stock per its own branch |
| Exercised (bought call / put) | option SELL at 0 `close_reason=exercised` + a stock BUY (call) / SELL (put) at the strike | the premium paid is the option's realized loss (R5) | option `sale`; the stock per its own branch |
| Standard adjustment | `OPTION_ADJUST` old → new, `ratio_to:ratio_from` | §4.3 | none |

### 4.2 Close rules (validators — every door: CSV, manual, edit)

- `close_reason` requires an option instrument, **price exactly 0, fee 0, tax 0**, `short_sale=0`.
  It is refused on a stock.
- **Side is derived, not chosen.** Let *open* = the signed position of that account and contract
  through the row's date, counted by `holdings.shares_through(account, contract, on=date,
  pending=siblings booked before ∪ replacement)` — the row itself never counts. `expired`: BUY if
  *open* < 0, SELL if *open* > 0, refused if 0 (`option_close_no_position`). `assigned`: *open*
  must be < 0 (a sold contract), side BUY. `exercised`: *open* must be > 0, side SELL. The manual
  door derives the side server-side; a CSV or edit row whose side contradicts it is refused.
- **Quantity.** `expired` = `|open|` exactly; `assigned` / `exercised`: 0 < quantity ≤ `|open|`, a
  multiple of 100.
- **Date.** `expired` = the contract's expiry exactly; `assigned` / `exercised` ≤ expiry. Any row on
  a contract dated after its expiry is refused. A same-day row of the same contract ordered after an
  `expired` row (higher id, or below it in a file) is refused.
- **Every option row:** quantity a multiple of 100 (「選擇權數量須為 100 的倍數（1 口 = 100 股當量）」); a
  sell that would open a short without `short_sale=1` is the ordinary 賣超 (never inferred); a BUY
  that covers more than the open short raises the soft finding 「買進平倉口數超過空頭部位，多出的 N 口將成為
  買方部位」 (`needs_confirm`); a 「買進平倉」 with no short, or a 「賣出平倉」 with no long, raises the soft
  finding that the row opens a position instead.
- **Elsewhere:** a dividend on an option is refused (`dividend_on_option`); an opening-inventory row
  on an option is refused 「選擇權請以交易登錄」; `SPLIT` / `EXCHANGE` / `SPINOFF` naming an option are
  refused; a `SPLIT` entered on an underlying with open options raises the soft finding
  「此標的有未平倉選擇權，合約調整請另行登錄（選擇權合約調整）」 — a split never adjusts a contract by itself.
- **Assignment / exercise are two rows** (option close + stock leg). The manual door's
  「被指派／履約」 writes both in one database transaction: both rows or neither; the stock leg runs the
  ordinary rules (fees, cash, 賣超, preview); one acknowledgement covers the pair and the response
  names the leg each finding belongs to.

### 4.3 `OPTION_ADJUST`

- **Validation:** both symbols are option contracts (registrable from their symbols at the same
  door), OPT-D11 holds, positive integer ratios, from ≠ to, the source holds a long or a short
  position on the action date, the destination is flat or in the **same** direction.
- **Replay branch** in `_apply_action`: inserted **immediately after the SPLIT branch and before the
  E5 block** (E5 would refuse every short source). It owns its destination checks: E22 (destination
  ever oversold) as today; opposite direction → `_reject` (待釐清). Priority CORPORATE_ACTION (start of
  its day). Field transfer, P = source, Q = destination, every `_Position` field:

| Field | `OPTION_ADJUST` P → Q |
| --- | --- |
| `quote_ccy` | Q keeps its own; both USD (registration guarantees it) |
| `shares` | `Q += P.shares × to / from`; `P := 0` |
| `original_total`, `adjusted_total` | `Q += P.*`; `P := 0` |
| `short_shares` | `Q += P.short_shares × to / from`; `P := 0` |
| `short_proceeds` | `Q += P.short_proceeds`; `P := 0` (the residue a full cover can leave included) |
| `ever_oversold` | source refused by E3, destination by E22; nothing transferred |
| `oversold_on`, `oversold_sold`, `oversold_held` | unreachable; never carried |
| `unbookable_dividend`, `unbookable_action`, `revived_by_dividend`, `unbookable_close` | OR-ed into Q |
| `vacated_to` | `P.vacated_to := to_symbol`; Q `_reopen` |
| `payback_from`, `payback_carried`, `payback_own` | as EXCHANGE (moved to Q; P cleared) — always empty on an option |

  `unbookable_close` is a new `_Position` field: the field-count test and the corporate-actions spec
  §4.4 table gain its row (SPLIT unchanged; EXCHANGE / SPINOFF unreachable — they refuse options).
- **Share walker** (`data_ingestion/holdings.py::_delta_of`, `created_by`; `opened_by`): moves the
  signed count by the ratio, shorts included; when source and destination hold opposite signs it
  skips the action and records both symbols through the existing `note_negative_side_skip`.

### 4.4 A close the replay cannot book

Validators refuse every close that would open or extend a lot, but an edit, delete or batch undo of
an earlier row can still leave one behind. The replay therefore refuses a `close_reason` row whose
side and quantity do not exactly close existing exposure (BUY beyond the short, SELL beyond the
long, or against the opposite direction): it books nothing for that row, records it in
`Book.refused_closes`, and sets the sticky `unbookable_close` on the position (strict path: raises
`UnbookableLedgerError`). Every reader of `unbookable_action` reads `unbookable_close` the same way
(the trend marks days incomplete from that date, XIRR and B are withheld with a reason naming the
row, the drawer shows 待釐清 with the remedy 「刪除該筆到期／指派／履約列，或補回被刪除的交易」, the manual
preview withholds its position math). The correction doors' replay guard treats a newly refused
close like a stranded dividend: hard refusal `refused_close_correction`, naming the row.

## 5. Valuation (R2, OPT-D4)

- **Price of an open contract** = the price of its last ordinary trade (`close_reason` rows
  excluded), **per symbol across accounts**, with `trade_date ≤` the valuation day; dated that
  trade's date. With no trade since an `OPTION_ADJUST`, the predecessor's last price ×
  `ratio_from / ratio_to`. Read at the valuation seam (`portfolio/dashboard.py` `price_reads` /
  `price_map`) from `portfolio/options.py`; **never written to `prices`**.
- Shown as 「最後成交價 YYYY-MM-DD」; never counted in `missing_prices` / `any_stale`.
- **Point-in-time figures include it** (signed market value; unrealized = (price − avg) × signed
  shares): KPI totals, total return A, XIRR's final value, the FX pool's `foreign_stock_value`,
  月度成績.
- **Trend-derived figures exclude its value:** `portfolio/timeseries.py` skips option holdings in the
  daily value **without** marking the day incomplete; the trend chart, B, TWR and the benchmark
  comparison show one note when `options.trend_excludes_value` is true:
  「走勢不含未平倉選擇權的市值；權利金已計入現金，未平倉期間淨值高估（賣方）或低估（買方）該部位市值」. Option cash
  flows stay in `net_invested` and the cash series — they are real cash.

## 6. Every surface

| Surface | v1 behaviour |
| --- | --- |
| Quote jobs | `shared/instrument_scope.py::TRACKED_SQL` filters `asset_type='stock'` — one seam for quotes, history, dividends, the inbox scan, FinMind, consensus, fundamentals. The five bypass paths each refuse an option: `quick_register`'s quote gate + history fetch, the gap backfill, `refresh_instrument_quote`, the Alpha Vantage held universe, held-only news. An option is never in a worklist, so it can never make a run 部分 |
| Dashboard | `holdings`, allocation, weights (`weight = stock MV / Σ stock MV`), subtotals, `realized.rows` and the realized block's totals: **stocks only**. KPI 總市值 / 總報酬 / unrealized / XIRR: stocks + options, with 「其中選擇權」 read from `options.market_value`. New 選擇權 section (§7) |
| Drawer (`/symbol/{symbol}/detail`) | works for a contract: contract header (underlying, expiry, strike, right, multiplier), trades in 口 and share-equivalents, no 配息史, 試算 replaced by 「選擇權不提供試算」; it reads the contract from `options.open` |
| Watchlist | contracts hidden by default, a 「顯示選擇權合約」 filter; archive / purge read `holds_position` as today |
| Target weights, rebalance, what-if, ex-dividend calendar, dividend projection | exclude options |
| Signals, alerts, alert inputs, insights, LLM variables, prompts, digest, news, fundamentals | exclude options (OPT-D10) |
| Cash page / 現金收支明細 | option trades settle like any trade; `CashLine` carries `short_sale` and `close_reason`, so a line reads 「賣出開倉 1 口 XYZ 2026-12-18 50 Put」, 「到期作廢」, 「被指派」, 「履約」 |
| Ledger tabs | 交易: contract name, 口數 beside share-equivalents, chips 「賣出開倉」/「買進平倉」/「買進開倉」/「賣出平倉」/「到期」/「被指派」/「履約」; 公司行動: `OPTION_ADJUST` |

## 7. The 選擇權 section and its wire block (R4)

`DashboardData.options: OptionsBlock | None`, `None` unless the **uncut** ledger has at least one
transaction or action on an `asset_type='option'` instrument. The dashboard router removes the key
when it is `None`, so a ledger without options produces exactly today's payload. **Every new wire
field lives inside this block** — no existing model (`TrendPoint`, `KpiSummary`, `HoldingRow`,
`RealizedPnL`, the benchmark comparison) gains a field. Contents (Decimal strings):

- `open` — per account × contract: 標的, 到期日 + 剩餘天數, 履約價, 買權/賣權, 方向 (買方/賣方), 口數,
  share-equivalents, 平均權利金, 最後成交價 + 日期, 市值, 未實現損益, 履約義務 (sold put: strike × 100 ×
  contracts — the cash an assignment would take).
- `realized` — rows per closed contract (open date, close date, how it closed, premium in / out,
  realized) and totals by year and by underlying; `realized_total`.
- `market_value`, `unrealized` — per currency and in the reporting currency (the KPI 「其中選擇權」).
- `trend_excludes_value` (true when an option position existed in the trend window) and `note`.
- The section renders after 持倉明細; the R3 note sits under it.

## 8. Import doors

- **Transaction CSV** (`csv_import.py`): `close_reason` joins `TRANSACTION_COLUMNS` (optional,
  `(選填)`); an unregistered OCC symbol is registered from its terms in the commit's transaction (the
  preview says 「將建立選擇權合約 …」); `shares` in share-equivalents; fees per OPT-D6; §4.2 rules.
  Template: an example sell-to-open and an expiry.
- **Corporate-action CSV:** kind `OPTION_ADJUST` (alias 「選擇權合約調整」), OPT-D11.
- **Opening-inventory CSV:** an option symbol is refused (§4.2).
- **Manual entry** (`input_center.py`): an option symbol (or the contract picker) switches to option
  mode — `contracts` (口數), the four sides mapped to side + `short_sale`, 「到期作廢」 and
  「被指派／履約」; the preview mirrors the replay's branch.
- **Schwab converter** (`data_ingestion/broker/`; pure — it emits the OCC symbol, the CSV door
  registers it): option rows stop being dropped. `option_symbol` "XYZ 12/18/2026 50.00 P" → OCC;
  Sell to Open → SELL `short_sale=1`, Buy to Close / Buy to Open → BUY, Sell to Close → SELL (fees
  verbatim, tax 0); `Expired` and TD's 「REMOVAL OF OPTION DUE TO EXPIRATION」 (re-mapped from
  `OPT_ADJUST` to expiry) → price-0 `close_reason=expired`, dated the contract's expiry, side from the
  running signed position in the file (no opening in the file → `UnmappedRow`), same-day rows of one
  contract merged, written after that day's trades; 「OPTION ODD SPLIT REORGANIZATION」 / 「OPTION
  POSITION CHANGE」 same-day journal pairs → one `OPTION_ADJUST` (ratio = |to qty| : |from qty|) when
  OPT-D11 holds, else `UnmappedRow` 「非標準交割，第一版不支援」 (an unpaired half also raises);
  「TRANSFER OF SECURITY OR OPTION OUT/IN」 of an option between the broker's own platforms → dropped
  with a stated reason; `Assigned` / `Exercise` stay `UnmappedRow` (OPT-D9).
- **AI input door:** `MARKET_CODE_PATTERNS["US"]` is unchanged; `matches_market_format` and
  `looks_like_market_code` also accept the option pattern, so an OCC symbol draws no
  `symbol_format_mismatch`.

## 9. Export doors

| Export | Change |
| --- | --- |
| Raw ledgers zip / per-tab ledger CSV | `transactions.close_reason` rides the `SELECT *` dump (re-pinned, §10). The zip carries ledger tables only (`instruments` is not in it), so neither `asset_type` nor `option_contracts` is exported: on re-import an option's terms re-derive from its symbol (OPT-D7) |
| 持股 CSV / 持股報表 (print) | stock rows as today; a separate 選擇權 block (§7 columns) and the 「其中選擇權」 KPI line — only when `options` is present |
| 已實現損益 CSV | stock rows from `realized.rows` plus option rows from `options.realized`; columns `asset_type` and `contract` appended (re-pinned, §10) |
| 稅務包 | option closes are `sale` / `short_cover` and already land in capital gains (`export/tax.py` kind filter); add the contract name and a 「其中選擇權」 subtotal |
| 帳本報告 (print) | 口數 + the option chips; `OPTION_ADJUST` in the actions section |
| 現金收支明細 CSV / print | the option descriptions of §6 |
| 個股明細 CSV | works for a contract |
| 再平衡報告 | excludes options |

## 10. Verification

- **Invariant 1 (blast radius, D38).** A ledger with no option is byte-identical in every API payload
  and export **except three additive column changes**, each re-pinned in its own test in the same
  commit: (1) the raw transactions dump / per-tab CSV gains `close_reason` (empty); (2) the
  transaction import template gains `close_reason（選填）`; (3) the realized CSV gains `asset_type`
  and `contract`. The golden payload does not change.
- **Hermetic tests** per mechanism (§4.1 rows both directions, §4.2 rules incl. side derivation,
  same-day ordering and over-cover, §4.3 transfer table on long and short, the walker's
  opposite-direction skip, §4.4 refusal incl. delete-the-STO-then-expiry), per surface (§6 / §9 — a
  scan of stock readers so a new reader cannot forget the exclusion), and e2e for the 選擇權 section and
  option-mode entry.
- **Stress-audit oracle** (`scripts/stress_audit/oracle.py`) transcribes from this spec, never
  importing the app: price-0 closes, §4.4 refusal, `OPTION_ADJUST`, the option fee branch, last-trade
  valuation, the trend exclusion; terms parsed from the symbol. Phase-1 scenario ops cover every
  §4.1 row and the cash tie per pool.
- **Accounting manual** (zh + en): an options section with each formula and its verification anchor.
- **Real-data acceptance** (local, git-ignored): the owner's Schwab option rows through the converter
  and the CSV door — option realized, the open contract and the option cash total equal the figures
  in `OPTIONS_ledger_proposals.md`. The account's full-history cash target is met once the whole
  history is imported (trades, dividends, openings — the next work item after this branch).

## 11. Build order

`W1` foundation → `W2` replay + valuation + trend + payload → `W3` doors → `W4` exclusions → `W5`
web → `W6` exports → `W7` oracle, manual, acceptance. Waves, ownership and gates: the plan.

## 12. Deferred — approach ⑤ (dedicated options sub-ledger), TODO plan

Started only when the owner asks. `shared/options.py` carries a `TODO(options-⑤)` block pointing
here.

- **Triggers:** multi-leg strategies, frequent rolls the owner wants analysed as chains, assignment
  volume that makes the two-row entry a burden, a need for live option quotes, or a non-standard
  deliverable in the owner's history.
- **Roll chains:** link a buy-to-close and a same-day sell-to-open on one underlying, with the
  chain's cumulative premium.
- **Collateral:** reserve a sold put's strike × 100 against the cash pool (「可用現金」 vs 「帳戶現金」).
- **Quotes and Greeks:** a provider path for open contracts; mark-to-market history so the trend
  can include option value (would retire R3's exclusion).
- **Non-standard deliverables:** a per-contract deliverable model (retires OPT-D11's refusal).
- **Assignment linkage:** an optional per-assignment choice to fold the premium into the stock's cost
  (the US tax-basis convention), which R5 declines for v1.
- **Option-aware AI and alerts:** expiry reminders, assignment-risk alerts, option context in insight
  prompts.
- **Strategy views:** P&L by strategy, annualised return on collateral.
