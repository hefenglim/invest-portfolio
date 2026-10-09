# Spec — US equity options in the ledger (P3, approach ④)

**Status:** APPROVED 2026-10-09 — rulings R1–R7 and decisions OPT-D1..D22 confirmed by the owner in
chat (OPT-D1..D10 「10 項全部照建議」; OPT-D11 / D12 after review round 1; OPT-D13..D19 after review
round 4; OPT-D20..D22 after the pre-build review). Implementation follows
`docs/superpowers/plans/2026-10-09-options-ledger.md` on branch `feat/options-ledger` (off `main`
after `feat/corporate-actions` was merged, `aa5c80a`) and starts only after the owner approves the
plan.
**Builds on:** the corporate-action framework, the replay and the declared short sale (`main`).
**Supersedes:** the P3 section of `docs/spec/2026-08-06-broker-import-backlog.md` as the plan of
record for options. Its multiplier census and its "direction is not a gap" finding still hold.

**Revision log**

| Date | Change |
| --- | --- |
| 2026-10-09 | First draft; owner rulings R1–R7; OPT-D1..D10 confirmed as recommended |
| 2026-10-09 | **Review round 1** (Fable 5.1, max; NOT READY → folded): OPT-D11; the replay refuses a close that would open a lot; new dashboard wire fields only in `options`; stock-only lists; `OPTION_ADJUST` placement and field table; named additive columns; OPT-D12; close rules; ownership; walker skip |
| 2026-10-09 | **Review round 2** (READY-WITH-FIXES → folded): additive wire keys; `Holding.unbookable_close`; drawer reconciliation; `options.realized.rows`; option-mode refusals; same right / expiry; the assignment's stock leg; freshness |
| 2026-10-09 | **Review round 3** (READY): wording |
| 2026-10-09 | **Review round 4** — reviewer A (consistency), reviewer B (entry-point usability), reviewer C (integration; M1–M21); owner OPT-D13..D19; fold verification N1–N9 (the `option_side` walk includes `OPTION_ADJUST`; `account_statement` carries the side) |
| 2026-10-09 | **Pre-build review** (Opus 5.5, max; GO-WITH-FIXES; P1–P21, two blockers). Owner OPT-D20 (本金匯率效果 withheld while an option is open), OPT-D21 (option-mode 賣出開倉 while long refused), OPT-D22 (no `option_contracts` table — terms parse from the symbol). Folded: P1 the converter's display text goes to `RowDetail.symbol` / `shares`, never `cells` (§8); P3 `Transaction.id` and the pinned `RefusedClose` (§3); P4 the manual / edit request wire (§4.2, §8); P5 the inbox routes (§7.2); P6 `CashLine.txn_id` (§3, §6.2); P7 the edit door (§4.2); P8 `past_expiry` / the inbox read the book, and a back-dated trade cannot strand an `expired` row (§4.2, §7); P9 classifier labels (§6.1); P10 the corporate-action validator's bypasses (§4.3); P11 the converter's error path, CLI and openings (§8); P12 fees (§2, §4.2, §8); P13 the fee snapshot in the zip (§10); P14 block keys (§7.1); P15 replay order (§4.3, §4.4); P17–P20 wording, preview, the stranded-close list. Fold verification (Opus 5.5): GO-WITH-FIXES → N1–N6 folded (inbox skip shapes; preview never registers; fee-field nullability; OPT-D21's mirror recorded) |

**Why now.** A real broker history cannot reconcile without its option legs: the premiums are cash
the account really received and paid. The owner's acceptance figures live in the git-ignored
`sample-trade-data/charles-schwab/OPTIONS_ledger_proposals.md` (real data — never copied into a
committed file); this spec uses synthetic examples only.

## 1. Scope

