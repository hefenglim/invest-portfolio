# Spec — US equity options in the ledger (P3, approach ④)

**Status:** APPROVED 2026-10-09 — rulings R1–R7 and decisions OPT-D1..D19 confirmed by the owner in
chat (OPT-D1..D10 「10 項全部照建議」; OPT-D11 / D12 after review round 1; OPT-D13..D19 after review
round 4). Implementation follows `docs/superpowers/plans/2026-10-09-options-ledger.md` on branch
`feat/options-ledger` (off `main` after `feat/corporate-actions` was merged, `aa5c80a`) and starts
only after the owner approves the plan.
**Builds on:** the corporate-action framework, the replay and the declared short sale (`main`).
**Supersedes:** the P3 section of `docs/spec/2026-08-06-broker-import-backlog.md` as the plan of
record for options. Its multiplier census and its "direction is not a gap" finding still hold.

**Revision log**

| Date | Change |
| --- | --- |
| 2026-10-09 | First draft; owner rulings R1–R7; OPT-D1..D10 confirmed as recommended |
| 2026-10-09 | **Review round 1** (Fable 5.1, max; NOT READY → folded): B1 → OPT-D11; B2 → the replay refuses a close that would open a lot (§4.4); M1 → new wire fields only in the `options` block; M2 / M10 → stock-only lists and weights; M3 / M4 → `OPTION_ADJUST` placement and field table; M5 → named additive columns; M6 → OPT-D12; M7 / M8 / M12 → close rules; M9 → ownership; M11 → walker skip; m5–m18 wording |
| 2026-10-09 | **Review round 2** (READY-WITH-FIXES → folded): N1 additive wire keys; N2 `Holding.unbookable_close`; N3 drawer reconciliation; N4 `options.realized.rows`; N5 option-mode refusals; N6 same right / expiry; N7 the assignment's stock leg; N8 / N11 wording; N10 freshness |
| 2026-10-09 | **Review round 3** (READY): N14–N16 wording |
| 2026-10-09 | **Review round 4** — reviewer A (fresh consistency, READY-WITH-FIXES, F1–F15), reviewer B (usability of every entry point, ADEQUATE, U1–U14 + D1–D5), reviewer C (integration: 26 confirmed / 3 partly / 0 incorrect, M1–M21, READY-AFTER-FOLD). Owner answered seven questions: OPT-D13 expiry inbox in v1; OPT-D14 option-mode 買進平倉 beyond the short refused; OPT-D15 server-derived `option_side`; OPT-D16 `ratio_from == 1`; OPT-D17 underlying price beside the strike; OPT-D18 default side by position; OPT-D19 the converter knows the ledger's open contracts. Folded everywhere below; a vocabulary table (§6.1) now fixes every zh term |
| 2026-10-09 | **Round 4 fold verification** (reviewer C; READY-WITH-FIXES, no owner decision): N1 the `option_side` walk includes `OPTION_ADJUST` (§6.1); N2 the converter's whole-number ratio and the form's 1 → N input (§4.3, §6.2, §8); N3 two more additive keys + the broker report adds none (§8, §10); N4 the fee settings panel (§8); N5 the what's-new wording (§8); N6 the underlying-price read (§7.1); N7 three branches — the inbox on a refused close (§7.2), the two-leg response (§4.2), the edit door's over-cover gate (§4.2); N8 `known_positions` as of the day before the request (§8) |

**Why now.** A real broker history cannot reconcile without its option legs: the premiums are cash
the account really received and paid. The owner's acceptance figures live in the git-ignored
`sample-trade-data/charles-schwab/OPTIONS_ledger_proposals.md` (real data — never copied into a
committed file); this spec uses synthetic examples only.

## 1. Scope

**In:** US equity options — single-leg calls and puts with the standard 100-share deliverable — in
the US accounts (`schwab`, and the US market of `moomoo_my`). Open / close in both directions,
expiry, assignment, exercise, **standard** contract adjustments, valuation of an open contract, a
separate 選擇權 section, an **expiry inbox** (§7.2), every import and export door.

