# Broker-import capability backlog (P1a / P1b / P2 / P3)

**Status:** recorded 2026-08-06, **not started**. P0 (corporate actions) is being built first and
has its own spec. Each item below is deferred until explicitly picked up.

**Updated 2026-08-10** from the spec-conflict audit (`docs/audit/2026-08-10-spec-conflict-audit.md`).
The two decisions this backlog was waiting on are now **recorded, not open** — P1b (D35) and P2b
(D36) — so neither needs re-asking. Two of the backlog's own statements were also corrected: P1a
rule 3's zero-sum check (F-27, **HIGH** — it could silently delete corporate-action rows) and P3's
multiplier site list (F-46). No item's status changed; all remain deferred.

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
   **NORMATIVE (2026-08-10, audit F-27): the zero-sum check must sum the share QUANTITY as well as
   the amount — both, not either.** A corporate action is itself a paired out/in row group, and its
   cash amount is **zero on both legs**: a split moves shares without moving money. An amount-only
   check therefore passes a 3-for-1 split (`−85` / `+255` shares, `$0` / `$0`) and **drops it
   silently** — deleting exactly the rows P0 exists to capture, and re-creating the permanent basis
   loss this whole assessment exists to prevent. Rule 7's hard-error protection does not fire,
   because rule 3 has already classified the group. Adding the quantity term closes it at no cost:
   `−85 + 255 = +170 ≠ 0` protects the split, while `−100 + 100 = 0` still drops a genuine journal.
   This is why rule 3's safety property was under-specified as first written.
   *(**Derived, not observed.** The converter does not exist and the broker export was out of scope,
   so this failure mode follows from rule 3's stated mechanism rather than from a reproduction.)*
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

**DECIDED — owner sign-off 2026-08-10 (audit D35): a US CASH dividend DOES reduce `adjusted_total`,
exactly as a TW/MY cash dividend does.** A `CASH`-type dividend falls into `CASH_DIVIDEND_TYPES`
(`shared/models/enums.py:26`), so it lowers that position's average cost. This needs **no
`domain-ledger.md` change** — the locked adjusted-cost model already covers it.

**Why, in one line: the alternative would put two dividend accounting models in one ledger.**
`CASH_DIVIDEND_TYPES` is a single frozenset driving four consuming sites uniformly —
`portfolio/cost_basis.py:406`, `portfolio/returns.py:144`, `portfolio/cash.py:118` and `:211`,
`portfolio/timeseries.py:94`. Booking US cash as income instead would fork that definition by
market, so 回本進度 and 股利回收率 would mean different things per market **on the same screen** —
which is precisely what `domain-ledger.md`'s single-definition discipline exists to prevent. This is
a whole-system consequence, not a preference.

**Coupled consequence — the part that would otherwise have been discovered late.** Under the rejected
income model, `dividend_portion` would always be 0 for US positions, so **D21** — the SPINOFF child's
「承接自母公司」 payback-provenance label in `docs/spec/2026-08-06-corporate-actions.md`, already
approved and scheduled into W7 — would have been unreachable on the only data that has corporate
actions. The approved decision keeps D21 live.

The **Work** list above is unchanged by this decision and stands as recorded.

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

### P2b — Whether interest/fees enter the return metrics (DECIDED 2026-08-10)

They currently cannot. `portfolio/cash.py:13` states it explicitly — the cash pool "feeds NO
return metric" — and `xirr_reporting` (`portfolio/returns.py:98`) admits only buys, sells, cash
dividends, opening inventory and the terminal market value.

**The consistency trap that shaped the decision:** deposits are *also* absent from XIRR. Adding
interest but not deposits yields a metric that includes some cash activity and not the rest, which
has no coherent interpretation. The three self-consistent options were:

| Option | Meaning | Verdict |
| --- | --- | --- |
| Keep as is | XIRR stays a *portfolio* (trade-flow) return; interest lands in the cash pool and net worth only, with a footnote on the report | not chosen |
| **Add a second metric** | **Keep XIRR untouched; add a whole-account IRR that includes every cash movement, deposits included** | **CHOSEN** |
| Redefine XIRR | Requires `domain-ledger.md` sign-off recorded in `CHANGELOG.md`; every historical figure moves | rejected |

**DECIDED — owner sign-off 2026-08-10 (audit D36): option 2. XIRR is untouched; a whole-account IRR
is added in the existing `portfolio/twr.py`** — no new module, and `twr.py` is already the home for
a whole-account view.

**Why not option 3.** Redefining XIRR moves every historical figure, invalidates the accounting
manual's worked anchors and the stress-audit oracle's expectations, and would have to be re-verified
against the whole corpus — for amounts this backlog itself calls trivial.

**What option 2 buys beyond the interest/fee rows.** It is purely additive, and it also resolves a
limitation the corporate-actions spec currently concedes: **D12** books the reorganisation fee as a
`WITHDRAW` cash movement, while `xirr_reporting` (`portfolio/returns.py:135-146`) builds its flow
series from `opening` + `transactions` + `dividends` only. The fee is therefore invisible to every
current return metric. The second metric is where it becomes visible.

**Sequencing consequence for whoever writes the accounting manual (corporate-actions W9):** D12's
limitation must now be written as **resolved by the whole-account IRR**, not as permanent.

---

## P3 — Options (evaluate as its own sub-project; do NOT bundle with the import)

**Currently unsupported end to end.** The gaps, in rough order of depth:

| Gap | Where it bites |
| --- | --- |
| **Contract multiplier of 100** | **The real blocker.** Inline `quantity × price` is written out at **28 sites across 13 files** (measured at HEAD; list below). A multiplier is not a field to add; it is an assumption to remove from every money formula |
| Instrument identity (expiry / strike / right / underlying) | `shared/symbol_format.py:26` — the US pattern `^[A-Z]{1,5}(\.[A-Z])?$` rejects an option symbol; `instruments` has no columns for the contract terms |
| Expiry-worthless | No event type; closes a position at zero with the full premium realized |
| Assignment / exercise | Converts to an equity position at the strike. **Needs an owner ruling** |
| Quotes for an open contract | No provider path; yfinance is not reliable for arbitrary option chains |

**Direction is NOT a gap.** Sell-to-open / buy-to-close map cleanly onto the existing declared
short-sale model (option C, 2026-07-31): an STO is a short position holding the proceeds received,
a BTC is the cover. The multiplier is what blocks reuse, not the sign convention.

### The multiplier site list (corrected 2026-08-10, audit F-46)

The earlier text named five files, and one of them was wrong: **`export/tax.py` has no such site.**
It consumes pre-computed `RealizedRow` fields; its only multiplication is `r.realized * rate`
(`export/tax.py:83`), an FX conversion of an already-formed money figure. Of the five named, four
were real — and **nine more were missing. P3's blocker is larger than first recorded, not smaller.**

Re-measured at `adc3977` by walking the AST for a `Mult` whose one operand is a quantity name
(`qty` / `quantity` / `shares` / `units`) and whose other is a price name (`price` / `px` / `close`):
**28 sites across 13 files.**

| Layer | Module | Sites |
| --- | --- | --- |
| calc core | `portfolio/cost_basis.py` | 4 |
| calc core | `portfolio/cash.py` | 4 |
| calc core | `portfolio/returns.py` | 3 |
| calc core | `portfolio/timeseries.py` | 2 |
| calc core | `portfolio/pnl.py` | 1 |
| calc core | `forex/pools.py` | 2 |
| strategy | `strategy/whatif.py` | 2 |
| strategy | `strategy/rebalance.py` | 2 |
| ingestion | `data_ingestion/fees.py` | 1 |
| export | `export/ledgers_report.py` | 1 |
| api | `api/routers/input_center.py` | 3 |
| api | `api/routers/symbol.py` | 2 |
| api | `api/routers/ledgers.py` | 1 |

**28 is a floor, not a ceiling.** The same assumption also appears as a *per-share* figure multiplied
by a share count, which that pattern does not match: `portfolio/pnl.py:47-48` forms unrealized P&L and
capital gain as `(price − avg) × shares`, and `data_ingestion/opening_import.py` multiplies an average
cost by a share count. Scope the removal by the assumption, not by the grep.

**P0 does not make this harder — verified negative.** The census is **identical at the merge-base
`734833b`, at the spec-audit point `f80e462`, and at `adc3977`**: 28 sites, the same per-file
counts, only line numbers moved (`timeseries.py:145 → :135`, `whatif.py:186 → :161`). `apply_ratio`
operates on a quantity and never forms money; `split_factor` is prices-only; `_apply_action`
*transfers* totals rather than re-deriving them from `qty × price`. P3 can be sequenced after P0 at no
added cost. *(The 2026-08-10 audit reached the same negative independently, counting 24 sites under a
stricter name match — the absolute count depends on the matcher; the invariance across revisions does
not.)*

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
