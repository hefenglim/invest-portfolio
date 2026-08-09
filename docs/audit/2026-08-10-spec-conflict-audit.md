# Spec conflict audit — corporate actions, the broker-import backlog, and the grill report

**Date:** 2026-08-10 · **Branch:** `feat/corporate-actions` @ `f80e462` · **Status:** review complete,
no code changed by this audit.

**Documents audited**

| Document | Size | Role |
| --- | --- | --- |
| `docs/spec/2026-08-06-corporate-actions.md` | 1,909 lines | P0, normative, under implementation (W0–W3 landed) |
| `docs/spec/2026-08-06-broker-import-backlog.md` | 170 lines | P1a/P1b/P2/P3, deferred |
| `docs/spec/2026-08-09-corporate-actions-grill.html` | 4,878 lines | the adversarial demo session that produced D13–D28 |

**Method.** Four independent Opus-5 reviews ran in parallel over disjoint scopes — (A) spec vs. grill
traceability, (B) spec vs. implemented code, (C) cross-spec interaction with the broker-import
backlog, (D) adversarial design review of the three unbuilt packages W4/W5/W6. Findings were then
de-duplicated and the severe ones re-verified directly.

**Verification legend**

| Mark | Meaning |
| --- | --- |
| **V** | Re-verified directly during consolidation — file read, grep run, or arithmetic executed |
| **A** | Verified by one review (file read or command run) and not independently re-checked |
| **I** | Inferred from reasoning; no execution proves it |

Three findings were reproduced numerically with the repo's own `pricing.store._cap_dp`; those tables
are reproduced verbatim below.

---

## 0. Headline

**The three specs do not conflict with each other. Every material conflict is between the
corporate-actions spec and itself — and it concentrates in one place.**

§10.2 is the section the spec explicitly describes as "written to be handed to an implementing agent
as its brief" (`spec:1787`). It is also the stalest text in the document. An implementer following it
literally today would:

1. cut and deploy a separate W0 release the owner ruled against (D26 retired, `spec:4-6`);
2. write a net-worth continuity test for `EXCHANGE ratio != 1` that §10.4's own trap #16 forbids and
   §7.1 contradicts (D18 withdrawn by D22);
3. put the `split_basis` migration in a module that does not own the `prices` table, crashing first
   boot on a fresh database.

Separately, and independently of the documents: **on `HEAD` today the feature cannot accept the data
it exists for.** `validate.py`'s E1a hard-rejects the second corporate action of every chain, because
validation walks the action-unaware share path while the replay walks the action-aware one. §10.5
defines "done" as accepting a ledger full of such chains.

---

## 1. Implementation status (corrected)

| Pkg | Status | Evidence | Gap |
| --- | --- | --- | --- |
| **W0** LedgerBundle + registry | **DONE** | `shared/ledger_registry.py:53-77`; `shared/models/ledger.py:81-152`; all four enumerations migrated **(A)** | — |
| **W1** ratio algebra + EventPriority | **DONE** | `shared/corporate_actions.py`; `shared/ledger_events.py:20` **(V)** | — |
| **W2** ledger + validation | **PARTIAL** | DDL `data_ingestion/schema.py:55`; CRUD `store.py:785-928`; every **hard** rejection in `validate.py:252-482` **(A)** | **3 of 6 soft warnings absent: E23, D3, N3-price.** §10.2's W2 done-criterion is literally "every soft warning fires" |
| **W3** replay | **DONE (output unwired)** | `portfolio/cost_basis.py:96-212`; `results.py:50` **(V)** | The flag it emits has zero consumers — F-05 |
| **W4** action-aware `holdings.py` | NOT STARTED | `data_ingestion/holdings.py` is one flat SQL sum, no action import **(A)** | whole package |
| **W5** drawer reconciliation | NOT STARTED | `corporate_delta` has no occurrence outside the spec **(A)** | whole package |
| **W6** E17 price basis | NOT STARTED | `split_basis` has no occurrence outside the spec **(A)** | whole package |
| **W7** entry surfaces + CSV kind | NOT STARTED | `import_templates.py:31` still 4 kinds; no router; zero `corporate`/`公司行動` under `web/` **(V)** | whole package |
| **W8** stress-audit oracle | NOT STARTED | no match under `scripts/stress_audit/` **(A)** | whole package |
| **W9** accounting manual | NOT STARTED | no `公司行動` in either manual **(A)** | whole package |
| **W10** corpus / verify script / E2E / ship | NOT STARTED | `scripts/verify_corporate_actions.py` absent **(A)** | whole package |

**Gates at `f80e462`:** ruff clean, mypy 564 files clean, pytest 2,749 passed,
`tests/golden/dashboard_full.json` unchanged, working tree clean. Corporate-action tests: 118 across
five files, 0 failures **(A)**. The reliability of that green is qualified by root cause 3.

**Operational note.** The demo site currently tracks this branch. It is **safe**: no production code
path can create a `corporate_actions` row — `insert_corporate_action` and `validate_corporate_action`
have zero production callers, there is no router, and `TEMPLATE_KINDS` is still four **(V)**. E17's
price distortion (F-19) therefore cannot fire on a live site before W7. This corrects a review
conclusion that the branch was unsafe to deploy.

---

## 2. Five root causes

Twenty-six findings reduce to five mechanisms. Fixing the mechanism is worth more than fixing the
instances.

### RC-1 — Late decisions reached the decision log and the local section, but not §10.2

Four instances, all verified. In each, the owner's ruling is recorded and the normative body was
updated, while the implementer's brief was not.

| Ruling | Recorded at | Still contradicted at |
| --- | --- | --- |
| **D22** withdraws D18 (price scope stays SPLIT) | `spec:1745`, §5.1 `spec:915-934` | §8's D18 row `spec:1741` carries **no** SUPERSEDED marker, unlike `~~D6~~` at `spec:1728`; §10.2 W6 done-when `spec:1820` still requires the widened test — against trap #16 `spec:1875` and §7.1's byte-identical assertion `spec:1578` **(A)** |
| **D26 retired** (branch stays unmerged) | `spec:4-6`, `spec:22` | §8 `spec:1749` unmarked; §10.2 W0 `spec:1814` "ships as its OWN version"; `spec:1828` an eight-line justification; trap #23 `spec:1880` says *not* shipping separately is the trap **(V)** |
| **D29** E15 hard, before E12 | `spec:539`, `spec:625-650`, `spec:1752` | §6.5's soft list `spec:1345` still names E15, and E15 is absent from §6.5's ordered hard list — the only place the order is specified. `validate.py:371` therefore documents itself as "deviation from the spec's soft warning" **(V)** |
| **D20** §7.1a measures the exchange leg only | `spec:105-109` (§2.1) | §7.1a `spec:1596` still says "the fixture keeps the action alone on its date" and cites only the *two exceptions* note, not the exception to it **(A)** |

