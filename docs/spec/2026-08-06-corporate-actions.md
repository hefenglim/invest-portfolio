# Spec — Corporate actions (SPLIT / EXCHANGE / SPINOFF)

**Status:** All owner decisions D1–D39 **approved** (§8; D30–D39 on 2026-08-10, from the
spec-conflict audit and the blast-radius question it prompted — see §8.1). **Implementation in progress** on `feat/corporate-actions` (owner ruling
2026-08-09: the branch is NOT merged into `main` until the whole feature is done, so it stays
abandonable — which also retires **D26**, W0's separate release). Delivered: **W0** LedgerBundle +
ledger registry · **W1** the ratio algebra · **W2** the ledger, CRUD and validation (three soft
warnings still open) · **W3** the replay (its `unbookable_action` output is not yet wired).
§10 is the implementation brief, **rebuilt 2026-08-10**: the build order is now
`P0 → decisions → W6a → W4 → W6b → W5 → E23+W7 → W8/W9/W10`, and every "Done when" cites the §7
test that defines it instead of restating it.

**Revision log**

| Date | Change |
| --- | --- |
| 2026-08-06 | First draft. D1–D7 approved |
| 2026-08-07 | Review round 1 (Fable-5 xhigh): §5.1 E17 rewritten — the original analysis was inverted and D6 superseded; §6.3 `corporate_delta` re-specified; E18–E21 + E1a added |
| 2026-08-08 | Review round 2 (senior review vs the conservation principle): **§3.1(ii)** ratio stored as two terms; **§5.1** remedy made idempotent + the carry-forward cliff replaced by re-expression; **§3.3** the reorganisation fee ruled on; **§2.1** the conservation law written down and given a test |
| 2026-08-08 | Owner directives: **§6.0** module design (模組化集中) and **§6.7** entry-surface design |
| 2026-08-08 | Second formula verification pass: **§4.4** complete field-transfer table + `EventPriority` enum; §5.1 three normative details (short-circuit · prices-only `split_factor` · market-fact deduplication); the price re-expression seam pinned to `dashboard.py:264`; §7.1a summation rules; **§10** implementation brief added |
| 2026-08-09 | **Grill review (Opus 5), demo-verified — owner approved all nine.** Nine unasked decisions found and folded in as **D13–D21**: multi-account partial application HARD-BLOCKED (the claimed ⚠ safety net provably never fires); ratio terms constrained to positive **integers**; same-date intersecting actions **rejected** (ordering changes money and §7.1a is blind to it); **E22** destination-`ever_oversold`; the `pricing/` factor seam resolved by dependency **injection**; §5.1's scope predicate widened past SPLIT; broker identifier strings **normalised to tickers at import**; cash-and-stock mergers written into §9 with a recipe; the SPINOFF child's 回本進度 labelled with its provenance. Evidence + interactive proof: `docs/spec/2026-08-09-corporate-actions-grill.html` (85/85 self-checks, `verify_report.py` PASS) |
| 2026-08-09 | **Three prior claims measured and CORRECTED** — §3.1(ii)'s worked example was arithmetically wrong, §7.1's detection-power companion test was unsatisfiable as written, and the trap ranking in §10.4 was aimed at the weaker of the two ratio defects. See §3.1(ii) |
| 2026-08-09 | **Grill round 2 — owner approved all seven, folded in as D22–D28.** D18's EXCHANGE clause **withdrawn** (folding it in revealed it would corrupt the price history of any already-held merger destination); the `holdings.py` walker specified (§6.2); identifier refusal keyed on registration rather than string shape; demo-site corpus (§7.7) and the owner-run acceptance script (§10.5) added; W0 promoted to its own release |
| 2026-08-09 | **Both round-2 deviations resolved — no open items.** D22: "reject or warn" was a false dichotomy; `validate.py` already has a THIRD tier (`needs_confirm`, the 賣超 tier) and E23 takes it, with a four-part condition that fires on the identifier signature and stays silent on ordinary mergers, plus a one-click convert-to-SPLIT. D23: the omitted cycle check is confirmed, and the two details that made the termination argument load-bearing are now normative — the recursion's **strictly-before** date bound (the inclusive form hangs) and the depth cap's **degrade-not-raise** behaviour on read paths (`corporate_delta` reaches an API route). Both outcomes are stronger than either originally-offered option |
| 2026-08-09 | **W2 implementation revised E15 (D29).** The duplicate action is now a HARD rejection checked BEFORE E12, not a soft warning. Found by W2's own test: soft would apply the ratio twice (3-for-1 → 9-for-1), and as specified the warning was **unreachable**, since an exact duplicate is by construction the same-date intersecting pair E12 already rejects — the same "the ⚠ provably never fires" defect the E13 note exists to correct, recurring one row later. Also recorded: **D26 is retired** — the branch stays unmerged by owner ruling, so W0 does not ship as its own release |
| 2026-08-10 | **Spec-conflict audit folded in** (`docs/audit/2026-08-10-spec-conflict-audit.md`) — **owner approved D30–D37**, each on the audit's stated recommendation: two-column price basis (**D30**), the depth cap keeps its `Decimal` signature and degrades through the existing flag / `needs_confirm` mechanisms (**D31**), a dividend on an EXCHANGE-vacated symbol is refused and flagged — new **E24** (**D32**), the SQL path skips an action on a negative source (**D33**), §9's cash-and-stock recipe **withdrawn** (**D34**), US cash dividends reduce `adjusted_total` (**D35**, recorded in the backlog spec), a whole-account IRR joins XIRR and resolves D12's blind spot (**D36**), and pre-history opening cost totals with `original_cost_total > 0` hard-validated (**D37**). **§10.2 rebuilt** — the section the document calls the implementer's brief was its stalest text: every "Done when" now cites the §7 test that defines it, W6 splits into **W6a/W6b**, and the build order becomes `P0 → decisions → W6a → W4 → W6b → W5 → E23+W7 → W8/W9/W10`. Propagation repairs: E15 into §6.5's hard list before E12 (F-01); ~~D26~~ and ~~D18~~ struck as SUPERSEDED with W0's and W6's rows rewritten and traps #18/#19/#23 corrected (F-02, F-03); D20's §7.1a exception removed with the recipe it existed for (F-04); E10 covers **either** symbol (F-35); §4.4 gains `unbookable_action` and its count corrected to nine (F-37); §2.1's value leg qualified to SPLIT + a third §2.1a blind spot (F-38); §7.1a's impossible `unbookable_dividend` claim deleted (F-39); "identifier-shaped" → "unregistered" (F-41); the ratio products written over the **dedup key** (F-42); §4's preamble re-pointed at the measured defect (F-43); `dashboard.py` anchored on the construct, not the line number (F-44) |
| 2026-08-10 | **D38 — blast-radius containment (owner question, same day).** Asked whether a symbol with no corporate action can be guaranteed identical to pre-feature `main`, so that a defect in the new flow damages only the triggering stock. Ruled: **no runtime "sandbox mode"** — two maintained paths and an off-configuration is how the aggregate-vs-detail divergence recurred three times and how `mock-data.js` and the four ledger enumerations drifted. Instead **three testable invariants** (§8.1): name and test the containment that already holds structurally, preferring a short-circuit over an equal-answer computation (binds **W4** hardest); accept XIRR's portfolio-wide blanking but make its reason **name the account, symbol and date**; and prove `prices` **byte-identically reversible** on deleting a split. Recorded with it: 重算 and `corporate_delta` are both strictly stronger than a sandbox — one *erases* the damage, the other *displays* it per symbol — and **reversibility is D30's second, independent justification**, since `prices` is the only non-replayable mutation in the feature |

| 2026-08-10 | **D39 — two W6a implementation findings, owner sign-off.** (a) The `scheduler → data_ingestion` edge the injected split factor needs was not merely absent but **guarded by a test**; the guard is narrowed to a one-line allowlist and **`.claude/rules/architecture.md`'s dependency diagram now carries the edge** — an edge that exists in code but not in the diagram is the next audit's F-01. (b) §5.1's "all four prices take the same factor" is **reversed**: only `close` is re-expressed, because it is the only price with a preserved raw column, and multiplying `open`/`high`/`low` would put them outside D38's reversibility invariant. Verified zero readers; `split_basis` keeps their basis recoverable and `pricing/schema.py` carries the warning |

**Priority:** P0 — the only *blocking* gap found by the 2026-08-06 broker-import assessment
(`docs/spec/2026-08-06-broker-import-backlog.md`).
**Scope of this version:** a complete, self-contained feature — ledger table, replay semantics,
entry surfaces, CSV import, and the full verification set. Options-related corporate actions are
explicitly **out of scope** (they belong to P3).

---

## 1. Why this blocks everything else

A corporate action changes a share count **without moving cash**. The ledger has no way to
express that: `transactions.side` is `BUY` or `SELL` only, and both settle money.

The consequence is not a missing report line. `build_book` raises/degrades on a sell that exceeds
the held shares, and — since 2026-07-30 — that degradation is **STICKY**:

```python
pos.ever_oversold = True          # cost_basis.py, the oversell branch
pos.original_total = _ZERO
pos.adjusted_total = _ZERO
```

A later buy nets the position positive again but **does not restore the discarded basis**
(`domain-ledger.md`: "賣超 is STICKY"). So a 3-for-1 split that cannot be recorded turns every
subsequent sell of that position into a permanent loss of its cost basis — silently, with the
position still rendering a number.

That guard is correct and must not be weakened. The fix is to make the share increase
**recordable**, not to make the guard forgiving.

---

## 2. What the accounting must preserve

These follow from `CLAUDE.md` and `domain-ledger.md` and are non-negotiable:

| Invariant | Consequence for this feature |
| --- | --- |
| `original_cost` is never overwritten (CLAUDE.md #7) | Binds the **ledger rows**, not the replay accumulator. A corporate action never edits a transaction; it is a new row that the replay reads. `_Position.original_total` is a running total that EXCHANGE zeroes and SPINOFF scales — that is not "overwriting original cost", because the next 重算 rebuilds it from the same unchanged ledger |
| `average_cost = total / shares`, computed on read | **Nothing else has to change.** The average corrects itself the instant the share count does |
| No double counting (CLAUDE.md #6) | A corporate action is not income and not a purchase. It emits **no** `RealizedRow` and does **not** touch `gross_invested` |
| All reports rebuild from the ledgers (重算) | A corporate action is a *ledger row*, never a snapshot or an adjustment applied to a computed result |
| Money is never a float | `cost_carry` is `Decimal` stored as TEXT. The **ratio is NOT a Decimal** — see §3.1 |
| **Total net worth is continuous across the event** | The conservation law of §2.1 — the property this whole feature is measured against |

### 2.1 The conservation law (review finding N1)

Everything above is a rule about one field. This is the rule about the **whole book**, and it is
the owner's stated first principle: *net worth grows or shrinks; cost is the cash actually put in.*
A corporate action re-labels and re-denominates an existing position. It creates and destroys
nothing. Therefore, at the instant any action applies, **within each quote currency**:

```
Σ original_total    (over all positions)   unchanged
Σ adjusted_total                           unchanged
Σ dividend_portion  ( = Σorig − Σadj )     unchanged   (follows from the two above)
gross_invested                             unchanged
Σ shares × price                           unchanged   for a SPLIT — qualified below, and §2.1a
```

Verified algebraically against the §4 formulas:

| | SPLIT | EXCHANGE | SPINOFF |
| --- | --- | --- | --- |
| Σ `original_total` | untouched | `−P + P = 0` | `−c·P + c·P = 0` |
| Σ `adjusted_total` | untouched | `−P + P = 0` | `−c·P + c·P = 0` |
| `gross_invested` | untouched — only `opening` and `buy` add to it (`cost_basis.py:108`, `:145`) | untouched | untouched |

This is **not** self-evident from the per-formula tests, and §6.6's "asserted-unchanged" set proves
only that *other modules* are unaffected. The law itself gets its own property test (§7.1a). It is
also the cheapest possible detector for the two defects this revision fixes: a rounded ratio (§3.1)
breaks the share leg, and a mis-denominated price (§5.1) breaks the value leg.

**The value leg is qualified — SPLIT only (F-38, 2026-08-10).** The four basis lines above hold for
every kind. `Σ shares × price` does not: §5.1's price re-expression is **SPLIT-scoped by D22**, so it
is what makes a split date continuous, and nothing re-expresses anything for the other two kinds —
deliberately, because an EXCHANGE *adds to* its destination rather than re-denominating it (trap
#16), and a SPINOFF's child begins its own series at the action. An EXCHANGE onto a destination whose
series is missing, or quoted in different terms, therefore moves the value leg while every basis sum
stays exactly equal. That is a **blind spot of the law, not a breach of it** — the law is about
basis, and the basis is conserved — so it is enumerated as the third row of §2.1a rather than tested
here. Stating the value leg unconditionally, as this line did until 2026-08-10, invites the
"widen §5.1's scope" repair that trap #16 exists to stop.

**The two deliberate exceptions.** Both are real economic events that genuinely change net worth,
so they must NOT be conserved — and both are booked *outside* the action row:

1. **Cash-in-lieu of a fraction** → an ordinary SELL (§3.2). Real disposal, real realized P&L.
2. **A reorganisation fee** → §3.3. Real cost.

A conservation test that accidentally includes either of them is testing the wrong thing; the
fixture must keep them on separate dates from the action being measured.

> **The D20 exception-to-the-exception is WITHDRAWN (D34, 2026-08-10).** It read: "§9's
> cash-and-stock recipe puts a SELL leg and an EXCHANGE leg on the SAME date by construction, so
> §7.1a measures only the exchange leg for such a fixture." It existed for exactly one fixture — and
> **D34 withdraws the recipe that fixture came from** (§9: cash-and-stock stays a hard exclusion,
> because `EventPriority` runs CORPORATE_ACTION before SELL, so the EXCHANGE zeroes the source and
> the SELL then produces `OversellError` / STICKY 賣超). No normative fixture dates a SELL together
> with an action any more, so the rule has no case left to govern and a per-leg carve-out would be a
> mechanism kept alive for nothing. §7.1a therefore keeps the simple form: **the fixture keeps the
> action alone on its date.**

### 2.1a What the conservation law CANNOT see (2026-08-09, grill findings D15 and D20)

The law is the cheapest detector in the plan, and §2.1 says so. It is not a complete one, and two
approved decisions exist precisely because it is blind to them. Stating the blind spots here stops a
future reader from treating a green §7.1a as proof of correctness:

| Blind to | Why | Covered instead by |
| --- | --- | --- |
| **Action ORDERING on a shared date** | Both orderings move the same basis between the same positions, so Σ`original_total` is identical either way — while the SHARE count, and therefore every average and market value, differs by a whole ratio (measured: 600 vs 200 shares, Σ = 5,000 in both) | **D15** — the state is rejected at validation, so it cannot exist |
| **A consideration leg that leaves the book entirely** | Cash paid out in a merger reduces nothing the law sums; Σ`original_total` is unchanged because the money simply left | **D34 (2026-08-10)** — the event is a hard exclusion, so the state is not enterable. D20's two-row recipe, which used to book the cash leg as a SELL the law could see, is **withdrawn** (§9) |
| **A value discontinuity across an EXCHANGE** (added 2026-08-10, F-38) | The basis legs are conserved by `−P + P = 0` **whatever the destination's price series says**, so the law stays green through a measured **95% net-worth cliff** (1-for-20 over 200 shares at 0.4250: 85.00 → 4.25, §5.1 "Scope"). The share count moves by the ratio and the price leg does not follow it, because D22 keeps the re-expression SPLIT-only | **D19** — the identifier is normalised to its ticker at import, so the event is entered as a **SPLIT** and never reaches this state — and **E23**, the `needs_confirm` guard on the four-part identifier signature for rows that pre-date D19, which offers a one-click convert-to-SPLIT. **After the owner acknowledges E23 the row commits as an EXCHANGE and the cliff remains**: no price factor is applied to either series (that would be trap #16), so what the acknowledgement buys is that the discontinuity is *recorded and seen* rather than silent. Acknowledging is the mitigation; converting to a SPLIT is the repair |

The first two were found by demo, not by reading: the ordering case renders a PASS badge next to a
number that is wrong by 3×. `docs/spec/2026-08-09-corporate-actions-grill.html` §Q3 and §Q8
reproduce them. The third was found by the 2026-08-10 spec-conflict audit, by reading §2.1's value
leg against D22's scope.

---

## 3. Data model

```sql
CREATE TABLE IF NOT EXISTS corporate_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  TEXT NOT NULL,
    date        TEXT NOT NULL,          -- effective date (ISO)
    kind        TEXT NOT NULL,          -- SPLIT | EXCHANGE | SPINOFF
    from_symbol TEXT NOT NULL,
    to_symbol   TEXT NOT NULL,          -- == from_symbol for SPLIT (enforced: E20)
    -- The ratio is a RATIONAL, stored as its two terms — NEVER as one decimal (§3.1).
    -- "3-for-1" is (to=3, from=1); "1-for-10" is (to=1, from=10); "2-for-7" is (2, 7).
    -- BOTH terms are positive INTEGERS (D14). "Decimal > 0" was not enough: it let a rounded
    -- quotient in through the CSV importer and the API, which is the very defect §3.1(ii)
    -- exists to prevent. Enforced at validation (E6), not merely by the entry form.
    ratio_to    TEXT NOT NULL,          -- positive integer: shares received
    ratio_from  TEXT NOT NULL,          -- positive integer: shares surrendered
    cost_carry  TEXT,                   -- Decimal in [0,1]; SPINOFF only, NULL otherwise
    note        TEXT
);
```

Additive migration via the existing `create_tables` path; every existing DB migrates in unchanged
(no column added to any existing table).

Throughout this document `ratio` is shorthand for the value `ratio_to / ratio_from`. It is a
notation, **not a stored column** — the replay never materialises it as an intermediate.

### 3.1 The ratio is a RATIONAL, and it is the authority (REVISED 2026-08-08, review finding B1)

Two separate decisions, and the first version got the second one wrong by applying the first
one only halfway.

#### (i) A ratio, not an absolute post-action share count — unchanged

The alternative — storing "85 → 255" straight off the statement — was rejected:

- A ratio is **exact and externally verifiable** (`TSLA 3:1` is public record). An absolute count
  is only correct relative to *your* pre-action share count.
- If your ledger's pre-action share count is wrong (a missed DRIP, say), an absolute post-count
  **silently papers over the discrepancy** and hard-codes the error into the position. A ratio
  leaves the discrepancy visible, which is the behaviour this whole feature exists to produce.
- It is the same principle as `average = total / shares`: store the input, derive the result.

#### (ii) Two terms, not one decimal — the correction

The first version then stored that ratio as a single `Decimal`, which **re-commits the very
mistake (i) rejects, one level down**: a decimal ratio is a rounded quotient, and
`data-and-pricing.md` forbids storing a rounded quotient as the authority.

Real corporate actions are quoted as ratios (`3-for-1`, `1-for-10`, `2-for-7`), and not all of
them have a finite decimal expansion. The error is then bounded only by **how many digits the
owner happened to type**:

```
700 shares × Decimal("0.2857")         = 199.9900       ← 0.01 shares short
700 shares × Decimal("0.2857142857")   = 199.9999999900 ← more digits does NOT fix it
700 shares × Decimal(2) / Decimal(7)   = 200            ← exact (multiply first, divide last)
700 shares × (Decimal(2) / Decimal(7)) = 200.0000000000000000000000000   ← also 200; see below
```

`validate.py:167` compares with a **bare `>` and no epsilon**
(`if inp.quantity > held_then or inp.quantity > held`). So a later sell of 200 raises
`sell_exceeds_holdings`; the owner acknowledges it; and the STICKY 賣超 guard **discards the cost
basis permanently**. That is precisely the disaster §1 says this feature exists to prevent —
re-created by the storage format of the fix.

#### The 2026-08-09 correction — the earlier version of this table was wrong

The line above previously read `700 × (Decimal(2)/Decimal(7)) = 199.999…9900`. **Measured with the
project's own interpreter, that is false**: the 28-significant-digit context rounds the product back
up, the result compares `== Decimal(200)`, and `Decimal(200) > it` is **False** — the guard does not
trip. Two things follow, and both were live defects in this document:

1. **§7.1's mandated detection-power companion test was unsatisfiable.** It required asserting that
   `shares × (to / from)` does *not* equal 200 for exactly this case. Written as specified it fails
   on its first run. Re-pointed in §7.1 at a fixture that genuinely differs.
2. **The two defects were ranked the wrong way round.** An exhaustive sweep (share counts 1–1,000 ×
   `to` 1–20 × `from` 1–20 = 400,000 combinations) finds 77,577 pairs where the parenthesised form
   differs — and **3,530 of those do cross an integer boundary**, e.g. `3 × 1/3` is exactly `1`
   multiply-first and `0.999…9` parenthesised; the largest such share count is 935 (`935 × 18/17`).
   So evaluation order is a real defect, not a cosmetic one. But its magnitude is ~10⁻²⁷ and it only
   bites in a narrow small-share-count band, whereas a **typed decimal** is ~5×10⁻⁵ and bites at
   **any** scale. Both must be defended; the typed decimal is the one that was left undefended.

**Therefore, two rules, not one:**

**(a) Evaluate in one expression** so the division happens **last**, against a value large enough to
absorb it:

```python
new_shares = pos.shares * ratio_to / ratio_from      # exact for 700 × 2 / 7
```

Not `pos.shares * (ratio_to / ratio_from)` — 3,530 measured counter-examples. This ordering is a
**correctness requirement, not a style preference**, and it gets its own test (§7.1).

**(b) BOTH terms must be positive integers (D14).** Rule (a) protects the arithmetic; it does
nothing about the *input*. `ratio_to = 0.2857, ratio_from = 1` satisfies "Decimal > 0", passes the
CSV importer and the API, and reproduces the cascade exactly — the two integer boxes in §6.7's form
constrain the form and nothing else. Every real corporate action is expressible as an integer pair
(`3-for-1`, `1-for-20`, `2-for-7`, `8-for-5`), so nothing legitimate is lost.

> **The one case this rejects that a broker might really publish:** a merger announced as a decimal
> exchange ratio (`0.8161 shares of B per share of A`). The correct entry is the fraction the
> registrar actually used, not the rounded decimal the press release printed. If no fraction is
> obtainable, the action must not be entered as a guess — the same posture `cost_carry` already
> takes (§4.3) and `acq_home_amount` takes in the FX pool (`domain-ledger.md` F1). Recording
> `8161/10000` is legal and exact, and the entry form's preview shows the resulting share count so
> the owner can check it against the statement before saving.

`cost_carry` stays a single `Decimal` because its source is different in kind: an 8-K publishes an
allocation **percentage** (`58.31%`), which is already exact as a decimal. The parent's share is
never stored — it is computed as `1 − c` on read, so parent and child sum to exactly 1 with no
rounding leak (the same reason the average is computed on read).

**The entry form must display the computed resulting share count** so the owner can compare it
against the statement before saving. That is the check the absolute form would have provided,
without making the wrong number authoritative.

### 3.2 The broker rounded — where does the fraction go?

A reverse split (and most spin-offs) round to whole shares and pay **cash in lieu** of the
fraction. That cash is a real disposal with a real gain or loss. It is therefore recorded as an
**ordinary SELL transaction** at the implied price (`cash received / fractional shares`), *not*
as part of the corporate action.

This keeps one rule — *the ratio is applied exactly* — and produces correct realized P&L on the
fraction. Verified against the real data: a 1-for-4 spin-off of 74.878 parent shares yields
18.7195, the broker delivered 18 shares plus cash for 0.7195 at an implied price consistent with
the child's opening price.

### 3.3 The reorganisation fee — where it goes, and what that costs (review finding B4)

The first version said "the fee is a `cash_movements` debit". **There is no such thing.** Measured:

| Fact | Evidence |
| --- | --- |
| The only movement kinds are DEPOSIT / WITHDRAW / OPENING / REBATE | `api/routers/cash.py:68` — `_KINDS = {"DEPOSIT", "WITHDRAW", "OPENING", "REBATE"}` |
| Everything except WITHDRAW is a **credit** | `portfolio/cash.py:77` — `return Decimal("-1") if kind == "WITHDRAW" else Decimal("1")` |
| Cash movements never reach XIRR | `portfolio/returns.py:135-146` — the flow series is built from `opening` + `transactions` + `dividends` only |
| A foreign WITHDRAW recognises no realized FX | `forex/pools.py:66`, and `domain-ledger.md` N1 |

So the only bookable form is a `WITHDRAW`, and that carries three consequences: it reads in the
cash statement as *the owner took money out*, it reduces the FX pool's exposure without
recognising a loss, and it is **invisible to both `xirr_reporting` and `total_return`** — a real
cost that no return metric can see.

**Ruling for this version: book it as `WITHDRAW` with a `note`, and record the understatement as a
known limitation.** Reasons:

- The alternative — a `FEE` kind wired into `_movement_sign` **and** into the XIRR flow list — is a
  money-of-record change to `returns.py` whose blast radius is every XIRR in the system, not just
  the ones touching a corporate action.
- It is not one fee. The import assessment already counts a whole class that never reaches XIRR
  (bond/margin interest, interest adjustments, ADR management fees, foreign tax reclaim). Solving
  it for the reorganisation fee alone would leave the class half-handled and the accounting manual
  self-contradictory.
- That class is **P2's** scope (`docs/spec/2026-08-06-broker-import-backlog.md`). This spec must
  not invent a partial answer that P2 then has to unpick.

Recorded as decision **D12** (§8) because "should a fee move the reported return" is the owner's
call, not an implementation detail. Whichever way it goes, the accounting manual states it
explicitly — a silent omission is the one outcome that is not acceptable.

> **The limitation is no longer permanent (D36, 2026-08-10).** "Invisible to every return metric"
> was true of the metrics that existed when this was written. D36 leaves **XIRR untouched** — every
> historical figure, every manual anchor and every oracle expectation stays where it is — and adds a
> **whole-account IRR** in the existing `portfolio/twr.py`, which does see a `WITHDRAW`. So the fee
> is invisible to XIRR **by design**, and visible in the second metric. §7.5 and W9 must document it
> that way; writing it up as a permanent blind spot would be recording a decision that has been
> reversed.

### 3.4 A broker identifier string is NOT a symbol (D19, 2026-08-09)

When a security's identifier changes — a merger, a mandatory reverse split, a CUSIP reassignment —
the broker's export writes the old position out under its **raw identifier string** and the new
position in under the **ticker**. Often the ticker itself never changed; only the identifier moved.
Several such strings appear in the owner's export, one of them on dozens of rows.

The question the spec never asked: **does that string become a first-class `instruments` row?**
E10 requires both `from_symbol` and `to_symbol` to be registered, and E20 forbids
`to_symbol == from_symbol` on an EXCHANGE — so taking the export at face value forces the answer
"yes". Everything downstream follows from that one string decision:

| | Identifier becomes a symbol | **Normalised to its ticker (ADOPTED)** |
| --- | --- | --- |
| `instruments` | gains a row that is not a security, needing a market / quote ccy / sector it does not have | unchanged |
| Quotes | no provider resolves it → it sits in `RefreshSummary.failed` on **every run, forever**, in the freshness panel (`pricing/refresh.py:39-43`) | nothing to quote |
| Where it is visible | after the action its position holds 0 shares, so `cost_basis.py:294` drops it from holdings — invisible where it would be explained, visible only where it looks like a fault | n/a |
| Forced event kind | `from != to` ⇒ **EXCHANGE** ⇒ inherits §5.1's re-denomination problem in full | `to == from` ⇒ **SPLIT**, which §5.1(d) already handles |
| Elsewhere | symbol picker, sector allocation and the instrument list each carry a non-security | unchanged |

**Rule.** The importer resolves a raw identifier to its ticker from the ticker appearing on sibling
rows of the same broker event group, and **stops and asks the owner when it cannot** — never
guesses, the same posture as `cost_carry` and `acq_home_amount`. The identifier is retained in the
action row's `note` for provenance, not as a symbol.

**This is narrow on purpose.** It converts *identifier changes*, not mergers. A genuine merger into
a **different** ticker stays an EXCHANGE — that is a real change of security, and D18's widened
price scope exists to cover it. Getting this distinction wrong in the other direction (normalising a
real merger away) would silently merge two companies' cost bases.

**How the ledger refuses one — a FACT, never a string shape (D24, 2026-08-09).** R2-Q3 asked how to
detect "identifier-shaped". The answer is: **do not detect it.** Any regex for a CUSIP-like token
(nine alphanumerics, a leading letter, a check digit) will eventually reject a legitimate ticker, and
a false positive here blocks the owner from recording a real event with no way around it.

The guard already exists and needs no new logic: **E10 requires both symbols to be registered in
`instruments`.** A raw identifier is not registered, because D19 says it never becomes an
instrument. So the rejection is keyed on a *fact about the database*, not on how the string looks —
zero false positives by construction. Only the **message** branches, and only as a hint:

| Situation | Message |
| --- | --- |
| `from_symbol` unregistered, and it appears as the pre-change identity of a registered symbol elsewhere in the import | 「`<x>` 看起來是券商的識別碼。請改用它的股票代號 —— 同一檔證券的識別碼變更應記為分割(SPLIT)。」 |
| `from_symbol` unregistered, no such relationship | 「`<x>` 尚未登錄。若這是一檔真實證券,請先登錄後再記錄此行動。」 |

Getting the hint wrong costs the owner one confusing sentence. Getting a *shape test* wrong costs
them the ability to record their own ledger.

**Placement:** the resolution step belongs to the P1a broker-CSV converter
(`docs/spec/2026-08-06-broker-import-backlog.md`), not to this version's manual entry form — but the
*rule* is recorded here because it decides this feature's data model, and because W2's validation
must never auto-register an unregistered `from_symbol` to make an action fit.

---

## 4. Replay semantics

`build_book` (`portfolio/cost_basis.py`) sorts events as `(date, priority)`. Priorities become:

```
opening 0  ->  CORPORATE ACTION 1  ->  buy 2  ->  sell 3  ->  dividend 4
```

A corporate action is effective at the **start** of its date: a same-day buy or sell trades in
post-action terms (post-split price, new ticker), so the action must apply first. Opening
inventory dated on an action date is treated as **pre-action** (it describes the position as it
stood before) — documented, and a soft warning at entry because it is inherently ambiguous.

Notation below: `P` is the `_Position` for `(account, from_symbol)`, `Q` for `(account, to_symbol)`.

**Evaluation order is normative (§3.1(ii)).** Every `× ratio` below means
`× ratio_to / ratio_from` evaluated left to right — multiply first, divide last. Never
`× (ratio_to / ratio_from)`: the parenthesised quotient rounds to the Decimal context precision
before it ever touches the share count. **Reworded 2026-08-10 (F-43)** to the measured form, because
this preamble still carried the ranking §3.1(ii)'s 2026-08-09 correction reversed: the parenthesised
form is a **real** defect — 3,530 of a 400,000-pair sweep cross an integer boundary, e.g. `3 × 1/3`
is exactly `1` multiply-first and `0.999…9` parenthesised — but its magnitude is ~10⁻²⁷ and it bites
only in a small-share-count band. What re-creates the 賣超 cascade **at any scale** is a **typed
decimal** ratio term (~5×10⁻⁵), which is defended at validation by E6a/D14, not by this rule. Both
are required; do not read this line as making the evaluation order the whole defence.

### 4.1 SPLIT — `from_symbol == to_symbol`

```
P.shares          := P.shares × ratio_to / ratio_from
P.short_shares    := P.short_shares × ratio_to / ratio_from     # see E4
P.original_total  := unchanged
P.adjusted_total  := unchanged
P.short_proceeds  := unchanged
```

No realized row. `gross_invested` unchanged. `ratio > 1` is a forward split, `ratio < 1` a
reverse split; the same formula covers both.

`original_avg` and `adjusted_avg` divide by the new share count on read, so both averages scale by
`1/ratio` automatically. `dividend_portion = original_total − adjusted_total` is unchanged, and
`payback_ratio` (its quotient over `original_total`) is therefore unchanged — correct, since a
split changes nothing about how much of the cost has been returned as dividends.

### 4.2 EXCHANGE — the whole position moves to a new symbol

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

Covers a de-SPAC conversion, a merger, and a pure ticker/CUSIP rename (`ratio = 1`).

If `Q` already holds a position, the two merge by weighted average — which is simply the sum of
the totals over the sum of the shares, exactly what the weighted-average method prescribes. No
special case.

`gross_invested` is **not** touched: no new capital entered.

### 4.3 SPINOFF — the parent keeps its position, a child is created

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

The parent is written as **`total − carved`, not `total × (1 − c)`**. Algebraically identical,
numerically not: `1 − c` rounds once and `× (1−c)` rounds again, so the two sides can miss the
conservation law of §2.1 by an ulp. Subtracting the exact amount that was added to the child makes
`Σ original_total` conserved **by construction** rather than by luck — the same reason the parent's
share of the allocation is never stored (§3.1).

Scaling **both** totals by the same factor preserves `dividend_portion` proportionally on each side,
so the conservation law holds and each side's arithmetic is internally consistent.

**But the child's 回本進度 label is not (D21, 2026-08-09).** Scaling both by the same `c` makes the
ratio *identical* on both sides — the `c` cancels:

```
child payback = c·(orig − adj) ÷ (c·orig) = (orig − adj) ÷ orig = the parent's payback, exactly
```

So a company spun off last year, which has never paid a dividend in its existence, renders
**「已回收 40% 成本」** — a figure produced entirely by dividends someone else paid, years before the
child existed. Under weighted average that is arguably the honest number (the basis carried over, so
the recovery carried with it), and **the arithmetic is not changed**: this is a truth-in-labelling
ruling, not a calculation fix.

**Rule:** where a position's basis originates from a SPINOFF carve-out, 回本進度 renders with its
provenance — 「已回收 40.00%(承接自 <parent>)」 — on the drawer and anywhere else the figure
appears. Nothing is hidden and nothing is invented; the reader can tell where it came from.

Two consequences to implement with it:

- **`fully_recovered` (已回本) inherits too.** When cumulative dividends exceed cost, the child
  prints 「已回本・配息已完全沖減成本」 (`symbol.py:138`, `detail.js:457-458`) on its first day of
  existence. It carries the same provenance suffix.
- **`cost_carry == 1` (E9) MIGRATES the figure, it does not merely copy it.** The parent keeps its
  shares with zero basis, and `cost_basis.py:299`'s `else _ZERO` guard then makes the parent — the
  entity that actually collected every dividend — read **0.00%**, while the child reads the full
  40%. E9's soft warning text must say this explicitly; a warning that does not name the
  consequence is not a warning.

`cost_carry` comes from the company's Form 8-K allocation and **is never guessed, interpolated or
defaulted**. A SPINOFF row without it is rejected at validation — the same posture as
`acq_home_amount` in the FX pool spec (`domain-ledger.md` F1) and as `reinvest_shares` on a DRIP.

### 4.4 Complete `_Position` field transfer table — NORMATIVE

The formulas above name only the fields they change, which leaves the rest to inference. `_Position`
has **nine** fields (`cost_basis.py:52-72`); every one of them gets an explicit rule here, because
"the formula didn't mention it" is not a specification.

> **Count corrected 2026-08-10 (F-37).** This paragraph said *seven* while the table listed *eight*
> rows and the class already had *nine* — `unbookable_action`, W3's own output, occurred nowhere in
> `docs/spec/`. A section whose stated reason for existing is that inference is not a specification
> cannot be allowed to fall behind the class again, so a **field-count assertion** is being added to
> the test suite: it fails when `_Position` gains a field this table does not list. Adding a field
> therefore forces a row here, in the same change.

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
| `unbookable_action` *(added 2026-08-10)* | unchanged | OR-ed into Q: `Q.unbookable_action \|= P.unbookable_action` (`cost_basis.py:181`) | same OR into the child (`:203`); `P` keeps its own |

**Why the DESTINATION's `ever_oversold` needs its own rule (E22, D16).** The first version of this
table reasoned only about the source: "E3 rejects the action, so it is `False` here." That covers
`P` and says nothing about `Q`. Measured consequence of the gap: `Q.original_total +=
P.original_total` deposits **real money** onto a position whose basis the STICKY guard deliberately
discarded — the mirror of E19, except E19 was only about a flag and this is about the cost basis
itself. Before the exchange the position reads 均價 0 / 未實現 +1,890 and nobody believes it; after,
it reads 均價 33.33 / 未實現 −660, which looks entirely ordinary while the 60 shares with no basis
are silently averaged into a real one. See §5's E22 row and the demo report's §Q4.

**Why `unbookable_action` propagates (F-37, 2026-08-10).** It is E19's rule applied to the flag W3
itself raises: a position that carries a skipped action holds shares in **pre-action terms against
post-action prices**, and moving that position onto a successor without the flag launders exactly
the state the flag exists to announce. The implementation already does this
(`cost_basis.py:181`, `:203`); it was the *table* that had not been told. Note the asymmetry with
`shares` — the flag is OR-ed into the destination and the source **keeps** its own, because on the
dashboard path the source may still be a live position.

**Why EXCHANGE must explicitly zero the short fields even though E5 says they are already zero.**
They are *nearly* zero, not zero. Covering a short does
`short_avg = short_proceeds / short_shares` then `short_proceeds -= short_avg * cover`
(`cost_basis.py:122,137`). On a full cover that is `P − (P/S)×S`, and Decimal division is inexact
whenever `S` does not divide `P` — so a residue `ε` survives. Today it is invisible: the emitted
`shares` is `0 − 0` and the holdings loop drops the position (`:294`). But EXCHANGE leaves the
source position in the dict with a *live* meaning, and `original_total = pos.original_total −
pos.short_proceeds` (`:292`) would then subtract `ε` from a position that a later buy on the old
ticker could reopen. Zeroing all three is one line and removes the whole class.

**Event priority is a named, spaced enum — not a renumbered literal.** Inserting the action at
priority 1 forces buy/sell/dividend to renumber, and `cost_basis.py` writes those literals in two
places (`:95`, `:97`) with a third copy in the docstring (`:70`). An agent that updates two of the
three produces a silently mis-ordered replay. So:

```python
# shared/ledger_events.py
class EventPriority(IntEnum):
    OPENING = 0
    CORPORATE_ACTION = 10
    BUY = 20
    SELL = 30
    DIVIDEND = 40
```

Spaced by 10 so the next event type inserts without touching any existing value, and named so the
docstring is no longer a third place to keep in sync. The stress-audit oracle keeps its **own**
copy (§7.4).

---

## 5. Edge-case matrix

Every row below gets a test. "strict" = `allow_oversell=False` (重算 / what-if / tax export);
"dashboard" = `allow_oversell=True`.

| # | Situation | Strict path | Dashboard path |
| --- | --- | --- | --- |
| E1 | Action on a symbol with **no prior position** | `UnbookableLedgerError` — fabricating a position would invent a $0-cost ghost (mirrors the dividend branch) | **skip + flag** (see E1a) |
| E2 | Action on a **closed (0-share, 0-basis)** position | `UnbookableLedgerError` with a zh message | skip the event, flag the position |
| E3 | Action on an **oversold** position (`ever_oversold`, basis already discarded) | `UnbookableLedgerError` | skip; the 賣超 flag stays. Scaling an undefined basis produces an undefined result |
| E4 | **SPLIT on an open declared short** | Supported: `short_shares × ratio`, `short_proceeds` unchanged. You owe more shares, you still received the same money. Average sale price scales correctly | same |
| E5 | **EXCHANGE / SPINOFF on an open declared short** | `UnbookableLedgerError` — no honest booking exists (precedent: dividend-on-short) | skip + flag |
| E6 | `ratio_to` or `ratio_from` ≤ 0, non-numeric, or non-finite | Rejected at **validation**, never reaches the replay. `ratio_from == 0` would be a division by zero *inside* the replay, i.e. a 500 on the dashboard path — so this rejection is load-bearing, not cosmetic | — |
| **E6a** | **A NON-INTEGER ratio term** (`ratio_to = 0.2857`), from any path | Rejected at **validation** (D14). The earlier wording — "not representable, the form takes two fields" — was wrong: the form constrains the form, while the CSV importer and the API both accepted a decimal and reproduced the cascade of §3.1(ii). A one-column legacy import remains a HARD parse error, never coerced | — |
| E7 | `ratio == 1` on a **SPLIT** | Soft warning at entry (a no-op row). **SPLIT only** — `ratio == 1` on an EXCHANGE is the ordinary rename case and must NOT warn | — |
| E8 | `cost_carry` outside `[0,1]`, or absent on a SPINOFF | Rejected at validation | — |
| E9 | `cost_carry == 1` on a SPINOFF | Soft warning: the parent keeps its shares with **zero** basis — legal but almost always a data error | — |
| E10 | **Either** symbol — `from_symbol` **or** `to_symbol` — not registered in `instruments` (**corrected 2026-08-10, F-35**: this row said `to_symbol` only, while §3.4, §6.5 and `validate.py:335` all say both, and **D24's identifier guard is void unless `from_symbol` is covered** — refusing a raw broker identifier *is* refusing an unregistered `from_symbol`) | Rejected at validation; routes to the existing register-first flow, and **never auto-registers to make an action fit** | — |
| E11 | **Quote currency differs** between `from_symbol` and `to_symbol` | Rejected at validation. Carrying a basis across currencies needs an action-date FX rate; inventing one would corrupt the basis | — |
| E12 | Two actions, same date, same account, **symbol sets intersecting** | **Rejected at validation (D15)** — see the E12 note. The earlier `id` ASC tie-break was insertion order masquerading as economic order, and the two orders produce different money. The combined "reverse split + rename" seen in real data is ONE `EXCHANGE` row, not two, so nothing legitimate is blocked | — |
| E13 | Same symbol held in **two accounts** | **All-or-nothing (D13).** Positions are keyed `(account, symbol)`, so N rows are required — and a partial application is **rejected at validation**, not warned about. See the E13 note: the ⚠ the earlier version promised provably never fires | — |
| E14 | A sell **back-dated before** the action, entered after | Handled by the date-aware guard — *provided* `shares_through` applies corporate actions (§6.2). This is the integration point most likely to be missed | — |
| **E15** | Duplicate identical action entered twice | **REJECTED at validation (D29, revised 2026-08-09 during W2)** — hard, and checked **before E12**, with its own message. No DB constraint; the check is an exact `(account, date, kind, from, to, ratio)` match against the stored ledger. See the E15 note | — |
| E16 | Editing / deleting an action **re-computes history** | Intended (`domain-ledger.md` N2). Captured in `ledger_audit` like every other ledger edit | — |
| E17 | **Stored price basis vs the action** | See §5.1 (rewritten) — canonical as-traded basis, un-adjust at the write seam, `fetched_at`-discriminated correction, carry-forward gap guard | — |
| **E18** | **EXCHANGE / SPINOFF whose `to_symbol` position holds an OPEN SHORT** (review B3) | `UnbookableLedgerError` | skip + flag |
| **E19** | EXCHANGE / SPINOFF **from a position flagged `unbookable_dividend`** (review B4) | Allowed — but the flag **propagates** (see E19 note) | same |
| **E20** | `to_symbol` vs `from_symbol` **coherence per kind** (review N4) | SPLIT **requires** `to == from`; EXCHANGE and SPINOFF **reject** `to == from`. All three rejected at validation | — |
| **E21** | Action referencing a symbol **not registered in `instruments`** (survives E10 via CSV import or a later instrument deletion) | Its symbols must join the dashboard's unregistered skip-set (`dashboard.py`, now `bundle.unregistered_symbols` / `without_unregistered()` after W0), or `quote_ccy()` raises `KeyError` → **500** | skipped with the rest of that symbol's rows |
| **E22** | **EXCHANGE / SPINOFF whose `to_symbol` position is flagged `ever_oversold`** (grill D16) | `UnbookableLedgerError` — the mirror of E18, and of E19 one level deeper: E19 stops a flag being laundered, this stops a **cost basis** being restored onto a position whose basis was deliberately discarded. Scaling an undefined basis is undefined; so is averaging into one | skip + flag |
| **E23** | **EXCHANGE, `ratio_to != ratio_from`, `to_symbol` HAS prior prices, `from_symbol` has NONE** (grill D22) | **`needs_confirm` — the 賣超 tier**, not a hard rejection and not a passive notice. The four-part condition is the identifier signature: a real merger's source was a listed security and has prices. Offers a one-click conversion to SPLIT. See §5.1 "The residual hole" | same |
| **E24** | **A dividend on a symbol an EXCHANGE moved away** (audit 2026-08-10, **D32**) — after the exchange the source stays in the position map with **zeroed** fields (§4.4, required so a later buy cannot reopen it carrying `−ε`), so `existing is not None` and `short_shares == 0`: neither the closed-position refusal nor the dividend-on-short refusal applies, and the payment books | `UnbookableLedgerError` — **both branches, uniformly**. CASH/NET would book post-close realized income on a dead ticker; DRIP/STOCK does `existing.shares += reinvest_shares` and **resurrects** the position at `avg = 0`, which is delisted so it never gets a price, and one unpriced holding makes `returns.py` return `rate=None` for the **whole portfolio** — the XIRR goes blank indefinitely | skip the event, flag the position (待釐清). The owner records the payment as a **cash movement** |

#### E1a — why the dashboard path must SKIP, not raise (review finding N3)

The first version said the strict path raises and "the call site degrades as today". **It does
not.** `portfolio/dashboard.py` calls `build_book` with **no `try`/`except`** — the existing
`ValueError` cases (a dividend for an unknown position) are prevented at entry/edit time, never
degraded at read time. An action row whose `from_symbol` was never held — a typo, or a transaction
deleted *after* the action was entered — would therefore **500 the dashboard**, breaching the
standing never-500-at-every-`build_book`-call-site rule.

Three changes, all required together:

1. **Entry-time validation (new, missing from §6.5's original list):** the `from_symbol` position
   must exist **on the action date**. This is the corporate-action analogue of the date-aware sell
   guard and uses the same §6.2 path.
2. **Dashboard path skips + flags**, exactly like E2/E3/E5, so a stranded row degrades instead of
   crashing.
3. **`api/routers/ledgers.py` edit-revalidation must guard the deletion** of a transaction or
   opening row that would strand an existing action. Deleting the buy that created a position is
   the realistic way to reach this state, and it is a *delete*, not an insert — so an insert-only
   validation would miss it entirely.

#### E18 — the short on the RECEIVING side (review finding B3)

E5 rejects an EXCHANGE/SPINOFF on a short **source**. Nothing checked the **destination**, and
`Q.shares += carried` on a position with `short_shares > 0` breaks the long/short mutual
exclusivity the whole replay is built on (`_Position`'s docstring: "mutually exclusive **by
construction**"). The emitted holding would become `shares = long − short` with
`total = carried_basis − short_proceeds` — a real cost basis blended with short proceeds, with no
honest average, and the `abs(cost_total)` ratio rule and `fully_recovered` gate both mis-fire on
it.

Auto-covering the short out of the carried shares was considered and rejected: it would have to
invent a cover price, and `domain-ledger.md` never guesses a price.

#### E19 — a corporate action must not launder a 待釐清 position (review finding B4)

`unbookable_dividend` is a sticky marker on `_Position` and **survives the short being covered**,
so a currently-long position can still carry it (E5 only blocks a *currently open* short). Under
the original §4.2, EXCHANGE zeroed the source, the holdings loop dropped it at
`cost_basis.py:294` (`shares == 0 → continue`), and the flag **vanished** while the successor
rendered clean — an unresolved money-of-record problem erased by an unrelated event. Textbook
"wrong number that looks right".

**Resolution: propagate, do not block.** The action itself is legitimate; blocking it would
punish a real corporate event for an unrelated older data problem.

```
Q.unbookable_dividend |= P.unbookable_dividend      # EXCHANGE and SPINOFF alike
```

SPINOFF propagates to the **child** as well, because the child's basis is carved out of the
flagged position — anything deriving its basis from an unresolved position inherits that
uncertainty. (`ever_oversold` needs no rule here: E3 rejects the action outright.)

#### E20 — `to_symbol` vs `from_symbol` (review finding N4)

The original spec stated `to == from` for SPLIT only in a **SQL comment**, and never constrained
the other two. Both directions are real defects:

- a **self-EXCHANGE** zeroes the position and re-adds it, silently rescaling shares by `ratio`
  while masquerading as a rename;
- a **self-SPINOFF** carves `c` of the basis out of a position and adds it straight back to the
  same position — **double-counting** the carried portion, since `P` and `Q` are the same object.

So: SPLIT requires equality, EXCHANGE and SPINOFF reject it. All three enforced at validation, not
in the replay.

#### E21 — an action can reach the replay with an unregistered symbol

E10 validates registration **at entry**, but two paths get behind it: CSV import, and deleting an
instrument after the action was written. The dashboard's unregistered skip-set (built on the bundle
since W0 — `bundle.unregistered_symbols`) covers transactions, dividends and openings only; a
corporate-action row referencing a symbol
outside `instruments` would then reach `build_book`, whose `quote_ccy()` raises `KeyError` — a 500,
and a different exception type from every other degradation path. The action ledger must join that
skip-set on **both** its symbols.

#### E15 — the duplicate is HARD, and it must be checked BEFORE E12 (D29, 2026-08-09)

Written during W2, when the implementation's own test failed. The original row said "the entry
form warns", i.e. a soft `needs_confirm` issue. **Two separate defects, and the second is the
familiar one:**

1. **Soft is the wrong tier here.** The stated reasoning — re-entering is plausible — is true for
   a *transaction*: you really can buy the same stock twice in one day at one price, so the
   duplicate guard has to be acknowledgeable. A corporate action is not a transaction, it is an
   **event**, and an event happens once per `(account, symbol, date)`. There is no ledger in which
   two identical 3-for-1 rows on one day are both correct. Acknowledging the warning applies the
   ratio **twice** — a 3-for-1 becomes a 9-for-1 — which is the same silent share-count corruption
   D15 rejects same-date ordering to prevent, arrived at from the other direction.

2. **As specified it could never fire.** An exact duplicate is, by construction, a same-date
   same-account pair whose symbol sets intersect — that is E12's condition, exactly. E12 is a hard
   rejection and would swallow every duplicate before E15 was reached, so the warning this row
   promised was unreachable. **That is the identical defect §5's E13 note was itself rewritten to
   remove** ("the ⚠ the earlier version promised provably never fires"), reappearing one row later
   and surviving two review rounds and a grill. Recorded here rather than quietly fixed, because
   the recurrence is the interesting part: a promised warning is not a check until something
   proves it fires.

E12's message is about **ambiguous ordering**, which does not apply to two identical rows — they
have no order problem, they have a doubling problem — so the duplicate needs its own text, not
E12's. Hence: check first, reject hard, say "this would apply the ratio twice".

#### E12 — same-date ordering is REJECTED, not tie-broken (D15)

The original rule was "deterministic tie-break on `id` ASC, documented". `id` is
`INTEGER PRIMARY KEY AUTOINCREMENT` — the order the owner happened to TYPE the rows. Measured on a
position of 100 shares with `original_total` 5,000, given a same-date `SPLIT X 3-for-1` and
`EXCHANGE X → Y 2-for-1`:

```
typed split first:    X → 300,  EXCHANGE carries 300 × 2/1  →  Y = 600 shares, avg  8.3333
typed exchange first: EXCHANGE carries 100 × 2/1 → Y = 200, X zeroed; the SPLIT then
                      scales an empty X                     →  Y = 200 shares, avg 25.0000
```

Same ledger, same two rows, same day — a 3× difference in the share count and in every market value
derived from it, decided by typing order. And **§7.1a cannot see it**: Σ`original_total` is 5,000 in
both, so the conservation test is green on both (§2.1a).

An explicit `sequence` column was considered and rejected: it puts a field on every row to serve a
case the real ledger sees essentially never, and it asks the owner to reason about an ordering they
have no way to observe. **Validation rejects two actions with the same `(account_id, date)` whose
`{from_symbol, to_symbol}` sets intersect.** Non-intersecting same-date actions are independent and
stay legal. The owner's remedy for a genuine two-step event is the one E12 already prescribes: book
it as ONE row (a reverse split + rename is one `EXCHANGE`), or date the steps apart.

#### E13 — a partial multi-account application is REJECTED (D13)

The original rule warned and let the row through, on the stated basis that "the drawer will show
⚠ 對帳不一致 for that account". **Verified: it does not, and cannot.** The drawer proves
`opening + buy − sell + reinvest + corporate_delta == book_shares` (`symbol.py:246` + §6.3), and
§6.3 defines `corporate_delta := shares_action_aware − shares_naive`. For an account with no action
row that delta is 0 and `build_book` applies nothing, so both sides equal the naive sum and the
footer prints **✓ 對帳一致**.

Meanwhile `prices` has no `account_id`, so §5.1's correction is global. The un-actioned account
therefore holds **pre-action share counts** while reading **post-action prices** — measured on a
3-for-1 with 40 shares: displayed 3,526.80 against a true 10,580.40, understated by 7,053.60, with
every existing check green. The defect lives *between* the share count and the price, and nothing in
the system computes that relationship.

**Rule:** if `from_symbol` has a position in N accounts **on the action date**, the action must be
written for all N. Fewer is rejected at validation; the entry form's account checklist is
therefore **not deselectable** (§6.7). Accounts whose position opens *after* the action date are not
counted in N — they never held the pre-action shares.

#### E22 — the oversold DESTINATION (D16)

See §4.4's note. E3 guards the source, E18 guards the destination against an open short, E19
propagates the 待釐清 flag — and nothing guarded the destination against a discarded basis.
`Q.original_total += P.original_total` on such a position produces a confident, ordinary-looking
average over shares that have no basis at all. Rejected strict; skipped + flagged on the dashboard
path. The owner's route out is to resolve the 賣超 first — which §6.7's door 1 now gives them.

#### E24 — a dividend on a symbol an EXCHANGE moved away (D32, 2026-08-10)

A live defect in W3, found by the spec-conflict audit rather than by a test. §4.2 leaves the source
position **in the map with zeroed fields** — deliberately, so a later buy on the old ticker cannot
reopen it carrying `−ε` (§4.4). A dividend arriving on that ticker afterwards therefore passes both
existing refusals: the position exists, and it holds no short.

Not exotic. US brokers routinely pay a final dividend on or **after** a merger's effective date, and
a Schwab DRIP arrives as a three-row group — so this is on the path of the very import that
commissioned this feature.

**The ruling is refuse-and-flag, uniformly for both branches** (raise strict · skip + flag on the
dashboard path), and the owner records the payment as a **cash movement**. Three reasons:

- It is the posture the replay already takes five times — E3, E5, E18, E22, and the
  dividend-on-an-open-short — so it adds no mechanism. "Record it as a cash movement instead" is
  the exact remedy `domain-ledger.md` already prescribes for the short case.
- Redirecting the payout to the destination symbol would be a money-of-record movement between two
  symbols with no conservation analysis behind it, and §2.1a already lists "cash leaving the book"
  as something the law cannot see. There would be no detector.
- The DRIP branch is the dangerous one and it fails *loudly at the wrong place*: the resurrected
  position is emitted at `avg = 0`, is delisted so it never prices, and a single unpriced holding
  blanks the **portfolio's** XIRR. The symptom appears nowhere near the cause.

**The scope is narrow, and the narrowness is the point.** An *ordinary* sold-out position — closed by
selling, not by an action — keeps today's behaviour: a dividend arriving after the close is booked as
realized income, which is the 2026-07-26 audit's H2 ruling and raises the historical return of a
closed symbol correctly. Only the **action-zeroed** state changes. Distinguishing the two needs one
marker on `_Position` (the zeroing is what makes them look alike), so the repair adds a field — and
§4.4's field-count assertion (F-37) will require the table to gain its row in the same change.

### 5.1 E17 — the split/price interaction (REWRITTEN 2026-08-07 · REVISED 2026-08-08 · SCOPE SETTLED 2026-08-09)

> Two rounds of correction. **2026-08-07 (finding B1):** the original analysis was inverted.
> **2026-08-08 (findings B2, B3):** the replacement remedy's in-place correction was not
> idempotent, and its carry-forward resolution traded a `ratio`-sized error for a 100% one. The
> "three facts" and "the invariant this yields" below survived both rounds unchanged; everything
> from "The remedy" onward is the 2026-08-08 version.

> **The first version of this section was inverted, and the remediation it mandated (D6:
> "one-click re-fetch") would have *manufactured* the artifact it claimed to remove. Recorded
> here rather than deleted, because the wrong version was owner-approved and the reasoning
> matters.** The original claimed stored history is unadjusted while share counts become
> post-split. Measured against the code, the opposite is true of the rows that matter.

#### The three facts (verified in code, not assumed)

| Fact | Evidence |
| --- | --- |
| Yahoo's `Close` is **always split-adjusted**; `auto_adjust` governs *dividend* adjustment only | `pricing/providers/yfinance_provider.py:88` passes `auto_adjust=False` and still receives split-adjusted closes |
| A re-fetch **overwrites** stored rows | `pricing/store.py` `upsert_prices` — `ON CONFLICT(instrument, as_of_date) DO UPDATE SET close=excluded.close, …` |
| The trend pairs each day's price with **that day's** share count | `portfolio/timeseries.py:117-123` rebuilds the book from events `<= day`; `:137` takes `_at_or_before(price_history[symbol], day)` |

#### The invariant this yields

> **A stored price for date *D* must be expressed in the same share terms as the ledger's share
> count on *D*.**

The ledger's share count on a pre-split day is in **pre-split** terms (the action event is dated
later and is not in the filtered list). Therefore the correct stored price for that day is the
**price as actually traded on that day**, not the adjusted one.

That splits the stored rows into two populations with *opposite* correctness:

- **Fetched before the split** (the daily refresh accumulating rows as time passes) → as-traded →
  **already correct**. The original E17 claimed these were broken. They are not.
- **Fetched after the split** (any backfill, or any re-fetch) → split-adjusted → **understates by
  the ratio**, because a post-split adjusted price is paired with a pre-split share count.

The owner's own migration is squarely the second case: import the 2021–2026 ledger, then backfill
history. So the artifact is present from day one — and re-fetching cannot fix it, because
re-fetching is what causes it.

#### The remedy — one stored invariant, two operations, one read rule

> Revised again **2026-08-08** (review findings B2, B3). The three-part remedy was directionally
> right but (c) was **not idempotent** and (d) traded an inflated number for a worse one. Both are
> replaced below.

**(a) Declare the canonical basis.** Stored prices are **as-traded** (unadjusted for splits).
Written once in `data-and-pricing.md` next to the float-noise cap, because it is the same class of
rule: a statement about what a stored price *means*.

**(b) One stored invariant.** `prices` gains one column, migrated by the existing
`_add_column_if_missing` path — **in `pricing/schema.py`, not `data_ingestion/schema.py`** (F-14:
`bootstrap_db` runs *before* `create_pricing_tables`, so on a fresh database the `prices` table does
not exist yet and the migration crashes first boot):

```sql
ALTER TABLE prices ADD COLUMN split_basis TEXT NOT NULL DEFAULT '1';
```

and every row must always satisfy:

```
split_basis  ==  target(row)
stored_close ==  provider_close_as_delivered_at_fetch_time  ×  split_basis

target(row)  :=  Π{ ratio(k) :  k ∈ keys(row.instrument, row.as_of_date, row.fetched_at) }

   keys(sym, after, through)  :=  { key(a) :  a.kind == SPLIT
                                           and a.from_symbol == sym
                                           and after < a.date <= through }
   key(a)                     :=  (a.from_symbol, a.date, Fraction(a.ratio_to, a.ratio_from))
   ratio(k)                   :=  the reduced fraction in k, as a quotient
```

> **The product is over the DEDUP KEY, never over the values (F-42, 2026-08-10).** Written the
> obvious way — `Π{ a.ratio_to / a.ratio_from : … }` — the set-builder collects **quotients**, so two
> genuine 2-for-1 splits on different dates (2020 and 2023) collapse into the single value `{2}` and
> the factor comes out **2 instead of 4**. Deduplication must key on the *event*, which §5.1 detail 3
> already requires for the multi-account case; F-10 corrects that key from the term-wise
> `(symbol, date, ratio_to, ratio_from)` to the **reduced fraction** plus symbol and date, so `3/1`
> and `30/10` are one entry rather than two (term-wise they survive as two and `split_factor`
> returns **9**, not 3). One key definition, used by every product in this document.

`target` is *"the splits the provider had already folded into this row when we fetched it"* — read
straight off `fetched_at` and the **current** action ledger. It is state, not history: nothing has
to be remembered about what a previous run did.

**(c) Two operations, both restoring the invariant.**

| Operation | When | Effect |
| --- | --- | --- |
| **Write** (`upsert_prices`) | every fetch | `close := raw × target`, `split_basis := target`. Recomputed from the raw provider value, so a re-fetch is a full restatement |
| **Reconcile** | any insert / edit / delete of a SPLIT row | `close := raw × target_new`, then `split_basis := target_new`. Recomputed from the stored raw value, exactly like the write; needs no network and works for a delisted symbol |

> **AMENDED by D30 (owner ruling 2026-08-10) — the price basis is TWO stored columns.** The
> single-column design above kept only the *re-expressed* close and reconstructed by dividing the
> old basis back out (`close × target_new / split_basis_old`). Measured, that is not
> order-independent **at the stored value**: `_cap_dp(v, 4)` re-applies on every pass and the two
> orders traverse different intermediates — for `(1/3)` then `(7/2)` on 0.0013 the two orders store
> 0.0014 and 0.0015, a 7.7% difference, and **neither equals the one-shot value**. §5.1 claims
> order-independence below and §7.1b tests it, so a correct implementation of the single-column
> design turns that test red. Reconstruction also cannot recover an as-traded price from an
> already-rounded feed (a US 7-for-1 reconstructs 100.07 as 100.10).
>
> **Therefore: keep the raw provider close as its own column, and define BOTH operations as
> `close := raw × target`.** The reconcile stops being a rescale-in-place and becomes the same
> restatement the write already is. Consequences, all in the direction of fewer mechanisms:
> idempotency, reversibility and order-independence hold **by construction** rather than by rounding
> luck; detail 1's short-circuit demotes from a **correctness requirement** to an optimisation; and
> `prices` comes under the discipline the ledgers already obey — nothing authoritative is
> overwritten, every derived figure is recomputed on read (CLAUDE.md #7), which is also what
> `data-and-pricing.md`'s "store at full source precision" requires once a close is a derived value.
> Cost: one TEXT column on a table sized for 1–2 users. W6a writes the DDL and settles whether
> `split_basis` is still worth storing as a materialised marker or is recomputed from the ledger —
> it is no longer load-bearing either way, because the raw value is what the close is rebuilt from.

Why this is the correct shape, checked case by case:

- **Idempotent.** Run reconcile twice: the second pass recomputes the same product from the same
  stored raw value, so it writes the identical close (under D30 this is byte-identical by
  construction, not by a short-circuit). The old (c) had no such state — it keyed off
  `fetched_at >= action.date`, which stays true forever, so **editing a ratio or
  deleting-and-re-entering an action multiplied the price a second time.** E16 explicitly makes an
  edit re-compute history, so that bug was not hypothetical: it was scheduled.
- **Reversible.** Delete the split → `target_new` loses that factor → the close is rebuilt from the
  raw value without it, landing exactly on the pre-action figure.
- **Order-independent — at the stored value, and only under D30.** Two splits entered in either
  order converge on the same `target`, because `target` is a product over a *set* of dedup keys, not
  a sequence of applications. Under the single column that convergence stopped **at `target`**: the
  stored close did not follow it, because every pass re-capped a different intermediate (measured —
  see the D30 amendment above). Rebuilding `close := raw × target` caps **once**, on one product, so
  the stored value converges too. This bullet is what §7.1b's either-order clause tests, and it was
  false as written.
- **No-op without splits.** The empty product is 1 and the default is `'1'`, so every existing row
  and every symbol with no corporate action stores byte-identically. This is a precondition, not a
  hope: it is what lets the change ship without moving any existing number.
- **No-op for a live quote.** Today's row has no split dated after it, so the daily refresh path is
  untouched.
- **It makes re-fetch SAFE.** D6's instinct was not worthless, it was *premature*: once (b)+(c)
  exist, a re-fetch is a restatement that lands on the same as-traded value. Before them,
  re-fetch is destructive.
- **Legacy rows need no backfill.** At migration the action ledger is empty, so `target` is 1 for
  every row — which is exactly what `DEFAULT '1'` asserts. The first SPLIT the owner enters is
  what moves them, through the same reconcile path as everything else.

**Three details the second verification pass surfaced. All three are normative.**

1. **Reconcile SHORT-CIRCUITS on `target == split_basis`** — **demoted to an optimisation by D30
   (2026-08-10); it was a correctness requirement only for the single-column design.** The original
   reasoning: `close × target / split_basis` is not guaranteed to return `close` exactly when
   `target == split_basis`, because both are products of `to/from` quotients, inexact for any
   non-terminating ratio, and `x × t / t` rounds twice — so idempotency held by luck, not by
   construction, and §7.1b tests exactly that. Under D30 the reconcile recomputes `close := raw ×
   target` instead of rescaling, so re-running it is byte-identical whether or not it compares
   first. Keep the comparison to avoid pointless writes; do **not** rely on it for idempotency, and
   do not treat its absence as a defect. Either way the write re-applies `_cap_dp(v, 4)`
   (`pricing/store.py:32`) so the 4-dp storage cap is preserved — **applied last, to the product**
   (F-20: capping the raw value first and multiplying after gives 2.8340 where the correct order
   gives 2.8333).

2. **`split_factor` is for PRICES ONLY. Share counts must never touch it.** It returns a *quotient*
   (`Π to/from`), so for a ratio like 2-for-7 it is a rounded Decimal — acceptable for a price
   already float-derived and capped at 4 dp, **fatal for a share count** (§3.1(ii) is the entire
   reason). Share counts go through `apply_ratio(qty, action)` one action at a time, which never
   forms the quotient. This split of responsibility is the single most important thing for an
   implementer to get right, and it is asserted by a test: `apply_ratio` must be the only caller
   path that touches `_Position.shares`.

3. **A split is a MARKET fact; the ledger row is per-ACCOUNT.** `prices` has no `account_id`, so
   the price-side factor cannot be per-account. Resolution:
   - `split_factor` **deduplicates** on `(symbol, date, ratio_to, ratio_from)`, so a symbol held in
     three accounts with three identical rows yields one factor, not a cubed one. Getting this
     wrong turns a 3-for-1 into a 27-for-1 — silently, and only for multi-account holders.
   - Validation **rejects** two rows with the same `(symbol, date)` but different ratios: a stock
     cannot split two ways on one day, so that is a data error, not a per-account nuance.
   - Validation **REJECTS** an application to fewer than all N accounts holding the position on the
     action date (D13, E13). This bullet previously said "warns hard … and the drawer will show
     ⚠ 對帳不一致 for that account" — **that claim was false** (E13's note proves the footer reads
     ✓ instead). The incomplete state is a globally-corrected price against a locally-uncorrected
     share count, which no check in the system computes, so it must not be reachable at all.

**OHLC and volume.** `upsert_prices` writes `close, open, high, low, volume`
(`pricing/store.py`). **Only `close` is re-expressed** — `open` / `high` / `low` keep the
provider's basis, exactly like `volume`.

> **AMENDED 2026-08-10 (owner sign-off), reversing this paragraph's earlier "all four prices take
> the same factor".** Under D30 the row stores **one** raw column, `close_raw`, and every
> re-expression is `close := close_raw × target`. Multiplying `open`/`high`/`low` would therefore
> produce derived values with no preserved source — **permanently stale the moment a ratio is
> edited or deleted, and unrecoverable by any reconcile.** That is F-23's defect made unfixable,
> and it would put those three columns outside the reversibility invariant D38 exists to
> guarantee. The alternative considered and rejected was three more `*_raw` columns: `open`,
> `high` and `low` have **zero readers** — every `SELECT` against `prices` in the codebase names
> its columns explicitly and none of them names these — so that is storage for a consumer that
> does not exist. `split_basis` sits on the same row, so a future OHLC reader can recover the
> basis; the schema carries a comment saying so, because a row whose `close` is post-split and
> whose `open`/`high`/`low` are pre-split is a landmine for whoever first draws a candlestick.

**`volume` is left untouched in this version**, with the limitation stated in the manual: whether
the provider adjusts volume — and in which direction — has not been measured here, and volume is
not money-of-record, so guessing at it would violate the same rule this section exists to enforce.
A probe belongs with P1a, not here.

**(d) The read rule — carry-forward is re-expressed, not discarded.**

On the split date the book applies the ratio immediately, but `_at_or_before`
(`timeseries.py:30-35`) may still return the last **pre-split** price until the next refresh
writes one. The day is then valued at `post-split shares × pre-split price` — inflated by the
ratio, on the trend **and** on the live dashboard (`dashboard.py`'s `price_map` → `value_holdings`).

The previous resolution — mark the day `incomplete` and let the holding contribute nothing — was
measured against the code and is **worse than the defect**. `timeseries.py:126-151` does not omit
the *day*; it omits the *holding* and still emits `total_value`:

```python
if price is None:
    incomplete = True
    continue                 # this holding contributes 0
...
points.append(TrendPoint(date=day, total_value=total, ...))
```

So the net-worth chart would drop by the position's entire value on every split date. For a
pre-split TSLA that is a five-figure cliff. Replacing an error of *ratio* with an error of *100%*
is not an improvement, and it breaks §2.1's continuity clause outright.

**The rule instead:** a carried-forward price is re-expressed into the valuation day's share terms.

```
price_in(sym, d)  =  as_traded(pd)  ÷  Π{ ratio(k) : k ∈ keys(sym, pd, d) }
```

with `keys` / `key` / `ratio` exactly as defined in (b) — **the product is over the dedup key, never
over the quotient values** (F-42): two genuine 2-for-1 splits on different dates are two keys and
give 4, while a set of values gives 2. Here `pd` is the date of the price `_at_or_before` actually
returned. Continuity is then true by
construction, which is the whole point:

```
day pd  (pre-split):   S     × p
day d   (post-split): (S×r)  × (p ÷ r)   =   S × p      ✓ §2.1
```

**This is not guessing a price.** `domain-ledger.md`'s "live price unobtainable → label clearly,
never guess" governs *market* prices — inventing a level nobody traded at. Re-expressing a known
price into post-split units is arithmetic on the unit of account, exactly like `average =
total / shares` correcting itself when the share count changes. Nothing is invented; the same
trade is quoted in the new denomination.

Properties: the product self-cancels the moment a genuine post-split price exists (`pd >= a.date`
→ empty product → 1), so the correction disappears without anyone switching it off. It is a
**read-path** transform — never written back to `prices` — so 重算 stays authoritative and the
stored basis stays as-traded per (a).

**Where it is applied — one seam, verified against the call graph.** Not inside `value_holdings`.
`portfolio/dashboard.py` builds **`price_map`** once, from `price_reads`, and feeds that one dict to
**both** consumers:

```python
price_map = {sym: pr.value for sym, pr in price_reads.items() if pr is not None}
valued = value_holdings(book.holdings, price_map)
outcome = xirr_reporting(txs, divs, opening, valued, instruments, fx_at,
                         price_map, resolver.rate, as_of, reporting)
```

> **Anchor on the construct, not the line number (F-44, 2026-08-10).** This paragraph pinned the
> seam to `dashboard.py:264` on 2026-08-08. W0's refactor moved it: `price_map` is `:236`,
> `value_holdings` `:237` and `xirr_reporting` `:296` today, and an implementer following the old
> numbers would edit the wrong statements. Line numbers in this document are **evidence of a
> reading**, never an instruction; the instruction is *the assignment to `price_map`, immediately
> before `value_holdings` consumes it*. Numbers below are dated where they matter.

Re-expressing inside `value_holdings` would leave `xirr_reporting`'s terminal value on the raw
price, so the displayed market value and the XIRR would silently disagree by the split ratio —
a discrepancy between two numbers on the same screen, which is the worst kind. Applying it **at the
`price_map` assignment**, where `pd = PriceRead.as_of` and `d = as_of`, gives every downstream
consumer (valuation, XIRR, allocation weights, KPIs, net worth) the same corrected price from one
place. That reasoning is trap #3 and it is correct — only its anchor was stale.

The trend path cannot precompute the same way — its factor depends on the valuation day `d`, which
varies per point — so `timeseries.py` applies it at lookup time. Two call sites, **one**
`split_factor`, different windows (§6.0's table).

**Scope: SPLIT only — D18's finding stands, its remedy is D19 (D22, 2026-08-09).**

```
kind == SPLIT          # to_symbol == from_symbol by E20; applied to that symbol's series
```

A SPINOFF is excluded (the parent's share count is untouched; the child's series begins at the
action). **An EXCHANGE is excluded too, and getting there took two rounds:**

| Case | Does the destination's series need a factor? |
| --- | --- |
| Identifier change + reverse split (**same security**, ticker unchanged) | **Yes** — the case that motivated D18 |
| Genuine merger A → B, and you **already held B** | **No.** B's share count never changed. A factor would **corrupt B's entire price history** |
| Genuine merger A → B, you did not hold B | **No.** Your position in B begins at the action |

The row cannot distinguish them — `from_symbol != to_symbol` in all three. But **D19 converts the
first case into a SPLIT** (identifier normalised → `to == from` → E20 forces SPLIT), which this
SPLIT-only scope already handles. A widened EXCHANGE clause would therefore cover *only* the two
cases where it is wrong. The reason is one line of §4.2: `Q.shares += carried` — an EXCHANGE **adds
to** its destination, it does not re-denominate what was already there.

> **D18's finding is not withdrawn.** The old justification — *"the destination of a rename is a
> newly registered ticker, so in practice it has no prior history"* — is false, and the cliff is
> real: measured on a 1-for-20 over 200 shares at 0.4250, net worth falls from 85.00 to 4.25 on the
> action date (95%). What changed is **which decision repairs it**: D19, at the import seam, not a
> widened price predicate. The demo report's §Q6 still reproduces the cliff; read its mode 1 —
> "the same event recorded as a SPLIT" — as the fix, which is exactly what D19 produces.

**The residual hole, and the guard for it (E23 — SETTLED 2026-08-09).** If a raw identifier was
registered as an instrument before D19 existed, E10 passes and case 1 can still be entered as an
EXCHANGE, silently leaving the destination's series in the wrong terms.

**"Reject or warn" was a false choice.** `validate.py` already has three tiers, not two
(`Issue.needs_confirm`; see its own docstring at `validate.py:79-81`):

| Tier | Behaviour | Already used by |
| --- | --- | --- |
| no issue | commits silently | — |
| `needs_confirm=True` | **blocks until explicitly acknowledged**, then commits | the 賣超 guard, duplicate rows, future dates |
| `needs_confirm=False` (hard) | cannot be committed at all | market/account incoherence, negative fees |

E23 is the **middle** tier — the same one the 賣超 guard uses, and the closest existing analogue in
meaning: *probably an error, the system cannot be sure, and the owner may legitimately overrule.* A
passive notice would let the artifact through; the hard tier would block ordinary mergers.

**The condition is narrowed so it fires on the error, not on every merger.** "`to_symbol` has prior
prices" is true of cases 2 and 3 as well — i.e. of most mergers — and a guard that mostly cries wolf
trains the owner to click through, so it fails exactly when it matters. The discriminator is the
**source**:

```
kind == EXCHANGE  and  ratio_to != ratio_from
  and  to_symbol   has stored prices dated before the action
  and  from_symbol has NO stored prices at all          ← the identifier signature
```

A real merger's `from_symbol` was a listed security and has a price series. An identifier never
does, because no provider resolves one — which is D19's own central argument (§3.4). So the guard
fires on case 1 and stays silent on cases 2 and 3.

It offers the repair rather than only complaining, mirroring §6.7's door 1:

```
⚠ 這可能是同一檔證券的識別碼變更,而不是併購。
   SYM-B 在此日期前已有價格資料,而來源代號完全沒有價格紀錄 — 這是識別碼、而非證券的特徵。

   ▸ 這是識別碼變更 → 改記為分割(SPLIT)      ← 一鍵轉換,預設
   ▸ 這確實是併購,繼續存檔
   ▸ 取消
```

Residual false positive: an obscure merger source whose prices were never fetched. It costs one
acknowledgement — which is precisely why the middle tier exists. Recorded as **D22**.

#### Consequences for the plan

- This is no longer "a hint on a form". It is a change to `pricing/store.py`, a one-shot
  correction routine, and a guard in `timeseries.py` — and it **moves displayed market value**,
  so it needs its own tests and a manual entry.
- **The factor reaches `pricing/` by INJECTION (D17). Specified, not left to the implementer.**
  The earlier text said "passed **into** `upsert_prices` by its caller (or read through a
  `shared/`-level accessor)". That parenthesis was the document's only unresolved architecture
  question, and the three candidate answers are not equivalent:

  | Option | New dependency edge | Verdict |
  | --- | --- | --- |
  | A — `pricing/refresh.py` imports `data_ingestion.store` | `pricing → data_ingestion` | **Rejected.** `architecture.md` has no such edge |
  | B — move the SELECT into a `shared/`-level accessor | none | **Rejected.** Breaks §6.0 Owner 2 ("no module writes its own SELECT") and puts ledger SQL in the layer that holds no I/O |
  | C — inject a callable | **none** | **ADOPTED** |

  ```python
  # pricing/store.py — pricing/ never learns that corporate actions exist
  def upsert_prices(conn, rows, *, fetched_at,
                    factor_of: Callable[[str, date], Decimal] = _no_factor) -> None: ...

  # pricing/refresh.py — pass-through only, still importing nothing but pricing.*
  def refresh_quotes(conn, registry, instruments, fx_pairs, *, now, factor_of=_no_factor): ...
  def refresh_history(conn, registry, instruments, start, *, now, factor_of=_no_factor): ...
  ```

  `_no_factor` returns `Decimal(1)`, so **every existing caller and test is unchanged** and the
  no-corporate-actions case stores byte-identically. The injection is made by `refresh`'s callers —
  the scheduler and the API routers — which sit above `data_ingestion` and may already import it.

- **Three files must change that the plan never listed.** `upsert_prices` has exactly TWO production
  callers, both in `pricing/refresh.py` (`:36`, `:63`), and that file appears in **none** of §6.1's
  eight call sites, §6.5's file list, or the price package's file column (which named only
  `pricing/store.py`). An implementing agent following the document literally would never open it,
  and the feature would ship storing split-adjusted prices with an unchanged basis. **W6a's** file
  list in §10.2 is corrected to add `pricing/refresh.py`, `scheduler/jobs.py` and
  `api/instrument_service.py` — and, since 2026-08-10, to put the migration in `pricing/schema.py`
  rather than `data_ingestion/schema.py` (F-14).
  - Note the honest cost of option C: `scheduler/jobs.py` currently imports no `data_ingestion` at
    all, so it gains a file-level import. That is a legal downward edge the `api`/`scheduler` layer
    already has elsewhere, and it is cheaper than either rejected option — but it is not free, and
    the implementer should not be surprised by it.
- **D6 is superseded** — see §8.

---

## 6. Integration points — the complete list

The earlier estimate of "8 call sites" was **too low**. Ten distinct places must change or be
proven unchanged.

### 6.0 Module design — ONE owner per concept (owner directive 2026-08-08)

> *「需要規格進行模組化集中,避免同樣功能分散多個地方,避免這種維護困難的開發模式。」*

The count above is itself the warning sign. Read plainly, the integration list says: the ratio
algebra would be re-typed in `cost_basis.py`, `holdings.py`, `pricing/store.py` and
`timeseries.py`; the ledger would be loaded ad hoc at eight call sites; and the table name would be
hand-added to four enumerations. Four copies of one formula is four places to drift, and this
spec's own §6.3 already documents what drift costs (the drawer footer contradicting the replay).

**So the feature is built as three owners, and everything else is a caller.**

#### Owner 1 — `shared/corporate_actions.py` (the algebra; pure, no I/O)

`shared/` depends on nothing internal and everything may import it (`architecture.md`), which makes
it the only home that all four consumers — `portfolio/`, `data_ingestion/`, `pricing/`, `api/` —
can reach without a lateral dependency. This is the single reason the design works at all: without
it, `pricing/` un-adjusting a price would have to import from `portfolio/`, which is an
architecture violation.

```python
class CorporateAction(BaseModel):        # the ledger row, ratio kept as two terms
    account_id: str; date: date; kind: CorporateActionKind
    from_symbol: str; to_symbol: str
    ratio_to: Decimal; ratio_from: Decimal
    cost_carry: Decimal | None; note: str | None

def apply_ratio(qty: Decimal, a: CorporateAction) -> Decimal:
    """qty × to / from — multiply first, divide last (§3.1(ii)). The ONE place this order lives."""

def split_factor(idx: ActionIndex, symbol: str, *, after: date, through: date) -> Decimal:
    """Cumulative SPLIT ratio on *symbol*'s price series over (after, through].

    SPLIT ONLY (D22, §5.1 "Scope"): an EXCHANGE adds to its destination rather than
    re-denominating it, so including it would corrupt the price history of any symbol
    you already held. Deduplicated on (symbol, date, ratio_to, ratio_from) because the
    row is per-account while `prices` is not. Empty window -> Decimal(1).

    PRICES ONLY. It returns a quotient, so it is rounded for any non-terminating ratio —
    acceptable for a float-derived price capped at 4 dp, FATAL for a share count. Share
    counts go through apply_ratio, one action at a time (§5.1 detail 2).
    """

class ActionIndex:
    """Actions pre-grouped by (account, symbol) and by symbol. Built once per request."""
```

There is **no `ratio` property** returning a quotient. Exposing one would put a rounded Decimal
back in reach of every caller, and §3.1(ii) is the whole reason this revision exists. The only way
to apply a ratio is `apply_ratio`.

`split_factor` is deliberately one function with a date window, because the three consumers differ
*only* in the window they pass:

| Consumer | `after` | `through` | Purpose |
| --- | --- | --- | --- |
| `pricing/store` write + reconcile | `row.as_of_date` | `row.fetched_at` | `target(row)`, §5.1(b) — reached by **injection**, never import (D17) |
| `dashboard.py`'s `price_map` assignment | `pd` = `PriceRead.as_of` | `d` = `as_of` | re-expression, §5.1(d) — one seam feeding valuation AND XIRR (the construct, not a line number — F-44) |
| `timeseries` read | `pd` (carried price date) | `d` (valuation day) | re-expression per point; the factor depends on `d`, so it cannot be precomputed |
| entry-form preview | `−∞` | `action.date` | show pre-action terms |

Writing them as three separate helpers is how the same product ends up implemented three ways.

#### Owner 2 — `data_ingestion/store.py` (persistence; the only SQL)

`insert_ / list_ / get_ / update_ / delete_corporate_action`, `ledger_audit` on update+delete —
the same shape as the other four ledgers, no new pattern. **Every** consumer loads through
`list_corporate_actions`; no module writes its own SELECT.

#### Owner 3 — `shared/ledger_registry.py` (the table catalogue)

Replaces §6.4's four hand-maintained enumerations with one declaration per ledger table
(name · zh label · account column · date column · export order). `db_stats.py`, `export/ledgers.py`,
`moomoo_merge.py` and `scripts/merge_reconcile.py` all read the registry. Adding a 6th ledger then
touches **one** list, and — more to the point — *forgetting* to touch it becomes impossible rather
than silent. Today a missed entry means an account merge orphans rows and the reconciliation
still reports PASS (§6.4).

#### The signature problem — `LedgerBundle`

Threading a 5th ledger through eight `build_book` call sites is the same disease. The parameter
list is already `(transactions, dividends, opening, instruments)` at every one of them, and this
spec adds a fifth — so the next ledger adds a sixth, at eight sites again.

```python
@dataclass(frozen=True)
class LedgerBundle:
    transactions: list[Transaction]
    dividends: list[Dividend]
    opening: list[OpeningInventory]
    actions: list[CorporateAction]
    instruments: dict[str, Instrument]

    def through(self, day: date) -> "LedgerBundle": ...   # the timeseries per-day filter, once
```

`build_book(bundle, *, allow_oversell=...)`, loaded once by
`data_ingestion/store.load_ledger_bundle(conn)`. Consequences that matter here:

- The 8 call sites become **8 identical two-line changes**, and the 9th ledger is a one-line change.
- `through(day)` centralises the per-day filtering `timeseries.py:118-120` currently open-codes —
  which is exactly where a new ledger gets forgotten, because forgetting it there is silent.
- The unregistered-symbol skip-set (E21) is computed **once**, on the bundle, instead of being
  rebuilt from three lists in `dashboard.py:247-254` and missing the fourth.

This is a mechanical refactor with no behaviour change, so it is covered by the existing suite
plus the golden payload: **if any number moves, the refactor is wrong.** It lands as its own commit
*before* the feature, so the feature diff is readable.

### 6.1 The eight `build_book` call sites — now one signature change each

| # | Site | Path |
| --- | --- | --- |
| 1 | `portfolio/cost_basis.py` | the definition itself |
| 2 | `portfolio/dashboard.py` | main read path |
| 3 | `portfolio/timeseries.py` | daily trend replay (also §5.1) |
| 4 | `strategy/whatif.py` | what-if |
| 5 | `export/tax.py` | tax package (strict) |
| 6 | `api/routers/actions.py` | 重算 / rebuild (strict) |
| 7 | `api/routers/input_center.py` | import preview + oversell check |
| 8 | `api/routers/ledgers.py` | ledger edit revalidation |

### 6.2 ⚠ `data_ingestion/holdings.py` — the SECOND, parallel share-count path

`_shares_until` / `current_shares` / `shares_through` / `shares_on` compute a share count
**independently of `build_book`**, from transactions + dividends + opening inventory. They feed
the date-aware oversell guard in `validate.py` and the symbol picker.

If corporate actions are added to the replay but not here, the two disagree: the replay says the
position holds the post-split count while the validator still sees the pre-split count, and a
**legitimate sell is blocked** as an oversell. This is the single most likely thing to be missed,
and it fails in the direction that looks like a bug in the user's data rather than in the code.

**Required test (the structural guard, not a sample):** for every `(account, symbol)` in a fixture
ledger containing corporate actions, `shares_through(as_of=max_date)` must equal the signed share
count `build_book` reports. Assert over the whole fixture, not one symbol.

#### The walker — how the action-aware path is built (D23, 2026-08-09)

`_shares_until` (`holdings.py:23-50`) is today **one flat SQL sum** with no ordering and no per-day
iteration. That shape cannot carry corporate actions, and the reason is worth stating so nobody
tries to bolt a `WHERE` clause onto it: a SPLIT's contribution is `shares_at_action_date ×
(ratio − 1)`, which needs an **ordered** replay; and an EXCHANGE's contribution to the destination
comes from **another symbol's entire history**, which is **transitive** (a de-SPAC into a ticker
that is later renamed). So it becomes a small date-ordered mini-replay that resolves predecessors
recursively. Three rules:

1. **Termination is by construction, not by a cycle detector — but ONLY under a date bound that is
   normative, not incidental.** Evaluating an action dated `D` requires the source's position
   **before the actions of `D`**, never "through `D`":

   ```
   contribution_of(a)  needs  shares_before_actions_on(a.from_symbol, a.date)
                       which considers only actions dated  < a.date
   ```

   Written the obvious way — `shares_through(source, a.date)`, inclusive — the evaluation of `a`
   re-enters itself and **recurses forever**. It is a one-character difference between a correct
   walk and a hang, so it is stated here as a rule rather than left as a consequence. With the
   strict bound, the recursion descends monotonically in date and terminates.

   That bound is *sufficient* because **D15 rejects two same-date actions whose symbol sets
   intersect**: `A → B` and `B → C` on one day cannot both exist, so no same-date chain needs
   resolving. Non-intersecting same-date actions are independent. And a cycle in the *symbol graph*
   (`A → B` in 2023, `B → A` in 2025) is not a cycle in the *walk* — it is two ordinary sequential
   events, which the owner is entitled to have.

   - **Therefore no validation-time cycle rejection is added.** R2-Q2 approved one; it is omitted
     deliberately, because it would block a legitimate re-use of a ticker while defending a state
     D15 has already made unreachable. Deviation recorded as **D23** in §8.
   - A **depth cap** is implemented as insurance against the two ways the argument above can stop
     holding: a corrupted or hand-edited DB, and a future change that relaxes D15. Its whole
     purpose is to convert a hang into a diagnosable failure, so **it must never loop and never
     return a partial number.**
   - **Its failure mode follows the never-500 rule.** `corporate_delta` (§6.3) reaches an API route
     via the symbol drawer, so a bare `raise` there is a 500. Strict paths (重算 / tax export)
     raise `UnbookableLedgerError`; read paths **skip the symbol and flag it**, exactly like
     E1a/E2/E3. A guard that crashes the dashboard is not a guard.
   - **What the walker RETURNS when it caps — D31 (owner ruling 2026-08-10).** "Skip the symbol and
     flag it" needs a channel for the flag, and all three wrappers return a bare `Decimal`. **It does
     not become `Decimal | None`.** Nine call sites consume these wrappers, six of them read paths,
     and several (`instruments.py` `_held`, `strategy.py`) only want a boolean — a nullable return
     would force each one to invent its own policy and would be a **sixth** way of saying "this
     number is not trustworthy" in a codebase that already has exactly one vocabulary for it
     (`price_stale` · `oversold` · `short_open` · `unbookable_dividend` · `unbookable_action`) and one
     for "validation must not guess" (`validate.py`'s three tiers). So: **read paths keep the
     `Decimal` signature** and the capped symbol is recorded in a per-request set, surfaced through
     the same 待釐清 chip path `unbookable_action` uses; **the validation path reads that set and
     raises a `needs_confirm` issue** — the 賣超 tier, blocking until the owner acknowledges. No
     signature changes, no new concept.
2. **One `ActionIndex` per batch, not per row.** `validate.py`'s oversell guard calls this path
   **once per transaction**, so a 1,375-row import would otherwise re-read and re-group the action
   ledger 1,375 times. The index is built once per validation batch / per request and threaded in.
3. **`shares_naive` stays exactly as it is.** §6.3's `corporate_delta` is defined as
   `shares_action_aware − shares_naive`, so the old function is not replaced — it becomes the
   second term of the drawer's reconciliation. Renaming or "improving" it silently changes the
   drawer's footer.

### 6.3 ⚠ `api/routers/symbol.py::_reconcile` — the reconciliation identity breaks

The symbol drawer proves:

```
opening + buy − sell + reinvest  ==  book_shares          # symbol.py:246
```

A corporate action adds shares outside that identity, so **every affected symbol would report
⚠ 對帳不一致** — the exact defect class fixed in the current `[Unreleased]` block, arriving from a
new direction. The identity gains a term:

```
opening + buy − sell + reinvest + corporate_delta  ==  book_shares
```

#### Why the obvious definition of `corporate_delta` does not work (review finding B2)

The first version of this spec defined it arithmetically — "`+carried` on the to-side,
`−pre-action shares` on the from-side, `×(ratio−1)` for a SPLIT". **That is not computable where
it is needed.** Verified in code:

- `_reconcile` (`symbol.py:222-260`) is a sum over raw ledger buckets. It performs **no replay**,
  yet a SPLIT's delta is `shares_at_action_date × (ratio − 1)` — which requires replaying
  openings, buys, sells, DRIPs **and earlier actions** up to that date.
- The EXCHANGE/SPINOFF **to-side** delta comes from the *from_symbol's* position, and
  `symbol_detail` never loads it: `symbol.py:309`, `:315` and `:317` all filter on
  `symbol == <this symbol>`. The data is simply absent.
- Chains exist in the real ledger (a de-SPAC into a symbol that is later renamed), so the
  from-side is itself the to-side of an earlier action — the lookup is **transitive**.

And the tempting shortcut — deriving the delta from `build_book`'s own output — makes the identity
circular. The footer would then prove only that a number equals itself, and the §7.3
detection-power test would be testing the test. That is worse than not having the check.

#### The definition that does work

`corporate_delta` is sourced from the **action-aware share path of §6.2** — the second,
independent implementation — as the difference between it and today's naive sum:

```
corporate_delta(symbol[, account])  :=  shares_action_aware(...)  −  shares_naive(...)
```

where `shares_naive` is exactly today's `_shares_until` (openings + buys − sells + reinvests) and
`shares_action_aware` is the §6.2 mini-replay. Both live in `data_ingestion/holdings.py`; both are
SQL-path; **neither is `build_book`.**

This is right for four reasons:

1. **Exact by construction.** The delta is *defined* as the action-attributable component, so no
   per-kind arithmetic has to be re-derived — and therefore cannot drift from the replay's.
2. **The identity stays a genuine cross-check** of two independent implementations (SQL path vs
   `build_book`), which is the whole point of the footer.
3. **The transitive closure is inherited free.** §6.2's mini-replay must walk it anyway (N1); the
   drawer just calls the function and never loads another symbol's ledger.
4. It works per-account and aggregated, which the drawer needs for both its footer and its
   per-account breakdown.

#### Flagged positions SHOULD fail to reconcile

`build_book` deliberately *skips* an action on a 賣超 / 待釐清 / short-conflicted position
(E2/E3/E5), while the SQL path cannot know basis or short state. The reviewer read this as a
parity problem to be scoped around. It is better treated as **information**:

> A position whose basis was discarded genuinely does not reconcile. Reporting ⚠ 對帳不一致 on it
> is the correct answer, not a false alarm.

So the SQL path applies every action unconditionally, the drawer shows the mismatch **together
with the existing `oversold` / `unbookable_dividend` flag** so the cause is visible, and the
parity test (§7.2) asserts equality for **unflagged** positions and merely permits divergence on
flagged ones. No special-casing inside the calculation.

> **One exception, and only one — D33 (owner ruling 2026-08-10).** "Unconditionally" has a failure
> case: applied to a source whose count is **negative** on the action date, the SQL path manufactures
> a destination `build_book` never created — a symbol with no transaction, no opening and no holding,
> and therefore **no flag of any kind** — so the drawer renders `＋公司行動 −100` under a red
> 對帳不一致 with nothing to explain it. The paragraph above only works when the mismatch comes with
> its cause attached. **The SQL path therefore skips an action whose source count is `< 0` on the
> action date, and flags the symbol.**
>
> **This does not weaken the independence argument, which is what the unconditional rule protects.**
> E3's precondition is a **share-domain** condition the share-only path computes from its own data —
> one comparison, no import. That is exactly what E5 (short state) and E18 (destination short) are
> not: those would require importing the replay's model into the SQL path and would collapse the two
> implementations into one, ending the cross-check. Skipping on `< 0` imports nothing.

#### Presentation

The footer gains a `＋公司行動` term, shown only when non-zero (same rule as `＋配股/DRIP`), and
`_SHARE_EPS` applies unchanged. The drawer's activity list gains the corporate-action rows so the
term is traceable to the events that produced it.

### 6.4 Table enumerations that will silently omit the new table

| Site | What breaks if missed |
| --- | --- |
| `api/routers/db_stats.py:39` `_TableSpec` list | Records/retention page under-reports; the row count is invisible |
| `export/ledgers.py:20` `_LEDGER_TABLES` | The ledger CSV export omits corporate actions — a "full export" that is not full |
| `data_ingestion/moomoo_merge.py:47,58` | An account merge **orphans** corporate-action rows on the dead account id |
| `scripts/merge_reconcile.py:140` | The merge reconciliation reports PASS while rows were left behind |

**All four are replaced by `shared/ledger_registry.py` (§6.0, Owner 3).** They are listed here as
the *evidence* for the registry, not as four edits to make: a table that must be remembered in four
places will eventually be forgotten in one, and every failure mode in the right-hand column above
is silent. The registry turns "remember to add it" into "it is declared once".

### 6.5 Ledger CRUD, entry surfaces, import

- `data_ingestion/store.py` — `insert_corporate_action` / `list_` / `get_` / `update_` /
  `delete_`, with `ledger_audit` capture on update+delete (same shape as the other four ledgers).
- `data_ingestion/validate.py` — the validation rules of §5. **Hard rejections, in the order they
  are evaluated.** The list below is an **evaluation order, and that order is normative** (D29): a
  later check may be unreachable behind an earlier one, which is how E15 was specified as a warning
  nothing could ever reach. Do not reorder it for tidiness.
  - **E6 / E6a** — `ratio_to` and `ratio_from` are both **positive integers** (D14). `> 0` alone is
    not enough: it admits a rounded quotient through the CSV importer and the API. `ratio_from == 0`
    would also be a division by zero *inside* the replay, i.e. a 500 on the dashboard path.
  - **E8** — `cost_carry` present and in `[0,1]` on SPINOFF.
  - **E10** — **either** symbol unregistered is a rejection (F-35), and **never auto-register to
    make an action fit**. This is also how a raw broker identifier is refused (D19/D24, §3.4): it is
    unregistered because it is not a security, so the existing rule already covers it — which only
    works because the rule covers `from_symbol`. **No string-shape test** — the message branches,
    the rejection does not.
  - **E11** — same quote currency on both symbols.
  - **E15** — **duplicate action: hard, and evaluated BEFORE E12 (D29).** An exact
    `(account, date, kind, from, to, ratio)` match against the stored ledger and against the rest of
    the submitted batch. It must come first because an exact duplicate **is** a same-date
    intersecting pair, so E12 swallows every one of them and E15 becomes unreachable — the defect
    that made D29 necessary. Its position in this list is therefore behaviour, not layout. No DB
    constraint; see the E15 note in §5.
  - **E12** — no second action with the same `(account_id, date)` whose symbol set intersects
    this one (D15).
  - **E13** — the action covers **every** account holding `from_symbol` on the action date (D13).
    This is a whole-submission rule, so it validates the N-row batch, not one row in isolation.
  - **E20** — `to == from` required on SPLIT, rejected on EXCHANGE/SPINOFF.
  - **E22** — the `to_symbol` position must not be flagged `ever_oversold` (D16).
  - **E1a.1** — the `from_symbol` position must exist **on the action date** (the corporate-action
    analogue of the date-aware sell guard, using the §6.2 path).
  - Same `(symbol, date)` with two different ratios (§5.1 detail 3) — a stock cannot split two ways
    in one day.

  (**E23 is deliberately NOT in this list** — it is `needs_confirm`, see the soft set below. **E15
  is no longer in the soft set**: it was moved here on 2026-08-10, F-01, closing the last place where
  §6.5 still contradicted D29 and `validate.py`'s implementation, which had to document itself as a
  "deviation from the spec's soft warning".)

  **Soft — `needs_confirm=True`** (blocks until acknowledged, then commits; the 賣超 tier):
  **E7** (SPLIT `ratio == 1`), **E9** (`cost_carry == 1` — the text must state that 回本進度
  MIGRATES to the child and the parent reads 0.00%, per §4.3), **E23**
  (the identifier-signature EXCHANGE, D22 — with a one-click convert-to-SPLIT action attached),
  **D3** (opening dated on an action date), and **N3-price** (the `to_symbol` has no stored price →
  the portfolio XIRR goes dark, see §6.6). The **depth-cap** `needs_confirm` of D31 (§6.2) joins
  this set once W4 lands.

  The three tiers are `validate.py`'s existing ones and are **not** re-invented here: no issue /
  `needs_confirm=True` / `needs_confirm=False`. A finding placed in the wrong tier is a behavioural
  bug, not a wording choice — the hard tier makes a row uncommittable
  (`preview.PreviewRow.has_hard_issue`), which for E23 would block legitimate mergers.
- `api/routers/ledgers.py` — beyond the 5th tab, the edit path must **guard deletions that strand
  an action** (E1a.3). Deleting the buy that created a position is how this state is realistically
  reached, and a delete is invisible to insert-time validation.
- `portfolio/dashboard.py` — the unregistered-symbol skip-set (`bundle.unregistered_symbols` since
  W0) must include the action ledger's **both** symbols (E21), or an unregistered reference reaches
  `quote_ccy()` as a `KeyError` → 500.
- `data_ingestion/import_templates.py` — a 5th template kind `corporate_actions:
  account, date, kind, from_symbol, to_symbol, ratio_to, ratio_from, cost_carry, note`, plus its
  parser and the round-trip header guard test. **Two ratio columns, never one** (E6a): a
  single-column variant is a hard parse error, because silently accepting `0.2857` is exactly the
  failure §3.1(ii) documents. **Both columns are parsed as integers** and a non-integer value is a
  row-level rejection with a zh message (D14) — the importer is the path the two-integer entry form
  cannot police.
- **The user-facing surfaces are designed in §6.7**, not enumerated here — where they live is a
  workflow question, not a file list.

### 6.6 Asserted-unchanged (tests that prove a non-effect)

- `portfolio/cash.py` — cash balances byte-identical with and without corporate actions.
- `portfolio/returns.py::xirr_reporting` — the **flow series** gains no entry.
  Note the *rate* legitimately changes because the terminal market value uses the corrected share
  count. The test asserts the flow list is unchanged and the terminal value moved as expected —
  it does not assert the rate is unchanged.
- `forex/pools.py` — no foreign-cash movement, `covered_ratio` unchanged.
- `export/tax.py` — no new realized rows (corporate actions are non-taxable events here;
  the cash-in-lieu fraction is taxable and appears as the ordinary SELL of §3.2).
- **NOT unchanged — `portfolio/returns.py::xirr_reporting` can go dark.** `returns.py:154-156` is
  all-or-nothing on the terminal value: *any* held symbol without a current price returns
  `rate=None` **for the whole portfolio**. An EXCHANGE or SPINOFF creates a position in a symbol
  that has never been priced, so entering one silently kills the headline XIRR until a refresh
  succeeds. Hence the soft warning in §6.5, an entry-flow price fetch (§6.7), and an explicit test.

### 6.7 Entry surface — where it lives and how it is operated

Design constraint from the data: corporate actions are **rare and reactive**. The owner's real
export has 15 of them across 5.5 years and 50 tickers. Nobody wakes up intending to record one —
they hit a wall, or they are reconciling a statement. A surface that requires the owner to *think
of it first* will not be used, and the failure of not using it is the silent basis loss of §1.

So: **one form, three doors, and the doors are placed where the owner already is when they need
it.** The form is a single shared component (§6.0's principle applied to the UI — one
implementation, three mount points), not three near-copies.

#### Door 1 (primary) — the 賣超 confirm dialog

This is the moment the feature exists for. Today `sell_exceeds_holdings` (`validate.py:175`)
offers 確認 / 取消, and 確認 discards the cost basis permanently. It gains a **third option,
listed first**:

```
⚠ 賣出 200 股，但 2022-09-14 當天只有 85 股

  ▸ 這是公司行動造成的（分割／換股／分拆）→ 補登公司行動     ← 開啟表單，已預填
  ▸ 確認為賣超，接受成本基礎歸零（待釐清）
  ▸ 取消
```

The form opens pre-filled with the account, the symbol, and a date range bounded by the last
reconciling date and the sell's trade date. This converts the most destructive confirmation in the
system into a guided repair — and it is the one place where the owner is *already looking at the
evidence* that an action is missing.

**Door 1 meets D13's all-accounts rule (D28, 2026-08-09).** The owner arrives here to fix ONE
account, and E13 will write N rows. That is correct — the action really did happen in every account
at once, and recording it in one is the state D13 exists to forbid — but it must not be a surprise.
So the preview below expands to **every affected account, before and after**, under a heading that
says so plainly:

```
這筆行動同時影響以下 3 個帳戶（公司行動對每個持有帳戶一體適用）
```

The rule: the owner sees the full scope **before** they commit, never after. A repair that quietly
reaches beyond the account they were looking at would be the same class of surprise as the silent
basis loss this door exists to prevent.

#### Door 2 — the symbol drawer

The drawer already renders the reconciliation footer (§6.3). When it reads ⚠ 對帳不一致, it offers
補登公司行動 beside the mismatch, pre-filled from that symbol. Same component, same prefill logic.

#### Door 3 — the ledger page, as a 5th ledger tab

Deliberate entry, editing and deletion live with the other four ledgers on `ledger.html`, **not**
as a 6th tab on the input page. The input page is high-frequency data capture (trades, dividends);
the ledger page is low-frequency corrective record-keeping. A corporate action is the latter, and
putting it on the input page buries a rare control behind five common ones.

#### The form — plain language over jargon

**Do not ask the owner to classify the event.** SPLIT / EXCHANGE / SPINOFF is our vocabulary, not
theirs. Ask what the statement shows, in terms of the observable effect:

| The question the form asks | Sets `kind` |
| --- | --- |
| 同一檔股票，股數變多或變少 | `SPLIT` |
| 整個部位換成另一檔股票 | `EXCHANGE` |
| 原持股不變，另外多拿到一檔新股票 | `SPINOFF` |

**The ratio is two boxes, phrased the way brokers announce it** — and this is where §3.1(ii)'s fix
becomes a UI affordance rather than a rule to remember:

```
每持有 [  1  ] 股   →   變成 [  3  ] 股          ← 3-for-1 forward split
每持有 [ 10  ] 股   →   變成 [  1  ] 股          ← 1-for-10 reverse split
每持有 [  7  ] 股   →   換得 [  2  ] 股 SYM-B    ← 2-for-7 exchange
```

The two integers map directly to `ratio_from` / `ratio_to` with no mental arithmetic and nothing to
convert, and the boxes accept integers only.

> **This form is an affordance, not the guard.** An earlier version of this section claimed the
> rounded quotient was "unreachable" because the form has no decimal box. It is reachable — through
> the CSV importer and the API, neither of which the form constrains. The guard is E6/E6a in
> `validate.py` (D14, §3.1(ii)(b)); the form merely makes the right thing the easy thing.

#### The preview — the conservation law, made visible

Always on, updating as the owner types. This is the single most important element on the form:

```
                股數        均價              成本總額
行動前   TSLA      85    US$ 264.51        US$ 22,483.35
行動後   TSLA     255    US$  88.17        US$ 22,483.35
                                          ────────────────
                                          成本不變 ✓
```

Three jobs: it shows the resulting share count so the owner can check it against the statement
(§3.1's whole justification for storing a ratio), it shows the average correcting itself, and it
states **成本不變** explicitly. A SPINOFF preview is two rows — parent-after and child — with
`成本合計不變 ✓` across both. The owner learns the accounting model by watching it hold, which is
worth more than any documentation of it.

**When the symbol is held in more than one account, the preview repeats per account** (D13/D28) with
a combined `成本合計不變 ✓` across all of them, plus a line naming any account **not** affected
because its position opens after the action date. Showing the untouched account by name is not
clutter — it is how the owner can tell the system understood their ledger rather than merely applied
a rule.

When the action would unblock a currently-failing sell, say so **before** saving:

```
✓ 這筆行動會讓 2022-09-14 的 200 股賣出通過檢查（目前為賣超）
```

#### Follow-ups the form offers, so they are not forgotten

1. **Cash-in-lieu (§3.2).** If the result is fractional, offer it directly on save:
   「產生 0.7195 股零股，券商通常折現。要現在補登這筆賣出嗎?」 with the fraction pre-filled and
   the price left blank. Left to the owner's memory, §3.2 stays theoretical.
2. **Multi-account (E13) — NOT optional (D13).** If the symbol is held in more than one account on
   the action date, list those accounts, state how many rows will be written, and make the list
   **read-only**: the owner cannot deselect one. The partial state is rejected by validation
   anyway, so a deselectable checklist would only offer a choice that ends in an error — and the
   error would arrive after the owner had formed the belief that partial application is legal.
   Accounts whose position opens after the action date are listed separately as *not affected*, so
   the owner can see the system understood their ledger rather than merely obeying a rule.
3. **The price for a new `to_symbol` (§6.6).** Trigger a quote fetch on save and say so, because
   otherwise the portfolio XIRR goes dark with no visible cause.
4. **The reorganisation fee (§3.3).** If the statement carried one, offer the cash-movement entry
   with the D12 caveat shown inline.

---

## 7. Verification plan

### 7.1 Unit tests — `tests/portfolio/test_corporate_actions.py`

One test per §4 formula and per §5 edge row. Specifically including:

- forward split, reverse split — `original_total` / `adjusted_total` / `gross_invested` unchanged,
  `shares` scaled, both averages scaled by `1/ratio`;
- split on a position **with DRIP history** (`adjusted ≠ original`) — both scale, neither changes,
  `payback_ratio` unchanged;
- split → sell — realized P&L computed against the **post-split** adjusted average;
- exchange 1:1 (rename) and n:m; exchange into an **existing** position (weighted-average merge);
- spinoff with `cost_carry`, then sell the child; parent's remaining basis is `(1−c)×` the original;
- split on an open declared short (E4);
- **E18** — EXCHANGE and SPINOFF into a `to_symbol` that holds an open short: rejected strict,
  skipped+flagged on the dashboard path. Detection power: without the guard the emitted holding
  blends a real basis with short proceeds, so the test must assert on the *rejection*, and a
  second test must show the un-guarded formula produces the incoherent position (otherwise the
  guard is untested);
- **E19** — `unbookable_dividend` propagates through EXCHANGE and to the SPINOFF child. The
  failing-before-the-fix case: EXCHANGE a flagged position and assert the successor is **still**
  flagged (pre-fix it renders clean, which is the defect);
- **E20** — self-EXCHANGE and self-SPINOFF rejected; SPLIT with `to != from` rejected;
- **E1a** — an action whose `from_symbol` has no position on the action date: raises strict,
  **skips + flags** on the dashboard path. A regression test must assert
  `GET /api/dashboard` returns **200**, not 500, for that ledger;
- **E21** — an action referencing an unregistered symbol appears in
  `freshness.unregistered_symbols` and does not 500;
- **§3.1(ii)(a) exactness** — a 2-for-7 EXCHANGE of 700 shares yields **exactly** `Decimal("200")`,
  and a later sell of 200 passes `validate.py`'s bare `>` comparison.
  - **Detection power — CORRECTED 2026-08-09.** The companion test previously specified here
    asserted that the parenthesised form `shares × (to / from)` does **not** equal 200 for this
    case. **Measured: it does equal 200**, so that test fails on its first run. Re-point it at a
    fixture where the two forms genuinely differ across an integer boundary — verified examples:
    `3 × 1/3` (exact `1`, parenthesised `0.999…9`) and `935 × 18/17` (exact `990`, parenthesised
    `989.999…998`). Assert the multiply-first form hits the integer and the parenthesised form does
    not, so "simplifying" the evaluation order back fails the suite.
- **§3.1(ii)(b) integer terms (D14)** — `ratio_to = 0.2857, ratio_from = 1` is rejected by
  `validate.py`, by the CSV importer and by the API. Detection power: without the rule the same
  input yields `199.9900`, a later sell of 200 trips the guard, and `build_book` reports
  `original_total == 0` with `ever_oversold` — assert that whole cascade in a test marked as the
  pre-fix behaviour, so the guard cannot be quietly removed;
- **E12 ordering (D15)** — two same-date actions with intersecting symbols are rejected. Detection
  power: with the rejection removed, the two insertion orders produce 600 and 200 shares from the
  same fixture **while §7.1a stays green** — assert both facts, because the second is the reason
  the rejection has to exist rather than a test;
- **E13 multi-account (D13)** — an action covering fewer than all holding accounts is rejected;
  and, as the proof the warning could not have substituted for it, a test asserting the drawer
  footer of the un-actioned account reads `balances == true` with `diff_shares == 0` while its
  market value is wrong by the ratio;
- **E22 (D16)** — EXCHANGE/SPINOFF into an `ever_oversold` destination: rejected strict,
  skipped+flagged on the dashboard path. Detection power: without the guard the destination's
  emitted `original_cost_total` becomes the carried basis over shares that include the
  basis-discarded ones, producing a plausible average — assert on the rejection AND on the
  incoherent number the un-guarded formula gives;
- **D22 price scope is SPLIT-only, and provably so in BOTH directions** — two tests, because a
  scope is only correct if it excludes as reliably as it includes:
  - a 1-for-20 SPLIT: the net-worth series is continuous across the action date; **without** the
    re-expression it drops by 95%, so the detection-power companion must fail before the fix;
  - an EXCHANGE with `ratio_to != ratio_from` into a `to_symbol` **that you already held**: every
    stored price of that symbol is **byte-identical** before and after the action is entered. This
    is the test that stops a future reader from "fixing" the scope back to include EXCHANGE — the
    change would corrupt an untouched price history, and only this assertion catches it;
- **E23 soft warning (D22)** — that same EXCHANGE fires the warning, and entering it anyway
  succeeds. Assert the warning is **non-blocking**: a test that only checks the text would pass
  equally on a hard rejection, which is the outcome this decision deliberately avoided;
- **D19 identifier rejection** — an **unregistered** `from_symbol` is rejected at validation (E10)
  and never auto-registered in `instruments`. **Reworded 2026-08-10 (F-41):** this said
  "identifier-shaped", the wording D24 abolished — it is the phrasing that leads an implementer to
  write the regex trap #17 exists to stop, and a shape test eventually rejects a legitimate ticker
  and locks the owner out of their own ledger. The rejection is keyed on a **database fact**;
  only the message branches;
- **D21 provenance label** — a SPINOFF child's 回本進度 and `fully_recovered` both render with the
  parent's provenance; and at `cost_carry == 1` the parent reads `0.00%` (the E9 migration), with
  the soft warning naming that consequence;
- every rejection in §5 raises the expected type with a zh message.

### 7.1a The conservation property test (§2.1) — the law, not the formulas

One property test, run over a fixture ledger containing all three kinds, asserting the §2.1 law
directly rather than any single formula. For each action date `D`, build the book at `D − 1` and at
`D` (the fixture keeps the action alone on its date, per §2.1's note about the two deliberate
exceptions) and assert, per quote currency:

```
Σ original_total     equal
Σ adjusted_total     equal
Σ dividend_portion   equal
gross_invested       equal
```

Two things about *what* is summed, both of which decide whether the test means anything:

1. **Sum over all positions, including zero-share ones.** The holdings loop drops them
   (`cost_basis.py:294`), so summing `Book.holdings` would silently exclude the EXCHANGE-emptied
   source and pass for the wrong reason. Assert on the replay's internal state or an
   `_Position`-level accessor.
2. **Sum the RAW accumulators, not the emitted form.** `Holding.original_cost_total` is
   `pos.original_total − pos.short_proceeds` (`:292`), so summing it imports the short-cover
   residue `ε` described in §4.4 — a pre-existing artifact of `(P/S)×S`, unrelated to corporate
   actions. Chasing it would send an implementer to fix the wrong thing. Sum `pos.original_total`
   and `pos.adjusted_total` directly; the law is about those.

Plus the value leg, in `tests/portfolio/test_timeseries.py`: across a SPLIT date with **no**
post-split price stored, `TrendPoint.total_value` is **equal** on `D − 1` and `D` (§5.1(d)'s
re-expression). Two detection-power companions, both of which must fail before the fix:

- without the re-expression the value is inflated by `ratio` (the original E17 artifact);
- with the superseded "mark incomplete" resolution the value **drops by the position's full
  market value** — the cliff measured in §5.1(d).

This is the cheapest detector in the plan: it catches a rounded ratio, a mis-denominated price, and
any future formula edit that fails to balance — none of which the per-kind unit tests would notice
on their own.

> **What it does NOT catch: a dropped `unbookable_dividend` (F-39, 2026-08-10).** This paragraph
> claimed it did. It cannot: the assertions are **four Decimal sums**, and a sum cannot observe a
> boolean — the flag could be silently lost on every EXCHANGE in the fixture and all four totals
> would still be equal. Its real detector is §7.1's **E19** bullet, which exchanges a flagged
> position and asserts the successor is **still** flagged (pre-fix it renders clean, which is the
> defect). A test believed to cover something it cannot is worse than an absent one, because it
> stops the real test from being written.

### 7.1b The idempotency test (§5.1(c))

The price reconcile must be provably re-runnable, because E16 guarantees it will be re-run:

- run reconcile **twice** → the stored close is identical after the second pass (the old
  `fetched_at`-only rule multiplied again);
- **edit** a SPLIT's ratio → the close moves to the new value, not the compounded one;
- **delete** the SPLIT → every affected close returns to its exact pre-action value;
- enter two splits in **either order** → the same final close **at the stored value**, not merely
  the same `target`;
- a symbol with **no** corporate action → every price row byte-identical, including the basis
  column.

> **Under D30 (2026-08-10) the first four clauses hold BY CONSTRUCTION, and that is the point.**
> They were written against the single-column design, where the close was rescaled in place and
> idempotency depended on the short-circuit while order-independence was **false at the stored
> value** (measured: 0.0014 vs 0.0015, and neither equal to the one-shot figure — see D30). With the
> raw provider close stored, every operation recomputes `close := raw × target` from an unchanged
> input, so all four are structural. Keep the tests: they now assert that the implementation really
> does rebuild from the raw column, and a red result means it does not. The old parenthetical
> "assert this fails without `split_basis`" is dropped — it asked for a test against an algorithm
> that will not exist in the codebase, so it could only ever degrade into a comment.

### 7.2 The parity test (§6.2) — the single most valuable test here

`shares_through` vs `build_book` over a fixture ledger that contains all three kinds, in a
**property style** across every `(account, symbol)` — not a hand-picked case.

**What "every `(account, symbol)`" means, normatively (F-16, 2026-08-10).** The natural reading is
"iterate `book.holdings`", and that is a test that **cannot fail on the case this package exists
for**: `cost_basis.py` drops every zero-share position before emitting, so the **EXCHANGE-emptied
source** — the exact position F-07 and F-08 are about — is structurally excluded. §7.3's footer
inherits *all* of its detection power from this test (the reconciliation identity reduces
algebraically to `shares_action_aware == build_book`), so if this ships as written, W4 and W5 are
both verified by something that cannot go red. Therefore:

- iterate the **union of `(account, symbol)` across all four ledgers, plus both symbols of every
  action** — not the emitted holdings;
- assert against the replay's **internal position map**, the same source §7.1a rule 1 requires, not
  against `Book.holdings`;
- carry explicit fixtures for the **EXCHANGE-emptied source** and for F-18's **same-day opening**
  (`EventPriority.OPENING = 0 < CORPORATE_ACTION = 10`, and D3 rules a same-day opening pre-action —
  so the walker's date bound is `opening <= D` while `transactions` / `dividends` / `actions` are
  `< D`. Three bounds, not one);
- equality is asserted for **unflagged** positions and divergence merely permitted on flagged ones
  (§6.3), which is why D33's `< 0` skip must be flagged rather than silent — an unflagged divergence
  must always be a failure.

### 7.3 Contract tests

- `_reconcile` reports `balances: true` for a symbol carrying a corporate action, and
  `diff_shares == 0`; a deliberately wrong `corporate_delta` makes it false (**detection power**).
- The golden dashboard payload (`tests/golden/dashboard_full.json`) round-trip still passes;
  regenerate only if the wire shape genuinely gains a field.
- `test_web_pdapi_only` and the static-cache discipline test stay green.

### 7.4 Stress-audit oracle — `scripts/stress_audit/`

The oracle is an **independent reimplementation**, so it must implement §4 from this spec rather
than by reading the app's code:

- `oracle.py` — a `CorpFact` dataclass, a field on `Facts`, a branch in `replay()`, and the same
  priority ordering. It carries **its own** two-term ratio arithmetic and must **not** import
  `shared/corporate_actions.py`: an oracle that calls the implementation it is checking proves
  nothing. This is the one place where §6.0's "one owner" rule is deliberately suspended, and the
  reason is worth stating in the file itself so a future cleanup does not "de-duplicate" it.
- `run_phase1.py` — a scenario op sequence exercising: forward split → sell; reverse split →
  cash-in-lieu sell; exchange into an existing position; spinoff → sell child; split on a
  DRIP-adjusted position; a **2-for-7 exchange** (the §3.1(ii) exactness case); and at least one
  rejection path.
- Gate: `python scripts/stress_audit/run_all.py --phase 1` → `fail=0`.

### 7.5 Accounting manual

`docs/accounting-formula-manual.md` (zh, the authority) gains a new **§ 公司行動** placed after
§4 成本基礎 and cross-referenced from §5 已實現／未實現損益 and §7 總報酬. It must carry the three
formulas of §4, the edge matrix of §5, a worked numeric example per kind, and a verification
anchor tying each to its oracle scenario. `docs/accounting-formula-manual.en.md` is the
hand-maintained mirror and is updated in the same change; §12 附錄's table of contents and the
manual version line both move.

**Two limitations must be written up, and one of them changed on 2026-08-10.** **D11** (volume is
not un-adjusted) is stated as a standing limitation. **D12** is **not**: D36 resolves it. The manual
must say that the reorganisation fee is invisible to **XIRR by design** — XIRR is deliberately
untouched, so every historical figure, every worked anchor in this manual and every oracle
expectation stays where it is — and that it **is** visible in the whole-account IRR added in
`portfolio/twr.py`. Writing D12 up as a permanent blind spot, which this section previously implied,
would document a decision the owner has since reversed. If W9 somehow runs before D36 is
implemented, it writes "pending D36", never a permanent statement. Cash-and-stock mergers are
documented as a **hard exclusion** (D34) — §9's two-row recipe is withdrawn and must not appear in
the manual as a procedure; the nearest expressible workaround is recorded there as unofficial, with
its inexactness stated.

### 7.6 E2E

Enter each kind through the UI; assert the resulting position, the drawer's `＋公司行動` term, the
footer reading ✓ 對帳一致, and that a sell which was previously blocked as an oversell now passes
validation.

### 7.7 Demo-site corpus — `scripts/seed_demo.py` (D25, 2026-08-09)

`seed_demo.py` appeared **nowhere** in this plan until 2026-08-09, and that is a shipping defect,
not an omission of detail. `engineering-process.md` requires the tag to be deployed to the **test
site first and verified there** before prod — and the demo ledger contains no corporate actions, so
that verification would exercise **none** of this feature. The staged deploy would return green
having tested nothing.

Seed one of each kind, chosen to cover the decisions that only appear at runtime:

| Seeded | Covers |
| --- | --- |
| A forward SPLIT on a symbol **held in two accounts**, both rows written | D13's all-accounts rule, and §5.1's multi-account price dedup — the 27-for-1 trap only exists for multi-account holders |
| A SPLIT whose price history spans the action date | §5.1(b)(c)(d): `split_basis`, the reconcile, and net-worth continuity, on a real HTTP round trip |
| An EXCHANGE (rename, `ratio = 1`) | the §6.3 drawer term and the `＋公司行動` footer |
| A SPINOFF with `cost_carry` | D21's provenance label on the child's 回本進度 |

Demo data **accumulates and is never reset** (owner ruling 2026-07-31), so this is seeded once and
keeps paying. `verify_live.py` gains an assertion that the drawer of the multi-account symbol reads
✓ 對帳一致 on **both** accounts — the one check that would have caught D13's defect from outside.

### 7.8 Gates

`pytest` green · `mypy --strict` clean (bare invocation) · `ruff` clean · stress-audit phase 1
`fail=0` · self-review pass over the diff.

---

## 8. Owner decisions

**Status: ALL DECIDED.** D1–D5 and D7 were approved 2026-08-06 (D6 later superseded by review
finding B1). D8–D12 were approved **2026-08-08**. **D13–D21 were approved 2026-08-09**, after the
owner reviewed each one as a working demo (`docs/spec/2026-08-09-corporate-actions-grill.html`,
85/85 self-checks, `verify_report.py` PASS) rather than as prose — every one on the stated
recommendation. D22–D28 were approved in grill round 2 (2026-08-09) and **D29** was found during W2.
**D30–D37 were approved 2026-08-10**, every one on the stated recommendation of the spec-conflict
audit (`docs/audit/2026-08-10-spec-conflict-audit.md` §3). No decision remains open; the table is
retained as the decision record.

**A superseded decision keeps its number and its row.** The rendering is `~~Dn~~` with the question
struck through and the outcome cell opening **SUPERSEDED `<date>` (`<by what>`)** — established for
D6, and applied on 2026-08-10 to **D18** and **D26**, both of which had been withdrawn in prose
while their rows still read as live instructions. Nothing is renumbered and nothing is deleted: the
reasoning of a decision that turned out wrong is part of the record.

| # | Question | Recommendation — **all APPROVED** |
| --- | --- | --- |
| D1 | **`ratio` vs absolute post-action share count** as the stored authority (§3.1) | Ratio, with the computed result shown at entry. **AMENDED 2026-08-08** — the ratio is stored as its two terms (`ratio_to` / `ratio_from`), not as one Decimal; the owner's original approval of "比例" stands and is strengthened, see §3.1(ii) |
| D2 | **Cash-in-lieu as an ordinary SELL** (§3.2), rather than a field on the action | Yes — it produces correct realized P&L on the fraction and keeps one rule |
| D3 | **Opening inventory dated on an action date** — pre-action or post-action? | Pre-action (priority 0 before 1), with a soft warning at entry |
| D4 | **SPLIT on an open short supported, EXCHANGE/SPINOFF on a short rejected** (E4/E5) | As stated — the first has an honest booking, the others do not |
| D5 | **Auto-archive the dead `from_symbol` after an EXCHANGE?** | No automatic side effect; surface a hint instead |
| ~~D6~~ | ~~**E17 price re-fetch** — hint only, or a one-click re-fetch wired into the entry flow?~~ | **SUPERSEDED 2026-08-07 (review finding B1).** The premise was inverted: a re-fetch returns *split-adjusted* closes and `upsert_prices` overwrites, so the recommended one-click action would have destroyed the correct as-traded rows and manufactured the artifact. Replaced by the §5.1 remedy (canonical as-traded basis · un-adjust at the write seam · `fetched_at`-discriminated one-shot correction · carry-forward gap guard). Re-fetch becomes safe only *after* that lands |
| **D8** *(new)* | **§5.1(b) changes what a stored price means.** It is a no-op for every symbol without a SPLIT, but it is still a write-seam change in `pricing/`. Ship it inside this version, or as its own preceding change? | **Inside this version.** The corporate-actions ledger is what the un-adjustment reads; shipping the ledger first would leave a window where entering a SPLIT visibly breaks the trend |
| D7 | Does this ship as **one version** or split into waves (ledger+replay, then UI+import)? | One version — a half-shipped ledger with no entry surface is not usable, and the verification set is what makes it trustworthy |
| **D9** *(new)* | **The `LedgerBundle` + `shared/ledger_registry.py` refactor** (§6.0) — do it as a preceding no-behaviour-change commit, or keep threading a 5th parameter through 8 call sites? | **Do the refactor first.** It is the owner's 模組化集中 directive applied to the exact structure that made this spec's integration list ten items long. Mechanical, covered by the existing suite + golden payload (any moved number = wrong refactor), and it makes the feature diff readable instead of eight-way scattered |
| **D10** *(new)* | **§5.1(b) adds a `split_basis` column to `prices`.** Additive with `DEFAULT '1'`, so every existing row migrates in byte-identically — but it is still a schema change to a pricing table | **Inside this version**, with D8. The column is what makes the correction idempotent (§7.1b); without it the reconcile is a latent double-application bug that E16 schedules |
| **D11** *(new)* | **`volume` is not un-adjusted** (§5.1). Volume-based signals spanning a split date are therefore not comparable | **Accept and document.** Volume is not money-of-record, and the direction of the provider's volume adjustment has not been measured. Guessing would break the same rule this section enforces; a probe belongs with P1a |
| **D12** *(new)* | **The reorganisation fee** (§3.3) — book as `WITHDRAW` and accept that it is invisible to XIRR / `total_return`, or add a `FEE` movement kind wired into the XIRR flow list? | **`WITHDRAW` + a documented known limitation.** A `FEE` kind is a money-of-record change to `returns.py` affecting every XIRR in the system, and the fee belongs to a whole class (interest, ADR fees, tax reclaim) that is **P2's** scope. Solving one member of the class here would leave the manual self-contradictory. Flagged for the owner because "should a fee move the reported return" is a judgement, not an implementation detail.<br>**AMENDED 2026-08-10 by D36 — the limitation is no longer permanent.** XIRR stays untouched (option 2), and a **whole-account IRR** is added in the existing `portfolio/twr.py`, which *does* see a `WITHDRAW`. So the fee is invisible to XIRR by design and visible in the second metric. §3.3, §7.5 and W9 must state it that way; the ruling itself — book it as a `WITHDRAW` — is unchanged |

| **D13** *(2026-08-09)* | **Multi-account partial application** (E13) — warn, or reject? | **Reject.** The warning the spec promised (「drawer shows ⚠」) provably never fires: the reconciliation identity is exactly satisfied for an account with no action row, so the footer reads ✓ while its market value is wrong by the ratio. The defect lives between the share count and the price, and nothing computes that relationship. Demo §Q1 |
| **D14** *(2026-08-09)* | **Ratio terms** — `Decimal > 0`, or **positive integers**? | **Positive integers**, enforced in `validate.py`. `> 0` admitted a rounded quotient through the CSV importer and the API, reproducing the exact cascade §3.1(ii) exists to prevent; the form's two integer boxes constrain only the form. A decimal exchange ratio from a press release is entered as the registrar's fraction, never as the rounded decimal. Demo §Q2 |
| **D15** *(2026-08-09)* | **Same-date actions** — tie-break on `id` ASC, add a `sequence` column, or reject? | **Reject** when the symbol sets intersect. `id` is typing order; the two orders produce a 3× difference in shares and average cost, and §7.1a is green on both. A `sequence` column taxes every row for a case the real ledger never sees. Demo §Q3 |
| **D16** *(2026-08-09)* | **Destination `ever_oversold`** on EXCHANGE/SPINOFF — allow with the flag kept, or reject? | **Reject (E22).** Keeping the flag does not help: the flag already failed to stop a confident number being rendered, which is exactly what happens here — a discarded basis is averaged into a real one and reads as ordinary. Resolve the 賣超 first. Demo §Q4 |
| **D17** *(2026-08-09)* | **How the split factor reaches `pricing/`** — import `data_ingestion`, move the SELECT to `shared/`, or inject a callable? | **Inject** `factor_of: Callable[[str, date], Decimal]`, defaulting to `Decimal(1)`. Zero new dependency edges, §6.0 Owner 2 intact, every existing caller unchanged. Also corrects the plan: `pricing/refresh.py` — the only production caller of `upsert_prices` — appeared in no file list in the document. Demo §Q5 |
| ~~**D18**~~ *(2026-08-09)* | ~~**§5.1's scope** — SPLIT only, or any re-denominating action?~~ | **SUPERSEDED 2026-08-09 by D22.** The approved answer was to **widen** the scope to `SPLIT or (EXCHANGE and ratio_to != ratio_from)`. Folding it in revealed that an EXCHANGE **adds to** its destination rather than re-denominating it (§4.2 `Q.shares += carried`), so the clause would corrupt the entire price history of any symbol already held before a merger into it — it would cover only the two cases where it is wrong. **The finding stands**: the 95% net-worth cliff is real and measured (1-for-20 over 200 shares at 0.4250: 85.00 → 4.25). **The remedy is D19**, at the import seam — normalise the identifier so the event is entered as a SPLIT — with **E23** guarding rows that pre-date it. Widening the price predicate is now trap #16. *(Row marked SUPERSEDED 2026-08-10, F-03: the withdrawal was recorded in §5.1 and in D22 but this row still read as a live instruction, and §10.2's W6 done-when was still demanding the widened test.)* |
| **D19** *(2026-08-09)* | **A broker identifier string** — first-class `instruments` row, or normalised to its ticker at import? | **Normalise at import**, asking the owner when it cannot be resolved. Registering it creates a non-security instrument, a permanently failing quote key, a position invisible where it would be explained, and forces the event into the EXCHANGE path that drags in D18's whole problem. Narrow by design: identifier changes only — a genuine merger into a different ticker stays an EXCHANGE. Demo §Q7 |
| **D20** *(2026-08-09)* | **Cash-and-stock mergers** — model a fourth kind, or exclude with a recipe? | **Exclude in §9, with the two-row recipe written down** (allocate basis by relative consideration; cash leg = ordinary SELL, remainder = EXCHANGE) — the same precedent D2 set for cash in lieu of a fraction. It was in neither the model nor the exclusions, so an implementing agent would have invented something. Carries a required §7.1a amendment: the two legs are same-day by construction, so conservation measures the exchange leg only. Demo §Q8.<br>**AMENDED 2026-08-10 by D34 — the EXCLUSION stands, the RECIPE is withdrawn.** The two-row recipe is unexecutable: `EventPriority` runs CORPORATE_ACTION (10) before SELL (30), so the EXCHANGE zeroes the source and the same-day SELL then produces `OversellError` / STICKY 賣超 with the basis discarded. The §7.1a amendment this row required is withdrawn with it (§2.1). See D34 and §9 |
| **D21** *(2026-08-09)* | **The SPINOFF child's 回本進度** — display as-is, label its provenance, or suppress? | **Label the provenance.** The arithmetic is correct and unchanged; only what the screen claims is at issue — a child that has never paid a dividend renders 「已回收 40%」. Suppressing loses information; labelling keeps it and tells the truth. Includes `fully_recovered` and the `cost_carry == 1` migration case. Demo §Q9.<br>**Reachable only because of D35 (2026-08-10).** The label renders `dividend_portion`, and a US position has one only if a US **cash** dividend reduces `adjusted_total` the way TW/MY cash does — which is what D35 rules. Under the income model `dividend_portion` is always 0 for a US position, i.e. on the only data that has corporate actions, and D21 would have nothing to label. D35 itself lives in the broker-import backlog spec (P1b); it is recorded here because it is the precondition for a decision this spec has already scheduled into W7 |
| **D22** *(round 2, refined and CONFIRMED 2026-08-09)* | **D18's EXCHANGE clause** — keep the widened price scope, or withdraw it? And what guards the residual hole? | **Withdraw; scope stays SPLIT.** Folding D18 in exposed that an EXCHANGE *adds to* its destination rather than re-denominating it (§4.2 `Q.shares += carried`), so the clause would corrupt the price history of any symbol already held before a merger. D18's finding stands; D19 is its remedy.<br>**On the guard, "reject or warn" was a false choice** — `validate.py` has THREE tiers and E23 takes the middle one (`needs_confirm`, the 賣超 tier: blocks until acknowledged, then commits). It also gains a **four-part condition** so it fires on the identifier case and stays silent on ordinary mergers (`from_symbol` has NO prices — a real merger source is a listed security and has them), plus a one-click convert-to-SPLIT. Stronger than the approved rejection *and* than the interim warning |
| **D23** *(round 2, refined and CONFIRMED 2026-08-09)* | **The `holdings.py` action-aware walker** — cycle guard, recursion, per-row cost | **Depth cap + one `ActionIndex` per batch; no validation-time cycle rejection.** The omission is confirmed: D15 makes the walk terminate by construction, so the check would only block a legitimate re-use of a ticker. **Two details were missing and are now normative** (§6.2): the recursion must ask for the source's state **strictly before** the action's date — the inclusive form re-enters itself and hangs, a one-character difference — and the depth cap must **degrade on read paths rather than raise**, since `corporate_delta` reaches the drawer's API route and a bare raise there is a 500 |
| **D24** *(round 2)* | **How an identifier is refused** — string-shape test, or a database fact? | **A fact: it is not registered (E10).** No regex. A shape test eventually rejects a legitimate ticker and locks the owner out of their own ledger; the registration check has zero false positives by construction. Only the *message* branches |
| **D25** *(round 2)* | **Demo-site corpus** | **Seed one of each kind in `scripts/seed_demo.py`, incl. a multi-account SPLIT.** Without it the mandatory staged deploy verifies none of this feature. Demo data accumulates and is never reset, so it is seeded once |
| ~~**D26**~~ *(round 2)* | ~~**W0 (`LedgerBundle` + registry refactor)** — its own version, or bundled?~~ | **SUPERSEDED 2026-08-09 (owner ruling).** The approved answer was "its own version, deployed and verified on the demo site before W1". The owner then ruled that **`feat/corporate-actions` is not merged into `main` until the whole feature is done**, so the branch stays abandonable — and a separate W0 release would require merging an unfinished feature, giving up exactly that. W0 therefore ships **inside the one feature release** (D7's answer, unchanged). D26's underlying concern is met differently: the golden payload (§7.3) covers the wire shape, and the single release is deployed to the demo site and verified against a run taken before it, so a moved number is still observable on a real accumulated ledger. *(Row marked SUPERSEDED 2026-08-10, F-02: the retirement was recorded in the status header and the revision log, while this row, §10.2's W0 row, its eight-line justification and trap #23 all still mandated the separate release.)* |
| **D27** *(round 2)* | **§10.5's acceptance run** — who, when, and is failure blocking? | **Owner-run, on their own machine, after W10 and before `/ship-version`; failure is BLOCKING.** Delivered as `scripts/verify_corporate_actions.py` — the script is committed, the data and the output are not. A gate that depends on remembering is not a gate |
| **D28** *(round 2)* | **Door 1 vs D13's all-accounts rule** | **Write all N rows, and show all N accounts before the owner commits.** The action really did happen in every account at once; but a repair that quietly reaches past the account the owner was looking at is the same class of surprise as the silent basis loss door 1 exists to prevent |
| **D29** *(found during W2, 2026-08-09 — implementation, not review)* | **E15's duplicate action** — soft warning as specified, or hard? | **Hard, and checked BEFORE E12.** Two defects. (a) Soft is the wrong tier: a duplicate *transaction* is plausible, a duplicate *event* is not, and acknowledging it applies the ratio twice — a 3-for-1 becomes a 9-for-1. (b) As specified it was **unreachable**: an exact duplicate is by construction a same-date intersecting pair, so E12's hard rejection swallowed every one. That is the same "the ⚠ provably never fires" defect the E13 note was rewritten to remove, recurring one row later and surviving two review rounds plus a grill. Found by the test, not by reading. See the E15 note in §5 |

**D30–D37 — approved 2026-08-10**, each on the recommendation of the spec-conflict audit
(`docs/audit/2026-08-10-spec-conflict-audit.md` §3), which states the **system fit** of every one:
a locally reasonable answer that adds a sixth way of doing something the system already does five
ways is a net loss, so each answer reuses a mechanism the codebase already has.

| # | Question | Recommendation — **all APPROVED 2026-08-10** |
| --- | --- | --- |
| **D30** *(blocks W6a)* | **The price basis** — one stored column (re-express and divide back out), or two (keep the raw provider close)? | **Two columns.** Keep the raw provider close and define **both** operations as `close := raw × target` (§5.1(c)). The single column is not order-independent *at the stored value*: `_cap_dp` re-applies on every pass and the orders traverse different intermediates — measured, `(1/3)` then `(7/2)` on 0.0013 stores 0.0014 one way and 0.0015 the other, 7.7% apart, and neither equals the one-shot value, while §5.1 claims order-independence and §7.1b tests it. Reconstruction also cannot recover an as-traded price from an already-rounded feed (US 7-for-1: 100.07 → 100.10). With two columns idempotency, reversibility and order-independence hold **by construction**, and detail 1's short-circuit demotes to an optimisation. *System fit:* `data-and-pricing.md` requires storing at full source precision and calls the 4-dp cap "representation noise, not information" — which stops being true once the close is derived and the source is discarded; it also brings `prices` under CLAUDE.md #7, where nothing authoritative is overwritten. Cost: one TEXT column |
| **D31** *(blocks W4)* | **The depth cap** — what does the walker return when it degrades? | **Not `Decimal \| None`. Split by path** (§6.2). Read paths keep the bare `Decimal` signature and record the capped symbol in a per-request set, surfaced through the same 待釐清 chip path `unbookable_action` uses; the **validation** path reads that set and raises a **`needs_confirm`** issue. *System fit:* the codebase has exactly one vocabulary for "this number exists but is not trustworthy" (`price_stale`, `oversold`, `short_open`, `unbookable_dividend`, `unbookable_action`) and one for "validation must not guess" (`validate.py`'s three tiers). A nullable return would be a **sixth** mechanism, forcing nine call sites to each invent a policy — several of which only want a boolean. Reusing the two existing ones changes no signature and adds no concept |
| **D32** *(a live defect in W3)* | **A dividend on a symbol an EXCHANGE moved away** — book it, redirect it, or refuse it? | **Refuse and flag, uniformly for both branches** — raise strict, skip + flag on the dashboard path; the owner records the payment as a **cash movement**. New row **E24** in §5, with its note. The state is reachable because §4.2 leaves the source in the map with zeroed fields, so neither the closed-position nor the short refusal applies: CASH/NET books post-close income on a dead ticker, and DRIP/STOCK **resurrects** the position at `avg = 0` — delisted, never priced, and one unpriced holding blanks the **whole portfolio's** XIRR. *System fit:* it is the posture the replay already takes five times (E3, E5, E18, E22, dividend-on-an-open-short), and "record it as a cash movement instead" is `domain-ledger.md`'s existing remedy for the short case. **Narrow scope:** an ordinary sold-out position keeps today's post-close-income behaviour (the 2026-07-26 audit's H2 ruling); only the action-zeroed state changes |
| **D33** *(blocks W5)* | **§6.3's SQL path applying an action to a negative source** | **Skip the action when the source count is `< 0` on the action date, and flag the symbol** (§6.3). Applied unconditionally it manufactures a destination `build_book` never created — no transaction, no opening, no holding, therefore **no flag** — and the drawer renders `＋公司行動 −100` under a red 對帳不一致 with nothing to explain it. *Why this does not break §6.3's independence argument:* **E3's precondition is a share-domain condition the share-only path computes from its own data** — one comparison, importing nothing. E5 (short state) and E18 (destination short) are not: honouring those would require importing the replay's model and would collapse the two implementations into one, ending the cross-check the footer exists to be |
| **D34** *(blocks W9's manual text)* | **§9's cash-and-stock recipe** — keep it, or withdraw it? | **Withdraw the recipe; cash-and-stock stays a hard exclusion, with the reason stated** (§9). The recipe is unexecutable: `EventPriority` runs CORPORATE_ACTION (10) before SELL (30), the EXCHANGE sets `source.shares = 0`, and the same-day SELL then hits a zero-share position — `OversellError` strict, **STICKY 賣超 with the basis discarded** on the dashboard path: the exact disaster §1 says this feature exists to prevent. Second defect: under weighted average the cash leg disposes of `f × N` shares, so the corrected ratio is `B_received / ((1−f) × N)`, which is generally **not** expressible as two positive integers — which **D14 requires**. §9's stated verification covered only the two degenerate cases where one leg does not exist, neither of which can exhibit either defect. *System fit:* both available repairs dismantle load-bearing rules (widening D14 reintroduces the typed-decimal hazard on 3,530 measured cases; re-ordering the events breaks "an action is effective at the start of its date") for an event the spec records the owner does not have. The nearest expressible form — EXCHANGE all shares, then SELL the **destination** (priority 10 then 30, so the ordering works) — is recorded as an **unofficial workaround with its inexactness stated**, never as a normative recipe |
| **D35** *(P1b; not blocking, cheap now)* | **Does a US cash dividend reduce `adjusted_total`?** | **Yes — treat it exactly as TW/MY cash.** *System fit:* `CASH_DIVIDEND_TYPES` drives four sites uniformly; booking US cash as income instead would put **two dividend accounting models in one ledger**, so 回本進度 and 股利回收率 would mean different things per market on the same screen — which is what `domain-ledger.md`'s one-definition discipline exists to prevent. **Recorded in this file only as D21's precondition** (see D21); the decision belongs to `docs/spec/2026-08-06-broker-import-backlog.md`, where it is written up in full |
| **D36** *(blocks W9's wording only)* | **Does anything besides trades reach the return metrics?** | **Option 2 — leave XIRR untouched, add a whole-account IRR in the existing `portfolio/twr.py`.** *System fit:* `twr.py` already exists as the home for a whole-account view; redefining XIRR would move every historical figure, invalidate the accounting manual's worked anchors and the stress-audit oracle's expectations, and require re-verification against the whole corpus — for amounts the backlog itself calls trivial. Additive instead. **It also resolves D12's conceded blind spot**: the reorganisation fee is booked as a `WITHDRAW`, which XIRR does not see and the account IRR does. §3.3, §7.5 and W9 state the limitation as *resolved*, not permanent |
| **D37** *(owner INPUT, not a decision — longest lead item)* | **Pre-history opening cost totals**: per-symbol `original_cost_total` for every position whose earliest event in the broker export is a **sell** | **Owner-supplied; nobody else can.** It gates §10.5's blocking acceptance run (D27). ⚠ **The shortcut is refused explicitly:** `opening_import.py` validates only `shares > 0`, so **a cost total of 0 imports cleanly** and permanently zeroes the position's basis **with no 待釐清 flag** — strictly worse than the oversell it appears to fix, because the oversell at least announces itself. **`original_cost_total > 0` therefore becomes a hard validation** in the same change (F-13). That is an asymmetry repair — the file already hard-validates the share count — not a new mechanism |
| **D38** *(owner question 2026-08-10: "can a symbol with no corporate action be guaranteed identical to pre-feature `main`, so a defect in the new flow damages only the triggering stock?")* | **Blast-radius containment — is a per-symbol "sandbox mode" the right instrument?** | **No mode. Three testable invariants instead — see the note below.** *System fit:* a runtime mode means two code paths, both maintained and both needing tests, and a configuration in which the new code does not run — so the day it is switched on, every untested interaction arrives at once. This codebase has lost that argument three times already: the aggregate-vs-detail divergence recurred **three** times, `mock-data.js` was retired for being a second source of truth, and `shared/ledger_registry.py` exists because four hand-maintained enumerations drifted. **The containment the owner is asking for already holds structurally for three of the four output tiers; what is missing is that it is neither named nor tested.** And two mechanisms already in this design are strictly stronger than a sandbox: **重算** *undoes* a bad action rather than merely confining it (a sandbox limits damage; replay erases it), and **`corporate_delta`** (§6.3) is a per-symbol runtime cross-check between the naive and action-aware paths that **shows** the discrepancy instead of hiding it |

| **D39** *(two W6a implementation findings, owner sign-off 2026-08-10)* | **(a)** `scheduler/jobs.py` needs the corporate-action ratio to build the injected split factor, but an existing test **forbids** it importing `data_ingestion`, citing `architecture.md` — and `jobs.py` keeps a local copy of `_add_column_if_missing` rather than import one. **(b)** §5.1 said "all four **prices** take the same factor", but only `close` has a preserved raw column | **(a) Narrow the guard to a ONE-line allowlist** (`list_corporate_actions`), and **record the edge in `architecture.md`'s diagram**. *System fit:* the rejected alternatives are worse in this codebase's own terms — injecting from `api/app.py` respects the diagram but degrades to a **silently wrong** price basis if registration is ever missed, and a lookup in `shared/` keeps every edge legal at the cost of a **second SQL site** for `corporate_actions`, which is the duplication `shared/ledger_registry.py` exists to remove. A second test asserts the allowlisted line is actually present, because an exception nobody uses is an exception nobody notices. **An edge that exists in code but not in the diagram is the next audit's F-01**, so the diagram moves.<br>**(b) Only `close` is re-expressed** (§5.1 amended). Multiplying `open`/`high`/`low` off a single `close_raw` produces derived values no reconcile can restate — F-23's defect made unfixable, and outside D38's reversibility invariant. Three more `*_raw` columns were rejected: those three have **zero readers**, verified — every `SELECT` against `prices` names its columns and none names them. `split_basis` keeps their basis recoverable, and the schema carries the warning, because a row with a post-split `close` and pre-split `high`/`low` is a landmine for the first candlestick chart |

---

### 8.1 D38 — the three containment invariants (owner ruling 2026-08-10)

Sort every output this feature can touch by whether it *can* be contained. The answer differs by
tier, and pretending otherwise is what a "sandbox mode" would do.

| Tier | Output | Containable? |
| --- | --- | --- |
| **1 — per-symbol figures** | cost basis, averages, shares, market value, unrealized, realized rows, the drawer | **Already a sandbox, structurally.** `positions` is a `dict` keyed `(account, symbol)` and `_apply_action` touches exactly `src_key` and `dst_key`. It cannot reach another symbol |
| **2 — sums over symbols** | KPI band, sector allocation, weights, net worth, the trend series | **Yes, and already implemented.** `pnl.py` nulls the one position's `market_value`, and every aggregate gates on `market_value is not None`, so the total becomes "everything except the 待釐清 one". Smaller, labelled, and **no other symbol's number is wrong** |
| **3 — indivisible portfolio scalars** | **XIRR** | **No, and it never can be.** One number over one cashflow series and one terminal value. A wrong share count makes the sum wrong, and excluding the position measures a *different portfolio*. Blanking is the only honest option, and it matches the `has_oversold` precedent |
| **4 — stored prices** | the `prices` table | **The real exposure — see invariant 3.** This is the ONLY place the feature writes outside the ledgers, so 重算 does not cover it |

**Invariant 1 — name and test the containment that already exists.** A property test: for any ledger
`L` and any symbol `S` carrying no corporate action, `S`'s `Holding` in `build_book(L)` equals `S`'s
`Holding` in `build_book(L with every action removed)`. Prefer a **structural short-circuit** over an
equivalent computation wherever a new path is added — an explicit "no actions for this symbol → take
the pre-existing branch", not "the new code happens to agree". *Code that does not execute cannot
drift; code that computes an equal answer can.* This binds **W4** above all: `shares_through` becomes
action-aware for *every* symbol, which makes it the largest blast radius in the remaining plan.

**Invariant 2 — XIRR blanks portfolio-wide, and must name the culprit.** The gate is accepted as the
one place a single symbol degrades a portfolio-level figure; the cost is now known rather than
unnoticed, and the code says so at the gate. In exchange, the reason string identifies the **account,
symbol(s) and date** of the unapplied action — unlike the existing 賣超 reason, which says only that
*something* is 待釐清. `Book.unapplied_actions` carries all of it. Blanking a number is tolerable;
blanking it without saying which row to fix is what makes a defect expensive.

**Invariant 3 — `prices` must be provably reversible.** Deleting a SPLIT must return **every affected
row byte-identical** to its pre-action value — asserted on the stored TEXT, not on `Decimal` equality,
because `Decimal("1.5") == Decimal("1.50")` is `True` and a value comparison therefore cannot see a
representation change. Note the related trap, which is exactly the failure this invariant exists to
catch: `Decimal` multiplication by one is **not** identity in the stored representation — the result's
exponent is the *sum* of the operands', so `Decimal("1.5") * Decimal("1.0")` is `Decimal("1.50")`. A
default factor of `Decimal("1.0")` instead of `Decimal(1)` would silently add a decimal place to every
price row in the database, **on symbols that have no corporate action at all**.

> **This is D30's second, independent justification.** The two-column ruling was made on
> order-independence (§5.1). Reversibility is the other half: with one column the raw close is
> overwritten and re-capped on every pass, so **a wrong reconcile would be unrecoverable** — the only
> non-replayable mutation in the entire feature. With `close := raw × target`, `target → 1` restores
> exactly, and tier 4 rejoins the 重算 guarantee that covers tiers 1–3.

---

## 9. Explicitly out of scope

- **Cash-and-stock mergers (D20, recipe WITHDRAWN by D34 2026-08-10)** — "each share of A becomes
  0.6 shares of B plus US$12.00 cash". No fourth action kind is added, **and there is no supported
  way to enter one.** This is a hard exclusion, and the reason has to be stated, because the
  exclusion looks arbitrary next to D2's ruling for cash in lieu of a fraction.

  **The two-row recipe this section carried until 2026-08-10 does not execute.** It prescribed an
  ordinary **SELL** for the cash leg and an **EXCHANGE** for the remainder, both dated on the
  effective date. Two independent defects:

  1. **The order is fixed against it.** `EventPriority` runs `CORPORATE_ACTION = 10` before
     `SELL = 30`, and §4.2's EXCHANGE sets `source.shares = 0`. The same-day SELL therefore lands on
     a zero-share position: `OversellError` on the strict path and, on the dashboard path, **STICKY
     賣超 with the cost basis discarded** — the exact disaster §1 says this feature exists to
     prevent, produced by following the spec. Re-ordering the events is not available: an action is
     effective at the **start** of its date and the day's trades are quoted in post-action terms
     (§4), which every other formula here rests on.
  2. **The corrected ratio is generally not enterable.** Under weighted average the cash leg disposes
     of `f × N` **shares**, so the EXCHANGE carries only `(1−f) × N` and the published ratio
     over-delivers by that factor. The ratio that is actually correct is `B_received / ((1−f) × N)`,
     which is generally **not** expressible as two positive integers — and **D14 requires exactly
     that**. Widening D14 to admit a decimal term would reintroduce the typed-decimal hazard on the
     3,530 measured boundary-crossing cases §3.1(ii) exists to defend against.

  The recipe's stated verification — "a 0% cash leg reduces to a plain EXCHANGE and a 100% cash leg
  to a plain SELL, so the rule is general" — covers only the two degenerate cases **in which one leg
  does not exist**, and neither can exhibit either defect. A general rule cannot be established from
  the cases where it is vacuous.

  **What is still true, and still worth knowing:** booking the whole event as one EXCHANGE moves 100%
  of the basis, leaves the cash booked **nowhere** — not as a receipt, not as realized P&L, not as an
  XIRR flow — overstates the destination's average, and **passes §2.1's conservation law**, because
  the money simply left the book (§2.1a). So the naive treatment is wrong *and* undetectable. That is
  the reason for a stated exclusion rather than a silent one.

  > **Unofficial workaround, if the event ever occurs.** EXCHANGE **all** shares to the destination,
  > then SELL the **destination** for the cash consideration — priority 10 then 30, so the ordering
  > works and no guard trips. It is **not** a normative recipe and must not be documented as one:
  > its realized figure differs from the relative-consideration allocation (the disposal is priced
  > at the destination's terms, not by splitting the basis between the two considerations), and the
  > tax package will therefore report a different gain from the one the registrar's allocation
  > implies. Use it knowing that, or record the event and ask.

  The owner's real de-SPAC events are pure stock, so this exclusion does not block §10.5's
  acceptance test.
- Options-related corporate actions (odd-lot option split reorganizations, option position
  changes) — P3.
- Automatic detection of corporate actions from a data provider — a later item under the
  "unified auto-import principle" already in `CHANGELOG.md`'s Planned section.
- Cost-basis allocation *calculation* for a spin-off. `cost_carry` is user input from the 8-K;
  the system never derives it.
- Tax-lot-level treatment. This project uses weighted average by locked decision.

---

## 10. Implementation plan — the executable form of this document

Everything above is *what* and *why*. This section is *in what order*, *how you know each step is
done*, and *what will silently break if you take a shortcut*. It is written to be handed to an
implementing agent as its brief.

### 10.1 Read this before writing a line

The four non-negotiables. Every one of them was a defect found by review, not a preference:

1. **The ratio is two positive INTEGERS, and the quotient is never formed for a share count.**
   Two rules, both load-bearing (§3.1(ii)):
   (a) `validate.py` rejects a non-integer term on **every** path — form, CSV, API. This is the one
   that was missing, and it is the one that fires at any scale.
   (b) `qty × to / from`, left to right, via `apply_ratio`. `qty × (to / from)` and `split_factor`
   are both forbidden on shares (§5.1 detail 2). A 2-for-7 exchange of 700 shares must produce
   **exactly** `Decimal("200")`.
2. **Conservation is the acceptance test, not a nice property.** Σ`original_total`,
   Σ`adjusted_total`, Σ`dividend_portion` and `gross_invested` are unchanged across every action,
   per quote currency (§2.1). If a change makes that test fail, the change is wrong — including a
   change that looks like a rounding tidy-up.
3. **One owner per concept** (§6.0). If you find yourself writing the ratio product a second time,
   or adding a table name to a second list, stop: the first one is in the wrong place.
4. **Never 500 the dashboard.** `portfolio/dashboard.py` calls `build_book` with **no
   `try`/`except`** (verified — E1a). Every new failure mode needs a skip-and-flag path there,
   and a test asserting `GET /api/dashboard` returns 200.

### 10.2 Work packages and their order

> **Rebuilt 2026-08-10 (audit RC-1).** This section is the implementer's brief, and it had become
> the **stalest text in the document**: an implementer following it literally would have cut a W0
> release the owner ruled against, written a net-worth test §10.4's own trap #16 forbids, and aimed
> a schema migration at a module that crashes a fresh database. Two structural rules now apply, and
> they matter more than any single row:
>
> 1. **A "Done when" clause names the §7 test that defines it — it never restates the criterion in
>    its own words.** A restated criterion is a second source of truth that drifts from the first,
>    which is exactly how the W6 row came to demand a test §7.1 forbids. It is also the defect the
>    ledger registry (§6.0, Owner 3) was built to remove from the *code*; the same rule applies to
>    the plan. If a package needs a criterion §7 does not yet state, **add it to §7 and cite it**.
> 2. **The build order is the audit's**, replacing "W4, W5 and W6 in parallel". Each position is
>    justified below the table, and the reasons are about *where a failure surfaces*, not about
>    package size.

**Build order (2026-08-10):**

```
P0  →  D30–D37  →  W6a  →  W4  →  W6b  →  W5  →  E23 + W7  →  W8 / W9 / W10  →  §10.5  →  ship
```

| # | Package | Status | Blocked by | Files | Done when (§7 clause) |
| --- | --- | --- | --- | --- | --- |
| **W0** | `LedgerBundle` + `shared/ledger_registry.py` refactor (D9) | **DONE** | — | `shared/ledger_registry.py` (new), `data_ingestion/store.py`, `portfolio/cost_basis.py`, the 8 call sites, `db_stats.py`, `export/ledgers.py`, `moomoo_merge.py`, `scripts/merge_reconcile.py` | **§7.3** — the golden payload round-trip unchanged (`git diff --exit-code tests/golden/dashboard_full.json`) with the full suite green. Any moved number means the refactor is wrong. **It does NOT ship as its own version** — ~~D26~~ is retired; it rides the single feature release (D7) |
| **W1** | `shared/corporate_actions.py` + `shared/ledger_events.py` | **DONE** | W0 | both new, no wiring | **§7.1** — the 2-for-7 exactness bullet and its corrected detection-power companion (`3 × 1/3`, `935 × 18/17`); `apply_ratio`, `split_factor`, `ActionIndex`, `EventPriority` unit-tested standalone |
| **W2** | Ledger: schema, CRUD, validation | **PARTIAL** | W1 | `data_ingestion/schema.py`, `store.py`, `validate.py` | **§7.1's per-rejection bullets** — every §5 rejection raises the expected type with a zh message, in §6.5's stated **evaluation order** (E15 before E12, D29). **Still open:** three of the five soft warnings do not exist — **E23** (F-30: it is the guard D19's residual hole depends on, so it is W2 scope, not W7's), **D3** and **N3-price**. E9's text must name the 回本進度 migration |
| **W3** | Replay in `build_book` | **DONE**, output unwired | W2 | `portfolio/cost_basis.py` | **§7.1a** (conservation, basis legs) + **§7.1's** per-kind and per-edge bullets. The unwired output is P0's F-05 |
| **P0** | Audit propagation + the two live defects | **this package** | — | this spec, `docs/spec/2026-08-06-broker-import-backlog.md`; then `portfolio/timeseries.py`, `api/dashboard_models.py`, `portfolio/dashboard.py`, `api/routers/symbol.py`, `web/app.js`, `tests/portfolio/test_corporate_actions.py` | **§7.1 — extended by this package**, because a flag with no consumers had no clause: `unbookable_action` is wired exactly where `unbookable_dividend` is wired (six sites, F-05), and a position carrying it marks the trend day `incomplete` instead of contributing `price × pre-action shares` unflagged. Plus **F-11**: the "DETECTION POWER" test must mutate `_apply_action`, not a local variable. No decision is required for any of it |
| **W6a** | Price basis — schema column, write seam, `factor_of` injection (D8/D10/D11/**D17**/**D30**) | not started | W1, W2 · **D30** | **`pricing/schema.py`** (not `data_ingestion/schema.py` — F-14), `pricing/store.py`, **`pricing/refresh.py`**, **`scheduler/jobs.py`**, **`api/instrument_service.py`** | **§7.1b's final clause** — a symbol with **no** corporate action stores byte-identically, including the new column — plus the write-seam units §5.1 detail 1 now requires: the cap applied **last, to the product** (F-20's ×20 case gives 2.8333, not 2.8340) and the upsert's `DO UPDATE` restating every basis column (F-23). Boot gate: a **fresh** database migrates and starts. `pricing/` still imports nothing but `pricing.*` (D17 — grep the import block, do not eyeball it) |
| **W4** | `holdings.py` action-aware share path (D23, **D31**) | not started | W2 · **D31** | `data_ingestion/holdings.py`, `data_ingestion/validate.py`, `shared/corporate_actions.py` | **§7.2 in full, including its 2026-08-10 normative clauses** (F-16 — the union enumeration, the internal position map, and the two required fixtures; a `book.holdings` iteration does **not** satisfy it). Plus §6.2's rules — strict per-ledger date bounds · the depth cap degrading per **D31** · one `ActionIndex` per batch · `shares_naive` untouched — and the four `validate.py` repairs F-06/F-07/F-08/F-32. **Re-points W2 (F-33):** E1a.1 and E13 run on the **naive** path until this lands, which is why the second action of a chain is currently uncommittable; this package moves both onto the action-aware path and asserts it |
| **W6b** | Price basis — reconcile + read-path re-expression (**D30**, ~~D18~~ → **D22**) | not started | W6a | the reconcile entry point (invoked on any insert / edit / delete of a SPLIT row; factors injected per D17), `portfolio/dashboard.py` (the `price_map` assignment — the construct, not a line number), `portfolio/timeseries.py`, `.claude/rules/data-and-pricing.md` | **§7.1b in full** (run twice · edit · delete · either order — under D30 these hold by construction, so a red here means the raw column is not what the close was rebuilt from) and **§7.1a's value leg** with both detection-power companions. Plus **§7.1's byte-identical assertion**: an EXCHANGE with `ratio_to != ratio_from` into a `to_symbol` **you already held** leaves every stored price of that symbol byte-identical before and after the action is entered. *(That clause replaces "net-worth continuity for an EXCHANGE with `ratio != 1` as well as a SPLIT (D18)", which demanded the widened scope trap #16 forbids — F-03.)* |
| **W5** | Drawer reconciliation (**D33**) | not started | **W4** | `api/routers/symbol.py`, `web/detail.js`, `api/dashboard_models.py`, `portfolio/dashboard.py`, `tests/golden/dashboard_full.json` | **§7.3's first bullet** — `balances: true` with `diff_shares == 0` for a symbol carrying an action, and a deliberately wrong `corporate_delta` makes it false. The flag chips must reach the wire (F-17), or §6.3's red footer renders with nothing to explain it |
| **W7** | Entry surfaces (§6.7) + the CSV kinds | not started | W2 (incl. E23), W4 | `web/ledger.html`+`ledger.js`, the 賣超 confirm dialog, `web/detail.js`, `api/routers/ledgers.py`, `import_templates.py` **and every other registration point a new CSV kind needs** (F-28) | **§7.6** — each kind entered through the UI produces the asserted position, the `＋公司行動` term and a ✓ 對帳一致 footer, and a previously-blocked sell now validates. Plus D13/D28's multi-account preview and the CSV round-trip header guard. **W7 owns D15's enforcement in practice (F-40):** the importer must call `validate_corporate_action` with the **full batch**, or nothing enforces it |
| **W8** | Stress-audit oracle | not started | W3 | `scripts/stress_audit/oracle.py`, `run_phase1.py` | **§7.4** — `run_all.py --phase 1` → `fail=0`, with the oracle carrying **its own** ratio arithmetic |
| **W9** | Accounting manual | not started | W3, W6b · **D36** | `docs/accounting-formula-manual.md` (zh, authority) + `.en.md` mirror | **§7.5** — including its two limitation rules: D11 stated as standing, **D12 stated as resolved by D36** (not as a permanent blind spot), and cash-and-stock documented as a **hard exclusion** per D34 |
| **W10** | E2E, demo corpus, gates, ship | not started | all · **D37** | `tests/e2e/`, **`scripts/seed_demo.py`**, **`scripts/verify_live.py`**, **`scripts/verify_corporate_actions.py`** (new, §10.5), `CHANGELOG.md`, `shared/whatsnew.py` | **§7.6 + §7.7 + §7.8**, then **§10.5**'s owner-run acceptance script (blocking, and it needs D37's opening cost totals), then `/ship-version` |

**Dependencies, stated once (F-34).** **W3, W4 and W6a are independent** of each other once W2
lands. **W6b follows W6a** (it rebuilds closes from the column W6a adds) and **W5 follows W4** — not
optionally: §6.3 *defines* `corporate_delta` as a difference against W4's walker, so W5 built in
parallel would be a footer over a number that does not exist yet. The earlier line here said
"W4, W5 and W6 are independent … they can proceed in parallel" while W5's own blocker column said
W4; the two contradicted each other on the same screen.

**Why this order, and not W4 → W5 → W6.** Each reason is about where a failure surfaces:

- **W6a first.** It is the only piece of W4/W5/W6 that touches a **schema** and the **boot path**,
  and F-14 shows the plan aimed it at a module that crashes a fresh database — a class of failure
  that surfaces at step 2 of the two-environment loop (the deploy), the most expensive place to find
  it. W6a is also a provable no-op without corporate actions (`DEFAULT '1'`, empty product,
  `factor_of` returning `Decimal(1)`), so it lands early and is then exercised by every subsequent
  demo deploy against D25's accumulating corpus.
- **W4 second.** F-08 is a live blocker: on `HEAD` the **second action of every chain is
  uncommittable**, because `validate.py` walks the naive share path while the replay walks the
  action-aware one — the de-SPAC-then-rename case §6.2 itself cites. Until that is fixed, every
  downstream package is verified against a corpus that stops one action deep, and both D25's demo
  corpus and §10.5's acceptance run are defined against chained ledgers. W4 also owns F-06's repair,
  which W6b's `split_factor` depends on for correctness.
- **W6b third.** The read-path re-expression **moves displayed market value**. Doing it before W5
  means W5's footer is debugged against a screen whose numbers are already right; doing it after
  means every discrepancy has two candidate causes. §5.1's own argument for the `price_map` seam —
  that a disagreement between two numbers on one screen is the worst kind — applies to the build
  order too.
- **W5 last.** It is presentation over two paths that must both already be correct; its dependency
  is W4's *correctness*, not W4's code (a wrong walker yields a footer that is confidently wrong
  rather than red); and it carries the largest undeclared surface, including the golden payload,
  which is cheapest to regenerate **once**, after the price basis has settled.

**One cross-cutting prerequisite: W4 is not done until F-16 is fixed.** §7.2 is this document's
self-declared most valuable test, and §7.3's footer inherits **all** of its detection power from it
(`corporate_delta`'s footer identity reduces algebraically to `shares_action_aware == build_book`).
As written, §7.2 iterates `book.holdings`, which drops every zero-share position — structurally
excluding the EXCHANGE-emptied source, the exact case W4 and W5 exist for.

### 10.3 Verification ladder — run after each package, not at the end

```
W0        pytest  +  git diff --exit-code tests/golden/dashboard_full.json
P0        pytest tests/portfolio tests/api  →  full pytest   (the wiring touches the wire shape)
W1–W5     pytest tests/portfolio tests/unit  →  then bare `mypy`  →  then full pytest
W6a       pytest tests/pricing  +  boot a FRESH database (the migration's own gate, F-14)
W6b       pytest tests/pricing tests/portfolio/test_timeseries.py  →  full pytest
W7        pytest tests/contract tests/api  →  browser click-through of all three doors
W8        .venv/Scripts/python scripts/stress_audit/run_all.py --phase 1   → fail=0
W10       pytest · bare `mypy` · ruff · stress phase 1 · self-review over the diff
```

`mypy` runs **bare**, never `mypy --strict <paths>` — the narrowed invocation reports ~260 false
errors and a narrowed run has previously missed what the full 500+ file run caught.

### 10.4 The traps — each of these has already been found once

An implementer who avoids these will produce a correct implementation. **Ranked by measured blast
radius, re-ranked 2026-08-09** — the first version of this table led with the weaker of the two
ratio defects and did not list the stronger one at all.

| # | If you… | …you have silently broken |
| --- | --- | --- |
| 1 | accept a **non-integer** `ratio_to` / `ratio_from` from the CSV importer or the API | the 賣超 cascade this feature exists to prevent, at **any** share count (measured: 700 × 0.2857 = 199.9900, error ~5×10⁻⁵). The entry form's two integer boxes do NOT cover these paths (§3.1(ii)(b), D14) |
| 2 | write `qty × (to / from)` | share exactness — real, but ~10⁻²⁷ and only in a small-share-count band (3,530 boundary-crossing cases in a 400,000-pair sweep, e.g. `3 × 1/3`). Genuine; just not the one that was undefended (§3.1(ii)(a)) |
| 2a | use `split_factor` on a share count | the same as #2 but unbounded, via a rounded product of quotients (§5.1 detail 2) |
| 3 | apply the price re-expression inside `value_holdings` | `xirr_reporting` keeps the raw price → market value and XIRR disagree by the ratio, on one screen (§5.1(d)) |
| 4 | omit the `target == split_basis` short-circuit | idempotency holds only by rounding luck; E16 schedules the re-run (§5.1 detail 1) |
| 5 | sum `Book.holdings` in the conservation test | the EXCHANGE-emptied source is dropped at `:294`; the test passes for the wrong reason (§7.1a) |
| 6 | sum `Holding.original_cost_total` instead of the raw accumulator | you import the short-cover residue `ε` and chase a pre-existing artifact (§7.1a) |
| 7 | leave `short_proceeds` / `short_shares` untouched on EXCHANGE | `ε` contaminates a position a later buy can reopen (§4.4) |
| 8 | mark the split day `incomplete` | the net-worth chart drops by the position's entire value; `incomplete` omits the **holding**, not the day (§5.1(d)) |
| 9 | forget to deduplicate multi-account rows in `split_factor` | a 3-for-1 held in three accounts becomes 27-for-1 — only for multi-account holders (§5.1 detail 3) |
| 10 | update two of the three event-priority literals | a silently mis-ordered replay; use the `EventPriority` enum (§4.4) |
| 11 | let the oracle import `shared/corporate_actions.py` | the oracle checks the implementation against itself and proves nothing (§7.4) |
| 12 | let `pricing/` import **anything above `shared/`** for the ratio lookup | one-way dependency violation (`architecture.md`). **Reworded 2026-08-09:** the earlier text named only `portfolio/`, so the tempting shortcut — `pricing → data_ingestion` — slipped past its literal wording while breaking its intent. Factors are **injected** (D17); `pricing/refresh.py` imports only `pricing.*` — keep it that way |
| 13 | write the action for some of the accounts holding the symbol | a globally-corrected price against a locally-uncorrected share count, with the drawer printing **✓ 對帳一致** over it. No existing check computes that relationship (D13, E13) |
| 14 | keep the `id` ASC tie-break for same-date actions | a 3× share-count difference decided by typing order, with §7.1a green on both orderings (D15, E12) |
| 15 | let an EXCHANGE land on an `ever_oversold` destination | a discarded cost basis restored as real money and rendered as an ordinary average (D16, E22) |
| 16 | **widen** §5.1's scope to include EXCHANGE (it looks like the obvious fix for the 95% cliff) | the entire price history of any symbol you **already held** before a merger into it — an EXCHANGE adds to its destination, it does not re-denominate it. The cliff's real fix is D19, at the import seam (D22, §5.1 "Scope") |
| 17 | register a broker identifier string as an instrument, **or** write a regex to detect one | the first gives a non-security in `instruments`, a permanently failing quote key, and the event forced onto the EXCHANGE path; the second eventually rejects a legitimate ticker and locks the owner out of their own ledger. The rejection is keyed on **registration**, a database fact (D19/D24, §3.4) |
| 20 | add a validation cycle-check for the `holdings.py` walker | a legitimate re-use of a ticker, blocked to defend against a state D15 already makes unreachable. The walk descends strictly in date; the depth cap is insurance, not the mechanism (D23, §6.2) |
| 21 | build one `ActionIndex` per `shares_through` call | the oversell guard runs once per transaction, so a ~1,400-row import re-reads and re-groups the action ledger ~1,400 times. One index per batch (D23, §6.2) |
| 22 | ship this feature without seeding the demo site | the staged deploy returns green having exercised **none** of it — `engineering-process.md` verifies the tag on the test site first, and that site's ledger has no corporate actions (D25, §7.7) |
| 23 | **cut a separate release for the W0 refactor** — *this trap said the exact opposite until 2026-08-10 (F-02)* | the owner ruled 2026-08-09 that `feat/corporate-actions` is **not merged into `main` until the whole feature is done**, so the branch stays abandonable. A W0-only version means merging an unfinished feature and giving that up. ~~D26~~ is retired; W0 rides the single feature release, and "no number moved" is evidenced by §7.3's golden payload plus the demo deploy of that release |
| 18 | book a cash-and-stock merger as one EXCHANGE | the cash leg booked nowhere at all — and §2.1's conservation law **passes**, because the money left the book (§2.1a). But do not solve it locally either: since D34 the event has **no supported entry form** at all. It is a hard exclusion with a stated reason (§9), not a modelling puzzle to improvise |
| 19 | **follow §9's old two-row cash-and-stock recipe** (SELL + EXCHANGE on the effective date) | `EventPriority` runs CORPORATE_ACTION (10) before SELL (30), so the EXCHANGE zeroes the source and the same-day SELL lands on a zero-share position: `OversellError` strict, **STICKY 賣超 with the basis discarded** on the dashboard path — the disaster §1 exists to prevent, produced by following the spec. The recipe and D20's §7.1a exception are both **withdrawn** (D34, §9, §2.1). *This trap previously said the opposite: that dating the legs apart was the error.* |

### 10.5 What "done" means for the whole feature

The owner's real broker export (`sample-trade-data/`, git-ignored) replays without tripping the
賣超 guard on any of the affected tickers recorded in the private assessment note, with cost basis
intact. That is the acceptance test the feature was commissioned for; every gate above is a proxy
for it.

**It runs as a script, and the script is the deliverable (D27, 2026-08-09).** The data is
git-ignored, so this can never be a CI gate — which is exactly why it must not be left as a
paragraph someone remembers to honour:

```
scripts/verify_corporate_actions.py  --export <path>  --actions <path>
```

| | |
| --- | --- |
| **Committed** | the script. **Not committed:** the export, the ticker list, or any figure it prints. The script takes both paths as arguments and has no defaults, so it cannot accidentally read or embed private data |
| **Output** | one line per affected ticker — `symbol · 賣超 tripped? · original_total intact? · shares reconcile?` — and a final `PASS` / `FAIL n`. Nothing else, so the output can be pasted into a session without leaking amounts |
| **Who runs it** | the **owner**, on their own machine, against their own data |
| **When** | after W10, **before** `/ship-version` — i.e. before anything is tagged, and therefore before the demo site or prod see it |
| **On failure** | **blocking.** Not a note, not a follow-up. The feature exists for this one outcome; shipping it while this fails would be shipping the thing the spec says is broken |

The script also prints the count of corporate-action rows it needed but did not find, so a `FAIL`
distinguishes "the replay is wrong" from "the ledger is still missing rows" — two very different
next actions, and the run is useless if the owner cannot tell them apart.

**The run has one owner-supplied precondition, and it is the programme's longest lead item (D37,
2026-08-10).** Every position whose earliest event in the broker export is a **sell** needs a
per-symbol `original_cost_total` for its opening inventory; the export does not carry one, and
nobody but the owner can supply it. Start collecting it now, not at W10. ⚠ **The shortcut is
refused:** `opening_import.py` validates only `shares > 0`, so a cost total of **0** imports cleanly
and permanently zeroes the position's basis **with no 待釐清 flag** — strictly worse than the
oversell it appears to fix, because the oversell at least announces itself. `original_cost_total > 0`
becomes a **hard validation** in the same change (F-13).