**Out (v1):** TW and MY options (R6). Non-standard deliverables (OPT-D11 / D16). Multi-leg grouping,
roll chains, collateral reservation, quotes / Greeks for open contracts, option-aware AI insights and
**alerts** (pre-expiry reminders, assignment risk) — deferred to approach ⑤ (§12). Index and
non-equity options.

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
| OPT-D1 | Quantity unit | **Share-equivalents** (contracts × 100); price = premium per share | Every `quantity × price` (≥ 28 sites, backlog F-46) stays correct untouched; the premium multiplier is 100 for every listed equity option |
| OPT-D2 | Expiry / assignment / exercise | A **price-0 closing trade** with `close_reason` ∈ {`expired`, `assigned`, `exercised`} | Brokers list them as transaction rows; a same-day buy-to-close sorts by write order. As an action it would run BEFORE that day's trades, and actions demand positive ratios and a `to_symbol` |
| OPT-D3 | Contract adjustment | New corporate-action kind **`OPTION_ADJUST`**, option → option, carrying long AND short lots | `EXCHANGE` refuses a short source (E5, D33); a new kind leaves equities byte-identical (D38) |
| OPT-D4 | Which figures include an open option | **Point-in-time** figures include it at its last trade price (KPI 總市值 / 總報酬 / XIRR, total return A, the FX pool, 月度成績). **Trend-derived** figures exclude its value (trend chart, B, TWR, benchmark) with one note | XIRR's final value must carry the open liability. Consequence, in the note: while a contract is open the trend's net worth carries its premium in cash without its value |
| OPT-D5 | Where options appear | `holdings` rows, allocation, weights, subtotals: **stock-only**; KPI totals **include** options with a 「其中選擇權」 line; the 已實現損益 section shows a separate option line (not summed) linking to the 選擇權 section | R4 without dropping option P&L from the totals |
| OPT-D6 | Fees | Per-account `option_per_contract` (Schwab 0.65) + SEC on sells (notional basis) + optional `option_reg_per_contract` (default 0); a statement-supplied fee wins; `moomoo_my` has no default rate (fee required until set) | The engine must not guess a rate it was never given |
| OPT-D7 | Registration | An option instrument is **created from its symbol** at the CSV door and the manual door, with no quote lookup; the underlying need not be registered | A quote gate cannot pass for an option |
| OPT-D8 | Entry units | CSV `shares` = share-equivalents (multiple of 100); the manual form sends **口數** (`contracts`); the server converts | The raw ledger CSV must round-trip (DEF-026); "1" for one contract is refused instead of booking 1/100; the multiplier lives only in `shared/options.py` |
| OPT-D9 | Assignment / exercise rows in the broker converter | **Not mapped blind** — `Assigned` / `Exercise` raise `UnmappedRow` with a reason (§8); manual door and CSV support them | No rule without a real row behind it |
| OPT-D10 | v1 exclusions | Options are out of AI insights / signals / alerts / digest / news / fundamentals, the watchlist list (filterable), target weights, rebalance, what-if, the ex-dividend calendar and projection | Each reader treats a held symbol as a stock (§6) |
| OPT-D11 | Non-standard deliverables (owner, round 1) | **Standard only**: an `OPTION_ADJUST` is accepted iff both contracts parse with multiplier 100, the destination root has no digit, the right and the expiry are unchanged, **`ratio_from == 1`** (OPT-D16), and `strike_from × ratio_from == strike_to × ratio_to`; otherwise refused at the doors and `UnmappedRow` in the converter | v1 has no deliverable model; the owner's two real adjustments are a 1→3 split and a 1:1 rename |
| OPT-D12 | 月度成績 (owner, round 1) | **Includes options** — it stores the KPI 總市值 (`api/snapshots.py`); no note | A point-in-time figure, not a trend reading |
| OPT-D13 | Expiry prompting (owner, round 4) | **An expiry inbox in v1** — a third 收件匣 panel 「待確認到期」 (§7.2) | The most frequent option chore (several contracts a month); a ledger-completeness prompt drawn from the ledger, not an alert (alerts stay §12) |
| OPT-D14 | Option-mode 買進平倉 beyond the short (owner, round 4) | **Refused** 「買進平倉口數超過空頭部位（N 口），請分成平倉與開倉兩筆」 | Symmetric with 賣出平倉; the manual door has one ack (`ack_oversell`) and no other confirmation exists there |
| OPT-D15 | Side labels (owner, round 4) | **Server-derived `option_side`** per row (§6.1), one classifier, never in JS | 買進平倉 and 買進開倉 are both a bare BUY in storage |
| OPT-D16 | Adjustment ratios (owner, round 4) | **`ratio_from == 1`** only (1 → N contracts, or 1:1) | A reverse or 3-for-2 split is non-standard; `ratio_from > 1` would leave a fractional contract |
| OPT-D17 | Underlying price beside the strike (owner, round 4) | **Shown** in the open row when the underlying is registered and priced, with its date; no 價內／價外 wording | The put seller's first question in expiry week |
| OPT-D18 | Default side in option mode (owner, round 4) | **By position**: a held short → 買進平倉, a held long → 賣出平倉, nothing held → 賣出開倉; 口數 defaults to the whole position on a closing side | The owner's dominant actions; the preview names the side in large type |
| OPT-D19 | Converter across exports (owner, round 4) | **The converter knows the ledger's open contracts**: `api/routers/broker_import.py` injects `known_positions` (required argument) so an expiry / close whose opening is in an earlier export resolves its side | The owner may import statements separately; the converter stays pure (injection, not an import) |

## 3. Data model

- **`instruments.asset_type`** `TEXT NOT NULL DEFAULT 'stock'` — `'stock'` | `'option'`. Additive.
  `Instrument.is_option` **is** `asset_type == 'option'`; every registering door sets `asset_type`
  from `parse_option_symbol`, so symbol and type never disagree.
