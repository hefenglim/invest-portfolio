# Rule: Markets & Fee / Tax Rule Sets

Market trading rules (lot, tick) are fixed by the exchange. Fee/tax **numbers** are
configurable per account and **must be confirmed in the data-source probe (Q11)** —
the structures below are the schema; exact rates are filled/verified at setup.

> **Fee-engine v2 (2026-07-15, owner sign-off).** The owner supplied the complete,
> authoritative broker schedules in `docs/reference/broker-fee-schedules-2026-07.md`;
> the engine (`data_ingestion/fees.py` + `config_seed.py::FEE_RULES`) implements them.
> Rates that change over time (US SEC/TAF, commission, stamp) live in config, never
> hard-coded. Per-row `fee_rule_snapshot` records the regime each row was booked under,
> so historical rows are NOT recomputed when rates change. The v2 skeletons below
> supersede the earlier placeholders.

## Market trading rules

### TW — TWSE / TPEx
- **Lot:** 1 張 = 1,000 股. Record the unit as **shares (股)**. Odd lot (零股) = integer
  shares.
- **Price tick (股票):** <10 → 0.01 · 10–50 → 0.05 · 50–100 → 0.1 · 100–500 → 0.5 ·
  500–1000 → 1 · ≥1000 → 5.
- **Securities transaction tax (sell side):** 現股 0.3% · 當沖 0.15% · ETF 0.1%.
  Buy side: none.
  ⚠ **The ETF flag is REGISTRY-AUTHORITATIVE and now THREE-STATE — never guessed** (AI-D40,
  owner ruling 2026-08-24). The stress-audit fix of 2026-07-15 made a registered instrument's
  `is_etf` win over the input flag at both fee seams (`manual.py`, `csv_import.py`), but left
  the thing that **seeds** the registry defaulting to `False`: `quick_register`'s auto-register
  path (`api/routers/input_center.py`) never passed the flag at all, so a TW ETF entered on its
  first manual trade was taxed at 現股 **0.3%** instead of **0.1%** — 3× — and `tax_rate: 0.003`
  was written into `fee_rule_snapshot` as the authority. The three states are `True` / `False` /
  **`None`** **at the INPUT** (`quick_register(is_etf: bool | None)`, `UpdateBody.is_etf`,
  `ManualBody.new_symbol_is_etf`); **at rest they are TWO `NOT NULL` booleans** —
  `instruments.is_etf` + `instruments.etf_flag_unknown` (`data_ingestion/schema.py`: SQLite
  cannot relax `NOT NULL` without a table rebuild, so "unknown" rides alongside as an additive
  column). `None` at the door is stored as `is_etf=0, etf_flag_unknown=1`; an explicit answer
  stores `etf_flag_unknown=0`. **Query the unknowns with `etf_flag_unknown=1`, never
  `is_etf IS NULL`** (that column is never NULL — the query returns 0 rows on every database).
  Auto-registration lands unknown — both the manual-trade path (`input_center.py`) and the
  SPINOFF child (`register.py::spinoff_child_draft`, NEW-21 2026-09-06) — and the fee engine
  meeting `unknown` **on a TW SELL** computes with `False` but raises a soft issue
  `etf_flag_unknown`「無法判定是否為 ETF，賣出稅率待確認」(`fees.py::resolve_etf_flag` returns the
  `(is_etf, unknown)` pair). A BUY carries no TW tax, so it stays silent. This is the same red
  line as "a stale price is labelled, never guessed": **an unknown rate is disclosed, never
  defaulted in silence.** Existing rows were migrated in as `etf_flag_unknown=0` (KNOWN) and are
  never rewritten — only future auto-registrations land unknown.
  ⚠ **當沖 OUTRANKS ETF — settled, not incidental (QA-19, owner ruling 2026-09-01).** A TW sell
  that is both a same-day round trip AND an ETF resolves to the **當沖 0.15%**, never the ETF
  0.1%. `fees.py::_tw` already reads `tax_daytrade if daytrade else tax_etf if is_etf else
  tax_normal`, so this rule records the precedence rather than changing it — and it is recorded
  precisely because **no repository artefact stated the real-world answer**, which made the
  engine's ordering an accident that any future edit could reverse in good faith. A rate that
  only exists as the order of a ternary is not a decision, it is a coincidence.
  Consequence for every preview surface: `strategy/whatif.py` has no 當沖 input to read, so the
  drawer **discloses the assumption** (`daytrade_note`) instead of presenting the non-daytrade
  rate as the only answer — the same posture as `etf_flag_unknown`, and required by
  `domain-ledger.md`'s rule that a surface which cannot know which branch applies must say so.
- **Brokerage fee:** 0.1425% (configurable) + discount rate + **min NT$20**. Fee and
  tax are collected to integer NT$ by **無條件捨去 (unconditional floor, ROUND_DOWN)** —
  財政部 rule 角以下免收 (**owner sign-off 2026-07-15, FE-D3**; supersedes the earlier
  四捨五入). The min-NT$20 floor is applied **after** the floor (群益 example:
  100,000×0.1425% = 142.5 → floor 142; 5.5 → floor 5 → min 20).
