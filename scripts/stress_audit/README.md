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

## Must-pass assertion families

A run is only green when **all** of these hold (exact Decimal unless noted):

- **Per-stock cost basis** — for every `(account, symbol)` holding: `shares`,
  `original_cost_total`, `adjusted_cost_total`, `original_avg`, `adjusted_avg`,
  `dividend_portion`, and (when valued) `market_value`, `unrealized_pnl`, `capital_gain`.
- **Per-(account, currency) cash pool** — every pool balance, **and** the reconstructed
  running-balance **statement terminal** (deposits/withdrawals + trade settlements + FX
  legs + cash dividends) equals the app's reported balance.
- **Realized P&L rows** — count, `proceeds_net`, `adjusted_cost_removed`,
  `original_cost_removed`, `realized`, in order.
- **Fee engine** — expected fee/tax per trade, including the **TW ETF sell** (registry
  `is_etf` → 0.1%) and **TW daytrade sell** (`daytrade` flag → 0.15%) rate branches.
- **FX pool** — per FX-exposed account `avg_rate` + `realized_fx`, and the reporting
  rollups `fx_realized` / `fx_unrealized`.
- **Blended KPIs** — `realized_total`, `unrealized_total`, `total_market_value`,
  `total_return`, and **`xirr`** (the one tolerance check).
- **Ledger + export + report parity** — every raw ledger row, CSV export figure, and
  rendered report number matches.
- **Guards** — oversell blocks with 422; duplicate rows are accepted as distinct.

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