- **`option_contracts`** (owned by `data_ingestion/store.py`): `symbol TEXT PRIMARY KEY`,
  `underlying TEXT NOT NULL`, `expiry TEXT NOT NULL` (ISO), `strike TEXT NOT NULL` (canonical
  Decimal), `option_right TEXT NOT NULL` (`C`/`P`), `multiplier INTEGER NOT NULL DEFAULT 100`.
- **Symbol** — OCC without root padding: letters-only root, `YYMMDD`, `C`/`P`, strike × 1000 as 8
  digits — `^[A-Z]{1,6}\d{6}[CP]\d{8}$`, e.g. `XYZ261218P00050000`. A digit root is refused
  (OPT-D11). One parser and one composer in `shared/options.py`; **the browser never composes a
  symbol** — it calls the compose endpoint (§8). Display name 「XYZ 2026-12-18 50 Put」. Market `US`,
  currency `USD`. Registration defaults: `name` = display name, `sector=""`, `board=""`,
  `is_etf=False`, `etf_flag_unknown=False`.
- **`transactions.close_reason`** `TEXT NULL` — `expired` | `assigned` | `exercised`. Additive.
- **`OPTION_ADJUST`** joins `CorporateActionKind` with zh 「選擇權合約調整」.
- **`option_expiry_skips`** (owned by `api/option_expiry.py`, created at boot): `account_id`,
  `symbol`, `skipped_at`; primary key `(account_id, symbol)`.
- **Models:** `Instrument.asset_type` / `option` / `is_option`; `Transaction.close_reason`;
  `TxnInput.close_reason` and `TxnInput.contracts` (validator input); `_Position.unbookable_close`,
  `OpenedPosition.unbookable_close`, `Holding.unbookable_close` (sticky, §4.4 — `HoldingRow` has no
  extra-field config, so the flag never reaches the holdings wire); `Book.refused_closes:
  list[RefusedClose]`; `Book.realized_close_reasons: dict[int, str]` (index into `Book.realized.rows`
  in emission order → `close_reason`); `OptionSide` (§6.1); `DashboardData.options: OptionsBlock |
  None` (§7).

## 4. Ledger mechanics

A contract is a position in share-equivalents. The replay keeps a long lot and a separate positive
short lot; the signed `shares` is derived on output. Nothing below changes how a stock is booked.

### 4.1 Event mapping

| Broker event | Ledger row | Replay effect | Realized row |
| --- | --- | --- | --- |
| Sell to Open | SELL, `short_sale=1` | sells any long lot first; the rest opens / extends the short lot at net proceeds per share | none for the short part |
| Buy to Close | BUY | covers the short lot first (existing rule) | `short_cover` = (short avg − cover cost per share) × covered |
| Buy to Open | BUY | opens / adds to the long lot | — |
| Sell to Close | SELL | sells the long lot | `sale` |
| Expired, short | BUY, price 0, fee 0, tax 0, `close_reason=expired` | covers the whole short lot at 0 | `short_cover` = whole remaining premium |
| Expired, long | SELL, price 0, fee 0, tax 0, `close_reason=expired` | sells the whole long lot at 0 | `sale` = −remaining basis |
| Assigned (sold put / call) | option BUY at 0 `close_reason=assigned` + a stock BUY (put) / SELL (call) of the underlying at the strike, shares = contracts × 100 | option P&L = premium (R5); the stock leg is a normal trade | option `short_cover`; stock per its branch |
| Exercised (bought call / put) | option SELL at 0 `close_reason=exercised` + a stock BUY (call) / SELL (put) at the strike | the premium paid is the option's realized loss (R5) | option `sale`; stock per its branch |
| Standard adjustment | `OPTION_ADJUST` old → new, `ratio_to : 1` | §4.3 | none |

### 4.2 Close rules and door rules

- **Structural:** `transaction_structural_issues` accepts `price == 0` **only when** `close_reason`
  is set; every other price-0 row keeps 「價格必須大於 0」. `close_reason` requires an option
  instrument and `short_sale=0`; it is refused on a stock.
- **No fee on a close row:** every door sets fee = tax = 0 on a `close_reason` row **without calling
  the fee engine** (snapshot `{"source": "close_reason"}`); a supplied non-zero fee or tax is refused
  「到期作廢／被指派／履約列不收手續費與稅」. The engine's option branch also returns 0 / 0 when the
  notional is 0, mirroring the stock branch.
- **Side is derived.** *open* = the signed position of that account and contract through the row's
  date by `holdings.shares_through(account, contract, on=date, pending=siblings booked before ∪
  replacement)`; the row itself never counts. `expired`: BUY if *open* < 0, SELL if *open* > 0,
  refused if 0 (`option_close_no_position`). `assigned`: *open* < 0, side BUY. `exercised`: *open* >
  0, side SELL. A CSV or edit row whose side contradicts it is refused.
- **Quantity:** `expired` = `|open|` exactly; `assigned` / `exercised`: 0 < quantity ≤ `|open|`,
  multiple of 100. **Date:** `expired` = the contract's expiry; `assigned` / `exercised` ≤ expiry; any
  row on a contract dated after its expiry is refused; a same-day row of the same contract ordered
  after an `expired` row is refused.
