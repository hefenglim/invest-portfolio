# Mini-Spec — Fee Engine v2 (2026-07-15)

Source spec: `docs/reference/broker-fee-schedules-2026-07.md` (owner-provided, verbatim).
Owner rulings (recorded, 2026-07-15):

- **FE-D1 (TW monthly rebate 折讓款):** the rebate NEVER enters cost basis, P&L, or any
  calculation of record. It is a FORECAST + PENDING-CONFIRMATION feature: the system
  computes the expected rebate per trade, surfaces it as (a) an informational hint at
  trade preview and in rebalance estimates, and (b) a pending-refund item in the inbox.
  Only when the actual refund arrives (next month) does the owner CONFIRM the pending
  item, which books a cash-pool credit (editable amount, prefilled with the estimate).
- **FE-D2 (MY stamp duty on US trades):** computed via MYR but BOOKED IN USD on the
  trade row — the transaction stays single-currency (USD). Snapshot records the FX rate
  and the MYR figure for audit.
- **FE-D3 (TW rounding):** 財政部規範 — ALL taxes, fees, regulatory charges, interest
  etc. are collected to the NT$1 only (角以下免收): **unconditional floor (ROUND_DOWN)**.
  Supersedes the previous 四捨五入 rule for TW fee/tax (rule files updated with this
  sign-off; recorded in CHANGELOG). Historical rows are NOT recomputed — per-row
  fee_rule_snapshot preserves the regime each row was booked under.

Out of scope (recorded, not implemented): options/bonds/futures/OTC/forex/mutual-fund
schedules and US fractional-share orders — the app trades whole-share stocks/ETF only.
REIT-specific stamp caps: not modeled (no REIT flag); ETF flag governs; limitation noted.

---

## Wave A — fee engine + config + rounding (backend)

### `data_ingestion/config_seed.py` — FeeRuleSet v2 fields (all Decimal, configurable)
- Common: `rounding` ("floor" | "half_up") per rule set.
- TW (`tw`): brokerage 0.001425, discount 1 (charge-first model), min_fee 20,
  tax_normal 0.003, tax_etf 0.001, tax_daytrade 0.0015, `rounding="floor"` (FE-D3),
  `rebate_rate 0.77` (informational only — used by the forecaster, never by compute_fees).
- Moomoo MY (`moomoo_my`): commission 0.0003 min 0.01; platform_fee 3.00;
  clearing 0.0003 cap 1000; sst_rate 0.08 (on commission+platform+clearing);
  stamp: step function `ceil(amount/1000) × 1.00`, cap 1000 (stock) / **ETF exempt**.
- Moomoo US (`moomoo_us`): commission 0.0003 min 0.01; platform_fee 0.99;
  settlement 0.003/share cap 1% of amount; cat 0.000003/share (both sides);
  SELL-only: sec_rate 0.0000206 min 0.01; taf 0.000195/share min 0.01 cap 9.79;
  MY stamp on US trades per FE-D2 (ceil(amount×USD/MYR /1000)×RM1, cap 1000 stock /
  200 ETF, converted back to USD 2dp; FX = latest stored USD/MYR on-or-before trade
  date; if no rate → stamp 0 + soft issue 「無 USD/MYR 匯率,印花稅未計」).
- Schwab (`schwab`): commission 0 (online default; broker_assisted_surcharge 25.00
  config, default off, no UI); SELL-only SEC + TAF as above.

### `data_ingestion/fees.py` — compute_fees v2
- TW: fee = max(floor(notional×brokerage×discount), min_fee) [floor BEFORE min, per
  群益 examples 142.5→142]; tax = floor(notional×rate). Integer NT$.
- MY: fee = round2(commission)+platform+round2(clearing)+round2(sst); tax = stamp
  (integer RM by construction, capped, ETF→0). Per-component quantize 2dp HALF_UP
  (documented assumption pending statement verification).
- Moomoo US: fee = Σ per-component (each quantized to cent HALF_UP after min/cap):
  commission, platform, settlement, cat, + (SELL: sec, taf); tax = stamp_usd (FE-D2).
