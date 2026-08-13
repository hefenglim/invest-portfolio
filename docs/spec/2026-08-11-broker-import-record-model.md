# Broker-import record model & converter architecture

**Status:** recorded 2026-08-11, **not started**. Companion to
`docs/spec/2026-08-06-broker-import-backlog.md` (P1a/P1b/P2/P3 capability gaps) and to the P0
corporate-actions spec. This file answers a different question: *what records must exist, and
what shape should the converter take* — the backlog answers *what is missing*.

**Origin.** A real US broker export (5 CSVs, 1,375 rows, 2021-01 → 2026-07) was merged into one
statement file and reconciled row-for-row. That exercise produced the first **observed** evidence
about the converter's transformation rules, where the backlog could only reason from mechanism.
Sections 1–2 are normative corrections to the backlog; §3–§5 are the new design.

> **No tickers, no amounts, no per-symbol impact in this file** — same rule as the backlog. Those
> live in the git-ignored `docs/human_noted/` assessment note.

---

## 1. NORMATIVE — the zero-sum check is necessary but NOT sufficient (supersedes backlog rule 3's safety property)

The backlog states that for suppressing paired self-cancelling rows, *"Verify each group sums to
exactly zero before dropping it; **that check is the safety property, not the classification**."*
Audit finding F-27 then strengthened it: sum the share **quantity** as well as the amount, so a
forward split (`−85` / `+255` shares, `$0` / `$0`) is not silently dropped. F-27 was recorded as
**derived, not observed**.

**Observed 2026-08-11, on the real export: the strengthened check still deletes real corporate
actions.** A **1-for-1 ticker exchange** nets to zero in *both* dimensions — shares out equal
shares in, and both legs carry no cash — so it is arithmetically indistinguishable from an
internal journal:

| Grouping key | Rows the zero-sum rule would drop | Real corporate-action rows destroyed |
| --- | ---: | ---: |
| `(date)` | 180 | **8** (3 ticker exchanges incl. their option legs) |
| `(date, symbol)` | 168 | 0 |

F-27's quantity term protects a split precisely because a split's legs are *unequal*
(`−85 + 255 ≠ 0`). It offers no protection at all where the legs are equal. The cost-basis
carry-over from the old ticker to the new one is exactly what P0 exists to preserve, so this is
the same permanent-basis-loss failure, reached by a different route.

**Two corrections follow.**

**1a. Group by `(date, symbol)`. Never group across symbols.** This removes every observed
casualty, and cross-symbol grouping is never needed: the broker-migration transfer pair is
same-symbol per security, and its cash legs are symbol-less.

⚠ **The right grouping key is not by itself a safety property.** Symbol-less rows all collapse
into one blank-symbol bucket per date, and that bucket mixes kinds: on one observed date it holds
both a null cash sweep **and** a real dividend-withholding row. Its sum is non-zero, so an
arithmetic-only rule keeps the whole bucket — the withholding survives, but so do the two phantom
sweep rows. Getting the key right stops the deletions; only 1b decides correctly which rows are
noise.

**1b. Classification is primary; arithmetic is the guard, not the decision.** Reverse the
backlog's emphasis. A group may be dropped **only** if it is first *classified* as a null journal
by its `(action, description)` pair; the zero-sum check then runs as a veto. This ordering is
forced by a second observation: the broker overloads one action value, `Journaled Shares`, across
**14 distinct meanings** in a single export — internal account-type journal, intra-account cash
round-trip, cash-movement sweep, security transfer-out, dividend withholding, mark-to-market
round-trip, mandatory exchange, reverse split, forward split, spin-off, reorganisation fee, option
position change, option odd-split reorganisation, and option removal on expiration. Only the
free-text `Description` separates them, and the two largest meanings sit on opposite sides of the
keep/drop line: **145 rows are dividend withholding tax (keep) and 126 are null account-type
journals (drop)**. An importer keyed on the action column alone therefore either deletes 145 tax
rows as noise or keeps 126 phantom journals as real events. Arithmetic cannot recover that
distinction, in either direction.

Consequence for backlog rule 7 (*an unmapped broker action is a HARD ERROR*): the unit of mapping
is the **(action, description-pattern) pair**, not the action. An unrecognised description on a
known action must fail just as loudly.

## 2. NORMATIVE — two further observed facts the converter must encode

**2a. Fractional reinvest quantities are display-rounded; derive shares from `amount / price`.**
125 of 227 reinvest rows fail `quantity × price == amount`. In **every** case the printed quantity
is exactly `round(|amount| / price)` at the 3–4 dp the column shows. Amount and price are the
authoritative pair; the largest implied share error is ~5×10⁻⁴ per row, and it accumulates in the
same direction over a multi-year DRIP history. Trusting the quantity column drifts the share
count away from the statement. *(This is the ledger-side analogue of the `close_raw` /
`close` rule in `data-and-pricing.md`: store what the source actually asserts, derive the rest.)*