- **Every option row:** quantity a multiple of 100 (「選擇權數量須為 100 的倍數（1 口 = 100 股當量）；若填的是口數，
  請改為 口數×100」); a sell that would open a short without `short_sale=1` is the ordinary 賣超.
- **The manual door has exactly one acknowledgement, `ack_oversell`;** `cash_overdraft` is a preview
  warning. In option mode the intent is known: 「買進平倉」 with no short → refused
  「無空頭部位可平倉，請改選買進開倉」; beyond the short → refused (OPT-D14); 「賣出平倉」 with no long or
  beyond the long → refused 「無多頭部位可平倉，請改選賣出開倉」. A CSV or edit row carries no intent and
  keeps the stock rules; there a BUY covering more than the open short raises the soft finding
  「買進平倉口數超過空頭部位，多出的 N 口將成為買方部位」 — under the file-level `ack_warnings` gate on the CSV
  door, and as a preview warning on the edit door (which has no file gate, as `cash_overdraft` on the
  manual door).
- **Elsewhere:** a dividend on an option is refused (`dividend_on_option`); an opening-inventory row
  on an option is refused 「選擇權請以交易登錄」; `SPLIT` / `EXCHANGE` / `SPINOFF` naming an option are
  refused; a `SPLIT` on an underlying with open options raises the soft finding
  「此標的有未平倉選擇權，合約調整請另行登錄（選擇權合約調整）」.
- **Assignment / exercise — one action, two rows.** 「被指派／履約」 writes the option close and the
  stock leg in one database transaction (both or neither). The preview returns `legs: [option,
  stock]`, fully derived: stock symbol = underlying, side from right × direction, shares = contracts
  × 100, price = strike (editable), fee by the engine; the card shows 選擇權腿／股票腿 and the sentence
  「被指派：以 50.00 買入 200 股 XYZ，現金 −10,000.00 USD」 (server strings). The pair's one
  acknowledgement is `ack_oversell` when a call assignment's stock SELL exceeds holdings. The stock
  leg's underlying registers through the existing auto-register path (its quote gate applies); on
  failure the pair is refused 「請先登錄標的 XYZ」 with the inline 立即註冊 link. The commit answers
  `txn_ids: [option, stock]` and per-leg totals (an option-only response shape).

### 4.3 `OPTION_ADJUST`

