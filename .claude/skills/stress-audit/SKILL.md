---
name: stress-audit
description: "Re-launch the full 壓力驗證 + 帳目可信度 loop for portfolio-dash: drive realistic operation sequences against the running app and reconcile every money-of-record figure against an independent Decimal oracle. Use on owner request, and BEFORE shipping any version that touches a money-of-record calculation (cost basis, realized/unrealized P&L, fees/tax, dividends, FX pool, returns/XIRR). Invoke with /stress-audit."
---

# Stress Audit

The one-command entry point to the permanent stress-audit harness in
`scripts/stress_audit/` (full SOP: `scripts/stress_audit/README.md`). It proves the
ledger is trustworthy by reconciling the app against an **independent** accounting oracle
that shares no code with `portfolio_dash`.

## When to run

- The **owner asks** to re-run the stress / credibility audit.
- **Before shipping any version** whose diff changes a money-of-record calculation:
  cost basis, realized/unrealized P&L, fee/tax, dividend handling, FX pool, returns/XIRR.
  (This is the ship-version money-change checklist item.)

## Commands

Always use the repo `.venv` python and UTF-8. From the repo root:

```bash
# Phase 1 — clean-room, local, safe (default). Fresh DB under evidence/, own uvicorn,
# scheduler disabled, ABSOLUTE oracle reconciliation incl. the XIRR scalar (tolerance) check.
.venv/Scripts/python.exe scripts/stress_audit/run_all.py --phase 1

# Phase 1 + real browser happy-paths + DOM read-back (Playwright):
.venv/Scripts/python.exe scripts/stress_audit/run_phase1.py --ui

# Phase 2 — investor-realistic UI-first stress on the LIVE demo (mutating, additive).
# The real demo URL lives in docs/human_noted/ — NEVER hard-code it here. Pass it:
.venv/Scripts/python.exe scripts/stress_audit/run_all.py --phase 2 --base-url https://<demo-from-human_noted>
```

Phase 2 leaves its data on the demo (`--keep-data`, default) and never resets it. Do NOT
run phase 2 unless the owner wants the demo mutated — phase 1 is the default for a routine
audit and the pre-ship gate.

## Reading the result

The runner prints `ops=<n> pass=<n> fail=<n>` and the first failures inline. Evidence is
under `scripts/stress_audit/evidence/` (git-ignored, regenerated every run):

```bash
grep -c '"pass": false' scripts/stress_audit/evidence/assertions.jsonl   # must be 0
grep    '"pass": false' scripts/stress_audit/evidence/assertions.jsonl   # each failure
grep '"check": "kpi.xirr"' scripts/stress_audit/evidence/assertions.jsonl # XIRR delta vs tol
```

Every assertion line carries `{check, scope, phase, expected, actual, pass}` (plus
`tol, delta` for the XIRR case). Every operation is in `evidence/oplog.jsonl` with its
surface (API / UI / CSV) and response. **Never** diagnose from a previous run's evidence —
regenerate.

## Report template (to the owner, Traditional Chinese summary; artifacts English)

1. **Coverage stats** — `ops`, `pass`, `fail`; phases run; surfaces exercised (API / CSV /
   report-HTML / DOM); assertion families touched.
2. **Findings** — each failure: the `check` + `scope`, expected vs actual, and the
   classification (app bug vs oracle assumption vs fixture issue).
3. **Fix → re-run loop** — for every real bug: fix in the app, add BOTH a hermetic pytest
   regression AND a permanent scenario op here (accumulation rule ①), then re-run until
   green. Record the loop.
4. **Credibility score** — rate the run on the four axes (independence proof / detection
   power / evidence trail / disclosed limitations; rubric in the README) and state it.

## Accumulation checklist (do NOT skip)

- [ ] Every bug found this run has a **hermetic pytest regression** under `tests/` (permanent).
- [ ] Every bug found this run has a **permanent scenario op** in `run_phase1.py` (re-exercised).
- [ ] Any new money-of-record feature extended the **oracle** (`oracle.py`) + a **scenario op**
      + **`docs/accounting-formula-manual.md`** (formula + verification anchor) BEFORE shipping.
- [ ] Independence intact: `grep -E '^\s*(import|from)\s+portfolio_dash' scripts/stress_audit/oracle.py`
      returns nothing.
- [ ] Phase 1 re-run is **green** (`fail=0`) after any change.
