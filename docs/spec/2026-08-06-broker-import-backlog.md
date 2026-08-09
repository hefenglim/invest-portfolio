# Broker-import capability backlog (P1a / P1b / P2 / P3)

**Status:** recorded 2026-08-06, **not started**. P0 (corporate actions) is being built first and
has its own spec. Each item below is deferred until explicitly picked up.

**Origin.** A coverage assessment of a real US broker (Charles Schwab) transaction export —
5 CSVs, ~1,375 rows, 34 distinct broker action types, 2021-01 → 2026-07 — against what this
codebase can currently ingest. About three quarters of the rows map onto existing entry points
(equity buy/sell, DRIP dividends as 3-row broker groups, wire deposits, one declared short).
The rest is what this backlog covers.

> **Data-specific figures — tickers, amounts, per-symbol impact — are deliberately NOT in this
> file.** They are the owner's real holdings. They live in the git-ignored
> `docs/human_noted/schwab-import-assessment-2026-08-06.md`. This file records only capability
> gaps, which are properties of the code.

---

## P1a — Broker-statement converter (Schwab first)

**Problem.** The four CSV import kinds (`transactions`, `dividends`, `fx`, `openings` —
`data_ingestion/import_templates.py:31`) take THIS project's canonical column set. No broker
emits it. A four-figure row count cannot be hand-entered.

**Recommended shape: an offline script first, a server-side adapter only after it is proven.**
It reads the broker export and emits the existing import templates; the owner then uploads those
through the current UI. Rationale: zero new surface, zero core change, the risk is concentrated in
the transformation logic where it can be regression-tested against the real file, and the raw
broker export never has to reach the server. Promoting it to a `data_ingestion/` broker adapter
afterwards is mechanical.

**Transformation rules the converter must implement** (each verified against the real export):

1. **Collapse the broker's 3-row DRIP group into one dividend row.** A US reinvested dividend
   arrives as three separate rows — gross payout, withholding adjustment, reinvest purchase —
   which must be grouped by `(date, symbol)` into a single `type=DRIP` row carrying
   `gross` / `withholding` / `reinvest_shares` / `reinvest_price`.
2. **Recover the symbol from the description.** Rows from the pre-migration era carry an empty
   `Symbol` column with the ticker only inside the description text, as a trailing `(TICKER)`.
   Roughly a quarter of all rows are affected. Rows that legitimately have no symbol (interest,
   wires) must not be forced to have one.
3. **Suppress paired self-cancelling rows** — internal sub-account journals, the broker-migration
   transfer-out/transfer-in pair, mark-to-market pairs. **Verify each group sums to exactly zero
   before dropping it**; that check is the safety property, not the classification.