- **Validation:** both symbols are option contracts (registrable from their symbols at the same
  door); OPT-D11 holds (incl. `ratio_from == 1` — a CSV or API row with `ratio_from ≠ 1` is refused
  「選擇權合約調整以 1 口為基準，請填 1 → N」); positive integer `ratio_to`; from ≠ to; the source
  holds a **non-zero signed** position at the `(date, CORPORATE_ACTION)` cut
  (`shares_before_action_on`, E13's predicate — a short counts); the destination is flat or in the
  same direction. The corporate-action preview returns `strike_products` (two Decimal strings) and
  `conserves`, so the form prints 「履約價×口數：60×1 ＝ 20×3 ✓」 without arithmetic in the browser.
- **Replay branch** in `_apply_action`, **immediately after the SPLIT branch and before the E5
  block**; E22 as today; opposite direction → `_reject`. Field transfer, P = source, Q = destination:

| Field | `OPTION_ADJUST` P → Q |
| --- | --- |
| `quote_ccy` | Q keeps its own; both USD |
| `shares` | `Q += P.shares × ratio_to`; `P := 0` |
| `original_total`, `adjusted_total` | `Q += P.*`; `P := 0` |
| `short_shares` | `Q += P.short_shares × ratio_to`; `P := 0` |
| `short_proceeds` | `Q += P.short_proceeds`; `P := 0` (residue included) |
| `ever_oversold` | source refused by E3, destination by E22 |
| `oversold_on`, `oversold_sold`, `oversold_held` | unreachable; never carried |
| `unbookable_dividend`, `unbookable_action`, `revived_by_dividend`, `unbookable_close` | OR-ed into Q |
| `vacated_to` | `P.vacated_to := to_symbol`; Q `_reopen` |
| `payback_from`, `payback_carried`, `payback_own` | as EXCHANGE — always empty on an option |

  `unbookable_close` is a new `_Position` field: the field-count test and the corporate-actions spec
  §4.4 table gain its row — SPLIT unchanged; EXCHANGE / SPINOFF OR it into Q like `unbookable_action`
  (only a hand-edited row reaches them; the replay must stay total).
- **Share walker** (`_delta_of`, `created_by`, `opened_by`): moves the signed count × `ratio_to`,
  shorts included; opposite signs → skip + `note_negative_side_skip` on both symbols.

### 4.4 A close the replay cannot book

The replay refuses a `close_reason` row that **would open or extend a lot** — a BUY beyond the short
lot, a SELL beyond the long lot, or a close against the opposite direction (a partial `assigned` /
`exercised` within the lot books normally). It books nothing for that row and always records it in
`Book.refused_closes`; when the position exists it also sets the sticky `unbookable_close` (strict
path: raises `UnbookableLedgerError`). **XIRR and B are withheld whenever `Book.refused_closes` is
non-empty** — the record, like `orphan_dividends`, independent of the flag — with a reason naming
the row. Every reader of `unbookable_action` — the per-holding ones included (`portfolio/pnl.py`,
`position_aggregate.py`, `dividends.py`, `timeseries.py`, the manual preview, the drawer, the badges)
— reads `unbookable_close` the same way. Every surface that lists the row marks it
「未入帳（待釐清）」 with the remedy 「刪除該筆到期作廢／被指派／履約列，或補回被刪除的交易」: the 交易 tab, the
drawer's trade list, the 選擇權 section, a dashboard banner (like the unapplied-action banner) with
「前往該筆交易」. The correction doors' replay guard refuses a correction that creates a new refused
close (`refused_close_correction`, naming the row); the delete dialog lists, before it asks, the later
close rows of the same contract that would be stranded.

## 5. Valuation (R2, OPT-D4)

- **Price of an open contract** = the price of its last ordinary trade (`close_reason` rows
  excluded), per symbol across accounts, `trade_date ≤` the valuation day; dated that trade's date.
  With no trade since an `OPTION_ADJUST`, the predecessor's last price ÷ `ratio_to`. Read at the
  valuation seam from `portfolio/options.py`; **never written to `prices`**. Options are left out of
  the freshness use of `price_reads` (never in `freshness.prices`, `missing_prices`, `any_stale`).
- **Point-in-time figures include it**; **trend-derived figures exclude its value** without marking
  the day incomplete, with the note 「走勢不含未平倉選擇權的市值；權利金已計入現金，未平倉期間淨值高估（賣方）或低估（買方）該部位市值」
  when `options.trend_excludes_value`. Option cash flows stay in `net_invested` and the cash series.
- **A contract past its expiry with no close row** keeps its last-trade value (R2); it is flagged
  `past_expiry` and prompted by the expiry inbox (§7.2) — never re-valued silently.
- Every `+ options` addition to a KPI / XIRR / FX figure is **skipped** when the block is `None` —
  never `+ Decimal(0)` (Decimal text would drift on a stock ledger).

## 6. Surfaces

### 6.1 Vocabulary — one table, every surface uses it

| Concept | zh (the only spelling) |
| --- | --- |
| `option_side` values | 賣出開倉 (STO) · 買進平倉 (BTC) · 買進開倉 (BTO) · 賣出平倉 (STC) · 買進平倉＋買進開倉 (a CSV / edit BUY that over-covers) · 到期作廢 · 被指派 · 履約 |
| direction | 買方 · 賣方 |
| units | 口 · 股當量 |
| price | 權利金（每股） · 履約價 · 履約義務 |
| right | 買權 · 賣權 (column headers); the display name keeps 「Put／Call」 as on the statement |
| action kind | 選擇權合約調整 |

`option_side` is derived by **one** pure classifier, `shared/options.py`, from (side, short_sale,
close_reason, signed position before the row), applied by **one walk per account over its option
rows and its `OPTION_ADJUST` rows** in replay order `(date, priority, id)` — an adjustment (before
the day's trades) moves the count × `ratio_to` from the source contract to the destination, so a
buy-back after an adjustment reads 買進平倉 exactly as the replay books it — never per row, never in
JS. The ledger tab, the cash lines, the drawer, the exports
and the inbox all read it. A vocabulary guard test (like `test_i14`) pins the table.

### 6.2 Surface table

| Surface | v1 behaviour |
| --- | --- |
| Quote jobs | `TRACKED_SQL` filters `asset_type='stock'` (one seam for every scheduled fetch). Bypasses refuse an option: `quick_register` and `restore_archived` **at entry, regardless of `force`** 「選擇權合約由交易登錄建立」; the gap backfill; `refresh_instrument_quote`; the Alpha Vantage held universe; held-only news. An option is never in a worklist |
| Dashboard | `holdings`, allocation, weights (`weight = stock MV / Σ stock MV`), subtotals, `realized.rows` and its totals: stocks only, computed by feeding the same functions the stock-only list. KPI totals: stocks + options with 「其中選擇權」 from `options.market_value`. A 待釐清 banner when `options.refused_closes` is non-empty. The 選擇權 section (§7) |
| Drawer | a contract: header (underlying, expiry, strike, right, multiplier), trades in 口 with `option_side` chips, realized rows with reason chips, no 配息史, 「選擇權不提供試算」, 待釐清 + remedy; its reconciliation footer reads the book's count from `options.open` (unit 股當量). A stock: the line 「未平倉選擇權：2 口 2026-12-18 50 Put（賣方）→ 選擇權區」 from `options.open` by underlying |
| Watchlist | contracts hidden unless 「顯示選擇權合約」; `/api/instruments` elements carry `asset_type` |
| `/api/input/holdings` (picker, sell hints) | an option position with a **non-zero signed** count is `held` (never 已清倉), carrying `asset_type`, `direction`, `contracts`, signed `shares`; the picker shows 「XYZ 2026-12-18 50 Put・賣方 2 口」; the hint reads 「可平倉 N 口」. Stock rows unchanged |
| Target weights, rebalance, what-if, ex-dividend calendar, projection; signals, alerts, alert inputs, insights, LLM variables, prompts, digest, news, fundamentals | exclude options (OPT-D10). The dividend inbox is already covered by `TRACKED_SQL` |
| Ledger tab (交易) | rows carry `close_reason`, `contracts`, `option_side`, `refused_close` (null / false on a stock); contract display name, 口數 beside share-equivalents, the §6.1 chips; keyword filter matches the display name; a refused close reads 「未入帳（待釐清）」; deep link `?ledger=tx&txn_id=` |
| 公司行動 tab / form | the fourth kind 「選擇權合約被調整（分割／更名後合約代號改變）」, example 「1 口 60 Put → 3 口 20 Put；SPAC 更名 1：1」; 來源 = a held contract; 目的 = 新標的代號（預設同）＋新履約價 → the compose endpoint → `to_symbol`; 來源 is fixed at 1 口 and the form asks 目的 口數 (= `ratio_to`); the preview's `strike_products` / `conserves` |
| Cash page / 現金收支明細 | `CashLine` carries `option_side`; a line reads 「賣出開倉 1 口 XYZ 2026-12-18 50 Put」, 「到期作廢」, 「被指派」, 「履約」 |
| 收件匣 | the third panel 「待確認到期」 (§7.2); the sidebar badge adds its count |

## 7. The 選擇權 section, the expiry inbox, and the wire

### 7.1 Section and block

`DashboardData.options: OptionsBlock | None`, `None` unless the **uncut** ledger has ≥ 1 transaction
or action on an option instrument; the dashboard router removes the key when `None`. **Every new
dashboard wire field lives inside this block.** Contents (Decimal strings, server-computed):

- `open` rows (per account × contract): contract, `underlying`, 到期日 + 剩餘天數, 履約價,
  `underlying_price` + its date (OPT-D17; one latest-price read per distinct registered underlying —
  usually not held, so never from the holdings' price reads and never added to freshness; omitted when
  unpriced), 買權/賣權,
  方向, 口數, 股當量, 平均權利金, `premium_total` (已收／已付權利金), 最後成交價 + 日期, 市值, 未實現損益,
  履約義務 (sold put: strike × 100 × contracts), `past_expiry`, `unbookable_close`.
- `obligation_subtotals` (per account × currency); `market_value`, `unrealized` (per currency and in
  the reporting currency); `refused_closes[]` (row id, account, contract, date, reason, remedy).
- `realized.rows` — the replay's own `RealizedRow`s for option symbols, same shape as the stock
  `realized.rows`, one per closing event, plus `close_reason` (from `Book.realized_close_reasons`) and
  `contract`; aggregates `by_contract` (how each closed), `by_year`, `by_underlying`; `realized_total`.
- `trend_excludes_value`, `note`.

**Layout (after 持倉明細):** 未平倉 sorted by 剩餘天數 ↑; leading columns 合約・方向/口數・剩餘天數・
履約價（標的現價）・已收權利金・未實現・履約義務; subtotal 「履約義務合計」 per account / currency; 股當量 and
最後成交日 in the row expander; ≤ 640 px renders as cards (no horizontal scroll). A `past_expiry` row
carries the class badge 「已過到期日・待登錄」 and a button 「登錄到期作廢」 that opens the manual form in
option mode prefilled (account, contract, 到期作廢, 口數 = whole position, date = expiry). Empty open
list: 「目前無未平倉合約」 with 已實現 still shown. 已實現 defaults to 依合約 with reason chips; tabs
依年度／依標的. The R3 note sits under the section.

### 7.2 Expiry inbox 「待確認到期」 (OPT-D13)

Compute-on-read, like the rebate inbox; its only state is `option_expiry_skips` (created at boot — a
GET never writes). **An item** = an open option position (non-zero signed, per account × contract)
whose expiry is before the server's today, with no close row and not skipped: 「XYZ 2026-12-18 50 Put
（嘉信）・賣方 2 口・到期日已過 N 天 → 到期作廢，權利金 400.00 USD 全數實現」 (server strings). **Actions:**
「確認到期作廢」 writes the §4.1 price-0 row through the same validator helper and write path as the
manual door; 「改登錄被指派／履約」 deep-links to the manual form prefilled for the two-leg action;
「略過」 records a skip (「取消略過」 in the skipped list); 「全部確認」 (an in-page confirm dialog) confirms
every listed item, each validated on its own, and reports per item. An item whose contract already
carries a refused close shows 待釐清 and the §4.4 remedy instead of 「確認到期作廢」 (a confirm would be
refused by §4.2's same-day rule). A count route feeds the sidebar badge. Detector and routes live in `api/` (the detector reads the book; `data_ingestion` never
imports `portfolio` for it).

## 8. Import doors

- **Transaction CSV:** `close_reason` joins `TRANSACTION_COLUMNS` (optional); an unregistered OCC
  symbol is registered from its terms in the commit's transaction (the preview says 「將建立選擇權合約
  …」); `shares` in share-equivalents — the hint 「shares（選擇權填股當量＝口數×100）」, the example note
  「2 口＝200」, the preview cell 「200 = 2 口」 (server `contracts`) and `contract_name` (the OCC symbol as
  tooltip); fees per OPT-D6 and §4.2; a soft `option_premium_exceeds_strike` on a put.
- **Corporate-action CSV:** `OPTION_ADJUST` / 「選擇權合約調整」, OPT-D11. **Opening CSV:** option
  symbols refused. **`unknown_action_kind`** and the CSV hint list the four kinds (from `KIND_ZH`).
- **Compose endpoint:** `GET /api/input/option-symbol?underlying&expiry&strike&right` → `{symbol,
  name}` or a zh refusal (digit root, malformed strike, non-US). Used by the picker builder and the
  `OPTION_ADJUST` form; the browser never encodes a strike.
- **Manual entry:** on a US account the picker footer gains 「＋選擇權合約」 beside 「＋新增標的」 → an
  inline builder 標的（US 代號）／到期日／履約價／買權｜賣權 → the compose endpoint. Option mode relabels
  股數→口數 (`step=1`, the server returns `share_equivalents` → 「= 200 股當量」) and 價格→權利金（每股） (hint
  「對帳單 Price 欄；總權利金由伺服器計算」); four side buttons replace 買進／賣出＋放空, defaulted by
  position (OPT-D18); 口數 defaults to the whole position on a closing side; the preview's large label
  reads 「淨收權利金」／「權利金總成本」; 「到期作廢」 and 「被指派／履約」 actions (§4.2). After a 買進平倉 commit
  an inline button 「轉倉：賣出開倉同標的新合約」 reopens the builder prefilled (underlying, right, 口數,
  date), side 賣出開倉, focus on 到期日 — two rows, no chain (§12). The fee refusal for `moomoo_my`
  reads 「此帳戶未設定選擇權手續費：請按 ✎ 覆寫填入對帳單手續費，或到 系統設定 → 費率 設定每口費率」 with a link;
  the fee label reads 「每口 0.65＋SEC（賣出）」. The fee settings panel lists the two new fields
  「選擇權每口手續費」／「選擇權每口監管費」 for US rule sets.
- **What's new:** an entry titled 「可以登錄美股選擇權了：賣出 Put、到期、被指派、合約調整都進帳本」, href
  `trades.html`, target `#m-sym-foot`, area 「交易帳本 → 交易輸入／儀表板 → 選擇權」.
- **AI input door:** an OCC symbol is code-shaped; AI-recognised option rows commit through the CSV
  door's path and register there (OPT-D7); `resolve.py`'s register-first path meets
  `quick_register`'s entry refusal. `MARKET_CODE_PATTERNS["US"]` is unchanged.
- **Schwab converter** (pure; the CSV door registers): option rows stop being dropped. `option_symbol`
  → OCC; **quantity = `contracts_to_shares(Quantity)`**, price = the listed per-share premium (so the
  reconciler's qty × price = Amount check still ties); Sell to Open → SELL `short_sale=1`; Buy to
  Close / Buy to Open → BUY; Sell to Close → SELL; fees verbatim, tax 0; `Expired` and TD's
  「REMOVAL OF OPTION DUE TO EXPIRATION」 (re-mapped from `OPT_ADJUST`) → price-0 `expired`, dated the
  contract's expiry, side from the running signed position **across every file of the request plus
  `known_positions`** (OPT-D19, injected by `api/routers/broker_import.py`: the ledger's signed
  share-equivalents per (account, contract) **through the day before the request's earliest row**;
  the running count = that + the request's rows; an overlap with already-imported rows is caught at
  the CSV door by `expired = |open|`), same-day
  rows per contract merged, written after that day's trades; odd-split / position-change journal
  pairs → one `OPTION_ADJUST` with `ratio_to = |to qty| ÷ |from qty|`, accepted only when a whole
  number and OPT-D11 holds (else `option_nonstandard_adjust`); 「TRANSFER OF SECURITY OR OPTION OUT」 and
  「…OPTION IN」 between the broker's own platforms → dropped with a stated reason. `UnmappedRow`
  carries `reason` ∈ {`option_assigned_unmapped`, `option_nonstandard_adjust`,
  `option_expiry_no_opening`} and the line number; the 422 body carries both; the page prints a
  per-reason remedy (e.g. 「第 N 列是選擇權被指派／履約：第一版轉換器不處理。請先從匯出檔刪除這一列再轉換；被指派本身請用手動交易的
  「被指派／履約」登錄」). The report's labels use §6.1 and 「2 口（200 股當量）」 — the display name goes into the
  existing `symbol` cell and 「2 口（200 股當量）」 into the `shares` cell of `RowDetail.cells`, so the
  conversion report gains **no** new key.

## 9. Export doors

| Export | Change |
| --- | --- |
| Raw ledgers zip / per-tab CSV | `transactions.close_reason` rides `SELECT *`. `instruments` is not in the zip; an option's terms re-derive from its symbol on re-import |
| 持股 CSV / 持股報表 | stock rows as today; a 選擇權 block (§7.1 columns) and the 「其中選擇權」 line only when `options` is present |
| 已實現損益 CSV | stock `realized.rows` + `options.realized.rows` (same columns, one per closing event); `asset_type`, `contract` appended |
| 稅務包 | option closes already land in capital gains (`sale` / `short_cover`); contract name and a 「其中選擇權」 subtotal |
| 帳本報告 | 口數, §6.1 chips, refused closes marked 「未入帳（待釐清）」; `OPTION_ADJUST` in the actions section |
| 現金收支明細 CSV / print | descriptions from `option_side` |
| 個股明細 CSV | works for a contract |
| 再平衡報告 | excludes options |

## 10. Verification

- **Invariant 1 (blast radius, D38).** A ledger with no option is byte-identical in every API payload
  and export **except these additive changes**, each re-pinned in the same commit wherever a test pins
  it: (1) the raw transactions dump / per-tab CSV gains `close_reason`; (2) the transaction import
  template gains `close_reason（選填）`; (3) the realized CSV gains `asset_type`, `contract`; (4)
  additive **wire keys**, null / false / `"stock"` on a stock ledger: cash statement rows
  `option_side`; `/api/ledgers/transactions` rows `close_reason`, `contracts`, `option_side`,
  `refused_close`; instrument elements of `/api/instruments` and `/api/input/context` `asset_type`;
  `/api/input/holdings` rows `asset_type`, `direction`, `contracts`; the manual preview
  `share_equivalents`, `contract_name`, `legs`; the corporate-action preview `strike_products`,
  `conserves`; the broker-import 422 body `reason`, `line`; the CSV import preview rows' `data`
  `contracts`, `contract_name`; the `/api/fee-rules` `fields` `option_per_contract`,
  `option_reg_per_contract` (every rule set, editable). **New routes** (the compose endpoint, the
  expiry inbox) add no key to an existing payload; the inbox count is 0 on a stock ledger, so the
  sidebar badge is unchanged. The dashboard payload — the golden — does not change at all.
- **Hermetic tests** per mechanism and per rule (§4 incl. structural price-0, fee-free closes, side
  derivation, OPT-D14 refusals, refused closes with no position, the delete pre-list), per surface (a
  scan of stock readers; the §6.1 vocabulary guard; `option_side` identical on the ledgers wire, cash
  lines and exports), the converter (every option row shape, `known_positions`, units vs Amount, every
  `UnmappedRow` reason), the inbox (detector, skip, confirm, confirm-all per item), and e2e for the
  section, option-mode entry, the roll button, the inbox and the drawer footer. The e2e seed never
  dates an expiry on the DEF-056 sweep's seeded day; the sweep's docstring records that 到期日 is a
  contract term.
- **Stress-audit oracle** transcribes from this spec, never importing the app: price-0 closes, §4.4,
  `OPTION_ADJUST` (`ratio_from == 1`), the option fee branch, last-trade valuation, the trend
  exclusion; terms parsed from the symbol. Phase-1 scenario ops cover every §4.1 row and the cash tie.
- **Accounting manual** (zh + en): an options section with its verification anchor.
- **Real-data acceptance** (local, git-ignored): the owner's Schwab option rows through the converter
  and the CSV door — option realized, the open contract and the option cash total equal
  `OPTIONS_ledger_proposals.md`. The full-history cash target follows the trade / dividend / opening
  conversion (the next work item).

## 11. Build order

`W1` foundation → `W2` replay + valuation + trend + payload → `W3` doors → `W4` exclusions → `W5` web
→ `W6` exports + the expiry inbox backend → `W7` oracle, manual, acceptance. Waves, ownership and
gates: the plan.

## 12. Deferred — approach ⑤ (dedicated options sub-ledger), TODO plan

Started only when the owner asks. `shared/options.py` carries a `TODO(options-⑤)` block pointing
here. (The expiry **inbox** is v1 — OPT-D13 — because it prompts the ledger's own completeness; the
items below remain deferred.)

- **Triggers:** multi-leg strategies, frequent rolls the owner wants analysed as chains, assignment
  volume, a need for live option quotes, or a non-standard deliverable in the owner's history.
- **Roll chains:** link a buy-to-close and a same-day sell-to-open, with the chain's cumulative
  premium (v1 has only the 「轉倉」 shortcut, two unlinked rows).
- **Collateral:** reserve a sold put's strike × 100 against the cash pool (「可用現金」 vs 「帳戶現金」).
- **Quotes and Greeks:** a provider path and mark-to-market history (would retire R3's exclusion).
- **Non-standard deliverables:** a per-contract deliverable model (retires OPT-D11 / D16's refusal).
- **Assignment linkage:** optionally fold the premium into the stock's cost (R5 declines for v1).
- **Option alerts:** pre-expiry reminders, assignment-risk alerts, option context in AI insights.
- **Converter:** per-row skip of unmapped option rows; an `Assigned` / `Exercise` mapping once a real
  row exists (OPT-D9).
- **Strategy views:** P&L by strategy, annualised return on collateral.