**2b. Detecting pre-history positions by "first event is a sell or a reinvest" is insufficient.**
That heuristic misses a symbol whose first event is a **buy** and which still oversells later
(observed: one symbol buys twice, sells more than it bought, and nets negative). The correct
detector is the **running-balance minimum** over the replay; the shares needed are `−min(balance)`.
The two detectors are complementary and must be **unioned**:

| Detector | Catches | Severity if unhandled |
| --- | --- | --- |
| running balance goes negative | positions that will trip the sticky 賣超 guard | **hard** — basis discarded permanently |
| first event is a sell **or a reinvest** | positions held but never oversold (a DRIP implies a holding) | soft — basis silently wrong, no alarm |

---

## 3. Record model — what must exist

### 3.1 Already sufficient — do not redesign

`corporate_actions` (P0, in flight) covers **every** corporate action in the assessed export with
no schema change: `SPLIT | EXCHANGE | SPINOFF`, rational `ratio_to/ratio_from`, and `cost_carry`
for spin-offs. Forward splits, a reverse split, ticker/CUSIP renames, SPAC exchanges and one
spin-off all map onto it directly. The identifier-continuity problem (a position that changes
symbol mid-history) is an `EXCHANGE` row, not a new alias table.

`dividends` already carries `gross / withholding / net / reinvest_shares / reinvest_price`, which
is exactly the shape a collapsed 3-row DRIP group produces.

### 3.2 Gap A — import provenance (recommended: build first)

No ledger table records where a row came from. `transactions`, `dividends`, `cash_movements` and
`fx_conversions` have no `source_*` columns and there is no import-batch concept. Two consequences:

- **Re-importing the same export duplicates the entire ledger.** There is no idempotency key.
- **No number can be traced back to a statement line**, so a reconciliation difference cannot be
  localised — the failure mode is "the totals disagree and nobody can say which row".

This is also an internal inconsistency: `data-and-pricing.md` requires *"Source is recorded per
row, so data provenance is always auditable"*. That holds for `prices` today and not for the
ledgers.

Recommended shape: an `import_batches` table (id, broker, file name, file hash, imported_at,
row counts, status) plus, on each ledger table, `import_batch_id` and `source_row_hash`. The row
hash is the idempotency key — a re-import matches and skips rather than inserting. It also makes
an import **reversible**: deleting a batch deletes exactly its rows, which is what makes trying an
import safe on real data.

### 3.3 Gap B — opening inventory with known shares and unknown cost

`opening_inventory.original_cost_total` is `NOT NULL`, but a broker export that starts mid-history
contains no cost for positions opened before the window. The importer must not fabricate one, and
requiring the owner to supply ~20 costs before *any* row imports makes the feature unusable.

**This problem already has a signed-off precedent in this codebase.** The FX pool solves the
identical shape — an acquisition whose home-currency cost is unknown — under rulings F1/F2/F3 in
`domain-ledger.md`: store the amount and never the rate, let an unbased acquisition fund the
balance but never the average, and publish a `covered_ratio` that scales the whole exposure.
Applying the same pattern to opening inventory means shares are known and count toward position
and market value, while cost-derived figures (return rate, realized P&L) degrade explicitly rather
than silently reporting a wrong number. See §5 D2 for the decision.

### 3.4 Gap C — options

The only gap needing a genuinely new record shape: a contract identity (underlying, expiry, strike,
right) and a short-premium position whose basis is negative. It is orthogonal to the equity model
and material in cash terms. Recommended: keep it out of this work and leave it at P3 — do **not**
widen `transactions.side` to accommodate it, which would put option semantics in the path of every
equity replay.

---

## 4. Converter architecture

**Do not extend the four canonical import CSVs to accept broker exports.** They are a *human*
entry format — one row is one intent, hand-editable in a spreadsheet. A broker export is a
*machine* format at a different grain: one row is one cash or share leg, and a single domain event
spans up to three rows (92 such groups in the assessed file). Merging the two grains degrades
both: the human template acquires columns no human fills, and the broker path inherits a shape
that cannot express its own groups.

Two stages, with a broker-neutral intermediate representation between them:

```
broker export ──▶ [adapter] ──▶ RawEvent stream (IR) ──▶ [grouper ▸ mapper] ──▶ ledger writes
                  per broker                             broker-neutral
```

- **Adapter** (one per broker): parse rows, recover symbols from description text, classify each
  row into a closed `EventKind` vocabulary by its `(action, description)` pair, resolve dual dates
  to the trade date. Emits IR; touches no ledger.
