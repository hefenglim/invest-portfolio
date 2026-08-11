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

> **Version**: `v1.7` (2026-08-11)
> **Code baseline**: `v0.1.28 + feat/corporate-actions` (corporate actions
> SPLIT / EXCHANGE / SPINOFF)
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
> reconciled against — the **resident live stress run**: a set of adversarial
> reconciliation assertions (`scripts/stress_audit/evidence/oplog.jsonl` +
> `scripts/stress_audit/evidence/assertions.jsonl`). **Counted in place for v1.7**:
> phase-1 **118 ops, 3,791/3,791 passing, 0 fail**; phase 2 (live demo) **1,192
> assertions, 0 fail**. Each numeric example is tagged with its `scope` verification
> anchor; scenario-dependent terminal values also carry their phase (`phase1:final`,
> `phase1:corp_applied` etc.). The manual author fabricated no numbers. **Note**: the
> stress scenario evolves per release (v1.6 was 77 ops / 1,806 assertions; the
> corporate-action `CA*` scenario joined in this version), so every revision must
> re-reconcile its anchors against the then-current run (see §12.3).

---

## Table of Contents

1. [General Principles & Precision Rules](#1-general-principles--precision-rules)
2. [Account / Market / Currency Model](#2-account--market--currency-model)
3. [Fee & Transaction-Tax Formulas](#3-fee--transaction-tax-formulas)
4. [Cost Basis (Weighted Average, Declared Short Sale, **Corporate Actions**)](#4-cost-basis-weighted-average)
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

If any amount displayed on the site is disputed, **replay the five permanent ledgers
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

Five **permanent sources of truth**: `opening_inventory`, `transactions`, `dividends`,
`fx_conversions`, **`corporate_actions` (corporate actions, §4.4)**. **All** derived
numbers (holdings, cost, realized / unrealized, returns, FX P&L, cash balances) are
computed on read by **replaying these five in date order**; "computed results" are never
treated as the source of truth (cache them only if profiling shows a need). Arbitration
always uses the replay.

> **`corporate_actions` joined as the fifth in 2026-08** (corporate-actions spec §3). It is
> a **ledger row** like the other four: a corporate action **never** edits an existing
> transaction row and is **never** an adjustment applied to a computed result; it is a row
> the replay reads. So "`original_cost` is never overwritten" (I2) still holds — the
> replay accumulator's `original_total` is zeroed by an EXCHANGE and scaled by a SPINOFF,
> but the next rebuild reconstructs the same value from the **same unchanged ledger rows**.
> **Omitting this ledger from a replay yields an amount that looks entirely normal and is
> priced against pre-action share counts** — the most dangerous failure shape in
> arbitration. The ledger catalogue's single declaration site is
> `shared/ledger_registry.py::LEDGER_TABLES`.

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

`cost_basis.py::build_book` sorts the five ledgers by **(date, same-day priority)** and
replays row by row. **Same-day priority** (single declaration site
`shared/ledger_events.py::EventPriority`):

$$\text{OPENING}(0) \prec \textbf{CORPORATE\_ACTION}(10) \prec \text{BUY}(20) \prec \text{SELL}(30) \prec \text{DIVIDEND}(40)$$

> **The numbers changed from 0/1/2/3 to 0/10/20/30/40 in 2026-08**, inserting the
> corporate action between opening and buy (§4.4.3 explains *why that position*). The gap
> of 10 lets a future event type insert **without touching any existing value**, and the
> priorities are a **named enum** rather than scattered literals, because updating two
> copies and missing a third produces a **mis-ordered replay that looks correct**. The
> **relative order is unchanged**, so a ledger with no corporate action replays
> byte-identically to before.

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

### 4.3 Declared Short Sale (owner ruling 2026-07-31)

The earlier "**NOT** short-position accounting" stance is **narrowed, not reversed**: short
accounting applies **only** to a sell the user explicitly declared, never to an oversell. A
transaction carries `short_sale` (default **false**); **only** a declared sell may exceed
holdings, and an undeclared oversell still follows the 賣超 path of §5.3. The flag is
**never inferred** — the system cannot distinguish a genuine short from a missing buy, and
auto-applying short accounting to every oversell would turn one typo into a
**plausible-looking realized loss** (more dangerous than the absurd number it replaced).

**Replay (weighted average, no lot tracking — consistent with the equity cost method)**

A declared sell first exhausts the **long** lot (emitting an ordinary realized row), then the
remainder opens/extends a **short** lot holding the **net proceeds received**:

$$\text{short\_proceeds} \mathrel{+}= \frac{q\times p - \text{fee} - \text{tax}}{q}\times q_{\text{short}},\qquad
\text{short\_avg} = \frac{\text{short\_proceeds}}{\text{short\_shares}}$$

A buy **covers** the short first, then adds the remainder to the long lot. Long and short are
therefore **mutually exclusive** (a sell consumes the long first, a buy the short first), so a
position is always long / flat / short and carries **one signed quantity**.

**Cover P&L (the owner's rule: settle the gain at the buy-back's per-share cost; the leftover
shares start from that cost)**

$$c_{\text{cover}} = \frac{q\times p + \text{fee} + \text{tax}}{q}\qquad
\text{realized} = (\text{short\_avg} - c_{\text{cover}})\times q_{\text{covered}}$$

The realized row carries `kind = "short_cover"` and is dated the **cover date** (not the sell
date); the leftover shares begin their long life at $c_{\text{cover}}$. An open short is
presented with `shares < 0` and **negative** cost fields (the proceeds received), so
`avg = total/shares` is the **average sale price**, `market_value = price × shares` is the
negative exposure, and `unrealized = (price − avg) × shares` is **positive when the price
falls** — every existing valuation formula works unchanged on the signed quantity. Any
**ratio** over the basis must divide by `abs(cost_total)` (a negative denominator flips the
sign and shows a profitable short as a loss); `fully_recovered` (已回本) must be gated on
`not short_open` (a short's basis is negative by construction).

**Verified worked example — `tw_broker/2609` (phase1 scenario)**

| Step | Detail | Result |
| --- | --- | ---: |
| Undeclared sell (empty position) | — | **422** blocked (anchor `guard.short_needs_declaration`) |
| Declared sell 2,000 @100 | fee **285**, tax **600** | net proceeds 200,000−285−600 = **199,115** |
| Open state | `shares = −2,000`, `original_total = −199,115` | `short_avg` = 199,115/2,000 = **99.5575** |
| Market value (price 96) | −192,000 | `unrealized = +7,115`, `unrealized_pct = +0.035733…` (**positive**) |
| Cover ① buy 800 @95 | fee 108 → all-in 76,108, `c=95.135` | realized = (99.5575−95.135)×800 = **3,538** |
| Cover ② buy 1,200 @98 | fee 167 → all-in 117,767, `c=98.13916̄` | realized = **1,701.999999999999999999999996** |

Cover ② is the **repeating-decimal** case: `117,767/1,200` does not terminate, so the result
carries a 28-digit tail — **deliberately not rounded**, consistent with §1.3's "quantize only
at settlement/display".

> **Verification anchors**: `holding.shares / holding.original_total / holding.original_avg /
> holding.market_value / holding.unrealized_pnl / holding.unrealized_pct / holding.short_open`,
> `scope = tw_broker|2609`; `realized.realized / realized.proceeds_net / realized.kind`,
> `scope = tw_broker/2609@2026-07-01 #16` (3,538) and `tw_broker/2609@2026-07-06 #17`
> (1,701.999…996); `fee_engine.fee/tax scope = tw_broker/2609 sell 2000@100 id=46`.

**A dividend during an open short is unbookable.** A short seller **pays** the dividend
(payment in lieu) and this ledger has no debit row for it. Booking the recorded (positive) net
as income, or adding DRIP/stock shares straight to the long lot, are **both money errors** —
the latter also breaks long/short exclusivity (a DRIP equal to the short nets the position to
zero, and the holding together with its proceeds vanishes from the report). Therefore: the
strict path **raises `UnbookableLedgerError`** (a `ValueError` subclass, so existing degrading
call sites are unaffected), and the dashboard path **skips the event and flags
`unbookable_dividend` (待釐清)**, never booking it. Record such a payment as a cash movement
instead. Anchors: `holding.unbookable_dividend scope = tw_broker|2609`,
`short.unbookable_dividend_removable`.

**Known limitations (ruled, not defects)**: `gross_invested` **excludes** short capital (a
cover is funded by proceeds already received), so a currency whose only activity is a short
has a `None` simple return rate (XIRR remains the rigorous metric); XIRR over a pure short
round trip reports a **borrowing rate** (the flow pattern is a loan); allocation weights use a
net-exposure convention that sign-flips when the portfolio is net short.

> **Implementation**: `portfolio/cost_basis.py::build_book`, `_Position`; holding result
> `portfolio/results.py::Holding`.
> **Basis**: `.claude/rules/domain-ledger.md` (Cost basis; Declared short sale 2026-07-31).

### 4.4 Corporate Actions (SPLIT / EXCHANGE / SPINOFF)

A corporate action **changes a share count without moving any cash**. The transaction ledger
cannot express that: `transactions.side` is `BUY` or `SELL` only, and both settle money. The
consequence is not a missing report line: a 3-for-1 split that cannot be recorded makes
**every subsequent sell** exceed the recorded holding and trip the oversell guard — and once
the owner acknowledges it, **STICKY 賣超** (§5.3) **discards that position's cost basis
permanently**. That guard is correct and must not be weakened; the correct fix is to make the
share change **recordable**. That is what this section is.

**A corporate action is not income and not a purchase.** It emits **no `RealizedRow`**
(§5.1), does **not** touch `gross_invested` (§7.1), and is **not an XIRR cash flow** (§7.2) —
it only re-labels and re-denominates an existing basis.

> **Normative source**: `docs/spec/2026-08-06-corporate-actions.md` (owner decisions D1–D39).
> The three formulas and the field-transfer table below are **quoted verbatim** from that
> spec and are deliberately **not restated in this manual's own words**: a paraphrased
> formula is a second source of truth, and a second source of truth eventually drifts from
> the first (this project has been burned by exactly that, repeatedly).

#### 4.4.1 The ledger row and the conservation law

**The fifth permanent ledger**, `corporate_actions` (§1.4): `account_id`, `date` (effective
date), `kind` (`SPLIT` / `EXCHANGE` / `SPINOFF`), `from_symbol`, `to_symbol`, `ratio_to`,
`ratio_from`, `cost_carry`, `note`.

A corporate action re-labels and re-denominates an **existing** position. It creates and
destroys nothing. Therefore, at the instant any action applies, **within each quote
currency** (this is the section's only acceptance criterion):

| Quantity | Across the event | Why |
| --- | --- | --- |
| Σ `original_total` (all positions) | **unchanged** | SPLIT untouched; EXCHANGE `−P + P = 0`; SPINOFF `−c·P + c·P = 0` |
| Σ `adjusted_total` | **unchanged** | same |
| Σ `dividend_portion` (= Σorig − Σadj) | **unchanged** | follows from the two above |
| `gross_invested` | **unchanged** | only `opening` and `buy` add to it |
| Σ `shares × price` | **unchanged (SPLIT only)** | see the qualification below |

**The value leg is SPLIT-only.** Only a split re-denominates **both** the share count and the
price (§4.4.7, price basis). An EXCHANGE **adds to** its destination rather than
re-denominating it, and a SPINOFF's child begins its own price series at the action. So an
exchange into a destination whose series is missing, or quoted in different terms, moves the
value leg while **all four basis legs stay exactly equal** — a **blind spot of the law, not a
breach of it** (the basis is conserved). It is handled by D19 (a broker identifier is
normalised to its ticker at import, so the event is entered as a SPLIT) and by E23 (a
賣超-tier `needs_confirm` guard on pre-existing suspicious rows, offering a one-click
conversion to SPLIT).

**The two deliberate exceptions** — both are real economic events, so they must NOT be
conserved, and both are booked *outside* the action row:

1. **Cash in lieu of a fraction** → an ordinary **SELL** (§4.4.3). Real disposal, real
   realized P&L.
2. **A reorganisation fee** → a `WITHDRAW` cash movement (§4.4.7, limitation 2). Real cost.

#### 4.4.2 The ratio is a rational, and the evaluation order is normative

The ratio is stored as **two positive integers** (`ratio_to` / `ratio_from`) and **never as a
single decimal**. A decimal ratio is a **rounded quotient**, and §1.3 forbids storing a
rounded quotient as the authority — the same principle as I7 ("the average is never stored;
it is divided on read"). "3-for-1" is `(to=3, from=1)`; "1-for-10" is `(1, 10)`; "2-for-7" is
`(2, 7)`.

$$\boxed{\text{new\_shares} = \text{shares} \times \text{ratio\_to} \div \text{ratio\_from}}\qquad(\textbf{multiply first, divide last})$$

**Multiply-first is a correctness requirement, not a style preference.** Measured by this
manual's author with the project's own interpreter (`.venv/Scripts/python.exe`):

| Expression | multiply first | parenthesised quotient | equal? |
| --- | ---: | ---: | :---: |
| `210 × 1 / 3` | **70** | `69.99999999999999999999999999` | ✗ |
| `3 × 1 / 3` | **1** | `0.9999999999999999999999999999` | ✗ |
| `935 × 18 / 17` | **990** | `989.9999999999999999999999998` | ✗ |
| `700 × 2 / 7` | **200** | `200.0000000000000000000000000` | ✓ (equal in this one case) |

`data_ingestion/validate.py` compares a sell against holdings with a **bare `>` and no
epsilon**. So the `69.999…9` produced by `210 × (1/3)` turns a legitimate "sell all 70
shares" into an oversell → the owner acknowledges → **STICKY discards the cost basis**: the
disaster this section exists to prevent, re-created by the evaluation order alone. An
exhaustive sweep (share counts 1–1,000 × `to` 1–20 × `from` 1–20 = 400,000 pairs) found
**3,530 pairs that cross an integer boundary**.

**The single implementation exit** is `shared/corporate_actions.py::apply_ratio`. That module
deliberately exposes **no property returning the quotient** `to / from` — exposing one would
put a rounded Decimal back within reach of every caller, which is the entire defect it exists
to prevent.

**Both rules are required.** The evaluation order protects the **arithmetic**; it does
nothing about the **input**. `ratio_to = 0.2857` satisfies "Decimal > 0", passes both the CSV
importer and the API, and reproduces the whole cascade — and its magnitude (~5×10⁻⁵) is far
larger than the evaluation-order defect (~10⁻²⁷), so it **bites at any scale**. Hence
**both terms must be positive integers**, enforced at validation (E6a).

#### 4.4.3 Effective instant, same-day ordering, cash in lieu

A corporate action is effective at the **start** of its date: a same-day buy or sell trades in
**post-action** terms (post-split price, new ticker), so the action must apply first. The
same-day order is in §4.1 (`OPENING(0) ≺ CORPORATE_ACTION(10) ≺ BUY(20) ≺ SELL(30) ≺
DIVIDEND(40)`). **Opening inventory dated on an action date is treated as pre-action** (it
describes the position as it stood before) — flagged with a soft warning at entry, because it
is inherently ambiguous.

**Cash in lieu of a fraction**: a reverse split (and most spin-offs) round to whole shares and
pay cash for the fraction. That cash is a **real disposal** with real P&L, so it is recorded
as an **ordinary SELL** at the implied price (`cash received / fractional shares`), **never**
folded into the action row — whose only job is to apply the ratio **exactly**. Worked example
in §4.4.5(e).

#### 4.4.4 The three formulas (normative — quoted verbatim from spec §4.1 / §4.2 / §4.3)

`P` is the position for `(account, from_symbol)`, `Q` for `(account, to_symbol)`. Every
`× ratio` below means `× ratio_to / ratio_from`, **multiply first, divide last** (§4.4.2).

**SPLIT (`from_symbol == to_symbol`)**

```
P.shares          := P.shares × ratio_to / ratio_from
P.short_shares    := P.short_shares × ratio_to / ratio_from     # see E4
P.original_total  := unchanged
P.adjusted_total  := unchanged
P.short_proceeds  := unchanged
```

Neither total moves, so both averages divide by the new share count **on read** (each scales
by `1/ratio`, I7), and `dividend_portion` / `payback_ratio` (§6.4) are unchanged — a split
changes nothing about how much of the cost has been returned as dividends. `ratio > 1` is a
forward split and `ratio < 1` a reverse split; **the same formula covers both**.

**EXCHANGE (the whole position moves to a new symbol)**

```
carried_shares    := P.shares × ratio_to / ratio_from
Q.shares          += carried_shares
Q.original_total  += P.original_total
Q.adjusted_total  += P.adjusted_total
Q.unbookable_dividend |= P.unbookable_dividend      # E19
P.shares          := 0
P.original_total  := 0
P.adjusted_total  := 0
```

Covers a de-SPAC conversion, a merger, and a pure ticker/CUSIP rename (`ratio = 1`). If `Q`
already holds a position, the two merge by **weighted average** — the sum of the totals over
the sum of the shares, exactly what the method prescribes, so there is **no special case**.
`gross_invested` is **not** touched (no new capital entered).

**SPINOFF (the parent keeps its position, a child is created)**

```
c                 := cost_carry          # fraction of the parent's basis moving to the child
Q.shares          += P.shares × ratio_to / ratio_from    # child shares per parent share
Q.original_total  += P.original_total × c
Q.adjusted_total  += P.adjusted_total × c
Q.unbookable_dividend |= P.unbookable_dividend           # E19 — the child inherits it too
P.original_total  := P.original_total − (P.original_total × c)
P.adjusted_total  := P.adjusted_total − (P.adjusted_total × c)
P.shares          := unchanged
```

The parent is written as **`total − carved`, not `total × (1 − c)`**: algebraically identical,
numerically not — `1 − c` rounds once and `× (1−c)` rounds again. Measured counter-example
(run in `.venv`): with `total = 3` and `c = 0.6666666666666666666666666667`,
`total − total×c = 1.000000000000000000000000000` while
`total × (1−c) = 0.9999999999999999999999999999` (a `1E-28` difference). Subtracting **exactly
the amount that was added to the child** makes Σ`original_total` conserved **by construction**
rather than by luck. (In the CA7 example below, with `c = 0.30`, the two forms happen to
coincide — the rule is about the general case, not about that example.)

`cost_carry` comes from the company's Form 8-K allocation and is **never guessed,
interpolated or defaulted**; a SPINOFF row without it is rejected at validation (the same
posture as `acq_home_amount` in the FX pool and `reinvest_shares` on a DRIP). The parent's
share is **never stored** — it is `1 − c` computed on read, so parent and child sum to exactly
1 with no rounding leak (the same reason the average is computed on read).

**Complete `_Position` field-transfer table (normative — quoted verbatim from spec §4.4).**
`_Position` has **nine** fields and every one of them gets an explicit rule, because "the
formula didn't mention it" is not a specification:

| Field | SPLIT (P) | EXCHANGE: source P → dest Q | SPINOFF: parent P → child Q |
| --- | --- | --- | --- |
| `quote_ccy` | unchanged | Q keeps its own; E11 guarantees they match | same |
| `shares` | `× to / from` | `P := 0`; `Q += P.shares × to / from` | `P` unchanged; `Q += P.shares × to / from` |
| `original_total` | unchanged | `P := 0`; `Q += P.original_total` | `Q += P.original_total × c`; `P −= (that same amount)` |
| `adjusted_total` | unchanged | `P := 0`; `Q += P.adjusted_total` | `Q += P.adjusted_total × c`; `P −= (that same amount)` |
| `short_shares` | `× to / from` (E4) | **`P := 0`** — see below | unchanged (E5 guarantees 0) |
| `short_proceeds` | unchanged (E4) | **`P := 0`** — see below | unchanged (E5 guarantees 0) |
| `ever_oversold` | unchanged | **SOURCE** is `False` (E3 rejects). **DESTINATION** must be `False` too — **E22** rejects the action otherwise; nothing is transferred | same (E22 applies to the child's destination as well) |
| `unbookable_dividend` | unchanged | OR-ed into Q: `Q.unbookable_dividend \|= P.unbookable_dividend` (E19) | same OR into the child (E19); `P` keeps its own |
| `unbookable_action` | unchanged | OR-ed into Q: `Q.unbookable_action \|= P.unbookable_action` | same OR into the child; `P` keeps its own |

- **EXCHANGE must explicitly zero the two short fields even though E5 proved them "already
  zero".** They are *nearly* zero, not zero: a full cover computes `P − (P/S)×S`, and Decimal
  division is inexact whenever `S` does not divide `P`, so a residue `ε` survives. Today it
  hides (the emitted `shares` is `0 − 0` and the holdings loop drops the position); but
  EXCHANGE leaves the source position in the map with a **live** meaning, and a later buy on
  the old ticker could reopen it carrying `−ε` of basis.
- **`unbookable_action` propagates** for E19's reason: a position carrying a skipped action
  holds shares in **pre-action terms against post-action prices**, and moving it onto a
  successor without the flag launders exactly the state the flag exists to announce. Note the
  asymmetry with `shares` — the flag is OR-ed into the destination and the source **keeps**
  its own, because on the dashboard path the source may still be a live position.

**The child's payback progress must carry its provenance (D21).** Both totals are scaled by
the same `c`, so `c` cancels in the ratio:

$$\text{child payback} = \frac{c\,(\text{orig}-\text{adj})}{c\cdot\text{orig}} = \frac{\text{orig}-\text{adj}}{\text{orig}} = \text{exactly the parent's payback}$$

Measured (borrowing CA9's two anchored totals, 60,085 / 56,085, as inputs; **CA9 itself is not
a spinoff** — this only demonstrates the identity): `c = 0.30` and `c = 0.5831`
both give `0.06657235582924190729799450778`, identical to the parent's. So a company spun off
last year, which has never paid a dividend in its existence, renders "6.66% of cost
recovered". Under weighted average **the number itself is honest** (the basis carried over, so
the recovery carried with it) — **the label is not**. Rule: where a position's basis
originates from a SPINOFF carve-out, payback progress renders with its provenance —
「已回收 X.XX%（承接自 `<parent>`）」 — and `fully_recovered` (已回本, §6.4) carries the same
suffix. **This is a truth-in-labelling ruling, not a calculation fix: the arithmetic does not
change.** (`cost_carry == 1` (E9) **migrates** the figure rather than copying it: the parent
keeps its shares with zero basis, so **the entity that actually collected every dividend**
reads 0.00% while the child reads the full figure.)

#### 4.4.5 Verified worked examples (each with a verification anchor)

The examples below come from the resident stress run's phase-1 `CA*` scenario
(`scripts/stress_audit/run_phase1.py::run_corporate_actions`), account `tw_broker` (TWD; fees
floored to whole NT$ with a NT$20 minimum; sell tax 0.3%, §3.1). Every fee below also carries
its own `fee_engine.fee` / `fee_engine.tax` anchor, so **no number in these examples was
fabricated by the manual author**.

**(a) SPLIT — `tw_broker/CA1`, 3-for-1, with a sell on the same date**

| Date | Event | Step |
| --- | --- | --- |
| 2026-02-03 | buy 100 @ 600 | notional 60,000; fee `⌊60,000×0.1425%⌋ = ⌊85.5⌋ = 85`; `original_total = adjusted_total = 60,085` |
| 2026-03-02 | **SPLIT 3-for-1** | `shares = 100 × 3 / 1 = 300`; **both totals unchanged** (60,085); `original_avg = 60,085/300 = 200.2833…` |
| 2026-03-02 | sell 40 @ 205 | **same day**: `SELL(30)` runs after `CORPORATE_ACTION(10)`, so this trade is in post-action shares and price. fee `max(⌊8,200×0.1425%⌋, 20) = max(11, 20) = 20`; tax `⌊8,200×0.3%⌋ = 24`; `proceeds_net = 8,200 − 20 − 24 = 8,156` |
| | proportional removal | `frac = 40/300`; `adjusted_removed = 60,085 × 40/300 = 8,011.333333333333333333333331` |
| | realized | `8,156 − 8,011.3333… = 144.666666666666666666666669` |
| | final | `shares = 300 − 40 = 260`; `original_total = 52,073.66666666666666666666667` |

> **Anchors**: `corp.anchor.split_forward`, `scope = tw_broker/CA1.shares = 260`
> (`phase1:anchor`); `export.holdings.shares` / `export.holdings.original_cost_total`,
> `scope = tw_broker|CA1` (`phase1:corp_applied` = 260 / 52,073.666…67);
> `realized.proceeds_net` = 8,156, `realized.adjusted_removed` = 8,011.333…331,
> `realized.realized` = 144.666…669, `realized.kind` = `sale`,
> `scope = tw_broker/CA1@2026-03-02`; `fee_engine.fee/tax`,
> `scope = tw_broker/CA1 buy 100@600` (85 / 0) and `tw_broker/CA1 sell 40@205` (20 / 24).

**(b) SPLIT on a dividend-adjusted position — `tw_broker/CA9`, 2-for-1**

| Date | Event | Step |
| --- | --- | --- |
| 2026-02-21 | buy 200 @ 300 | fee `⌊85.5⌋ = 85`; `original_total = adjusted_total = 60,085` |
| 2026-03-05 | cash dividend net 4,000 | `adjusted_total = 60,085 − 4,000 = 56,085` (§6.1 cost reduction); `dividend_portion = 4,000` |
| 2026-04-02 | **SPLIT 2-for-1** | `shares = 200 × 2 / 1 = 400`; **neither total moves** (60,085 / 56,085) |
| | on read | `original_avg = 60,085/400 = 150.2125`; `adjusted_avg = 56,085/400 = 140.2125` |
| | invariants | `dividend_portion` still **4,000**; `payback_ratio = 4,000/60,085 = 0.06657235582924190729799450778`, **identical either side of the split** |

> **Anchors**: `corp.anchor.split_shares_dividend_adj` (`tw_broker/CA9.shares = 400`) and
> `corp.anchor.split_keeps_dividend_portion` (`tw_broker/CA9.dividend_portion = 4000`), both
> `phase1:anchor`; `export.holdings.original_cost_total / adjusted_cost_total`,
> `scope = tw_broker|CA9` = 60,085 / 56,085 (`phase1:corp_applied`); `fee_engine.fee`,
> `scope = tw_broker/CA9 buy 200@300` = 85.

**(c) EXCHANGE — `tw_broker/CA3 → CA4`, 1-for-2, into a position already held**

| Date | Event | Step |
| --- | --- | --- |
| 2026-02-11 | buy CA4 40 @ 500 (**destination already held**) | fee `⌊28.5⌋ = 28`; `CA4.original_total = 20,028` |
| 2026-02-12 | buy CA3 100 @ 200 | fee `⌊28.5⌋ = 28`; `CA3.original_total = 20,028` |
| 2026-03-16 | **EXCHANGE 1-for-2** | `carried = 100 × 1 / 2 = 50` |
| | destination `Q` = CA4 | `shares = 40 + 50 = 90`; `original_total = 20,028 + 20,028 = 40,056`; `original_avg = 40,056/90 = 445.0666…` |
| | source `P` = CA3 | `shares := 0`, `original_total := 0`, `adjusted_total := 0` |
| | **conservation** | Σ`original_total`: 40,056 before → 40,056 after ✓ |

> **Anchors**: `corp.anchor.exchange_merge`, `scope = tw_broker/CA4.shares = 90`
> (`phase1:anchor`); `export.holdings.original_cost_total`, `scope = tw_broker|CA4` =
> **40,056** (`phase1:corp_applied`, and identical at `corp_refused` / `final`);
> `fee_engine.fee`, `scope = tw_broker/CA4 buy 40@500` = 28 and
> `tw_broker/CA3 buy 100@200` = 28.

**(d) SPINOFF — `tw_broker/CA7 → CA8`, 1-for-4, `cost_carry = 0.30`**

| Date | Event | Step |
| --- | --- | --- |
| 2026-02-19 | buy CA7 400 @ 250 | notional 100,000; fee `⌊100,000×0.1425%⌋ = ⌊142.5⌋ = 142`; `original_total = 100,142` |
| 2026-03-24 | **SPINOFF 1-for-4, `c = 0.30`** | child shares `= 400 × 1 / 4 = 100`; child basis `= 100,142 × 0.30 = 30,042.60` |
| | parent (`total − carved`) | `100,142 − 30,042.60 = 70,099.40` |
| 2026-03-24 | buy CA7 100 @ 190 (**same day**) | `BUY(20)` runs after `CORPORATE_ACTION(10)`, so the carve used the **pre-action 400 shares**. fee `⌊27.075⌋ = 27`; all-in 19,027 |
| | parent, final | `shares = 400 + 100 = 500`; `original_total = 70,099.40 + 19,027 = 89,126.40`; `original_avg = 178.2528` |
| | child, final | `shares = 100`; `original_total = 30,042.60`; `original_avg = 300.426` |
| | **conservation** | Σ`original_total`: `100,142` → `30,042.60 + 70,099.40 = 100,142.00` ✓ |
| | **ordering is observable** | had the action run *after* the same-day buy, the child would be `500 × 1/4 = 125` shares (measured) — which is why the same-day order is normative, not a convention |

> **Anchors**: `corp.anchor.spinoff_child` (`tw_broker/CA8.shares = 100`),
> `corp.anchor.spinoff_parent` (`tw_broker/CA7.shares = 500`),
> `corp.anchor.spinoff_child_basis` (`tw_broker/CA8.original_cost_total = 30042.60`),
> `corp.anchor.spinoff_parent_basis` (`tw_broker/CA7.original_cost_total = 89126.40`), all
> `phase1:anchor`; `fee_engine.fee`, `scope = tw_broker/CA7 buy 400@250` = 142 and
> `tw_broker/CA7 buy 100@190` = 27.

**(e) Reverse split and cash in lieu — `tw_broker/CA2`, 1-for-10**

| Date | Event | Step |
| --- | --- | --- |
| 2026-02-05 | buy 705 @ 30 | fee `max(⌊21,150×0.1425%⌋, 20) = max(30, 20) = 30`; `original_total = 21,180` |
| 2026-03-10 | **SPLIT 1-for-10** | `shares = 705 × 1 / 10` = **70.5** — the ratio applies **exactly**, nothing is rounded |
| 2026-03-12 | cash in lieu → **ordinary SELL** 0.5 @ 300 | fee `max(⌊150×0.1425%⌋, 20) = max(0, 20) = 20`; tax `⌊150×0.3%⌋ = 0`; `proceeds_net = 130.0` |
| | proportional removal | `frac = 0.5/70.5`; `adjusted_removed = 150.2127659574468085106382979` |
| | realized | `130.0 − 150.2127…9 = −20.2127659574468085106382979` (**a real, negative** realized P&L — dominated here by the NT$20 minimum commission) |

> **Anchors**: `corp.anchor.split_reverse`, `scope = tw_broker/CA2.shares = 70.5`
> (`phase1:anchor`); `realized.proceeds_net` = 130.0, `realized.adjusted_removed` =
> 150.212…979, `realized.realized` = −20.212…979, `realized.kind` = `sale`,
> `scope = tw_broker/CA2@2026-03-12`; `fee_engine.fee/tax`,
> `scope = tw_broker/CA2 buy 705@30` (30 / 0) and `tw_broker/CA2 sell 0.5@300` (20 / 0).

**(f) What ratio exactness protects — `tw_broker/CAR`, 1-for-3 on 210 shares**

`210 × 1 / 3` = **70** exactly. Written `210 × (1/3)` it is `69.99999999999999999999999999`,
and `validate.py`'s bare `>` would then **reject** the subsequent "sell all 70 shares". In the
stress run that sell **commits with 201**.

> **Anchors**: `corp.anchor.split_ratio_exact`, `scope = tw_broker/CAR.shares = 70`
> (`phase1:anchor`); `corp.sell_exact_ratio_accepted`,
> `scope = tw_broker/CAR sell 70 (== 210 x 1/3)` = 201 (`phase1:corp`). Same shape:
> `corp.anchor.exchange_2for7` (`tw_broker/CA6.shares = 200`, i.e. `700 × 2 / 7`) and
> `corp.sell_exact_200_accepted` = 201.

#### 4.4.6 Edge-case matrix (E1–E24)

"**strict**" = `allow_oversell=False` (rebuild / what-if / tax export); "**dashboard**" =
`allow_oversell=True`. Every refusal is an `UnbookableLedgerError` (a `ValueError` subclass,
so existing degrading call sites are unaffected); "skip + flag" means the event is skipped and
the position marked `unbookable_action` (**待釐清**). This matrix is **not only the happy
path**: a manual that documents only the happy path is how the owner learns the wrong model.

| # | Situation | Strict path | Dashboard path |
| --- | --- | --- | --- |
| E1 | Action on a symbol with **no prior position** | Refused — fabricating a position would invent a $0-cost ghost holding | skip + flag (see E1a) |
| E1a | The same case on the **dashboard** path | — | **Must skip, must not raise**: `portfolio/dashboard.py` calls `build_book` with **no** try/except, so a raise is a 500 and breaches the standing never-500-at-every-`build_book`-call-site rule. Also requires entry-time validation (the source position must exist **on the action date**) and re-validation when deleting a transaction / opening row that would strand an action |
| E2 | Action on a **closed (0-share)** position — the predicate is **share-only** | Refused (zh message) | skip the event, flag the position |
| E3 | Action on an **oversold** position (`ever_oversold`, basis already discarded) | Refused | skip; the 賣超 flag stays. **Scaling an undefined basis produces an undefined result** |
| E4 | **SPLIT on an open declared short** | Supported: `short_shares × ratio`, `short_proceeds` **unchanged**. You owe more shares and you still received the same money, so the average sale price scales correctly | same |
| E5 | **EXCHANGE / SPINOFF on an open declared short** | Refused — no honest booking exists (precedent: dividend-on-short, §4.3) | skip + flag |
| E6 | `ratio_to` / `ratio_from` ≤ 0, non-numeric, or non-finite | Rejected at **validation**, never reaches the replay. `ratio_from == 0` would be a division by zero *inside* the replay, i.e. a dashboard 500 — so this rejection is load-bearing | — |
| **E6a** | A **non-integer ratio term** (`ratio_to = 0.2857`), from any path | Rejected at **validation** (D14). "The form has two fields" is not a defence: the form constrains the form, while the CSV importer and the API both accepted a decimal and reproduced §4.4.2's cascade in full. A one-column legacy import is a **hard parse error**, never coerced | — |
| E7 | `ratio == 1` on a **SPLIT** | Soft warning at entry (a no-op row). **SPLIT only** — `ratio == 1` on an EXCHANGE is the ordinary rename case and must NOT warn | — |
| E8 | `cost_carry` outside `[0,1]`, or absent on a SPINOFF | Rejected at validation | — |
| E9 | `cost_carry == 1` on a SPINOFF | Soft warning: the parent keeps its shares with **zero** basis — legal but almost always a data error (and it makes the side that actually collected the dividends read 0.00% payback, §4.4.4) | — |
| E10 | **Either** `from_symbol` **or** `to_symbol` not registered in `instruments` | Rejected at validation; routes to the existing register-first flow, and **never auto-registers to make an action fit** | — |
| E11 | **Quote currency differs** between the two symbols | Rejected at validation. Carrying a basis across currencies needs an action-date FX rate; inventing one would corrupt the basis | — |
| E12 | Two actions, same date, same account, **symbol sets intersecting** | **Rejected at validation** (D15), not tie-broken. Ordering by `id` ASC is the order the owner happened to *type* masquerading as economic order, and the two orders **produce different money** (measured: 600 vs 200 shares) — while the conservation law **cannot see it** (Σ is identical both ways). A genuine two-step event is booked as **one** row (a reverse split + rename is one EXCHANGE), or the steps are dated apart | — |
| E13 | The same symbol held in **two accounts** | **All-or-nothing** (D13). Positions are keyed `(account, symbol)`, so N rows are required and a **partial application is rejected at validation**. The un-actioned account would hold **pre-action share counts** against **post-action prices** (`prices` has no `account_id`, so the price correction is global) with every existing check green — the defect lives *between* the share count and the price, and nothing computes that relationship | — |
| E14 | A sell **back-dated before** the action, entered afterwards | Handled by the date-aware guard — *provided* `shares_through` applies corporate actions. This is the integration point most likely to be missed | — |
| **E15** | The identical action entered twice | **Hard rejection at validation** (D29), checked **before E12**, with its own message. An action is an **event**, not a transaction: no ledger has two identical 3-for-1 rows on one day both correct. Acknowledging would apply the ratio **twice** (a 3-for-1 becomes a 9-for-1) | — |
| E16 | **Editing / deleting** an action **re-computes history** | Intended (§10, `domain-ledger.md` N2). Captured in `ledger_audit` like every other ledger edit | — |
| E17 | **Stored price basis vs the action** | See §4.4.7 "price basis": canonical as-traded basis, un-adjust at the write seam, `fetched_at`-discriminated correction, read-time re-expression | — |
| **E18** | **EXCHANGE / SPINOFF whose `to_symbol` position holds an open short** | Refused. `Q.shares +=` breaks the long/short **mutual exclusivity by construction**; the emitted holding would blend a real cost basis with short proceeds, leaving no honest average, and the `abs(cost_total)` ratio rule and the `fully_recovered` gate both mis-fire | skip + flag |
| **E19** | EXCHANGE / SPINOFF **from a position flagged `unbookable_dividend`** | Allowed — but the flag **propagates** (`Q \|= P`). The action itself is legitimate and blocking it would punish a real corporate event for an unrelated older data problem; without propagation the source is dropped at 0 shares and **an unresolved money-of-record problem is erased by an unrelated event** | same |
| **E20** | `to_symbol` vs `from_symbol` **coherence per kind** | SPLIT **requires** `to == from`; EXCHANGE and SPINOFF **reject** it. A self-EXCHANGE zeroes the position and re-adds it, silently rescaling shares by `ratio` while masquerading as a rename; a self-SPINOFF carves `c` out of a position and adds it straight back — **double-counting**. All three enforced at validation | — |
| **E21** | An action referencing a symbol **not registered in `instruments`** (arrives via CSV import or a later instrument deletion, behind E10) | Both of its symbols must join the dashboard's unregistered skip-set, or `quote_ccy()` raises `KeyError` → **500**, a different exception type from every other degradation path | skipped with the rest of that symbol's rows |
| **E22** | **EXCHANGE / SPINOFF whose `to_symbol` position is flagged `ever_oversold`** | Refused — the mirror of E18 and E19 one level deeper: E19 stops a **flag** being laundered, this stops a **cost basis** being restored onto a position whose basis was deliberately discarded. Measured: before the exchange the position reads 均價 0 / 未實現 +1,890 and nobody believes it; after, it reads 均價 33.33 / 未實現 −660, which looks entirely ordinary | skip + flag |
| **E23** | **EXCHANGE, `ratio_to != ratio_from`, `to_symbol` HAS prior prices, `from_symbol` has NONE** | **`needs_confirm` — the 賣超 tier**, not a hard rejection and not a passive notice. The four-part condition is the broker-identifier signature: a real merger's source was a listed security and has prices. Offers a one-click conversion to SPLIT. **After acknowledgement the row commits as an EXCHANGE and the value cliff remains** (no price factor is applied to either series); what the acknowledgement buys is that the discontinuity is *recorded and seen* | same |
| **E24** | **A dividend on a symbol an EXCHANGE moved away** (D32) | Refused, **uniformly on both branches**. EXCHANGE leaves the source in the position map with zeroed fields (§4.4.4, required so a later buy cannot reopen it carrying `−ε`), so neither existing refusal applies and the payment books: CASH/NET books post-close realized income on a dead ticker; DRIP/STOCK does `shares += reinvest_shares` and **resurrects** the position at `avg = 0`, which is delisted so it never prices — and **one unpriced holding blanks the whole portfolio's XIRR indefinitely** | skip the event, flag the position (待釐清); the owner records the payment as a **cash movement** |

> **Implementation-status note (checked in place 2026-08-11; not part of the ruling).** The
> rows above are the **ruling**; if the code disagrees, §12.4 step 4 makes that a **code
> defect**. As checked:
>
> - **Implemented in the replay** (`cost_basis.py::_apply_action`): E1, E1a (dashboard-path
>   skip), E2, E3, E4, E5, E18, E19, E22.
> - **Implemented in the ledger loader**
>   (`shared/models/ledger.py::unregistered_symbols` / `without_unregistered`): E21 — an
>   action contributes **both** of its symbols to the unregistered skip-set.
> - **Implemented in the share walk** (`data_ingestion/holdings.py`): E14 — the date-aware
>   guard walks the action-aware path.
> - **Implemented in the price basis**: E17 (`pricing/schema.py`, `pricing/store.py`,
>   `pricing/reconcile.py`, `portfolio/price_basis.py`).
> - **Implemented in validation but with no production caller yet**
>   (`data_ingestion/validate.py::validate_corporate_action`): E6, E6a, E7, E8, E9, E10,
>   E11, E12, E13, E15, E20, E23 — the entry surfaces are a later work package, so until
>   they are wired these rejections and warnings **take effect in tests only**.
> - **Not implemented: E24** (`_Position` today has no marker distinguishing "zeroed by an
>   action" from "ordinarily closed").
>
> This note describes implementation progress and **changes none of the rulings above**.

#### 4.4.7 Price basis, known limitations, and the hard exclusion

**Price basis (E17).** The stored price is authoritative **as traded**. A provider returns
**already-adjusted** (post-split) closes for dates *before* a split, while the ledger's share
count for the same date is **not** adjusted — multiply the two and the market value is wrong.
So `prices` carries the basis in **two columns**: `close_raw` (the provider's value exactly as
delivered, **un-capped**) and `split_basis` (the factor applied), with
`close = close_raw × split_basis` **recomputed** at the write seam (the 4 dp cap applied to
the **product**, §1.3). On read, a price that was **carried forward across a split date** is
re-expressed into the valuation day's share terms by
`portfolio/price_basis.py::split_factor`. **The factor is for prices only, never for share
counts** (share counts go through §4.4.2's two integer terms). The re-expression is
**SPLIT-scoped**: an EXCHANGE adds to its destination rather than re-denominating it, and
widening the factor to EXCHANGE would corrupt the price history of any merger destination you
**already held**.

**(Limitation 1, standing) D11 — `volume` is not un-adjusted.** A price has `close_raw` to
recover from; `volume` has no corresponding raw column, and the direction of the provider's
volume adjustment **has not been measured** — guessing one would break the same rule this
section enforces. So **volume-based signals spanning a split date are not comparable**. Volume
is not a number of record (§12.5 class B), so this is an **accepted and documented standing
limitation**, not a defect; the correct future fix is its own `*_raw` column carried with the
factor.

**(Limitation 2) D12 — the reorganisation fee is invisible to XIRR *by design*, and visible in
the whole-account IRR (*pending D36*).** The only bookable cash-movement kinds are `DEPOSIT` /
`WITHDRAW` / `OPENING` / `REBATE` (`api/routers/cash.py::_KINDS`), and only `WITHDRAW` is a
debit (`portfolio/cash.py::_movement_sign`). **Ruling: book the fee as a `WITHDRAW` with a
`note`.** The consequences must be stated: it reads in the cash statement as *the owner took
money out*; it reduces the FX pool's exposure without recognising realized FX (§8,
`domain-ledger.md` N1); and `portfolio/returns.py::xirr_reporting` builds its flow series from
`opening` + `transactions` + `dividends` **only**, so **cash movements never reach XIRR**.

> **This is no longer a permanent blind spot (D36, owner ruling 2026-08-10).** XIRR is
> **deliberately left untouched** — so every historical figure, every worked anchor in this
> manual and every stress-audit oracle expectation **stays where it is** — and a
> **whole-account IRR** is added *additively* in the existing `portfolio/twr.py`, which *does*
> see a `WITHDRAW`. So the fee is invisible to XIRR **by design** and visible in the second
> metric.
> **⚠ Pending D36 (checked in place 2026-08-11): `portfolio/twr.py` currently holds only
> `twr_index` / `convert_closes` / `build_overlay` and no IRR at all.** Until that metric
> lands, the reorganisation fee is invisible to **every** current return metric. This
> paragraph deliberately says "pending D36" rather than stating a permanent limitation,
> because writing it up as permanent would document a decision the owner has since reversed.
> The fee also belongs to a whole **class** of items that never reach XIRR (bond/margin
> interest, interest adjustments, ADR management fees, foreign tax reclaim); that class is
> the broker-import backlog's scope, and this section does not invent a partial answer for
> one member of it.

**(Hard exclusion, D34) Cash-and-stock mergers.** "Each share of A becomes 0.6 shares of B
plus US$12.00 cash" — **no fourth action kind is added, and there is no supported way to enter
one.** This is a **hard exclusion**, and the reason has to be stated, because otherwise it
looks arbitrary next to the ruling that cash in lieu of a fraction is an ordinary SELL:

- **The two-row recipe the spec carried until 2026-08-10 is withdrawn and must not appear
  here as a procedure.** It does not execute: `EventPriority` runs `CORPORATE_ACTION(10)`
  before `SELL(30)`, and the EXCHANGE sets the source's shares to 0, so the same-day SELL
  lands on a zero-share position — `OversellError` on the strict path and, on the dashboard
  path, **STICKY 賣超 with the cost basis discarded**: the exact disaster named at the top of
  this section, produced by following the documentation. Re-ordering the events is not
  available: "an action is effective at the start of its date, and that day's trades are
  quoted in post-action terms" is the premise every other formula here rests on.
- **The corrected ratio is generally not enterable.** Under weighted average the cash leg
  disposes of `f × N` **shares**, so the EXCHANGE carries only `(1−f) × N` and the published
  ratio over-delivers. The ratio that is actually correct is `B_received / ((1−f) × N)`, which
  is generally **not** expressible as two positive integers — which E6a / D14 requires.
- **Booking the whole event as one EXCHANGE** moves 100% of the basis, leaves the cash booked
  **nowhere** (not a receipt, not realized P&L, not an XIRR flow), overstates the
  destination's average, and **passes the conservation law**, because the money simply left
  the book (§4.4.1's blind spot). The naive treatment is wrong *and* undetectable — which is
  why the exclusion is stated rather than silent.

> **Unofficial workaround, if the event ever occurs.** EXCHANGE **all** shares to the
> destination, then SELL the **destination** for the cash consideration (priority 10 then 30,
> so the ordering works and no guard trips). **It is not normative and must not be cited as
> normative**: its realized figure differs from the relative-consideration allocation (the
> disposal is priced at the destination's terms rather than by splitting the basis between the
> two considerations), so the tax package will report a different gain from the one the
> registrar's allocation implies. Use it knowing that, or record the event and ask.

> **Verification anchors**: the three formulas and the refusal paths are anchored by phase-1's
> `CA*` scenario — **13 absolute `corp.anchor.*` anchors** (11 at `phase1:anchor`, 2 at
> `phase1:corp_refused`), `corp.refusal_codes` (`corp_applied` = `[]`; `corp_refused` =
> `["E5","E2","E1"]`),
> `corp.anchor.e5_source_unmoved` (`tw_broker/CAX.shares = −500` — a refused action **changes
> no money at all**, only the disclosure), `corp.anchor.e5_source_flagged`
> (`unbookable_action = True`), `corp.e1a_dashboard_200` (an action on a never-held symbol
> **must not 500**), `corp.xirr_blanked_by_unapplied` (3 unapplied actions → `xirr` is
> `None`), and `corp.xirr_reason_names_row` (the reason string must name the **account, symbol
> and date**).
> **D11 / D12 / D34 currently have no stress anchor** (neither `volume` nor a reorganisation
> fee is exercised by this scenario, and a cash-and-stock merger is by definition not
> enterable); the next adversarial round should at minimum add a negative anchor for "a
> reorganisation-fee `WITHDRAW` does not move XIRR".
> **Implementation**: `shared/corporate_actions.py` (`CorporateAction`, `apply_ratio`,
> `split_factor`, `ActionIndex` — the single owner of the ratio algebra),
> `shared/ledger_events.py::EventPriority`, `shared/ledger_registry.py::LEDGER_TABLES`,
> `portfolio/cost_basis.py::build_book` (`_apply_action`, `_reject`,
> `UnbookableLedgerError`, `Book.unapplied_actions`), `portfolio/results.py::UnappliedAction`,
> `data_ingestion/store.py` (the only SQL),
> `data_ingestion/validate.py::validate_corporate_action`, `data_ingestion/holdings.py` (the
> action-aware share path; `shares_naive` stays deliberately **unaware**, because
> `corporate_delta` is defined as the difference between the two), `pricing/schema.py` +
> `pricing/store.py` (`close_raw` / `split_basis`), `pricing/reconcile.py` (restating stored
> closes when the SPLIT ledger changes), `portfolio/price_basis.py` (read-time
> re-expression).
> **Basis**: `docs/spec/2026-08-06-corporate-actions.md` (§2 conservation law, §3 data model
> and ratio, §4 replay semantics and the field-transfer table, §5 edge matrix, §8 decisions
> D1–D39, §9 exclusions), `.claude/rules/domain-ledger.md` (Cost basis; STICKY 賣超; Declared
> short sale), `.claude/rules/data-and-pricing.md` (precision; never store a rounded
> quotient).

---

## 5. Realized / Unrealized P&L

### 5.1 Realized P&L

Each **sell** produces a `RealizedRow` (`cost_basis.py`, `kind="sale"`; a second kind,
`kind="dividend"`, is described in §6.3b):

$$\text{proceeds\_net} = \text{quantity}\times\text{price} - \text{fees} - \text{tax}$$

$$\boxed{\text{realized} = \text{proceeds\_net} - \text{adjusted\_removed}}$$

i.e. **net sale proceeds (after fees and tax) − the sell fraction's
`adjusted_avg × shares_sold`**. Realized is measured against **adjusted cost** (dividends
are already folded into cost, so no separate dividend income line → invariant I4, avoiding
double counting). Cross-currency is aggregated by currency in `RealizedPnL.by_currency`.

> **How corporate actions (§4.4) relate to realized P&L.** A corporate action emits **no
> `RealizedRow`** of its own — it is not a disposal, not income, not a purchase, only a
> re-labelling and re-denomination of an existing basis (§4.4.1's conservation law). It does
> change the realized amount of **every subsequent sell**, because
> `adjusted_avg = adjusted_total / shares` is divided **on read**: after a split the totals
> are unchanged and the share count is not, so the average scales by `1/ratio` automatically.
> **Two real exceptions** do produce realized rows, and both are booked *outside* the action
> row: **cash in lieu** is an ordinary SELL (§4.4.3; verified example §4.4.5(e),
> `realized = −20.2127659574468085106382979`), and a **reorganisation fee** is a `WITHDRAW`
> cash movement (§4.4.7, limitation 2). When arbitrating any post-action realized amount, the
> `corporate_actions` ledger must be included in the replay (§1.4, §12.4 step 2), or the
> result is a number that **looks entirely normal and is computed on pre-action share
> counts**.

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
  - Fix: enter the missing opening inventory / buy, **or the missing corporate action**
    (§4.4) — one unrecorded 3-for-1 split makes every subsequent sell read as an oversell, and
    賣超 is **STICKY**: a later buy does not bring the discarded basis back.
- **An unapplied corporate action (`unbookable_action`, 2026-08)**: the third member of the
  same honest-degradation family. Strict path raises `UnbookableLedgerError`; the dashboard
  path **skips the event** and flags the position `unbookable_action` (**to be clarified**). A
  skipped action **changes no money at all**, only the disclosure — the share count stays in
  **pre-action** terms while the prices it meets are **post-action**, which is why the
  position is 待釐清 rather than merely stale. The record lives on **`Book.unapplied_actions`
  (the book)**, not only on a holding, because two of the three ways it happens **leave no
  holding to flag** (E2's source is zero-share and dropped, E1's never existed). Whenever
  `unapplied_actions` is non-empty, **the share counts in that book are not trustworthy**:
  XIRR therefore blanks for the **whole portfolio** (§7.2), and the reason string must name
  the **account, symbol and date**. The predicate and the full refusal list are in §4.4.6.

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

### 6.3b Cash Dividend Paid After the Position Closed (`CASH` / `NET`) — Realized Income

**Rule (audit H2, 2026-07-26; applies to TW `CASH` and MY `NET`)**: when a cash dividend's
payment date falls **after** its `(account, symbol)` position has already reached zero
shares (TW/MY pay weeks after the ex-date, so selling out in between is ordinary), there is
**no cost basis left to reduce**, and the net is booked as one **realized** row
(`RealizedRow`, `kind="dividend"`):

$$\text{realized} = \text{proceeds\_net} = \text{net},\qquad
\text{shares\_sold} = \text{original\_removed} = \text{adjusted\_removed} = 0$$

$$\text{sell\_date} = \text{the dividend's payment date}$$

**The test is the share count at the moment of the event** (same-day order: opening 0 →
buy 1 → sell 2 → dividend 3):

| Case | Shares at payment | Handling |
| --- | ---: | --- |
| Dividend while held | > 0 | `adjusted_total −= net` (§6.1 / §6.3, unchanged) |
| Dividend after a partial sale | > 0 | Same, against the remaining position (unchanged) |
| **Dividend after full close** | **= 0** | **One realized row (`kind="dividend"`)** |
| Closed, re-bought, then paid | > 0 | Reduces the **new** position's cost (not a special case) |

**No double counting (invariant I4 holds)**: the two paths are mutually exclusive — either
cost reduction or a realized row, exactly once. The `dividends` ledger itself (the dividend
overview) and the XIRR cashflow series (§7.2) each already counted the payout; after the
fix all three agree.

**Tax separation**: a `kind="dividend"` row is **not** a capital gain. The annual tax
package's `realized_gains_{year}.csv` takes `kind == "sale"` only; the payout is already
reported by `dividends_{year}.csv` from the dividend ledger (`export/tax.py`), so it is
never filed twice.

**Verification anchor**: `moomoo_my/5225` buy 200@6.00 (2026-05-04) → sell 200@6.50
(2026-05-20, position → 0) → `NET` dividend 120 (2026-06-16) → the payout enters
`realized.by_currency[MYR]` (`scripts/stress_audit/run_phase1.py`, "Found-bug op #3";
hermetic regression in `tests/portfolio/test_post_close_dividend.py`).

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

> **A corporate action does not move the denominator (§4.4).** `gross_invested` accumulates
> from `opening` and `buy` only, so **none of the three formulas touches it**: no new capital
> entered, and counting an action as invested capital would be a fabricated double count. Nor
> does it move the numerator — the action emits no realized row (§5.1) and does not change
> total unrealized (the totals are unchanged, the share count is not, and the average scales
> on read). So **a corporate action does not change `total_return`; it only changes how that
> return is distributed across tickers** — which is exactly what §4.4.1's conservation law
> asserts.

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

> **Corporate actions and XIRR (§4.4).** A corporate action is **not a cash flow**: it has no
> row in the table above, because it moves no money. It reaches XIRR only through the
> **terminal market value** row (the share count changed), which is the correct economics.
>
> **But one *unapplied* action blanks XIRR for the whole portfolio**
> (`unbookable_action` / `Book.unapplied_actions`, §5.3). This is a **deliberate blast
> radius**: everywhere else a skipped action damages exactly one stock, but XIRR is a
> **single figure** whose terminal value sums **every** holding, so one pre-action share count
> makes the whole sum wrong. The degradation **must name the row** — the reason string carries
> the account, symbol and date (anchor `corp.xirr_reason_names_row`) — or the owner has to
> hunt it across a multi-account book. Measured anchor: 3 unapplied actions → `kpis.xirr` is
> `None` (`corp.xirr_blanked_by_unapplied`, `phase1:corp_refused`).
>
> **The reorganisation fee (D12 / D36) is invisible to XIRR by design.** The flow series above
> is built from `opening` + `transactions` + `dividends` **only**, so cash movements
> (including a reorganisation fee booked as `WITHDRAW`) never reach XIRR; XIRR is
> **deliberately left untouched** so that every anchored historical figure in this manual
> stays where it is. The fee surfaces in a **whole-account IRR** instead — **⚠ that metric is
> not implemented yet (pending D36; `portfolio/twr.py` was checked in place on 2026-08-11 and
> still holds only the TWR index and the benchmark overlay)**. Full treatment in §4.4.7,
> limitation 2.

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

#### 7.2.1 Observation-window floor: below 30 days the figure is not annualized (owner ruling 2026-08-05)

XIRR **annualizes**, so its exponent is `365 / window_days` and a short window amplifies
violently. For a position whose book gain is `+131.7%` (cost 1,001,425 → value 2,320,000):

| `window_days` | annualized XIRR | digits |
| ---: | ---: | ---: |
| 1 | `1.5 × 10^133` | 136 |
| 14 | `325,589,627,815%` | 12 |
| **30** | `2,749,353%` | 7 |
| 90 | `2,918%` | 4 |
| 365 | `132%` | 3 |

Every value above is the arithmetic working **correctly** on a degenerate input — it is
simply not readable as a return rate. Therefore:

- **When `window_days < 30`, `kpis.xirr` is `null`**, and
  `freshness.xirr_unavailable_reason` states 「觀察期 N 天・不足以年化（需 ≥30 天）」.
  The boundary is **inclusive**: 30 days still annualizes, 29 days does not.
- **What is withheld is the annualization, never the return.** `total_return_rate` (§7.1)
  is untouched and stays on the wire, carrying the same information un-annualized, so the
  KPI band is never left blank.
- `xirr_window_days` is reported in **every** case, including when the rate is `None`;
  the existing short-window hint still applies over 30–365 days.
- **The calculation itself is unchanged**: `returns.py::xirr_reporting` builds and solves
  the same cashflow series, and the stress-audit oracle's anchors are unaffected. This
  section governs a **presentation threshold**, not the formula.
- **Origin**: found 2026-08-05 on a freshly reset instance — after the first trade was
  entered, `window_days = 1` made the dashboard render a 136-digit value as the headline
  return and pushed the layout 1,915px sideways. Implemented at
  `portfolio/dashboard.py::_XIRR_MIN_WINDOW_DAYS`; regression in
  `tests/contract/test_xirr_short_window.py` (both sides of the 30/29 boundary).

> **Implementation**: `portfolio/returns.py` (`total_return`, `xirr_reporting`),
> `portfolio/results.py` (`ReturnSummary`, `CurrencyReturn`),
> `portfolio/dashboard.py` (the §7.2.1 presentation threshold).
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

Implementation: `forex/pools.py::average_acquisition_rate` / `acquisition_basis`. There are
**two** acquisition sources (spec 2026-07-30): `home → foreign` conversions, and **foreign
cash INFLOWS that carry an `acq_home_amount`** (`cash_movements` DEPOSIT/OPENING/REBATE;
WITHDRAW is a disposal and is not an acquisition):

$$\text{avg\_rate} = \frac{\sum \text{from\_amount}\ (\text{home}) + \sum \text{acq\_home\_amount}}{\sum \text{to\_amount}\ (\text{foreign}) + \sum \text{amount}_{\text{with basis}}}\quad(\text{None when no acquisition carries a cost})$$

**Store the AMOUNT, not the rate.** `acq_home_amount` is a **home-currency amount**: a rate is
an average, and §1.3 forbids an average as the stored authority (`fx_conversions` likewise
stores two amounts). The displayed acquisition rate is always `acq_home_amount / amount`,
computed on read.

**Covered ratio.** A foreign inflow **without** a cost basis has an unknown rate that is
**never guessed**. Cash is fungible and weighted average tracks no lots, so outflows are
absorbed **pro rata**:

$$\text{covered\_ratio} = \frac{\sum \text{amount}_{\text{with basis}}}{\sum \text{amount}_{\text{with basis}} + \sum \text{amount}_{\text{no basis}}}\quad(\text{the literal } 1 \text{ when nothing is unbased})$$

It must **not** be "total balance − unbased amount": that expression goes **negative** as soon
as the balance drops below the unbased amount, recreating the reversed-sign figure this design
removes. When the ratio is exactly 1 the caller **skips the multiply**, so a fully covered
ledger is **byte-identical** to the pre-spec engine.
Anchors: `fx.covered_ratio scope = schwab / moomoo_my` (both **1** in phase1);
`fx.basis_gap` (both **0**).

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

$$\text{unreal\_stocks} = \text{foreign\_stock\_value}\times\text{covered\_ratio}\times(\text{spot} - \text{avg\_rate})$$

$$\text{unreal\_cash} = \text{foreign\_cash}\times\text{covered\_ratio}\times(\text{spot} - \text{avg\_rate})$$

**One ratio applies to the WHOLE foreign exposure (both the cash and the stock leg)**:
`avg_rate` itself comes from the with-basis population, so scaling only the cash leg would
leave the **larger** error — the stock leg — unflagged (measured: +42,359 TWD). When
`covered_ratio < 1`, `fx_basis_incomplete = true` and
`fx_basis_gap = foreign_cash × (1 − covered_ratio)`, and BOTH legs are flagged. Realized FX is
not scaled (the proceeds really were received), but its cost side uses the same incomplete
average, so it is flagged too.

where `foreign_cash` is the foreign balance from the **FX-exposure perspective**. Since spec
2026-07-30 it **also counts foreign cash inflows/outflows**, so for the same
(account, foreign ccy) it now **equals §9's operating cash pool** (the two diverged
deliberately before — see audit C9); the remaining difference is only the **cost basis**: an
inflow without `acq_home_amount` counts toward the balance but not toward the weighted
average. `avg_rate is None` or `spot is None` → unrealized = `None`.
Anchors: `fx.foreign_cash scope = schwab` (**25,800**) / `moomoo_my` (**−11,244.14**);
`fx.pool_equals_funds` (phase2).

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
- **Rebuild (重算)**: **fully rebuild** all statistics from the five ledgers (see §1.4;
  including `corporate_actions`, §4.4).

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

> **Numbering note**: the `E#` here are **worked-example** numbers. They are a separate
> sequence from the `E#` of §4.4.6's edge-case matrix and do not correspond to them.

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
| E18 | **SPLIT** (CA1 3-for-1 + same-day sell → 260 shares; realized 144.666…669) | §4.4.5(a) | `corp.anchor.split_forward`; `realized.realized tw_broker/CA1@2026-03-02` |
| E19 | **SPLIT leaves the dividend portion alone** (CA9 2-for-1 → 400 shares; `dividend_portion` 4,000 unchanged) | §4.4.5(b) | `corp.anchor.split_shares_dividend_adj`; `corp.anchor.split_keeps_dividend_portion` |
| E20 | **EXCHANGE** (CA3→CA4 1-for-2 into a held position → 90 shares / 40,056) | §4.4.5(c) | `corp.anchor.exchange_merge`; `export.holdings.original_cost_total tw_broker\|CA4` |
| E21 | **SPINOFF** (CA7→CA8 1-for-4, `c=0.30` → child 100 shares / 30,042.60; parent 500 shares / 89,126.40) | §4.4.5(d) | `corp.anchor.spinoff_child_basis`; `corp.anchor.spinoff_parent_basis` |
| E22 | **Reverse split + cash in lieu** (CA2 1-for-10 → 70.5 shares; the 0.5-share ordinary sell realizes −20.2127…979) | §4.4.5(e) | `corp.anchor.split_reverse`; `realized.realized tw_broker/CA2@2026-03-12` |
| E23 | **Ratio exactness** (CAR `210 × 1 / 3 = 70`; the sell of all 70 commits with 201) | §4.4.5(f) | `corp.anchor.split_ratio_exact`; `corp.sell_exact_ratio_accepted` |
| E24 | **Degradation of an unapplied action** (CAX shares −500 unmoved, flag True; XIRR blanks portfolio-wide and the reason names the row) | §4.4.6 / §5.3 / §7.2 | `corp.anchor.e5_source_unmoved`; `corp.anchor.e5_source_flagged`; `corp.xirr_blanked_by_unapplied`; `corp.xirr_reason_names_row` |

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
| 公司行動 (corporate action, ledger) | `corporate_actions` / `CorporateAction` | §4.4 |
| 分割／換股／分拆 (split / exchange / spinoff) | `SPLIT` / `EXCHANGE` / `SPINOFF` (`CorporateActionKind`) | §4.4.4 |
| 比例 (ratio, two positive integers) | `ratio_to` / `ratio_from` (sole application exit `apply_ratio`) | §4.4.2 |
| 分拆基礎移轉比例 (spinoff basis carve fraction) | `cost_carry` | §4.4.4 |
| 同日優先序 (same-day priority) | `EventPriority` (`OPENING`/`CORPORATE_ACTION`/`BUY`/`SELL`/`DIVIDEND`) | §4.1 / §4.4.3 |
| 未能套用之公司行動 (unapplied corporate action) | `unbookable_action` / `UnappliedAction` / `Book.unapplied_actions` | §4.4.6 / §5.3 |
| 行情原值／分割基礎 (raw close / split basis) | `close_raw` / `split_basis` (the two-column price basis) | §4.4.7 |

### 12.3 Version History

| Version | Date | Notes |
| --- | --- | --- |
| `v1.0-draft` | 2026-07-15 | First draft. Baseline `v0.1.18 + feat/p3-batch3`. Reconciled against 966 adversarial assertions (966/966 passing). **Pending owner confirmation as the arbitration standard.** |
| `v1.1-draft` | 2026-07-15 | **Adversarial completeness audit**: after a repo-wide census of every amount / ratio / metric calculation, filled in the missing class-A formulas — added §6.5 (dividend-detection inbox estimation: pre-ex-date entitlement, DRIP reinvest-price estimate, TW stock-dividend par-value-10 conversion), §7.1 blended reporting-currency return rate + monthly snapshot, §7.3 (single-holding / sector / market allocation weight, currency view, reporting-currency valuation rule, export TOTAL rows, tax realized converted at sell-date FX), §7.4 (dividend-income summary + annual projection), §7.5 (net-value and cumulative-invested trend), §11.4 (rebalance turnover / fees / projected balance + leg amounts), §11.5 (What-if simulation). Added §12.5 "Inventory of numeric formulas outside arbitration scope", itemizing all class B (technical indicators / alert thresholds / export ratios) and class C (LLM budget / spend), achieving "complete enumeration". Baseline unchanged; **still pending owner confirmation.** |
| `v1.2` | 2026-07-15 | **Formally signed off by the owner as the arbitration standard, effective from v0.1.19** (removed the "pending owner confirmation" draft status; version leaves -draft). Folds in the owner's four rulings: ① added the English mirror `docs/accounting-formula-manual.en.md` (a working copy for AI/agent reading; the zh manual is the arbitration authority, and each zh change must regenerate the mirror in the same change set); ② this activation (this row); ③ the §11.1 rebalance ruling's canonical date is set to **2026-07-13** (the ship record's 07-14 was only the ship date); ④ §3 rate honest statement: the owner's complete schedules are on file (→ `docs/reference/broker-fee-schedules-2026-07.md`), superseding the seed rates at the fee-engine-v2 upgrade; until then §3 documents what the current engine computes and lists the known divergences (sec_fee 0.0000278→0.0000206, TAF/CAT/platform/settlement not modeled, MY schedule shape differs, TW Capital Securities (群益) 23%-of-list charge-first-refund-later + rounding divergence), and a fee-dispute note was added to §12.4; ⑤ the §7.3 / §12.5 boundary ruling is settled (weights / return rates remain within arbitration scope). Baseline unchanged. |
| `v1.3` | 2026-07-15 | **fee-engine v2 shipped** (owner sign-off; §3 fully rewritten). ① **TW rounding FE-D3**: fee/tax switch from round-half-up to **unconditional floor (ROUND_DOWN) to integer NT$**, with the min-NT$20 compared after the floor (群益 142.5→142; day-trade tax example 11→10); ② **US regulatory v2**: Schwab / Moomoo US commission $0 / platform $0.99, SELL adds SEC `0.0000206` + TAF `0.000195` (cap $9.79), settlement `0.003/share` (cap 1%), CAT `0.000003/share` — each component rounded then summed; ③ **MY v2**: commission `0.03%` (min RM0.01) + platform RM3 + clearing (cap RM1,000) + **SST 8%**; stamp becomes `ceil(amount/1000)×RM1` (stock cap RM1,000, **ETF exempt**); ④ **FE-D2 US stamp**: the MY stamp on US trades is computed in MYR, booked in USD (`stamp_fx` resolved by the caller; no rate → 0 + soft issue); ⑤ **FE-D1 rebate**: new §3.6 forecast `⌊fee×0.77⌋` (**not a number of record**, never in `compute_fees`; inbox/confirm is Wave B); ⑥ the snapshot carries `engine="v2"`, a **per-row regime** (old rows arbitrated under their old snapshot, never recomputed). All rates live in config. §3 example anchors updated to fee-engine v2 stress phase1 (`fee_engine.*` 80/80). Mirror regenerated in the same change set. Baseline unchanged. |
| `v1.4` | 2026-07-22 | **Batch B (Moomoo merge) revision** (baseline `v0.1.20 + Batch B`). ① **Account model**: the two former per-market Moomoo accounts (legacy ids documented in `data_ingestion/moomoo_merge.py`) are merged into ONE dual-market account `moomoo_my` (settlement USD / funding MYR; rules bind per (account, market): US→(`moomoo_us`,`drip_us`), MY→(`moomoo_my`,`cash`), held in `account_market_rules`) — §2 account table 4→3 rows, invariant I6 changed from "bind to account" to "bind to (account, market)", and the account labels + `scope` anchors in §3.3/§3.4/§6.2/§6.3/§8/§9 re-anchored onto `moomoo_my` (market carried by the symbol). ② **Full anchor re-reconciliation**: the stress suite was regenerated to the post-merge topology (1,060 assertions, 66 ops, 1,060/1,060 passing, 0 fail; spot USD/MYR 4.5→**4.6**, plus one Schwab USD→TWD reconversion). Scenario-dependent terminal values updated to this current run: §7.1 total return 514,752.85→**516,336.55** (realized 186,333.50 / unrealized 330,003.05), §8.2 realized FX 0→**−2,375** (Schwab reconversion), §8.3 unrealized FX rollup −31,830.94→**−11,757.48** (`moomoo_my` now contributes a positive leg because spot 4.6≠avg 4.5), §9.2 cash pools fully updated with the MYR pool now a single directly-anchored `moomoo_my|MYR = 123,201.91`, §5.1 TSLA proceeds/realized 5,199.86/199.86→**5,199.88/199.88** (SEC fee 0.14→0.12); fixed pre-existing typos E5 (NVDA fee 1.41→5.89) and E6 (1155 fee/tax 10.45/9.50→9.40/10.00). ③ **Anchor robustness**: the volatile `id=NN` (renumbered per release) removed from the §12.1 fee examples, keeping the stable check+scope; the `negative_cash` example (former op47) — no longer triggered by the scenario — is re-anchored to unit tests (§9.3/E16); the oversell anchor is stated via the `guard.oversell_blocks` scope. ④ Verification-basis line, §7.2 harness count (1,006→1,060), §6.5 count (966→1,060) updated. Mirror regenerated in the same change set. **No formula or accounting-definition change — purely a (account, market) binding relabel + anchor re-reconciliation.** |
| `v1.5` | 2026-07-26 | **A cash dividend paid after the position closed is booked as realized income** (audit H2, owner ruling 2026-07-26; baseline `v0.1.24`). New **§6.3b**: when a CASH/NET dividend lands while its `(account, symbol)` position is already at zero shares there is no cost basis left to reduce, so it becomes one `RealizedRow(kind="dividend")` (`realized = proceeds_net = net`; shares_sold / original_removed / adjusted_removed all 0; `sell_date` = the payment date). Before the fix the payout was absorbed by the zero-share position and discarded with it, so the dividend overview and the XIRR cashflows counted it while total return did not — three figures, three answers. It is now counted exactly once and **invariant I4 holds**. §5.1 notes that `RealizedRow` now carries `kind: "sale" | "dividend"`. **Tax separation**: the annual package's `realized_gains_{year}.csv` takes `kind == "sale"` only — the payout is already reported by `dividends_{year}.csv` from the dividend ledger, so it is never filed twice. Verification anchor: `moomoo_my/5225` buy 200@6.00 → sell 200@6.50 (position → 0) → NET dividend 120 enters `realized.by_currency[MYR]` (run_phase1 "Found-bug op #3"; stress ops 66→**69**, assertions 1,060→**1,088**, fail=0); hermetic regression `tests/portfolio/test_post_close_dividend.py` (5 cases, including closed → re-bought → paid, which still reduces cost). **Effect on a real ledger: the historical total return of already-closed symbols RISES** (previously dropped payouts now count). Mirror regenerated in the same change set. No other formula changed. |
| `v1.6` | 2026-08-01 | **Cost basis for foreign cash inflows + the declared short sale** (owner rulings 2026-07-30 / 07-31; baseline `v0.1.25`). ① **§8.1/§8.3 rewritten**: acquisitions widen from "conversions only" to "conversions **+** foreign cash inflows carrying `acq_home_amount`"; the **AMOUNT is stored, never the rate** (a rate is an average and §1.3 forbids an average as the authority; the displayed rate is computed on read). New **`covered_ratio`** (with-basis acquisitions / all acquisitions) absorbs outflows **pro rata** — "total balance − unbased amount" is forbidden (it goes negative once the balance drops below the unbased amount, recreating the reversed-sign figure). The ratio scales **both** the cash and the stock leg (`avg_rate` itself comes from the with-basis population; scaling only cash left the LARGER error — the stock leg, +42,359 TWD measured — unflagged). When the ratio is the literal 1 the caller skips the multiply, so a fully covered ledger is **byte-identical** to the pre-spec engine. `foreign_cash` now counts foreign cash inflows/outflows too, so for the same (account, foreign ccy) it **equals §9's operating cash pool** (they diverged deliberately before, audit C9); only the cost basis still differs. ② **New §4.3, declared short sale**: `short_sale` (default false, **never inferred**); a declared sell exhausts the long lot then opens a short lot holding the net proceeds, a buy covers first then adds to the long, long and short are mutually exclusive so a position is **one signed quantity**; cover P&L is `(short_avg − the covering buy's all-in per-share cost) × covered`, dated the **cover date**, `kind="short_cover"` (it reaches the tax capital-gains sheet). Ratios must divide by `abs(cost_total)` and `fully_recovered` is gated on `not short_open` (a short's basis is negative by construction). **A dividend during an open short is unbookable** (the short pays it; strict path raises `UnbookableLedgerError`, the dashboard skips and flags `unbookable_dividend`). Ruled limitations: `gross_invested` excludes short capital, a pure short XIRR reports a borrowing rate, weights use a net-exposure convention. ③ **The 賣超 guard is now DATE-AWARE** (`shares_through(trade_date)`, mirroring cash's `running_min`) and `oversold` is **sticky** (a later buy does not clear it, because it does not restore the discarded basis). Anchors: the full `tw_broker/2609` short lifecycle (see the §4.3 table) and `fx.covered_ratio/basis_gap/foreign_cash`; stress ops 69→**77**, assertions 1,088→**1,806**, fail=0; phase 2 (live demo) 1,192 assertions fail=0. Mirror regenerated in the same change set. |
| `v1.7` | 2026-08-11 | **Corporate actions (SPLIT / EXCHANGE / SPINOFF)** (owner decisions D1–D39, spec `docs/spec/2026-08-06-corporate-actions.md`; baseline `v0.1.28 + feat/corporate-actions`). ① **New §4.4**, placed under §4 Cost Basis after §4.3, **renumbering nothing** — the spec's own §7.5 names "§5 Realized/Unrealized P&L" and "§7 Total Return" by their current numbers in the same sentence, and renumbering would invalidate that reference together with every existing §5.1/§7.2-style citation across the repo: the ledger row and the **conservation law** (Σ`original_total` / Σ`adjusted_total` / Σ`dividend_portion` / `gross_invested` all unchanged; the value leg **SPLIT-only**) plus the two deliberate exceptions (cash in lieu = ordinary SELL, reorganisation fee = `WITHDRAW`); **the ratio is two positive integers** and `qty × to ÷ from` is **multiply-first** (measured `210×1/3 = 70` vs `210×(1/3) = 69.999…9`, which `validate.py`'s bare `>` turns into an oversell → STICKY basis discard); **the three formulas and the nine-field `_Position` transfer table are quoted verbatim from spec §4.1–§4.4** (the manual never restates a formula in its own words); D21's provenance label on a spun-off child's payback progress; **six verified worked examples** (§12.1 E18–E24); the **edge matrix E1–E24** (including the oversell / declared-short interactions E3/E4/E5/E18/E22 and E24); the price basis (`close_raw` / `split_basis`, read-time re-expression, **SPLIT-scoped**). ② **§1.4 / §1.1 / §12.4: the permanent ledgers go from four to five** (adding `corporate_actions`) — omitting it from a replay yields an amount that looks normal and is priced on pre-action share counts. ③ **§4.1's same-day priority changes from `0/1/2/3` to `EventPriority`'s `0/10/20/30/40`**, inserting the action between `OPENING` and `BUY`; the **relative order is unchanged**, so a ledger with no action replays byte-identically. ④ **Cross-references**: §5.1 (an action emits no `RealizedRow`), §5.3 (`unbookable_action` as the third honest degradation), §7.1 (`gross_invested` untouched), §7.2 (an action is not a cash flow; an unapplied action blanks XIRR **portfolio-wide** and the reason must name account / symbol / date). ⑤ **Two limitations**: **D11** (`volume` is not un-adjusted) is stated as **standing**; **D12** (the reorganisation fee) is **not** a permanent blind spot — D36 leaves XIRR **deliberately untouched** and adds a **whole-account IRR** in `portfolio/twr.py`; checked in place for this revision, that file still holds no IRR, so it is written up as "**pending D36**". ⑥ **D34: cash-and-stock mergers are a hard exclusion**; the spec's former two-row recipe is withdrawn and **must not appear as a procedure** (`CORPORATE_ACTION(10)` precedes `SELL(30)` → the EXCHANGE zeroes the source → the same-day SELL lands on a zero-share position → STICKY 賣超); the nearest expressible form is recorded as an **unofficial workaround** with its inexactness stated. ⑦ Verification basis updated to the current run (phase-1 **118 ops / 3,791 assertions / 0 fail**; phase 2 **1,192 / 0 fail**), with all 23 `corp.*` corporate-action assertions passing. Mirror regenerated in the same change set. **No existing formula or accounting definition changed other than the above.** |

### 12.4 How to Arbitrate a Disputed Amount

Given an amount "displayed as X on the site but believed to be Y":

1. **Locate the amount type** → the corresponding section: fee/tax §3; holding cost /
   average §4; **corporate actions (split / exchange / spinoff) §4.4**;
   realized §5.1; unrealized / capital gain §5.2; dividends §6;
   **dividend-detection estimate §6.5**; total return / return rate (incl. blended) §7.1;
   XIRR §7.2; **allocation weight / sector / currency view / reporting-currency valuation /
   tax realized §7.3**; **dividend-income summary / annual projection §7.4**; **net-value
   and invested trend §7.5**; FX P&L §8; cash balance §9; rebalance §11 (**rollup §11.4;
   What-if §11.5**). If the number is none of the above → check §12.5 whether it is
   out-of-scope class B / C (technical indicator, alert threshold, LLM budget).
2. **Pull the relevant ledger rows** (the five permanent ledgers):
   - fee/tax, cost, realized, unrealized → `transactions` (that account×symbol, **sorted
     by `trade_date`**) + `dividends` + `opening_inventory` + **`corporate_actions` (that
     account; take rows where the symbol appears as `from_symbol` **or** `to_symbol` — an
     exchange carries a basis in from **another** ticker)**.
   - FX P&L → that account's `fx_conversions` + `fx_rates` (current spot).
   - cash → `cash_movements` + `fx_conversions` + that pool's `transactions` + cash
     dividends.
3. **Replay step by step per that section's formula** (rebuild). Be sure to apply: the
   same-day priority `OPENING(0) ≺ CORPORATE_ACTION(10) ≺ BUY(20) ≺ SELL(30) ≺
   DIVIDEND(40)` (§4.1), the sell **proportional removal**, the dividend model (§6), **the
   three corporate-action formulas and their multiply-first ratio (§4.4.2 / §4.4.4)**, and
   the precision rules (§1.3, store full precision, quantize only at settlement / display).
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
