# Broker-import capability backlog (P1a / P1b / P2 / P3)

**Status — updated 2026-08-15: three of the five items are BUILT, one is CLOSED, one is untouched.**
This header said 「recorded 2026-08-06, **not started** … all remain deferred」 until that date. It
was true when written and false from 2026-08-13, when three of its items landed in one release
package — which is the failure mode a backlog is most prone to: the file that records what is left
is the file nobody updates when something stops being left.

| Item | Status |
| --- | --- |
| **P1a** — broker-statement converter | **BUILT, unreleased** (2026-08-13). `data_ingestion/broker/` (`ir` · `schwab` · `grouping` · `reconcile` · `registry` · `convert`) + `scripts/schwab_convert.py` + `POST /api/broker/convert` + the import UI. All **seven** transformation rules below are implemented, including F-27's quantity term |
| **P1b** — US cash dividend | **BUILT, unreleased** (2026-08-13). All three items on its **Work** list |
| **P2a** — cash-movement CSV kind | **BUILT, unreleased** (2026-08-13), as the **6th** kind — see the note in that section |
| **P2b** — do interest/fees reach the return metrics? | **CLOSED: they do not, and nothing is planned that would.** Ruled twice; there is nothing here to build |
| **P3** — options | **NOT STARTED**, unchanged. `assignment` / `exercise` still need an owner ruling before it can begin |

⚠ **"BUILT" is not "released."** All of it sits on `feat/corporate-actions`, unmerged by owner
ruling; `__version__` is still `0.1.28`, and prod has never run any of it. The distinction is worth
the line because P0's own spec calls the same state **DONE** — it means "the code is finished", not
"the owner has it".

**Updated 2026-08-10** from the spec-conflict audit (`docs/audit/2026-08-10-spec-conflict-audit.md`).
The two decisions this backlog was waiting on were **recorded, not open** — P1b (D35) and P2b
(D36, **since retired by D45** — see P2b). Two of the backlog's own statements were also corrected:
P1a rule 3's zero-sum check (F-27, **HIGH** — it could silently delete corporate-action rows) and
P3's multiplier site list (F-46). No item's status changed **on that date**; all were still
deferred then.

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

> **BUILT 2026-08-13 — and the shape came out inverted from the recommendation above, deliberately.**
> The logic lives in `data_ingestion/broker/` (`ir` · `schwab` · `grouping` · `reconcile` ·
> `registry` · `convert`) and `scripts/schwab_convert.py` is a thin command line around it, because
> "new script only, no core module changes" would have put a four-figure-row transformation
> **outside** `mypy --strict` and outside the regression suite — the two gates that make it safe to
> change later. The offline path is preserved as the recommendation intended: the CLI never touches
> the server and the raw export never has to leave the owner's machine. A `POST /api/broker/convert`
> door was added **beside** it, not instead of it. All seven rules below are implemented.

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
   *(Written 2026-08-10 as **derived, not observed** — the converter did not exist yet, so the
   failure mode followed from rule 3's stated mechanism rather than from a reproduction. **It is
   now covered by a test that fails without the quantity term**:
   `tests/data_ingestion/test_broker_adapter.py::test_a_forward_split_survives_the_quantity_term`.)*