- Schwab: fee = (SELL: sec+taf) (+assisted surcharge if configured); tax = 0.
- Snapshot: records EVERY component + rates + (for stamp) fx_rate + stamp_myr +
  `engine: "v2"`. Old rows keep their v1 snapshots (arbitration per row).
- compute_fees signature gains what it needs (shares already available; conn NOT
  added — FX for stamp is resolved by the caller seam and passed in, keeping fees.py
  pure; manual.py/csv_import.py resolve the rate like they resolve is_etf).

### Rule-file + manual updates (same wave, same sign-off)
- `.claude/rules/markets-and-fees.md`: TW rounding 四捨五入 → 無條件捨去 (財政部,
  owner 2026-07-15); TW rebate model note (charge-first, refund-later — FORECAST ONLY);
  complete-schedule pointer; per-account skeletons updated to v2 shapes.
- `.claude/rules/data-and-pricing.md`: the "fees/tax 四捨五入 to integer" line updated.
- `docs/accounting-formula-manual.md` → v1.3: §3 rewritten to the v2 engine (formulas +
  per-component rounding + FE-D2 stamp mechanics + snapshot regime clause), new §3.x
  折讓款預估 (forecast formula floor(fee×rebate_rate); NOT money of record; pending-
  confirm flow → cash movement kind rebate), §12.5 gains the forecast as class-B;
  regenerate `accounting-formula-manual.en.md` (same change set); CHANGELOG entry at
  ship records the rounding-rule change + rate updates.

### Tests (Wave A)
- Rewrite fee worked examples to v2 (incl. the 群益 walk: buy 1000×100 → fee 142;
  sell 1000×110 → fee 156, tax 330; Moomoo MY RM examples w/ SST 8% + stamp steps +
  ETF stamp exemption; Moomoo US sell w/ SEC/TAF mins+caps; settlement cap; stamp
  USD conversion + missing-FX degrade; Schwab sell-only fees).
- Update ALL tests that encode v1 rates (worked examples, golden payloads, spec17,
  input previews, stress-consistency) — semantic updates, never weakened assertions.
- Extend `scripts/stress_audit/oracle.py` + phase-1 scenario to the v2 formulas
  (accumulation rule ②) and re-run phase 1 green. The oracle derives its logic from
  THIS spec + the reference doc, not from fees.py.

## Wave B — rebate forecaster + inbox + hints (frontend + service)

- `api/rebates.py` (compute-on-read, mirrors dividend_inbox pattern — NO state table
  except a skip table `rebate_skips(month_key, account_id)`): for each account whose
  rule set has rebate_rate>0, group that account's transactions by calendar month;
  expected = Σ floor(fee × rebate_rate) per trade; a month becomes PENDING on the 1st
  of the following month; suppressed when a matching cash movement (kind='rebate',
  same account+month tag) exists or the month is skipped.
- Cash: new movement kind `rebate` (退款/折讓) — deposit-like credit in cash_balances +
  statement (label 折讓款); note auto-filled 「YYYY-MM 折讓款」; amount editable at
  confirm (prefilled with estimate; actual wins — the estimate is never money of record).
- UI: `dividend-inbox.html` page becomes 「收件匣」 (nav label rename) with two
  sections: 配息/配股偵測 (existing) + 待確認退款(折讓款) — per-month rows: account,
  month, trade count, estimated amount, 確認入帳 (→ POST /api/rebates/confirm
  {account_id, month, amount}) / 略過 / 取消略過; badge counts both sections.
- Hints (informational only, clearly labeled 不計入成本):
  - manual TW trade preview: 「預估次月折讓 +X」 line under the fee field;
  - rebalance drawer + execution report: TW legs footnote Σ estimated rebate.
- whatsnew CATALOG (next version) entries: fee engine v2 (調整), 折讓款預告與確認 (新增,
  href dividend-inbox.html), with targets.
- Tests: unit (forecast math incl. floor, month grouping, suppression/skip/unskip),
  contract (endpoints + guest consistency with inbox siblings), e2e (inbox two-section
  render + confirm→cash movement→resurface-on-delete), cash statement label.

Gates per wave: targeted pytest + ruff + mypy --strict clean; full suite + /stress-audit
phase 1 at the end; manual/en-mirror regenerated in the same change set (ship item 9).