- **群益 charge-first (先收後退) rebate model (FE-D1):** the account is charged the FULL
  0.1425% at settlement and receives a **77% rebate (2.3折)** next month. The rebate is
  **FORECAST-ONLY** — it NEVER enters cost basis, P&L, or `compute_fees`; it is surfaced
  as a preview hint + a pending-confirmation inbox item, booked as a cash credit only when
  the owner confirms the actual refund (`rebate_rate` lives in config for the forecaster).
- ⚠ **`discount` and `rebate_rate` express the SAME broker benefit two different ways** —
  charge less now, or charge full and refund later. Setting `discount < 1` **and**
  `rebate_rate > 0` applies it twice, and since fees are part of `original_total` that
  quietly understates cost basis (measured on the test site 2026-07-30: a full commission of
  869 was charged as 199 and still forecast a 153 refund → net 46 instead of 200). The two
  coherent configurations are **charge-first** (`discount=1`, `rebate_rate=0.77` — the seed
  default, 群益) or **discount-at-settlement** (`discount<1`, `rebate_rate=0`). The fee-rule
  API reports this as a **non-blocking** `conflicts` entry with a plain-language explanation
  and one-click resolutions: a broker really could do both, so it informs, never blocks.

### US — NYSE / NASDAQ
- **Lot:** 1 share (fractional shares deferred).
- **Price:** 2 decimals (for ≥ US$1).
- **Regulatory fees (SELL-only, configurable — SEC/FINRA adjust annually):**
  **SEC** = max(rate × notional, $0.01), rate ≈ `0.0000206`; **TAF** = min(max(per-share ×
  shares, $0.01), **$9.79**), per-share ≈ `0.000195`. **CAT** = per-share × shares (both
  sides). Each component is cent-quantized (ROUND_HALF_UP) then summed. No US transaction tax.
- **Dividend:** 30% US withholding (W-8BEN), applies to both Schwab and Moomoo US.

### MY — Bursa Malaysia (verified at bursamalaysia.com)
- **Lot:** board lot = **100 units**; odd lot = 1–99 units.
- **Share price tick:** < RM1 → **0.005** · RM1–9.99 → 0.01 · RM10–99.98 → 0.02 ·
  ≥ RM100 → 0.10.
- **ETF price tick:** < RM1 → **0.001** · RM1–2.995 → 0.005 · ≥ RM3 → 0.01.
- ⚠️ MY prices therefore need **up to 3 decimal places** (sub-RM1 shares at 0.005,
  ETFs at 0.001). See the precision rule in `data-and-pricing.md` — do **not** truncate
  MY prices to 2 dp.
- **Fees (Moomoo MY, fee-engine v2):** **commission** = max(0.03% × amount, RM0.01) +
  **platform fee RM3.00**/order + **clearing** = min(0.03% × amount, **RM1,000**) +
  **SST 8%** on (commission + platform + clearing). Each component cent-quantized then summed.
- **Stamp duty (tax):** step function `ceil(amount / 1,000) × RM1.00`, cap **RM1,000**
  (ordinary stock) — **ETF is exempt (RM0)**; REITs/warrants cap RM200 (REITs not modeled —
  no REIT flag; the ETF flag governs).
- **US trades on Moomoo (US market, FE-D2):** the MY stamp still applies, computed in MYR
  from the USD notional × trade-date USD/MYR rate (`ceil(amount_usd × fx / 1,000) × RM1`,
  cap RM1,000 stock / RM200 ETF) then **booked back in USD** on the single-currency row; the
  snapshot records the FX rate + the MYR figure. No stored USD/MYR rate → stamp 0 + a soft
  issue 「無 USD/MYR 匯率,印花稅未計」.
- **Dividend:** cash; Malaysian single-tier system. Record net received (Q10); confirm
  any high-income dividend surtax in the probe.

## Per-account fee rule sets (configurable; bind to ACCOUNT, not market)

| Account | Market | Settle ccy | Funding ccy | Fee/tax skeleton (fee-engine v2) |
| --- | --- | --- | --- | --- |
| TW broker | TW | TWD | TWD | fee = max(floor(0.1425% × notional × discount), NT$20); sell tax floor(0.3% / 0.15% / 0.1%); **floor** rounding; rebate 77% forecast-only |
| Charles Schwab (Intl) | US | USD | TWD | commission $0 (online); SELL: SEC + TAF; broker-assisted $25 config, off |
| Moomoo MY (US) | US | USD | MYR | commission max(0.03%,$0.01) + platform $0.99 + settlement min($0.003/sh, 1%×amt) + CAT + (SELL SEC+TAF); MY stamp booked USD (FE-D2) |
| Moomoo MY (MY) | MY | MYR | MYR | commission max(0.03%,RM0.01) + platform RM3 + clearing min(0.03%,RM1,000) + SST 8%; stamp ceil(amt/1,000)×RM1 cap RM1,000, ETF exempt |

The US market spans **Schwab** and **Moomoo MY** with different cost structures —
this is exactly why fee rules are per-account. Each account references one rule set;
rates live in **config** (`config_seed.py::FEE_RULES`) and are versioned (a rate change is a
config change, recorded in `CHANGELOG.md`); the complete authoritative schedules are in
`docs/reference/broker-fee-schedules-2026-07.md`.