4. **Drop cancelled orders together with the original order row they cancel.**
5. **Dates:** `MM/DD/YYYY` → ISO. Rows carrying a dual `settle as of trade` date resolve to the
   **trade** date (the ledger's `trade_date` is the trade date).
6. **Use the broker's own fee/commission figure as the `fee` override** rather than letting
   `compute_fees` recompute it, so the ledger reconciles to the statement to the cent.
7. **An unmapped broker action is a HARD ERROR, never a silent drop.** A type that quietly
   disappears produces a ledger that looks complete and is not — the failure mode this whole
   assessment exists to prevent.

**Touches:** ~~new script only (plus its tests). No core module changes.~~ **As built:** a new
`data_ingestion/broker/` subpackage, one API route, one UI pane, and the `import_batches`
provenance table + `import_batch_id` / `source_row_hash` columns on five ledgers — because a
four-figure import with no undo is a worse failure than no import at all.

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

> **BUILT 2026-08-13 — all three Work items, plus one the list did not foresee.** `CASH` is
> admitted to `drip_us`; the `d-drip` pane has the type switch; withholding is typed rather than
> derived. The unforeseen one: a **blank** withholding on a US cash dividend now raises the soft
> `us_cash_dividend_no_withholding` question **without altering the row** — dropping the readonly
> `gross × 0.30` removed the guarantee that the field was ever filled, and silently booking
> `net = gross` on a payout that was in fact withheld is a money-of-record error that looks
> perfectly ordinary on screen. Documented as manual §6.2b (both mirrors), with §12.1's E26 anchor
> and two unit tests.

---

## P2 — Interest, broker fees, and whether they reach returns

Two independent problems with very different difficulty. Treat separately.

### P2a — Entry (small, additive, safe)

Cash movements can only be written one at a time
(`POST /api/cash/movements`, `api/routers/cash.py:437`; kinds `DEPOSIT` / `WITHDRAW` / `OPENING` /
`REBATE`, `cash.py:68`). There is **no CSV import kind for cash**, so a broker's interest and fee
rows must be typed in individually.

**Work:** add a template kind — `account, date, kind, ccy, amount, note, acq_home_amount`.
Purely additive; touches no calculation.

**DONE 2026-08-13 — and it is the SIXTH kind, not the fifth.** This item said "fifth" because it
was written on 2026-08-06, when four existed; corporate actions (P0) took the fifth while this
stayed deferred. The column set shipped as the same seven columns predicted above, with
`acq_home_amount` and `note` in the opposite order
(`data_ingestion/cash_import.py::CASH_MOVEMENT_COLUMNS`).

Two things arrived with it that this item did not anticipate, both because "a broker's interest and
fee rows" turned out to have **no ledger kind to be imported as**: three new movement kinds
(`INTEREST` / `INTEREST_EXPENSE` / `BROKER_FEE`), and the two-axis table in `shared/cash_kinds.py`
that governs them — the old predicate ("`WITHDRAW` is the only debit, everything else is an
acquiring credit") would have made a `BROKER_FEE` **increase** the cash balance *and* drag
`covered_ratio` down. `INTEREST` is the row that proves one boolean was never enough: a credit that
is not an acquisition.

⚠ **The rows import, and almost nothing else knows they exist.** Four gaps, all found after this
landed and none of them in this item's scope as written — one root cause, which is that every
ledger enumeration was updated for corporate actions and none of them for cash:

- **not exportable.** `shared/ledger_registry.py` gives `cash_movements` no `export_kind`, so it is
  in neither the per-ledger CSV nor the zip — the only ledger you can import and not export;
- **no ledger tab.** The input page's tab bar has five (交易/股利/換匯/期初庫存/公司行動); the rows
  are therefore unreadable on the page that owns the ledgers;
- **no AI-input kind.** That door parses trades only;
- **seven UI strings still say 「四帳本」** — and the exportable set is now **five**, since
  corporate actions joined it. The strings are wrong in both directions at once: they under-report
  the zip's contents *and* imply cash is one of the four when it is in neither.

### P2b — Whether interest/fees enter the return metrics — **CLOSED: they do not**

**They do not, and no metric is planned that would.** Ruled twice by the owner, on independent
grounds and in that order:

- **D45 (owner ruling 2026-08-11)** — 「IRR 標記不需要」. D36 had chosen a **whole-account IRR**
  alongside XIRR on 2026-08-10; the owner retired it **unimplemented**. `portfolio/twr.py` was
  checked in place: `twr_index` / `convert_closes` / `build_overlay` and nothing named
  `account_irr` — D36 never left the decision table.
- **D1 = A (owner ruling 2026-08-13, cash-kinds work; recorded in `CHANGELOG.md` and manual §7.2)**
  — reached later, independently, and it is the **wider** ruling: **all seven cash-movement kinds
  are excluded from XIRR.**
  `xirr_reporting` does not take `cash_movements` in its signature at all; the flow series is
  `opening` + `transactions` + `dividends`, and stays that way. Two of the three kinds P2a added
  are exactly this item's interest and fee rows, so the question was re-asked on its own merits
  with the rows in hand — and answered the same way.

> **The whole-account IRR's design notes are DELETED from this section, not struck through**
> (owner instruction 2026-08-15). The other correction in this file — P1a's **Touches** line — is
> struck rather than removed, because the superseded text still explains why the shipped shape
> differs from it. This one explains nothing: it was never
> built, nothing references it, and a rejected design left standing in a **backlog** reads as work
> waiting to be picked up — which is the one thing it must not read as. The decision's full record,
> with its original reasoning intact, is in `docs/spec/2026-08-06-corporate-actions.md` §8
> (~~D36~~ → D45), where it *is* struck rather than removed because the manual's wording was
> written against it.

**What survives, because it is still load-bearing:**

- **The consistency trap.** Deposits are *also* absent from XIRR. Admitting interest without
  admitting deposits yields a metric that includes some cash activity and not the rest, which has
  no coherent interpretation. Anyone re-opening this has to answer that first — it is why "just add
  the interest rows to XIRR" is not the small change it looks like.
- **Why XIRR is not redefined either.** It moves every historical figure, invalidates the
  accounting manual's worked anchors and the stress-audit oracle's expectations, and would have to
  be re-verified against the whole corpus — for amounts this backlog itself calls trivial (the
  measured net is in the private assessment note; it is small enough that the re-verification cost
  is orders of magnitude larger than the figure). It would also need `domain-ledger.md` sign-off
  recorded in `CHANGELOG.md`.

**Consequence — D12 is a STANDING limitation.** This section used to instruct whoever wrote the
corporate-actions accounting manual (W9) to record the reorganisation fee's blind spot as *resolved
by the whole-account IRR*. **That instruction is withdrawn.** The fee is booked as a `WITHDRAW`
cash movement and is invisible to **every** return metric this system has; it surfaces in the cash
ledger and in net worth, and no resolution is planned. The manual already says exactly that (v1.7a,
both mirrors) — **this file was the last place still promising otherwise**. **No figure ever
moved:** XIRR has never included cash movements, so retiring a metric that was never built changed
nothing except what the documentation claims.

**What P2a delivered instead.** The rows themselves import and land where they belong — the cash
pool and net worth. That was always the recoverable half of this item, and it is done.

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

> **Status unchanged 2026-08-15: NOT STARTED.** What P1a did add is that option rows are now
> **recognised and routed out**, never mistaken for equity: `broker/ir.py::OPTION_KINDS` names the
> five, `grouping.py` routes them (and anything flagged `is_option`) away from the ledger CSVs, and
> the converter reports them rather than dropping them. That is the opposite of support — it is the
> guarantee that the absence of support is **visible**. The ground-truth build this was lifted from
> ended its classifier with a catch-all that would have booked them as adjustments and buried them.
> **`assignment` / `exercise` still need an owner ruling before this can begin.**

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
  **Update 2026-08-15:** the engineering half shipped with P0's W7 — `original_cost_total > 0` is
  hard-validated (D37), because `opening_import.py` used to check only `shares > 0`, so a cost of
  **0** imported cleanly and permanently zeroed the basis **with no 待釐清 flag** — strictly worse
  than the oversell it appears to fix, since the oversell at least announces itself. The owner has
  since compiled the per-symbol figures in the git-ignored sample folder (some measured, some
  estimated, and the file marks which is which). **They have not been imported into any instance
  yet**, so this stays open as an owner input item.
- **Nothing here is blocked on P0** except the converter's ability to emit corporate-action rows,
  which is why P0 goes first. *(P0's code is complete; see the status table at the top for what
  "complete" does and does not mean.)*