**In:** US equity options — single-leg calls and puts with the standard 100-share deliverable — in
the US accounts (`schwab`; `moomoo_my`'s US market, fee rule set `moomoo_us`). Open / close in both
directions, expiry, assignment, exercise, **standard** contract adjustments, valuation of an open
contract, a separate 選擇權 section, an **expiry inbox** (§7.2), every import and export door.

**Out (v1):** TW and MY options (R6). Non-standard deliverables (OPT-D11 / D16). Multi-leg grouping,
roll chains, collateral reservation, quotes / Greeks for open contracts, option-aware AI insights and
**alerts** — deferred to approach ⑤ (§12). Index and non-equity options.

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
| OPT-D1 | Quantity unit | **Share-equivalents** (contracts × 100); price = premium per share | Every `quantity × price` (≥ 28 sites, backlog F-46) stays correct untouched |
| OPT-D2 | Expiry / assignment / exercise | A **price-0 closing trade** with `close_reason` ∈ {`expired`, `assigned`, `exercised`} | Brokers list them as transaction rows; a same-day buy-to-close sorts by write order |
| OPT-D3 | Contract adjustment | New corporate-action kind **`OPTION_ADJUST`**, option → option, carrying long AND short lots | `EXCHANGE` refuses a short source (D33); a new kind leaves equities byte-identical (D38) |
| OPT-D4 | Which figures include an open option | **Point-in-time** figures include it at its last trade price (KPI 總市值 / 總報酬 / XIRR, total return A, the FX pool, 月度成績). **Trend-derived** figures exclude its value (trend chart, B, TWR, benchmark) with one note | XIRR's final value must carry the open liability |
| OPT-D5 | Where options appear | `holdings` rows, allocation, weights, subtotals: **stock-only**; KPI totals **include** options with a 「其中選擇權」 line; the 已實現損益 section shows a separate option line (not summed) | R4 without dropping option P&L from the totals |
| OPT-D6 | Fees | Per-**rule-set** `option_per_contract`: `schwab` 0.65, `moomoo_us` unset; + SEC on sells (notional basis) + optional `option_reg_per_contract` (default 0); no MY stamp on an option in v1; a statement-supplied fee wins; an unset rate makes the fee required | The engine must not guess a rate it was never given |
| OPT-D7 | Registration | An option instrument is **created from its symbol** at the CSV door and the manual door, with no quote lookup; the underlying need not be registered | A quote gate cannot pass for an option |
| OPT-D8 | Entry units | CSV `shares` = share-equivalents (multiple of 100); the manual and edit forms send **口數** (`contracts`); the server converts | The raw ledger CSV must round-trip; the multiplier lives only in `shared/options.py` |
| OPT-D9 | Assignment / exercise rows in the broker converter | **Not mapped blind** — `UnmappedRow` with a reason (§8) | No rule without a real row behind it |
| OPT-D10 | v1 exclusions | Options are out of AI insights / signals / alerts / digest / news / fundamentals, the watchlist list (filterable), target weights, rebalance, what-if, the ex-dividend calendar and projection | Each reader treats a held symbol as a stock (§6) |
| OPT-D11 | Non-standard deliverables | **Standard only**: both contracts parse with multiplier 100, letters-only destination root, same right, same expiry, **`ratio_from == 1`**, `strike_from × ratio_from == strike_to × ratio_to`; else refused (doors) / `UnmappedRow` (converter) | v1 has no deliverable model |
| OPT-D12 | 月度成績 | **Includes options** (it stores the KPI 總市值); no note | A point-in-time figure |
| OPT-D13 | Expiry prompting | **An expiry inbox in v1** — a third 收件匣 panel 「待確認到期」 (§7.2) | The most frequent option chore; a ledger-completeness prompt, not an alert |
| OPT-D14 | Option-mode 買進平倉 beyond the short | **Refused** 「買進平倉口數超過空頭部位（N 口），請分成平倉與開倉兩筆」 | The manual door has one ack (`ack_oversell`) |
| OPT-D15 | Side labels | **Server-derived `option_side`** (§6.1), one classifier, never in JS | 買進平倉 and 買進開倉 are both a bare BUY |
| OPT-D16 | Adjustment ratios | **`ratio_from == 1`** (1 → N contracts, or 1:1) | `ratio_from > 1` would leave a fractional contract |
| OPT-D17 | Underlying price beside the strike | **Shown** when the underlying is registered and priced, with its date; no 價內／價外 wording | The put seller's first question in expiry week |
| OPT-D18 | Default side in option mode | **By position** (held short → 買進平倉, held long → 賣出平倉, none → 賣出開倉); 口數 defaults to the whole position on a closing side | The owner's dominant actions |
| OPT-D19 | Converter across exports | **`known_positions` injected** by the broker-import route (§8) | Statements may be imported separately; the converter stays pure |
| OPT-D20 | 本金匯率效果 while an option is open (pre-build review) | **Withheld** — `principal_fx_effect` is `None` (「—」) whenever an option position is open on the valuation day, with the reason in `options.principal_fx_note` | B excludes the open option's value while A includes its unrealized; their difference would absorb the option's value as "FX effect" (AI-D48) |
| OPT-D21 | Option-mode 賣出開倉 while long (pre-build review) | **Refused** 「目前持有買方部位 N 口，請先賣出平倉」; and its mirror, 買進開倉 while short, refused 「目前持有賣方部位 N 口，請先買進平倉」 (the mirror was added at the fold — confirmed with the plan approval) | Symmetric with OPT-D14; one row does one thing |
| OPT-D22 | Contract terms storage (pre-build review) | **No `option_contracts` table** — `Instrument.option` is parsed from the symbol | The symbol carries every term and v1 fixes the multiplier at 100; a table would be a second copy that can disagree. A deliverable model (§12) may add one |

## 3. Data model

- **`instruments.asset_type`** `TEXT NOT NULL DEFAULT 'stock'` — `'stock'` | `'option'`. Additive.
  `Instrument.is_option` **is** `asset_type == 'option'`; `Instrument.option: OptionTerms | None` is
  `parse_option_symbol(symbol)` when `is_option` (OPT-D22; no table). Every registering door sets
  `asset_type` from the parse, so symbol and type never disagree.
- **Symbol** — OCC without root padding: letters-only root, `YYMMDD`, `C`/`P`, strike × 1000 as 8
  digits — `^[A-Z]{1,6}\d{6}[CP]\d{8}$`, e.g. `XYZ261218P00050000`. One parser and one composer in
  `shared/options.py`; the browser never composes a symbol (compose endpoint, §8). Display name
  「XYZ 2026-12-18 50 Put」. Market `US`, currency `USD`. Registration defaults: `name` = display name,
  `sector=""`, `board=""`, `is_etf=False`, `etf_flag_unknown=False`.
- **`transactions.close_reason`** `TEXT NULL` — `expired` | `assigned` | `exercised`. Additive.
- **`OPTION_ADJUST`** joins `CorporateActionKind` with zh 「選擇權合約調整」.
- **`option_expiry_skips`** (owned by `api/option_expiry.py`, created at boot): `account_id`,
  `symbol`, `skipped_at`; primary key `(account_id, symbol)`.
- **Models:** `Instrument.asset_type` / `option` / `is_option`; `Transaction.id: int | None = None`
  and `Transaction.close_reason` (`load_ledger_bundle` passes both); `TxnInput.close_reason` (the
  validators work in share-equivalents — the routers convert 口數 before validating);
  `_Position.unbookable_close`, `OpenedPosition.unbookable_close`, `Holding.unbookable_close`
  (sticky, §4.4; never on the holdings wire); `RefusedClose` = `txn_id`, `account_id`, `symbol`,
  `date`, `side`, `quantity`, `close_reason`, `reason`, `remedy`; `Book.refused_closes:
  list[RefusedClose]`; `Book.realized_close_reasons: dict[int, str]` (index into `Book.realized.rows`
  in emission order → `close_reason`); `CashLine.txn_id: int | None = None` (an identity set by
  `pool_lines`, not a label) and `CashLine.option_side: str | None = None`; `OptionSide` (§6.1);
  `DashboardData.options: OptionsBlock | None` (§7.1).

## 4. Ledger mechanics

A contract is a position in share-equivalents. The replay keeps a long lot and a separate positive
short lot; the signed `shares` is derived on output. Nothing below changes how a stock is booked.

### 4.1 Event mapping

| Broker event | Ledger row | Replay effect | Realized row |
| --- | --- | --- | --- |
| Sell to Open | SELL, `short_sale=1` | sells any long lot first; the rest opens / extends the short lot | none for the short part |
| Buy to Close | BUY | covers the short lot first | `short_cover` = (short avg − cover cost per share) × covered |
| Buy to Open | BUY | opens / adds to the long lot | — |
| Sell to Close | SELL | sells the long lot | `sale` |
| Expired, short | BUY, price 0, fee 0, tax 0, `close_reason=expired` | covers the whole short lot at 0 | `short_cover` = whole remaining premium |
| Expired, long | SELL, price 0, fee 0, tax 0, `close_reason=expired` | sells the whole long lot at 0 | `sale` = −remaining basis |
| Assigned (sold put / call) | option BUY at 0 `close_reason=assigned` + a stock BUY (put) / SELL (call) of the underlying at the strike, shares = contracts × 100 | option P&L = premium (R5) | option `short_cover`; stock per its branch |
| Exercised (bought call / put) | option SELL at 0 `close_reason=exercised` + a stock BUY (call) / SELL (put) at the strike | the premium paid is the option's realized loss (R5) | option `sale`; stock per its branch |
| Standard adjustment | `OPTION_ADJUST` old → new, `ratio_to : 1` | §4.3 | none |

### 4.2 Close rules and door rules

- **Structural:** `transaction_structural_issues` accepts `price == 0` **only when** `close_reason`
  is set; any other price-0 row keeps 「價格必須大於 0」. `close_reason` requires an option instrument
  and `short_sale=0`; it is refused on a stock.
- **No fee on a close row:** every door sets fee = tax = 0 on a `close_reason` row **without calling
  the fee engine** (snapshot `{"source": "close_reason"}`); a supplied non-zero fee or tax is refused
  「到期作廢／被指派／履約列不收手續費與稅」. The engine's option branch returns 0 / 0 at notional 0.
- **Side is derived.** *open* = the signed position of that account and contract through the row's
  date by `holdings.shares_through(account, contract, on=date, pending=siblings booked before ∪
  replacement)`; the row itself never counts. `expired`: BUY if *open* < 0, SELL if *open* > 0,
  refused if 0 (`option_close_no_position`). `assigned`: *open* < 0, side BUY. `exercised`: *open* >
  0, side SELL. A CSV or edit row whose side contradicts it is refused.
- **Quantity:** `expired` = `|open|` exactly; `assigned` / `exercised`: 0 < quantity ≤ `|open|`,
  multiple of 100. **Date:** `expired` = the contract's expiry; `assigned` / `exercised` ≤ expiry;
  any row on a contract dated after its expiry is refused; a same-day row of the same contract ordered
  after an `expired` row is refused. **A stored `expired` row is never stranded:** any new or edited
  row of that contract dated on or before its expiry is refused unless the `expired` row would still
  equal `|open|` afterwards — 「此合約已登錄到期作廢，請先修改或刪除該列」.
- **Every option row:** quantity a multiple of 100 (「選擇權數量須為 100 的倍數（1 口 = 100 股當量）；若填的是口數，
  請改為 口數×100」); a sell that would open a short without `short_sale=1` is the ordinary 賣超.
- **Manual door — option mode.** The request carries `option_intent` ∈ {`STO`, `BTC`, `BTO`, `STC`}
  and `contracts` (口數; `shares` is then omitted and the server sets it with `contracts_to_shares`),
  or `close_reason` for 到期作廢 / 被指派／履約. The manual door has exactly one acknowledgement,
  `ack_oversell`; `cash_overdraft` is a preview warning. Refusals: `BTC` with no short
  「無空頭部位可平倉，請改選買進開倉」, beyond the short (OPT-D14); `STC` with no long
  「無多頭部位可平倉，請改選賣出開倉」, beyond the long 「賣出平倉口數超過多頭部位（N 口），請分成平倉與開倉兩筆」;
  `STO` while long (OPT-D21) 「目前持有買方部位 N 口，請先賣出平倉」; `BTO` while short
  「目前持有賣方部位 N 口，請先買進平倉」. The preview of a close row shows 口數, the realized figure the
  replay would book (server string) and the position after (0 for an expiry).
- **CSV / edit rows** carry no intent and keep the stock rules; a BUY covering more than the open
  short raises 「買進平倉口數超過空頭部位，多出的 N 口將成為買方部位」 — under the file-level `ack_warnings` gate
  on the CSV door, a preview warning on the edit door.
- **Edit door:** the edit body takes `contracts` for an option row (the server converts); the `price
  <= 0` gate is relaxed for a row whose stored `close_reason` is set; the fee recompute skips the engine
  on such a row (fee = tax = 0); `close_reason` absent means keep, and the v1 edit door cannot change
  it (delete and re-enter instead).
- **Elsewhere:** a dividend on an option is refused (`dividend_on_option`); an opening-inventory row
  on an option is refused 「選擇權請以交易登錄」; `SPLIT` / `EXCHANGE` / `SPINOFF` naming an option are
  refused; a `SPLIT` on an underlying with open options raises the soft finding
  「此標的有未平倉選擇權，合約調整請另行登錄（選擇權合約調整）」.
- **Assignment / exercise — one action, two rows.** The request carries `close_reason`
  (`assigned` / `exercised`), `contracts` and `stock_price` (defaults to the strike, editable). Both
  rows are written in one database transaction (both or neither; a failed second leg rolls back the
  first). The preview returns `legs: [option, stock]`, fully derived: stock symbol = underlying,
  side from right × direction, shares = contracts × 100, fee by the engine; the card shows
  選擇權腿／股票腿 and 「被指派：以 50.00 買入 200 股 XYZ，現金 −10,000.00 USD」 (server strings). The pair's
  one acknowledgement is `ack_oversell` when a call assignment's stock SELL exceeds holdings. The
  underlying registers through the existing auto-register path (quote gate applies); on failure the
  pair is refused 「請先登錄標的 XYZ」 with the inline 立即註冊 link. The commit answers
  `txn_ids: [option, stock]` and `legs` with per-leg totals.

### 4.3 `OPTION_ADJUST`

- **Validation:** both symbols are option contracts (an unregistered destination is registered from
  its symbol — E10's registration is satisfied that way); OPT-D11 holds (a row with `ratio_from ≠ 1`
  is refused 「選擇權合約調整以 1 口為基準，請填 1 → N」); positive integer `ratio_to`; from ≠ to; the source
  holds a **non-zero signed** position at the `(date, CORPORATE_ACTION)` cut
  (`shares_before_action_on`, a short counts); the destination is flat or in the same direction.
  `OPTION_ADJUST` **bypasses** the generic short checks E5 (`short_source`) and E18
  (`short_destination`) — the same-direction rule replaces them — and **skips** N3-price
  (`to_symbol_unpriced`; an option is never quoted); every zh message names 「選擇權合約調整」, never the
  SPINOFF fallback wording. The preview returns `strike_products` and `conserves` (the form prints
  「履約價×口數：60×1 ＝ 20×3 ✓」 without browser arithmetic).
- **Replay branch** in `_apply_action`, **immediately after the SPLIT branch and before the E5
  block**. Because it sits before the existing destination lookup, it does its own: look up / create
  Q, E22 (Q ever oversold), the opposite-direction `_reject`, then the transfer, then `_reopen(Q)`.
  Field transfer, P = source, Q = destination:

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
  §4.4 table gain its row — SPLIT unchanged; EXCHANGE / SPINOFF OR it into Q like `unbookable_action`.
- **Share walker** (`_delta_of`, `created_by`, `opened_by`): moves the signed count × `ratio_to`,
  shorts included; opposite signs → skip + `note_negative_side_skip`. The walker does not replay §4.4,
  so on a ledger that already carries a refused close its option count may differ from the book's —
  a permitted divergence on a 待釐清 ledger only (as D33 permits for skipped actions).

### 4.4 A close the replay cannot book

The replay refuses a `close_reason` row that **would open or extend a lot** — a BUY beyond the short
lot, a SELL beyond the long lot, or a close against the opposite direction (a partial `assigned` /
`exercised` within the lot books normally). The test runs **before** the position is created or
`_reopen`ed, so a refused close never creates a position nor clears `vacated_to`. The replay books
nothing for that row and always records a `RefusedClose`; when the position exists it also sets the
sticky `unbookable_close` (strict path: raises `UnbookableLedgerError`). **XIRR and B are withheld
whenever `Book.refused_closes` is non-empty** — the record, like `orphan_dividends` — with a reason
naming the row. Every reader of `unbookable_action` — `portfolio/pnl.py`, `position_aggregate.py`,
`dividends.py`, `timeseries.py`, the manual preview, the drawer, the badges — reads
`unbookable_close` the same way. Every surface that lists the row marks it 「未入帳（待釐清）」 with the
remedy 「刪除該筆到期作廢／被指派／履約列，或補回被刪除的交易」: the 交易 tab, the drawer's trade list, the 選擇權
section, a dashboard banner with 「前往該筆交易」. The correction doors' replay guard refuses a
correction that creates a new refused close (`refused_close_correction`); its 422 lists every close
row the correction would strand, and the delete dialog renders that list (it may also pre-list them
from `GET /api/ledgers/transactions?account_id&symbol`).

## 5. Valuation (R2, OPT-D4, OPT-D20)

- **Price of an open contract** = the price of its last ordinary trade (`close_reason` rows
  excluded), per symbol across accounts, `trade_date ≤` the valuation day; dated that trade's date.
  With no trade since an `OPTION_ADJUST`, the predecessor's last price ÷ `ratio_to`. The price enters
  `price_map` (XIRR's terminal value reads it) but **not** `price_reads` — never in
  `freshness.prices`, `missing_prices`, `any_stale`; **never written to `prices`**.
- **Point-in-time figures include it; trend-derived figures exclude its value** without marking the
  day incomplete, with the note 「走勢不含未平倉選擇權的市值；權利金已計入現金，未平倉期間淨值高估（賣方）或低估（買方）該部位市值」
  when `options.trend_excludes_value` (true when at least one trend day skipped an option holding).
  Option cash flows stay in `net_invested` and the cash series.
- **本金匯率效果 (OPT-D20):** whenever an option position is open on the valuation day,
  `principal_fx_effect` is `None` (shown 「—」) and `options.principal_fx_note` reads
  「有未平倉選擇權時，含匯兌總損益不含其市值而資產損益含其未實現，兩者相減無法拆出本金匯率效果；平倉或到期後恢復顯示」. With no open
  option the figure is computed exactly as today.
- **Past expiry:** a contract whose expiry is before the valuation day while the **book's position is
  non-zero** keeps its last-trade value (R2), is flagged `past_expiry`, and is prompted by the expiry
  inbox (§7.2).
- Every `+ options` addition to a KPI / XIRR / FX figure is **skipped** when the block is `None` —
  never `+ Decimal(0)`.

## 6. Surfaces

### 6.1 Vocabulary — one table, every surface uses it

| Concept | zh (the only spelling) |
| --- | --- |
| `option_side` | 賣出開倉 (STO) · 買進平倉 (BTC) · 買進開倉 (BTO) · 賣出平倉 (STC) · 買進平倉＋買進開倉 (a BUY that over-covers, CSV / edit) · 賣出平倉＋賣出開倉 (a declared SELL while long, CSV / edit) · 賣出平倉（賣超） (an undeclared SELL beyond the long) · 到期作廢 · 被指派 · 履約 · 未入帳（待釐清） (a §4.4-refused close) |
| direction | 買方 · 賣方 |
| units | 口 · 股當量 |
| price | 權利金（每股） · 履約價 · 履約義務 |
| right | 買權 · 賣權 (column headers); the display name keeps 「Put／Call」 as on the statement |
| action kind | 選擇權合約調整 |

`option_side` is derived by **one** pure classifier, `shared/options.py::classify_option_rows`,
applied by **one walk per account over its option rows and its `OPTION_ADJUST` rows** in replay
order `(date, priority, id)`: an adjustment (before the day's trades) moves the count × `ratio_to`
from source to destination; the walk mirrors the replay's refusals — a §4.4-refused close is labelled
未入帳（待釐清） and moves nothing; an adjustment the replay would refuse (no source position, opposite
direction, an oversold source or destination) moves nothing. Never per row, never in JS. The ledger
tab, the cash lines, the drawer, the exports and the inbox all read it. A vocabulary guard test pins
the table.

### 6.2 Surface table

| Surface | v1 behaviour |
| --- | --- |
| Quote jobs | `TRACKED_SQL` filters `asset_type='stock'`. Bypasses refuse an option: `quick_register` and `restore_archived` **at entry, regardless of `force`** 「選擇權合約由交易登錄建立」; the gap backfill; `refresh_instrument_quote`; the Alpha Vantage held universe; held-only news |
| Dashboard | `holdings`, allocation, weights (`weight = stock MV / Σ stock MV`), subtotals, `realized.rows` and its totals: stocks only, via the same functions fed the stock-only list. KPI totals: stocks + options with 「其中選擇權」; KPI 總市值 = the stock grand subtotal + `options.market_value` on an option ledger. A 待釐清 banner from `options.refused_closes`. 本金匯率效果 per OPT-D20. The 選擇權 section (§7) |
| Drawer | a contract: header (underlying, expiry, strike, right, multiplier), trades in 口 with `option_side` chips, realized rows with reason chips, no 配息史, 「選擇權不提供試算」, 待釐清 + remedy; the reconciliation footer reads the book's count from `options.open` (unit 股當量). A stock: 「未平倉選擇權：2 口 2026-12-18 50 Put（賣方）→ 選擇權區」 from `options.open` by underlying |
| Watchlist | contracts hidden unless 「顯示選擇權合約」; `/api/instruments` elements carry `asset_type` |
| `/api/input/holdings` | every row gains `asset_type`, `direction`, `contracts` (null on a stock); an option with a non-zero signed count is `held` (never 已清倉), a flat one is `closed`; the picker shows 「XYZ 2026-12-18 50 Put・賣方 2 口」; the hint reads 「可平倉 N 口」 |
| Stock-only readers | target weights, rebalance, what-if, ex-dividend calendar and projection, signals, alerts, alert inputs, insights, LLM variables, prompts, digest, news, fundamentals exclude options (OPT-D10); the dividend inbox is covered by `TRACKED_SQL` |
| Ledger tab (交易) | rows carry `close_reason`, `contracts`, `option_side`, `refused_close`; contract display name, 口數 beside share-equivalents, §6.1 chips; keyword filter matches the display name; deep link `?ledger=tx&txn_id=` |
| 公司行動 tab / form | the fourth kind 「選擇權合約被調整（分割／更名後合約代號改變）」; 來源 = a held contract fixed at 1 口; 目的 = 新標的代號（預設同）＋新履約價 → compose endpoint → `to_symbol`, and 目的 口數 (= `ratio_to`); the preview's `strike_products` / `conserves` |
| Cash page / 現金收支明細 | `CashLine.option_side` is set by `account_statement` from a required `option_side_of: Mapping[int, str]` keyed by `CashLine.txn_id`; `pool_lines` / `cash_balances` never see a label; statement rows carry `option_side` and `contracts`; a line reads 「賣出開倉 1 口 XYZ 2026-12-18 50 Put」 |
| 收件匣 | the third panel 「待確認到期」 (§7.2); the sidebar badge adds its count |

## 7. The 選擇權 section, the expiry inbox, and the wire

### 7.1 Section and block

`DashboardData.options: OptionsBlock | None`, `None` unless the **uncut** ledger has ≥ 1 transaction
or action on an option instrument; the dashboard router removes the key when `None`. **Every new
dashboard wire field lives inside this block.** Keys (Decimal values as strings):

- `open[]`: `account_id`, `symbol`, `contract` (display name), `underlying`, `expiry`,
  `days_to_expiry`, `strike`, `right` (`C`/`P`), `direction` (`long`/`short`), `contracts`,
  `share_equivalents`, `average_premium`, `premium_total`, `last_price`, `last_price_date`,
  `market_value`, `unrealized`, `obligation` (sold put: strike × 100 × contracts, else null),
  `underlying_price` + `underlying_price_date` (OPT-D17; one latest-price read per distinct
  registered underlying, never in `price_reads` / freshness; omitted when unpriced), `past_expiry`,
  `unbookable_close`, `oversold` (a 賣超 contract: it has no holdings row to carry the badge).
  Signs (pinned at the build, 2026-10-10): `contracts`, `average_premium` and `premium_total` are
  magnitudes (positive in both directions — the premium received or paid); `share_equivalents`,
  `market_value` and `unrealized` are signed (a short is negative / profits when the price
  falls). `last_price`, `last_price_date`, `market_value` and `unrealized` are null on a 待釐清
  contract (`oversold` / `unbookable_close`) or with no last trade.
- `obligation_subtotals[]` (`account_id`, `ccy`, `amount`); `market_value` and `unrealized`, each
  `{by_currency: {ccy: amount}, reporting: amount}`; `refused_closes[]` (the `RefusedClose` fields).
- `realized`: `rows` — the replay's own `RealizedRow`s for option symbols, same shape as the stock
  `realized.rows`, plus `close_reason` (from `Book.realized_close_reasons`) and `contract`;
  `by_contract[]` (contract, account, opened, closed, how it closed, premium in, premium out,
  realized — `premium_in` = Σ the contract's realized rows' `proceeds_net`, `premium_out` = Σ
  their `adjusted_cost_removed`, so `premium_in − premium_out = realized` exactly; listed once it
  has a realized row; `closed` / `closed_how` only once the position is flat), `by_year[]`,
  `by_underlying[]`; `realized_total` (`{by_currency, reporting}`).
- `trend_excludes_value`, `note`, `principal_fx_note` (OPT-D20; null when no option is open).

**Layout (after 持倉明細):** 未平倉 sorted by 剩餘天數 ↑; leading columns 合約・方向/口數・剩餘天數・
履約價（標的現價）・已收權利金・未實現・履約義務; subtotal 「履約義務合計」; 股當量 and 最後成交日 in the row
expander; ≤ 640 px renders as cards. A `past_expiry` row carries the class badge 「已過到期日・待登錄」 and
a button 「登錄到期作廢」 (manual form, option mode, prefilled: account, contract, 到期作廢, 口數 = whole
position, date = expiry). Empty open list: 「目前無未平倉合約」 with 已實現 still shown. 已實現 defaults
to 依合約 with reason chips; tabs 依年度／依標的. The R3 note sits under the section.

### 7.2 Expiry inbox 「待確認到期」 (OPT-D13)

Compute-on-read, like the rebate inbox; its only state is `option_expiry_skips` (created at boot).
**An item** = an option position whose expiry is before the server's today while the **book's**
position is non-zero, per account × contract, not skipped. Text (server strings; the account as an
`account_ref` token): short — 「{account} XYZ 2026-12-18 50 Put・賣方 2 口・到期日已過 N 天 → 到期作廢，權利金
400.00 USD 全數實現」; long — 「… 買方 1 口 … → 到期作廢，權利金 250.00 USD 全數損失」. **Actions:**
「確認到期作廢」 writes the §4.1 price-0 row (quantity = the book's |position|) through the same
close-row helper and write path as the manual door; 「改登錄被指派／履約」 deep-links to the manual form
prefilled; 「略過」 / 「取消略過」; 「全部確認」 (in-page confirm dialog) confirms every listed item, each
validated on its own, and reports per item. An item whose contract carries a refused close or an
`expired` row that no longer equals `|open|` shows 待釐清 and the remedy instead of 「確認到期作廢」.

**Routes** (mirroring `/api/dividend-inbox/*`): `GET /api/option-expiry` → `{items: [{account_id,
account_name, symbol, contract, direction, contracts, expiry, days_past, premium_text, text,
confirmable, remedy}]}`; `GET /api/option-expiry/count` → `{count}`; `POST /api/option-expiry/confirm`
`{account_id, symbol}` → `{ok, txn_id}` or a zh 422; `POST /api/option-expiry/confirm-all` →
`{results: [{account_id, symbol, ok, txn_id | error}]}`; `POST /api/option-expiry/skip` /
`/unskip` `{account_id, symbol}` → `{ok: true}`; `GET /api/option-expiry/skipped` → `{items:
[{account_id, account_name, symbol, contract, expiry, skipped_at}]}`. Every GET is read-only. The
detector and routes live in `api/` (they read the book).

## 8. Import doors

- **Transaction CSV:** `close_reason` joins `TRANSACTION_COLUMNS` (optional); an unregistered OCC
  symbol is registered from its terms in the commit's transaction (the preview says 「將建立選擇權合約
  …」); `shares` in share-equivalents — hint 「shares（選擇權填股當量＝口數×100）」, example note 「2 口＝200」,
  preview `contracts` / `contract_name` (「200 = 2 口」, the OCC symbol as tooltip); fees per OPT-D6 and
  §4.2; a soft `option_premium_exceeds_strike` on a put.
- **Corporate-action CSV:** `OPTION_ADJUST` / 「選擇權合約調整」, §4.3. **Opening CSV:** option symbols
  refused. **`unknown_action_kind`** and the CSV hint list the four kinds (from `KIND_ZH`).
- **Compose endpoint:** `GET /api/input/option-symbol?underlying&expiry&strike&right` → `{symbol,
  name}` or a zh refusal (digit root, malformed strike, non-US).
- **Manual entry:** on a US account the picker footer gains 「＋選擇權合約」 → an inline builder
  標的（US 代號）／到期日／履約價／買權｜賣權 → the compose endpoint. Option mode: 口數 (`step=1`; the server
  returns `share_equivalents` → 「= 200 股當量」), 權利金（每股） (hint 「對帳單 Price 欄；總權利金由伺服器計算」),
  four side buttons sending `option_intent`, defaulted by position (OPT-D18); 口數 defaults to the
  whole position on a closing side; the preview's large label 「淨收權利金」／「權利金總成本」; 「到期作廢」 and
  「被指派／履約」 (§4.2). After a 買進平倉 commit an inline button 「轉倉：賣出開倉同標的新合約」 reopens the
  builder prefilled (underlying, right, 口數, date), side 賣出開倉, focus on 到期日 — two rows, no chain.
- **Fees (OPT-D6):** an option trade on a rule set with no `option_per_contract` is refused only when
  the **fee** is not supplied (tax may be blank — it is 0 for an option), issue kind
  `option_fee_unset`, 「此帳戶未設定選擇權手續費：請按 ✎ 覆寫填入對帳單手續費，或到 系統設定 → 費率 設定每口費率」 with a
  link; the preview's fee label for an option reads 「每口 0.65＋SEC（賣出）」 from a separate option label
  helper (`fee_rules_wire()["label"]` is unchanged). The fee settings panel lists
  「選擇權每口手續費」／「選擇權每口監管費」 for US rule sets (`option_per_contract` is nullable — unset is not
  0; `option_reg_per_contract` defaults to 0). The CSV and manual doors never write an instrument on a
  preview: a parseable OCC symbol resolves without writing (「將建立選擇權合約 …」) and
  `register_option_contract` runs only inside the commit's transaction.
- **What's new:** 「可以登錄美股選擇權了：賣出 Put、到期、被指派、合約調整都進帳本」, href `trades.html`, target
  `#m-sym-foot`, area 「交易帳本 → 交易輸入／儀表板 → 選擇權」.
- **AI input door:** an OCC symbol is code-shaped; AI-recognised option rows commit through the CSV
  door's path and register there; `resolve.py`'s register-first path meets `quick_register`'s entry
  refusal. `MARKET_CODE_PATTERNS["US"]` is unchanged.
- **Schwab converter** (pure; the CSV door registers): option rows stop being dropped. In
  `RowDetail.cells` — the CSV row the commit writes — an option row carries the **OCC symbol** and
  **share-equivalents** (`contracts_to_shares(Quantity)`); the display name and 「2 口（200 股當量）」 go to
  the report fields `RowDetail.symbol` / `RowDetail.shares` only. Price = the listed per-share premium
  (the reconciler's qty × price = Amount check still ties). Sell to Open → SELL `short_sale=1`; Buy to
  Close / Buy to Open → BUY; Sell to Close → SELL; fees verbatim, tax 0. `Expired` and TD's 「REMOVAL OF
  OPTION DUE TO EXPIRATION」 (re-mapped from `OPT_ADJUST`) → price-0 `expired`, dated the contract's
  expiry, side from the running signed position across every file of the request **plus
  `known_positions`** (OPT-D19: the ledger's signed share-equivalents per (account, contract) through
  the day before the request's earliest row, injected by `api/routers/broker_import.py`; an overlap
  with already-imported rows is caught at the CSV door), same-day rows per contract merged, written
  after that day's trades. Journal pairs → one `OPTION_ADJUST` with `ratio_to = |to| ÷ |from|` when a
  whole number and OPT-D11 holds (else `option_nonstandard_adjust`). 「TRANSFER OF SECURITY OR OPTION
  OUT」 / 「…OPTION IN」 between the broker's platforms → dropped with a reason. Option symbols are
  excluded from the converter's derived openings (the opening door refuses them). `UnmappedRow`
  carries `reason` ∈ {`option_assigned_unmapped`, `option_nonstandard_adjust`,
  `option_expiry_no_opening`} and the line; the broker-import route catches it around **both**
  `parse_export` and `convert(...)` and answers 422 with both keys; the page prints a per-reason
  remedy. The CLI `scripts/schwab_convert.py` passes `known_positions={}` (stated in its help).

## 9. Export doors

| Export | Change |
| --- | --- |
| Raw ledgers zip / per-tab CSV | `transactions.close_reason` rides `SELECT *`; the zip's `fee_rules_snapshot.json` carries the two new fee fields. `instruments` is not in the zip; an option's terms re-derive from its symbol |
| 持股 CSV / 持股報表 | stock rows as today; a 選擇權 block (§7.1) and the 「其中選擇權」 line only when `options` is present |
| 已實現損益 CSV | stock `realized.rows` + `options.realized.rows` (same columns, one per closing event); `asset_type`, `contract` appended |
| 稅務包 | option closes land in capital gains (`sale` / `short_cover`); contract name and a 「其中選擇權」 subtotal |
| 帳本報告 | 口數, §6.1 chips, refused closes marked; `OPTION_ADJUST` in the actions section |
| 現金收支明細 CSV / print | descriptions from `option_side` (the export computes `option_side_of` once) |
| 個股明細 CSV | works for a contract |
| 再平衡報告 | excludes options |

## 10. Verification

- **Invariant 1 (blast radius, D38).** A ledger with no option is byte-identical in every API payload
  and export **except these additive changes**, each re-pinned in the same commit wherever a test pins
  it: (1) the raw transactions dump / per-tab CSV gains `close_reason`; (2) the transaction import
  template gains `close_reason（選填）`; (3) the realized CSV gains `asset_type`, `contract`; (4) wire
  keys, null / false / `"stock"` on a stock ledger: cash statement rows `option_side`, `contracts`;
  `/api/ledgers/transactions` rows `close_reason`, `contracts`, `option_side`, `refused_close`;
  instrument elements of `/api/instruments` and `/api/input/context` `asset_type`;
  `/api/input/holdings` rows `asset_type`, `direction`, `contracts`; the manual preview
  `share_equivalents`, `contract_name`, `legs`; the corporate-action preview `strike_products`,
  `conserves`; the broker-import 422 body `reason`, `line`; the CSV import preview rows' `data`
  `contracts`, `contract_name`; the `/api/fee-rules` `fields` `option_per_contract`,
  `option_reg_per_contract`; (5) the raw zip's `fee_rules_snapshot.json` gains those two fields.
  **New routes** (compose endpoint, expiry inbox) add no key to an existing payload; the inbox count
  is 0 on a stock ledger. The dashboard payload — the golden — does not change at all.
- **Hermetic tests** per mechanism and rule (§4 incl. structural price-0, fee-free closes, derived
  side, option-mode refusals, the edit door on a close row, a back-dated trade against a stored
  `expired` row, refused closes with no position, the stranded-close list, two-leg rollback), per
  surface (stock-reader scan; §6.1 vocabulary guard; `option_side` identical on the ledgers wire, cash
  lines and exports; classifier labels agree with the replay's realized rows), the converter (every
  row shape, units vs Amount, `cells` commit through the CSV door, `known_positions`, every
  `UnmappedRow` reason incl. one raised inside `convert`), B − A with an open sold put (withheld per
  OPT-D20), KPI 總市值 = stock grand subtotal + `options.market_value`, the inbox (detector, skip,
  confirm, confirm-all) and e2e for the section, option-mode entry, the roll button, the inbox and the
  drawer footer. The e2e seed never dates an expiry on the DEF-056 sweep's seeded day.
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
here. (The expiry inbox is v1 — OPT-D13.)

- **Triggers:** multi-leg strategies, frequent rolls the owner wants analysed as chains, assignment
  volume, live option quotes, or a non-standard deliverable in the owner's history.
- **Roll chains:** link a buy-to-close and a same-day sell-to-open (v1 has the 「轉倉」 shortcut only).
- **Collateral:** reserve a sold put's strike × 100 against the cash pool.
- **Quotes and Greeks:** a provider path and mark-to-market history (would retire R3 and OPT-D20).
- **Non-standard deliverables:** a per-contract deliverable model — the point where a contract-terms
  table returns (OPT-D22) and OPT-D11 / D16's refusal retires.
- **Assignment linkage:** optionally fold the premium into the stock's cost (R5 declines for v1).
- **Option alerts:** pre-expiry reminders, assignment-risk alerts, option context in AI insights.
- **Converter:** per-row skip of unmapped option rows; an `Assigned` / `Exercise` mapping once a real
  row exists (OPT-D9).
- **Strategy views:** P&L by strategy, annualised return on collateral.
