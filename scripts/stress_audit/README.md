# Stress-Audit Harness (壓力驗證 + 帳目可信度)

A permanent, versioned, one-command harness that stress-tests portfolio-dash's
**money-of-record** calculations and proves the ledger is trustworthy. It drives the real
app through realistic operation sequences and reconciles every derived figure against an
**independent accounting oracle** that shares no code with the app.

Run it whenever you want to re-launch the full loop, and **grow it** every time a bug is
found or a money feature ships (see [Accumulation rules](#accumulation-rules)).

```
scripts/stress_audit/
  oracle.py            # INDEPENDENT Decimal oracle — imports NOTHING from portfolio_dash
  common.py            # evidence log, Decimal assertions, httpx client, uvicorn launcher, fact loaders
  phase1.py            # clean-room scenario + reconciliation (holdings/realized/cash/FX/KPI/XIRR)
  phase2.py            # live-demo additive scenario + baseline/delta reconciliation
  run_phase1.py        # phase-1 runner (the deterministic op sequence)
  run_phase2.py        # phase-2 runner (UI-first, additive on the demo)
  run_phase2_inbox.py  # phase-2 dividend-inbox 確認 flow (additive)
  ui.py                # Playwright driver (real browser forms + DOM read-back)
  run_all.py           # one-command entry point (--phase 1|2|all)
  evidence/            # regenerated per run, GIT-IGNORED (never committed, never trusted stale)
```

## How to run

Always run with the **repo `.venv` python** (so the spawned uvicorn uses the project's
deps) and `PYTHONIOENCODING=utf-8`. From the repo root:

```bash
# Phase 1 — clean-room, local, safe (the default). Own uvicorn, fresh DB, scheduler off.
.venv/Scripts/python.exe scripts/stress_audit/run_all.py --phase 1

# Phase 1 with the browser happy-paths + DOM read-back (Playwright):
.venv/Scripts/python.exe scripts/stress_audit/run_phase1.py --ui

# Phase 2 — investor-realistic stress on the LIVE demo (mutating, additive). The real
# demo URL lives in docs/human_noted/ (never committed); pass it explicitly:
.venv/Scripts/python.exe scripts/stress_audit/run_all.py --phase 2 --base-url https://<demo-from-human_noted>
```

Phase-2 data is intentionally **left in place** (`--keep-data`, the default) — the harness
is additive and never resets the demo. Phase 1 rebuilds its DB clean on every start.

## Methodology

### 1. Independent oracle (the core of the credibility claim)
`oracle.py` **imports nothing from `portfolio_dash`.** Every accounting formula is
re-derived from the rule documents:
- `.claude/rules/domain-ledger.md` — cost basis, dividend models, realized/unrealized
  P&L, XIRR cashflow signs, the FX-conversion pool.
- `.claude/rules/markets-and-fees.md` — the per-account fee/tax skeletons.
- `.claude/rules/data-and-pricing.md` — Decimal precision + per-currency minor units.

Numeric **parameters** (fee rates, min fees, minor units) are transcribed from the app's
seeded config as constants — parameters-from-config is allowed; the **logic is the
harness's own**. Because the two implementations never share a code path, they agree only
when *both* are correct: a bug in the app cannot hide behind a shared helper.

The corporate-action spec's §6.0 says "ONE owner per concept", and **this file is the one
deliberate suspension of that rule** (§7.4). The ratio algebra, the event-priority enum and
the §4.4 field table are duplicated here ON PURPOSE; `oracle.py` states the reason in its
own docstring so a future cleanup pass does not "de-duplicate" the oracle into uselessness.

**Corporate-action entry surfaces are W7 and do not exist yet**, so the scenario writes
`corporate_actions` rows through the app's own `insert_corporate_action` as a SETUP fixture
(`common.write_corporate_action`), exactly as instruments and prices are seeded. Move them
onto the API the moment W7 lands: an entry surface never exercised end-to-end is how the
ETF-sell and daytrade tax bugs survived. `load_facts_from_api` likewise does **not** read
actions (no public read endpoint), which is correct only while the demo corpus has none —
D25 seeds one, so that loader must gain them in the same change.

### 2. Two independent layers (keep them separate)
1. **Fee-engine oracle** (`fee_tax`) recomputes expected fee/tax from the rules and
   compares against the app's stored fee/tax.
2. **Bookkeeping oracle** (`replay`) replays the raw ledger **facts** (rows the harness
   wrote / read back) into holdings, realized P&L, cash pools and FX pools. It takes each
   trade's fee/tax as a **given ledger fact** — so bookkeeping correctness is verified
   *independently* of whether the fee engine is right. A single bug can only fail one
   layer, which localizes it.

### 3. Exact Decimal, no tolerance — with one disclosed exception
Every assertion is **exact-Decimal equality** (`Evidence.check`) — no epsilon, no
rounding slack. The single exception is the **reporting-currency XIRR scalar**
(`Evidence.check_close`, tolerance `oracle.XIRR_TOL = 1e-6`): XIRR is a numeric root-find
with no closed form, so the harness runs its **own** Newton+bisection solver over the
oracle's cashflows (built at trade-date FX, terminal at the app's own `as_of`) and asserts
`|oracle_rate − app_rate| ≤ 1e-6`. This closes the previously-open §7.2 XIRR gap. In
practice the observed delta is ~1e-11.

### 4. Four-surface comparison
The same computed truth is checked across every channel a user can see:
- **API JSON** — `/api/dashboard`, `/api/ledgers/*`, `/api/cash`.
- **CSV export** — `/api/export/holdings|realized|ledger` (full source precision).
- **Print-report HTML** — `/api/export/holdings-report|ledgers-report` (display parity:
  quantized + thousands-separated).
- **Browser DOM** — Playwright reads the rendered dashboard/cash cells (`--ui`).

### 5. Phase 2 is UI-first and additive
On the live demo, the harness prefers driving the **real browser forms** (so a broken
confirm handler is caught as a finding), reconciles the demo's **full** current ledger
absolutely, and additionally asserts `post-state == baseline + oracle-predicted deltas`
for touched cash pools and newly-registered instruments. It never deletes pre-existing
demo data. No FX rates are exposed remotely, so reporting-currency blended KPIs (incl.
XIRR) are out of phase-2 scope; every native-currency figure is still reconciled exactly.

### 6. Point-in-time state: CLOSED (2026-08-28)

**The gap, as disclosed on 2026-08-26.** Every reconciliation ran **at the current state** —
`/api/dashboard` holdings, realized rows, cash pools and KPIs against `oracle.replay(facts)`.
Nothing compared the app and the oracle **as at a past date**, because the oracle had no daily
replay: it produced one final `OracleResult`, not a series.

That is not academic, and R6 (the ex-dividend date) is the case that exposed it. A STOCK
dividend booked on its ex-date rather than its payment date changes the share count **only
between those two dates** — the final position is identical either way by construction. So the
scenario's permanent 配股 op genuinely exercises the app's ex-date path while every assertion
compared a point where the two answers coincide.

**Half one (AI-D51, 2026-08-27).** The oracle gained `facts_through` / `replay_through` /
`net_invested_through`, and `phase1.py` gained the `trend.*` family, which samples the daily
replay at **every ledger event date and the day before it** — dividends contributing BOTH
`effective` (the ex-date for a 配股) and `d` (the payment date), so the two edges of R6's window
are sampled by construction — plus `ASOF` and the last point.

| check | price-free? | what it pins |
| --- | --- | --- |
| `trend.start` · `trend.contiguous_days` | yes | the replay's span, dividends on their EFFECTIVE date |
| `trend.net_invested` | **yes** | the subtrahend of **B** (`total_value − net_invested`), AI-D41 |
| `trend.incomplete` | yes | the app's honesty about days it cannot value |
| `trend.total_value` | no | the per-day valuation — see below |

`trend.net_invested` remains the family's first job: it is exactly what AI-D48 moves when B
takes the three cash kinds, so the app and the oracle must move together or this goes red in
between.

**Half two (AI-D59, 2026-08-28) — the per-symbol SHARE COUNT, which was R6's actual 落點.**
The blocker was never the assertion, it was the fixture: `seed_all` seeds exactly ONE close per
symbol, dated `ASOF`, so every earlier day was unpriced by construction, every pre-ASOF day was
`incomplete`, and `trend.total_value` was skipped precisely where the defect lives.

`_ensure_daily_prices` now seeds a close for every fixture symbol on **every day** from the
first ledger event through `ASOF`. At a KNOWN price, `total_value` pins the share count: the
app and the oracle read the same close, so any disagreement that survives is a quantity. Two
properties keep the fixture honest rather than merely green:

* **The closes are AS TRADED on their own date.** `oracle.price_as_traded` multiplies the ASOF
  quote back out by the splits between that day and ASOF — one action at a time, division
  last, never a product of quotients (trap #2a) — which is the invariant
  `data-and-pricing.md` states for the column. Seeding the post-split quote on a pre-split day
  would store a basis the ledger never traded on. It quantizes to 4 dp, deliberately: a ratio
  like `1/3` makes the price a repeating decimal that the capped column cannot hold, and one
  expression owning both the seed and the valuation is what keeps them equal (measured: 91
  `trend.total_value` failures off by 0.0070 before that line existed).
* **Each row is stamped `fetched_at` = its own date**, so the write window
  `(as_of, fetched_at]` and the read window `(priced_on, day]` are both empty. Nothing
  re-expresses these rows — not `upsert_prices`, not `reconcile_prices` — so inserting or
  deleting a SPLIT can never repaint the history this family asserts against.

Seeding is derived from `facts` inside `reconcile`, before the dashboard is fetched, so it is
self-correcting: a checkpoint that runs before the scenario's corporate actions exist seeds an
unadjusted series, and the next one re-seeds the same dates on the corrected basis.

**Detection power, measured — not claimed.** The R6 defect was deliberately reintroduced into
the ORACLE (`facts_through` cutting dividends on the payment date instead of the ex-date, which
is exactly the pre-R6 behaviour) and the harness re-run:

| harness | result |
| --- | --- |
| **pre-change** (one close at ASOF, pre-daily completeness rule) | `ops=128 pass=5663 fail=0` — **silent** |
| **current** (daily as-traded series) | `ops=128 pass=6022 fail=53` |

All 53 are `trend.total_value`, on **2026-05-02 … 2026-05-29** — exactly the ex-date-to-payment
window — each off by 70,000 TWD, the value of the 配股 shares that should exist in that gap.
The pre-change row reproducing the previously-recorded baseline *to the digit* is itself the
evidence that the probe faithfully restored the old state: that `fail=0` was not coverage, it
was blindness.

Baseline after this change: **`ops=128 pass=6075 fail=0`**. `ops` is unchanged and must stay
128 — this closed a gap with fixture data and assertions, not with a new route.

**What is still reviewed rather than reconciled.** The oracle derives the STOCK-only ex-date
rule independently (`DivFact.effective`), and that derivation is now *exercised* by the family
above, but the rule itself — that a 配股 attaches on the ex-date and a CASH dividend does not —
is still a reading of `domain-ledger.md` by both sides. Two independent implementations of the
same misreading would still agree. The hermetic hand-checked tests
(`tests/portfolio/test_review_r6_ex_date.py`) walk the share count day by day against
hand-computed values, which is the only thing that catches that class.

## Must-pass assertion families

A run is only green when **all** of these hold (exact Decimal unless noted):

- **Per-stock cost basis** — for every `(account, symbol)` holding: `shares` (SIGNED —
  an open declared short is negative), `original_cost_total`, `adjusted_cost_total`,
  `original_avg`, `adjusted_avg`, `dividend_portion`, the four state flags
  (`oversold` / `short_open` / `unbookable_dividend` / `unbookable_action`), and (when
  valued) `market_value`, `unrealized_pnl`, `capital_gain`, `unrealized_pct`
  (÷ `abs(basis)` — sign-safe on a short's negative basis).
- **Corporate actions** (SPLIT / EXCHANGE / SPINOFF — spec `2026-08-06-corporate-actions.md`)
  — the oracle applies §4.4's complete field-transfer table and the §5 refusal matrix
  (E1/E2/E3/E5/E18/E22) itself, from the SPEC, with its **own** two-term ratio arithmetic
  and its **own** `EventPriority`; it must never import `shared/corporate_actions.py` or
  `shared/ledger_events.py` (§7.4, trap #11 — and trap #10, which an imported enum makes
  invisible). Three legs, and the third is what the other two cannot do:
  1. oracle-vs-app on every derived figure (`holding.*`, `realized.*`, cash, KPIs, XIRR);
  2. `corp.refusal_codes` — WHICH §5 rule fired, so "the oracle skipped and the app
     applied" is triageable apart from "both applied and disagreed on the arithmetic";
  3. **`corp.anchor.*` — the app's number against a hand-computed literal.** Agreement
     between two replays cannot prove either landed on the share count the broker's
     statement says: a rounded ratio in BOTH agrees at 199.99. Note that `700 × 2/7`
     cannot discriminate evaluation order (the parenthesised form also lands on 200 at 28
     digits), so the scenario carries `210 × 1/3` — exactly 70 multiply-first,
     `69.999…99` parenthesised — and then sells exactly 70, which is the measured 賣超
     cascade. Same-day trades sit on two action dates, without which the whole event
     ordering is unobservable.
  4. `corp.xirr_blanked_by_unapplied` — an unapplied action blanks XIRR **portfolio-wide**
     (D38 invariant 2) and the reason names the account, symbol and date. Two of the three
     refusal shapes leave no surviving position to flag, so no holdings-level check can
     see them at all.
- **Per-(account, currency) cash pool** — every pool balance, **and** the reconstructed
  running-balance **statement terminal** (deposits/withdrawals + trade settlements + FX
  legs + cash dividends) equals the app's reported balance.
- **Realized P&L rows** — count, `kind` (`sale` / `dividend` / `short_cover`),
  `proceeds_net`, `adjusted_cost_removed`, `original_cost_removed`, `realized`, in order.
  The oracle's declared-short model (long-lot-first sell, cover-at-buy-cost, cover-dated
  realization) is derived independently from `domain-ledger.md`.
- **Fee engine** — expected fee/tax per trade, including the **TW ETF sell** (registry
  `is_etf` → 0.1%) and **TW daytrade sell** (`daytrade` flag → 0.15%) rate branches.
- **FX pool** — per FX-exposed account `avg_rate` + `realized_fx` + `foreign_cash` +
  **cost-basis coverage** (`covered_ratio`, `fx_basis_gap`, `fx_basis_incomplete`,
  `foreign_cash_negative`, pool == funds view, one-ratio scaling of BOTH unrealized
  legs), and the reporting rollups `fx_realized` / `fx_unrealized`.
- **Blended KPIs** — `realized_total`, `unrealized_total`, `total_market_value`,
  `total_return`, and **`xirr`** (the one tolerance check).
- **Ledger + export + report parity** — every raw ledger row, CSV export figure, and
  rendered report number matches.
- **Guards** — oversell blocks with 422, **date-aware** (a back-dated sell covered only
  by a later buy is blocked, naming the date); an UNDECLARED sell past the position is
  never treated as a short; a dividend on an open short is never booked (skip + flag);
  the `acq_home_amount` field rejects withdraw / home-ccy / amount+rate misuse;
  `discount<1` together with `rebate_rate>0` raises the fee-rule conflict warning;
  duplicate rows are accepted as distinct.
- **Ledger integrity** (validity, not arithmetic) — the LONG lot is never negative at any
  date (declared shorts replay through the two-lot model), and every undeclared sell has
  a realized row.

## Credibility-scoring rubric

Score each run on four axes; a run that would ship a money change should be strong on all
four:

| Axis | What earns credit |
| --- | --- |
| **Independence proof** | Oracle imports nothing from `portfolio_dash` (verify: `grep -E '^\s*(import\|from)\s+portfolio_dash' oracle.py` → none). Logic derived from the rules, only parameters from config. Two layers stay separate. |
| **Detection power** | Coverage across all 4 surfaces; exact-Decimal (no tolerance) everywhere except the one disclosed XIRR case; both found-bug ops present; realistic op mix (partials, same-day, oversell, corrections, all 3 dividend models, multi-account FX). |
| **Evidence trail** | `evidence/assertions.jsonl` records every check with expected/actual (and delta+tol for XIRR); `evidence/oplog.jsonl` records every operation with its surface and response. Counts reported (ops / pass / fail). |
| **Disclosed limitations** | Assumptions flagged in the oracle (e.g. Moomoo flat-fee both sides; stamp-duty-in-tax modeling); the single tolerance case named with its bound; phase-2 blended-KPI scope limit stated. |

## Reading the evidence

```bash
# pass/fail tally
grep -c '"pass": true'  scripts/stress_audit/evidence/assertions.jsonl
grep -c '"pass": false' scripts/stress_audit/evidence/assertions.jsonl
# every failure, readable
grep '"pass": false' scripts/stress_audit/evidence/assertions.jsonl
# the XIRR tolerance check with its delta
grep '"check": "kpi.xirr"' scripts/stress_audit/evidence/assertions.jsonl
```

Each assertion line: `{check, scope, phase, expected, actual, pass[, tol, delta]}`. Each
oplog line: `{op, phase, surface, kind, inputs, response, note, ts}`. **Evidence is
regenerated every run** — never diagnose from a previous run's files.

## Accumulation rules

This harness must **grow** — it is the permanent home for money-of-record correctness.

1. **Every bug the harness finds gets BOTH:** (a) a **hermetic pytest regression** under
   `tests/` (runs in every suite run, forever) **and** (b) a **permanent scenario op**
   here (so the exact shape is re-exercised end-to-end against the running app on every
   audit). One without the other is incomplete. *Precedent:* the 2026-07-15 ETF-sell and
   daytrade-sell tax bugs are permanent ops in `run_phase1.py` (ETF sell via the manual
   API; daytrade sell via both the manual body flag and a CSV `daytrade` column).
2. **Every new money-of-record feature** must, **before it ships**, extend: the **oracle
   logic** (`oracle.py`), the **scenario ops** (a `run_phase1.py` op exercising it), and
   **`docs/accounting-formula-manual.md`** (the formula + a verification anchor). Shipping
   a money change without extending the oracle is a process failure.
3. **Evidence files are regenerated per run and never trusted from a previous run.** They
   are git-ignored; a stale `assertions.jsonl` proves nothing about today's code.

See also `.claude/skills/stress-audit/SKILL.md` for the one-command entry point and the
report template, and the ship-version checklist item that enforces rule ②.
