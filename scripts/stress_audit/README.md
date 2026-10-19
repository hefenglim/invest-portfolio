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
  common.py            # evidence log, Decimal assertions, httpx client, uvicorn launcher (store ON,
                       #   child runs), fact loaders (ledger + the stored closes / FX rows)
  phase1.py            # clean-room scenario + reconciliation (holdings/realized/cash/FX/KPI/XIRR,
                       #   the settlement before every checkpoint, the month-end closes)
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

Phase 1 runs with the **settlement store ON** (`PD_SETTLE=on`, `PD_SETTLE_MODE=child`: every
run in a real child process, as on prod — set by `common.LocalServer`) and settles at every
checkpoint (section 9). `run_phase1.py --wave2-only` skips the checks that need PERF package G
(the store read path through the dashboard, `/api/settlement/periods*`, the period rows of
`/api/snapshots`) — for a run before the wave-3 merge ONLY; the gate never passes it.

The engine-equality oracle (`scripts/perf/engine_equality.py`) refuses a `--db` inside the
repository, so copy the phase-1 database out first (with its `-wal` / `-shm` if present):

```bash
cp scripts/stress_audit/evidence/phase1.db* <scratch>/
.venv/Scripts/python.exe scripts/perf/engine_equality.py --db <scratch>/phase1.db \
    --now 2026-10-19T14:00:00 --out <scratch>/ee   # books + value_series + forced fallback
```

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
| `trend.net_worth` | no | `total_value` + every cash pool's end-of-day balance at that day's rate (YQ3, 2026-10-05) |

Since YQ1/YQ3 (2026-10-05) two questions are kept apart: whether a day is **incomplete**
(`_day_incomplete`) and whether its **value** is honest (`_day_value_judgeable`). A refused
dividend marks every day from its date, but the shares are right, so `total_value` and
`net_worth` are still asserted on that day — the held carrier's value stays in both (Y3). The
family runs whenever no UNDECLARED oversell (sticky 賣超) is held, so it also runs while a
declared short is open (`short_open`); the KPI / XIRR gate is unchanged.

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

### 7. Refused dividends: all three classes, measured (YQ1 / YQ3, 2026-10-05)

**The gap.** The oracle modelled one refusal class — a dividend on an open declared short.
A dividend with no position made it RAISE, and a dividend on a ticker an EXCHANGE vacated was
BOOKED (post-close income for cash, a zero-cost resurrection for a 配股), while the app has
refused both since CR-02 / E24. The only scenario state carrying a refusal (2609's
`short_open`) was also exactly where the trend family was gated off, so Y3 (a held carrier
keeps its value) was never reconciled at all.

**Detection power, measured.** Each line is one deliberate break in the APP, a full phase-1
run (API-only, `run_phase1.py`), then a restore. The baseline it is compared with is
`ops=137 pass=11399 fail=0`.