4. **Drop cancelled orders together with the original order row they cancel.**
5. **Dates:** `MM/DD/YYYY` → ISO. Rows carrying a dual `settle as of trade` date resolve to the
   **trade** date (the ledger's `trade_date` is the trade date).
6. **Use the broker's own fee/commission figure as the `fee` override** rather than letting
   `compute_fees` recompute it, so the ledger reconciles to the statement to the cent.
7. **An unmapped broker action is a HARD ERROR, never a silent drop.** A type that quietly
   disappears produces a ledger that looks complete and is not — the failure mode this whole
   assessment exists to prevent.

**Touches:** new script only (plus its tests). No core module changes.

---

## P1b — US cash dividend (not reinvested)

**Problem.** A US account's dividend model is `drip_us`, and both the import gate and the manual
form assume every US dividend is reinvested. A US payout that arrives as plain cash has:

- **no manual entry path at all** — `web/trades.html:241` shows the `d-drip` pane (gross /
  withholding / net / reinvest shares / reinvest price) and nothing else for a US account
  (`web/input.js::divModelFor`, the `'drip'` branch);
- **a soft block on CSV import** — `_MODEL_ALLOWED_TYPES["drip_us"] == {"DRIP"}`
  (`data_ingestion/dividend_import.py:20`) raises a `dividend_type_mismatch` `needs_confirm`
  issue for any other type. Importable after per-row confirmation, but friction.

**Secondary defect found in the same place:** the manual form's withholding field is `readonly`
and hard-computes `gross × 0.30` (`web/input.js`, the `#d-drip-gross` oninput handler). Real
broker withholding differs by a cent through the broker's own rounding, so the manual path cannot
reproduce a statement exactly. CSV import already accepts a `withholding` override, so this is a
manual-path-only gap.

**Backend is already capable.** `apply_dividend_model` (`data_ingestion/dividend_model.py:55`)
handles the cash branch with a withholding override, and `check_amounts`
(`dividend_model.py:22`) deliberately tolerates `gross ≠ withholding + net`.

**Work:** allow `CASH` in `_MODEL_ALLOWED_TYPES["drip_us"]`; add a type switch to the `d-drip`
pane (mirror the TW pane's existing segmented control); make withholding overridable using the
**existing** pencil-override affordance (`m-fee-pencil`), not a new interaction.

**⚠ OWNER DECISION REQUIRED BEFORE IMPLEMENTING.** A `CASH`-type dividend falls into
`CASH_DIVIDEND_TYPES` and therefore **reduces `adjusted_total`** — i.e. a US cash dividend would
lower that position's average cost, exactly as a TW cash dividend does. That is consistent with
the locked single adjusted-cost accounting model (`domain-ledger.md`), but it is a visible
semantic choice. If US cash dividends should instead be booked as income without touching cost,
that is a different accounting model and a `domain-ledger.md` change. **Do not assume; ask.**

---

## P2 — Interest, broker fees, and whether they reach returns

Two independent problems with very different difficulty. Treat separately.

### P2a — Entry (small, additive, safe)

Cash movements can only be written one at a time
(`POST /api/cash/movements`, `api/routers/cash.py:437`; kinds `DEPOSIT` / `WITHDRAW` / `OPENING` /
`REBATE`, `cash.py:68`). There is **no CSV import kind for cash**, so a broker's interest and fee
rows must be typed in individually.

**Work:** add a fifth template kind — `account, date, kind, ccy, amount, note, acq_home_amount`.
Purely additive; touches no calculation.

### P2b — Whether interest/fees enter the return metrics (a DECISION, not an engineering task)

They currently cannot. `portfolio/cash.py:13` states it explicitly — the cash pool "feeds NO
return metric" — and `xirr_reporting` (`portfolio/returns.py:98`) admits only buys, sells, cash
dividends, opening inventory and the terminal market value.

**The consistency trap:** deposits are *also* absent from XIRR. Adding interest but not deposits
yields a metric that includes some cash activity and not the rest, which has no coherent
interpretation. The three self-consistent options are:

| Option | Meaning |
| --- | --- |
| **Keep as is** | XIRR stays a *portfolio* (trade-flow) return; interest lands in the cash pool and net worth only, with a footnote on the report |
| **Add a second metric** | Keep XIRR untouched; add a whole-account IRR that includes every cash movement, deposits included |
| **Redefine XIRR** | Requires `domain-ledger.md` sign-off recorded in `CHANGELOG.md`; every historical figure moves |

**Recommendation: option 1 or 2.** The amounts involved are trivial relative to the cost of moving
every historical return figure. Note `portfolio/twr.py` already exists and may be the better home
for a whole-account view.

---

## P3 — Options (evaluate as its own sub-project; do NOT bundle with the import)

**Currently unsupported end to end.** The gaps, in rough order of depth:

| Gap | Where it bites |
| --- | --- |
| **Contract multiplier of 100** | **The real blocker.** `quantity * price` is written inline in `portfolio/cost_basis.py`, `portfolio/cash.py`, `portfolio/returns.py`, `strategy/whatif.py` and `export/tax.py`. A multiplier is not a field to add; it is an assumption to remove from every money formula |
| Instrument identity (expiry / strike / right / underlying) | `shared/symbol_format.py:26` — the US pattern `^[A-Z]{1,5}(\.[A-Z])?$` rejects an option symbol; `instruments` has no columns for the contract terms |
| Expiry-worthless | No event type; closes a position at zero with the full premium realized |
| Assignment / exercise | Converts to an equity position at the strike. **Needs an owner ruling** |
| Quotes for an open contract | No provider path; yfinance is not reliable for arbitrary option chains |

**Direction is NOT a gap.** Sell-to-open / buy-to-close map cleanly onto the existing declared
short-sale model (option C, 2026-07-31): an STO is a short position holding the proceeds received,
a BTC is the cover. The multiplier is what blocks reuse, not the sign convention.

### Cheap interim worth considering

`RealizedRow` already supports a realized row with **no share movement**:
`kind: Literal["sale", "dividend", "short_cover"]` (`portfolio/results.py:80`) — added for
post-close dividend income (audit H2) and extended for short covers. A closed option round trip
could be booked as a `kind="option"` realized row carrying the net premium.

- ✔ cash reconciles, realized P&L reconciles, XIRR stops under-reporting
- ✘ no position tracking, and an OPEN contract cannot be marked to market

Viable as a bridge when almost every contract in the history is a completed round trip. It is a
**bridge, not the feature** — say so wherever it surfaces in the UI.

---

## Cross-cutting notes

- **Delisted / renamed symbols** need no quote source once the position is closed; `instruments`
  already carries an `archived` flag for exactly this (FU-D13). `xirr_reporting` is all-or-nothing
  on missing prices for **held** symbols only, so closed positions never poison the rate.
- **Pre-history positions.** Where a broker export begins after the account did, the earliest
  events are sells against holdings that were never bought inside the file. `opening_inventory`
  is the right mechanism, but it requires an **original cost total the export does not contain**.
  Resolving that is an owner input problem, not a code gap.
- **Nothing here is blocked on P0** except the converter's ability to emit corporate-action rows,
  which is why P0 goes first.
