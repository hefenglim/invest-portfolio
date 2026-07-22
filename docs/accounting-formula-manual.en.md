# Portfolio Accounting-Formula Manual (English Mirror)

> **⚠️ AUTHORITY NOTICE — read first.** The Traditional-Chinese file
> `docs/accounting-formula-manual.md` is the **ARBITRATION AUTHORITY** (the single
> source of truth for any money dispute). **This English file is a working MIRROR** —
> a faithful translation kept for efficient AI/agent consumption only; it holds **no
> independent authority**. Whenever the zh manual changes, **this mirror MUST be
> regenerated in the same change set** (see `.claude/skills/ship-version/SKILL.md`
> item 9). If the two files ever diverge, the zh manual wins and this mirror is the
> defect. Verification-anchor / `scope` strings and code identifiers are preserved
> **byte-identical** with the zh manual (they are machine identifiers); formula bodies
> are reproduced verbatim; only prose is translated.

> **Version**: `v1.4` (2026-07-22)
> **Code baseline**: `v0.1.20 + Batch B (Moomoo merge)`
> **Arbitration status**: **Formally signed off by the owner (2026-07-15)**; effective
> as the site's single **arbitration standard** for any money dispute **from version
> v0.1.19 onward**.
> **Language exception**: the zh manual uses **Traditional-Chinese prose + English
> technical identifiers** (column / table / function names) as an owner-facing
> arbitration document — a deliberate, flagged exception to the repo's "all artifacts in
> English" rule. **The zh manual is the arbitration authority**; this English mirror
> `docs/accounting-formula-manual.en.md` exists for efficient AI/agent reading and MUST
> be regenerated in the same change set whenever the zh manual changes.
> **Engineering source**: the English rule files under `.claude/rules/`
> (`domain-ledger.md`, `markets-and-fees.md`, `data-and-pricing.md` …) remain the
> **engineering source of record** from which this manual was compiled; where this
> manual, the code, and the rule files disagree, the manual's flagged "verified" numbers
> and the code they cite govern, and the conflict is reported.
>
> **Verification basis**: every numeric worked example in this manual is drawn from — or
> reconciled against — the **post-merge (Batch B) live stress run**: a set of **1,060
> adversarial reconciliation assertions** (`scripts/stress_audit/evidence/oplog.jsonl` +
> `scripts/stress_audit/evidence/assertions.jsonl`; phase-1 `--ui` run of **66 ops,
> 1,060/1,060 passing, 0 fail**). Each numeric example is tagged with its `scope`
> verification anchor; scenario-dependent terminal values also carry their phase
> (`phase1:final` etc.). The manual author fabricated no numbers. **Note**: the stress
> scenario evolves per release (the post-merge scenario differs from the v1.3-basis 966
> run), so this version has re-reconciled every anchor against the current run above (see
> §12.3 v1.4).

---

## Table of Contents