| break (app) | result | failing families |
| --- | --- | --- |
| ⑪ — a refused payout moves the cash pool; the net-worth cash series counts it; the listing marks every row booked; the trend's book-level marking (K3) removed | `pass=10951 fail=448` | `cash.balance` / `cash.statement.terminal` (+3,000 on tw_broker TWD), `trend.net_worth`, `ledger.div.booked`, `trend.incomplete` on the flat-carrier and orphan days, `kpi.fx_complete_withheld` / `kpi.trading_financing_cost` |
| Y3 — the trend drops a held flagged position; XIRR counts refused payouts; an orphan no longer withholds XIRR | `pass=11150 fail=249` | `trend.total_value` / `trend.net_worth` (incl. `short_open`: the short's −192,000), `kpi.xirr` at every vacated checkpoint, `xirr.withheld_for_orphan_dividend`, `xirr.orphan_reason_names_row` |
| E24 — the replay books a dividend on a vacated ticker | `pass=9827 fail=1572` | `holdings.keyset` (VCO resurrected at 50 shares), `realized.*` (a 3,000 income row), `cash.*`, `kpi.*`, `trend.*`, `ledger.div.booked`, `export.*`, `refused.anchor.*` |

A fourth state is worth naming: the first run of the new scenario failed 4
`kpi.trading_financing_cost` lines that were an ORACLE assumption, not an app bug. B is
withheld when the trend's last day is incomplete, and its third term rides with it (AI-D48).
No KPI checkpoint had ever had an incomplete last day before, so the oracle had never had to
say so. It now does (`kpi.fx_complete_withheld`).

Baseline after this change (`run_all.py --phase 1`, with the UI read-back):
**`ops=137 pass=11406 fail=0`** (was `ops=128 pass=6025 fail=0`).

**`ops` moved 128 → 137 (+9)**: `run_refused_dividends` adds a deposit, a buy, an action, two
E24 dividends, the orphan door probe, the legacy write, the successor's sale and the orphan's
delete. The pass count moved by much more, because the trend family now runs at `short_open`
and at four new checkpoints, and `trend.net_worth` doubles its assertions.

### 8. US equity options (spec `docs/spec/2026-10-09-options-ledger.md`, plan W7a, 2026-10-10)

**What the oracle transcribes from the SPEC** (never from `shared/options.py` or
`portfolio/options.py` — the same §7.4 suspension as the corporate actions, stated in the
oracle's docstring):

| spec | oracle |
| --- | --- |
| §3 OCC symbol, OPT-D22 | `parse_occ` — its own regex: letters-only root, `YYMMDD`, `C`/`P`, strike × 1000 as 8 digits; the display name 「XYZ 2026-12-18 50 Put」 |
| OPT-D1 | `OPTION_MULTIPLIER = 100`; quantities are share-equivalents |
| §4.1 / OPT-D2 | price-0 close rows go through the ordinary branches (a BUY at 0 covers the short → the whole remaining premium; a SELL at 0 of the long → `−basis`); `close_reason` rides on the realized row |
| §4.4 | `_close_refused`: a close that would OPEN OR EXTEND a lot books nothing, creates no position, sets the sticky `unbookable_close` on an existing one, and is recorded on `OracleResult.refused_closes` — which withholds XIRR and B |
| §4.3 / OPT-D11 / D16 | `OPTION_ADJUST` in `_apply_action`, after SPLIT's checks and before E5: the standard-adjustment check (`ratio_from == 1`, strike conserved, same right / expiry), E22, the opposite-direction refusal, the full field table (long AND short lot × `ratio_to`, proceeds carried, flags OR-ed, `vacated_to`) |
| OPT-D6 / §4.2 | `fee_tax(..., is_option=, close_reason=)`: close rows 0 / 0; `option_per_contract` × 口數 (+ reg) + SEC on a sell over the premium notional; no TAF, no tax, no MY stamp; every rule set has a rate (`schwab` 0.65, `moomoo_us` 0.8633 — owner ruling 2026-10-10, no "unset"), and a statement fee wins at the caller |
| §5 / R2 | `option_price_on`: the last ORDINARY trade per symbol across accounts ≤ the valuation day, dated that trade; after an `OPTION_ADJUST` with no trade since, the predecessor's price ÷ `ratio_to` |
| OPT-D4 / R3 | point-in-time figures (KPI 總市值 / 未實現 / 已實現 / 總報酬, XIRR's terminal value, the FX pool's exposure) include an open contract on its SIGNED quantity; the trend excludes its value without marking the day incomplete; `trend_skips_an_option` derives `trend_excludes_value` |
| OPT-D20 | `principal_fx_effect` is `None` and `principal_fx_note` set whenever a contract is open on the valuation day |

**Scenario (`run_options`, placed before `run_refused_dividends`** — that block leaves refused
payouts in the ledger until `final`, which would mark every later trend day incomplete and
withhold B, masking exactly what the options checkpoints must see). Every row goes through a
real door: option-mode manual entry (`option_intent` + 口數), the CSV door (share-equivalents,
statement fees on `moomoo_my`, whose rule set has no per-contract rate), the close-row manual
entry (`close_reason`, side and price derived by the server), and `OPTION_ADJUST` through
`POST /api/ledgers/corporate-actions` (not the DB fixture the equity actions use).

| op | contract | asserts beyond the reconcile families |
| --- | --- | --- |
| STO → BTC | `OPA260918P00045000` | realized literal 217.39 |
| STO → expire | `OPA260821C00055000` | realized literal 79.34 (the whole premium) |
| BTO → STC (CSV, moomoo_my) | `OPB260918C00030000` | realized literal 281.90; supplied fees |
| BTO → expire (long) | `OPB260821P00025000` | realized literal −111.30 (the whole basis) |
| 1 → 3 adjustment on a SHORT, then BTC 1 of 3 | `OPA261218P00060000` → `…P00020000` | 3 contracts, price 3.00 ÷ 3 dated the predecessor's trade, obligation 6,000; then realized 9.13 and the new last price |
| 1:1 rename on a LONG | `OPC261120C00010000` → `OPD261120C00010000` | valued at the predecessor's 1.25 |
| sold-put assignment (two legs) | `OPA260918P00040000` | realized 218.69 (R5: premium stays option P&L); stock leg 200 OPA at 8,000 |
| partial call exercise (two legs), the rest expires | `OPA260918C00042000` | realized −140.65 on the exercise and −140.65 on the expiry (R5); stock leg 100 OPA at 4,200 — OPA ends 300 shares at 12,200 |
| same-day BTC + expiry | `OPB260821P00020000` | 63.695 then 69.345, write order |
| left open past expiry | `OPB260904C00035000` | `past_expiry`, still valued at its last trade (R2) |
| §4.4 refused close (legacy data) | `OPB260918C00030000` BUY 100 `expired` on a flat contract | `refused_closes`, `refused_close`, XIRR withheld + reason names the row, B withheld, trend days from its date incomplete, remedy text; then deleted through the door |
| door probes | — | OPT-D6 (ruling 2026-10-10): a `moomoo_us` BTO / STC with NO statement fee is ACCEPTED at the default rate (literal fees 0.86 / 0.87); OPT-D14 BTC beyond the short (400, nothing written) |
| cash tie per USD pool | schwab · moomoo_my | the statement's option lines = the oracle's net premiums line for line, with the same §6.1 label as the ledger wire |

New families: `options.*` (presence, block / row / realized keys, `open[]` fields, roll-ups,
refused closes, the KPI 「其中選擇權」 decomposition — KPI − the block's part == the oracle's
STOCK part, `check_rel` because it subtracts two 28-digit figures — R3 / OPT-D20 notes),
`options.anchor.*` (literals), `options.option_side.ledger`, `options.cash_tie.*`,
`ledger.tx.close_reason` / `contracts` / `refused_close` / `stock_rows_carry_no_option_marks`,
`kpi.principal_fx_withheld_option_open`, `xirr.withheld_for_refused_close`,
`instrument.asset_type_matches_symbol`, and the integrity rules `trade.price_positive` /
`close_row.well_formed`.

**One oracle assumption corrected on the first run.** The oracle first read §4.4's "XIRR and
B are withheld" as a KPI-only gate; the app also marks every trend day from the refused close's
date incomplete (2 `trend.incomplete` failures). The spec's "the record, like
`orphan_dividends`" points at K3 — a refused dividend marks every day from its date — so the
app's reading is the spec's and the oracle now says the same (`_day_incomplete`).

**Detection power, measured (2026-10-10, API-only `run_phase1.py`, oracle-side breaks, each
restored; on the scenario before the call-exercise op was added).** Clean on that scenario:
`ops=160 pass=18819 fail=0`. The app was not modified: a break in the ORACLE that the app does
not share is the same disagreement a defect in the app would produce, seen from the other side.

| break (oracle) | result | failing families |
| --- | --- | --- |
| §4.4 off (a refused close books a lot) + R3 off (option days counted incomplete) + SEC dropped from the option fee | `pass=18741 fail=78` | §4.4: `options.open.keyset/present`, `options.refused_closes`, `ledger.tx.refused_close`, `kpi.*`, `kpi.xirr`, `fx.reporting_unrealized` (11); R3: `trend.incomplete` (58) + `kpi.fx_complete_withheld` / `trading_financing_cost` (4); SEC: `fee_engine.fee` on the 6 STO rows |
| adjustment valued at the predecessor's price NOT ÷ `ratio_to` + the short lot moved NOT × `ratio_to` | `pass=18573 fail=120` | `options.open.last_price` / `contracts` / `share_equivalents` / `average_premium` / `obligation`, `options.realized.*` and every roll-up, `options.obligation_subtotals`, `kpi.*`, `kpi.xirr`, `fx.reporting_unrealized`, the report's KPI lines — at 7 checkpoints |

**Disclosed limitations.** (1) `share_equivalents`, `premium_total` and `average_premium`
have no sign convention in the spec, so their MAGNITUDE is asserted; `market_value` /
`unrealized` are asserted signed. (2) `by_contract`'s `premium_in` / `premium_out` and its
`opened` / `closed` dates are not asserted (their definition is not in the spec); its
`realized` is. (3) Until W6 lands the realized CSV is compared on its stock rows only — the
check switches to stock + option rows by itself when the CSV gains the spec's `asset_type`
column; the holdings CSV's 選擇權 block is not parsed. (4) A statement line that moves no money
(a price-0 close) is compared through the ledger wire, not the cash tie. (5) No PUT exercise /
CALL assignment (a stock SELL leg) is written — the scenario's two pairs both BUY the
underlying; the oracle needs no branch for the other shape (it is an ordinary stock sell).
(6) The OPT-D17 underlying-price date is this fixture's last seeded close (`ASOF`).

Baseline after this change (`run_all.py --phase 1`, with the UI read-back, same-day A/B on
2026-10-10): **before** (the harness at `7f23c51`) `ops=137 pass=11406 fail=0`; **after**
`ops=163 pass=19252 fail=0`. **`ops` moved 137 → 163 (+26)**: `run_options` adds 18 manual
option entries (6 STO, 3 BTO, 3 BTC, 4 expiries, the exercise pair, the assignment pair), 2 CSV
commits, 2 `OPTION_ADJUST`s, 2 door probes, the legacy refused-close write and its delete.
The pass count moved by much more because the options checkpoints run every family (trend
included) on a ledger with open contracts, and every later checkpoint carries the `options`
block. After W6's exports merged (the realized CSV now carries the option rows, so its family
compares them too) and the final-review fixes, the branch gate on 2026-10-10 read
`ops=163 pass=19727 fail=0`. Owner ruling 2026-10-10 (one default option fee per rule set,
`moomoo_us` 0.8633) turned the OPT-D6 door probe — a moomoo_us BTO with no statement fee,
refused 400 — into two ACCEPTED moomoo_us entries with no statement fee (BTO + STC of a contract
of their own, literal fees 0.86 / 0.87). Same-day A/B, API-only `run_phase1.py`: **before**
(`d208ffa`) `ops=163 pass=19720 fail=0`; **after** `ops=164 pass=19941 fail=0` — the current
baseline (+1 op: one refused probe became two writes; the extra assertions are the new realized
row and its flows, compared at every later checkpoint). With the UI read-back
(`run_all.py --phase 1`) on the merged branch tip the same day: `ops=164 pass=19948 fail=0`.

### 9. The settlement store and the month-end closes (PERF, spec 2026-10-18 §11.2 / §18, 2026-10-19)

**What the spec asks of this harness** (§18 "Stress audit"): phase 1 `fail=0` with the store
on — "the harness runs a settlement after its writes and also asserts the stale state before
it" — and (§18 "Period closes") "the stress-audit oracle gains month-end values and its phase 1
compares them".

**The oracle (`oracle.py` layer 5, transcribed from spec §11.2 — never from `kpis_as_of`,
`period_close.py` or `price_basis.py`).** `period_end(facts, D, closes, fx_rows, reporting)` is
the KPI block "as at D", since inception:

| §11.2 row | oracle |
| --- | --- |
| book | `replay(facts_valued_as_of(facts, D))` — every row from its own date |
| price of a held stock | `price_at`: the latest STORED close with `as_of_date ≤ D`, re-expressed for the splits in `(priced_on, D]` — the read DIVIDES (`× ratio_from / ratio_to`, one action at a time), one per market event (a split recorded in two accounts is one event) |
| a held stock with no such price | out of 總市值 and of the unrealized part, its cost still in the rate's denominator, XIRR withheld, named in `missing_prices` |
| option contract | its last ordinary trade on or before D (`option_prices`, R2) |
| FX | `fx_rate_at`: the latest rate on or before D, direct, else the inverse; with the stored row's date |
| `total_market_value` | Σ price × SIGNED shares (stocks and options), converted at D |
| `by_currency_value` | the native STOCK market value per quote currency (OPT-D5: the currency view is stock-only) |
| `total_return` (A) | Σ_ccy (realized + unrealized), then converted (manual §7.1) |
| `total_return_rate` | A ÷ Σ_ccy `gross_invested` converted (manual §7.1, blended). `gross_invested` is NEW in the replay: an opening and a buy's LONG part add to it; a buy covering a declared short adds nothing (domain-ledger.md: short capital is excluded); nothing ever takes it out |
| `xirr`, window | flows counting by D, terminal at D on SIGNED shares (a declared stock short is a liability), `as_of = D`; withheld for a held 賣超 / unapplied action / orphan dividend / refused close / missing price / missing rate, and below 30 days (manual §7.2.1) |
| notes | `missing_prices`, `stale_prices` (`(D − price date) > 4 days`), `missing_fx`, `stale_fx`, `pending_positions` (賣超 incl. a flat one, unapplied action, refused dividend, refused close), `options_value_excluded` (an option open at some close of `(previous end, D]`), `options_past_expiry`, `dividends_receivable` (non-STOCK rows with `ex_date ≤ D < pay date`) |

The prices and rates are read back from the `prices` / `fx_rates` tables (`common.load_closes`
/ `load_fx_rows`) exactly as the ledger is — inputs, not results. The trend family now reads its
closes the same way (`oracle.price_at`), so a symbol whose series has a hole or starts late is a
fact of the prices table, not of the `PRICES` dict.

**The flow at every checkpoint** (`phase1.reconcile` → `settle`):

1. the fixture's price writes (`_ensure_daily_prices`) — they are writes, so they are changes
   the settlement must take;
2. BEFORE the run, from `GET /api/settlement/status` alone (`settle.*`): before the first
   generation nothing is stale and no day is live-from; afterwards every write since the last run
   marked the store dirty, `dirty_from` is NOT LATER than the earliest day the written rows can
   change (`_change_bound`, from the stored rows before and after — the prefix property's safety
   half; over-firing is allowed), `live_from = min(S + 1, max(dirty_from, W))`, the stale range is
   `[dirty_from, live_from − 1]` when non-empty, the prompt is due exactly when `dirty_from < W`
   and the chip reads 待結算;
3. with the read path (package G): the stale days of the dashboard trend ARE the previous
   generation's values (the oracle over the inputs the last settlement saw), the live days the new
   ones, and every month close ending in the range is flagged `stale` with the previous
   generation's KPIs and no receivable (`stale.*`);
4. `POST /api/settlement/run {scope: "dirty"}` → 202 → poll the status until no run is active and
   the run's report has ended → `published`, the store clean, `live_from = settled_through + 1`,
   `settled_through ≥ final_through`;
5. every family as before, on a settled store, plus:
   * `month.asof.*` — `period_end` at D = TODAY against the LIVE KPI block. §11.2 makes the live
     KPIs and a close ONE function with two price sources, and on or before today the latest
     stored row is the row: this proves the transcription on every build, and reaches figures the
     live family never compared (the rate, `by_currency_value`, the window, the KPI block while a
     declared stock short is open);
   * `store_period.*` — the PUBLISHED generation's `settled_period` / `_holding` / `_cash` rows,
     read straight from SQLite, against the oracle at each month end: package D's computation
     pinned on its own, with or without a route (holdings and cash at D included);
   * `period.*`, `period_detail.*`, `snapshot(s).*` (package G) — the served
     `/api/settlement/periods?kind=M|Y`, `/periods/M/{period}` (its one caller is this audit) and
     `/api/snapshots`: past months `source = store`, not stale, not 暫定, `as_of = D`, each equal
     to the oracle at D; the current month and year live.

**Scenario ops** (`run_phase1.py`):

| op | what it pins |
| --- | --- |
| `run_backdated_edit` (after `checkpoint1`) | one buy moved two days EARLIER on a clean store: `dirty_from` = `stale_from` = the new date exactly, `stale_to = W − 1`, `live_from = W`, the prompt from that day; then the run |
| `run_month_end` `MPX` | held across 2026-02-28 with no close before 03-02: missing price, XIRR withheld, cost in the denominator (`month_end.anchor.missing_price*`) |
| `run_month_end` `SPA` / `SPB` | fixed, as-traded, economically flat weekday series; a 2-for-1 dated Mon 06-01 (the first trading day after month end) and one dated Sun 05-31 (ON a weekend D: the read divides Friday's close). Entering them leaves the May close unchanged (`month_end.split_leaves_close_unchanged`) and the literals 1000 @100 / 1000 @40 (`month_end.anchor.split`); before they are entered, 1000 @100 / 500 @80 |
| `run_month_end` `STL` | closes with a hole before two month ends: 6 days old at 04-30 (stale) and EXACTLY 4 days old at 05-31 (not stale — "more than 4 days"), the boundary no daily-seeded symbol reaches |
| `run_month_end` 2330 CASH | ex-date 04-28, paid 05-15: the April close's `dividends_receivable` |
| `run_live_window` | a buy dated the 15th of the SERVER's last month, then corrected (10 → 12 shares): after each, with NO run, the change is inside the live window (dirty from that day, nothing stale, no prompt, `live_from` = that day) and last month's close and 月度成績 row already equal the oracle over the changed ledger, `source = live` (§11.4 "a correction dated last month changes last month's row at once") |

**Detection power, measured (2026-10-19, `run_phase1.py --wave2-only`, three ORACLE breaks at
once, then restored).** Clean: `ops=217 pass=60006 fail=0`. Broken: `pass=59802 fail=204`.

| break (oracle) | caught by |
| --- | --- |
| the read no longer re-expresses a close carried across a split | `store_period.position.price` / `market_value` / `unrealized` (SPB, May: 80 vs 40), `store_period.total_market_value` / `total_return` / `xirr` / `trend_total_value` / `net_worth`, `trend.total_value` / `trend.net_worth` on 2026-05-31 — 11–12 checkpoints each |
| a cover's shares counted in the rate's denominator | `month.asof.total_return_rate` (12), `store_period.total_return_rate` (56) |
| staleness at 3 days instead of "more than 4" | `store_period.notes.stale_prices` (13: STL at 05-31) |

The third row did NOT fail before `STL` existed: every other close is either seeded daily or
weeks old, so the boundary was unobservable. That is the measurement that added the symbol.

**Disclosed limitations and open questions.**

* **`xirr_window_days` while XIRR is withheld for a 待釐清 ledger — owner question.** Manual
  §7.2.1 says the window is "reported in any case, the rate None included". The app (live
  dashboard and closes alike) reports NONE when XIRR is withheld for a 賣超, an unapplied action,
  a refused close or an orphan dividend, or when a rate is missing: it builds no flow series then.
  Measured here 2026-10-19 (`month.asof.xirr_window_days` at `checkpoint2`, `corp_refused`,
  `options_refused_close`, `refused_orphan`: expected 282, got null). This is v0.1.31 behaviour
  and v0.1.32 must stay byte-identical, so the harness asserts the window only where the reading
  is unambiguous (computed, under 30 days, a missing price, no sign change) until the owner rules.
* **Not reconciled yet:** `period_twr` (needs an independent TWR index), the period ACTIVITY
  lines of §11.3 (realized, dividends, fees, cash movements, FX, openings — and the tax-package
  tie-out), `restated`, the trail / diff pages of a run.
* **Not reachable on this instance:** the scheduler is off, so `final_through` is always
  yesterday — no close is ever 暫定 and `notes.unconfirmed_closes` is always empty (asserted
  empty, never exercised non-empty). No year has ended in the scenario (2026 only), so the only
  `Y` row is the current, live year — "the year row ≡ the December row" is not exercised.
* **PERF-F2 and the price-basis split edge (owner ruling 2026-10-19: fixed after v0.1.32,
  which stays byte-identical to v0.1.31).** Neither shape occurs in this scenario (every XIRR-kind
  cash movement is in a currency a held instrument is quoted in; every fixture close is stamped
  `fetched_at` = its own date). If a later op adds one and the oracle disagrees with the app, it
  is that known, owner-accepted defect — recorded here, never "fixed" in the oracle.
* **One existing check moved to `check_rel`:** `kpi.realized_total`. The TWD realized total sums
  rows carrying 28-digit quotients, and the month-end block's three extra TWD realized rows moved
  it to a magnitude where the two addition orders land 1 ULP apart (…396 vs …397) while every row
  compares exactly in `realized.*`. Association, not money.

Baseline after this change (`run_phase1.py --wave2-only`, API only, 2026-10-19): **`ops=217
pass=60006 fail=0`** (was `ops=164 pass=19941 fail=0`). **`ops` moved +53**: 17 settlement runs
and 16 pre-run status records (one per checkpoint after the first), 4 `month.not_judged` records,
and 16 scenario ops (the back-dated edit; the month-end block's deposit, four buys, four sells,
dividend, close series and two splits; the live-window entry and its correction). The pass count
moved by much more because the store family compares every month close at every checkpoint. The
gate's own baseline is the first full run with package G merged (W4).

## Must-pass assertion families

A run is only green when **all** of these hold (exact Decimal unless noted):

- **Settlement and month-end closes** (README §9) — `settle.*` (the status before the run, the
  run, the clean state after it), `backdated_edit.*`, `live_window.*`, `month_end.*`,
  `month.asof.*` (the month-end oracle at today ≡ the live KPI block), `store_period.*` (the
  published closes, holdings and cash at D), and with package G `stale.*`, `period.*`,
  `period_detail.*`, `snapshot(s).*`. The rate is `check_rel` (a quotient of two 28-digit sums),
  XIRR `check_close`; everything else exact.

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
- **Refused dividends — all three classes** (YQ3, 2026-10-05). `domain-ledger.md` refuses a
  payout with no position by its date (CR-02), one landing on an open declared short, and one
  on a ticker an EXCHANGE vacated (E24, with CR-06's reopen rule). The oracle refuses all
  three from its own reading — `_Pos.vacated_to`, `_reopen`, `_live_successor` — and records
  them on `OracleResult` (`refused_dividends`, `refused_dividend_ids`, `orphan_dividends`).
  A refused row is booked nowhere (no income, no resurrection), moves no cash pool, statement
  line or FX cash (⑪), no `net_invested` and no XIRR flow (K2), marks every trend day from its
  date (K3) — held carrier or flat — while a held carrier keeps its value (Y3). An ORPHAN also
  withholds XIRR and B, and the XIRR reason names the row (`xirr.withheld_for_orphan_dividend`,
  `xirr.orphan_reason_names_row`); B's third term rides with B (`kpi.fx_complete_withheld`).
  The ledger listing marks each refused row (`ledger.div.booked`, X1). Scenario
  (`run_refused_dividends`): the E24 rows go through the REAL dividend door (VCO was held
  before its exchange, so the door accepts them; the replay refuses them) and are reconciled
  with the successor held, then flat, then at `final`; the orphan is a **legacy-data** row —
  every door refuses it since CR-02 (proved first: `refused.orphan_door_refuses`), so it is
  written below the validators (`common.write_legacy_dividend`), reconciled, then deleted
  through the door. The open-short class is the 2609 block.
- **US equity options** (spec 2026-10-09, README §8) — the `options` block against the oracle's
  own replay of contracts (OCC parse, §4.1 close rows, §4.4 refusal, §4.3 `OPTION_ADJUST`,
  §5 last-trade valuation); KPI / XIRR / FX include an open contract on its signed quantity,
  the trend excludes it; OPT-D20; the §6.1 label on the ledger wire and the cash statement;
  the cash tie per USD pool; the option fee branch (OPT-D6) in `fee_engine.*`.
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