**Structural remedy, not four edits.** §10.2 must stop being a parallel narrative. Every "Done when"
clause should reference the §7 test that defines it (`§7.1b`, `§7.2`) rather than restating the
criterion in its own words, and every §8 row that a later decision supersedes must carry the `~~Dn~~`
+ **SUPERSEDED** rendering the document already established for D6. A restated criterion is a second
source of truth, which is the same defect the ledger registry was built to remove from the code.

### RC-2 — "A rule that provably never fires" — four instances, two still live

The document has already corrected this twice (E13's ⚠, then E15 as D29). Two more are live:

- **F-06 — the conflicting-ratio guard iterates `stored` only** (`validate.py:421`, **V**). E15 and E12
  consult `siblings`, E13 consults `batch`; this one consults neither. **D28** mandates that a
  multi-account action is written as one N-row batch (`spec:1751`), where `stored` is empty for every
  row. The guard cannot fire on the door it was written for.
- **F-07 — E13 is blind to a position acquired by a prior action** (`validate.py:233-248`, **V**). The
  candidate set is `transactions UNION opening_inventory`; a position created purely by EXCHANGE or
  SPINOFF appears in neither, so `shares_through` is never even called for it. D13's "must not be
  reachable at all" state (`spec:836`) becomes reachable.

### RC-3 — "A test that cannot fail" — five instances, and they are not independent

| Test | Why it cannot fail |
| --- | --- |
| `test_the_conservation_property_can_actually_fail` (`tests/portfolio/test_corporate_actions.py:364-379`) | Ends `mutated = orig * D("3"); assert mutated != orig` — arithmetic on a local. The real detector is the pinned `assert orig == D("7035")` on the preceding line **(V)** |
| §7.1a's claim at `spec:1626` | Asserts the law catches "a dropped `unbookable_dividend`". Four Decimal sums cannot observe a boolean **(A)** |
| §7.1b clause 1 (`spec:1631`) | Detail 1 (`spec:812-818`) makes reconcile short-circuit when `target == split_basis`. After pass 1 that holds, so pass 2 returns before touching anything — for any implementation **(A)** |
| §7.1b clause 2 (`spec:1633`) | "Assert this fails without `split_basis`" — the superseded algorithm will not exist in the codebase, so the clause degrades to a comment **(A)** |
| **§7.2** (`spec:1641-1644`) — the spec's self-declared most valuable test | The natural implementation iterates `book.holdings`, and `cost_basis.py:456` drops every zero-share position. The **EXCHANGE-emptied source** — the case F-07 and F-08 are about — is structurally excluded **(A)** |

These are coupled. `corporate_delta`'s footer identity reduces algebraically to
`shares_action_aware == build_book`: with `delta := aware − naive` and `net := opening + buy − sell +
reinvest`, `net + delta == book` cancels the `net` buckets entirely **(A)**. §7.3 therefore inherits
**all** of its detection power from §7.2. If §7.2 ships as written, W4 and W5 are both verified by a
test that cannot fail on the case they exist for.

### RC-4 — Validation and replay disagree, because validation walks the naive share path

Three reviews found this independently. Both sites are in `validate.py`, both fixed by the same change,
and §6.2's consumer list (`spec:1163`) predates both.

- **F-08 — E1a hard-blocks the second action of every chain (`validate.py:360`).** Buy `ABC` 2020 →
  `EXCHANGE ABC→XYZ` 2024 → entering `SPLIT XYZ` 2025 calls `shares_through(…, "XYZ", …)`, which reads
  `opening_inventory`/`transactions`/`dividends` for `XYZ` — all empty — returns 0, and E1a raises
  `no_position_on_action_date`, a hard, uncommittable issue. `build_book` handles the identical chain
  correctly **(A, mechanism V)**. This is the de-SPAC-then-rename case §6.2 itself cites.
- **F-07** (above) is the same root in `_accounts_holding_on`.

§6.2's list names two consumers; there are **nine** call sites of the three wrappers **(A)**:
`validate.py:168, :174, :247, :360` · `api/routers/input_center.py:130, :222` ·
`api/routers/instruments.py:56` · `api/routers/strategy.py:170` · `api/signals_service.py:135` ·
`api/dividend_inbox.py:253`.

### RC-5 — W3's outputs are not wired to anything

`unbookable_action` exists on `_Position` (`cost_basis.py:72`), on `Holding` (`results.py:50`), and is
emitted at `cost_basis.py:480` — and stops there. `unbookable_dividend`, its exact analogue, has six
consumers **(V)**.

The load-bearing one is `portfolio/timeseries.py:119`:

```python
if h.oversold or h.unbookable_dividend:
    incomplete = True   # 賣超 / 待釐清 day — value undefined
```

A position whose action was skipped falls straight through. It contributes
`price × pre-action-shares` to the trend and net-worth series as though valid — which is precisely the
wrongness the W3 commit message describes. It is also absent from `dashboard_models.HoldingRow`,
`dashboard.py`'s row construction, `symbol.py::_account_wire`/`_aggregate_position`, and `web/app.js`,
so the drawer cannot show the chip §6.3 relies on to explain its own red footer.

---

## 3. Owner decisions required

Each recommendation states its **system fit** — the existing project rule or mechanism it follows —
because a locally reasonable answer that introduces a sixth way of doing something the system already
does five ways is a net loss.

### D30 — Price basis: one stored column or two? **(blocks W6a)**

The single-column design stores a *re-expressed* close and reconstructs by dividing back out. Two
measured consequences:

**(a) "Order-independent" is false at the stored value (V, reproduced):**

| splits | price | A→B | B→A | one-shot |
| --- | --- | --- | --- | --- |
| (3/1) then (1/7) | 123.4567 | **52.9100** | **52.9101** | 52.9100 |
| (1/3) then (7/2) | 0.0013 | **0.0014** | **0.0015** | 0.0015 |

The `target` product converges; the stored close does not, because detail 1 re-applies `_cap_dp(v, 4)`
on every pass and the orders traverse different intermediates. Row 2 is a 7.7% relative difference and
**neither** order equals the one-shot value. §5.1 claims order-independence at `spec:795-797` and
§7.1b clause 5 tests it — so a correct implementation turns that test red.

**(b) Reconstruction cannot recover as-traded from an already-rounded feed (A, numeric):** a US 7-for-1
against a 2-dp feed reconstructs 100.07 as 100.10; a MY 3-for-1 ETF reconstructs 0.425 as 0.426
(0.24%). §7.1a's continuity assertion has no stated tolerance.

➡ **Recommendation: two columns — keep the raw provider close, define both operations as
`close := raw × target`.**

> **System fit.** `data-and-pricing.md` requires storing "at full source precision", and states the
> 4-dp cap "removes representation noise, not information". A single column violates that on its own
> terms: once the close is a derived value with the source discarded, the noise cap becomes
> information loss. It also brings `prices` under the same discipline the ledgers already obey —
> CLAUDE.md invariant 7, "all reports rebuild from the ledgers" — where nothing authoritative is
> overwritten and every derived figure is recomputed on read. Idempotency, reversibility and
> order-independence then hold **by construction** rather than by rounding luck, and detail 1's
> short-circuit demotes from a correctness requirement to an optimisation. Cost: one TEXT column, on a
> table sized for 1–2 users.

### D31 — Depth-cap degradation: what does the walker return? **(blocks W4)**

D23 requires the cap to "skip the symbol and flag it" on read paths (`spec:1204-1208`). All three
wrappers return a bare `Decimal`; there is no channel for a flag. Six of the nine call sites are read
paths that would silently receive a wrong quantity — including `input_center.py:222` (sell hints) and
`dividend_inbox.py:253` (ex-date entitlement).

➡ **Recommendation: do not introduce `Decimal | None`. Split by path.** Keep the `Decimal` signature
for the read paths and record the capped symbol in a per-request set; validation reads that set and
raises a **`needs_confirm`** issue; the display surfaces it through the same 待釐清 chip path
`unbookable_action` uses once RC-5 is wired.

> **System fit.** The codebase already has exactly one vocabulary for "this number exists but is not
> trustworthy" — `price_stale`, `oversold`, `short_open`, `unbookable_dividend`, `unbookable_action` —
> and one vocabulary for "validation must not guess": `validate.py`'s three tiers. `Decimal | None`
> would be a sixth mechanism forcing nine call sites to each invent a policy, several of which
> (`instruments.py:56` `_held`, `strategy.py:170`) only want a boolean. Reusing the two existing
> mechanisms changes no signature and adds no concept.

### D32 — E24: a dividend on a symbol an EXCHANGE moved away **(a live defect in W3)**

After an EXCHANGE the source position stays in the map with zeroed fields (`cost_basis.py:182-192`,
required so a later buy cannot reopen it carrying `−ε`). A dividend on the old ticker then finds
`existing is not None` and `short_shares == 0`, so neither existing refusal applies **(V)**:

- **CASH / NET** → booked as post-close realized income on a dead ticker (`cost_basis.py:421-434`).
- **DRIP / STOCK** → `existing.shares += reinvest_shares` (`cost_basis.py:444`) **resurrects the
  position** with positive shares and zero basis. `cost_basis.py:456` no longer drops it, so it is
  emitted as a holding at `avg = 0`. It is delisted, so it never gets a price — and `returns.py:155-156`
  returns `rate=None` for the **whole portfolio** on any unpriced holding. Portfolio XIRR goes blank
  indefinitely **(V)**.

Not exotic: US brokers routinely pay a final dividend on or after a merger's effective date, and a
Schwab DRIP arrives as a 3-row group.

➡ **Recommendation: refuse and flag, uniformly for both branches** — raise on the strict path, skip and
flag on the dashboard path. Direct the owner to record the payment as a **cash movement**.

> **System fit.** That is the posture the replay already takes five times (E3, E5, E18, E22, and the
> dividend-on-an-open-short), and "record it as a cash movement instead" is the exact remedy
> `domain-ledger.md` already prescribes for the short case. Redirecting the payout to the destination
> symbol would be a money-of-record movement between two symbols with no conservation analysis behind
> it, and §2.1a already lists "cash leaving the book" as a blind spot the law cannot see. Note the
> narrow scope: an *ordinary* sold-out position keeps today's post-close-income behaviour (audit H2);
> only the action-zeroed state changes, which needs one `_Position` field to distinguish.

### D33 — W5-2: the SQL path applying an action to a negative source **(blocks W5)**

§6.3 states the SQL path "applies every action unconditionally" (`spec:1290-1293`). It has no notion of
`ever_oversold`, so it applies actions to a negative source and manufactures a destination that
`build_book` never created: a symbol with no transaction, no opening, no holding and therefore **no
flag of any kind**, rendering `＋公司行動 −100` under a red 對帳不一致 **(A)**.

➡ **Recommendation: the SQL path skips an action whose source count is `< 0` on the action date, and
flags the symbol.**

> **System fit.** This does not compromise §6.3's independence argument, which is what the
> unconditional rule protects. E3's precondition is a **share-domain** condition the share-only path can
> compute from its own data — unlike E5 (short state) and E18 (destination short), which would require
> importing the replay's model. Skipping on `< 0` costs one comparison and imports nothing.

### D34 — §9's cash-and-stock recipe is unexecutable **(blocks W9's manual text; not on the critical path)**

§9 prescribes two same-day rows: an ordinary **SELL** for the cash leg, an **EXCHANGE** for the
remainder (`spec:1758-1766`). `EventPriority` runs `CORPORATE_ACTION = 10` before `SELL = 30`
(`shared/ledger_events.py`), and the EXCHANGE sets `source.shares = 0` (`cost_basis.py:182`). The SELL
then hits a zero-share position: `OversellError` on the strict path, **STICKY 賣超 with the basis
discarded** on the dashboard path (`cost_basis.py:343-351`) — the exact disaster §1 says this feature
exists to prevent **(V)**.

A second defect rides along: under weighted average the cash leg disposes of `f × N` **shares**, so the
EXCHANGE carries only `(1−f) × N` and the published ratio under-delivers by that factor. The corrected
ratio `B_received / ((1−f) × N)` is generally **not** expressible as two positive integers, which
**D14 requires**.

§9's stated verification — "a 0% cash leg reduces to a plain EXCHANGE and a 100% cash leg to a plain
SELL, so the rule is general" — covers only the two degenerate cases in which one leg does not exist.
Neither can exhibit either defect.

➡ **Recommendation: withdraw the recipe; keep cash-and-stock a hard exclusion, and say why.**

> **System fit.** The two repairs available both dismantle load-bearing rules for a case the spec
> records the owner does not have ("the owner's real de-SPAC events are pure stock", `spec:1770`).
> Widening D14 reintroduces the typed-decimal hazard that §3.1's 2026-08-08 correction removed on
> 3,530 measured boundary-crossing cases. Re-ordering the events breaks the rule that an action is
> effective at the start of its date and that the day's trades are quoted in post-action terms
> (`spec:355`) — the premise every other formula rests on. A limitation stated honestly costs nothing;
> a recipe that produces a discarded cost basis costs the ledger.
>
> If the event ever occurs, the nearest expressible form is EXCHANGE-all-shares followed by a SELL of
> the *destination* (priority 10 then 30 — ordering works), whose realized figure differs from the
> relative-consideration allocation. Record it as an unofficial workaround with that inexactness
> stated, never as a normative recipe.

### D35 — P1b: does a US **cash** dividend reduce `adjusted_total`? **(not blocking; cheap now, expensive late)**

➡ **Recommendation: yes — treat it exactly as TW/MY cash.**

> **System fit.** `CASH_DIVIDEND_TYPES` (`shared/models/enums.py:26`) drives four sites uniformly
> (`cost_basis.py:406`, `returns.py:144`, `cash.py:118,211`, `timeseries.py:94`). Booking US cash as
> income instead would put **two dividend accounting models in one ledger**, so 回本進度 and 股利回收率
> would mean different things per market on the same screen — the "one definition" discipline in
> `domain-ledger.md` exists to prevent precisely that. The backend is already capable
> (`dividend_model.py:55`), so the cost is a `_MODEL_ALLOWED_TYPES` entry and a form control.
>
> **Coupled consequence to decide with it:** under the income model, `dividend_portion` is always 0 for
> US positions, so **D21** — the SPINOFF child's 「承接自母公司」 payback label, already approved and
> scheduled into W7 — becomes unreachable on the only data that has corporate actions.

### D36 — P2b: does anything besides trades reach the return metrics? **(blocks W9's wording only)**

➡ **Recommendation: option 2 — leave XIRR untouched, add a whole-account IRR in the existing
`portfolio/twr.py`.**

> **System fit.** `twr.py` already exists as the home for a whole-account view. Option 3 (redefine
> XIRR) moves every historical figure, invalidates the accounting manual's worked anchors and the
> stress-audit oracle's expectations, and would have to be re-verified against the entire corpus —
> for amounts the backlog itself calls trivial. Option 2 is additive and also resolves **D12**'s
> conceded blind spot: the reorganisation fee is booked as a WITHDRAW and is therefore invisible to
> every current return metric (`spec:267-273`, verified against `returns.py:135-146`).
>
> **Sequencing consequence:** W9 documents D12's limitation as settled. Either decide this before W9,
> or W9 must write "pending P2b" rather than a permanent statement.

### D37 — Owner **input**, not a decision: pre-history opening cost totals

Per-symbol `original_cost_total` for every position whose earliest event in the broker export is a
sell. This is the longest-lead item in the programme, it gates §10.5's blocking acceptance run (D27),
and no one else can supply it.

> ⚠ **Refuse the shortcut explicitly.** `data_ingestion/opening_import.py:126-130` validates only
> `shares > 0`; **a cost total of 0 imports cleanly (A)**. That permanently zeroes the position's basis
> with no 待釐清 flag — strictly worse than the oversell it appears to fix, because the oversell at
> least announces itself. Recommend adding `original_cost_total > 0` as a hard validation in the same
> change, which is an asymmetry repair, not a new mechanism.

---

## 4. Findings register

Severity: **H** = produces a wrong money-of-record number, an unreachable rule, or a shipped
implementation diverging from an owner ruling · **M** = an implementer must guess · **L** = editorial.

### Documentation — propagation failures (RC-1)

| ID | Sev | Location | Finding | Resolution |
| --- | --- | --- | --- | --- |
| F-01 | **H** | `spec:1345` vs `spec:539`, `spec:1752` | E15 hard in §5/§8 (D29), still soft in §6.5 — the only place the order is specified. `validate.py:371` calls itself a spec deviation **(V)** | Move E15 into §6.5's hard list immediately before E12; drop the "(deviation)" comment |
| F-02 | **H** | `spec:1749`, `:1814`, `:1828`, `:1880` vs `spec:4-6` | D26 retired by owner ruling; still mandated in §8, §10.2's W0 row, its justification paragraph, and trap #23 **(V)** | Strike D26 as `~~D26~~` + SUPERSEDED per the D6 precedent; rewrite W0's row; delete or invert trap #23 |
| F-03 | **H** | `spec:1741`, `:1820` vs `spec:1745`, `:915-934`, `:1578`, `:1875` | D18 withdrawn by D22; §8's row unmarked and W6's done-when still mandates the widened test, contradicting §7.1 and trap #16 **(A)** | Mark D18 SUPERSEDED; replace W6's done-when with §7.1's byte-identical assertion |
| F-04 | M | `spec:1596` vs `spec:105-109` | D20's "measure the exchange leg only" landed in §2.1, not in §7.1a — the section it was written to fix **(A)** | Carry the clause into §7.1a; re-derive its measurement point after D34 |

### Implementation — live defects on `HEAD`

| ID | Sev | Location | Finding | Resolution |
| --- | --- | --- | --- | --- |
| F-05 | **H** | `timeseries.py:119`; `dashboard_models.py`; `symbol.py:129,213`; `web/app.js` | `unbookable_action` has zero consumers; a skipped action contributes `price × pre-action shares` to trend and net worth unflagged **(V)** | Wire it exactly where `unbookable_dividend` is wired — six sites |
| F-06 | **H** | `validate.py:421` | Conflicting-ratio guard reads `stored` only; D28 mandates batch entry, so it never fires on the primary door **(V)** | Extend to `stored + siblings`, and compare **quotients** (`to_a × from_b == to_b × from_a`) so `3/1` and `30/10` are accepted as identical rather than rejected as conflicting |
| F-07 | **H** | `validate.py:233-248` | E13's candidate set is `transactions UNION opening_inventory`; an action-acquired position is in neither, so D13's all-accounts rule goes silent **(V)** | Enumerate candidates from the action ledger's `to_symbol` too, and use the action-aware count |
| F-08 | **H** | `validate.py:360` | E1a hard-rejects the second action of every chain — the feature cannot accept its own motivating data **(A, mechanism V)** | W4; §6.2's consumer list must name E1a and E13 as *correctness*, not performance |
| F-09 | **H** | `shared/corporate_actions.py:136-138` + `:143` | Every action is filed under **both** `_by_source` and `_by_dest`; E20 forces `to == from` for a SPLIT, so both keys are identical and a walker merging the lists squares the ratio (3-for-1 → 900 shares on 100) **(V)** | Add `ActionIndex.for_symbol(account, symbol)` returning the merged, de-duplicated, date-ordered stream, so no caller can get it wrong |
| F-10 | **H** | `shared/corporate_actions.py:143` | Split dedup key is `(symbol, date, ratio_to, ratio_from)` — **term-wise**. `3/1` and `30/10` survive as two entries → `split_factor` returns **9**, not 3 **(V)**. Reachable via F-06 | Key on the reduced fraction (`Fraction(to, from)`) plus symbol and date |
| F-11 | **H** | `tests/portfolio/test_corporate_actions.py:364-379` | The "DETECTION POWER" test ends in a tautology on a local variable **(V)** | Replace with a mutation applied to `_apply_action` |
| F-12 | M | `holdings.py:33-35` vs `symbol.py:87` vs `enums.py:26` | Three definitions of "a reinvesting dividend": SQL admits `NET`, `_REINVEST_TYPES` does not, `build_book` treats `NET` as cash. §6.3 asserts `shares_naive` is "exactly today's `_shares_until`" — it is not **(A)** | One definition, in `shared/`; §6.3's sentence corrected |
| F-13 | M | `opening_import.py:126-130` | `original_cost_total = 0` imports cleanly, permanently zeroing basis with no flag **(A)** | Hard-validate `> 0` (see D37) |
| F-47 | **H** | `cost_basis.py:92` + `:456` | **Added 2026-08-10 during P0.** `_reject` raises `unbookable_action` on the SOURCE, and the holdings loop drops any position at zero shares — so when the source is *already empty* the flag is discarded with its carrier and the skipped action leaves no trace at all. Reproduced against the real engine **(V)**: buy 100 AAA, then same-day `EXCHANGE AAA→BBB 2:1` + `SPLIT AAA 3:1` → `holdings = [('BBB', '200', unbookable_action=False)]`, `any flag visible? False`. The dashboard renders a clean 200-share BBB with no warning anywhere. Structurally the E19 laundering one level up: E19 stops a flag being *transferred away*, nothing stops it being *dropped*. Today only E12/E1a keep the state out, and validation reaches no production path (F-40) | **Design decision, not a patch.** Emitting flagged zero-share positions changes the payload and the `shares == 0` drop is load-bearing. The likely-correct model is a **`Book`-level** record of actions the replay could not apply — "an action in the ledger went unapplied" is a property of the replay, and no surviving position is guaranteed to carry it. Decide after the F-05 wiring lands, since it chooses where the flag surfaces |
| F-49 | **H** | `portfolio/pnl.py:13` + `portfolio/dashboard.py:291` | **Added 2026-08-10 during P0 — the brief that produced it was wrong.** F-05 was briefed as "wire `unbookable_action` exactly where `unbookable_dividend` is wired". That instruction is subtly incorrect, and mirroring it leaves the poison in every aggregate. The system has **three** mechanisms for a suspect position, not one: (a) the display flag, (b) `pnl.py:13` nulls `market_value` so "all aggregate code that gates on `market_value is not None` excludes this position automatically" — its own comment, (c) `dashboard.py:291`'s `has_oversold` makes XIRR not computable, with a stated reason. `oversold` uses all three; `unbookable_dividend` correctly uses only (a), **because its shares are right and only the adjusted basis is short one payout, so the market value is genuinely correct**; `unbookable_action` needs all three, because the SHARES THEMSELVES are wrong — and currently has only (a). Measured on a seeded ledger: a flagged holding kept `market_value 600,000` at `weight 0.944`, the KPI band reported `total_market_value 635,600`, and **XIRR came out `0.5717` with `xirr_reason: None`** — presented as trustworthy, computed from a terminal value that is 94% one poisoned row **(V — `pnl.py:13-16` and `dashboard.py:291-293` read directly)** | Add `unbookable_action` to (b) and (c), and **write the (a)-only vs all-three distinction into the code**, because "mirror the other flag" is what produced the gap. For (c), prefer gating on a **`Book`-level** record of unapplied actions rather than `any(h.unbookable_action)` — that one gate then also covers F-47's dropped position and E1's silent skip (`cost_basis.py:120` passes `None` as the position when the source never existed, so nothing is flagged anywhere) |
| F-50 | M | `portfolio/dashboard.py:423` | `ledger_symbols` is built from transaction / dividend / opening symbols and omits corporate-action symbols, so a SPINOFF child that appears in no other ledger gets no price history loaded. Every day after a **correctly booked** spinoff then marks `incomplete` and the trend flattens. Honest rather than fabricated, but wrong-looking, and it will read as a bug in the feature **(A)** | One line. The same omission at `timeseries.py:67`'s `event_dates` is harmless, since an action cannot predate its position's opening |
| F-51 | M | `scripts/stress_audit/oracle.py` | The independent oracle models `unbookable_dividend` (`:365`, `:436`, `:571`, `:600`) and both phases reconcile it against the API — but it does not model corporate actions at all, so `unbookable_action` has no counterpart. `/stress-audit` green is mandatory before shipping a money-of-record change, and it will pass this feature **without exercising it** **(A)** | W8's real scope. Note the oracle must keep its OWN ratio arithmetic and its own `EventPriority` copy — an oracle that imports the engine proves nothing |
| F-52 | M | `web/detail.js` | The drawer consumes only `fully_recovered` and `price_stale`. **Neither** `unbookable_dividend` nor `unbookable_action` is drawn, although both reach the drawer wire. F-17's "red footer with no chips" gap is therefore wider than reported — it predates corporate actions **(A)** | Fix with W5, and note that the pre-existing dividend flag gets fixed for free |
| F-48 | M | `cost_basis.py` strict path | **Added 2026-08-10 during P0.** For a same-date intersecting pair the strict path refuses only ONE of the two orders, and by accident: exchange-first raises because it happens to leave an empty source, split-first is accepted in full. So 重算 does not detect the ambiguity — it detects one arrangement of it looking ill. This makes E12's entry-time rejection the **only** real guard, which strengthens the case for D15 rather than weakening it **(A)** | State it in §5's E12 note so nobody assumes the replay is a backstop |

### Design gaps in the unbuilt packages

| ID | Sev | Location | Finding | Resolution |
| --- | --- | --- | --- | --- |
| F-14 | **H** | `spec:750-753`, §10.2 W6 files | The `split_basis` migration is aimed at `data_ingestion/schema.py`, but `prices` is created by `pricing/schema.py:4` and `bootstrap_db` runs at `app.py:142` **before** `create_pricing_tables` at `:143` → `no such table: prices` on a fresh DB **(V)** | Move the migration into `pricing/schema.py::create_tables`; correct `spec:750` and W6's file list. `TEXT NOT NULL DEFAULT '1'` is legal for `ADD COLUMN` |
| F-15 | **H** | `spec:795-797`, `spec:1631-1640` | Order-independence is false at the stored value; §7.1b tests it, so a correct implementation goes red **(V, table in D30)** | D30 |
| F-16 | **H** | `spec:1641-1644` | §7.2 iterates a collection that structurally excludes the EXCHANGE-emptied source; §7.3 inherits all its power from §7.2 **(A)** | §7.2 must iterate the union of `(account, symbol)` across all four ledgers **plus both action symbols**, assert against the replay's internal position map (as §7.1a rule 1 does), and carry explicit fixtures for the EXCHANGE source and F-18's same-day opening |
| F-17 | **H** | `spec:1283-1295` | §6.3's red-footer explanation assumes a visible flag; `unbookable_action` never reaches the wire, so the drawer renders an unexplained red footer with zero chips **(A)** | W5's file list gains `dashboard_models.py`, `dashboard.py`, and the golden payload |
| F-18 | **H** | `spec:1183-1193` vs `holdings.py:38` | The "strictly before" bound is wrong for `opening_inventory`: `EventPriority.OPENING = 0 < CORPORATE_ACTION = 10`, and D3 rules a same-day opening pre-action. `_shares_until` applies `<` to all three ledgers **(A)** | Three bounds, stated per ledger: `opening <= D`, `transactions/dividends < D`, `actions < D`. The walker cannot be built on `_shares_until` with a shifted `before` |
| F-19 | **H** | `spec:1290-1293` | The SQL path applies actions to a negative source, manufacturing a flagless phantom destination **(A)** | D33 |
| F-20 | M | `pricing/store.py:57` | The order of `_cap_dp` relative to the ratio multiply is unspecified, and the minimal edit picks the wrong one **(V, reproduced)**: `raw = 0.14166666865348816` → `cap(raw)×20 = 2.8340` vs `cap(raw×20) = 2.8333`; at ×3 → `0.4251` vs `0.4250`, the MY sub-RM1 ETF case | State normatively: cap **last**, on the product. Add the ×20 unit test — it fails on the natural implementation |
| F-21 | M | `spec:735-741`, `:756` | Un-adjusting cannot recover as-traded from an already-rounded feed (US 7-for-1: 100.07 → 100.10; MY 3-for-1 ETF: 0.425 → 0.426) and the stated invariant is self-referential, so no test can see it **(A)** | State the limitation beside D11's; give §7.1a's continuity assertion a tolerance of `ratio × storage_ulp`, not exact equality |
| F-22 | M | `spec:1005-1010` | `factor_of: Callable[[str, date], Decimal]` cannot express `target`, which is a window over **two** dates (`as_of_date < a.date <= fetched_at`) **(A)** | Specify that the upper bound is the caller-bound `fetched_at`, or widen the signature so it cannot be misread |
| F-23 | M | `pricing/store.py:54-56` | Adding `split_basis` to the INSERT without adding `split_basis=excluded.split_basis` to `DO UPDATE` leaves a stale basis that corrupts the next reconcile. Silent; only bites on re-fetch **(A)** | Specify both operations' SQL in §5.1 |
| F-24 | M | `symbol.py:222-227, :431-440` | `_reconcile` takes no `conn` and is called eleven times per request; a naive `corporate_delta` rebuilds `ActionIndex` eleven times — D23 rule 2's defect one layer up **(A)** | Thread one per-request index; state it in §6.3 |
| F-25 | M | `spec:1756-1781` | An EXCHANGE whose source and destination are in **different accounts** is not representable (`CorporateAction` carries one `account_id`), and §9 does not list it. The obvious workaround fabricates realized P&L on a non-disposal **(A)** | Add the bullet to §9 with the recipe or the exclusion |
| F-26 | M | `spec:1170-1174` | §6.2 never states a SPINOFF's contribution to its **own source** is zero. The walker is a separate implementation by design, so it cannot inherit `cost_basis.py:210` **(A)** | State it; add the fixture (child correct, parent zeroed looks half-right) |

### Cross-spec (broker-import backlog)

| ID | Sev | Location | Finding | Resolution |
| --- | --- | --- | --- | --- |
| F-27 | **H** | `backlog:42-44` | P1a rule 3 drops paired self-cancelling rows on a **zero-sum check**. A corporate action is a paired out/in group with **zero cash on both legs**, so it passes the safety check and is silently dropped — re-creating the permanent basis loss P0 exists to prevent. Rule 7's hard-error protection does not fire, because rule 3 classified the group first **(I)** | The check must sum **share quantity as well as amount**: `−85 + 255 ≠ 0` protects the split; `−100 + 100 = 0` still drops a genuine journal. One line, before P1a starts |
| F-28 | M | `spec:1360-1366`, `:1821` | W7's file list omits four of the seven registration points a 5th CSV kind needs: the parser module is unnamed, and `api/routers/input_center.py:629,633` and `web/input.js:741` are not mentioned **(A)** | Enumerate all seven in W7. P2a's cash kind needs the same seven — doing both in W7 is cheaper than sequentially |
| F-29 | M | `validate.py:252-258, :456-457` | The corporate-action preview builder needs two things the other four kinds have no precedent for: `batch=` threaded on every row (or E13 rejects every legitimate multi-account import), and `book` hoisted once (or an N-row import replays the whole ledger N times) **(A)** | State both in W7; the second is trap #21's shape for a different object |
| F-30 | M | `spec:547`, `:1745` | E23 is in **no work package**: absent from W2's done-when, absent from `validate.py`, and it is the guard for D19's residual hole that P1a depends on **(V)** | Add to W2's scope (the four-part condition) with the one-click convert-to-SPLIT surfaced in W7 |
| F-31 | M | `spec:1592-1595` (grill Q1) | The grill's second Q1 recommendation — a cross-account **per-share-unit** consistency check in the drawer footer — was adopted nowhere; no occurrence of 單位 in the spec **(A)** | Add it to §6.3's footer, or record in §8 that it was declined. Declining is only defensible once F-32 is closed |
| F-32 | **H** | `store.py:928`; §6.5 | E13 is enforced at insert only. `delete_corporate_action` performs no re-validation and `store.py` has **zero** references to E13 **(V)**. Deleting one row of N leaves `split_factor` unchanged (its dedup key is per-symbol, not per-account), so the global price correction stands while one account's shares go uncorrected — and the drawer footer prints ✓ 對帳一致 | §6.5 must state E13 re-validates on **delete and update**; deleting one row of a `(symbol, date)` set requires deleting the set. Add the test: write N, delete one, assert refusal |
| F-33 | M | `spec:1338-1339`, `:1818` | §10.2 has a **W2 ↔ W4 circular dependency**: W2's E1a.1 and E13 are specified to use §6.2's action-aware path, which W4 delivers, and W4 is blocked by W2 **(A)** | Either move the walker into W2, or state that W2 uses the naive path and add a W4 done-when clause re-pointing both |
| F-34 | M | `spec:1819` vs `:1826` | §10.2 says W4/W5/W6 are independent; W5's own blocker column says W4, and §6.3 makes it real **(A)** | Reword: W3, W4 and W6 are independent; W5 follows W4 |
| F-35 | M | `spec:534` vs `:300`, `:327`, `:1326` | E10 is stated as `to_symbol`-only in the §5 matrix — the table whose header says "every row below gets a test". D24's guard depends on E10 covering `from_symbol`; `validate.py:335` already implements the correct form **(A)** | Amend to "**either** symbol not registered" |
| F-36 | M | `spec:1578` vs `:966-969`, `:1582` | §7.1's price-scope test and its E23 test share one fixture, which §5.1 says E23 must stay **silent** on **(A)** | Split the fixtures; add a companion asserting E23 stays silent on a genuine merger |
| F-37 | M | `spec:463` vs `cost_basis.py:104` | §4.4, labelled "complete NORMATIVE", says `_Position` has **seven** fields, lists **eight** rows, and the class now has **nine**; the `cost_basis.py` docstring claims **eight** and claims every one has a §4.4 rule. `unbookable_action` occurs nowhere in `docs/spec/` **(V)** | Add the row (propagate on EXCHANGE/SPINOFF, unchanged on SPLIT), correct both counts, add a field-count assertion so the table cannot silently fall behind again |
| F-38 | M | `spec:80` vs `:915-921` | §2.1's value leg `Σ shares × price unchanged` is stated unconditionally but holds only for SPLIT after D22; §2.1a, which exists to enumerate blind spots, does not list it **(A)** | Qualify line 80; add the third §2.1a row naming E23 and D19 as mitigations; state E23's post-acknowledgement behaviour |
| F-39 | M | `spec:1626` | §7.1a claims the conservation law catches a dropped `unbookable_dividend`; four Decimal sums cannot **(A)** | Delete the clause; cross-reference §7.1's E19 bullet |
| F-40 | M | `spec:1195-1200`, `store.py:785` | "No cycle detector" is safe only while every insert passes `validate_corporate_action`. `insert_corporate_action` is a plain INSERT with no coupling, and there is **no production caller of either** **(V)** | Record as an explicit W7 obligation: the CSV importer must call `validate_corporate_action` with the full batch, or D15 is unenforced |
| F-41 | L | `spec:1585`, `:1816` | "identifier-shaped" survives in §7.1 and W2 after D24 abolished shape tests; trap #17 says that wording leads to the regex that locks the owner out **(A)** | Reword to "an **unregistered** `from_symbol`" |
| F-42 | L | `spec:769-772`, `:871-873` | `Π{ a.ratio_to / a.ratio_from : … }` as a set-builder over **values** collapses two genuine same-ratio splits on different dates (2-for-1 in 2020 and 2023 → factor 2, not 4). The correct key is stated twice elsewhere **(A)** | Write the product over the dedup key |
| F-43 | L | `spec:364-365` vs `:208-210` | §4's preamble still attributes the 賣超 cascade to the parenthesised quotient — the claim §3.1's correction demoted in favour of the typed decimal **(A)** | Reword to the measured form (3,530 boundary-crossing cases) |
| F-44 | L | `spec:900-905`, ~~`:1832`~~ → `:1820` | `dashboard.py` anchors have drifted ~28 lines (`price_map` is `:236`, `xirr_reporting` `:296`) and the spec instructs editing by line number **(A)**. **Anchor corrected 2026-08-10:** `:1832` was inside the W0 justification paragraph and contains no `dashboard.py` reference; the real target is the W6 file list at `:1820`. Four further drifted anchors were found during the fix (E21 ×2, E1a, §6.5), one of which describes a structure W0 replaced | Anchor on the construct, not the number |
| F-45 | L | `spec:1244`, `symbol.py:428-429` | A symbol whose whole life is two actions gets `acct_ids = ∅` and an empty per-account breakdown while the activity list shows two events **(A)** | One sentence in §6.3's Presentation paragraph |
| F-46 | L | `backlog:135` | The backlog names `export/tax.py` as an inline `quantity × price` site; it has none — its only `*` uses are a keyword-only marker and `r.realized * rate`, an FX conversion of a pre-computed `RealizedRow` field **(V)**. The real set is **13 files, not 5**, making P3's blocker larger than recorded | Correct the backlog's site list. **Correction 2026-08-10, after this audit was first committed:** the register originally said 12 files and omitted **`portfolio/pnl.py`** — which is the single most multiplier-sensitive file in the set, since `:46-48` is where market value, unrealized P&L and capital gain are all formed (`price * h.shares`, `(price − avg) * h.shares`), reached from `dashboard.py:237` via `value_holdings` **(V)**. Found by re-measuring with an AST matcher instead of a name-based grep |

---

## 5. Verified sound (findings that came back negative)

An audit that lists only problems misrepresents the system.

- **The eight `build_book` call sites are exactly eight, and every one passes a `LedgerBundle` (A):**
  `strategy/whatif.py:127` · `portfolio/timeseries.py:113` · `portfolio/dashboard.py:230` ·
  `export/tax.py:67` · `data_ingestion/validate.py:457` · `api/routers/ledgers.py:265` ·
  `api/routers/input_center.py:161` · `api/routers/actions.py:83` (top-bar refresh/recompute — not
  corporate actions, **V**). W0 did what it claimed.
- **P0 does not make P3 (options) harder (A, extended V).** Inline `quantity × price` sites are
  **per-file byte-for-byte identical at merge-base `734833b`, at `f80e462`, and at `adc3977`** — only
  line numbers moved (`timeseries.py:145 → :135`, `whatif.py:186 → :161`). `apply_ratio` operates on a
  quantity and never forms money; `split_factor` is prices-only; `_apply_action` *transfers* totals
  rather than re-deriving them from `qty × price`. W4 and W6 add none either.
  **On the site count, two numbers are both right and neither is a ceiling:** 24 by this audit's
  name-based matcher, 28 by a broader AST match (a `Mult` with a quantity-named and a price-named
  operand). Both are **floors** — the same assumption also appears as a per-share figure times a share
  count (`pnl.py:47-48`, `opening_import.py`), which neither pattern catches. P3 must be scoped by the
  assumption, not by the grep.
- **P1a's unblocking condition is met by the plan (A).** The 5th CSV template kind is specified at
  `spec:1360-1366` with a column list that exactly matches `CorporateAction`. The gap is W7's file
  list (F-28), not the design.
- **A corporate action introduces no XIRR cashflow (A).** `xirr_reporting` has no
  `corporate_actions` parameter; the flow series is built from `opening` + `transactions` +
  `dividends` only (`returns.py:135-146`). §6.6's asserted-unchanged test is correctly specified as
  asserting the **flow list**, not the rate.
- **The STICKY oversell state is recoverable (A).** `ever_oversold` lives on `_Position`, a local of
  `build_book`, and is recomputed on every replay — never persisted. Supplying the missing opening
  inventory later simply clears it on the next 重算. Order matters for *enterability*, not for final
  correctness; the one sequence that destroys data is acknowledging a 賣超 before entering the opening.
- **`corporate_delta` composes across accounts exactly (A).** Actions never cross accounts
  (`CorporateAction` carries one `account_id`; `_apply_action` keys both ends on it), so
  `delta(symbol) = Σ_accounts delta(symbol, account)`. The drawer's aggregate and per-account footers
  stay consistent. (The inability to *express* a cross-account action is F-25, a separate matter.)
- **FX is unaffected by corporate actions (A).** `fx_rates` is keyed `(base, quote, as_of_date)` with
  no instrument; `upsert_fx` caps at 6 dp independently; `split_factor` is symbol-keyed and cannot
  reach a currency pair.
- **The SPINOFF carve conserves by construction (V).** `total − carved`, not `total × (1−c)`
  (`cost_basis.py:197-209`), matching §4.3 and §4.4. Measured over 400,000 random `(total, c)` pairs
  the multiplication form conserves just as exactly — so §4.3's *justification* is not reproducible
  even though its *conclusion* is right. The subtraction is still correct to keep: it is exact
  structurally, not by rounding coincidence.
- **The grill's next-round frontier is closed.** All four deferred questions were asked and answered
  in round 2 (D23, D25, D26, D27); three of the four "uncovered findings" were absorbed into the spec
  verbatim. The fourth is F-04.
- **Three of the four unreachable-rule candidates checked out as genuinely reachable (A):** E19 is not
  swallowed by E5 (the flag survives a covered short), E23 is not swallowed by E10 (a pre-D19
  registered identifier passes E10 — the one place the document anticipated the hazard), and E22/E3
  are disjoint by construction.

---

## 6. Recommended work order

Not §10.2's W4 → W5 → W6. **P0 → decisions → W6a → W4 → W6b → W5 → E23+W7 → W8/W9/W10.**

```
P0   Documentation propagation + live defects            no decision required — start now
     F-01 F-02 F-03 F-04 F-35 F-37 F-41 F-42 F-43 F-44   (spec)
     F-05 F-11                                            (code + test)

P1   Owner decisions D30 D31 D32 D33  (+ D34 D35 D36 when convenient; D37 is long-lead)

P2   W6a  schema column + write seam + factor_of injection
          pricing/schema.py · pricing/store.py · pricing/refresh.py · scheduler/jobs.py
          api/instrument_service.py                        F-14 F-20 F-22 F-23

P3   W4   the walker + the four validate.py repairs + the index accessor
          F-06 F-07 F-08 F-09 F-10 F-12 F-18 F-26 F-32 F-33 · §7.2 rewritten (F-16)

P4   W6b  reconcile + read-path re-expression              F-15 F-21 · dashboard.py · timeseries.py

P5   W5   drawer reconciliation                            F-17 F-19 F-24 F-25 F-31 F-45

P6   E23 (F-30) + W7 entry surfaces + the 5th AND 6th CSV kinds  F-28 F-29 F-40

P7   W8 oracle · W9 manual (after D36) · W10 corpus + verify script + E2E + gates
     → §10.5 owner acceptance run (needs D37) → /ship-version

P8   P1a converter (F-27 first) → the real import          LATER: P3 options
```

**Why W6a first.** It is the only piece in W4/W5/W6 that touches a **schema** and the **boot path**,
and F-14 shows the spec currently aims it at a module that crashes a fresh database. That class of
failure surfaces at step 2 of the two-environment loop — the deploy — which is the most expensive
place to find it. W6a is also a provable no-op without corporate actions (`DEFAULT '1'`, empty
product, `factor_of` returning `Decimal(1)`), so it can ship early and be exercised by every
subsequent demo deploy against D25's accumulating corpus.

**Why W4 second.** F-08 is a live blocker: the second action of every chain is currently
uncommittable, and both D25's demo corpus and §10.5's acceptance run are defined against chained
ledgers. Until `validate.py:247` and `:360` are action-aware, every downstream package is being
verified against a corpus that stops one action deep. W4 also owns F-06's repair, which W6b's
`split_factor` depends on for correctness.

**Why W6b third.** The read-path re-expression moves displayed market value. Doing it before W5 means
W5's footer is debugged against a screen whose numbers are already right; doing it after means every
discrepancy has two candidate causes. §5.1's own argument for the `price_map` seam — that a
disagreement between two numbers on one screen is the worst kind — applies to the build order too.

**Why W5 last.** It is presentation over two paths that must both already be correct, its dependency
is W4's *correctness* rather than W4's code (a wrong walker yields a footer that is confidently wrong
rather than red), and it carries the largest undeclared surface including the golden payload — which
is cheapest to regenerate once, after the price basis has settled.

**One cross-cutting prerequisite.** **W4 is not done until F-16 is fixed.** §7.2 is the spec's
self-declared most valuable test, §7.3's footer inherits all of its detection power from it, and as
written it iterates a collection that structurally excludes the case both packages exist for.

---

## 7. Scope limits of this audit

- No code was changed. All commands run were read-only, plus one bounded `pytest` over the five
  corporate-action test files and two arithmetic reproductions.
- `docs/human_noted/`, `sample-trade-data/` and `broker-statements/` were excluded by instruction —
  they hold real financial data. Every broker-import finding is therefore stated as a capability
  property, never as an observation about a holding.
- F-27 is marked **I**: the converter does not exist and the export was out of bounds, so the failure
  mode is derived from rule 3's stated mechanism rather than observed.
- The `strategy/`, `llm_insight/`, `scheduler/` and `news/` subsystems were examined only where a
  corporate-action integration point reaches them. This is not a whole-system audit; the last of
  those is `docs/audit/full-system-audit-2026-07-25.html`.