1. [General Principles & Precision Rules](#1-general-principles--precision-rules)
2. [Account / Market / Currency Model](#2-account--market--currency-model)
3. [Fee & Transaction-Tax Formulas](#3-fee--transaction-tax-formulas)
4. [Cost Basis (Weighted Average)](#4-cost-basis-weighted-average)
5. [Realized / Unrealized P&L](#5-realized--unrealized-pl)
6. [The Three Dividend Models](#6-the-three-dividend-models)
7. [Total Return & Return Rates (incl. XIRR)](#7-total-return--return-rates-incl-xirr)
8. [FX Gain/Loss (FX P&L)](#8-fx-gainloss-fx-pl)
9. [Cash Pools & Running Statement](#9-cash-pools--running-statement)
10. [Corrections, Audit & Rebuild](#10-corrections-audit--rebuild)
11. [Rebalance Simulation](#11-rebalance-simulation)
12. [Appendix](#12-appendix)

---

## 1. General Principles & Precision Rules

### 1.1 Arbitration Clause

If any amount displayed on the site is disputed, **replay the four permanent ledgers
line by line through the formulas of the corresponding chapter** (replay / 重算); the
replayed result is the ruling value. No UI display, cache, or verbal recollection may
override a ledger replay. The ruling procedure is in
[§12.4 How to arbitrate](#124-how-to-arbitrate-a-disputed-amount).

### 1.2 Core Invariants (violating any is a bug, not a choice)

| # | Invariant | Source |
| --- | --- | --- |
| I1 | **Never use `float` for money**: price, quantity, rate, amount are `Decimal` end to end. | `shared/money.py` |
| I2 | **`original_total` (original cost) is never overwritten**; all reports rebuild from the ledgers. | `domain-ledger.md` |
| I3 | **Quote numbers come from finance APIs, never from an LLM.** | `data-and-pricing.md` |
| I4 | **Dividends enter total return exactly once** (via cost adjustment, not a separate income line). | §6 |
| I5 | **FX gain/loss is a *decomposition* of the reporting-currency total return, never added on top.** | §8 |
| I6 | **Fees / tax / dividend rules bind to the (account, market) pair**, not the "market" alone; a single-market account degenerates to the old statement (equivalent to binding to the account). The account row's scalar columns (`fee_rule_set` / `dividend_model` / `settlement_ccy`) remain as a documented fallback. | §2, §3 |
| I7 | **Average cost is always computed on read; a rounded average is never stored as the authoritative value.** | §4 |

### 1.3 Precision Model (non-negotiable)

**Storage precision (must not truncate)**

| Kind | Storage precision | Cap at the write seam | Implementation |
| --- | --- | --- | --- |
| Trade price `price` | Market finest tick (US/TW 2 dp, **MY up to 3 dp**) | **4 dp**, `ROUND_HALF_UP`, **cap-not-pad** | `data_ingestion/store.py::_cap_price` (`_PRICE_DP=4`) |
| Quote price `prices.close` (OHLC) | same | **4 dp**, same (the sole price write seam) | `pricing/store.py::_cap_dp` (`_PRICE_DP=4`) |
| FX rate `fx_rates.rate` | High precision (4–6 dp; a rate is not money, the 2-dp rule does not apply) | **6 dp**, `ROUND_HALF_UP`, cap-not-pad | `pricing/store.py::_cap_dp` (`_FX_DP=6`) |
| Average cost | **not stored**; store `total_cost` + `shares`, divide on read (see §4) | — | `portfolio/cost_basis.py` |

> **"cap-not-pad"**: clean values (e.g. `130`, `9.50`) are stored byte-identical; only
> the float-noise tail (e.g. `305.364990234375`) is capped to 4 dp. This **removes
> representation noise, not information**.

**Amount precision (per-currency minor unit, applied at settlement / display)**

| Currency | minor unit | dp | Definition |
| --- | --- | --- | --- |
| `TWD` | integer NT$ | **0 dp** | fee / tax rounded to integer NT$ |
| `USD` | cent | **2 dp** | — |
| `MYR` | sen | **2 dp** | — |

Implementation: `shared/money.py::MINOR_UNITS = {TWD:0, USD:2, MYR:2}`.

**The only moment quantization occurs**: **settlement / display**, via
`shared/money.py::quantize_amount(value, currency, ROUND_HALF_UP)`. Prices and FX rates
are **not** quantized here (they keep full precision). All currency conversion goes
through the single helper `shared/fx.py::convert(amount, rate)` (`rate` defined as "1
unit of source currency = rate units of target currency"); no module may scatter its own
multiply-by-rate.

**Persistence format**: `Decimal` is stored as a **canonical fixed-point string (TEXT)**
(`money.py::to_db` / `from_db`), rejecting `float` and non-finite values (NaN / Inf), and
guaranteeing lossless round-trip `from_db(to_db(x)) == x`.

### 1.4 Rebuild Principle (Rebuild / 重算)

Four **permanent sources of truth**: `opening_inventory`, `transactions`, `dividends`,
`fx_conversions`. **All** derived numbers (holdings, cost, realized / unrealized, returns,
FX P&L, cash balances) are computed on read by **replaying these four in date order**;
"computed results" are never treated as the source of truth (cache them only if profiling
shows a need). Arbitration always uses the replay.

> **Implementation**: `shared/money.py`, `shared/fx.py`, `data_ingestion/store.py`,
> `pricing/store.py`, `portfolio/cost_basis.py`.
> **Basis**: `.claude/rules/data-and-pricing.md` (Money & numeric precision model),
> `CLAUDE.md` (Core invariants).

---

## 2. Account / Market / Currency Model

Three orthogonal dimensions: **market** (where it trades) · **account** (which broker
holds it) · **currency** (the instrument's quote currency). **The same market can span
multiple accounts, and the same account can span multiple markets, with different rules**,
so fee / tax / dividend rules bind to the **(account, market) pair** (invariant I6).

| `account_id` | Name | Market | Settlement ccy `settlement_ccy` | Funding ccy `funding_ccy` | Dividend model `dividend_model` | Fee rule set `fee_rule_set` |
| --- | --- | --- | --- | --- | --- | --- |
| `tw_broker` | TW Broker | TW | TWD | TWD | `cash_cost_reduction` (cash → cost reduction) | `tw` |
| `schwab` | Charles Schwab | US | USD | **TWD** | `drip_us` (DRIP, 30% withholding) | `schwab` |
| `moomoo_my` | Moomoo MY | **US + MY** | USD (US leg) / MYR (MY leg) | **MYR** | US=`drip_us` (DRIP, 30% withholding) / MY=`cash` (single-tier net) | US=`moomoo_us` / MY=`moomoo_my` (bound per (account, market)) |

> **Batch B merge (2026-07-21)**: the **two former per-market Moomoo accounts (one
> US-settled, one MY-settled; their legacy account ids are documented in
> `data_ingestion/moomoo_merge.py`) are merged into ONE dual-market account `moomoo_my`**.
> Each market's rules are held as an explicit binding in `account_market_rules`
> (US → (`moomoo_us`, `drip_us`), MY → (`moomoo_my`, `cash`)); the account row's **scalar
> columns** (`settlement_ccy=USD` / `fee_rule_set=moomoo_us` / `dividend_model=drip_us`) pin
> the US pair as a **fallback for single-market accounts with no binding** (`tw_broker` /
> `schwab` take this fallback, equivalent to the old "bind to account" statement).

Key points:

- **The US market spans `schwab` and `moomoo_my` (the latter's US market leg) with
  different cost structures** → exactly why fee / tax / dividend rules bind to the
  **(account, market)** pair (not the market alone).
- **Moomoo MY is one brokerage account (`moomoo_my`) spanning two markets**: the US market
  leg holds USD-settled US stocks (funded via MYR→USD conversion), and the MY market leg
  holds MYR-settled MY stocks. The two markets carry different fee / tax / dividend rules,
  hence the (account, market) binding. **The MYR cash pool is a single shared
  `(moomoo_my, MYR)` operational pool across the two market legs** (see §9); the USD
  exposure is `moomoo_my`'s USD FX pool, anchored in MYR (see §8).
- A transaction row carries `account_id` + `symbol`; the `instruments` table knows that
  symbol's `market` and `quote_ccy` (the market is fixed by the symbol, so post-merge the
  fee/tax worked examples' `scope` anchors are written as `moomoo_my/<symbol>`, with the
  market carried by the symbol).
- The FX pool's **home currency = the account's `funding_ccy`**: the Schwab USD pool is
  anchored in **TWD**, the `moomoo_my` USD pool is anchored in **MYR** (see §8).

> **Implementation**: `data_ingestion/config_seed.py::DEFAULT_ACCOUNTS` (incl.
> `MarketBinding` per-market bindings), `data_ingestion/moomoo_merge.py` (Batch B one-time
> merge, 2026-07-21), table `account_market_rules`,
> `shared/models/assets.py` (`Account` / `Instrument`, incl. `is_etf`).
> **Basis**: `.claude/rules/domain-ledger.md` (Accounts), `.claude/rules/markets-and-fees.md`.

---

## 3. Fee & Transaction-Tax Formulas (fee-engine **v2**, 2026-07-15)

**Single implementation**: `data_ingestion/fees.py::compute_fees(rules, side, quantity, price, *, is_etf, daytrade, stamp_fx)`.
`notional = quantity × price`. Returns `FeeResult{fee, tax, snapshot}`, where **`snapshot`
is the rate + per-component snapshot used for that row** (incl. `engine="v2"`), persisted per
row in `transactions.fee_rule_snapshot`, so a later rule change can still reproduce history
(an extension of invariant I2).

**Rate source**: the owner's complete schedules `docs/reference/broker-fee-schedules-2026-07.md`
(authoritative), carried in `config_seed.py::FEE_RULES` as **config**; **rates that adjust over
time (US SEC/TAF, commission, stamp) live in config, never hard-coded** (reference §肆.1).

**Rounding (per rule set)**:
- **TW (`rounding="floor"`)**: both fee and tax are **floored (ROUND_DOWN) to integer NT$**
  (財政部 FE-D3); the min-NT$20 floor is compared **after** the floor.
- **US / MY (`rounding="half_up"`)**: **each fee component** is quantized to 2 dp
  (ROUND_HALF_UP) then summed (per-component rounding is a documented assumption, pending
  statement verification).

**Per-row regime clause**: fee-engine-v2 is a **per-row regime** — old rows keep their v1
snapshot and are arbitrated under the old regime; new rows carry the v2 snapshot and are
arbitrated under v2. Historical rows are **never recomputed** (see the fee-dispute note in
§12.4). `stamp_fx` (FE-D2) is resolved by the caller and passed into the pure `compute_fees`
(`fees.py` stays pure and never touches `conn`).

### 3.1 TW (`tw_broker` → rule set `tw`, `market = TW`, `rounding = "floor"`)

$$\text{fee} = \max\Big(\big\lfloor\text{brokerage}\times\text{discount}\times\text{notional}\big\rfloor,\ \text{min\_fee}\Big),\quad \text{買賣皆有}$$

$$\text{tax} = \big\lfloor\text{rate}\times\text{notional}\big\rfloor,\quad \text{僅賣方}$$

The sell-side tax rate is determined in order:

$$\text{rate} = \begin{cases} \text{tax\_daytrade} = 0.0015 & \text{當沖 } daytrade=\text{True}\\ \text{tax\_etf} = 0.001 & is\_etf=\text{True}\\ \text{tax\_normal} = 0.003 & \text{現股（預設）}\end{cases}$$

Seed values: `brokerage = 0.001425`, `discount = 1` (charge-first: full price at settlement,
77% refunded next month, see §3.6), `min_fee = 20` (NT$), `rebate_rate = 0.77` (FORECAST-ONLY,
never used by `compute_fees`). `rounding="floor"` → both fee and tax are **floored (ROUND_DOWN)
to integer NT$** (FE-D3); the **min NT$20 is compared after the floor** (群益 142.5→floor 142;
5.5→floor 5→min 20). Buy-side `tax = 0`.

- **`is_etf` source**: the instrument **registry** (`instruments.is_etf`, the sole source
  of truth, **never derived from sector**).
- **`daytrade`**: a **per-row flag**, written and **persisted in `transactions.daytrade`**,
  so a rebuild reproduces the day-trade tax rate (see §10).

**Verified examples** (anchor: fee-engine v2 stress phase1, 2026-07-15, `fee_engine.*` 80/80 pass)

| Scenario | notional | fee | tax | Verification anchor (`scope`) |
| --- | ---: | ---: | ---: | --- |
| 2330 buy 1,000@600 | 600,000 | `max(⌊855.0⌋, 20)=` **855** | 0 | `fee_engine.fee/tax tw_broker/2330 buy 1000@600` |
| 2330 sell 300@700 (cash equity) | 210,000 | ⌊299.25⌋=**299** | ⌊0.003×210,000⌋=**630** | `fee_engine.fee/tax tw_broker/2330 sell 300@700` |
| 0050 buy 1,000@1.15 (**min applies**) | 1,150 | ⌊1.6…⌋=1→**20** | 0 | cf. 群益 min case |
| 2330 sell 100@725 (**day-trade**) | 72,500 | ⌊103.3…⌋=**103** | ⌊0.0015×72,500⌋=**108** | `fee_engine.fee/tax tw_broker/2330 sell 100@725 [daytrade]` |

> Rounding-direction comparison (**v2 vs v1**): 0050 sell 50@140 with `daytrade=True` gives
> tax = ⌊0.0015×7,000⌋ = ⌊10.5⌋ = **10** (v1's ROUND_HALF_UP gave 11) — this is the effect of
> FE-D3 switching from round-half-up to unconditional floor.

### 3.2 US — Schwab (rule set `schwab`, `market = US`)

Listed-stock online orders are **$0 commission**; **sell-only** adds SEC + TAF regulatory
fees (adjusted annually, kept in config):

$$\text{fee} = \big[\,\text{SELL}\,\big]\cdot\Big(\underbrace{\max(\text{sec\_rate}\times\text{notional},\ 0.01)}_{\text{SEC}} + \underbrace{\min\big(\max(\text{taf\_per\_share}\times\text{shares},\ 0.01),\ 9.79\big)}_{\text{TAF}}\Big)$$

$$\text{tax} = 0.00 \quad(\text{美股無交易稅})$$

Seed values: `sec_rate = 0.0000206` (min $0.01), `taf_per_share = 0.000195` (min $0.01, cap
**$9.79**). `broker_assisted_surcharge = 25.00` is config (**default off**, no channel flag, so
never applied). Each component is quantized to the **cent (2 dp, ROUND_HALF_UP)** then summed.

**Verified examples**

| Scenario | fee | tax | Verification anchor |
| --- | ---: | ---: | --- |
| AAPL buy 100@180 | **0.00** (no buy-side fee) | 0.00 | unit `test_schwab_buy_zero` |
| sell 100@300 (notional 30,000) | SEC ⌈0.618⌉→0.62 + TAF 0.02 = **0.64** | 0.00 | unit `test_schwab_sell_sec_taf` |
| sell 100,000@10 (**TAF cap**) | SEC 20.60 + TAF **9.79** = **30.39** | 0.00 | unit `test_schwab_sell_taf_cap` |

### 3.3 US — Moomoo (rule set `moomoo_us`, `market = US`)

$$\text{fee} = \underbrace{\max(\text{comm\_rate}\times n,\ 0.01)}_{\text{commission}} + \underbrace{0.99}_{\text{platform}} + \underbrace{\min(0.003\times\text{shares},\ 0.01\times n)}_{\text{settlement}} + \underbrace{0.000003\times\text{shares}}_{\text{CAT}} + \big[\text{SELL}\big]\cdot(\text{SEC}+\text{TAF})$$

where SEC / TAF are as in §3.2; $n=\text{notional}$ (USD). Each component is cent-quantized
then summed.

**MY stamp duty (tax, FE-D2)**: the stamp on a US trade is computed in MYR and booked in USD:

$$\text{stamp\_myr} = \min\!\Big(\big\lceil (n\times\text{fx}) / 1000\big\rceil\times 1,\ \text{cap}\Big),\quad \text{cap}=\begin{cases}200 & \text{ETF}\\ 1000 & \text{stock}\end{cases}$$

$$\text{tax} = \text{round}_{2}\big(\text{stamp\_myr} / \text{fx}\big),\quad \text{fx}=\text{trade-date USD/MYR (on-or-before)}$$

`fx` is resolved by the caller (manual/CSV/edit/rebalance/whatif) and passed in; **no rate →
stamp 0** + a soft issue 「無 USD/MYR 匯率,印花稅未計」. The snapshot records `stamp_fx_rate`
and `stamp_myr`. Seed values: `commission_rate = 0.0003` (min 0.01), `platform_fee = 0.99`,
`settlement_per_share = 0.003` (cap 1%×n), `cat_per_share = 0.000003`.

**Verified examples (fx = 4.3; the stress phase1 on-or-before USD/MYR)**

| Scenario | fee breakdown | fee | tax (stamp, in USD) | Verification anchor |
| --- | --- | ---: | ---: | --- |
| NVDA buy 30@500 | 4.50+0.99+0.09+0.00 | **5.58** | ⌈64,500/1000⌉=65 → 65/4.3=**15.12** | `fee_engine.fee/tax moomoo_my/NVDA buy 30@500` |
| NVDA sell 25@600 | 4.50+0.99+0.08+0.00+SEC0.31+TAF0.01 | **5.89** | 65/4.3=**15.12** | `fee_engine.fee/tax moomoo_my/NVDA sell 25@600` |
| buy 1,000@0.10 (**settlement cap**) | 0.03+0.99+min(3.00,1.00)+0.00 | **2.02** | — | unit `test_moomoo_us_settlement_cap` |

### 3.4 MY (account `moomoo_my`'s MY market leg → rule set `moomoo_my`, `market = MY`, native MYR)

$$\text{comm} = \max(0.0003\times n,\ 0.01),\quad \text{clearing} = \min(0.0003\times n,\ 1000)$$

$$\text{sst} = 0.08\times(\text{comm}+\text{platform}+\text{clearing}),\quad \text{platform}=3.00$$

$$\boxed{\text{fee} = \text{comm} + \text{platform} + \text{clearing} + \text{sst}}\qquad \boxed{\text{tax} = \min\!\big(\lceil n/1000\rceil\times 1,\ \text{cap}\big)}$$

Stamp cap: **ordinary stock RM1,000**; **ETF exempt (cap = 0 → tax 0)**; REITs/warrants RM200
(**no REIT flag modeled** — the ETF flag governs; limitation noted). Each component is quantized
to the **cent (2 dp)**; SST is computed on the quantized comm/platform/clearing (a documented
assumption).

> **Important booking convention**: this app records **stamp duty in the `tax` column**
> and **comm + platform + clearing + SST in the `fee` column**. This is an MY-specific column
> mapping; be sure to distinguish it when arbitrating MY trade costs.

**Verified examples**

| Scenario | fee breakdown | fee | tax (stamp) | Verification anchor |
| --- | --- | ---: | ---: | --- |
| 1155 buy 1,000@9.50 | 2.85+3.00+2.85+0.70 | **9.40** | ⌈9,500/1000⌉=10 → **10.00** | `fee_engine.fee/tax moomoo_my/1155 buy 1000@9.50` |
| 1155 sell 400@11.00 | 1.32+3.00+1.32+0.45 | **6.09** | ⌈4,400/1000⌉=5 → **5.00** | `fee_engine.fee/tax moomoo_my/1155 sell 400@11.00` |
| **0800EA buy 1,000@1.15 (ETF)** | 0.35+3.00+0.35+0.30 | **4.00** | **0.00 (ETF exempt)** | `fee_engine.fee/tax moomoo_my/0800EA buy 1000@1.15 [etf]` |

### 3.5 Overrides, Coexisting Regimes & Rate Governance (fee-engine v2 is live)

- **Manual override**: on input / edit the user may explicitly overwrite `fee` / `tax`;
  the system then uses the override value and marks `override: true` in `snapshot`
  (see §10's `_recompute_edit_fees`).
- **User-adjustable rates (FU-D1, overlay)**: each rule set's rates / tax rates / rounding mode
  can be adjusted under Settings → Accounts & Fees, backed by a DB overlay
  (`data_ingestion/fee_overrides.py`, table `fee_rule_overrides`) layered over the v2 seed
  defaults: **effective rule set = v2 defaults ⊕ overlay**, resolved conn-aware at EVERY money
  call site (`get_fee_rule_set(name, conn)`; `conn=None` always returns the seed defaults, for
  the oracle / unit tests). Edits affect **FUTURE trades only** — historical rows are still
  arbitrated by their own `fee_rule_snapshot` (§3, §10.2) and are never recomputed. Reset
  semantics: clearing a field (null = revert one field) or deleting the whole overlay row
  (per-set / reset-all) returns it to the seed defaults.
- **fee-engine v2 is implemented from the owner's complete schedules (2026-07-15)**:
  `config_seed.py::FEE_RULES` now carries the complete schedules from
  `docs/reference/broker-fee-schedules-2026-07.md`; §3.1–§3.4 document what the v2 engine
  actually computes. The earlier v1-vs-schedule "known divergences" (US `sec_fee`
  0.0000278→0.0000206, TAF/CAT/platform/settlement, MY shape, TW rounding) have **all been
  reconciled in v2**.
- **Per-row regime**: v2 is a **per-row regime**. Old rows are arbitrated under the v1 rates
  and rounding in their `fee_rule_snapshot`; new rows carry an `engine="v2"` snapshot and are
  arbitrated under v2. Historical rows are **never recomputed** — the `fee_rule_snapshot`
  (§3, §10.2) is the final arbiter.
- **Config over hard-coding**: rates that change over time (SEC/TAF, commission, stamp) live
  in `FEE_RULES` (config); a rate change is a config change and must be recorded in
  `CHANGELOG.md`.
- **Limitations (documented)**: REIT-specific stamp caps are not modeled (no REIT flag; the
  ETF flag governs); the per-component rounding of MY/US fees is an assumption pending real
  statement verification; options/bonds/futures/fractional shares are out of scope (the app
  trades whole-share stocks/ETF only).

> **Implementation**: `data_ingestion/fees.py`, `data_ingestion/config_seed.py::FEE_RULES`,
> `data_ingestion/fx_lookup.py` (stamp FX resolution); complete schedules
> `docs/reference/broker-fee-schedules-2026-07.md`.
> **Basis**: `.claude/rules/markets-and-fees.md`.
> **Verification anchor**: the `fee_engine.*` entries in §3.1–§3.4 (stress phase1 2026-07-15,
> `fee_engine.fee` / `fee_engine.tax` **80/80 passing**); edge cases (TAF/settlement caps,
> missing-FX degrade) are guarded by unit tests.

### 3.6 TW Rebate Forecast (群益 charge-first-refund-later; FORECAST-ONLY, not a number of record)

The 群益 2.3折 (23%-of-list) model charges the full `0.1425%` list fee at settlement and
refunds 77% of it next month. The refund **never enters cost basis, P&L, or `compute_fees`**
(FE-D1): `compute_fees` always books the full list price (§3.1, `discount=1`). The system only
**forecasts** the refund for information:

$$\text{expected\_rebate}_{\text{per trade}} = \big\lfloor \text{fee} \times \text{rebate\_rate} \big\rfloor,\quad \text{rebate\_rate}=0.77\ (\text{floor on any fraction})$$

Implementation: `fees.py::forecast_tw_rebate(fee, rebate_rate)` (pure). **Full 群益 walk**: buy
142 → ⌊142×0.77⌋=**109**; sell 156 → ⌊156×0.77⌋=**120**; monthly total 229. When the actual
refund arrives (next month) the owner **confirms** it in the inbox, booking a cash movement
`kind='rebate'` (折讓款) with an editable amount (prefilled with the estimate; **the actual
wins — the estimate is never a number of record**). This forecast / confirmation flow (inbox,
hint, cash movement) is **Wave B**; this §3.6 defines only the pure formula. Classified in
§12.5 (class B).

> **Verification anchor**: the 109/120 of `forecast_tw_rebate` are guarded by unit
> `test_gunyi_rebate_forecast_floor` (and `test_fees`); being a FORECAST value, **not a number
> of record**, it is not part of the stress scalar reconciliation.

---

## 4. Cost Basis (Weighted Average)

**Method**: **weighted-average cost**, all markets. Tracked in the instrument's **quote
currency** (TW→TWD, US→USD incl. Moomoo, MY→MYR). Each position (`account_id` × `symbol`)
maintains two totals:

| Field | Definition | Overwritable |
| --- | --- | --- |
| `original_total` (original cost total) | **all-in**: cumulative buy `quantity×price + fees + tax` | **never overwritten** (I2) |
| `adjusted_total` (adjusted cost total) | `original_total − cumulative net cash dividends` (see §6) | changes with dividends / sells; **may be ≤ 0**, never floored |

**Average cost is always divided on read** (I7, avoiding cumulative rounding error across
lots):

$$\text{original\_avg} = \frac{\text{original\_total}}{\text{shares}}\qquad \text{adjusted\_avg} = \frac{\text{adjusted\_total}}{\text{shares}}$$

### 4.1 Chronological Replay

`cost_basis.py::build_book` sorts the four ledgers by **(date, same-day priority)** and
replays row by row. **Same-day priority**:

$$\text{opening}(0) \prec \text{buy}(1) \prec \text{sell}(2) \prec \text{dividend}(3)$$

- **Buy**: `cost = quantity×price + fees + tax`; `shares += quantity`;
  `original_total += cost`; `adjusted_total += cost`.
- **Sell (proportional removal)**: let `frac = quantity / shares` (shares before the
  sell), then

$$\text{original\_removed} = \text{original\_total}\times\text{frac},\quad \text{adjusted\_removed} = \text{adjusted\_total}\times\text{frac}$$

  after removal `shares -= quantity`, `original_total -= original_removed`,
  `adjusted_total -= adjusted_removed`.
- **Buy again after full sell (restart)**: when `shares` reaches zero, the position
  totals reset to zero; a subsequent buy accumulates a fresh batch (the new weighted
  average naturally starts from zero).

### 4.2 Verified Worked Example — `tw_broker/0050`

This example shows: all-in cost, **sort by trade date** (a sell precedes a later buy),
proportional removal, and a cash dividend lowering `adjusted_total`. Ledger:

| Date | Event | Detail |
| --- | --- | --- |
| 2026-01-12 | buy | 10 @ 130, fee 20 → cost 1,320 |
| 2026-02-01 | buy | 100 @ 132, fee 20 → cost 13,220 |
| 2026-04-10 | **sell** | 50 @ 140, fee 20, tax 7 |
| 2026-05-10 | buy | 50 @ 138, fee 20 → cost 6,920 |
| 2026-06-12 | dividend | CASH, net 800 |

Step by step (**note the 2026-04-10 sell sorts before the 2026-05-10 buy**):

1. buy 10: shares 10, total 1,320
2. buy 100: shares 110, total 14,540
3. sell 50: `frac = 50/110`; `removed = 14,540 × 50/110 = 6,609.0909…`; remaining
   shares 60, total 7,930.9090…
4. buy 50: shares 110, `original_total = 7,930.9090… + 6,920 = 14,850.9090…`
5. dividend net 800: `adjusted_total = 14,850.9090… − 800 = 14,050.9090…`

Final position (matches `build_book` output digit for digit):

| Quantity | Value |
| --- | ---: |
| `shares` | 110 |
| `original_total` | 14,850.909090909… |
| `adjusted_total` | 14,050.909090909… |
| `original_avg` | 135.008264462… |
| `adjusted_avg` | 127.735537190… |
| `dividend_portion` (= original − adjusted) | 800.000… |
| `payback_ratio` (see §6.4) | 0.053868756… |

> **Verification anchor**: `holding.original_total / holding.adjusted_total /
> holding.original_avg / holding.adjusted_avg / holding.dividend_portion /
> holding.shares`, `scope = tw_broker|0050` (phase1 final snapshot).

> **Implementation**: `portfolio/cost_basis.py::build_book`, `_Position`; holding result
> `portfolio/results.py::Holding`.
> **Basis**: `.claude/rules/domain-ledger.md` (Cost basis).

---

## 5. Realized / Unrealized P&L

### 5.1 Realized P&L

Each **sell** produces a `RealizedRow` (`cost_basis.py`):

$$\text{proceeds\_net} = \text{quantity}\times\text{price} - \text{fees} - \text{tax}$$

$$\boxed{\text{realized} = \text{proceeds\_net} - \text{adjusted\_removed}}$$

i.e. **net sale proceeds (after fees and tax) − the sell fraction's
`adjusted_avg × shares_sold`**. Realized is measured against **adjusted cost** (dividends
are already folded into cost, so no separate dividend income line → invariant I4, avoiding
double counting). Cross-currency is aggregated by currency in `RealizedPnL.by_currency`.

**Verified examples**

| Sell | proceeds_net | adjusted_removed | realized | Verification anchor |
| --- | ---: | ---: | ---: | --- |
| `tw_broker/0050` 2026-04-10 (50@140) | 6,973 | 6,609.0909… | **363.9090…** | `realized.realized tw_broker/0050@2026-04-10` |
| `schwab/TSLA` 2026-04-20 (20@260) | 5,199.88 | 5,000.00 | **199.88** | `realized.realized schwab/TSLA@2026-04-20 #3` (`phase1:final`) |

(The TSLA sell fee = 0.12 (SEC 0.11 + TAF 0.01, see §3.2 / E4) → `proceeds_net = 5,200 −
0.12 = 5,199.88`. Per-currency realized is anchored by the per-event `realized.realized`
rows (14 rows, `phase1:final`); the reporting-currency cumulative realized after conversion
is `kpi.realized_total TWD = 186,333.50…` (`phase1:final`, see §7.1). The native-ccy
cumulative sums are not a single anchor, so this version cites the anchored per-event rows
and the reporting-currency total instead of a run-specific hand-summed three-currency
aggregate.)

### 5.2 Unrealized P&L and Capital Gain

`portfolio/pnl.py::value_holdings` fills market-value columns using the current price
`price`:

$$\text{market\_value} = \text{price}\times\text{shares}$$

$$\boxed{\text{unrealized\_pnl} = (\text{price} - \text{adjusted\_avg})\times\text{shares}}$$

$$\text{capital\_gain} = (\text{price} - \text{original\_avg})\times\text{shares}\quad(\text{相對原始成本；供「資本利得 vs 股利」拆分})$$

**Verified example — `schwab/TSLA`**: `shares = 10`, `adjusted_avg = 240.00`, current
price 250 → `unrealized_pnl = (250 − 240)×10 = 100.00`; `market_value = 2,500`.
Verification anchor: `holding.unrealized_pnl / holding.market_value schwab|TSLA`.

### 5.3 Degradation Semantics for Missing Price and Oversell

- **Missing current price**: `price is None` → `market_value / unrealized_pnl /
  capital_gain` all set to `None`, `price_stale = True`; **never fabricate a price**. Any
  rollup gated on `market_value is not None` automatically excludes it.
- **Oversell (sell quantity > holdings)**: a distinction between **input error vs short
  sale**, with semantics of **"blocked pending ack"** (blocked-pending-ack):
  - Validation path (`allow_oversell=False`): `build_book` raises `OversellError`; the API
    returns **422 `oversell_unacknowledged`** (`需確認賣超`).
  - After the user sets `ack_oversell=True`: the dashboard path (`allow_oversell=True`)
    **degrades gracefully** — the position goes net-negative shares, drops its (now
    undefined) cost basis, **produces no realized row**, and the holding is flagged
    `oversold` (**to be clarified**). This is not short-sale accounting.
  - Fix: enter the missing opening inventory / buy.

> **Verification anchor**: `guard.oversell_blocks`, `scope = tw_broker/0050 sell 200>held 110`
> (sell 200 > held 110 → 422 `oversell_unacknowledged`). (Stress op numbers are renumbered
> per release, so this cites the stable check + scope rather than pinning run-specific op numbers.)
> **Implementation**: `portfolio/cost_basis.py` (`OversellError`, `RealizedRow`),
> `portfolio/pnl.py::value_holdings`, `api/routers/input_center.py::manual_commit`.
> **Basis**: `.claude/rules/domain-ledger.md` (P&L and returns; Data integrity).

---

## 6. The Three Dividend Models

Implementation: `data_ingestion/dividend_model.py::apply_dividend_model` (derives
withholding / net / reinvest_shares) + the dividend branch of `cost_basis.py::build_book`.
In the same-day priority, dividend sorts last (see §4.1). `CASH_DIVIDEND_TYPES = {CASH,
NET}` (TW cash + MY single-tier net share the same "cost-reduction" definition).

### 6.1 TW Cash (`CASH`, `tw_broker`) — Cost Reduction

Record the **net amount received**; fold into adjusted cost, **no separate income line**:

$$\text{adjusted\_total} \mathrel{-}= \text{net}\qquad(\text{net 於 TW 現金 = gross}，\text{無預扣})$$

**Verified example**: `tw_broker/0050` dividend net 800 (2026-06-12, after the last buy
and with no sell thereafter) → acts fully on the final 110 shares → `dividend_portion =
800.00`, `adjusted_total = 14,050.909…` (see §4.2).

### 6.2 US DRIP (`DRIP`, `schwab` / `moomoo_my`'s US market leg) — 30% Withholding, $0-Cost Reinvestment

$$\text{withholding} = \text{gross}\times 0.30\qquad \text{net} = \text{gross} - \text{withholding}$$

$$\text{reinvest\_shares} = \frac{\text{net}}{\text{reinvest\_price}}\quad(\text{reinvest\_price = 登錄之再投資價})$$

Reinvested shares are added to the position at **$0 cost**: `shares += reinvest_shares`;
**`adjusted_total` unchanged** (DRIP does **not** reduce adjusted cost) → the average
falls naturally because zero-cost shares are added. DRIP is **neutral** on cash flow (see
§7, §9).

**Verified example — `schwab/MSFT` dividend id=1**: `gross 100 → withholding 30.00 →
net 70.00`, `reinvest_price 350 → reinvest_shares = 70/350 = 0.20` shares, added at `$0`
cost. So MSFT `dividend_portion = 0.00` (adjusted cost unchanged by the dividend), and
`shares` increases by 0.20.
Verification anchor: `ledger.div.gross/net` (`schwab|MSFT`),
`holding.dividend_portion schwab|MSFT = 0.00`, `holding.shares schwab|MSFT`.

`US_WITHHOLDING = 0.30` applies to both US-stock legs, Schwab and `moomoo_my`'s US market
leg (W-8BEN).

### 6.3 MY Cash (`NET`, `moomoo_my`'s MY market leg) — Single-Tier Net Cost Reduction

Malaysia's single-tier system: record the **net amount received**, following the same
cost-reduction path as TW cash: `adjusted_total −= net`.
Verification anchor: `ledger.div.net moomoo_my|1155`;
`holding.dividend_portion moomoo_my/1155 = 306.25` (note: because that position had a later sell after the
dividend, `dividend_portion` is **proportionally removed** by the sell, so it does not
equal the cumulative dividend total — cross-reference §4.1 proportional removal, §5.1).

### 6.4 Stock Dividend (`STOCK`) and Display-Only Payback Progress

- **Stock dividend (配股)**: `shares +=` (no cash, no cost change); `withholding = net = 0`.
- **Dividends enter total return exactly once** (invariant I4): TW/MY cash via cost
  reduction, US DRIP via $0-cost shares — each exactly once; **no separate dividend line**
  (the old double-count trap).
- **Display-only payback progress / dividend recovery ratio**:

$$\text{payback\_ratio} = \frac{\text{cumulative cash dividends}}{\text{original\_total}} = \frac{\text{dividend\_portion}}{\text{original\_total}}$$

  (`cost_basis.py`: `dividend_portion = original_total − adjusted_total`. This is a
  display metric; it does not enter the return numerator.)

### 6.5 Dividend Detection and Pending-Confirm Import (inbox estimation)

Implementation: `api/dividend_inbox.py::detect` (**read-only, self-healing**, writes no
pending rows) + `confirm` (on confirm the **server recomputes** before writing to the
ledger; client numbers are display-only). The detection window = each symbol's earliest
acquisition date → today; **ex-dividend entitlement** uses "**held before the
ex-dividend date**":

$$\text{shares\_held} = \text{shares\_on}(account, symbol, \text{before}=ex\_date)\quad(\text{事件日期嚴格早於除息日者才計入})$$

(`data_ingestion/holdings.py::shares_on`: opening + buys − sells + non-cash
`reinvest_shares`, same replay rules as §4.1. A buy on the ex-dividend date itself does
**not** carry entitlement.) Each estimate's gross:

$$\text{est\_gross} = \text{cash\_amount（每股）}\times \text{shares\_held}$$

By the account's `dividend_model`, three formulas (after confirm they become the §6 ledger
row):

- **DRIP (`drip_us`)**: `est_withhold = est_gross × 0.30`, `est_net = est_gross −
  est_withhold` (same as §6.2). **The reinvest price is an estimate**: the last inventory
  close on or before the payment / ex-dividend date (`_price_on_or_before`, 14-day
  lookback), `est_reinvest_shares = est_net / est_reinvest_price`. **No inventory close →
  that row is not confirmable (`缺再投資價`)** and requires backfilling historical quotes
  first; after confirm the actual reinvest price can still be edited in the ledger.
- **MY cash (`cash` → `NET`)**: `est_net = est_gross` (single-tier net, no withholding,
  same as §6.3).
- **TW cash (`cash_cost_reduction` → `CASH`)**: `est_net = est_gross` (same as §6.1;
  cost reduction is applied at rebuild time).

**TW stock distribution (par-value basis)**: a separate share-only item (family =
`stock`):

$$\text{added\_shares} = \frac{\text{shares\_held}\times \text{stock\_amount（元，面額計）}}{\text{TW\_STOCK\_PAR}=10}$$

i.e. each share receives `stock_amount / 10` shares, booked at **$0 cost** (`STOCK`, see
§6.4; `withholding = net = 0`). This **par-value-10 share-conversion formula** is the
concretization of the §6.4 stock-dividend semantics; it governs arbitration of TW stock
distribution share counts.

**Suppression (dedup)**: if the same (account, symbol, family) already has a same-family
ledger dividend row within **±45 days** of the ex-dividend date, or the user has skipped
it (skip fingerprint persisted) → it no longer appears in the inbox.

> **Verification anchor**: the 1,060 stress assertions do not cover the inbox estimate
> scalars (`detect` is a read-only projection that writes no ledger); this section's
> formulas rest on `apply_dividend_model` (DRIP 30% is already anchored via §6.2's
> `ledger.div.gross/net`) and `shares_on`. **The stock-dividend par-value conversion and
> the DRIP reinvest-price estimate have no verification anchor (recommended for the next
> stress round).**
> **Implementation**: `api/dividend_inbox.py` (`detect`, `confirm`, `_price_on_or_before`,
> `_TW_STOCK_PAR=10`, `_US_WITHHOLDING=0.30`, `_MATCH_WINDOW_DAYS=45`),
> `data_ingestion/holdings.py::shares_on`.
> **Basis**: `.claude/rules/domain-ledger.md` (Dividend models; ex-dividend entitlement),
> `.claude/rules/markets-and-fees.md`.

> **Implementation**: `data_ingestion/dividend_model.py`, `portfolio/cost_basis.py`
> (dividend branch, `CASH_DIVIDEND_TYPES`, DRIP requires `reinvest_shares` else
> fail-loud).
> **Basis**: `.claude/rules/domain-ledger.md` (Dividend models; P&L and returns),
> `.claude/rules/markets-and-fees.md` (30% withholding).

---

## 7. Total Return & Return Rates (incl. XIRR)

### 7.1 Total Return and Cumulative Return Rate

Implementation: `portfolio/returns.py::total_return`.

$$\text{total\_return}_{ccy} = \text{realized}_{ccy} + \text{unrealized}_{ccy}\quad(\text{兩者皆相對「調整後成本」，含已平倉部位之已實現})$$

$$\text{reporting\_total\_return} = \sum_{ccy}\operatorname{convert}\big(\text{total\_return}_{ccy},\ \text{spot}(ccy\to\text{reporting})\big)$$

$$\text{rate}_{ccy} = \frac{\text{total\_return}_{ccy}}{\text{gross\_invested}_{ccy}}\quad(\text{分母 = 累計原始投入成本，非年化})$$

> **Degradation note**: when a currency's `gross_invested = 0`, `rate = None`; if a
> holding's current price is missing (stale), its unrealized is excluded from the
> numerator but the cost stays in the denominator → the simple rate **understates**
> return. So the rate is a secondary glance metric; **XIRR is the rigorous metric**.

**Verified rollup (reporting = TWD, spot USD/TWD = 32.5, MYR/TWD = 7.2; `phase1:final`)**

| KPI | Value (TWD) | Verification anchor |
| --- | ---: | --- |
| `realized_total` | 186,333.50 | `kpi.realized_total TWD` (`phase1:final`) |
| `unrealized_total` | 330,003.05 | `kpi.unrealized_total TWD` (`phase1:final`) |
| `total_return` (= realized + unrealized) | **516,336.55** | `kpi.total_return TWD` (`phase1:final`) |
| `total_market_value` | 3,896,529.28 | `kpi.total_market_value TWD` (`phase1:final`) |

(Cross-check: 186,333.50 + 330,003.05 = 516,336.55 ✓.)

**Blended reporting-currency return rate (blended reporting rate, dashboard KPI
`total_return_rate`)** (`portfolio/dashboard.py` step 10):

$$\text{realized\_total} = \sum_{ccy}\operatorname{convert}(\text{realized}_{ccy},\ \text{spot}),\qquad \text{unrealized\_total} = \sum_{ccy}\operatorname{convert}(\text{unrealized}_{ccy},\ \text{spot})$$

$$\text{total\_return\_rate} = \frac{\text{reporting\_total\_return}}{\displaystyle\sum_{ccy}\operatorname{convert}(\text{gross\_invested}_{ccy},\ \text{spot})}\quad(\text{混合分母；為 0 → None})$$

where `gross_invested` (from `cost_basis.build_book`) = each currency's **cumulative
all-in original buy cost**. The table's `realized_total` / `unrealized_total` are this
blended value (anchors `kpi.realized_total` / `kpi.unrealized_total`).

**Monthly snapshot (月度快照)**: `api/snapshots.py::write_snapshot` each night uses the
**same combiner** to store the current month's `total_market_value / total_return /
total_return_rate / xirr / by_currency` (by_currency see §7.3 currency view) as a
**month-end record** (at month close the last ascending value is the month-end value,
upsert-by-month). The snapshot only **persists** the KPIs of this section and §7.3; it
**introduces no new formula**; optional KPIs missing price / FX are stored NULL (honest
degradation). When arbitrating a month-end historical amount, the value stored in the
snapshot row = the combiner's output under this manual's formulas at that time governs.

### 7.2 XIRR (annualized, money-weighted, FX-aware — the primary decision metric)

Implementation: `portfolio/returns.py::xirr_reporting` (solver `pyxirr.xirr`). **Single
reporting currency**; **each flow is converted at its trade-date FX**, and the terminal
value at the **current spot**. Cash-flow signs:

| Flow | Sign | Amount (reporting ccy, converted) |
| --- | :---: | --- |
| buy | **−** | `−(quantity×price + fees + tax)`, date = `trade_date` |
| sell | **+** | `+(quantity×price − fees − tax)`, date = `trade_date` |
| cash dividend (TW `CASH` / MY `NET`) | **+** | `+net`, date = dividend date |
| **DRIP / STOCK** | **neutral** | not included (not an external cash flow; reinvest is not a − outflow, dividend not a + inflow) |
| opening inventory | **−** | `−original_cost_total`, date = **`build_date`** (so opening capital is counted) |
| terminal market value | **+** | `Σ price×shares` (each holding), date = `as_of` |

**Degradation (all-or-nothing)**: if any held symbol is missing a current price → no
terminal value can be formed → returns `None` (no partial degradation); no sign change
(e.g. all outflows) or a non-finite result also returns `None`.

**Flow-construction example (`schwab/TSLA`, single-currency USD, each total has an
anchor)**

| Date | Event | Flow (USD) | Anchor |
| --- | --- | ---: | --- |
| 2026-04-01 | buy 20@250 | −5,000.00 | `ledger.tx.total id=23` (TSLA buy, `phase1:final`) |
| 2026-04-20 | sell 20@260 | +5,199.88 | `ledger.tx.total id=24` (TSLA sell, `phase1:final`) |
| 2026-05-01 | buy 10@240 | −2,400.00 | `ledger.tx.total id=25` (TSLA buy, `phase1:final`) |
| `as_of` | terminal 10 shares @250 | +2,500.00 | `holding.market_value schwab|TSLA` |

XIRR is the annualized rate r that makes the NPV of the `(dates, amounts)` sequence above
equal zero.

> **Verification anchor (added by the resident harness 2026-07-15)**: the XIRR **scalar**
> is anchored by the **independent solver** in `scripts/stress_audit/` (Newton+bisection,
> not using `pyxirr`) — for the same cash-flow sequence and the applied value, the suite's
> only "documented tolerance" comparison `|Δ| ≤ 1e-6`; the measured diff is **well within
> tolerance** (`checkpoint1` / `final` both ≪ 1e-6) (post-merge phase-1 run with
> **1,060/1,060** assertions passing; `kpi.xirr` `phase1:final ≈ 0.4092`).
> The cash-flow construction rules remain governed by `returns.py::xirr_reporting` (the
> table above can be rebuilt from the verified `ledger.tx.total` and
> `holding.market_value`).

> **Implementation**: `portfolio/returns.py` (`total_return`, `xirr_reporting`),
> `portfolio/results.py` (`ReturnSummary`, `CurrencyReturn`).
> **Basis**: `.claude/rules/domain-ledger.md` (Total return; XIRR cashflow signs),
> `.claude/rules/data-and-pricing.md` (Returns & FX P&L).

### 7.3 Allocation Weights, Sector Allocation, Currency View, and Reporting-Currency Valuation

**Reporting-currency valuation rule**: any quote-currency position converted into the
reporting currency always goes through

$$\operatorname{convert}(\text{market\_value}_{quote},\ \text{spot}(quote\to reporting))$$

(`market_value = price × shares`, see §5.2; `spot` is the current spot, via `RateResolver`:
identity → direct pair → inverted pair → KeyError). Missing price → that row's
`market_value is None` and is excluded; missing FX → `weight = None`, **never fabricated**.

**Single-holding weight** (`portfolio/dashboard.py` step 8):

$$\text{weight}_h = \frac{\operatorname{convert}(\text{market\_value}_h,\ \text{spot})}{\text{total\_market\_value}}\quad(\text{total 為 §7.1 報告幣總市值；total}=0\text{ 或缺 → None})$$

This weight drives the `single_weight` alert and rebalance §11.

**Sector allocation** (`portfolio/allocation.py::sector_allocation`; the market-view
allocation `market_view.py::market_allocation` uses the same form):

$$\text{sector\_value}_s = \sum_{h\in s}\operatorname{convert}(\text{market\_value}_h,\ \text{spot}),\qquad \text{sector\_weight}_s = \frac{\text{sector\_value}_s}{\sum_s \text{sector\_value}_s}$$

Sector is determined by the registry `instruments.sector`; stale (missing-price) holdings
are skipped.

**Currency view (combined view)** (`portfolio/allocation.py::combined_view`):

$$\text{by\_currency\_value}[ccy] = \sum_{h:\ quote=ccy}\text{market\_value}_h\ (\text{原幣，不換算}),\qquad \text{reporting\_total\_value} = \sum_h \operatorname{convert}(\text{market\_value}_h,\ \text{spot})$$

`reporting_total_value` is §7.1's `total_market_value`; `by_currency_value` is **each
quote currency's native market value** (the monthly snapshot's `by_currency` stores this,
see §7.1).

**Export-layer reporting-currency values and TOTAL rows**: the export reports'
(`export/holdings*.py`, `ledgers_report.py`, `tax.py`, `rebalance_report.py`)
"reporting-currency value" column uses the same `convert(...)` above; their **TOTAL /
subtotal rows** are the **per-currency sum** of the corresponding column (e.g. `Σ net`,
`Σ original_cost_total`, `Σ market value`, `Σ dividends.net`, `Σ fx from/to`), **introducing
no new formula** (itemized in §12.5). **The one exception** — **the tax report's realized
is converted at the "sell-date FX"** (`export/tax.py`):

$$\text{reporting\_realized} = \text{realized}\times\text{rate}(quote\to reporting\ \text{於賣出日})$$

(**not** the current spot; for local tax purposes, and **different** from §7.1's
spot-converted total-return view — be sure to distinguish when arbitrating a tax amount.)

> **Verification anchor**: weight / sector / currency view have **no stress scalar anchor**
> (the `weight`/`alloc`/`sector`/`by_currency` assertion count = 0, **recommended for the
> next stress round**); the `convert` rule is indirectly verified between §7.1 and §8's
> rollups; the export `original_cost_total` / `adjusted_cost_total` / `shares` totals are
> verified via `export.holdings.*` (20 each).
> **Arbitration-boundary note**: weight / allocation are "ratios of amounts"; following
> §11.2's established precedent, this manual keeps them **within arbitration scope** (with
> formulas). The owner **ruled this settled on 2026-07-15**: weights / return rates
> **remain within arbitration scope**, and the current approach is the standard — see the
> boundary note in §12.5.
> **Implementation**: `portfolio/allocation.py` (`sector_allocation`, `combined_view`),
> `portfolio/market_view.py::market_allocation`, `portfolio/dashboard.py` (holding
> `weight`, step 10 blends), `export/holdings.py`, `export/holdings_report.py`,
> `export/ledgers_report.py`, `export/tax.py`.
> **Basis**: `.claude/rules/domain-ledger.md`, `CLAUDE.md` (module map: portfolio computes
> allocation, web does not).

### 7.4 Dividend-Income Summary and Annual Projection

**Dividend-income summary (display-only)** (`portfolio/dashboard.py` step 6): sums booked
dividend net **per currency, per year**, **excluding stock dividends `STOCK`**, **including
DRIP net**:

$$\text{dividend\_total}[ccy] = \sum_{d:\ type\ne STOCK}\text{net}_d,\qquad \text{by\_year}[y][ccy] = \sum_{\substack{d:\ year=y\\ type\ne STOCK}}\text{net}_d$$

**Currencies are never summed across currencies**. This is a **display-only dividend
statistic** (including DRIP reinvested net as "declared income"), **separate from total
return**: dividends were already folded into cost (TW/MY) in §5 / §6 or turned into
$0-cost shares (US DRIP), each counted once (invariant I4); this statistic **must not** be
added into total return again (else double counting); it is also different from §6.4's
`payback_ratio` (cash dividends only, single position).

**Annual dividend projection (declared-only projection)**
(`portfolio/dividends.py::project_dividends`): for the current year, over held symbols'
ex-dividend events (`ex_date.year == year` and having a cash amount):

$$\text{declared\_gross}[ccy] = \sum \text{shares}_h \times \text{cash\_amount}_{ev},\qquad \text{declared\_net}[ccy] = \sum \text{apply\_dividend\_model}(model_h,\ gross).\text{net}$$

The net **only applies withholding** (DRIP 30%; the Moomoo-US per-order platform fee is
probe-pending and not counted for now); the currency is keyed by the event currency
(fallback quote currency), **never summed across currencies**; an unknown `account_id` →
fail-loud (`KeyError`).

> **Verification anchor: none** (`dividend_summary` / `projection` have no stress
> assertion, **recommended for the next stress round**); their components `dividends.net`
> (`ledger.div.net`, 15) and §6's DRIP 30% are verified.
> **Implementation**: `portfolio/dashboard.py` (step 6 dividend summary),
> `portfolio/dividends.py::project_dividends`,
> `data_ingestion/dividend_model.py::apply_dividend_model`.
> **Basis**: `.claude/rules/domain-ledger.md` (Dividend models; no double counting).

### 7.5 Net-Value and Cumulative-Invested Trend (daily replay)

Implementation: `portfolio/timeseries.py::daily_value_series` (pure function, the combiner
preloads price / FX history). From the first ledger event date to `as_of`, **replay day by
day**, two series per day (reporting currency):

- **Market value `total_value`**: $\displaystyle\sum_{h:\ shares>0}\operatorname{convert}(\text{price}_{\le day}\times \text{shares}_h,\ \text{fx}_{\le day})$, with price and FX using the **last value on or before that day (carry-forward)**. If any holding has **no quote at all** that day or is **oversold (negative shares)** → that day is flagged `incomplete` (**no fabrication**, contributes no market value).
- **Cumulative net invested `net_invested`**: the flow accumulation up to that day, with
  **signs opposite to XIRR (§7.2's negative sign)**: opening `+original_cost_total`, buy
  `+(qty×price+fees+tax)`, sell `−(qty×price−fees−tax)`, cash dividend (CASH/NET) `−net`;
  DRIP/STOCK neutral. Each flow is converted at **its date's carry-forward FX**.

If any flow date has no "on-or-before" FX → the whole series `available = False` (consistent
with §7.2 XIRR's all-or-nothing).

> **Verification anchor: none** (`trend` / `net_invested` have no stress assertion,
> **recommended for the next stress round**); their components (`price × shares`, all-in
> buy cost, sell net, dividend net, `convert`) are verified in §4 / §5 / §7.
> **Implementation**: `portfolio/timeseries.py` (`daily_value_series`, `_at_or_before`,
> `_fx_at`), `portfolio/dashboard.py` (step 9 preload history).
> **Basis**: `.claude/rules/domain-ledger.md` (XIRR flow signs; carry-forward valuation),
> `.claude/rules/data-and-pricing.md`.

### 7.6 Total Net Worth (incl. cash) (FU-D29 / deferred C8)

Implementation: `portfolio/networth.py` (a pure composition layer, called from
`portfolio/dashboard.py` step 9b). **Display / attribution only — NOT a money-of-record
figure**; it feeds no return metric. Without modifying §7.5's `daily_value_series`, it
layers a daily cash series on top and composes (reporting currency):

$$\text{net\_worth}_t \;=\; \underbrace{\textstyle\sum_{h:\ shares>0}\operatorname{convert}(\text{price}_{\le t}\times\text{shares}_h,\ \text{fx}_{\le t})}_{\text{market value } total\_value_t\ (\S7.5)} \;+\; \underbrace{\textstyle\sum_{p\in pools}\operatorname{convert}(\text{balance}_{p,\le t},\ \text{fx}_{p,\le t})}_{\text{cash that day } cash_t}$$

- **Daily cash `cash_t`**: for each `(account, ccy)` pool, take its **carry-forward
  end-of-day running balance** from the dated lines (`pool_lines`: movements ± fx legs ±
  trade settlements ± cash dividends), convert at the **last FX rate on or before that
  day**, and sum across pools into the reporting currency. **Unregistered-symbol rows are
  skipped** (exactly as `cash_balances` does — an unbookable row never poisons the series).
- **Composition `compose_net_worth`**: aligns on §7.5's date axis (cash before its first
  line = 0) and **adds ONLY the `net_worth` field — every other `TrendPoint` field is copied
  byte-identically** (guarded by a unit test).
- **Incomplete rule (mirrors §7.5)**: on a day where a **non-zero** pool has no on-or-before
  FX, `cash_t` is flagged incomplete and `compose_net_worth` leaves `net_worth = None` (the
  frontend draws a gap — **no fabrication**); a **zero-balance pool missing FX does not
  poison the day**. On a holdings-incomplete day (§7.5's `incomplete`) `net_worth` is still a
  partial value, mirroring the market-value line (flagged by the shared marker).
- **Consistency anchor (invariant)**: the last cash-complete day's `cash_t` **equals** the
  `cash_balances`-derived reporting cash total that `GET /api/cash` serves (same fixture,
  both paths, byte-identical). **No FX double count**: this series already sums each pool at
  the day's FX; it is not an FX gain/loss added on top of market value (§8.4 invariant I5).

> **Verification anchor**: `tests/portfolio/test_networth.py` (per-day carry-forward, both
> fx legs, missing-FX incomplete, zero-pool no-poison, negative pool not floored, composition
> leaves pre-existing fields intact) + `tests/contract/test_networth_dashboard.py`
> (cross-endpoint consistency) + golden addition (**`net_worth` only**).
> **Implementation**: `portfolio/networth.py` (`daily_cash_series`, `compose_net_worth`,
> `CashDay`), `portfolio/dashboard.py` (step 9b), `portfolio/dashboard_models.py`
> (`TrendPoint.net_worth` additive field).
> **Basis**: `.claude/rules/domain-ledger.md` (cash pools; FX decomposition never added on
> top), `.claude/rules/data-and-pricing.md` (Decimal; carry-forward).

---

## 8. FX Gain/Loss (FX P&L)

**Dedicated ledger** `fx_conversions` records **every actual conversion**: `date,
account_id, from_ccy, from_amount, to_ccy, to_amount` → implied rate `implied_rate =
from_amount / to_amount` (**home per 1 unit foreign**; e.g. `id=1` TWD 320,000→USD 10,000
→ 320,000/10,000 = **32**, anchor `ledger.fx.implied id=1`). Each foreign pool (per
account) carries a **home-currency (home = the account's `funding_ccy`) cost basis = the
weighted-average acquisition rate**. The Schwab USD pool is anchored in **TWD**; the
`moomoo_my` USD pool is anchored in **MYR**.

### 8.1 Weighted-Average Acquisition Rate (home per foreign)

Implementation: `forex/pools.py::average_acquisition_rate`. Only `home → foreign`
conversions count:

$$\text{avg\_rate} = \frac{\sum \text{from\_amount}\ (\text{home})}{\sum \text{to\_amount}\ (\text{foreign})}\quad(\text{無此類換匯則 None})$$

**Verified examples**

| Account | home→foreign conversions | avg_rate | Anchor |
| --- | --- | ---: | --- |
| `schwab` | TWD 320,000→USD 10,000 (32.0); TWD 2,310,000→USD 70,000 (33.0) | (320,000+2,310,000)/(10,000+70,000) = **32.875** | `fx.avg_rate schwab` |
| `moomoo_my` (USD pool, anchored in MYR) | MYR 44,000→USD 10,000 (4.4); MYR 46,000→USD 10,000 (4.6) | 90,000/20,000 = **4.5** | `fx.avg_rate moomoo_my` |

### 8.2 Realized FX P&L (on reconversion foreign→home)

Implementation: `forex/fx_pnl.py::realized_fx_rows`. For each `foreign → home`
reconversion:

$$\text{realized\_fx} = \text{home\_received} - \text{foreign\_sold}\times\text{avg\_rate}$$

(Deliberately **not** through `shared.fx.convert`, because `avg_rate` is a **derived pool
rate**, not a market spot.) `avg_rate = None` (no cost basis) → returns `None`; a basis
but no reconversion → 0.
**Verified example (`phase1:final`)**: the post-merge scenario includes one Schwab USD→TWD
reconversion (USD 5,000 → TWD 162,000, implied rate 32.4, 2026-06-20). Before it the Schwab
USD pool `avg_rate = 32.875` (see §8.1), so
`realized_fx = 162,000 − 5,000 × 32.875 = −2,375.00 TWD` (reconverted at 32.4, below the
acquisition avg 32.875 → FX loss). `moomoo_my` has no foreign→home reconversion in this
scenario → `realized_fx = 0`. Anchors: `fx.realized schwab = −2,375.000`,
`fx.realized moomoo_my = 0`, `fx.reporting_realized rollup = −2,375.000` (all `phase1:final`;
at `checkpoint1` / `checkpoint2` there is no reconversion yet, so it is `= 0` there — the
scenario evolves by phase).

### 8.3 Unrealized FX P&L (remaining foreign exposure mark-to-spot)

Implementation: `forex/fx_pnl.py::compute_account_fx`. Let `spot = the current foreign→home
rate`:

$$\text{unreal\_stocks} = \text{foreign\_stock\_value}\times(\text{spot} - \text{avg\_rate})$$

$$\text{unreal\_cash} = \text{foreign\_cash}\times(\text{spot} - \text{avg\_rate})$$

where `foreign_cash` is the foreign balance from the **FX-exposure perspective** (rebuilt
from conversions + foreign buys/sells + foreign cash dividends; **different from §9's
operating cash pool**, see the C9 note in the `forex/pools.py` file header). `avg_rate is
None` or `spot is None` → unrealized = `None`.

**Verified example (`phase1:final`; spot USD/TWD = 32.5, USD/MYR = 4.6, MYR/TWD = 7.2)**

Each account is valued as "its remaining foreign exposure × (spot − avg_rate)", with the
rollup converted into the reporting currency (TWD):

- **Schwab (home = TWD)**: `avg_rate = 32.875`, `spot(USD→TWD) = 32.5` → `spot − avg =
  −0.375` (USD depreciated → FX loss; contributes a **negative** amount on Schwab's USD
  exposure).
- **`moomoo_my` (USD pool, home = MYR)**: `avg_rate = 4.5`, `spot(USD→MYR) = 4.6` →
  `spot − avg = +0.10` (USD appreciated vs MYR → FX gain; contributes a **positive** amount
  — unlike the v1.3-basis run's "diff 0", the spot has now moved to 4.6), its MYR value then
  converted into the reporting currency via `MYR→TWD`.

Composing both legs: the reporting (TWD) rollup unrealized FX = **−11,757.483… TWD**. Anchor:
`fx.reporting_unrealized rollup` (`phase1:final`). (Each account's foreign-exposure
components (FX-view cash + stock market value) vary by scenario and have no single assertion
anchor, so this version pins only the anchored rollup plus the verifiable avg_rate / spot;
the per-account exposure decomposition is governed by replaying the formula.)

### 8.4 CRITICAL — FX P&L is a "decomposition", never added on top (invariant I5)

The reporting-currency total return / XIRR **already embeds** FX (flows converted at
trade-date rates, terminal value at the current rate). **FX P&L is an attribution
decomposition of that number (asset P&L vs FX P&L), never an extra gain added on top of
total return.** Any practice of adding `reporting_unrealized_fx` (e.g. the −11,757.48
above) on top of `total_return` (§7) is **double counting** and is a bug.

> **Implementation**: `forex/pools.py` (`average_acquisition_rate`,
> `foreign_cash_balance`), `forex/fx_pnl.py` (`compute_account_fx`, `compute_fx_summary`),
> `forex/results.py`.
> **Basis**: `.claude/rules/domain-ledger.md` (FX / currency-exchange ledger; CRITICAL —
> no double count).

---

## 9. Cash Pools & Running Statement

Implementation: `portfolio/cash.py` (pure calc) + `api/routers/cash.py` (gates and
guards). **One operating cash pool per (account, currency)**. This is **operating cash
tracking**; it **feeds no return metric** (XIRR is still computed purely from trade flows,
see the `cash.py` file header).

### 9.1 Debit/Credit per Flow (`cash_balances` / `pool_lines`)

| Flow | Delta to the (account, ccy) pool |
| --- | --- |
| deposit / opening funding (cash movement) | **+ amount** (credit) |
| withdraw | **− amount** (debit) |
| fx (both legs) | `from_ccy`: **− from_amount**; `to_ccy`: **+ to_amount** |
| buy | **− (quantity×price + fees + tax)** (all-in debit, booked to the `quote_ccy` pool) |
| sell | **+ (quantity×price − fees − tax)** (net credit) |
| cash dividend (`CASH` / `NET`) | **+ net** (credit) |
| **DRIP / STOCK** | **0** (a stock event, does not move cash) |

> **`opening_inventory` deliberately does not touch the cash pool** (its funding predates
> the tracking start). To make the cash pool balance from day one, record a separate
> `deposit` or `opening` (opening funding) cash movement. Note: `opening_inventory`
> (inventory) and the `opening` cash movement (opening funding) are **two different
> concepts**.

Rows whose `symbol` is not registered are skipped (same degradation rule as the
dashboard), so the cash view does not crash.

### 9.2 Running-Balance Statement and Same-Day Ordering

Implementation: `pool_lines` → `_ordered` → `running_statement` / `running_min`. **Same-day
ordering: credit before debit** (`key = (date, 0 if delta≥0 else 1)`), so a same-day
inflow can cover a same-day outflow and the balance does not falsely dip negative for an
instant. `running_statement` returns each row + its subsequent **per-row running balance**;
`running_min` returns the **minimum running balance within the period** (empty pool = 0).

**Verified terminal balances (reporting = TWD; `phase1:final`)**

| Pool | Terminal balance | Anchor |
| --- | ---: | --- |
| `tw_broker` / TWD | 1,089,099 | `cash.balance` / `cash.statement.terminal tw_broker|TWD` |
| `schwab` / USD | 18,159.42 | `cash.balance schwab|USD` |
| `schwab` / TWD | 532,000 | `cash.balance schwab|TWD` |
| `moomoo_my` / USD | 829.95 | `cash.balance moomoo_my|USD` |
| `moomoo_my` / MYR | **123,201.91** | `cash.balance moomoo_my|MYR` |

(The `cash.balance` and `cash.statement.terminal` anchor sets agree at the terminal,
proving the rollup view and the per-row statement converge on the same value.)

> **Batch B merged MYR pool (important)**: cash pools are keyed by `(account_id, ccy)`
> (`portfolio/cash.py`), so after the merge `moomoo_my`'s MYR exposure is a **single
> `(moomoo_my, MYR)` operational pool**. The post-merge stress suite now anchors this single
> pool directly: `cash.balance moomoo_my|MYR = 123,201.91` (`phase1:final`; the US market
> leg's MYR funding and the MY market leg's MYR now share this pool, per-ccy conservation
> guaranteed by `data_ingestion/moomoo_merge.py`'s in-span self-check). **The earlier
> v1.3-basis version derived this value as the sum of the two legacy pools; this version
> adopts the single-pool terminal value directly anchored by the current run** (no formula
> changed; §9.1's balance identity is unchanged).

### 9.3 Negative-Pool Semantics and Guards (date-aware guard)

**A negative pool usually means an unrecorded deposit or conversion.** The guard has two
layers:

- **Hard guard on cash gates (deposit/withdraw, fx.convert)**: uses **`running_min`
  (date-aware, incl. future backfill)** to check whether the row would drive the pool
  negative at **some point in time**; if `running_min < 0` and not `ack_negative` →
  **422 `negative_cash`** (`此筆會使 … 現金於某時點降至 … — 通常代表漏記入金或換匯;確認無誤可強制寫入`).
  An edit / delete must leave **all affected pools** (old + new account/ccy) non-negative.
- **Soft warning on the transaction gate (soft)**:
  `api/routers/input_center.py::_cash_overdraft_issue` — **only if** the account already
  has cash tracking enabled (≥1 cash movement) **and** the buy would drive that symbol's
  cash pool < 0, it attaches a **warning issue (never a hard block)**. Accounts not
  tracking cash do not trigger it.

**Example and current coverage**: once the `running_min` hard guard detects that a pool
would go negative at **some point** without `ack_negative`, it returns **422 `negative_cash`**
(message of the form `此筆會使 … 現金於某時點降至 …`). The post-merge stress scenario **does not
trigger** a `negative_cash` block (its single Schwab USD→TWD reconversion, USD 5,000 → TWD
162,000, passes the running_min check and settles — see §8.2; the scenario has no
`negative_cash` assertion). This hard guard's behaviour is anchored by unit tests (the
`_negative_response` / `_pool_min` paths under `tests/api/…`), not by a single op in this
phase-1 scenario.

> **Implementation**: `portfolio/cash.py` (`cash_balances`, `pool_lines`, `running_min`,
> `running_statement`), `api/routers/cash.py` (`_pool_min`, `_negative_response`,
> `add_movement` / `add_fx` guards), `api/routers/input_center.py::_cash_overdraft_issue`.
> **Basis**: `.claude/rules/data-and-pricing.md` (cash pools; audit C3/C5/C9).

---

## 10. Corrections, Audit & Rebuild

**"Append-only in spirit"**: corrections are **explicit** PUT/DELETE user actions,
**never silent mutation**. Before each write, the **"whole corrected book" is replayed
through `build_book`**, and **only the problems this correction newly introduces** are
blocked.

### 10.1 Replay Guard (replay guard, `ledgers.py::_replay_block`)

Compares the **current book vs the corrected book**, in two categories:

| Block code | Trigger | Nature | Response |
| --- | --- | --- | --- |
| `orphan` | the correction leaves some dividend / opening record **without a corresponding holding** (no buy / opening before that dividend date) | **hard** (cannot be acked around) | 422 `orphan_correction` |
| `oversell` | the correction **newly creates or worsens** an oversell of some position (more negative) | **soft** (`ack_oversell` can bypass) | 422 `oversell` |

**Key scoping**: `introduced_orphans = orphans(post) − orphans(pre)`; for oversell, compare
per key `post_over[key] < pre_over[key]` or newly appearing. **Pre-existing, unrelated**
orphans / oversells **do not** pollute an unrelated correction (audit H3/H8). If the
corrected book **cannot be rebuilt at all** (e.g. DRIP stripped of `reinvest_shares`) and
this problem was introduced by this correction → hard block.

### 10.2 Automatic Fee/Tax Recompute (`_recompute_edit_fees`, audit M6)

On a transaction edit, if a **core field** (account / symbol / side / quantity / price /
date / **daytrade**) changes **and** the user did not explicitly overwrite fee/tax
(`fee_overridden` / `tax_overridden` both False) → **recompute fee/tax with the new
account rule set and regenerate the snapshot**; an explicit override is preserved as an
override (snapshot marked `override: true`).

- **`daytrade` preservation**: on the wire, `daytrade = None` means **keep the DB's
  existing flag** (MED-1); changing daytrade is a core change (it governs the TW sell-side
  tax rate) and feeds `compute_fees` so the recompute reproduces the day-trade rate rather
  than silently reverting to cash equity.
- **Overflow protection**: an over-large notional raises `FeeComputationError` at the
  quantize seam → 400 (audit M4), not 500.

### 10.3 Audit Trail (audit trail, `store.py`, audit M9)

Any update / delete writes the **before-values** to `ledger_audit` **before the change**
(`table_name, row_id, action, before_json, at`). Query via `list_ledger_audit` (newest
first). `original_cost` is inviolable (I2) — a correction produces a new authoritative
state, but the historical prior values are always auditable and recoverable.

### 10.4 Modes

- **Simulate (試算)**: compute, **no write**.
- **Report / update / performance**: full report + live price fetch.
- **Rebuild (重算)**: **fully rebuild** all statistics from the four ledgers (see §1.4).

### 10.5 Verified Correction Examples

| op | Action | Result |
| --- | --- | --- |
| `op44` | delete transaction id=28 (a previously acked oversell, 0050 sell 200) | `ok` (the oversell row disappears, the book recovers) |
| `op45` | edit id=3 (2330 buy 500, price 640→645, explicit fee=460, tax=0) | `ok`, returns `fee=460, tax=0` (override in effect) |
| `op46` | delete transaction id=16 (1155 buy 500@10.20) | `ok` (1155 cost basis recomputed accordingly) |

> **Implementation**: `api/routers/ledgers.py` (`_replay_block`, `_orphan_keys`,
> `_oversold_shares`, `_recompute_edit_fees`, `edit_transaction` / `remove_*`),
> `data_ingestion/store.py` (`_write_audit`, `update_transaction` / `delete_*`,
> `_cap_price`, `daytrade` persistence).
> **Basis**: `.claude/rules/domain-ledger.md` (Data integrity),
> `.claude/rules/engineering-process.md` (append-only spirit).

---

## 11. Rebalance Simulation

Implementation: `strategy/rebalance.py::compute_rebalance`. **Compute-only, never writes
any ledger** — it only projects "which orders to place to reach these weights". It uses the
**same** spot rates (`RateResolver`) and valuation (`build_dashboard`) as the dashboard.

### 11.1 Owner Ruling (2026-07-13) — Option 1 Combined Cross-Account Engine

> **Ruling-date note**: the owner ruled the canonical date is **2026-07-13** (as recorded
> in the code docstring), the authoritative ruling date. The ship record (MEMORY /
> v0.1.18) once noted 07-14, but **canonical = 2026-07-13** governs (both refer to the
> same ruling, Option 1). For arbitration, the semantics of "symbol-level target applied
> to the combined position" govern.

A symbol's **target weight applies to its combined position across "all accounts"**
(Option 1; Option 2's per-account target was rejected). For each target symbol:

1. **Aggregate** that symbol's `shares` + reporting-currency market value across every
   priced account; `delta = target_weight × portfolio_total − combined_MV`.
2. **Route** the execution legs to concrete accounts (fees/tax bind to account —
   invariant I6):
   - **BUY**: a single leg, routed to the account **holding the most shares** (tie-break:
     `account_id` ascending).
   - **SELL**: **greedy, most-held first**, each leg bounded by that account's holding,
     until delta is filled → so a **target of 0 clears every account**, and an
     **oversell never exceeds actual holdings**.
3. **Whole-share rounding** (per leg, by that leg's market): TW → shares (integer, odd-lot
   flag if not a round thousand), **MY → 100-unit board lot**, US → 1 share. Rounding
   implemented in `_round_shares` (MY via `round(raw/100)×100`).
4. Each leg's fee/tax is computed with **that account's rule set** via the real fee engine
   `compute_fees` (see §3).

### 11.2 Weight and Rollup Formulas

$$\text{current\_weight} = \frac{\operatorname{convert}(\text{combined\_MV}_{quote},\ \text{rate})}{\text{portfolio\_total}}$$

$$\text{delta\_reporting} = \text{target\_ratio}\times\text{portfolio\_total} - \text{current\_MV}_{reporting},\quad \text{side} = \begin{cases}\text{BUY} & \delta>0\\\text{SELL} & \delta<0\end{cases}$$

$$\text{raw\_shares} = \frac{|\delta_{reporting}| / \text{rate}}{\text{price}}$$

$$\text{new\_weight} = \frac{\operatorname{convert}(\text{new\_combined\_shares}\times\text{price},\ \text{rate})}{\text{portfolio\_total}}\quad(\text{分母為「原」總市值，非重算後})$$

### 11.3 Honest Degradation

- A target symbol with **no current price** (unknown, unheld and unpriced, or listed in
  `freshness.missing_prices`, or current price ≤ 0) → **excluded**, listed in `excluded`;
  **never fabricate a price**, never divide by zero.
- v1 **acts only on symbols in `targets`**; unlisted holdings are untouched and do not
  appear in the output.
- `summary.over_allocated`: when Σ(submitted targets) > 1, **flag only** (no hard block).
  `summary.excluded_with_target`: symbols with an existing target weight that will not form
  a row (unheld / unpriced), surfaced so the UI does not silently drop them.
- Money is `Decimal` throughout; the router then serializes to wire strings.

> **Implementation**: `strategy/rebalance.py` (`compute_rebalance`,
> `_priced_constituents`, `_round_shares`, `_Leg`), `strategy/target_weights.py` (access
> to target weights).
> **Basis**: `.claude/rules/domain-ledger.md` (invariant #5 fees bind to account),
> `CLAUDE.md` (rebalance ruling).
> **Verification anchor**: stress phase1 does not cover the rebalance-simulation scalars
> (the engine is compute-only, writes no ledger); this section's formulas are governed by
> the code, and its leg fees are indirectly verified via §3's `fee_engine.*` anchors.

### 11.4 Rebalance Rollup and Leg Amounts

Per leg: `amount = shares × price`; each row's (symbol) `shares / amount / fee / tax` =
the sum of that row's legs. Overall rollup (reporting currency):

$$\text{turnover\_reporting} = \sum_{rows}\operatorname{convert}(\text{total\_amount},\ \text{rate})$$

$$\text{total\_fees\_reporting} = \sum_{rows}\operatorname{convert}(\text{total\_fee}+\text{total\_tax},\ \text{rate})$$

$$\text{cash\_after} = \sum_{rows}\begin{cases}+\operatorname{convert}(\text{total\_amount}-\text{fee}-\text{tax},\ \text{rate}) & \text{SELL（淨流入）}\\[2pt] -\operatorname{convert}(\text{total\_amount}+\text{fee}+\text{tax},\ \text{rate}) & \text{BUY（成本流出）}\end{cases}$$

All are compute-only projections, writing no ledger; `rate` and valuation are the same
dashboard spot (§7.3).

### 11.5 What-if Simulation Projection

Implementation: `strategy/whatif.py::compute_whatif`. **Pure projection**, reusing the
**real fee engine** (§3 `compute_fees`) and the **real ledger replay** (§4 `build_book`),
never writing a ledger. Account binding (Q1): an explicit `account_id` wins, otherwise the
account **holding the most shares**; unheld and unspecified → `WhatIfError` → 400.
`amount = shares × price`.

- **Buy**: `total_cost = amount + fee + tax`; `new_shares = held_shares + shares`;

$$\text{new\_original\_avg} = \frac{\text{held\_orig\_total} + \text{total\_cost}}{\text{new\_shares}},\qquad \text{new\_adjusted\_avg} = \frac{\text{held\_adj\_total} + \text{total\_cost}}{\text{new\_shares}}$$

  (same weighted average as §4.)
- **Sell**: `proceeds_net = amount − fee − tax` (§5.1); `adjusted_cost_removed =
  held_adj_avg × shares` (**equivalent** to §4.1's proportional removal `frac ×
  adjusted_total`, since `held_adj_avg = held_adj_total / held_shares`); `realized =
  proceeds_net − adjusted_cost_removed` (§5.1); `oversell = shares > held_shares` (**flag
  only**, the simulation does not block).
- `new_weight = new_position_reporting / new_total`, where `new_total = current_total −
  old_position_reporting + new_position_reporting` (honest degradation: missing price / FX
  → None).

> **Verification anchor**: §11.4 / §11.5 are both compute-only with no stress scalar anchor;
> their fee/tax via §3 `fee_engine.*` and cost / realized via §4 / §5.1's formulas and
> anchors are indirectly verified. **Recommended for the next stress round.**
> **Implementation**: `strategy/rebalance.py` (`compute_rebalance` rollup section,
> `_Leg.amount`), `strategy/whatif.py` (`compute_whatif`, `_new_weight`).
> **Basis**: `CLAUDE.md` (rebalance ruling), `.claude/rules/domain-ledger.md` (fees bind
> to account I6; weighted-average; realized).

---

## 12. Appendix

### 12.1 Worked-Example Index (each with a verification anchor)

| # | Example | Section | Verification anchor (`scope`) |
| --- | --- | --- | --- |
| E1 | TW fee/tax (2330 buy 1,000@600 → fee 855) | §3.1 | `fee_engine.fee tw_broker/2330 buy 1000@600` |
| E2 | TW cash-equity sell tax (2330 sell 300@700 → tax 630) | §3.1 | `fee_engine.tax tw_broker/2330 sell 300@700` |
| E3 | TW ETF sell tax (0050 sell 50@140 → tax 7) | §3.1 | `fee_engine.tax tw_broker/0050 sell 50@140` |
| E4 | US Schwab sell (TSLA 20@260 → fee 0.12) | §3.2 | `fee_engine.fee schwab/TSLA sell 20@260` |
| E5 | US Moomoo sell (NVDA 25@600 → fee 5.89) | §3.3 | `fee_engine.fee moomoo_my/NVDA sell 25@600` |
| E6 | MY fee + stamp (1155 buy 1,000@9.50 → fee 9.40 / tax 10.00) | §3.4 | `fee_engine.fee/tax moomoo_my/1155 buy 1000@9.50` |
| E7 | Weighted-average cost (0050 full replay → orig 14,850.91 / adj 14,050.91) | §4.2 | `holding.* tw_broker|0050` |
| E8 | Realized (0050 sell → 363.9091) | §5.1 | `realized.realized tw_broker/0050@2026-04-10` |
| E9 | Unrealized (TSLA → 100.00) | §5.2 | `holding.unrealized_pnl schwab|TSLA` |
| E10 | DRIP (MSFT gross 100 → 0.20 shares $0 cost, div_portion 0) | §6.2 | `holding.dividend_portion schwab|MSFT = 0.00` |
| E11 | TW cash dividend cost reduction (0050 net 800 → div_portion 800) | §6.1 | `holding.dividend_portion tw_broker|0050 = 800` |
| E12 | Total return (TWD 516,336.55) | §7.1 | `kpi.total_return TWD` (`phase1:final`) |
| E13 | FX weighted-avg rate (schwab 32.875 / moomoo 4.5) | §8.1 | `fx.avg_rate schwab / moomoo_my` |
| E14 | Unrealized FX (rollup −11,757.48 TWD) | §8.3 | `fx.reporting_unrealized rollup` (`phase1:final`) |
| E15 | Cash-pool terminal (tw_broker TWD 1,089,099) | §9.2 | `cash.balance tw_broker|TWD` (`phase1:final`) |
| E16 | Negative-pool guard (`negative_cash` hard guard; not triggered in the current scenario, behaviour anchored by unit tests) | §9.3 | unit `_negative_response` / `_pool_min` |
| E17 | Oversell block (422 `oversell_unacknowledged`) | §5.3 / §10.5 | `guard.oversell_blocks` (`tw_broker/0050 sell 200>held 110`) |

### 12.2 Glossary (Chinese term ↔ English identifier)

| Chinese | English identifier | Defined in |
| --- | --- | --- |
| 原始成本總額 (original cost total) | `original_total` / `original_cost_total` | §4 |
| 調整後成本總額 (adjusted cost total) | `adjusted_total` / `adjusted_cost_total` | §4 |
| 原始均價 (original average) | `original_avg` | §4 |
| 調整後均價 (adjusted average) | `adjusted_avg` | §4 |
| 淨賣出價款 (net sale proceeds) | `proceeds_net` | §5.1 |
| 已實現損益 (realized P&L) | `realized` / `RealizedRow` | §5.1 |
| 未實現損益 (unrealized P&L) | `unrealized_pnl` | §5.2 |
| 資本利得 (capital gain) | `capital_gain` | §5.2 |
| 股利折入部分 (dividend-folded portion) | `dividend_portion` | §6.4 |
| 回本進度／股利回收率 (payback progress / dividend recovery ratio) | `payback_ratio` | §6.4 |
| 加權平均取得匯率 (weighted-avg acquisition rate) | `avg_rate` / `average_acquisition_rate` | §8.1 |
| 已實現換匯損益 (realized FX P&L) | `realized_fx` | §8.2 |
| 未實現換匯損益 (unrealized FX P&L) | `unrealized_fx_stocks` / `unrealized_fx_cash` | §8.3 |
| 費率快照 (fee-rate snapshot) | `fee_rule_snapshot` / `snapshot` | §3 |
| 當沖旗標 (day-trade flag) | `daytrade` | §3.1 / §10.2 |
| 稽核前值 (audit before-value) | `ledger_audit.before_json` | §10.3 |
| 期初庫存 (opening inventory) | `opening_inventory` | §2 / §9.1 |
| 期初資金（現金移動）(opening funding, cash movement) | cash movement `opening` | §9.1 |
| 單一持股權重 (single-holding weight) | `weight` | §7.3 |
| 產業／市場配置權重 (sector / market allocation weight) | `sector_weight` / `weights` | §7.3 |
| 幣別視圖原幣市值 (currency-view native market value) | `by_currency_value` | §7.3 |
| 報告幣總市值 (reporting-currency total market value) | `reporting_total_value` / `total_market_value` | §7.1 / §7.3 |
| 稅務已實現（賣出日匯率換算）(tax realized, sell-date FX) | `reporting_realized` | §7.3 |
| 混合報告幣報酬率 (blended reporting-currency return rate) | `total_return_rate` (blended) | §7.1 |
| 股利收入彙總 (dividend-income summary) | `dividend_total` / `total_by_currency` | §7.4 |
| 年度股利預估 (annual dividend projection) | `declared_gross` / `declared_net` | §7.4 |
| 淨值趨勢市值／累計淨投入 (net-value trend / cumulative net invested) | `total_value` / `net_invested` (`TrendPoint`) | §7.5 |
| 配息偵測估算 (dividend-detection estimate) | `est_gross` / `est_net` / `est_reinvest_shares` | §6.5 |
| 配股面額換股常數 (stock-dividend par-value conversion constant) | `TW_STOCK_PAR = 10` | §6.5 |
| 再平衡週轉／費用／預估餘額 (rebalance turnover / fees / projected balance) | `turnover_reporting` / `total_fees_reporting` / `cash_after` | §11.4 |
| 試算後新均價 (post-simulation new average) | `new_original_avg` / `new_adjusted_avg` | §11.5 |

### 12.3 Version History

| Version | Date | Notes |
| --- | --- | --- |
| `v1.0-draft` | 2026-07-15 | First draft. Baseline `v0.1.18 + feat/p3-batch3`. Reconciled against 966 adversarial assertions (966/966 passing). **Pending owner confirmation as the arbitration standard.** |
| `v1.1-draft` | 2026-07-15 | **Adversarial completeness audit**: after a repo-wide census of every amount / ratio / metric calculation, filled in the missing class-A formulas — added §6.5 (dividend-detection inbox estimation: pre-ex-date entitlement, DRIP reinvest-price estimate, TW stock-dividend par-value-10 conversion), §7.1 blended reporting-currency return rate + monthly snapshot, §7.3 (single-holding / sector / market allocation weight, currency view, reporting-currency valuation rule, export TOTAL rows, tax realized converted at sell-date FX), §7.4 (dividend-income summary + annual projection), §7.5 (net-value and cumulative-invested trend), §11.4 (rebalance turnover / fees / projected balance + leg amounts), §11.5 (What-if simulation). Added §12.5 "Inventory of numeric formulas outside arbitration scope", itemizing all class B (technical indicators / alert thresholds / export ratios) and class C (LLM budget / spend), achieving "complete enumeration". Baseline unchanged; **still pending owner confirmation.** |
| `v1.2` | 2026-07-15 | **Formally signed off by the owner as the arbitration standard, effective from v0.1.19** (removed the "pending owner confirmation" draft status; version leaves -draft). Folds in the owner's four rulings: ① added the English mirror `docs/accounting-formula-manual.en.md` (a working copy for AI/agent reading; the zh manual is the arbitration authority, and each zh change must regenerate the mirror in the same change set); ② this activation (this row); ③ the §11.1 rebalance ruling's canonical date is set to **2026-07-13** (the ship record's 07-14 was only the ship date); ④ §3 rate honest statement: the owner's complete schedules are on file (→ `docs/reference/broker-fee-schedules-2026-07.md`), superseding the seed rates at the fee-engine-v2 upgrade; until then §3 documents what the current engine computes and lists the known divergences (sec_fee 0.0000278→0.0000206, TAF/CAT/platform/settlement not modeled, MY schedule shape differs, TW Capital Securities (群益) 23%-of-list charge-first-refund-later + rounding divergence), and a fee-dispute note was added to §12.4; ⑤ the §7.3 / §12.5 boundary ruling is settled (weights / return rates remain within arbitration scope). Baseline unchanged. |
| `v1.3` | 2026-07-15 | **fee-engine v2 shipped** (owner sign-off; §3 fully rewritten). ① **TW rounding FE-D3**: fee/tax switch from round-half-up to **unconditional floor (ROUND_DOWN) to integer NT$**, with the min-NT$20 compared after the floor (群益 142.5→142; day-trade tax example 11→10); ② **US regulatory v2**: Schwab / Moomoo US commission $0 / platform $0.99, SELL adds SEC `0.0000206` + TAF `0.000195` (cap $9.79), settlement `0.003/share` (cap 1%), CAT `0.000003/share` — each component rounded then summed; ③ **MY v2**: commission `0.03%` (min RM0.01) + platform RM3 + clearing (cap RM1,000) + **SST 8%**; stamp becomes `ceil(amount/1000)×RM1` (stock cap RM1,000, **ETF exempt**); ④ **FE-D2 US stamp**: the MY stamp on US trades is computed in MYR, booked in USD (`stamp_fx` resolved by the caller; no rate → 0 + soft issue); ⑤ **FE-D1 rebate**: new §3.6 forecast `⌊fee×0.77⌋` (**not a number of record**, never in `compute_fees`; inbox/confirm is Wave B); ⑥ the snapshot carries `engine="v2"`, a **per-row regime** (old rows arbitrated under their old snapshot, never recomputed). All rates live in config. §3 example anchors updated to fee-engine v2 stress phase1 (`fee_engine.*` 80/80). Mirror regenerated in the same change set. Baseline unchanged. |
| `v1.4` | 2026-07-22 | **Batch B (Moomoo merge) revision** (baseline `v0.1.20 + Batch B`). ① **Account model**: the two former per-market Moomoo accounts (legacy ids documented in `data_ingestion/moomoo_merge.py`) are merged into ONE dual-market account `moomoo_my` (settlement USD / funding MYR; rules bind per (account, market): US→(`moomoo_us`,`drip_us`), MY→(`moomoo_my`,`cash`), held in `account_market_rules`) — §2 account table 4→3 rows, invariant I6 changed from "bind to account" to "bind to (account, market)", and the account labels + `scope` anchors in §3.3/§3.4/§6.2/§6.3/§8/§9 re-anchored onto `moomoo_my` (market carried by the symbol). ② **Full anchor re-reconciliation**: the stress suite was regenerated to the post-merge topology (1,060 assertions, 66 ops, 1,060/1,060 passing, 0 fail; spot USD/MYR 4.5→**4.6**, plus one Schwab USD→TWD reconversion). Scenario-dependent terminal values updated to this current run: §7.1 total return 514,752.85→**516,336.55** (realized 186,333.50 / unrealized 330,003.05), §8.2 realized FX 0→**−2,375** (Schwab reconversion), §8.3 unrealized FX rollup −31,830.94→**−11,757.48** (`moomoo_my` now contributes a positive leg because spot 4.6≠avg 4.5), §9.2 cash pools fully updated with the MYR pool now a single directly-anchored `moomoo_my|MYR = 123,201.91`, §5.1 TSLA proceeds/realized 5,199.86/199.86→**5,199.88/199.88** (SEC fee 0.14→0.12); fixed pre-existing typos E5 (NVDA fee 1.41→5.89) and E6 (1155 fee/tax 10.45/9.50→9.40/10.00). ③ **Anchor robustness**: the volatile `id=NN` (renumbered per release) removed from the §12.1 fee examples, keeping the stable check+scope; the `negative_cash` example (former op47) — no longer triggered by the scenario — is re-anchored to unit tests (§9.3/E16); the oversell anchor is stated via the `guard.oversell_blocks` scope. ④ Verification-basis line, §7.2 harness count (1,006→1,060), §6.5 count (966→1,060) updated. Mirror regenerated in the same change set. **No formula or accounting-definition change — purely a (account, market) binding relabel + anchor re-reconciliation.** |

### 12.4 How to Arbitrate a Disputed Amount

Given an amount "displayed as X on the site but believed to be Y":

1. **Locate the amount type** → the corresponding section: fee/tax §3; holding cost /
   average §4; realized §5.1; unrealized / capital gain §5.2; dividends §6;
   **dividend-detection estimate §6.5**; total return / return rate (incl. blended) §7.1;
   XIRR §7.2; **allocation weight / sector / currency view / reporting-currency valuation /
   tax realized §7.3**; **dividend-income summary / annual projection §7.4**; **net-value
   and invested trend §7.5**; FX P&L §8; cash balance §9; rebalance §11 (**rollup §11.4;
   What-if §11.5**). If the number is none of the above → check §12.5 whether it is
   out-of-scope class B / C (technical indicator, alert threshold, LLM budget).
2. **Pull the relevant ledger rows** (the four permanent ledgers):
   - fee/tax, cost, realized, unrealized → `transactions` (that account×symbol, **sorted
     by `trade_date`**) + `dividends` + `opening_inventory`.
   - FX P&L → that account's `fx_conversions` + `fx_rates` (current spot).
   - cash → `cash_movements` + `fx_conversions` + that pool's `transactions` + cash
     dividends.
3. **Replay step by step per that section's formula** (rebuild). Be sure to apply: the
   same-day priority open≺buy≺sell≺dividend (§4.1), the sell **proportional removal**, the
   dividend model (§6), and the precision rules (§1.3, store full precision, quantize only
   at settlement / display).
4. **Compare**: replayed value = ruling value. If it disagrees with the code output → a
   code bug (report it); if it disagrees with this manual's formula → a manual defect
   (report and update).
5. **Audit evidence**: if the row was ever corrected, check `ledger_audit` (§10.3) for the
   before-value to reconstruct history.
6. **FX-dispute-specific check**: confirm the disputer **did not add FX P&L on top of total
   return** (§8.4, invariant I5 — the most common source of double counting).

> **Fee-dispute-specific note (fee-engine v2 is live, per-row regime)**: when arbitrating any
> fee/tax amount, first read the disputed row's **`fee_rule_snapshot` (§3, §10.2) — the final
> arbitration basis**: rows carrying `engine="v2"` are arbitrated by the v2 formulas of
> §3.1–§3.4; rows without an `engine` marker are arbitrated under the v1 rates / rounding
> recorded in their snapshot (**never recomputed**). The authoritative schedules are
> `docs/reference/broker-fee-schedules-2026-07.md`. For a US stamp dispute, also read the
> snapshot's `stamp_fx_rate` / `stamp_myr` (the FE-D2 conversion trail). The TW rebate
> (`⌊fee×0.77⌋`, §3.6) is a **forecast, not a number of record**, and is not an object of
> fee/tax arbitration (classified in §12.5 class B).

### 12.5 Inventory of Numeric Formulas Outside Arbitration Scope (complete enumeration)

**Complete-by-enumeration principle**: every number displayed / pushed / exported on the
site, if not **within arbitration scope with a formula** (§3–§11, class A amounts), is
**listed below as out of scope**. Out of scope splits into two classes: **class B
informational indicators** (technical indicators / alert thresholds / scores / percentages
— not "records of amounts") and **class C operational-cost accounting** (USD measurement of
LLM budget / spend). Out-of-scope items are **not the object of a money-dispute
arbitration**; their correctness is guarded by their own unit tests, not adjudicated by
this arbitration document.

**Boundary note (the A/B line)**: allocation weights (holding / sector / market weight,
§7.3) and return rates (§7.1) are "ratios of amounts"; this manual keeps them **in class A**
(with formulas), because they are derived directly from market-value amounts and drive
§11 / alert decisions; all other pure ratios / scores / thresholds are class B. The owner
**ruled this settled on 2026-07-15: weights / return rates remain within arbitration scope,
and the current approach is the standard** (see the arbitration-boundary note in §7.3);
this point is closed.

**Class B — Informational Indicators (informational; not records of amounts)**

| Indicator | Formula (one-line) | Implementation | Why out of scope |
| --- | --- | --- | --- |
| day-change % | `(last − prev)/prev` (pure price, deliberately excludes FX) | `api/digest_service.py::_pct_from_last_two` | percentage; the push rule mandates only percentages and counts |
| portfolio day change | `Σ(wᵢ·pctᵢ)/Σwᵢ` (value-weighted) | `api/digest_service.py::_weighted_pct` | percentage |
| movers ranking | sort by day-change %, top-N | `api/digest_service.py::_movers` | ranking |
| SMA / moving average | `Σ(last N closes)/N` | `portfolio/technicals.py::moving_average` | indicator (currency reference, not a record) |
| price_vs_maN | `(price − maN)/maN` (N=20/60/120) | `portfolio/technicals.py::ma_signals` | ratio |
| annualized volatility | `stdev_sample(daily returns) × √252` | `portfolio/technicals.py::annualized_volatility` | volatility |
| max drawdown | `min((close − running_peak)/running_peak)` | `portfolio/technicals.py::max_drawdown` | ratio |
| RSI(14) | `100 − 100/(1+RS)`, `RS=avg_gain/avg_loss` (Wilder smoothing) | `portfolio/technicals.py::rsi` | indicator |
| MA cross | flip of `sign(SMA_fast − SMA_slow)` + `days_ago` | `portfolio/technicals.py::ma_cross` | classification |
| 52-week position | `pct_from_high=(price−hi)/hi`, `pct_from_low=(price−lo)/lo` | `portfolio/technicals.py::week52_position` | ratio (hi/lo are currency reference) |
| trend structure / volume | half-window high-low comparison; `ratio_to_avg=latest/avg`, `surge=ratio≥2` | `portfolio/technicals.py::trend_structure` / `volume_signal` | classification / ratio |
| price_vs_cost | `(price − original_avg)/original_avg`, `…/adjusted_avg` | `portfolio/technicals.py::price_vs_cost` | ratio (inputs are cost amounts, output a ratio) |
| institutional consecutive buy/sell, net_buy_sum | consecutive-day count; `Σ recent N days daily_net` | `portfolio/external_signals.py` | count / external flow (not a record) |
| chg_pct / yoy / mom / percentile | `(curr−prev)/prev`; `count(h≤v)/len` | `portfolio/external_signals.py` | ratio / ranking |
| VIX / Fear&Greed banding | threshold classification; `change = newest − oldest` | `portfolio/external_signals.py` | classification |
| PER / PBR / yield, margin, monthly revenue yoy/mom, index close | passthrough or `chg_pct/yoy/mom` | `portfolio/external_signals.py` | external context (currency reference, not a record) |
| market allocation weight | `sector_value / market_total` (same as §7.3) | `portfolio/market_view.py::market_allocation` | ratio |
| analyst consensus delta | `score_now − score_then`; target-price cut `(then−now)/then` | `api/alert_inputs.py` / `strategy/alerts.py` | score / ratio |
| SymbolMetric | `pct_from_52w_high`, `vol_30d`, `vol_90d` (√252 annualized) | `api/alert_inputs.py::assemble` | indicator |
| TechScore (composite) | `clamp(50 + Σ(score·applied_w·0.5), 0, 100)` | `strategy/rules/composite.py::compose` | score (0–100) |
| 12-1 momentum / MA-cross / RSI-regime / trend-filter scores | each rule's [−1,1] score (param constants in `strategy/rules/params.py`) | `strategy/rules/*.py` | score |
| alert threshold comparisons | `single_weight` / `sector_weight` / `fx_drift=\|spot/avg−1\|` / `drawdown=−pct_from_52w_high` (warn=0.5×risk) / `vol_spike=vol_30d/vol_90d` / `rebalance_drift band=min(abs, 0.25×target)` (Swedroe 5/25) / `calib_gap` (pp) | `strategy/alerts.py::compute_alerts_from` | trigger boolean (whether to alert, not an amount) |
| export info columns | `_return_ratio=unrealized_pnl/adjusted_cost_total`; TOTAL weight `Σ weight`; `sum_target=Σ targets`; `cash_level=max(0, 1−Σtargets)`; tax `rate_used` | `export/holdings_report.py` / `export/rebalance_report.py` / `export/tax.py` | ratio / percentage |
| read-window derivation | `required_sessions`; `required_calendar_days=ceil(sessions×1.4×1.6)` | `api/signals_service.py` | integer window |
| TW rebate forecast (§3.6, FE-D1) | `⌊fee × rebate_rate⌋` (rebate_rate=0.77) | `data_ingestion/fees.py::forecast_tw_rebate` (inbox/confirm is Wave B) | **FORECAST**; the charge-first-refund-later estimate, not a number of record — booked to cash only after the actual refund is confirmed (`kind='rebate'`) |

**Class C — Operational-Cost Accounting (operational cost; USD measurement, not a record of portfolio amounts)**

| Item | Formula (one-line) | Implementation | Why out of scope |
| --- | --- | --- | --- |
| per-call cost | `cost = (in_tok × in_price_per_mtok + out_tok × out_price_per_mtok) / 1,000,000` (USD) | `shared/llm.py::cost_of` | LLM operating spend, not a portfolio amount |
| remaining budget | `budget_remaining = Σ topups − Σ usage.cost` (cumulative, no reset) | `shared/llm_config.py::budget_remaining` | budget accounting |
| budget gate | `remaining ≤ 0 → LLMBudgetExceeded` | `shared/llm_config.py::check_budget` | gate |
| budget-alert threshold | default `1.00` (USD); `quota_low` triggers when `remaining < threshold` | `shared/llm_config.py::get_alert_threshold`, `strategy/alerts.py` | threshold / operational |
| usage export | `llm_usage` / `job_runs` passthrough export (token, cost read directly, no new calc) | `export/usage.py` | passthrough operating record |

> **Complete-by-enumeration claim**: as of baseline `v0.1.18 + feat/p3-batch3`, after this
> adversarial census, every number the site produces is **either in §3–§11 (class A, with
> an arbitration formula) or in this §12.5 (class B / C, listed out of scope)**. Any future
> displayed / pushed / exported number must be classified and added to this manual in step
> (class A gets a formula; class B / C gets a table row), else it is a manual defect (see
> §12.4 step 4). The **class-A formulas not yet covered by a stress anchor** (§6.5,
> §7.3–§7.5, §11.4–§11.5) are each marked "Verification anchor: none (recommended for the
> next stress round)" for the next adversarial reconciliation round to fill in.

---

_This manual is `portfolio-dash`'s accounting-formula arbitration standard (signed off by
the owner on 2026-07-15, effective from v0.1.19). All artifacts (code, rule files,
CHANGELOG) remain in English; this arbitration document's Traditional-Chinese prose is a
deliberate, flagged exception and is the **arbitration authority**; the English mirror
`docs/accounting-formula-manual.en.md` is for AI/agent reading only and must be regenerated
in the same change set whenever the zh manual changes._