- **Grouper ▸ mapper** (written once): fold multi-row groups into domain events, then write. All
  ledger knowledge lives here, so a second broker costs one adapter and no core change.
- **Reconciler**: the invariants below, run as an import gate.

```
portfolio_dash/data_ingestion/broker/
    ir.py          # RawEvent + EventKind — frozen Pydantic, closed vocabulary
    schwab.py      # (action, description) -> EventKind; spans both broker eras
    grouping.py    # fold DRIP triples and paired journals into domain events
    reconcile.py   # the import gate (below)
    registry.py    # broker id -> adapter
```

This respects `architecture.md`: `data_ingestion/` normalises into the canonical model before
persisting and rejects bad input loudly. Nothing here imports upward.

**`reconcile.py` is the point of the design.** The checks that caught both defects in this
exercise — a mis-paired reversal that would have left a position over-stated and double-booked a
reinvest, and the 8 corporate-action rows above — must be a **built-in import gate**, not a
one-off script. Before commit, assert: cash conserved; per-symbol shares conserved; every
suppressed group nets zero on **both** amount and quantity; every priced row reconciles to
`qty × price ± fees` (with 2a's derivation applied); no group reinvests more than it received.
Any failure rejects the **whole batch** — a partially-imported ledger is worse than none, and
§3.2's batch id is what makes rejection clean.

**Offline script first, server-side adapter after** — unchanged from backlog P1a, and reinforced:
the raw export never needs to reach the server, and the transformation logic is where the risk is
concentrated, so it wants regression tests against the real file before it gets a UI.

---

## 5. Open decisions

Each needs an owner ruling before implementation. Recommendation marked ▶.

### D1 — Broker interest and fees in XIRR

Small net cash across ~63 rows, currently reachable only as an undifferentiated cash movement.

| | Option | Consequence |
| --- | --- | --- |
| ▶ **A** | Book to `cash_movements` with distinct `kind` values (`INTEREST`, `BROKER_FEE`), **excluded** from XIRR | XIRR stays a pure investment-return metric; the cash balance still reconciles to the statement. Smallest change. |
| **B** | Include as XIRR flows | XIRR becomes total-account return. Defensible, but changes the meaning of every historical figure and needs a migration note. |
| **C** | Book as a cost-basis adjustment on the related holding | Rejected on sight — most of these rows have no symbol, so there is nothing to attach them to. |

### D2 — Opening inventory whose cost the export does not contain

| | Option | Consequence |
| --- | --- | --- |
| ▶ **A** | Reuse the F1/F2/F3 pattern: nullable cost + a coverage ratio that degrades cost-derived figures | Consistent with a precedent you already signed off. Import proceeds immediately; return rates are explicitly marked partial rather than wrong. Requires making `original_cost_total` nullable + a UI degradation state. |
| **B** | Hard-require the owner to supply every cost before import | Numbers are complete and trustworthy from day one, but ~20 costs must be researched from outside the export before a single row lands. |
| **C** | Import shares at zero cost | **Do not choose.** Produces a plausible-looking, badly wrong return — the exact dangerous failure mode `domain-ledger.md` names for declared shorts. |

A hybrid is available: **A** to unblock the import, with an inbox item per unbased position so the
cost can be filled in later — `domain-ledger.md` N2 already establishes that filling a cost in
later legitimately re-computes history.

### D3 — Scope of the first build

| | Option | Consequence |
| --- | --- | --- |
| ▶ **A** | Equity + dividends + corporate actions + provenance; options and interest deferred | Covers ~75% of rows plus the P0 blocker. Ledger is correct for everything it admits, and rule 7 makes the remainder fail loudly rather than silently vanish. |
| **B** | Add interest/fees (D1) in the same pass | Cash balance ties to the statement exactly; adds one small table change. |
| **C** | Everything including options | Largest scope; options need a new position model and would gate the rest behind it. |

### D4 — Where the converter runs (confirm or revisit backlog P1a)

| | Option | Consequence |
| --- | --- | --- |
| ▶ **A** | Offline script emitting the existing import templates, owner uploads through today's UI | Zero new server surface; raw export never leaves the machine. Backlog's existing recommendation. |
| **B** | Server-side upload endpoint from the start | Better UX, but puts an unproven transformation in the request path and the raw broker file on the server. |

Note that **A** conflicts with §3.2 as written: the current templates carry no provenance columns,
so an offline script cannot populate `import_batch_id` / `source_row_hash` through them. Either
the templates gain optional provenance columns, or provenance arrives with the server-side adapter
in **B**. This needs resolving together with D4 rather than after it.
