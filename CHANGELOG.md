# Changelog

All notable changes to this project are documented here. Format based on
*Keep a Changelog*; released versions use the heading `## [vMAJOR.MINOR.PATCH] - YYYY-MM-DD`.

**Integrity check** — after any edit to this file, run
`grep -c "^## \[v" CHANGELOG.md`; the count must equal the number of released version
headings. (`## [Unreleased]` is intentionally not counted.)

## [Unreleased]

**AI investment-assistant programme (2026-08-16)** — spec `docs/spec/2026-08-16-ai-assistant.md`.
An inventory pass over the whole AI surface (four closed LLM loops, 34 variables, the four-rule
engine, 13 alert rules) concluded the machinery exists and the gaps are one missing leg,
verification, and a surface. **W1 already shipped (below): three AI-door defects found by
inspection, one of which was writing the wrong tax right now.** Owner rulings — named `AI-D<n>`
so they never collide with the corporate-actions spec's `D<n>` space — **AI-D1** (pure batch
cards, no chat), **AI-D2** (fix the double ma_cross definition, then signal history + backtest;
no new indicators), **AI-D3** (the AI door takes transactions + dividends + cash in ONE prompt),
**AI-D4** (fundamentals: all three of yfinance / Finnhub / Alpha Vantage as key-gated providers,
union-merged with per-field provenance, disagreement kept as two values and never averaged —
**recorded as the scoped exception to the fallback chain in
`.claude/rules/data-and-pricing.md`**), **AI-D5** (the official `on_alert` advice card, in scope,
default-enabled; push stays off), **AI-D6** (prototype on demo first, fuel and verification in
parallel). The TW-only fundamentals gap matters because the real portfolio is US-heavy — a US
card is today structurally weaker than a TW card. **W3 rulings (2026-08-17):** **AI-D13** (hybrid
mount — capability + key gate via `supports()`/`capable_ids`, never the fallback chain),
**AI-D14** (**no merge layer — one block per source**; refines AI-D4's mechanism, not its red
line: different values are reported side by side with their sources, never averaged — enforced
by the prompt, and the rules file was amended to match), **AI-D15** (a canonical 8-field
intersection; TW's `finmind` block is mapped from the existing valuation snapshot, never
double-fetched), **AI-D16** (yfinance + Finnhub daily; Alpha Vantage Saturday, held symbols
only — its free quota cannot survive a full-universe pass). **W4 rulings (2026-08-18):**
**AI-D17** (the union schema is a discriminated union — `rows: list[TxnDraft|DivDraft|CashDraft]`
keyed on a `kind` literal, plus an `unparsed` list so the model confesses the rows it cannot
classify — FX / corporate actions / options — instead of silently dropping them; silent drops
are exactly the ledger-distortion AI-D3 exists to kill), **AI-D18** (preview and commit go
through the three EXISTING doors — the union drafts are grouped by kind, rendered to each
kind's canonical CSV, and built by each kind's whole-file builder, with the cash pool injected
by the router as a required argument; no new endpoint, no new writer, undo granularity and the
row↔line invariant unchanged), **AI-D19** (prompt v6: three explicit sections + the cash-kind
vocabulary joined from `cash_kinds.py` at module level so it can never drift; `daytrade` finally
teaches its semantics — only on explicit 當沖 wording, the debt W1 deliberately left; and
`short_sale` arrives with its prompt rule — only on explicit 放空/融券/short wording — exactly as
the column comment said it would), **AI-D20** (a synthetic text corpus under
`tests/golden/ai_extraction/` + a manual live runner `scripts/ai_extraction_eval.py` producing
field-level hit rates with the cash-`kind` / `daytrade` / `short_sale` mislabel rates listed
separately — the only fields that move money silently; thresholds calibrated to the first
baseline run; screenshot cases go through the owner's manual review, never into the repo), and
**AI-D21** (the preview renders three sections — transactions / dividends / cash — with the cash
rows showing the Chinese kind label + an explicit ± sign, server-sourced from `CASH_KIND_ZH`).
**W5 rulings (2026-08-19):** **AI-D22** (benchmark = a fixed server-side market map —
TW→`0050`, US→`sp500` — looked up from the instrument's market; MY has no benchmark and stays
honestly unscorable; the LLM never picks the yardstick, and `^KLSE` is a noted cheap follow-up
with a yfinance suffix-routing catch), **AI-D23** (excess return compares both legs in their
LOCAL currencies — per market the two legs share a currency by construction; converting both to
TWD would cancel to the same answer while adding FX-data drop points, so `convert_closes` is
deliberately NOT used), **AI-D24** (`vol_change_pct = vol_30d(due) / vol_30d(create) − 1` —
fixed 30-trading-day windows of the same `annualized_volatility` estimator the alert inputs use,
over the split-re-expressed close series; a zero baseline or insufficient history degrades to an
honest None, never a miss), **AI-D25** (volatility gets its own flat band, ±5% — the shared
±0.5% band would make nearly every `direction=flat` volatility call an automatic miss, since a
30-day estimator jitters by several percent on a constant series; price_change/relative keep
±0.5%), and **AI-D26** (scope: per-symbol wiring + the scoreboard's status-rendering fix;
portfolio-scope scoring stays an honest None for W7). **W6 rulings (2026-08-20):** **AI-D27**
(signal history comes from REPLAY backfill + scan incremental — the rules engine is a pure,
params-stamped function, so re-evaluating each historical date through the same `_read_series`
assembly deterministically rebuilds the row the scan would have written that day; the scan fills
the missing date SET (`price_dates − stored as_of`), so a later deeper price backfill, a filled
provider gap, or an aborted first backfill all self-heal on the next scan), **AI-D28** (each
`signal_history` row is the full daily state vector — four rule states + scores, tech_score,
evaluation_context, params_version — keyed `(symbol, as_of)` on the PRICE-DATA date, not the scan
date, so a holiday re-scan overwrites the same row idempotently), **AI-D29** (event-study events
= rule score-sign changes with hold semantics + composite tech_score band crossings; the
daily-observation conditional distribution was rejected — 200 days of one trend are 200
autocorrelated fake samples), **AI-D30** (forward windows +20/+60/+120 TRADING days against the
SAME symbol's unconditional distribution as baseline, local currency, split-re-expressed
(AI-D23/W6c discipline); guards: n<8 answers 「不足以判斷」 with no numbers, overlapping events
annotated, right-censored events excluded per window and counted, NOTHING annualized), **AI-D31**
(the two stub variables keep their DECLARED meaning — `backtest_json`/`calibration_gap_json`
light up as AI self-calibration from `insight_evaluations`, data that exists today (the design
mock prompts anchor confidence caps on them); the event study rides a NEW per-symbol
`signal_backtest_json`; official template prompts untouched), and **AI-D32** (the composite event
thresholds are the EXISTING 65/35 state bands (`composite.py` `_BAND_HIGH/_BAND_LOW`) — one
vocabulary; a second 70/30 vocabulary on the same prompt surface would be the AI-D2
two-definitions defect all over). **W7 rulings (2026-08-21):** **AI-D33** (the advice template
advances to v3 citing the three W6 variables — verbatim numbers only, an n<8 event cell is
called sample-insufficient and cites nothing, every citation carries the sample count and the
same-window baseline — plus the confidence-anchoring law the design mock always intended:
confidence ≤ the matching bucket's actual hit rate + 5, capped at 70 when that bucket has
n<8, and a negative `calibration_gap_json` lowers it by that magnitude; the checkup template
(v2.6) cites the per-symbol event study too; the InsightCard schema is untouched and there is
deliberately NO code-side confidence clamp — a validator silently rewriting the model's stated
confidence is the same defect class as averaging two providers' fundamentals), **AI-D34** (the
portfolio-scope half of "the assistant, complete" is the EXISTING weekly report, v2.1→v2.2,
citing `backtest_json` + `calibration_gap_json` — the AI narrates its own track record weekly;
no new template, preset, or task type), **AI-D35** (portfolio-scope `price_change` predictions
are now SCORED — the `symbol=None` arm of `_measure_actual` measures the create→due window via
the existing `twr_index` chain, flow-adjusted so a mid-window deposit never reads as profit,
in TWD (the cards are narrated against the TWD dashboard) under the shared ±0.5% flat band;
`relative`/`volatility` stay per-symbol — a blended three-market benchmark is a separate
ruling), **AI-D36** (the scoreboard becomes a decision-quality dashboard with NO new route:
`/api/ai-score` gains a rolling calibration gap sharing ONE definition with the prompt
variable, per-combo sample-gate display, and a backend-computed trust tier — 樣本不足 (n<8,
the MIN_SAMPLE anchor) / 早期 / 可參考 (success ≥ 0.6 AND calibration error ≤ 10pp, the
`gap_alert_pp` anchor), with narrative-only combos judged on `1 − miss_rate` rather than a
fabricated 0% quant rate — plus `calibration_bins` moves to Decimal ROUND_HALF_UP, retiring
the float/HALF_EVEN path that shared a prompt surface with W6's ROUND_HALF_UP), and **AI-D37**
(template upgrades finally REACH existing installs: `POST /api/strategy-prompts/from-template`
gains an explicit replace mode — same route, no new endpoint — and the settings page shows a
per-strategy 同步官方 button when the name matches an official template and the body differs;
tasks bind strategies by id, so the overwrite upgrades every bound task in place; a renamed or
archived strategy answers 409, so a replayed request cannot overwrite a row the user moved).
**W7.1 ruling (2026-08-23), after the first live run on the demo:** **AI-D38** — the machinery
was right and the OUTPUT was not, so the fix goes on both sides. Measured over 13 advice cards
and 13 checkup cards: **0 of 13 obeyed the confidence-anchoring law** (40–70 against a 22.14 /
5.00 cap); two printed a sub-gate cell's same-window BASELINE as if it were that signal's event
return; one fabricated an event mean outright for an `insufficient` cell; and one batch rendered
the same fraction four ways (`0.1336%`, `0.0640 USD`, a bare `0.1053`, and a correct `+9.61%`).
Every producer was correct in every case — the sub-gate cells emitted no numbers, the baseline
arithmetic matched an independent oracle 12/12, and the law reached the model with its real bins
interpolated — so what moves is where the SEMANTICS live: `signal_backtest_json` now carries a
`units` block (the unit previously existed only in the variable registry's `desc`, which the
model never sees), the citation law states the unit, forbids substituting the baseline for a
missing event mean, and forbids any number absent from the input, and the anchoring law hands
over ONE precomputed integer (`backtest_json.confidence_ceiling`) instead of a three-step walk
over a bins table. **AI-D33's red line is unchanged** — nothing clamps the model's stated
confidence; it is simply no longer asked to derive the cap mid-generation. `calibration_gap_json`
also gains a plain-language `reading` (an extension beyond the ruling's letter, reported as
such): the first v2.2 weekly card read gap −0.466 as 「低估自身表現」 — the exact opposite — with
the convention stated in the same prompt section, and a signed fraction is one negation away
from asserting the reverse of the truth. The ceiling has **no floor by design**: on the demo's
record it computes to 0 ("your track record supports asserting nothing"), which the prompt
states in words rather than having this layer invent a floor the owner never ruled.

**Investment-logic review → the repair programme (2026-08-24).** A four-lens review of the whole
branch (cost/return correctness · tax filing · multi-currency · decision layer) returned 14
findings, 13 of which I re-verified against the code line by line, correcting two of the review's
own characterisations. **The verdict: single-currency accounting is solid, the multi-currency layer
is not — and almost nothing is a broken formula. Nearly every finding is a correct number
answering a different question than its label claims.** Only ONE finding is a regression this
branch introduced (`confidence_ceiling`, W7.1); the other thirteen predate W1. Owner rulings
**AI-D39** (the ceiling double-counts one error — delete the gap subtraction, replace the
absent-bucket 70 with `overall_hit_rate + 5`, and record violation rates instead of clamping),
**AI-D40** (`is_etf` becomes three-state: never guess, mark unknown, ask), **AI-D41** (total
return is presented as A · B · B−A — asset P&L, FX-complete P&L, and the principal-FX effect
between them — never summed), **AI-D42** (the three cash-movement kinds that are COSTS OF
TRADING AND FINANCING — `REBATE` / `INTEREST_EXPENSE` / `BROKER_FEE` — enter XIRR; capital
movements and idle-cash interest do not), **AI-D43** (`convert_closes` IS permitted for the
benchmark counterfactual — AI-D23 parked it for the scoring legs, where both sides are
same-currency; here FX is the substance).

⚠ **AI-D42 reverses the owner's own earlier ruling `D1 = A` (2026-08-13)**, which made "cash
movements never enter XIRR" an explicit rule and re-confirmed it for the three kinds added
with the broker importer. Neither the four-lens review nor my first framing of the choice
surfaced that conflict — it turned up while writing the manual entry, and the owner ruled
again knowing it. The second ruling also drew a sharper line than the option offered: idle-cash
`INTEREST` is EXCLUDED, because the principal earning it was never in XIRR's denominator, so
crediting its income to the numerator is asymmetric. `D12`'s 重組費 is booked as a `WITHDRAW`,
so that named standing limitation is untouched. The superseded text stays in the manual,
annotated — why a decision was once made the other way is part of the record.

The programme stays on `feat/corporate-actions` — not merged, not tagged, `__version__` 0.1.28.
### Fixed — investment-logic review, wave R1 (entry, display and export; no ledger figure moves)

Five defects that all shared one shape: **the arithmetic was right and the question was wrong.**
None of them changes a number the ledger already holds — R1 touches only what gets computed at
the door, shown on a screen, or written into an export.

- **A TW ETF's first manual trade was taxed at three times the correct rate** (AI-D40). The
  2026-07-15 stress audit made the instrument registry authoritative for `is_etf` at both fee
  seams and left a comment at each saying so — but nothing guarded the value that *seeds* the
  registry. `quick_register` defaulted `is_etf=False` and the manual door's auto-registration
  never passed the argument at all, so a symbol first seen by trading it was permanently
  recorded as not-an-ETF: 現股 0.3% instead of ETF 0.1%, with `tax_rate: 0.003` written into
  `fee_rule_snapshot` as though it had been established. On a NT$110,000 sell that is NT$220.
  The fix is the third state, not a better guess — `instruments.etf_flag_unknown` (additive
  column; existing rows migrate in as *known*, and nothing reaches back to relabel them). The
  engine still computes with False because a number has to come out, but raises a soft
  `etf_flag_unknown` issue **only where the answer moves money** (a TW SELL whose rule set
  prices the two differently — a buy carries no TW transaction tax, and noise is how a real
  warning gets clicked through). Answering the flag on the instruments form clears the marker.
  `fees.resolve_etf_flag` is the one definition both write doors read, so the bulk door can
  never ship a weaker guard than the single-row form. `scripts/audit_etf_flags.py` (READ-ONLY)
  lists TW rows that may need a human answer, ranked by whether the flag has already priced a
  real sell — it reports and stops, because a script that "corrects" a ledger by guessing is
  the same defect wearing a repair badge.
  ⚠ The review's proposed fix — "pass one argument" — was under-specified: the manual body has
  no `is_etf` field to forward (`input_center.py` says so in a comment), which is why the third
  state had to be persisted rather than plumbed.
- **The sell preview invented a realized P&L for trades the ledger books nothing for.**
  `_position_preview`'s docstring claimed it "replicates build_book's OWN sell arithmetic
  exactly … bit-for-bit". True of the ORDINARY branch it was written against, and only that
  one: `build_book` has since grown the declared short (a sell realizes only the long portion;
  the remainder opens a short lot) and the acked oversell (basis discarded, **no** realized
  row). Measured: **+2,000 shown on a short extension and +9,000 on an undeclared oversell,
  where the ledger books nothing**, and 9,000 shown where a declared partial books 6,000 —
  while the holdings column two rows above honestly printed 「—」. The drawer's 試算 gave a
  *third* answer for the same trade (+3,000), because its `held_adj_avg` collapses to 0 on a
  negative share count, making the whole proceeds look like profit. The preview now mirrors the
  replay's **branches**, and a parametrised contract test replays each hypothetical through
  `build_book` and demands agreement — so a fourth branch fails on its own. The drawer, which
  has no 放空 declaration to read, states the fork instead of picking one.
  The same seam's BUY side is fixed with it: a buy against an open short **covers** it (owner
  rule 2026-07-31) rather than averaging the short's proceeds into a long lot, and an exact
  cover now reads 「已回補」 instead of dividing by zero into a blank card. The comment claiming
  "qty > 0 guaranteed, so never a zero divisor" was false and is gone.
- **回本進度 could print a negative percentage on a position that had recovered 30% of its
  cost.** `domain-ledger.md` already requires every ratio over the basis to divide by
  `abs(cost_total)`, and `unrealized_pct` six lines away obeys it with a comment explaining
  why; `payback_ratio` predates the declared-short work and was never revisited. An open short
  contributes a NEGATIVE basis, so the cross-account aggregate's signed sum shrinks or flips
  the denominator.
- **The tax package declared the year's dividends twice** (AI-D41's sibling). Its realized
  subtotal was the *performance* figure — proceeds minus the **adjusted** cost, which the
  locked 2026-06-06 model has already reduced by cash dividends — printed next to a dividends
  sheet declaring those same dividends as income. `original_cost_removed` was already a column;
  nothing subtotalled it. The sheet now leads with `realized_original` (the filing figure) and
  keeps `realized_adjusted` beside it, labelled, for reconciliation against the dashboard.
  ⚠ The review's worked example for this one was wrong — the post-close-dividend row it cited
  is explicitly filtered at `export/tax.py`. The conclusion survived the example.
- **Every broker-imported row carried an empty `fee_rule_snapshot`.** `csv_import` only
  snapshots a fee or tax it had to compute, and a broker statement supplies both (the fee
  verbatim, the tax a literal `"0"`), so the auto-fill branch never fired and `{}` was
  indistinguishable from "no rule was applied". 重算 never needed it — `cost_basis.py` reads
  `ev.fees`/`ev.tax` off the row — which is exactly why the silence went unnoticed: nothing
  computed wrong, the provenance just was not there. Supplied rows now record
  `{"engine": "supplied", …}`; a partially auto-filled row names which of the two numbers came
  from the caller. `broker/convert.py`'s refusal to recompute is deliberate and unchanged.
  ⚠ A second review claim here did not survive: 重算 does **not** depend on the snapshot, so
  this is a provenance gap, not a correctness one.
### Changed — investment-logic review, wave R2 (the return figures; KPI and XIRR values move)

Three defects in what the headline numbers MEAN. Unlike R1 these move figures the owner has
already seen, so the stress-audit oracle was extended first and the accounting manual states
each formula with its verification anchor before the code landed.

- **A payment the ledger refuses was still an inflow to XIRR.** A cash dividend landing on an
  open short is not representable here (a short seller pays the dividend in lieu and this
  ledger has no debit row for it), so the replay skips it and flags the position. But
  `xirr_reporting` read the raw dividend ledger, so ONE payment got THREE answers on three
  screens: excluded from 總報酬, counted by XIRR, and the trend flagging the whole day 待釐清.
  `Book` now reports `refused_dividends` — the ROWS, not just the existing per-position flag,
  because a flag cannot say *which* payment and excluding by position would over-correct by
  dropping that symbol's other, perfectly bookable dividends. Matched by value rather than
  object identity, since the caller may re-load the bundle in between.
- **The headline profit contained no FX on the principal** (AI-D41). `total_return` is
  `Σ_ccy (realized + unrealized) × today's spot` — the rate reaches each currency's GAIN and
  never its PRINCIPAL — while the trend's `total_value − net_invested` (every flow at its own
  trade-date rate) is the FX-complete lifetime figure and was labelled 「浮動損益」. Both are
  now presented, with the honest names: **A** 資產損益（不含本金匯率）, **B** 含匯兌總損益, and
  **B − A** 本金匯率效果 — which is exactly the content of the 換匯損益 card.
  ⚠ They are shown side by side and never summed: adding the 換匯損益 figure to A double-counts
  the cross term `(MV − C)(spot − acq)`. That is the red line `domain-ledger.md` already drew
  for XIRR, applied to the figure the rule did not cover — and manual invariant **I5 is
  corrected accordingly**: "総報酬 already embeds FX" is true of XIRR and of the trend, and
  false of `total_return`. `total_return`'s own definition is UNCHANGED, so every golden
  payload, stored snapshot and export still reconciles; the golden gains two keys and no value
  moves. B is computed in `portfolio/dashboard.py` because that is the only layer holding both
  the `ReturnSummary` and the `TrendSeries` — neither module could see the other's figure,
  which is precisely why the discrepancy survived this long. It degrades to null when the last
  trend day is `incomplete` rather than falling back to an earlier day: a B measured on a
  different date than A makes B − A a plausible-looking wrong number.
- **77% of every TW commission reached no return metric** (AI-D42). `xirr_reporting` had no
  `cash_movements` parameter at all, so FE-D1's charge-first rebate — 0.229% of capital per
  round trip, and the owner's normal billing — plus every broker fee and all margin interest
  were invisible to every return figure the app computes. The three kinds that are **costs of
  trading and financing** (`REBATE` / `INTEREST_EXPENSE` / `BROKER_FEE`) now enter the flow
  series, signed by the shared `cash_kinds` table so a movement can never be a credit here and
  a debit in the pool balance.
  ⚠ **This reverses the owner's own ruling `D1 = A` (2026-08-13)**; see the rulings note above.
  Idle-cash `INTEREST` is excluded — the principal earning it never entered this metric's
  denominator, so crediting its yield to the numerator is asymmetric — and capital movements
  stay out because admitting them would quietly redefine XIRR as an account return. D12's
  重組費 is booked as a `WITHDRAW`, so that standing limitation is untouched.

Both new parameters are **required keyword arguments with no default**, deliberately: a
default would let a caller silently ship the pre-2026-08-24 behaviour, and both omissions are
invisible in the output — a plausible rate, quietly missing flows. Forgetting either is now a
mypy error and a `TypeError`.
### Changed — investment-logic review, wave R3 (the decision layer)

- **The confidence ceiling charged one error twice, and rewarded having no record** (AI-D39).
  This is the only finding in the whole review that THIS BRANCH introduced: W7.1 implemented
  AI-D33's three-step law faithfully and the law itself was wrong in two ways.
  **(a)** A bucket's cap is `actual_pct + 5`, which IS the calibration correction; subtracting
  the global rolling gap on top charged the same over-confidence a second time — measured on
  the demo, bins said 39, the gap took 47, the answer was 0. That 0 was arithmetic, not
  evidence. The step is gone and so is the `gap` parameter: a parameter kept after it stops
  being read is how the next reader concludes it still matters.
  **(b)** `CEILING_NO_DATA = 70` capped an unscored bucket at 70 while a bucket measured at
  40% capped at 45, so a model could raise its own ceiling by claiming a confidence in a range
  it had never been scored in — and over a 20-sample rolling window that is a limit cycle, not
  a calibration loop. An unscored bucket now falls back to `overall_hit_rate + 5`; the constant
  survives only for "nothing has EVER been scored", which is a genuinely different state.
  ⚠ **AI-D33's red line is untouched** — nothing clamps the model's stated confidence. Only
  the integer handed to the prompt changes.
  ⚠ **Consequence worth naming:** with the gap step gone the ceiling can no longer reach 0
  (`CEILING_HEADROOM` puts the floor at 5), so the advice template's 「confidence_ceiling 為 0
  時…」 branch became text the model reads and can never act on. Rewritten to key on a LOW
  ceiling, which is reachable; the test that pinned the old clause now pins its ABSENCE. Dead
  text in a prompt is worse than dead code. `LIBRARY_VERSION` → `official-v17`.
  ⚠ The helper takes a PERCENTAGE while the wire field carries a FRACTION — converted once at
  the call site, with the unit in the parameter name. Mixing those silently is exactly the
  defect W7.1 shipped (one score rendered four different ways).
- **The R5 degradation gate had never fired in production**, and not because of a judgement
  call: `RunInputs.unavailable_vars` was threaded end to end — `RunInputs` → `GateContext` →
  R5 — and populated at **none** of `insight_service`'s four construction sites. A run whose
  every external variable came back `{"unavailable": true}` was recorded as clean, so the one
  signal saying "this batch was synthesised from absent inputs" never reached `job_runs`. The
  fix is a READ, not a new mechanism: the producers already emit that shape and `variables.py`
  already documents it as the contract. R5 is a per-RUN gate while unavailability is
  per-symbol-per-token, so the reduction is a **union** — if any symbol in the batch was built
  without its news / consensus / fundamentals, the run was degraded. An intersection would
  report a clean run whenever a single symbol happened to have complete data.

- **「持平」 was nearly unhittable, so the scoreboard was training the assistant out of saying
  it** (review ⑩). Over a 14-day horizon at 30% annualised volatility the one-sigma move is
  ~7%, so a fixed ±0.5% band gave `direction=flat` roughly a 6% chance of scoring a hit while
  `up` and `down` sat near 51% each — and `price_change` is the only metric the official cards
  emit, so that bias applied to almost every scored prediction. AI-D25 had already diagnosed
  the identical problem for the volatility metric and widened its band; the same reasoning was
  never carried across. The band is now `0.5 × the symbol's own one-sigma move over the actual
  horizon`, floored at the old ±0.5% — roughly 38 / 31 / 31 instead of 6 / 51 / 51.
  Three choices worth stating: the horizon is counted in **trading days from the price series
  itself** (`annualized_volatility` annualises with 252, so a calendar-day horizon would be a
  silent units error, and counting closes is exact through holidays); the baseline volatility
  is the one **at create**, which is what the model could actually have seen; and `relative`
  deliberately keeps the fixed band, because an excess return has a different variance from
  the raw move and scaling it by the symbol's own vol would overstate it.
  The band travels **with the measurement** rather than being derived inside `scoring`, so it
  can be recorded alongside the verdict — a stored hit whose threshold is unknowable is not
  evidence. Legacy rows and any seam that cannot compute a band fall back to the fixed one.
  ⚠ `ActualMeasurement` also gains `extra="forbid"`: while adding the field a typo in its name
  was silently DROPPED by Pydantic's default and three new tests went green against a
  measurement that did not contain what they asserted. This model is the input to a scored,
  permanently stored verdict — an unknown field must fail loudly.

- **The evidence windows outran the horizon they were cited to support** (review ⑪). The
  advice card hands the model `signal_backtest_json` to back a DIRECTIONAL claim, and that
  claim is then graded over the card's own prediction horizon — 14 CALENDAR days on the
  official template. The study only ever measured **+20/+60/+120 TRADING days**, so the
  assistant cited a +20-session distribution in support of a 14-day call: two different
  questions, one quietly substituted for the other. `FORWARD_WINDOWS` now LEADS with **+10**
  (≈14 calendar days); the longer windows all stay, because 「and then what?」 is a real and
  separate question. The scoring contract is untouched.
  Worth naming: right-censoring is monotone in window length, so the aligned window is also
  the **best-sampled** one — the question the card is graded on is the one the data can
  answer most often, not least. Three prompt bodies described the old window set verbatim;
  leaving them would have made the instruction and the payload disagree, which is the same
  defect class as AI-D2's two `ma_cross` definitions. `LIBRARY_VERSION` → `official-v18`,
  持倉建議 v3.2 / 個股健檢 v2.8. The unreleased v0.1.29 what's-new entry also still
  advertised the gap-based auto-adjustment that AI-D39 had already removed — corrected.

- **The signals said 「資料基準＝今天」 over data that was days old** (review ⑬). `signal_history`
  had always stamped its rows with the PRICE date. `signal_states` and the `/api/signals` wire
  did not — both used `now.date()`. So on any day a provider had not delivered, the drawer and
  `rule_signals_json` announced today as the basis, and the prompt 守則 (「資料基準 {{as_of}} —
  在卡首標注基準日」) faithfully copied that wrong date onto the card. **The golden fixture was
  itself an instance:** `GOLDEN_NOW` is 2026-06-11 while its newest close is 2026-06-09, and a
  contract test had been pinning the wall-clock answer since it was written. `as_of` is now the
  DATA date, `null` when a symbol has no prices at all (an honest gap beats a confident wrong
  date), and `evaluated_at` keeps the wall clock — the two fields were always two facts.
  ⚠ This restores the third return value of `_read_series` that W7 step 0(b) removed as dead
  code. It WAS dead then; it is load-bearing now, and the docstring says so, because otherwise
  the next dead-code pass removes it again. It returns a named dataclass rather than a bare
  tuple so the third element identifies itself at every call site. `to_wire`'s `price_as_of`
  has **no default** — the injection rule: forgetting it is a mypy error and a `TypeError`,
  never a silent fallback to the wall clock, which is the defect the parameter exists to close.
  There is also no scan-wide `as_of` any more: each symbol is stamped with its own last close,
  so a market that has not delivered cannot borrow a fresher market's basis date.
- **`freshness_json` covered prices and FX while four templates told the model to check
  everything against it** (review ⑬, second half). It now carries a `sources` block — one row
  per fed external variable with its `last_as_of`, `age_days`, and whether it degraded —
  derived from the payloads the router **already built** (every producer stamps `last_as_of`),
  so it costs no extra query. `FreshnessReport` itself is untouched: it is a pure function of
  ledger + prices + FX and has no business knowing about news or fundamentals; the extension
  rides the router-fed `VarContext` convention that `fx_rates` established.
  ⚠ Deliberately **no invented staleness verdict**. A price has a market calendar to be judged
  against; a fundamentals snapshot does not, and picking a 「good for N days」 threshold would be
  exactly the guess this project forbids everywhere else. The date is reported; the judgement
  belongs in the prompt, and the four 守則 now say so.

- **Nothing recorded whether the model obeyed its own confidence ceiling** (AI-D38 step three).
  Steps (a) and (b) fixed the ceiling's arithmetic; the accountability half was still missing,
  and a rule with no measurement is a suggestion — W7.1's first live run had 0 of 13 cards obey
  a comparable one. `insights` gains `ceiling_at_create`, stamped at generation from the SAME
  `backtest_json` payload the prompt rendered (so the recorded number and the number the model
  saw cannot disagree), and `/api/ai-score` gains a `ceiling_violations` block rendered as a
  sixth scoreboard card.
  The column has to live on the CARD, not the evaluation: an `insight_evaluations` row is
  written a horizon later, while the ceiling is derived from calibration bins that keep moving
  — unrecoverable after the fact. `price_at_create` (M4) is the exact precedent, migration and
  all. Population (owner ruling 2026-08-26): every non-shadow card with both a stated
  confidence and a recorded ceiling, **scored or not** — a violation is a fact about the moment
  of creation, and gating it on evaluation would lag the number by a full horizon and would
  permanently exclude every card that ends `undetermined`. A card with **no** recorded ceiling
  is outside the population rather than compliant; counting those as obedient would flatter the
  rate with rows the rule never applied to. An empty population reports `rate: null`, because
  0% compliance and 「no comparable card yet」 are different facts.
  ⚠ **AI-D33's red line, restated in a test:** a violating card is stored with the confidence
  THE MODEL STATED. Measuring obedience must not slide into enforcing it — a validator quietly
  rewriting the model's own stated confidence would be the same class of defect as averaging
  two providers' PE into a number neither reported.
### Added — investment-logic review, wave R4 (benchmark counterfactual)

- **「同一筆錢、同樣的日期，買指數會是多少？」** — the review named this the single
  highest-value missing capability, and the reason is that without it every return figure in
  the app is unanchored: XIRR 15% is excellent or dismal depending entirely on what the market
  did over the same period, and nothing here answered that. New pure module
  `portfolio/benchmark_counterfactual.py` spends the portfolio's own reporting-currency flow
  stream on each market's index at each flow's own date, and values the units at the last
  close. Landed as **added fields on `/api/dashboard`** — no new route, so the stress-audit
  `ops` invariant is untouched.
  **Scope: lifetime, one number** (owner ruling 2026-08-26). The windowed comparison is
  already answered by `twr.build_overlay`; a second, differently-scoped counterfactual would
  be two answers to one question (the AI-D2 defect class), and a windowed version additionally
  needs a 「what is the opening capital at the window start」 convention — cost? market value?
  they differ — that nothing else in the app requires.
  ⚠ **The excess is measured against B, never against A.** `total_return` (A) applies FX to
  each currency's gain and never to its principal (AI-D41); the counterfactual buys its units
  with reporting-currency money at each flow's own trade-date rate, exactly as
  `trend.net_invested` does. Subtracting the counterfactual from A would contrast two
  different treatments of the principal's FX and attribute the difference to skill.
  ⚠ **A market with no index is NAMED, not dropped.** MY has no benchmark (AI-D22), and a flow
  can also predate an index's stored history or land on a non-positive close. All of those
  count toward `uncovered_ratio` and, where applicable, `uncovered_markets`. Silently omitting
  them would compare a three-market portfolio against a two-market counterfactual and call the
  gap alpha. The KPI line degrades its own label accordingly — 「差額（部分涵蓋）」 plus a
  plain-language note — rather than printing a bare 「超額報酬」; same discipline as
  `covered_ratio` (F2).
  ⚠ **Nothing computable → `available=false` with a reason, never a zero.** A zero reads as
  「the index went nowhere」, which is the opposite of 「we cannot tell」.
- **One definition of 「money put in」, extracted rather than duplicated.**
  `timeseries.build_reporting_flows` is now shared by the trend's `net_invested` and by the
  counterfactual. Two copies would let the portfolio and its counterfactual quietly disagree
  about which flows exist, and the entire comparison rests on them being the same money on the
  same dates. Each flow now carries its market (routing) and symbol (so a per-symbol view can
  filter without rebuilding); the trend ignores both.
  ⚠ **Asymmetry recorded, not silently fixed:** these flows exclude cash movements, as they
  always have, while `xirr_reporting` has taken REBATE / INTEREST_EXPENSE / BROKER_FEE since
  AI-D42. So XIRR and B now disagree about those three kinds, and they sit side by side on one
  KPI band. B's definition predates AI-D42 and moving it is an owner ruling of the same weight,
  not a refactor — flagged here rather than changed.
- `tests/golden/dashboard_full.json` gains the block and **nothing else**: 15 insertions, 0
  deletions. The golden fixture seeds no benchmark prices, so its snapshot records the honest
  degradation — which means the golden file alone never exercises the happy path, hence a
  dedicated integration file that seeds a real index series.
  ⚠ `test_a_ledger_with_no_corporate_action_takes_exactly_the_pre_change_branches` guards that
  the corporate-action feature did not widen the LEDGER symbol set. Benchmark reads are not
  ledger symbols, so the two sets are now asserted separately, with the benchmark keys
  enumerated from the registry — widening the list wholesale would have quietly retired a
  guard that is still doing its job.

### Added — investment-logic review, wave R5 (two portfolio-level risks)

- **A portfolio could fall 25% with nothing firing.** `drawdown_from_peak` is per-SYMBOL,
  against each name's own 52-week high, so a diversified book — the normal case — can drop a
  quarter with no single holding down far enough to trip it. New **`portfolio_drawdown`** rule
  reads the daily total-value series the trend already builds. Same two-severity shape as its
  per-symbol sibling (risk at the knob, warn at half), threshold in `rules_config` like every
  other rule.
  ⚠ **`incomplete` days are skipped.** A trend point is flagged incomplete when a held symbol
  had no price that day, and its `total_value` collapses accordingly — counting it would turn
  a missing quote into a 「組合回撤 100%」 risk alert. The test asserts the same series with and
  without the flag, so the skip is proven to be what makes the difference.
  ⚠ **Naming is substance here, not wording.** Two switches both reading 「回撤」 on the settings
  page, the notification list and the rule comments would be the AI-D2 two-definitions defect.
  They are 「高點回撤」 (per-symbol) and 「組合整體回撤」 (whole book), and the settings copy states
  the difference outright.
- **The currency axis had no rule at all.** For a three-currency investor the largest
  undiversified bet is usually the currency mix, not any one stock; `single_weight` and
  `sector_weight` could not see it. New **`currency_weight`** rule, defaulting looser than
  sector (0.70 vs 0.60) because a three-currency book concentrates naturally and a threshold
  that fires constantly is one nobody reads.
  ⚠ `CombinedView.by_currency_value` is **native**, so ranking or summing across currencies is
  meaningless arithmetic — 10,000 USD and 300,000 TWD are 31 : 300 natively and roughly
  half-and-half in fact. `by_currency_reporting` is added and accumulated in the SAME loop as
  the native leg and the blended total, so the three can never disagree.
- Both rules guard a non-positive denominator: a net-short book can carry a non-positive total
  value or peak, and a ratio over one flips every sign — the audit-H1 trap. Silence beats a
  table of inverted percentages.
- ⚠ A new strategy rule must also enter `ops/notify.RULE_CATALOG` — a drift guard asserts the
  subset, and a rule with no subscription entry is a rule nobody can be told about.
- The golden payload gains `by_currency_reporting` and nothing else: 5 insertions, 0 deletions.

### Added — investment-logic review, wave R6 (the ex-dividend date)

- **A 配股 was worth −9% for a month, once a year.** The `dividends` ledger carried ONE date.
  For a TW stock dividend the two that matter are ~30 days apart: the share price adjusts on
  the **ex-date**, the shares arrive on the **payment date**. With only the payment date
  recorded the replay held the OLD share count against the ALREADY-ADJUSTED price for that
  whole gap, so a 10% 配股 read as a ~9% loss on every TW holding that pays one.
  `dividends.ex_date` is added **NULLABLE, no default**: every existing row migrates in as
  「unknown」, never as a guessed date, and a row with NULL replays **byte-identically** — the
  regression test pinning that is the load-bearing one, because this touches money of record.
  `date` is now pinned to mean the PAYMENT date.
  ⚠ **Only STOCK moves**, and the rule is about what you actually own on a given day.
  **STOCK** is an entitlement that attaches on the ex-date — you own the shares from then and
  the quote says so. **DRIP** is a PURCHASE made when the cash lands; those shares do not exist
  earlier. **CASH / NET** keeps the payment date deliberately: the dip between ex-date and
  payment is HONEST — the position really is worth less and you really have not been paid, and
  unlike the stock case nothing about what you own changed.
  ⚠ The rule lives in ONE property (`Dividend.effective_date`) because three filters must agree
  — `build_book`'s event ordering, `LedgerBundle.through` and `before_action_on`. `through`
  matters most: it filters `dividends` by date, so without it the event is not even IN the
  bundle on the days between ex and payment, and the ordering change alone would do nothing.
- **The provider door was already discarding the answer.** `dividend_inbox` computes
  `div_date = p.pay_date or p.ex_date` — it had both dates in hand and kept one. All four
  inserts now pass `ex_date` through, which is what makes TW 配股 work with no user action.
  The other three doors take it too: the dividend CSV gains an optional `ex_date` column, the
  AI door gains the field plus a prompt rule forbidding an inferred one (a guessed ex-date
  moves shares to the wrong day, which is the error this exists to remove), and the manual form
  shows the input **only for 配股** — offering it on a cash payout invites filling a value the
  ledger deliberately ignores.
  ⚠ **A defect this nearly introduced:** `broker/convert.py` writes its header from
  `template_columns()` but built its rows by hand. Adding a column to `DIVIDEND_COLUMNS`
  without extending the row would have emitted 10 headers over 9 values, misaligning every
  field after the gap — silently, into the ledger. Both that writer and the AI door's
  `_div_csv` are fixed, and the broker one now asserts its row width against the template so
  the next column addition fails loudly instead.
  The broker converter itself supplies **blank**: a statement's distribution line carries the
  payment date, not the ex-date, and inventing one would break its 「supplied verbatim, never
  recomputed」 discipline.

### Added — investment-logic review, wave R7 (three owner rulings: AI-D48…D51)

- **B took the three cash kinds XIRR already had** (AI-D48). AI-D42 moved `xirr_reporting`
  onto `REBATE` / `INTEREST_EXPENSE` / `BROKER_FEE` and left B (含匯兌總損益) behind — but **A
  and B are printed side by side on one KPI band** (AI-D41) and their difference is labelled
  「本金匯率效果」. From the moment one of them counted a 77% rebate and the other did not, that
  label was false: the difference was 「本金匯率效果 ＋ 三類資金收支」. R4 then made it
  consequential — the benchmark comparison's `excess` is measured against B, so a broker fee B
  could not see was being reported as beating the index. `build_reporting_flows` now takes
  `cash_movements` as a **required keyword argument with no default**: a default would let a
  caller silently ship the pre-ruling figure, which is a plausible number quietly missing
  flows. `DEPOSIT` / `WITHDRAW` / `OPENING` (capital) and `INTEREST` (idle-cash yield whose
  principal never entered the figure) stay out, for AI-D42's reasons unchanged.
  Sign: a fee RAISES `net_invested` exactly as a buy-side fee always has; a rebate lowers it.
  ⚠ **The ruling immediately reproduced its own defect one field over**, and that is fixed in
  the same change: `principal_fx_effect` was computed as `B − A` and labelled 「本金匯率效果」
  on the KPI band, so the moment B counted a broker fee that label was carrying a cost. The
  decomposition is now THREE terms — `B = A + principal_fx_effect + trading_financing_cost` —
  with the cost subtracted out rather than left as a residual that quietly absorbs whatever
  joins B next. `trading_financing_cost` carries the P&L sign (a fee is negative) and renders
  beside the FX effect. A / B are still never summed (I5); these three are.
- **The index leg does not pay your broker** (AI-D49). A cash-movement flow carries
  `market=None`, and that is load-bearing rather than a missing value: `counterfactual()`
  leaves those flows out of **both** legs, so the costs are charged to the portfolio and never
  placed on the index — which is what 「同一筆錢、同樣的日期，買指數會是多少」 literally means.
  They are also kept out of `uncovered_ratio`, whose meaning is 「how much of the money has no
  index to compare against」 (MY today), not 「money that was never going to buy an index」.
  Rejected: charging both legs (invents a cost no index investor paid — the same red line as
  never averaging two providers' numbers) and keeping a second securities-only B′ (two nearly
  identical 「含匯兌總損益」 on one site is the AI-D2 defect class).
- **The stress harness gained a point-in-time family** (AI-D51). Every reconciliation compared
  the app and the oracle **at the current state**, where anything whose effect is confined to a
  window between two dates is invisible — R6's ex-date exactly (the position at `as_of` is
  identical either way), which is why that run's `fail=0` was silent about it. The oracle gained
  `facts_through` / `replay_through` / `net_invested_through` (dividends cut on their EFFECTIVE
  date, so an oracle that cut on the payment date could not have seen R6 either), and phase 1
  gained `trend.*`: `start` · `contiguous_days` · `net_invested` · `incomplete` ·
  `total_value`, sampled at **every ledger event date and the day before it** — dividends
  contributing BOTH the ex-date and the payment date — plus `ASOF` and the last point.
  ★ **Its detection power is measured, not asserted.** With the app moved onto the three cash
  kinds and the oracle not yet, the run came back `fail=27` — every one of them
  `trend.net_invested`, on exactly the dates carrying a cash movement, with nothing else in the
  run disturbed. Green again once the oracle followed: `ops=128` (unchanged — no scenario
  change), `pass` up by ~860 assertions.
  ⚠ **A point-in-time per-symbol SHARE COUNT is still not compared**, and that is R6's actual
  落點. No app surface answers it (`trend.points` carries four portfolio-level numbers; the sell
  preview's date-awareness lives in the 賣超 guard, not a report), and `total_value` cannot
  stand in because it is portfolio-wide while the fixture seeds one close per symbol at `ASOF`.
  Closing it needs a daily price fixture seeded BEFORE the corporate actions — see
  `scripts/stress_audit/README.md` §6, which now states what was closed and what was not.
- `trend`'s start date now uses `Dividend.effective_date` like the other three filters. Latent
  rather than live — a 配股's ex-date cannot precede the position that earns it — but three
  date filters spelling one rule three ways is how two of them drift, and this was the third.

### Fixed — the KPI band pushed the dashboard sideways from 761px to 860px

- **The breakpoint ladder had a gap, the third of this exact shape in `styles.css`.** The KPI
  band drops to TWO columns at `<=860px`, but 「2-col KPIs clip, never push」
  (`min-width: 0; overflow: hidden`) only began at `<=760px`. Between those two widths every
  card was a grid item with `min-width: auto`, unable to shrink below its own min-content, and
  the whole document scrolled horizontally — measured at 768px: `scrollWidth` 881 against a 768
  viewport, a hero card 398px wide in a 250px track. The guard now starts at the same
  breakpoint as the layout that needs it. Prior gaps of the same shape: 861–1023px (the KPI
  band again) and 761–1435px (the 資料來源 table).
  ⚠ **This was NOT introduced by this wave**, and finding that out was the point: the failure
  appeared beside new work on the same card, and a `git stash push web/` bisect returned the
  identical 881px with every web change removed. It also means the 「3834/3834」 recorded for the
  R5/R6 commit was an over-read — counting pytest's result characters proves a run FINISHED,
  not that it passed, because `F` and `E` are result characters too (LESSONS_LEARNED).
- The AI-D48 cost term sits on its OWN subline rather than as a third segment of the
  含匯兌總損益 line (which is what re-broke 768px while the gap above was still open), and a
  ZERO is not rendered at all — an account that has never paid a rebate, margin interest or
  broker fee has nothing to disclose there. `tests/e2e/test_kpi_trading_cost_layout.py` pins
  both: the golden fixture books only `DEPOSIT`, so the non-zero path is exercised through a
  canned `/api/dashboard` rather than by seeding money into the spec-17 payload.

### Fixed — the export centre was congratulating the user (AI-D50)

- **Five buttons that reported success and did nothing.** The 設定 → 匯出中心 rendered five
  cards with a ⬇ glyph and a 「產生並下載」 button whose handler fired `toast('已排入產生佇列',
  'ok')` — there is no queue — and downloaded nothing. All five endpoints (`holdings` ·
  `ledgers` · `llm-usage` · `job-runs` · `tax-package`) had existed for months while the
  tooltip still said 「等待 export endpoint」. Every card now goes through the same backend
  reconciliation channel as the per-page 匯出 CSV buttons.
  ⚠ **A button that reports success and does nothing is worse than a missing one**, and it is
  invisible to a wiring sweep that asks only whether a control has a listener — v0.1.26 swept
  2,287 controls and found 0 dead. `tests/contract/test_export_endpoints_have_callers.py` is
  the guard that would have caught it: every `POST /api/export/*` route must have a real
  frontend caller, and the 匯出中心 must call the download seam.
  ⚠ It found a **second** orphan on its first run: `POST /api/export/ledgers`, the whole-ledger
  zip, reachable only by curl. The first version of the test missed it because the same path
  has a GET (「what would the zip contain?」) whose literal satisfied a substring search — GET
  calls are now stripped before the search.
- **年度稅務包 is reachable, and its year menu is derived.** R1 added the 申報用原始成本 column
  (`realized_original` + its per-currency subtotal — the number that goes on a return) to a
  package no page could ask for. A 稅務包 control now sits in the 帳本頁 export row beside the
  ledger exports (owner ruling AI-D50; NOT the dashboard's 已實現損益 panel, which has no year
  concept). The year options span 「最早一筆帳本」..今年 from `GET /api/db-stats` and default to
  **去年** — a filing is filed for the year that closed, and defaulting to 今年 hands over an
  authoritative-looking part-year package. ONE definition
  (`web/export.js::fillTaxYearSelect`), shared with the 匯出中心, whose own copy was a
  hard-coded `['2026','2025','2024']`.

### Fixed — R2 follow-up

- **The 含匯兌總損益 line vanished instead of explaining itself** (owner ruling 2026-08-25). B
  is absent on any day a held symbol lacks a price, which is not rare, and a row that silently
  disappears is indistinguishable from a feature that was never built. `fx_complete_return` now
  returns an outcome (value + reason) in the same shape and for the same reason as
  `XirrOutcome`, and the KPI band renders 「—」 plus the server's wording. The reason states two
  things, and the second is the load-bearing one: why an earlier complete day is **not**
  substituted — a B measured on a different date than A turns B − A into a plausible-looking
  wrong number, which is the failure mode this decomposition exists to prevent.
### Added
- **AI 投資助手 — the prototype (W2).** The assistant's first surface: position-advice cards
  that synthesize the owner's holding × technicals × the rule-signal engine × news × analyst
  consensus into **建議與提點** — conditional evaluation scenarios with trigger conditions,
  never a position size or an order (AI-D8).
  - **「持倉建議與提點」** — a new `per_symbol` official preset, scheduled daily after the US
    close and **enabled on creation** like every other pack member (AI-D12). Prediction is left
    **free** (AI-D10): the card schema already requires a confidence whenever a prediction is
    present, so "the data does not support a call" is a legitimate answer, not a forced number.
  - **「持倉提點」** — the alert-triggered companion (AI-D5). When one of the six risk-alert
    rules fires (AI-D11: 目標價 / 單一標的過重 / 匯率漂移 / 回撤 / 波動突升 / 共識下修 — the
    data-health pair deliberately excluded, and `signal_*` transitions stay opt-in by design),
    a card interprets what that alert means **for the position actually held**, on the ≤3
    trading-day window. This preset is created **enabled** — the deliberate, scoped override of
    the on_alert default-disabled convention (every hand-created on_alert task still defaults
    off). Push (ntfy) stays off.
  - **The drawer surface.** The symbol drawer gains an 「AI 建議」 section under 技術訊號: the
    latest advice card for that symbol plus a 立即產生 button on the preset. It keys on
    `preset_key`, never the (renamable) task name, and degrades to an honest note when the
    preset is missing / disabled / the fetch fails — no dead button, no spinner that never
    resolves. Two read fixes make it possible: `_insight_type_wire` now exposes `preset_key`
    (it was stored but never served), and the existing fingerprint cache means a whole-task
    re-run only pays for symbols whose inputs actually changed.
  - **The two "moving-average cross" definitions are named apart** (AI-D2). `technicals.ma_cross`
    (20/60) and the rules engine's 50/200 both reached the same prompt under the bare word
    "cross"; the former is now `ma_cross_short` and both variable descriptors say their window,
    so the assistant cannot quote one as the other.
- **The fundamentals leg, for all three markets (W3, AI-D4 + AI-D13..D16).** The chips
  variables were FinMind-only, so a US card was structurally weaker than a TW card. Three
  providers now fetch fundamentals as a **union** — every enabled one writes its own
  `external_snapshots` row (`(source, dataset, symbol, as_of)` was already the key) — and the
  new per-symbol variable **`fundamentals_json`** assembles them as **one block per source**,
  never merged and never averaged (AI-D14: the block key is the provenance; the prompt carries
  the never-average red line). Capability + the key gate ride the providers' existing
  `supports()` wiring, so a keyless Finnhub / Alpha Vantage simply writes nothing and raises
  nothing until a key lands on the settings page (AI-D13).
  - **Canonical 8 fields** (AI-D15): `pe_ratio` / `pb_ratio` / `eps_ttm` / `market_cap` /
    `dividend_yield_pct` / `beta` / `roe_pct` / `revenue_growth_yoy_pct` — same names inside
    every block; a field a provider cannot supply is absent, never fabricated. TW symbols also
    get a `finmind` block mapped from the existing valuation snapshot (no double fetching).
  - **yfinance leg, never `Ticker.info`** — ratios are derived at the fetch seam from the light
    statement endpoints + `fast_info` (the `consensus_source` precedent), with the
    honest-denominator rule (a negative-EPS PE is omitted, not stored negative). **Probe
    verified 2026-08-18** (`scripts/probe_fundamentals.py`): 7 of 8 fields return for US, TW,
    and MY — including a sub-RM1 MY counter; `beta` is the designed absence.
  - **Cadence (AI-D16):** `fundamentals_daily` (09:20) runs yfinance + Finnhub over the whole
    universe; `fundamentals_av_weekly` (Sat 09:40) runs Alpha Vantage over **held symbols
    only** — its 25-calls/day free quota cannot survive a full-universe pass. The held set is a
    portfolio replay result `pricing/` cannot compute, so the weekly job dispatches to a
    registered api-side runner (the `signal_scan` / `alert_compute` pattern).
  - The advice strategy advances to **v2**: a 基本面 section joins the card, with the
    source-tagging and never-average rules spelled out. (Existing installs keep their v1 body
    — the pack reuses a same-name strategy; use 重置為官方 or recreate the task to adopt v2.)
    Variable count 34 → **35**, categories 10 → **11** (`web/vars.js` learned the new
    category's label + order).
- **The AI door reads a whole statement — transactions + dividends + cash in one prompt
  (W4, AI-D3 + AI-D17..D21).** A real statement is mixed; the door used to admit only
  transactions, so the other rows needed separate doors (four pastes, four vision calls) —
  and "skip the awkward rows" is exactly how a ledger goes wrong. The extraction schema is
  now a **discriminated union** (`rows: list[TxnDraft|DivDraft|CashDraft]` on a `kind`
  literal — a dividend row missing `gross` fails at the parse boundary, with one retry,
  instead of inside a builder) plus an **`unparsed` confession list**: FX conversions,
  corporate actions, options, and anything the model cannot classify are surfaced in a
  banner, never silently dropped and never forced into a kind.
  - **Preview and commit go through the three EXISTING doors** (AI-D18): the drafts are
    grouped by kind, rendered to each kind's canonical CSV (the same column constants the
    templates are generated from), and previewed by each kind's whole-file builder — what
    the preview priced and what `/api/import/commit` re-derives cannot diverge, by
    construction. The cash door's pool probe is bound by the router and injected as the
    **required** argument it is, so the withdraw guard runs on AI-extracted withdrawals
    exactly as it does on the CSV and manual doors. No new endpoint, no new writer; undo
    stays per-kind granular; the row↔line commit invariant holds inside each kind.
  - **Prompt v6** (AI-D19): three explicit sections + one worked example per kind; the
    cash-kind vocabulary is joined from `cash_kinds.py` at module level (the GICS-keys
    precedent — a registry test now asserts every kind appears verbatim, so the prompt can
    never drift from the door's allowed set). **`daytrade` finally teaches its semantics**
    (only on explicit 當沖 wording — the debt W1 left) and **`short_sale` arrives with its
    rule** (only on explicit 放空/融券/short wording — never inferred from an oversized
    sell), riding the CSV to the ledger exactly as the column comment planned.
  - **The preview renders three sections** (AI-D21): transactions / dividends / cash, each
    with its own columns and checkboxes; a kind with zero drafts stays hidden. Cash rows
    show the **server-owned Chinese kind label + an explicit ± sign** (＋入金 / −券商費用) —
    the direction lives in the kind, and a mislabel reverses the pool by 2× with no error,
    so the label is the guard. Transaction rows gain the 放空 chip beside 當沖.
  - **Accuracy is now measurable** (AI-D20): `tests/golden/ai_extraction/cases.json` holds
    38 synthetic cases (three kinds × three markets × the edge cases — 當沖 both legs, an
    undeclared same-day round trip, declared shorts, DRIP, the MY net dividend, every cash
    kind, zh kind aliases, mixed statement blocks, and four must-confess-unparsed rows),
    guarded against rot by a deterministic structure/coverage test, and measured by
    `scripts/ai_extraction_eval.py` — a manual live runner that reports field-level hit
    rates with the cash-`kind` / `daytrade` / `short_sale` mislabel rates **listed
    separately** (the only fields that move money silently). Thresholds pin to the first
    baseline run, not before. Screenshot cases go through the owner's manual review; real
    exports never enter the repo.
  - The txn arm also gains **sibling awareness** (C1 extended to this door): a sell covered
    by a buy earlier in the same paste no longer flags 賣超 against a position the same
    batch is still building.
  - **Senior-review hardening (two independent reviewers, five findings).** The parse
    boundary now fails loud in BOTH directions: the drafts and `AiDraftList` are
    `extra="forbid"`, so a mistyped optional money field (`with_hold` for `withholding`) or
    a model regressing to the v5 `{"drafts": [...]}` shape raises at the boundary and takes
    the one retry instead of silently dropping the statement's number — or the whole
    extraction. (Behavior note: a model that habitually appends junk keys now degrades
    loudly after the retry rather than being silently tolerated — deliberate.) The v6
    prompt's cash one-shot example carried a raw newline inside a JSON string — invalid
    JSON in the model's strongest anchor; all three example blocks are now machine-parsed
    by a test. And the per-kind commit loop **settles before it reports**: a mid-loop hard
    failure (a kind 500s after another kind already wrote) used to strand the written kinds
    on screen looking uncommitted — no section retirement, no ledger refresh, and a retry
    that re-posts them; the loop now finishes the settled kinds first, then reports the
    failure with the remainder retry-able.
- **The scoreboard can now score all three prediction metrics (W5, AI-D7 + AI-D22..D26).**
  Two of the three quantitative prediction types could never be scored: `_measure_actual`
  returned `benchmark_return_pct=None` for `relative` and `vol_change_pct=None` for
  `volatility`, so every such card deferred five times and died `undetermined` — never a
  hit, never a miss, and excluded from every hit-rate stat (the scoreboard looked clean
  partly because the hard-to-score cards were invisible to it). The scorers themselves
  were already written and tested; what was missing was the measurement seam feeding them.
  - **`relative` replays the benchmark** (AI-D22/D23): the card's symbol maps through a
    fixed server-side table (TW→`0050`, US→`sp500` — the series `history_daily` already
    refreshes into `prices`) and the benchmark's create→due return is measured with the
    same ±14-day tolerance as the symbol's own legs, both legs in their local currencies
    (per market they share one by construction). An MY card, an unregistered symbol, or a
    missing benchmark series degrades to `pending_data`, never a forced miss.
  - **`volatility` compares two fixed 30-day windows** (AI-D24): realized vol over the 30
    trading days ending at the due date vs the 30 ending at the create date, over the
    split-re-expressed close series (`series_in`, so a split inside the window is not
    mistaken for a vol spike), using the same `annualized_volatility` estimator the alert
    inputs already use. A flat regime call now has a fair chance: the flat band for this
    metric is **±5%** (AI-D25) instead of the shared ±0.5%, which a 30-day estimator
    exceeds on noise alone. A zero baseline vol or insufficient history → honest None.
  - **The scoreboard rows now tell unscorable from hit** (AI-D26): `renderScoreRows` never
    read `status`, so a `pending_data` / `undetermined` row (miss=0) painted as
    "✓ 命中" — the page reported a better record than the ledger held. Rows now render a
    status chip (待資料 / 未定) unless the evaluation actually scored. Portfolio-scope
    quant cards stay narrative-only (the `:421` None branch), by ruling, until W7.
- **Signal history + the event-study backtest (W6, AI-D2 + AI-D27..D32).** The rules
  engine had no memory: `signal_states` kept one row per symbol (latest state only), so
  "how did TechScore get here" was unanswerable and the backtest the blueprint's D3a gates
  indicator expansion on could never run — a deadlock (no history → no backtest → no
  indicators). This wave builds the memory and the study, and lights the last dark prompt
  variables (36/36 live).
  - **`signal_history` — one row per symbol per price-data date** (AI-D27/D28): the full
    state vector (four rule states + scores, tech_score, evaluation_context,
    params_version), keyed `(symbol, as_of)` on the DATA date so a holiday re-scan
    overwrites the same row idempotently. Depth comes from **replay** — the rules engine
    is a pure, params-stamped function, so the scan re-evaluates each missing historical
    date through the same `_read_series` assembly and rebuilds exactly the row it would
    have written that day (years deep on the existing 5y price backfill). Forward
    maintenance is the daily scan's missing-set fill, which also self-heals a later
    deeper price backfill, a filled provider gap, or an aborted first backfill. The head
    row rewrites only on real change (compare-then-skip), so a re-scan is a provable
    no-op, `updated_at` included.
  - **The corporate-action seam invalidates BOTH derived tables** (a pre-existing hole
    this wave's seam would have inherited): a restated price basis makes every stored
    evaluation wrong, and the stale `signal_states` comparison row could fire transition
    events for a restatement that is not a market event. `reconcile_split_prices` — the
    single wrapper all five corporate-action mutation doors call — now deletes both
    tables' rows for the affected symbols; the next scan rebuilds the history and
    silently reseeds the state with zero events. Instrument purge cleans
    `signal_history` too.
  - **The event study** (`portfolio/backtest.py`, pure Decimal, no conn): rule score-sign
    changes with hold semantics + composite TechScore crossings of the EXISTING 65/35
    state bands (AI-D32 — one threshold vocabulary; a second 70/30 would be the AI-D2
    two-definitions defect), forward-return distributions at +20/+60/+120 trading days
    vs the same symbol's unconditional distribution, local currency, over the
    split-re-expressed full-span series. The guards are the point: a cell with n<8
    answers 「不足以判斷」 with the count shown and the numbers withheld; overlapping and
    right-censored events are counted and disclosed; nothing is annualized.
  - **All 36 prompt variables are live** (AI-D31): the two spec-04 stubs keep their
    DECLARED meaning — `backtest_json` serves the global confidence-bucket calibration
    bins + overall hit rate, `calibration_gap_json` a rolling 20-evaluation signed gap
    (actual − claimed, gated unavailable below 8 scored rows), both computed from
    `insight_evaluations`; the event study rides the new per-symbol
    `signal_backtest_json` (category `price`, external-fed like `rule_signals_json`).
    Official template prompts are unchanged — adopting the new variables into prompts is
    a W7 corpus-quality decision.
- **The assistant, complete — cards cite their own evidence, and the scoreboard answers
  "should I listen" (W7, AI-D33..D37).** W6 built the evidence (signal history, the event
  study, the calibration variables); W7 puts it on the cards and on the scoreboard. The
  LLM only narrates — every number is computed locally (the programme's first rule).
  - **The advice card (v3) cites its backtest and anchors its confidence** (AI-D33):
    `signal_backtest_json` (this symbol's event study), `backtest_json` (the model's own
    global calibration bins), and `calibration_gap_json` (its recent signed gap) join the
    prompt under a strict citation law — verbatim numbers, an n<8 cell is called
    sample-insufficient and cites nothing, every citation carries the sample count and the
    same-window baseline. The confidence-anchoring law caps a card's stated confidence at
    the matching bucket's actual hit rate + 5 (cap 70 when the bucket has n<8) and a
    negative rolling gap lowers it by that magnitude. The checkup card (v2.6) cites the
    event study as well; the weekly report (v2.2) narrates the AI's own track record from
    the two portfolio-scope calibration variables (AI-D34). No schema change, and no
    code-side confidence clamp — the anchoring is prompt law, auditable in the body.
  - **Portfolio-scope predictions are scored now** (AI-D35, closing AI-D26's deferred
    question): a portfolio card's `price_change` prediction is measured by the existing
    chain-linked TWR index over the create→due window — flow-adjusted, so a mid-window
    deposit never reads as a gain (a disproof test watches exactly that). Thin or missing
    trend data stays an honest `pending_data`, never a fabricated miss.
  - **The scoreboard is a decision-quality dashboard** (AI-D36): `/api/ai-score` (same
    route) additionally serves the rolling calibration gap (ONE definition shared with
    the prompt variable), each task's sample-gate progress (n vs its min_samples), and a
    backend-computed **trust tier** per task — 樣本不足 / 早期 / 可參考 — synthesizing hit
    rate × calibration error × sample count into the page's single vocabulary. The page
    renders the tier as a stamp and the gap as a fifth stat card; it computes nothing.
    `calibration_bins` now quantizes ROUND_HALF_UP in pure Decimal (the float/HALF_EVEN
    path was a pre-existing second rounding vocabulary on the same surface).
  - **Official template upgrades reach existing installs** (AI-D37): the strategy-prompt
    settings card shows a 「同步官方 vX」 button when the row's name matches an official
    template and its body has drifted; confirming (an explicit danger dialog — custom
    edits are lost) overwrites the body through the existing from-template endpoint's new
    replace mode, and every task bound to that strategy id runs the new body on its next
    pass. A renamed or archived row answers 409 — a replayed request cannot overwrite a
    row the owner moved.
- **Corporate actions — SPLIT / EXCHANGE / SPINOFF** (spec
  `docs/spec/2026-08-06-corporate-actions.md`; W0–W10 on `feat/corporate-actions`, **not yet
  released**). A share count that changes without a trade previously had no representation, so
  the later sell of a split position tripped the 賣超 guard and **permanently discarded that
  position's cost basis**. The ledger now records the event, the replay applies the ratio as two
  positive integers (multiply first, divide last), and the drawer's reconciliation footer gains
  a `＋公司行動` term so the correction is visible rather than merely applied. Entry lives at
  three doors — the 賣超 confirm dialog offers 補登公司行動 **first**, the drawer offers it beside
  a ⚠ 對帳不一致 footer, and the ledger page gains a 5th tab — all sharing one form whose
  always-on preview states **成本不變 ✓**. Prices carry a `close_raw` / `split_basis` pair so a
  split's re-expression is idempotent and reversible, and the accounting manual gains
  **§4.4 公司行動** (zh authority + en mirror) with a worked example and an oracle anchor per
  formula.

  **The feature's code is complete.** §7.6's full enter-through-the-UI e2e landed with the four
  entry-surface defects it surfaced; the e2e harness flake that stopped the suite being called
  green was root-caused and fixed; and **D46 (2026-08-12) unblocked §10.5** — the acceptance gate
  runs against the **simulated** corpus (D43) rather than waiting on the owner's real statements,
  and it PASSes there while FAILing on a corpus one action row short. What that proves is stated
  precisely rather than rounded up: the feature **accepts the shape** of the owner's ledger. It
  does not prove the owner's *actual* cost basis reconciles — only real figures can, and that run
  is now a post-release verification rather than a gate. Decisions: **D40 and D41 ratified**
  2026-08-11; **D42** (W6c in scope), **D43** (simulated corpus), **D45** (D36 retired) and
  **D46** are the owner's own rulings; **D44 was the last open one and is now RULED**, together
  with **D47** and **D48** which the owner gave with it (2026-08-15) — see below. A single
  prose summary here would drift from the spec within a week, which is the failure this release
  spent an audit correcting, so the detail stays in §8's decision table and the commits.

- **6th CSV import kind — cash movements (`kind=cash`).** 入金 / 出金 / 期初資金 / 折讓款, with the
  foreign-credit acquisition cost `acq_home_amount` — the **amount**, never a rate (spec F1: a rate
  is an average, and a rate *column* would put a rounded average into a file that gets re-read,
  re-edited and re-imported). Omitting the column was the tempting choice and would have been the
  wrong one: a bulk import is exactly how a foreign pool gets funded, so every bulk-imported foreign
  credit would have been permanently basis-less, and `covered_ratio` scales the **whole** foreign
  exposure including stocks (F3), not just the cash leg. Two soft advisories, both CSV-only because
  the manual door has no preview seam: a re-imported duplicate (a cash movement has no natural key,
  so a re-upload books every amount twice silently) and a foreign `WITHDRAW` that is probably an
  unrecorded 換匯 (N1) — named, never coerced, because a genuine foreign withdrawal is legitimate
  and the importer cannot tell them apart. The whole file is one batch, or the first import into a
  fresh ledger would reject every withdrawal it contains.

- **Broker-statement converter — Charles Schwab (P1a).** `scripts/schwab_convert.py` turns a raw
  Schwab transaction export into this app's own import CSVs, offline. All the logic lives in
  `portfolio_dash/data_ingestion/broker/` (`ir` · `schwab` · `grouping` · `reconcile` · `registry`)
  so it is under `mypy --strict` and the regression suite; the script is only a command line
  around it. The classification unit is the **`(action, description)` PAIR, never the action**:
  one observed broker action carries **14 distinct meanings**, and its two largest sit on opposite
  sides of the keep/drop line — 145 rows of dividend withholding tax against 126 null journals —
  so an action-keyed importer either deletes the tax or keeps the phantoms, and no arithmetic
  recovers either. An unmapped pair is a **hard error** (rule 7): the ground-truth build this was
  lifted from ended its classifier with a catch-all `return OPT_ADJUST`, which would have booked
  every future broker string as an option adjustment and dropped it out of every equity figure
  without a word.

  Four transformations exist in the shape they do because a plausible simpler version was measured
  against a real 1,375-row export and found to destroy data. Suppression is **classification
  first, arithmetic as a veto**, keyed on `(date, symbol)` and never across symbols — grouping by
  date alone dropped 180 rows of which **8 were real corporate actions**, because a 1-for-1 ticker
  exchange nets to zero in *both* cash and shares and is therefore arithmetically indistinguishable
  from an internal journal. The zero-sum veto covers shares as well as cash, or a 3-for-1 split
  (−85 / +255 shares, $0 / $0) passes an amount-only check and vanishes. A reinvested share count
  is **derived from `amount / price`**, not read off the row: 125 of 227 real reinvest rows fail
  `quantity × price == amount`, and the printed quantity is a rounded view whose error accumulates
  in one direction across a multi-year DRIP history. Pre-history positions are found by the
  **union** of two detectors (running-balance minimum ∪ first-event-is-a-sell-or-reinvest) because
  each misses what the other catches, and a **third** case — already short before the window,
  balance never negative, first event a buy — is documented as undetectable rather than implied
  away.

  **`broker/reconcile.py` is the point of the package.** Every blocking check re-measures from the
  original rows rather than re-reading the transformation's own output: dropped groups are re-added
  leg by leg (suppression *asserts* its zeros without ever summing them), trades are re-derived
  from `quantity × price ± fees`, and cash and per-symbol shares are conserved end to end. Blocking
  and advisory are split on a stated line — blocking means *our own transformation* invented or
  destroyed money, and refuses the **whole** batch; advisory means rows that will not be written,
  named individually by `file:line`, because an export legitimately contains option legs and
  positions older than its window and a gate that rejected those would be switched off within a
  week. Two ratios are deliberately **left blank** for the owner rather than derived: a one-leg
  split states only its delta, and replaying the file to recover the ratio was implemented, then
  measured on the two real one-leg splits, and **removed** — it produced `4:1` for one (right) and
  `109:24` for a 3-for-1 (wrong, and wrong in the silent way, because that position predates the
  export and the replay cannot know it is incomplete).

  Privacy is structural, in `verify_corporate_actions.py`'s terms: no path has a default, **stdout
  carries only labels, integer counts, issue codes and masked symbols** (held by a test that greps
  its own output for a decimal point), and the detailed report goes to a file inside `--out`, which
  already holds the converted ledger. `scripts/privacy.py` now owns the symbol mask and the Windows
  UTF-8 stdio fix for both scripts — a privacy control with two copies is one copy short of a leak.
  The regression corpus (`tests/golden/broker/`, generated by `scripts/gen_broker_corpus.py`) is
  synthetic, committed, and its output is re-parsed through the **production** preview builders, so
  a column the importer would reject fails here rather than on the owner's ledger. Workflow, the blocking-issue table and the paste-safety rule are written up in `docs/reference/broker-import.md`.

- **Three cash-movement kinds — `INTEREST` / `INTEREST_EXPENSE` / `BROKER_FEE`**, excluded from
  XIRR (owner ruling D1=A, 2026-08-13). Adding them was **not** adding enum values: three places
  wrote the debit test as *"WITHDRAW vs everything else"*, so a broker fee would have **increased**
  the cash balance and been counted as an unbased foreign-currency acquisition, dragging
  `covered_ratio` down on a pool that had lost nothing. `shared/cash_kinds.py` now holds one table
  classifying every kind on **two orthogonal axes** — cash direction and whether it is an FX
  *acquisition* — and `INTEREST` is the row proving one boolean was never enough: a **credit that
  is not an acquisition**, because income arising inside the pool inherits the pool's average
  exactly as sale proceeds and foreign cash dividends already do. Treated otherwise, a USD account
  that never converted a cent would report an incomplete basis merely for being paid interest, and
  a false alarm over the whole foreign exposure is worse than none. The overdraft guard stays
  WITHDRAW-only: a withdrawal is an intention worth blocking, a fee is a recorded fact, and a
  margin account legitimately runs negative.

- **US cash dividends (P1b).** `drip_us` accepts `CASH` as well as `DRIP`. The same US position
  pays plain cash in some quarters and reinvests in others, so every real cash payout was being
  rejected row by row as a `dividend_type_mismatch`. No accounting change was needed — a `CASH`
  row is in `CASH_DIVIDEND_TYPES` and reduces `adjusted_total` like any other (D35) — and that
  claim is only worth something because the phase-1 oracle now recomputes such a position and
  agrees. The manual dividend form gains a CASH/DRIP switch, and its withholding field is
  **overridable** instead of locked to `gross × 0.30`: real broker cent-rounding does not match the
  computed product, and a read-only field that is wrong forces the owner to lie somewhere else.

- **Import provenance — `import_batches` + `import_batch_id` / `source_row_hash`** on the five
  append-only ledgers. Re-importing the same export no longer duplicates it, and a batch can be
  deleted precisely. The batch is a property of the **file** (name + sha256); the row hash is
  derived from the row's own content plus its **occurrence ordinal** among identical rows, because
  two identical $50 deposits on one date are a real statement pair and a set-based hash would
  silently merge them. Stated limitation: the ordinal is per-file, so the same row arriving in two
  different exports reads as a duplicate — right for five overlapping real exports, wrong for a
  genuinely separate movement, and the manual form is the escape hatch.

  The import banner now **says** when rows were already held. Re-uploading a file the ledger
  already has used to read 「成功 0 筆・跳過 0 筆」 — true, uninformative, and the reading a user
  takes from it is *the upload failed, try again*. A duplicate is neither written nor skipped
  (skipped means the user deselected it), so it gets its own words. ~~⚠ Known gap: the batch list
  and the undo exist as API only.~~ **Closed below.**

- **The broker converter has a door in the app** — 輸入 → CSV 匯入 → 來源「券商對帳單」. Drop the
  broker's raw export, read one report, press one button. `POST /api/broker/convert` runs the
  **same** `data_ingestion/broker/convert.py` the CLI runs (the row-building moved out of
  `scripts/` so there is one implementation and not two that disagree about money), and returns
  CSV **text** which the page feeds back through the ordinary `/api/import/*` path — so the
  converted rows meet every validation a hand-made CSV meets, inherit the duplicate detection,
  and land in the same undoable batches. Nothing is written server-side and nothing is stored; a
  blocking reconcile issue withholds **every** file rather than shipping them behind a flag.
  The CLI stays, unchanged and byte-identical, for anyone who would rather the statement never
  left the laptop. What the file genuinely does not determine — a one-leg split's ratio, a
  pre-history position's cost — is asked for as **form fields** instead of a `*_TO_COMPLETE.csv`
  to open in a spreadsheet, with the ratio as two integer boxes (D14: a decimal ratio replays as
  a wrong share count that looks right). Blank stays blank and the result says which rows were
  left out.

- **最近匯入, with 復原** — the import-batch list and its undo now exist on the page (they had
  been API-only). This ships in the same change as the one-click import deliberately: an easy way
  to load five years of broker history into a ledger, with no way to take it back, is a button
  nobody should press. Visible for ordinary CSV imports too.

### Fixed
- **The assistant cited its own record — in the wrong unit, from the wrong cell, at the wrong
  confidence (W7.1, AI-D38).** The first live run of the v3 advice cards on the demo produced
  three output defects with correct producers behind all of them, so all three fixes put the
  semantics next to the numbers rather than adding a validator. **(1) Unit.** Every
  `mean`/`median`/`pct_positive` in the event study is a FRACTION, a fact that lived only in
  the variable registry's `desc` — UI documentation the model never sees. One batch of cards
  rendered `0.1336` as 「+0.1336%」 (the true value 100× smaller), as `+0.0640 USD`, as a bare
  `0.1053`, and correctly as `+9.61%`. `signal_backtest_json` now carries a `units` block with
  a worked example, and both citing templates state it. **(2) Substitution and fabrication.**
  A cell below the sample gate emits no mean at all; two cards printed the same-window
  BASELINE (the unconditional average over every trading day) as that signal's event return,
  and one invented a number present nowhere in the payload. The citation law now forbids both
  by name. **(3) The ceiling.** Asking the model to walk a bins table and apply a three-step
  conditional mid-generation produced 0 of 13 compliant cards; `scoring.confidence_ceiling`
  computes the largest self-consistent value once and ships it as
  `backtest_json.confidence_ceiling`. **AI-D33 stands** — nothing clamps what the model
  writes. Also: `calibration_gap_json` gains a plain-language `reading`, because the weekly
  card read a −0.466 gap as 「低估自身表現」 (the opposite) with the sign convention stated in
  the same section.
- **W6 post-ship review (three LOW findings, no HIGH/MEDIUM across three lenses).** The
  `signal_backtest_json` producer rebuilt the corporate-action index per symbol per run —
  `_per_symbol_ctx` already threads a caller-built `actions` (trap #21) but never passed it
  in; the index is now built once per request and threaded through `_external_vars`
  (a required argument — forgetting it is a TypeError, not a silent re-read).
  `_read_series` briefly returned a third value (the last price date) that no call site
  consumed — reverted to the 2-tuple rather than leave a decoy that invites a future reader
  to treat it as load-bearing. And an honest limitation is now documented where it lives:
  a provider *revision* mid-series (a corrected close on an already-stored date) leaves
  downstream `signal_history` rows stale — the missing-set rule only fills absent dates;
  the manual remedy (`delete_symbol` + rescan) is named in the module docstrings.
- **The AI input door showed one tax and wrote another.** `AiDraft.daytrade` reached the
  preview's `TxnInput`, so a TW same-day round trip was priced at the 當沖 rate of 0.15% — but
  the commit-ready CSV the same function emitted had no `daytrade` column, and
  `/api/import/commit` **re-derives its own preview from that CSV** ("the preview's answer is
  advisory, this one writes"). The row was therefore written as an ordinary sell at 0.3%,
  **double what the screen had just shown**, with the difference riding into `original_total`
  as cost basis. Measured on the disproof test before the fix: previewed 900, written 1800.
  The column already existed in `TRANSACTION_COLUMNS` and in `OPTIONAL_COLUMNS`; it was simply
  never emitted. Two neighbours were checked and are **not** the same bug: `is_etf` is dropped
  too but the instrument registry wins at the fee seam in both paths, and an unregistered
  symbol is a hard issue that never commits — so there is no reachable divergence; `short_sale`
  is absent from the draft schema entirely, which is a gap awaiting its prompt rule, not a
  disagreement between two paths.
  - **The flag is now visible, because it now has teeth.** While `daytrade` was being dropped
    it was harmless whatever the model inferred; carrying it to the ledger makes a wrong
    inference a money error, so the AI preview row gains an amber 當沖 chip next to the
    direction chip. A flag the reader cannot see is a flag the reader cannot correct. What the
    model should *infer* is a separate question and is deliberately not answered here: the
    prompt has never taught it when to set the field, and changing that without the extraction
    accuracy corpus would be a guess dressed as an improvement.
  - ⚠ **A second defect found on the way, worse than expected:** the same generator did no CSV
    quoting at all — its own docstring said so — while the prompt *asks* the model for a
    free-text `note`. One comma did not merely shift the columns: the row parsed to more fields
    than the header declared, `csv.DictReader` filed the surplus under a `None` key, and
    `build_transaction_preview` raised `AttributeError` on its first line, **outside** the
    try/except that catches malformed rows. So the commit route did not degrade, it crashed —
    on text an LLM was invited to write. Now emitted through `csv.writer` (QUOTE_MINIMAL, so
    comma-free values stay byte-identical); CR/LF in a note are still collapsed to a space,
    because quoting would faithfully preserve a newline and the frontend commits only the
    checked rows by splitting this text on `\n` and taking data line `index + 1`.

- **A pasted JPEG went to the vision model labelled as a PNG.** The intake door sniffed a
  screenshot's real format by magic bytes to decide whether to accept it, and the transport
  then hardcoded `data:image/png` for every payload — the app identified the type correctly and
  told the provider something else. Lenient providers sniff the bytes and shrug; a strict one
  rejects it, and the failure surfaces as a generic vision/parse error nowhere near the cause.
  One sniffer now serves both (`shared/image_types.py`), so "is this an image?" and "what do we
  call it?" can no longer be answered differently. An unrecognised payload still falls back to
  PNG rather than raising: the door rejects non-images before this point, so reaching it means a
  direct caller skipped the check, and a wrong label beats no message.

- **The cash ledger was writable and invisible: no export, no ledger tab, no printed
  section.** It became the 6th ledger on 2026-08-13 and every surface that enumerates ledgers
  was left at five, so a round trip through the export silently dropped rows the import had
  just accepted, and the page that owns the ledgers had no way to show them. Closed by one
  registry field (`export_kind`), which carried the zip, the single-ledger CSV, the export
  centre's copy and the printable report with it — the payoff for the previous entry's work.
  - **`GET /api/ledgers/cash`** feeds the new 資金收支 tab, in the same shape as its five
    neighbours so the pager, the account/date filters and the CSV button need no special
    case. It sends a **`signed_amount`**: amounts are stored unsigned with the direction in
    the kind, so a page rendering the raw figure would print a broker fee as money arriving,
    and `web/` may not compute money. The printed report's new section signs its total the
    same way.
  - ⚠ **A defect found on the way, already live:** `export/cash_statement.py` kept its own
    four-entry label map, and the three kinds added on 2026-08-13 never reached it — so a
    Chinese statement printed 利息 as `interest` and 券商費用 as `broker_fee`. Nothing failed,
    because a `.get(kind, kind)` fallback is exactly what turns a missing label into
    plausible output. One `CASH_KIND_ZH` in `shared/cash_kinds.py` now feeds every consumer.
  - The tab↔export mapping is asserted from outside the page source
    (`tests/contract/test_ledgers_cash_tab.py`). `trades.html`'s own comment already warned
    what an omission costs — *"a 5th tab that is not here silently exports the transactions
    ledger under the corporate-action tab"* — a wrong FILE under a right-looking button, with
    no error to notice. Proven by removing the pair and watching it go red.

- **A target weight stranded itself on a dead ticker after an EXCHANGE.**
  `target_weights_config` is a single-row JSON keyed by SYMBOL STRING and nothing re-keyed it,
  so a merger or rename left the owner's target filed under a symbol the ledger no longer
  holds — unmeetable, and visible only as a permanent `rebalance.excluded_with_target` entry,
  one indirection away from the event that caused it. **This is D47's shape, missed when D47
  shipped**, and the reason it was missed is worth keeping: a weight is *config that nothing
  recomputes*, so there is no second number for it to disagree with. No reconciliation goes
  red, no replay complains. The alert band at least fires.
  - **Value unchanged, source cleared, destination never overwritten** (D47 parity — that
    number is the owner's judgement about the destination security and no merge rule could be
    right). ⚠ In that collision case the source's weight **stays stranded**, deliberately:
    `excluded_with_target` is the honest place for an unsatisfiable target, and silently
    deleting a value the owner typed is worse. **SPINOFF does nothing** — the child is a
    different company and carving the parent's target between them needs an allocation rule
    nobody has given. A SPLIT is immune either way: a ratio is unitless.
  - Called from `api/` at **both doors**, not from the row writer where the band move lives:
    `data_ingestion -> strategy` is not an authorised edge (`architecture.md`), and
    re-deriving the weights format on that side would make it a second owner. Two call sites
    means two chances to wire only one, which is this defect's own shape — so each door has
    its own test, and each was watched to fail with the call removed.

- **Nine sentences said the app had four ledgers. It has six, and exports five.** The ledger
  registry removed hand-maintained ledger enumerations from the *code* and never reached the
  *copy*, so when corporate actions became the 5th ledger the words stayed put: the export
  centre told the owner they were about to download four CSVs while the route built five; the
  帳本記錄 panel printed a count beside a tab bar showing more tabs than the count; and the
  report button read 「四帳本＋期初庫存」, which names five things over a report that had four
  sections and counts 期初庫存 twice. **A count typed into a sentence is not wrong in any way a
  type checker, a route test or a golden payload can see** — it is only wrong against a fact
  stated somewhere else — which is why the fix is structural rather than a set of better
  numbers: `GET /api/export/ledgers` serves the list from `EXPORT_KINDS` for the one card that
  genuinely needs it, and every other site stops stating a count. Adding a 7th ledger now
  updates that sentence with no edit to the frontend at all.
  - ⚠ **One of the nine was not a count but a wrong claim.** The 重算 dialog said it rebuilt
    from 「期初庫存、交易、股利、換匯」. `LedgerBundle` has no `fx_conversions` field —
    `build_book` cannot see the FX ledger — and the one ledger it *does* replay, 公司行動, was
    missing from the sentence. So the dialog promised a check it never performed, on the ledger
    a user is most likely to want checked after typing a rate by hand. The copy now names what
    is actually replayed and says the other two are record-only. **Deliberately not "fixed" by
    widening the endpoint:** `forex/pools.py` and `portfolio/cash.py` cannot raise, so replaying
    them would be a verification that can never fail — which is a progress bar, not a check.
  - The printable 帳本報告 gained its **公司行動** section (it had four sections out of six). It
    is the one section with no money total, and that is the point: an action moves shares
    without moving cash, so a 「合計」 line would have to be zero or invented. The ratio prints
    as **two terms**, because a quotient on a reconciliation page turns 7-for-1 into 0.142857.
  - `KIND_ZH` moved to `shared/corporate_actions.py` so `export/` could use it without importing
    `api/`. ⚠ Recorded there, not fixed: `web/detail.js` holds a **third** copy that disagrees —
    it labels SPINOFF 「分割」, which is the backend's word for SPLIT. Two different kinds under
    one word, on two screens of the same app.
  - Two guards, each **watched to fail** before being kept: no file may state a ledger count
    (`tests/contract/test_ledger_count_not_hardcoded.py`), and the endpoint, the zip's members
    and the report's section count must all equal the registry
    (`tests/contract/test_export_ledger_list.py`). The first one found a **ninth** site on its
    first run — inside `ledger_registry.py` itself, the file whose existence is the argument
    against counting ledgers by hand. The zip's existing member test could never have caught
    any of this: it asserts a **subset**, and a subset assertion cannot see an addition.
- **A CSV import now validates each row against the ledger PLUS its own siblings.** Measured on a
  synthetic broker export 2026-08-12: **7 of 47** transaction rows raised 賣超 whose covering buy
  was three lines above them in the same file. The numbers end up right if the owner confirms —
  which is the harm, because 賣超 is the **one** confirmation in this system whose acknowledgement
  permanently discards a cost basis (the STICKY rule), so every false one trains the owner to
  click the dialog that must stay frightening. `cash_import.py` had named this class and fixed it
  a release earlier ("**The whole file is one batch** … the E1a class of failure"); the trade door
  never got the `batch` argument. ⚠ The fix is in the **share walker**, not in `validate.py`: the
  obvious version adds the siblings to the returned count and is corporate-action-*unaware* about
  them, so a sibling buy of 100 before a 4-for-1 split would meet a later sell of 400 as 100 — the
  split-then-sell cascade the corporate-action feature exists to prevent, reintroduced through the
  import door. One test fails on that version and only on that version.
  The same change hoists **one** `ActionIndex` per file instead of building one per row (spec trap
  #21, live on this door): 1,374 fewer full reads of the action ledger on the owner's export.

- **A corporate-action CHAIN imports in one file, instead of failing silently.** A SPLIT whose
  shares arrive from an EXCHANGE earlier in the *same* file was hard-rejected
  `no_position_on_action_date`, because the importer built its `ActionIndex` from stored rows
  only. **Both of the owner's real chains have that shape** (a de-SPAC then a rename; a de-SPAC
  then a reverse split). The failure was invisible: the row landed in `skipped` and the import
  reported success while the share count stayed wrong by the whole ratio. A row still cannot
  justify **itself** — the walk's cut excludes the action being validated by the ordering rather
  than by a rule anyone has to remember, and the paired test proves a lone SPLIT onto an empty
  position is still refused.

- **「跳過」 no longer means two different things.** A row the caller deselected and a row the
  importer **refused** shared one counter, so an import that dropped 3 of 5 corporate actions
  announced 「成功 2 筆・跳過 3 筆」 — read by everyone as *I didn't tick three*. `rejected` is now
  its own bucket and carries each row's own reason verbatim; the banner says 「擋下 N 筆」, explains
  that this is not a deselection, and stops clearing the paste box on a run that had refusals.
  Additive and only when non-zero, so the other kinds' payloads are byte-identical.

- **The corporate-action import template no longer contradicts itself.** Its EXCHANGE moved the
  whole AAPL position out on 2026-06-02 and its SPINOFF then carved cost from AAPL on 06-03 — not
  an enterable pair. It read as valid only because the importer could not see a row's siblings;
  the moment it could, the SPINOFF was correctly refused. The example was wrong, not the verdict.

### Changed
- **ECharts 5.5.0 is now self-hosted** (`web/echarts.min.js`), so a `cdn.jsdelivr.net` outage
  can no longer take the charts down (owner ruling 2026-08-12). The webfont deliberately stays
  on Google's CDN: `--font-ui` / `--font-num` already end in a generic family, 9 of 18 pages
  never request the font at all, and the e2e suite runs the whole app against an EMPTY Google
  Fonts stylesheet — a font outage is cosmetic and continuously tested, where a chart-library
  outage is not.
  - **Four load sites, not three.** `index.html`, `insights.html` and `settings.html` carry a
    parser-blocking `<head>` tag each; the fourth is a **runtime-injected** copy in
    `web/shell.js::pdEnsureDrawer`, which lazily loads the library on **every** page when the
    symbol drawer is first opened and which no HTML grep finds. It had **zero** browser
    coverage (every drawer e2e reaches the drawer via `/index.html`, where `window.echarts`
    is already set, so the load branch never ran); `tests/e2e/test_echarts_selfhosted.py` now
    opens the drawer from `/instruments.html` and covers it.
  - **Pinned artifact, cross-witnessed.** `scripts/vendor_echarts.py` (stdlib only — no Node,
    no bundler) downloads the file from **jsdelivr AND unpkg**, requires the two mirrors to be
    byte-identical, and requires
    `sha256 = 42f8329d989b6f6539dd2b15bbdf0d82025762ac112fbb60dc57b27d7bcf3946`
    (1,029,203 bytes) before writing anything. The pin lives in
    `tests/contract/test_vendored_assets.py` and the script imports it, so there is exactly one
    machine-readable copy. Apache-2.0; provenance and the upgrade procedure are in
    `docs/reference/vendored-assets.md`.
  - **`.gitattributes` is new, and load-bearing.** This repo is developed with
    `core.autocrlf=true`, and a minified bundle has no NUL bytes, so git classifies it as text
    and rewrites LF → CRLF on **checkout**. Measured before the fix: the committed file checks
    out as **1,029,248 bytes** with 45 conversions and a different digest — on a file nobody
    edited. It still *runs* (JS tolerates CRLF), so nothing but the pin would have noticed, and
    the pin would have failed on every fresh Windows clone while passing on the Linux host.
    `*.min.js -text` fixes it in both directions, and a test fails the build if the rule is
    removed — otherwise the symptom is a size mismatch whose suggested remedy (re-run the
    vendor script) leads straight into a loop.
  - **Flat path, not `web/vendor/`.** `scripts/stamp_asset_version.py` and
    `tests/contract/test_static_cache_discipline.py` share a regex that matches bare relative
    filenames only (`[A-Za-z0-9._-]+\.(?:js|css)`), so a nested path would be invisible to both
    the `?v=` stamper and its guard. Flat keeps the asset inside the existing cache discipline
    instead of carving out an exception for it.
  - **Accepted cost.** The library is now served by the app: **1,029,203 bytes** on the first
    load of the three chart pages and on the first drawer open of each other page, instead of
    from jsdelivr's edge. `_NoCacheStaticFiles` sends `Cache-Control: no-cache`, so subsequent
    loads revalidate to a 304 rather than re-transferring. The other 15 pages deliberately do
    **not** get a `<head>` tag: eager-loading 1 MB on `cash.html` for a drawer most sessions
    never open would trade a rare outage for a permanent regression.
  - **The dashboard now degrades legibly if the file is ever missing.** `web/charts.js` was the
    only unguarded consumer, and the failure was worse than blank charts: `initAll()` threw
    before `wireModeOnce()`, so `#trend-mode` / `#twr-windows` / `#value-ranges` got **no click
    listeners at all** — dead buttons with no cue — and every later theme toggle raised an
    uncaught error. It now renders 「圖表元件未載入」 into the chart hosts and wires the controls
    anyway.
  - **The browser suite now makes zero outbound connections.** With nothing third-party left to
    fetch, `tests/e2e/conftest.py`'s memo-cache machinery (`_cdn_get` / `_cdn_put` /
    `route.fetch`) is deleted: font hosts get an empty 200 stylesheet, anything else gets an
    empty 200 and is recorded. `test_thirdparty_isolation.py` asserts the only non-loopback host
    contacted is `fonts.googleapis.com`. `scripts/verify_live.py` checks that
    `GET /echarts.min.js` returns the pinned size and hash — the only check that proves the
    offline-resilience property on a **deployed** instance.
- **`validate_cash_movement` extracted from `api/routers/cash.py` into `data_ingestion/validate.py`.**
  `POST`, `PUT`, the rebate-inbox confirm and the new CSV importer now run **one** guard, so the
  bulk path cannot be weaker than the single-row form. Proven a pure refactor rather than asserted:
  35 recorded request/response cases plus the resulting ledger and balances reproduce **byte for
  byte** against the pre-change tree, and that matrix is now a permanent contract test.
- **Injection is now a stated convention, not a one-off** (`.claude/rules/architecture.md`).
  `portfolio/cash.py`'s pool arithmetic reaches `data_ingestion` through a required callable bound
  by `api/routers/cash.py::cash_pool_fn` — D17's shape, one binding per import rather than per row.
  D39's objection to injection was to an **optional** registration; a required parameter makes a
  missed binding a build failure instead of a silent loss of the guard.
- **An undocumented `data_ingestion → portfolio` edge is now authorised and guarded.**
  `validate.py` and `corporate_action_import.py` import `build_book` / `Book` so the four
  corporate-action rejections can read a replayed book. Added by W2/W4, it reached 2026-08-12 with
  no diagram entry and no guard — the exact F-01 class the architecture rules warn about, and it
  surfaced only because the cash-movement work declined to copy it. Together with the existing
  `portfolio → data_ingestion` it is a **package-level cycle**, dormant only because
  `portfolio/cost_basis.py` and `portfolio/results.py` import nothing above `shared/`. Nothing
  enforced that until now: `tests/architecture/test_layer_edges.py` asserts the allowlist, that the
  allowlisted imports are actually present, and — the load-bearing one — that those two modules
  stay leaves.
- The `tests/contract/test_import_template.py` round-trip guard kept its **own copy** of the five
  preview builders, so "re-parses through the real preview builder" was quietly untrue. It now
  imports the production registry.
- Fixed: the CSV preview table stamped a red 「賣」 chip on every non-transaction row (fx, openings,
  corporate actions — and would have on cash deposits).

- **E23's one-click convert-to-SPLIT** (spec §5.1 / D22). The `identifier_change_suspected` warning
  now carries its own repair: the corporate-action preview attaches the exact converted row — a
  SPLIT on `to_symbol`, the identifier retired into `note` per §3.4, ratio unchanged — and the
  shared entry form renders it as a one-click that rewrites the form and re-runs the preview. It
  never writes directly, so the owner reads the resulting shares, the corrected average and
  成本不變 ✓ before committing. `to_symbol` survives rather than `from_symbol` because a SPLIT on
  the identifier would re-express an empty price series (E23's own condition is that `from_symbol`
  has no stored prices) and leave the real security in pre-split terms — a repair that repairs
  nothing. Where the ledger records the position under the identifier rather than the ticker the
  converted row would fail E1a, so the API **validates it before offering it** and returns the
  reason plus the real fix instead of a button that ends in an error.

### Fixed
- **The shared modal could not be scrolled at desktop widths, so its primary button was
  unreachable.** `.modal` carried `max-height` + `overflow-y` **only** inside
  `@media (max-width: 760px)`, while `.modal-backdrop` is `position: fixed` + `align-items:
  center` — so any dialog taller than the window overflowed **both** ends with nothing to scroll.
  Measured on the tallest dialog in the app (補登公司行動, whose preview grows one block per
  holding account): 1,089px in a 1280×720 window, top −184.5px, computed `overflow-y: visible` /
  `max-height: none`, 登錄公司行動 184px below the fold — **the owner could not save a corporate
  action on an ordinary laptop.** The cap now lives on the base rule; the narrow-width rule keeps
  its tighter mobile value. Not corporate-action-specific: every shared dialog had it. The
  corporate-action e2e no longer forces a tall window, so all nine flows now measure the feature
  at 1280×720 and the whole file is a standing guard.
- **Door 2 opened the repair form underneath the drawer that launched it.** `.modal-backdrop` was
  z-index 60 against `.sd-backdrop`'s 70, so every control overlapping the drawer was inert — and
  because the interceptor was the drawer's own *content*, a click produced nothing at all: no
  error, no cue. Modals now sit at 72, above the drawers and below the search palette and toasts,
  with the whole overlay ladder written down beside the rule — the bug existed because it was not.
  Escape follows the same rule (capture-phase, so it no longer reaches through an open form to
  close the drawer under it). Rejected: dismissing the drawer as the form opens — door 2's whole
  argument is that the repair is offered *where the evidence is*.
- **Door 1 — the feature's primary door — was unreachable on the ordinary path.** The manual form
  renders its own 賣超 acknowledgement inline and gates 確認寫入 on it; ticking it makes the commit
  carry `ack_oversell: true`, which is exactly what stops the 422 the three-option dialog is wired
  to. So on the path every owner actually walks, **the owner ticked a box, the position's cost
  basis was discarded permanently, and 補登公司行動 was never offered.** The repair is now offered
  inline *above* the tick, through the same prefill the dialog uses so the two cannot drift. The
  tick remains — a declared short or a deliberate oversell is still the owner's call — but now
  names its consequence instead of reading 「我了解，仍要寫入」. Both additions are gated on the
  issue code, because that same box also carries `cash_overdraft`, `future_trade_date` and
  `duplicate_trade`: ungated, a cash overdraft would have grown a corporate-action repair button.
- **The corporate-action preview double-counted the sell it was judging.** `_unblocked_sells` read
  the covering position off a ledger that **already contains** the sell, testing `Q > H − Q` where
  the guard it mirrors tests `Q > H`. A legal 900-of-1,000 sell was announced as 「目前為賣超」,
  and in §1's own scenario (100 shares, an oversold 400, a 7-for-1 that legalises it)
  `400 <= 700 − 400` is false — so 「✓ 這筆行動會讓…賣出通過檢查」 stayed silent in the exact case
  it exists for. Both counts now add the sell back, which is *exact* rather than approximate
  because `EventPriority` evaluates a same-day action strictly before a same-day sell. That ✓
  sentence is asserted for the first time.
- **A spec'd repair that was never wired.** E23 shipped with the warning only —
  `identifier_change_suspected` appeared nowhere under `web/` or `portfolio_dash/api/`. A contract
  test now asserts every field of the repair is consumed by the shared form, including the branch
  that renders it. An earlier, weaker version of that guard stayed **green** with the branch
  disabled — every field name was still present in the file — so it was tightened to pin the
  condition and the handler, and only then went red.
- **Price SERIES reads are expressed in one share denomination** (spec §5.1(d2), W6c / D42).
  `get_price_history` returns closes **as traded on their own dates**, so a SPLIT inside a read
  window left the older points in a different denomination and every cross-date comparison over
  them was wrong by the whole ratio. Twelve `api/` readers now route through the new
  `portfolio/price_basis.series_in` (which calls the existing `price_in` per point — one owner,
  no second formula): the technical rule engine (momentum / MA cross / RSI / 52-week / MA200),
  the market-risk alert feed, the daily digest movers, the DRIP reinvest-price estimate, the
  insight evaluation window and per-symbol prompt context, the prompt preview, `spark_30d`, the
  watchlist day-change, the TWR benchmark overlay, and the symbol drawer's price chart. Worst
  case fixed: a 7-for-1 put a symbol at its 52-week low and **fired a real drawdown notification**
  for an 86% collapse that never happened.
- Where such a reader also used a live quote or a stored scalar **in the same computation**
  (`alert_inputs`'s target-cross price, the watchlist's `last`, `insight_service`'s
  `price_at_create`), that value is re-expressed into the **same** valuation day. Correcting one
  leg only would have *manufactured* the discrepancy rather than removed it.
- **The DRIP inbox's reinvest price is money of record, and was wrong across a split.** It divides
  the net dividend into a share count that `confirm()` writes to the dividend ledger, so a
  carried-forward pre-split close booked **one seventh** of the shares actually reinvested on a
  7-for-1. Now expressed in the pay date's share terms.
- **The canonical transaction CSV can express a declared short sale** (`short_sale` column). The
  engine has carried the flag since v0.1.25, but the import template had no column for it, so
  **every imported declared short became an ordinary sell** — which the 賣超 guard flags as an
  undeclared oversell and whose cost basis it then discards, stickily. Found by §10.5's acceptance
  run against a real broker export. The column is optional and never inferred: absent means
  `false`, because inferring a short from an oversell would turn a data-entry slip into a
  plausible-looking realized loss.
- **`scripts/verify_corporate_actions.py` — the acceptance gate's own privacy guarantee was
  incomplete, and its missing-row count double-counted.** (1) The module argued that no `Decimal`
  reaches stdout, which is true and insufficient: a *symbol* is a free string and a broker writes
  option contracts as `TICKER MM/DD/YYYY STRIKE C|P`, so **the strike printed**. Symbols are now
  masked — masked, not rejected, because dropping a supplied row would change the verdict to
  protect the output. The mask keys on whitespace or `\d\.\d`, never on "contains a digit": TW
  (`2330`, `0050`) and MY (`3182`) tickers are numeric and a digit rule would blank most of the
  report on two of the app's three markets. (2) The count excluded a supplied-but-rejected row
  from the oversold population by checking the **written** rows — and a rejected row is never
  written, so the exclusion could never fire and the same missing row was counted twice. (3) The
  verdict now reports the two counts separately (`FAIL — 標的 n、缺漏 m`) instead of summing them:
  the missing rows are the *cause* of the failing tickers, and the halves use different
  denominators (per symbol vs per account×symbol). **The PASS criterion is unchanged.**
- The transactions CSV column set is stated in three places (the parser constant, `web/input.js`'s
  dropzone hint, `web/trades.html`'s paste placeholder); the latter two now derive from the first
  in a contract test, proven by breaking the hint and watching it go red.

### Notes / known limitations
- `ActionIndex` is built from **recorded ledger actions**, so a symbol nobody holds — notably a
  **benchmark** — is not re-expressed even if the real instrument split. There is no independent
  split feed, and inferring one from a price gap is forbidden (`domain-ledger.md`) and would
  silently rewrite market history; the repair is a recorded action. The discontinuity is left
  **visible** rather than guessed away.
- `volume` keeps the provider's basis (D39b — it has no `*_raw` column, so a factor on it could
  never be restated or reversed), so the volume-confirmation signal still spans two denominations
  across a split.
- **D45 — the whole-account IRR (D36) is retired, and D12's blind spot is standing again.** D36 was
  approved on 2026-08-10 and never implemented; the owner retired it on 2026-08-11. Its only
  load-bearing consequence was the wording of one limitation, and that wording was wrong in a way
  worth naming: the accounting manual said the reorganisation fee is "invisible to XIRR by design
  but visible in the whole-account IRR (pending D36)". With D36 gone, the fee is invisible to
  **every** return metric this system has, and no resolution is planned. Corrected in five places —
  spec §3.3, §7.5 and §8, and both manual mirrors (§4.4.7 limitation 2 and the §7 XIRR flow note) —
  and the sentence is *deleted* rather than softened to "pending", because a manual promising a fix
  that never arrives is worse than one stating the blind spot plainly: the reader stops looking for
  the workaround. **No figure moved** — XIRR never included cash movements — so every historical
  number, worked anchor and oracle expectation is unchanged. Manual → `v1.7a`.
  **Sixth place, found 2026-08-15:** `docs/spec/2026-08-06-broker-import-backlog.md` §P2b was where
  D36 was *originally recorded*, and the propagation never reached it — so the file still called the
  metric **CHOSEN** and still instructed the manual to write D12 as resolved by it, four days after
  the manual had correctly stopped doing that. There is the shape of the failure: the correction
  swept the documents that *quoted* the decision and missed the one that *made* it. Owner
  instruction 2026-08-15 — in a **backlog**, delete the retired design rather than strike it: a
  rejected plan left standing reads as work waiting to be picked up. The struck record with its
  original reasoning stays in the corporate-actions spec §8, which is where a decision belongs.
  Closed independently by **D1=A** (2026-08-13), which excluded all seven cash kinds from XIRR after
  the interest and fee rows actually existed.
- **D44 — RULED 2026-08-15, option (b): ask once, at entry.** The owner-entered target band
  (`instruments.target_low` / `target_high`) is still **not** re-expressed across a SPLIT, so a
  stale band meets a re-expressed price (W6c) and `target_cross` crosses on the split date and
  every scan after it — on the **notification** path, which is what made this worth a mechanism
  rather than a docs note. Option (a) was rejected on this codebase's own terms: the band has no
  `*_raw` column, so an in-place rewrite is the irreversible restatement D39b already refused for
  `open`/`high`/`low`, and it would still be *guessing* — 「alert me at 600」 may survive a 10-for-1
  as 60 (a view about the company) or as 600 (a view about the share price). Recording the SPLIT
  now raises the soft `target_band_predates_split`, **quoting the restated number**, and the form
  offers it as a checkbox that **defaults to OFF** — doing nothing keeps what the owner typed,
  which is the (c) behaviour retained as the floor. **New column `instruments.target_set_at`**
  (nullable, additive) is what makes the finding *precise* rather than noisy: it dates the band, so
  a historical split imported from years of broker history — against a band set last week — stays
  silent. Without it the one-click broker import would warn on nearly every action it carries, and
  「a guard that mostly cries wolf trains the owner to click through」 is E23's own argument. `NULL`
  (every pre-existing row) means *unknown* and makes no claim.
- **D47 (new, owner ruling 2026-08-15) — an EXCHANGE carries the target band to the new ticker.**
  「ticker更換簡單一些可以直接換名字就好，其他都不動」: the band **moves** (source cleared) and
  its values are **not** restated. A move rather than a copy is the load-bearing half —
  `target_cross` fires for every *registered* symbol with a band, held or watched, so one left
  behind keeps alerting about a ticker that has stopped trading. A destination that already carries
  the owner's own band is never overwritten. Announced in the preview before saving; ⚠ **not
  reversible** — deleting the EXCHANGE does not move it back, which is stated rather than
  engineered around, because the band is a setting and 重算 never covered it.
- **D48 (new, owner ruling 2026-08-15) — a SPINOFF auto-registers its child, and can seed its
  first price.** E10's hard rejection of an unregistered destination is **narrowed to SPINOFF
  only**: that symbol does not exist until the very event being recorded, so "register it first"
  asked the owner to pre-create it. Market and quote currency are **inherited** from the parent
  (E11 requires them to match, so it is a derivation, not a guess); name and sector stay blank.
  **Soft, never silent** — D19/E10 exists so a broker CUSIP never becomes an instrument, so the
  owner is shown exactly what will be created. A source symbol and an EXCHANGE destination both
  keep the hard rejection. The form also gained an optional **子公司起始價**, written through
  `pricing.store.upsert_prices` from `api/` (`pricing/` owns every price write): one unpriced
  holding blanks the **whole** portfolio's XIRR, and a spin-off is guaranteed to create one. A
  malformed or inapplicable value is **refused**, not dropped.
- `shared/money.cap_dp` — one home for the cap-not-pad rounding that `pricing/store`,
  `data_ingestion/store` and now `shared/corporate_actions.apply_ratio_to_price` had each written.
  Cap-not-pad matters here specifically: `quantize` would store a restated `60` as `60.0000`,
  equal to Python and different to the byte comparison D38 invariant 3 and every golden payload use.
- A ledger with **no** corporate action reads byte-identically: `series_in` short-circuits
  structurally rather than computing an equal answer, proved by a `split_factor` landmine driving
  every re-expressed path (D38 invariant 1).

### Changed
- **`LedgerBundle` — one argument for every ledger replay** (corporate-actions spec W0 / D9;
  **pure refactor, no behaviour change**). `build_book` and `daily_value_series` took one
  positional argument per ledger, at eight sites; adding a ledger meant editing all eight, and
  *missing* one is silent — a book without a ledger still builds. They now take a single
  `shared.models.ledger.LedgerBundle`, loaded once by `data_ingestion.store.load_ledger_bundle`.
  The `Stored*` → ledger-model conversion, previously copy-pasted at five sites (dashboard,
  what-if, tax package, 重算, ledger-edit revalidation), lives only in that loader; the trend
  replay's per-day cut is `bundle.through(day)`; the unregistered-symbol skip-set is
  `bundle.unregistered_symbols` / `.without_unregistered()` instead of being rebuilt from three
  lists at each caller. Verified by the full suite plus a byte-identical
  `tests/golden/dashboard_full.json` — for a refactor, any moved number is the bug.
- **`shared/ledger_registry.py` — the ledger table catalogue, declared once.** Four modules
  enumerated the ledger tables by hand (`db_stats` labels, the export zip + CSV tabs, the Moomoo
  account merge, `scripts/merge_reconcile.py`), and every failure mode of a missed entry is
  silent: an account merge orphans rows on the dead account id and the reconciliation still
  reports PASS. All four now derive from one declaration. A new account-scoped ledger table in
  `data_ingestion/schema.py` that is not registered fails a named test
  (`tests/shared/test_ledger_registry.py`), proven to fire by removing `cash_movements` and
  watching exactly that assertion break.
### Planned
- **Corporate actions (SPLIT / EXCHANGE / SPINOFF) — P0, in progress.** A broker-export coverage
  assessment (2026-08-06) found this to be the only *blocking* ingestion gap: a share count that
  changes with no cash cannot be recorded, so the later sells trip the STICKY 賣超 guard and
  **discard that position's cost basis permanently**. Spec: `docs/spec/2026-08-06-corporate-actions.md`.
- **Broker-import backlog (P1a converter · P1b US cash dividend · P2 interest/fees · P3 options)**
  recorded in `docs/spec/2026-08-06-broker-import-backlog.md` — deferred, each with its code
  reference points and the owner decisions that gate it. P1b and P2b are decision-blocked, not
  effort-blocked.
- **Unified auto-import principle:** the manual ledger is the source of truth; data-source data
  (FinMind dividend/ex-div, Schwab transactions) is matched to holdings and offered for a
  **user-confirmed** auto-import into the ledger following the account's accounting rules —
  cutting manual entry, never bypassing confirmation, never double-counting (calc reads only the
  ledger), `original_cost` never overwritten; **manual entry always retained**.
- `data_ingestion/` confirmed auto-import (future): match `pricing/`'s fetched dividend/ex-div
  events (and Schwab transactions) to the holdings list → prompt "new distribution detected —
  auto-import?" → on confirm, write a ledger entry per the account's dividend model (TW cash →
  cost reduction, US DRIP $0-cost, MY cash). `web_ui/` provides the prompt UI.
- `llm_insight/` prediction self-tracking + backtest loop (future sub-project): the LLM
  records each recommendation/forecast, later replays and scores its own past predictions
  against realized outcomes, accumulating a per-prediction confidence index and a
  corrective feedback loop that informs future advice. Gets its own brainstorm at the
  `llm_insight/` stage.
- `llm_insight/` insight inputs & per-stock prompt (future): per-holding decision signals from
  FinMind (財報 / 月營收 / 法人 / 融資券 / PER-PBR / news URL) plus **US sentiment indicators —
  CNN Fear & Greed Index and VIX** — as buy/sell context. **Prompt architecture (decided
  2026-06-08):** one editable **default system prompt** (ships as a Claude-recommended best prompt; user
  fine-tunes in config) holds the output contract + invariants (JSON schema, no
  numbers-of-record, batch-only) and is immutable by overrides; reusable, named
  **Strategy Prompts** (the library ships with several Claude-generated optimized templates;
  users can add their own) add a per-type analytical focus, and each stock's Strategy is **blank by
  default**, optionally **selecting 0..1** from the library (per-stock assignment — option A; data model pre-reserves tag/category binding for a
  later upgrade). All prompts live in the settings (config) page, versioned and folded into the
  cache fingerprint + self-backtest attribution (per `llm-insight.md`).
- **AI cost-info + LLM settings page** (`web_ui/`, future): the **backend is now built** (model
  registry, four role-defaults, USD budget governance, `llm_usage` log + cost calc, vision plumbing —
  see Added). Remaining is the `web_ui/` page: usage stats + history-trend + per-model cost charts;
  model add/edit (provider / endpoint / key / vision / pricing); role-default pickers; budget
  set/reset; and the screenshot-upload widget for vision (statement → draft → confirm).
- **Design principle (all modules):** invest in adjustable structure — config-driven behavior,
  provider/strategy protocols + registries, swappable adapters, decoupled layers — so future
  changes are config edits + small additions, not rewrites; keep YAGNI on features/scale (per
  `stack.md`), deferring concrete specifics until real use surfaces them.
- **Per-user dataset management (future):** the earlier folder/dataset-switching idea is deferred
  and reframed as a **multi-user** feature — different users each independently manage their own
  dataset(s) within one deployment. (The current prod/test split is achieved by **separate
  instances** — own checkout + venv + data folder per instance — not by switching datasets on one
  site; see `engineering-process.md` → "Two-environment loop-engineering".)

**Full-site interaction sweep remediation (2026-08-27)** — report
`docs/audit/2026-08-27-full-site-interaction-sweep.html`. A hands-on pass over all 18 page
entry points in a real browser: 504 presses on 340 distinct named controls (974 distinct
controls in the resting DOM), 188 of them producing an `/api/*` call, 19 downloads opened and
content-checked. Its premise was that the 2026-08-03 wiring sweep (2,287 controls, "0 dead")
proved listeners EXIST and not that they DO anything — which the export-centre cards had since
demonstrated. 17 findings; all fixed here, each pinned by a test proven RED first. One
observation (F-18) was re-checked and DISMISSED — see below.

- **F-01 (high) — the sell preview printed the PRE-trade average as the post-trade average**
  on the declared-short and undeclared-oversell branches. `web/input.js` was handed no
  `new_*_avg` on a SELL and stated the omission as a fact — "a SELL leaves the averages
  unchanged" — which is true of the ORDINARY branch and of no other: a declared short leaves a
  SHORT lot based on the proceeds received, an oversell DISCARDS the basis, and a full exit
  leaves no position at all. Measured: preview 300.43 → 300.43 where the ledger books 308.63,
  and 「240.00 → 240.00」 printed directly above the card's own warning that the basis was about
  to be permanently discarded. ⚠ This is the SAME rule the 2026-08-24 review fixed one column
  over: that wave pinned `preview.realized` against the booked realized row for all three
  branches and left the average pair projecting a single happy path. `_position_preview` now
  projects the position TOTALS per branch and divides on read; `whatif.py` does the same for
  the drawer, which has no 放空 declaration to read and so states the fork and gives no figure,
  exactly as it already did for the realized amount. The parametrised guard now asserts BOTH
  columns against `build_book` for all six shapes.
- **F-02 (high) — one acked oversell blanked every symbol's 試算, and the reason was thrown
  away twice.** `compute_whatif` builds the book strictly on purpose, and its own comment
  records that the 2026-08-11 repair was "to degrade with the reason" — only the 500-to-400
  half had shipped. `OversellError` now carries the offending account / symbol / date as
  REQUIRED kwargs, `WhatIfError` carries `field` + `issues` so the router stops stamping every
  case with an unrelated `field="account_id"`, and the drawer repeats the server's sentence
  instead of a bare 「試算暫不可用」. Owner option A: the strictness is unchanged.
- **F-03 (med) — 「確認寫入勾選列」 ignored the ticks and wrote the whole paste.** Measured:
  3 rows pasted, 2 unticked, 3 written, banner 「成功 3 筆」. A step worse than the export
  buttons that reported success and did nothing — this wrote ledger rows the user had removed.
  `commit_preview` has always taken an `accept` set (its comment already reasons about "a row
  the caller deselected"); it simply had no caller. `/api/import/commit` gains an optional
  `select` of row INDICES — not a re-rendered CSV, because `csv.DictReader` row *n* is not text
  line *n+1* once a quoted field contains a newline, and this frontend has no CSV parser.
  Omitting it still writes everything, so the AI door and undo are byte-compatible.
- **F-04 (med) — `window.pdAfterLedgerChange` was never defined anywhere.** Called from two
  places in `broker-import.js`; the defensive `if (window.x)` idiom turned the wrong name into
  a silent no-op, so an undo toasted 「已復原 刪除 3 筆」 over a table still listing all three.
  The real seam is `pdLedgerRefresh`. A new contract test scans every `window.pd*()` call in
  `web/` against its assignments — the class of typo that is invisible to a listener count.
- **F-05 (med) — the symbol picker could not be driven from the keyboard.** Rows are bound on
  `mousedown` (deliberately — it beats the input's `focusout`), and `mousedown` is not what
  Enter fires, so typing filtered the list and nothing could then choose from it. ArrowUp /
  ArrowDown / Home / End / Enter added; the pointer path is untouched. Arrowing uses
  `ensureOpen()`, never `open()`, which would clear the filter the user just typed.
- **F-06 (med) — the rebalance footer said 100.00% over fields that summed to 100.1%.** The
  inputs were `toFixed(1)` views of full-precision weights while the footer total and the plan
  POSTed to `/api/rebalance/preview` summed the unrounded state, so 匯出執行報告 computed a
  plan the owner had never seen. Owner option A: seed the state THROUGH the display precision,
  so the two agree by construction and the existing over-100% warning fires when it should.
- **F-07…F-17 (low), each with its own regression:** the AI 「重試」 button had no listener at
  all and it is that card's only action (the two sibling degrade cards each offer a way out) ·
  the variable-table 「複製」 fired a success toast unconditionally because `clipboard.writeText`
  is async — the run's ONE uncaught exception, beside a green ✓ · the 「部分過期」 chip scrolled
  to a `<details>` it left collapsed · four of sixteen alerts rendered as `href="#"`, and they
  were exactly the portfolio-level risks (`sector_weight`, `fx_drift`, `portfolio_drawdown`,
  `currency_weight`) — each now lands on the panel showing the number it is about, guarded by
  an AST test that no `Alert(...)` is built without an href · 「產生洞察」 destroyed its own
  explanatory toast with a same-tick navigation · 「閒置」 named two different judgements on one
  screen (five cards stamped 閒置 behind a 閒置 filter matching none of them) · B's degradation
  blamed 「缺價」 for a 賣超, while XIRR two rows above named it correctly — `incomplete` has
  five causes and the reason named one · the insight wizard was the only overlay in the app
  that ignored ESC · fee reset was the app's only native `window.confirm`, and it is the
  control that changes what every future trade costs (now `confirmDialog`, stating up front
  that history keeps its own fee snapshot; a contract test bans native dialogs) · the oversell
  block quoted the position's FINAL NET quantity — 「部位將為 9.5 股」, a positive number offered
  as proof of a shortfall — where the date-aware guard knows the day: the replay now records
  `oversold_on` / `oversold_sold` / `oversold_held` and the message names it · the 加值 guard
  turned a field red and said nothing.
- **Owner review of the remediation itself (2026-08-27).** Four calls put back to the owner
  because each deviated from the report's recommendation or went beyond it. Ruled: the 賣超
  preview shows the ledger's **zero plus a 「基礎已捨棄」 note** (the report suggested an
  em-dash; zero is what the holdings row will actually hold, so the projection agrees with
  the screen the user is about to see, and the note stops it reading as "your average cost is
  nothing"); F-03 **keeps the server-side row indices** rather than the report's client-side
  CSV rebuild; F-12 **keeps 待執行/已停用** rather than the report's one-line 尚未執行, which
  would have duplicated the timestamp cell beside it and left a disabled task labelled as
  merely not-yet-run. And the ruling table itself: **only AI-D52 is an owner ruling** —
  AI-D53/D54 are marked 〔記錄〕, because a blanket 「fix them all」 accepts WHAT is done and is
  not a decision about how the implementer categorises it. The table's preamble now says so,
  or the marker would read as a typo.

- **F-18 — DISMISSED, nothing changed.** The sweep read a `DELETE /api/instruments/2330 → 422`
  as 永久移除 being pressable while the dialog said it was impossible. Re-checked: that 422 is
  the HIDE path (`DELETE`; purge is `POST …/purge`) refusing a HELD symbol with code `held`,
  which `instruments.js` catches by name to raise its 「無法移除」 dialog. 永久移除 is genuinely
  disabled whenever the warning shows — the type-to-confirm input that alone can enable it is
  not created on that branch. A test pins the two API facts so the next reader need not
  re-derive them.
- **Payload / spec impact.** `tests/golden/dashboard_full.json` moves by exactly TWO lines —
  `sector_weight` and `fx_drift` gain the hrefs they never had; nothing else in the snapshot
  changes (the other two href-less rules do not fire in the golden fixture). `_Position` gains
  three locator fields, so `docs/spec/2026-08-06-corporate-actions.md` §4.4 — the NORMATIVE
  field-transfer table — gains a row for each, stating that they are not transferred and are
  unreachable (E3/E22 refuse any action on a position that has them set) plus the warning that
  the two quantities are in PRE-action share terms and must never be carried forward if those
  guards are ever relaxed. That table's own count guard caught the addition and refused the
  change until the rows existed; it also surfaced that the `_apply_action` docstring still said
  "nine fields" while the table listed ten, stale since `vacated_to` landed. `.claude/rules/
  domain-ledger.md`'s preview rule is widened from `preview.realized` to every projected column.

- **A gate claim from `9fcc832` was wrong, and the tree carried a real error.** `mypy` was
  reported clean at 665 files; re-run with a COLD cache it found an existing type error in
  `tests/portfolio/test_review_r7_b_takes_cash_kinds.py` (a decomposition summed over
  `Decimal | None`). The earlier run was reading a stale incremental cache. Fixed, and the
  assertion is now stronger — `is not None` first, so a never-computed figure fails as itself
  rather than as a value mismatch. This is the second over-read of a gate in this programme
  (the first was counting pytest result characters); both are in `LESSONS_LEARNED.md`.

**Programme close-out, owner rulings 2026-08-27 (AI-D55…D58).** The owner reviewed every
remaining open item at once and ruled: hold on shipping, remove two things permanently, add
two, and hand two back as blocked-on-them.

- **二代健保補充保費 — permanently excluded, term removed (AI-D55).** It was never
  implemented: `FEE_RULES` and the dividend model carry no rate, threshold or field for it,
  and `dividend_model.py` already said "owner decision 2026-07-26: out of scope". What this
  ruling changes is the STATUS — from "paused, may return" to "excluded, will not" — and the
  term is now gone from live code and design docs (four call sites, all explanatory). The
  explanation those mentions were carrying is kept, generalised: the dividend gate does NOT
  demand `gross − withholding == net`, because a TW cash dividend legitimately nets less
  through levies this app does not model. That identity was genuinely proposed once
  (`LESSONS_LEARNED.md`), so the reason is written down rather than left to be re-derived.
  ⚠ **Dated records are untouched** — CHANGELOG entries, the lesson, and the 2026-07 audit
  reports say what was decided at the time, and rewriting them to erase a real decision is
  falsifying the log. The only forward-looking place the term survives is the exclusion
  ruling itself, which has to name what it excludes or the decision becomes unfindable.
- **MY finally has a benchmark: KLCI (`^KLSE`).** The blocker was real and was documented in
  `benchmarks.py`: `yf_symbol` appends the market suffix unconditionally, so `^KLSE` with
  `Market.MY` would have been fetched as `^KLSE.KL`. Re-derived: **a `^`-prefixed Yahoo
  ticker is an INDEX and carries no exchange suffix in any market.** That is not a guess
  about `^KLSE` — `pricing/index_source.py` has been fetching exactly those three raw tickers
  (`^TWII`, `^GSPC`, `^KLSE`) for the sentiment variable all along, bypassing `yf_symbol`
  entirely; the rule was already true of the codebase and only this function did not know it,
  with `^GSPC` never exposing the gap because the US suffix is empty anyway. MY `relative`
  predictions and the R4 counterfactual now have a yardstick. Safe before any price is
  fetched: `dashboard.py` only admits a market whose converted closes are non-empty, so MY
  stays honestly `uncovered` until the daily job has run. The `None` degradation path lost
  the market that used to exercise it, so it is now pinned by monkeypatch instead of deleted
  — code no test exercises is code that stops working quietly.
- **W8: `open` / `high` / `low` get the same two-column basis as `close` (AI-D58).** Each
  gains a `*_raw` and is stored as `cap_4dp(raw × split_basis)`, with the raw un-capped
  because it is the input the reconcile recomputes from. **This supersedes D39b**, whose
  objection was precisely that a factor on a column with no raw "could never be restated or
  reversed" and whose own escape clause was "or add its own raw columns". Until now a row
  could carry a post-split close beside pre-split highs and lows — harmless only because
  nothing read them (verified again: the sole statement naming those columns is the INSERT),
  and a trap for the first candlestick chart. The reconcile restates all four from their own
  raws, so a SPLIT inserted and then deleted restores the row byte-identically — pinned,
  because `prices` is the only place this feature writes outside the ledgers and 重算 does
  not rebuild it. ⚠ **`volume` is still never multiplied**, now for a stronger reason than
  the old OHLC one: it is a count, not a price, so the price factor would be actively wrong.
  ⚠ The three new columns sit AFTER `split_basis` in the DDL, which reads oddly, because
  `ALTER TABLE` can only append and a guard demands the fresh and migrated schemas agree
  column for column.
- **Permanently excluded, not deferred (AI-D56):** portfolio-scope `relative` / `volatility`
  scoring (it needs a blended-benchmark ruling the owner declined to make) and the
  `_ratio_str` / `_avg_str` HALF_EVEN inconsistency (accepted as-is; aligning it would move
  the last digit of `miss_rate` and several other displayed values). Both leave the backlog.
  The difference from "deferred" is that a deferred item comes back as a question next round.
- **Held on the owner, not on the code:** the ETF-flag audit found nothing to confirm because
  the local ledger is empty (0 instruments, 0 transactions) — it is meaningful only against
  real TW rows, and running it against demo/prod is a VM operation needing approval. The W4
  live extraction baseline needs an API key that is not configured.
- **A count correction:** this branch is **76 commits** ahead of `main` and of `v0.1.28`, not
  the 13–15 quoted earlier in the programme. That figure was this sub-programme's own commits,
  not the branch's divergence, and it understates the size of an eventual single deployment.

**The stress harness now reconciles a PAST-DATE share count (AI-D59).** The last disclosed gap
in `scripts/stress_audit/README.md` §6 is closed, and closed with a measurement rather than a
claim.

- **What was missing.** AI-D51 gave the harness a daily replay and the `trend.*` family, but
  `trend.total_value` could only be asserted from `ASOF` onward: `seed_all` seeds exactly ONE
  close per symbol, dated `ASOF`, so every earlier day was unpriced by construction and
  therefore `incomplete` — and skipped. That is precisely where a defect confined to a window
  lives, which is R6's ex-date: a 配股 booked on the payment date instead of the ex-date changes
  the share count only BETWEEN those dates, and ends at the same position either way.
- **What closed it.** `_ensure_daily_prices` seeds a close for every fixture symbol on every day
  from the first ledger event through `ASOF`. At a known price, `total_value` pins the share
  count — the app and the oracle read the same close, so any surviving disagreement is a
  quantity. The closes are **as traded on their own date** (`oracle.price_as_traded` multiplies
  the ASOF quote back out by the intervening splits, one action at a time with the division
  last, never a product of quotients), and each row is stamped `fetched_at` = its own date so
  that both the write window `(as_of, fetched_at]` and the read window `(priced_on, day]` are
  empty — nothing re-expresses these rows, so inserting or deleting a SPLIT can never repaint
  the history the family asserts against. Seeding is derived from `facts` inside `reconcile`
  before the dashboard is fetched, so it self-corrects across checkpoints.
- ★ **Detection power, measured.** The R6 defect was deliberately reintroduced into the ORACLE
  (`facts_through` cutting dividends on the payment date — exactly the pre-R6 behaviour) and the
  harness run both ways. **Pre-change harness: `pass=5663 fail=0` — silent.** Current harness:
  `fail=53`, every one a `trend.total_value`, every one dated **2026-05-02 … 2026-05-29** (the
  ex-date-to-payment window), each off by 70,000 TWD — the value of the 配股 shares that should
  exist in that gap. The pre-change row reproducing the previously recorded baseline *to the
  digit* is itself the evidence that the probe restored the old state faithfully: that `fail=0`
  was never coverage, it was blindness.
- **New baseline: `ops=128 pass=6075 fail=0`** (was 5663). `ops` is unchanged and must stay 128
  — this was closed with fixture data and assertions, not a new route.
- ⚠ **Still reviewed rather than reconciled:** that a 配股 attaches on the ex-date while a cash
  dividend does not is a reading of `domain-ledger.md` on BOTH sides, so two independent
  implementations of the same misreading would still agree. Only the hermetic hand-computed
  walk (`tests/portfolio/test_review_r6_ex_date.py`) catches that class, and it stays.
- Two fixture defects were found and fixed by the new family on its first run, both mine: 91
  `total_value` failures off by 0.0070 (the oracle valued at an unquantized repeating decimal
  the 4-dp price column cannot hold — the quantization now lives in the one expression that
  owns both the seed and the valuation), and 4 `trend.incomplete` failures from treating a day
  AFTER the seeded series as unpriced, when carrying the last close forward is a valid
  valuation and what the app does.

**R4's missing leg: the benchmark comparison is now visible to the prompt (AI-D60).** R4
shipped the counterfactual onto the dashboard and the drawer and never built the variable its
own plan specified — so the anchor existed and the narrator could not see it. No insight card
could say the portfolio beat or lagged the index, which is the one sentence R4 was built to
make sayable, and the four-lens review had called it the highest-value item in the programme.

- `benchmark_counterfactual_json` (36 → 37 variables, `position` category 6 → 7), fed from the
  dashboard's own `BenchmarkComparison` — a record-of-figure the LLM narrates and never
  recomputes.
- ⚠ **Wired into the WEEKLY PORTFOLIO report, not the per-symbol advice card.** The
  counterfactual is a portfolio-level number; on a per-symbol card it invites 「this stock beat
  the market」 from a figure about the whole book. `kpis_json` already carries a `scope_note`
  for exactly this trap ("a per_market card must not read them as this market's numbers"), and
  the same reasoning applies here. Weekly `v2.4` → `v2.5`, `LIBRARY_VERSION` →
  `official-v20`.
- ⚠ **The coverage red line lives in the DATA, not in the prompt's memory.** When
  `uncovered_ratio > 0` the variable attaches a `coverage_note` naming the uncovered markets,
  and the template is instructed to quote it verbatim and to avoid 「超額報酬」/「打敗大盤」
  entirely — `BenchmarkComparison`'s own docstring forbids the bare label in that state, and a
  variable that hands the model a bare `excess` invites the sentence the rule prohibits.
- **The ETF-flag audit ran against demo** (read-only, owner-authorised, via `vm_exec.py` so the
  operation log carries it). Demo sits at `v0.1.28-62-g4990b3b` — 16 commits behind this branch
  — so `scripts/audit_etf_flags.py` is not on that box and the audit's own question was run as a
  read-only query instead. Result: 18 TW instruments, `etf_flag_unknown` column ABSENT
  (confirming demo predates AI-D40's three-state fix), and one genuine suspect — `0056`
  元大高股息, a real ETF recorded as `is_etf=False`, with 0 sells so far. Nothing was written:
  AI-D40 forbids the program relabelling those rows, and prod's ledger is empty anyway.

**The ETF question is now asked at the door, and the weekly lens exists (AI-D61 / AI-D62).**

- **`is_etf` at entry, not after the first sell (AI-D61, owner's own proposal).** The manual
  door's auto-register path can now carry the answer: `ManualBody.new_symbol_is_etf`, surfaced
  as a checkbox beside the existing 「未註冊」 hint. The old path made the user find out later —
  register (unknown) → trade → sell → soft issue → go to 標的管理 → tick → re-check the rate.
  ⚠ Shown **only for TW and MY**, the two markets where the flag prices something (TW sell tax
  0.3% vs ETF 0.1%; the MY stamp duty is ETF-exempt); on a US trade it would be a control that
  never does anything. ⚠ Named `new_symbol_is_etf` rather than `is_etf` because the difference
  is the whole point: it is read at exactly one line — the auto-register call — so a REGISTERED
  instrument can never be relabelled by a trade. That is the 2026-07-15 stress-audit fix (an
  input flag beating the registry) and this field must not re-open it. AI-D40 is not relaxed:
  no answer still records unknown and still discloses `etf_flag_unknown` on the first TW sell.
  ★ Motivated by a measurement, not a hunch — the read-only demo audit found `0056` 元大高股息
  registered as `is_etf=False` with 0 sells: not yet wrong, wrong on its first sell.
- **W8's second half as a READ-ONLY LENS (AI-D62).** `technicals.resample_weekly` collapses
  daily closes to one per ISO week (the week's last close) at read time, feeding one new
  per-symbol variable `weekly_signals_json` (37 → 38) — MA over 10/20/40 **weeks** and 13/52
  **week** returns. It answers the question the daily-only stack cannot: 「日線轉弱，但週線
  還沒破」. ⚠ Grouped by ISO week, **never by taking every 5th row**: a four-session holiday
  week pushes the next Monday into the wrong bucket and the error compounds across a year of
  holidays rather than cancelling. ⚠ Nothing persists — `signal_history`'s key stays
  `(symbol, as_of)`, and TechScore / `alert_events` / the event-study backtest / `rules-v1` are
  untouched. A real second timeframe needs a timeframe dimension in that key plus a migration,
  because two timeframes under one key overwrite each other and a composite score would
  silently average two different questions; that is a W6-sized wave, not a close-out. Every
  key is named in weeks and the payload states its own `timeframe`, and the checkup card
  (v2.8 → v2.9, `LIBRARY_VERSION` → `official-v21`) is told the two timeframes may not be
  compared or written into one sentence — AI-D2's 「one name, two definitions」 applied before
  it can bite. Cited by a template on the way in, because W6 shipped three variables no
  official template referenced and they stayed dark until W7.

**W4's live extraction baseline exists at last — and measuring it stopped a regression the
obvious fix would have shipped (AI-D63).** The corpus was built so a prompt edit would be
measured rather than blind; it had never run, because it needs a real model and a real key. The
owner supplied a dedicated key on 2026-08-28. Thirteen runs of
`google/gemini-2.5-flash` via OpenRouter.

**Baseline: field hit rate 99.3–100% (best run 295/295), and the three silent-money fields
AI-D20 demands be reported separately are clean — cash `kind` 12/12, `short_sale` 24/24,
`daytrade` 24/24.** Known non-deterministic cases, listed rather than averaged away:
`div-tw-stock` ≈29%, `english-statement-fragment` (date) ≈23%, `unparsed-ambiguous-cash` ≈23%.

Three things the runs showed that no single run could:

- ★ **A harness defect wearing a model failure's clothes.** The first run read 34/40 and four of
  its six failures were `RUNNER ERROR: no such table: fx_rates` — the eval built its in-memory
  DB with `bootstrap_db` alone, which owns the LEDGER tables, while `prices`/`fx_rates` belong
  to pricing's schema. Every FX-touching case died: **all three MY cases plus the merged-account
  US case**. MY extraction was not poor, it was UNMEASURED. Fixed; evaluated fields 263 → 295.
- ★ **`daytrade` was a real defect and the prose rule could not hold it.** The prompt already
  said 「never infer it from two same-day opposite drafts」 and the model broke it on the exact
  scenario that sentence names — setting `daytrade=1` on a same-day round trip the user never
  declared, which halves the TW sell tax (0.3% → 0.15%). A **negative one-shot** — showing the
  shape that must NOT be produced — took it from **0/2 to 11/11**. Prompt `v7` → `v7.1`,
  `LIBRARY_VERSION` → `official-v22`.
- ★ **The same technique applied to `div-tw-stock` was measured to be a REGRESSION, and was
  reverted.** A STOCK one-shot fixed that case 6/6 — and taught the model to zero the `gross`
  on OTHER dividend types: `div-my-net` went from 0/4 failing to 4/6 failing, writing a real
  88.50 as 0. Trading 「a 配股's gross printed as 50」 (a field the replay does not read) for 「a
  MY net dividend zeroed」 is strictly worse, so the one-shot is gone and `div-tw-stock` is
  recorded as a known limitation rather than dressed up as fixed. **That is what the corpus is
  for**: it turned 「this fix is actually a regression」 into a visible fact instead of an
  undetected one.

⚠ Thresholds stay OFF. `--max-cash-kind-miss 0 --max-daytrade-miss 0 --max-short-miss 0` are
sound for a manual run; `--min-field-hit` is deliberately left unset, because pinning a
non-deterministic runner's default to one run turns the next run's noise into a false
regression. ⚠ The preview's 當沖 chip stays — a better prompt is not a reason to remove the
backstop that lets the user see and untick a flag that moves money.

## [v0.1.28] - 2026-08-09

A **share-reconciliation** release: the symbol drawer's 對帳 footer flagged a break that did not
exist, and printed the evidence at a precision too coarse to show why. Both halves are fixed —
the comparison and the display. Cut on its own, ahead of the corporate-actions work, so its
deliberate change to how share counts render cannot later be confused with a side effect of that
refactor.

### Fixed
- **Symbol drawer reported 對帳不一致 on a fully consistent ledger.** `activity_reconcile`
  compared the ledger flow with the book using an exact `==` between two Decimal sums built in
  DIFFERENT orders — `_reconcile` adds the reinvest shares to each other first, `build_book`
  folds each into a large running position. DRIP/STOCK reinvest shares are `net / price`
  quotients that do not terminate, so at the default 28-digit context the two orders disagree in
  the last digit (measured on the demo site 2026-08-05: **1E-26 shares** on AAPL). `balances` is
  now a DIFFERENCE test against `_SHARE_EPS` = `0.000001` (owner ruling 2026-08-06: share counts
  are ignored past the 6th decimal). Deliberately NOT quantize-then-compare: truncating both
  sides to 6 dp preserves the same bug class, since two values 1E-27 apart can straddle the 6-dp
  boundary and truncate to different results.
- **`GET /api/symbol/{symbol}/detail` → `activity_reconcile.{total,by_account}` now carries
  `diff_shares`** (the exact signed `net − book` gap, full precision on the wire). The drawer
  footer names it when the flag is red — a reported break without its size is unactionable.
- **Share counts are no longer rendered at 0 dp.** New `f.shares()` in `web/format.js` (up to
  6 dp, trailing zeros trimmed) is the single definition for every share display. The footer
  whose job is to prove 期初＋買−賣＋配股/DRIP ＝ 部位摘要 was printing a real 0.045712-share
  DRIP reinvest as `0`, so the equation read as perfectly balanced beside its own ⚠ — and a REAL
  sub-0.5-share break would have printed the same way. Four precisions previously coexisted for
  the same quantity (0 dp, 2 dp, 4 dp, and two hand-rolled "4 dp when fractional" branches)
  across the dashboard, drawer, ledger, inbox, rebalance and symbol picker; all 36 call sites
  now route through `f.shares`.
- No money-of-record change: `reinvest_shares` storage precision is untouched (capping it would
  move share counts and therefore average cost, market value, unrealized P&L, weights and XIRR).
  No stress-audit re-run is required for this release for the same reason.

### Tooling
- **`scripts/vm_exec.py` decodes remote output as UTF-8**, not the Windows locale codec — a
  remote command whose output contained Traditional Chinese came back mojibake in the operation
  log, i.e. the audit trail recorded something other than what the VM said.
- **Working-tree scratch is now git-ignored rather than merely remembered** (`.playwright-mcp/`,
  `invest-temp-noted.txt`, `ntfy_topic_tmp.txt`, `tpl.json`, and two design-handoff byproducts).
  These sat untracked-but-not-ignored across releases — one `git add -A` from being published,
  the same class as the raw broker exports ignored just before them.
- **`.claude/skills/demo-cycle/` is tracked.** The other three workflow skills already were, and
  `CLAUDE.md` lists `.claude/skills/` as a repository artifact, so this one was an asset that had
  simply never been committed.

## [v0.1.27] - 2026-08-05

The **0 → 1 sweep**: climb a fresh ledger one row at a time and measure at every rung. Every
fixture in this repo starts with history and the demo site carries 62 transactions across
months, so the states between "nothing" and "a working portfolio" had never been observed —
and a page that renders correctly at 0 rows and at 14 can still be wrong at exactly 1. That
is the state the owner enters on day one, on a prod instance whose ledger is currently empty.

Coverage: 21 rungs × (60 GET endpoints + 18 pages × 2 widths) = **1,260 endpoint calls and
756 page loads**, plus 54 read-only POSTs (every export/report builder) at both the empty and
the 1-of-each state. Zero 5xx, zero console errors, zero overflow, zero vertical clipping.
Everything below was found in what those pages *displayed*, not in whether they rendered.
**No money-of-record calculation changed** — every figure discussed here was already correct
on the wire.

### Fixed
- **A losing short position was displayed as a gain.** `web/app.js` still derived the
  holdings table's percentage client-side as `unrealized_pnl / adjusted_cost_total`. A short's
  basis is the proceeds received, i.e. negative by construction, so the ratio inverted:
  −75.98 USD of unrealized **loss** rendered as **`+3.17%`**, in the same cell as the loss.
  Audit H1 (2026-07-26) had already added the server-computed `unrealized_pct` — which divides
  by `abs(original_cost_total)` and carries the comment explaining why — and moved the *drawer*
  onto it; the main table was missed and kept the old divide for eleven days. The same divide
  was also wrong for a **long**: adjusted cost is legally ≤ 0 once cumulative dividends exceed
  cost (`domain-ledger.md`), which is the ordinary 已回本 case, and it divided by zero outright
  on a position built entirely from $0-cost DRIP shares.
  - *Visible change:* the holdings table's 未實現% is now on **original** cost, the same basis
    as 回本進度, the drawer, and the KPI 累計報酬率 — so the whole page reads on one basis. For a
    holding with dividends the figure shifts slightly (golden 2330: 133.42% → 132.42%).
- **A negative weight drew a full bar.** `.mini-bar .fill` declares no width, so as a block it
  fills its track; an *invalid* inline `width: -2.29%` is **discarded** by the CSSOM rather than
  clamped, and the element fell back to 100%. A net-short row therefore drew a bar
  indistinguishable from the largest holding while its own label read −2.29% (−176.80% on the
  golden fixture). Bar widths are now clamped to 0–100%; the label carries the sign, the bar
  only ever claims magnitude.
- **21 user-facing messages were English.** They are rendered verbatim — `web/input.js` puts
  `issue.text` and `row.reason` straight into the cell — so a first-time user, for whom every
  symbol is unregistered, met `unresolved 2330`, `quantity must be > 0`, `unknown account`,
  `from_ccy and to_ccy must differ`, and `no ledger events` (the last one on the owner's own
  production dashboard, which has no ledger yet). Not a decision but drift: inside a single
  `validate_transaction` call, `"quantity must be > 0"` sat four lines from `"股數過大,無法處理"`.
  All 21 are now zh-TW, reusing the app's established wordings (`帳戶 {id} 不存在`,
  `未註冊標的 {symbol} — 請先至「標的管理」註冊`).
- **Four job-run reason codes rendered as raw identifiers.** `web/pipeline.js` maps a reason
  through `SKIP_REASONS[code] || code`, so an unmapped code does not fail loudly — it prints
  itself. The `*_mid_run` codes (`budget_exhausted_mid_run` and the three `LLMError` kinds) had
  never been added, so a run that stopped part-way showed the owner an English identifier.

### Added
- **`tests/architecture/test_user_messages_are_zh_tw.py`** — every `Issue(message=…)`,
  `error_body(…)`, `*_reason` assignment, and (in `portfolio/dashboard.py`, which puts
  `str(exc)` on the wire) `raise KeyError(…)` must contain Chinese. Resolves module constants,
  so an f-string whose Chinese lives in a constant is not a false positive; exempts
  snake_case **codes**, which are deliberately English and are covered by the check below
  instead of by an allowlist that would need editing every release.
- **`tests/contract/test_skip_reason_coverage.py`** — every reason code the backend can emit
  has a Chinese label in the frontend, with the `*_mid_run` set expanded from the `LLMError`
  subclasses so a new subclass is caught the day it is added.
- **`tests/contract/test_frontend_never_computes_money.py`** — enforces the CLAUDE.md locked
  decision "the frontend never computes money or returns", which had been unguarded since
  2026-06-13. The money-field list is derived from the Pydantic wire models, not hard-coded.
  Run against the pre-fix tree it reports exactly the three real violations.
- **`tests/e2e/test_short_position_display.py`** — a *losing* short (both the right and the
  wrong formula agree in sign on a winning one) asserted from the rendered cell, not the
  payload; the payload was already correct, which is precisely why nothing caught this.

### Changed
- XIRR is no longer annualized over a window too short to annualize (owner ruling, < 30 days),
  and `.kpi-value` clips rather than letting any over-long value set the document width. Both
  were found on a freshly reset instance carrying one same-week trade, where a +131.7% book
  gain reported a 134-digit XIRR and pushed the dashboard 1,915px sideways.
- **Golden payload** (`tests/golden/dashboard_full.json`, the documented JSON contract)
  regenerated for the one field whose text changed: `freshness.xirr_unavailable_reason`.
  The contract diff is a single line and **no number moved** — which, together with stress
  audit phase 1 at an unchanged `77 ops / 1,806 assertions / fail=0`, is the evidence for
  "no money-of-record calculation changed".

## [v0.1.26] - 2026-08-03

Whole-site layout and control sweep. Three page-level horizontal overflows fixed, plus the
two independent reasons the existing guard never saw any of them. No money-of-record change.

### Fixed
- **資料來源: the source table pushed the DOCUMENT sideways from 761px to ~1,435px**
  (+648px at 768px, +159px at 1257px). `.ds-section { overflow-x: auto }` existed ONLY inside
  `@media (max-width: 760px)`; above that the condition is false and no other author rule
  declares `overflow-x`, so it computed `visible` and the ~1,200px table set the document
  width. 1440px was wide enough to hide it, which is why it survived 30 days. The table now
  sits in `.table-wrap` — the scroll container the rest of the app already uses — and the
  mobile-only rule is removed, because keeping both would nest two horizontal scrollers.
- **資料來源: the 市場報價抓取順位 grid overflowed 48px at 768px** — a SECOND defect that the
  table's larger overflow had been masking; it only appeared once the first was fixed.
  `settings.html` carried `style="grid-template-columns: repeat(3, 1fr)"` **inline**, which
  outranks every selector, so the `@media (max-width: 1100px)` two-column rule in the same
  file had never once applied. Only `styles.css`'s `1fr !important` at <=760px could beat it —
  exactly why phones looked correct and tablets did not. Column count now follows available
  width via `repeat(auto-fit, minmax(210px, 1fr))`.
- **洞察管線: the task card head pushed the page 50px sideways at 390px.**
  `.pp-card-head` is `display: flex` with no `flex-wrap` declared anywhere, so it took the UA
  default `nowrap` and seven children could not fit a 301px card. Wrapping is declared
  globally rather than under a media query: whether the row fits depends on the task NAME's
  length as much as the viewport, and wrap is inert whenever it does fit.

### Changed
- **The layout guard now sweeps all 18 shipped pages at 900/768/390** (was 9 pages at
  900/390). Both broken pages were among the 8 it never looked at, and 768 is not redundant
  with 900: the fallback grid fits 885px and does not fit 753px, so the old sweep stepped over
  that defect exactly.
- **A new guard test creates its own data.** 洞察管線 renders an empty state until an insight
  task exists, and neither the golden fixture nor `scripts/seed_demo.py` creates one — so the
  sweep walked a blank page and reported green while the live demo, with three tasks added
  through the UI, overflowed for 50 days. The test now creates a task through the app's own
  API on an isolated `flow_server`, deliberately at `level=fail` so the extra 為什麼沒跑？
  button renders and the assertion covers the widest the head ever gets.

### Added
- `scripts/vm_exec.py` — runs a command on the deployment VM **and** appends the entry to the
  append-only VM operation log in one step, so the audit trail cannot drift from what actually
  happened. Server-side UTC timestamps, secret redaction at the write seam, and `--log-only`
  for operations performed elsewhere. Host identity comes from git-ignored config, never from
  this repository.
- `docs/audit/2026-08-03-button-wiring-sweep.html` — the button-wiring sweep result:
  2,287 controls across 18 pages, **zero dead controls**. Records what remains unproven and
  why it is not worth proving yet (see below).

### Process
- **Shipping is now staged across both live instances** (owner directive 2026-08-01): back up
  both DBs -> deploy the tag to the test site -> verify -> the same tag to prod -> verify ->
  **external reachability of both public URLs** -> both sites end on the same tag. Any anomaly
  stops the sequence, and user data (prod above all) is proven intact before the bug is
  investigated. See `/ship-version` Part B/C and `.claude/rules/engineering-process.md`.

### Known / deliberately deferred
- **844 controls carry only `weak` wiring evidence** (a shared class selector matched, which
  proves nothing about any individual control). They are JS-rendered list rows — 複製 x170,
  立即執行 x105, 測試 x90, 設定金鑰 x35 — inside `#jobs-body`, `#vars-panel`, `#sources-wrap`
  and `#hist-filter`, and such lists bind handlers by delegation on the container, which is
  precisely what "shared selector" looks like. Proving them individually means switching every
  tab, expanding every collapsed panel and opening every modal: roughly 3-4x the sweep's cost
  for an expected result of "still fine". **Owner decision 2026-08-03: not now** — sweep those
  surfaces when work touches them. Everything visible at rest is proven wired.

## [v0.1.25] - 2026-08-01

Two owner-approved accounting rulings, each triggered by a real question about a real number,
plus the remediation of eight defects that three independent audit passes turned up — five in
the feature as first built, three in the fix for those five.

**How this version started.** The owner asked why 各帳戶現金 showed 101,587.90 USD while
換匯損益 showed 1,587.90 for the same account. Neither figure was miscalculated: they are two
definitions, and the FX one was missing 100,000 USD of opening capital because the FX pool had
no way to represent a foreign-currency deposit. Measured twice, 33 minutes apart, the pool
crossed from −4,600.10 to +1,587.90 and the old `foreign_cash < 0` warning went silent while
the error stayed at exactly 100,000 USD.

### Added

- **FX cost basis for foreign cash movements** (owner ruling 2026-07-30; spec
  `docs/spec/2026-07-30-fx-opening-basis.html`). `cash_movements.acq_home_amount` records the
  HOME-currency cost of a foreign deposit/opening, so it can fund the FX pool and carry a
  basis. Three design points the owner's accounting review settled:
  - **F1 — the AMOUNT is stored, never the rate.** A rate is an average, and
    `data-and-pricing.md` forbids storing an average as the authority; `fx_conversions`
    likewise stores two amounts. The form still accepts a rate and converts it on the way in.
  - **F2 — `covered_ratio`** = basis-known acquisitions / all acquisitions, with outflows
    absorbed **pro rata**. The intuitive `balance − unbased` shortcut goes negative once the
    balance drops below the unbased amount, recreating the reversed-sign figure this change
    exists to remove.
  - **F3 — the ratio scales the WHOLE foreign exposure, cash AND stocks**, because `avg_rate`
    itself comes from the covered population. Degrading only the cash leg left the larger
    error unflagged (+42,359 TWD on the demo ledger).
  - `GET /api/cash/acq-rate` pre-fills the stored close as a REFERENCE and stays blank when no
    rate exists on or before that date — prod's `fx_rates` only reach back to 2026-07-01, so
    manual entry is a routine path, not a fallback.
- **Declared short sale** (owner ruling 2026-07-31). `transactions.short_sale`, default false,
  offered only on the sell side. Only a declared sell may exceed holdings. The flag is never
  inferred: the system cannot tell a genuine short from a missing buy, and auto-applying short
  accounting to every oversell would turn a typo into a plausible-looking realized loss — a
  wrong number that looks right, which is worse than the absurd one it replaced. Per the
  owner's rule (買回的每股成本結算獲利，剩下的股數以本次成本為起點): a declared sell exhausts
  the long lot then opens a short lot holding the net proceeds; a buy covers first at that
  buy's all-in per-share cost, realizing `(short avg − cover cost) × covered` dated the COVER
  date (`kind="short_cover"`, reaching the tax capital-gains sheet); the remainder starts long
  at that same cost. Long and short are mutually exclusive, so a position is one signed
  quantity and every existing valuation formula works unchanged.
- **Fee-rule conflict warning.** `discount` and `rebate_rate` express the same broker benefit
  two ways — charge less now, or charge full and refund later (FE-D1). Both on applies it
  twice, and since fees are part of `original_total` that quietly understates cost basis
  (measured on the test site: a full 869 commission charged as 199 AND forecasting a 153 refund
  → net 46 instead of 200). `/api/fee-rules` reports a non-blocking `conflicts` entry computed
  from the EFFECTIVE values, with a plain-language explanation and one-click resolutions. It
  warns, never blocks — a broker really could do both, and only the owner knows.

### Changed

- **The 賣超 guard is DATE-AWARE.** `validate.py` checked `current_shares()`, the net across
  ALL dates, so a back-dated sell covered only by a LATER buy never even asked for
  acknowledgement — and the replay then discarded that symbol's cost basis permanently. It now
  also checks `holdings.shares_through(trade_date)` and names the date in the message. The cash
  ledger has had the equivalent `running_min` guard since audit C3; this closes the same hole
  on the share side.
- **`Holding.oversold` is STICKY.** It was `shares < 0` read at the END of the replay, so a
  later buy netted the position positive and cleared the warning while the discarded basis
  stayed gone. Measured on the demo: average cost 6,100 against a 379 market price,
  `oversold: false`, XIRR still computing, and 26,000 USD of proceeds absent from realized P&L.
- **Trend and net worth include a declared short.** They excluded any holding with
  `shares < 0`, which predated the ruling; cash kept the +proceeds while the −liability was
  dropped, so both series overstated by the short's full market value.
- **Ratios over a short's basis divide by `abs()`**, and `fully_recovered` (已回本) is gated on
  `not short_open` — a short's basis is negative by construction, so the bare tests rendered a
  profitable short as a −3.85% loss and badged every open short 「配息已完全沖減成本」.
- **`export/tax.py` reads the same weighted average** the dashboard does, so the tax package
  cannot disagree with 換匯損益 on the same reconversion.
- `GET /api/ledgers/transactions` exposes `short_sale`: the flag changes how a row is booked,
  so omitting it meant the book could not be rebuilt from the ledger.

### Fixed

- **A dividend landing on an open short is no longer booked as income.** The audit-H2
  post-close branch keys on `shares == 0`, which is ALSO an open short's long lot — it credited
  a payout the short seller actually PAYS. A DRIP/stock dividend was worse: it added shares
  straight to the long lot, and one equal to the short netted the position to zero, so the
  holding AND its proceeds vanished from the report with no realized row. Now: raise
  `UnbookableLedgerError` on the strict path, skip and flag `unbookable_dividend` on the
  dashboard path, never book it.
- **`UnbookableLedgerError` subclasses `ValueError`** so the call sites that already degrade on
  `except (ValueError, KeyError)` are untouched, while the three STRICT sites —
  `actions.recompute`, `strategy/whatif`, `export/tax` — catch it precisely and answer 422 with
  an actionable zh message instead of letting it escape as a 500. That regression was
  introduced by the fix above and caught by the phase-2 audit; the whatif and tax sites had
  never handled the module's pre-existing ValueErrors either.
- The 取得成本 field's hint said 「實際換匯價可能不同」, which reads as if it records a
  CONVERSION. It does not: a foreign deposit credits the pool and debits nothing, so recording
  an internal conversion that way overstates the home pool and double-counts net worth. A
  permanent amber note now says so and points internal conversions at 換匯中心.
- Editing 金額 alone silently rescaled the implied acquisition rate (1,000 USD / 32,388 TWD
  edited to 2,000 USD became 16.194). Both the entry form and the edit dialog now show that
  rate live, computed off both fields.
- The movement EDIT dialog omitted the acquisition-cost field while PUT is a full replace, so
  editing a note NULLed a recorded cost basis — data loss, not a display gap.

### Tooling

- `scripts/stress_audit`: the independent oracle gained a declared-short model **derived from
  `domain-ledger.md`, not from `cost_basis.py`** (that independence is what makes agreement
  meaningful), the unbookable-dividend rule, and a permanent **integrity layer**
  (`position.never_negative`, `sell.has_realized_row`, with an `acked` allowlist) — the part a
  reconciliation is blind to. It also reads the instance's EFFECTIVE fee rules instead of the
  seed defaults, and prints any divergence.
- Every destructive-path probe now derives its premise from live state at run time. A
  hard-coded oversell quantity had gone stale on the accumulating demo, so the app correctly
  accepted it and the "guard test" WROTE a back-dated sell that destroyed a symbol's cost basis.
- Stress coverage: phase 1 ops 69→**77**, assertions 1,088→**1,806**; phase 2 (live demo)
  **1,192** assertions. Both fail=0.
- `docs/accounting-formula-manual.md` → **v1.6** (new §4.3 declared short sale with the full
  `tw_broker/2609` worked example; §8.1/§8.3 rewritten for `acq_home_amount` + `covered_ratio`),
  English mirror regenerated in the same change set.

### Notes

- **Backward compatible, and proven so.** `acq_home_amount` is NULL and `short_sale` is 0 on
  every pre-existing row; `covered_ratio` is the literal `Decimal("1")` and the caller skips
  the multiply. The golden-payload diff across this whole version is field additions only —
  **zero money values changed**.
- **Invariant 6 intact**: `fx_unrealized` remains a separate KPI, never summed into
  `total_return` / `unrealized_total` / XIRR, and `networth.py` never reads it.
- Accepted limitations, documented in `domain-ledger.md` rather than fixed: `gross_invested`
  excludes short capital (a cover is funded by proceeds already received); XIRR over a PURE
  short round trip reports a borrowing rate (the flow pattern is a loan); allocation weights use
  a net-exposure convention that sign-flips when net short; a same-day buy-before-sell merges an
  intended intraday short into the long lot (total P&L conserved, attribution differs); and
  `cash_balances` still credits an unbookable on-short dividend, which the flag discloses.

## [v0.1.24] - 2026-07-26

Remediation of the 2026-07-25 full-site audit: 2 high / 5 medium / 6 low findings, plus
3 incidental defects and 2 pre-existing red tests found during implementation. **One
money-of-record change** (H2, owner ruling recorded below); everything else is display,
layout, validation or tooling.

**Owner ruling (H2, 2026-07-26).** A CASH-family dividend whose payment date falls after
its position already reached zero shares is booked as REALIZED INCOME, not absorbed into
the closed position. The owner confirmed the real case: dividends imported for symbols
that were later closed belong in that symbol's historical return. Consequence to expect
on the real ledger: **the historical total return of already-closed symbols RISES** by the
sum of such payouts (they were previously dropped). 股利總覽 and the XIRR cashflow series
already counted them, so this makes the three figures agree instead of disagree.

### Added
- **Post-close cash dividend → realized income** (audit H2). `RealizedRow` gains
  `kind: "sale" | "dividend"`; `cost_basis.build_book` emits a `kind="dividend"` row
  (`realized = proceeds_net = net`, zero shares, zero cost removed, dated at payment)
  when a CASH/NET dividend lands on a zero-share position. Booked exactly once — the
  cost-reduction path is skipped, not doubled (invariant I4 holds). Accounting manual
  §6.3b (zh authority + en mirror); oracle + phase-1 scenario op "Found-bug op #3"
  (`moomoo_my/5225` buy → sell-all → post-close NET dividend).
- **`HoldingRow.unrealized_pct`** (audit H1) — per-holding unrealized return, server-computed
  against ORIGINAL cost: the same basis as `KpiSummary.total_return_rate` and `payback_ratio`,
  and the only safe one (adjusted cost may legally be ≤ 0). Surfaced on the drawer aggregate
  and per-account breakdown, plus a `fully_recovered` flag for the 已回本 label.
- **`AccountFXResult.cash_basis_incomplete` + `spot_delta`** (audit M4/L2) — the negative
  foreign-cash signal the `foreign_cash` docstring always asked consumers to flag, and the
  rate delta the UI used to derive by subtracting two Decimal strings.
- **`fee_tax` on rebalance preview rows** (audit L1) — the combined cost, summed in Decimal
  server-side; the UI no longer adds `fee` and `tax` in float.
- **`dividend_model.check_amounts()`** (audit M5) — one conservation gate
  (`withholding + net ≤ gross`, non-negative) shared by the CSV/manual import preview AND
  the ledger edit endpoint. Deliberately NOT `gross − withholding == net`: that identity is
  the US DRIP model's, and TW 二代健保補充保費 / 匯費 and US ADR fees legitimately break it.
  No levy other than the US 30% DRIP withholding is computed anywhere (owner decision
  2026-07-26: fees/taxes stop at 手續費 + 證交稅).
- **Rebate inbox window** (audit L4) — pending months default to the last 12; `older_count`
  and `window_months` are ALWAYS reported and the UI offers 「顯示更早」, so the bound is never
  silent. Confirm still accepts an older month (the window is a view, not a cap).
- **`tests/e2e/test_no_horizontal_scroll.py`** — the document must never scroll sideways:
  six measured boundary widths on the dashboard, two widths across nine pages, plus a
  single-row-topbar assertion for 1280–1600px.
- **`tests/e2e/test_format_exact_rounding.py`** — exact-formatting cases in a real browser.
- **`tests/portfolio/test_post_close_dividend.py`** — five H2 cases including the
  closed → re-bought → paid ordering case.

### Changed
- **Exact decimal formatting in `web/format.js`** (audit L5) — money/percentage rendering
  moved off `Number().toLocaleString()` onto string decimal arithmetic. Two properties now
  hold: a value already quantized by the fee engine under its account's `rounding`
  (TW 手續費/證交稅 are 無條件捨去 per 財政部 FE-D3) is reproduced BYTE-FOR-BYTE, and an
  unquantized value rounds exactly as `Decimal.quantize(ROUND_HALF_UP)` would. Floor is
  deliberately NOT implemented in the display layer — it is a property of the TW fee/tax
  engine, and applying it to other TWD amounts would be wrong.
- **Narrow-window layout** (audit M1/M2) — removed `.topbar { flex-wrap: nowrap }` from the
  label-anti-wrap section (declared far below the `.topbar` block, it won on order and pinned
  the document at 1,257px for every viewport from 761 to 1279px); the base rule now owns
  wrapping. `.kpi-band.v2` added to the 1280/860 breakpoints — two classes out-specify a bare
  `.kpi-band`, so those breakpoints were dead for the v2 band; new ≤1023px tablet tier.
  `.panel-sub`'s nowrap scoped to panel HEADS — on body copy it made a paragraph one
  unwrappable line (and `overflow: hidden` is inert on an inline element). Topbar labels
  collapse to icons at ≤1365px so common laptops keep a single-row header.
- **Realized-P&L surfaces** carry `kind`: the dashboard table and the drawer mark a post-close
  dividend row and show 賣出股數 / 調整成本移除 as 不適用; `realized_pnl_*.csv` gains a `kind`
  column. The tax package's `realized_gains_{year}.csv` filters to `kind == "sale"` — the
  payout is already reported on the dividends sheet, and counting it twice would misstate
  taxable income.
- **`returns.by_currency` iteration is sorted** — it was built from a `set`, so the order (and
  therefore the dashboard's 各幣別報酬 chip order and the golden snapshot) varied per process.
  Values unchanged.
- **The unavailable-figure KPI badge shows a short zh label**, with the backend's (long,
  English) reason in the tooltip; the 資料新鮮度 panel still prints it in full.
- **`scripts/seed_demo.py`** now seeds funding deposits and a monthly price/FX history, so the
  demo instance shows a real XIRR and trend instead of "no FX rate stored…" and five overdrawn
  cash pools. Demo-only; no effect on prod data.
- **Export filter bodies reject unknown keys** (audit L3, `extra="forbid"`) — a misspelled
  filter key used to be dropped silently and return the WHOLE portfolio with a footer reading
  `filter: account=all`. Silently widening a reconciliation export's scope is its worst
  failure mode.

### Fixed
- **Drawer unrealized percentage flipped sign on a fully-recovered holding** (audit H1) — the
  frontend divided by ADJUSTED cost, which is legally ≤ 0 once cumulative dividends exceed
  cost, rendering a +223,473 gain as −1399.07%.
- **交易輸入「清除」was a dead button** (audit M3) — declared in the markup, referenced by no
  JS anywhere; clicking it did nothing at all.
- **What's-new e2e flows had been red since v0.1.23** — they pinned
  `data-wn-key="0.1.17:market-risk-alerts"`, and the ✦ panel keeps only the six most recent
  versions, so v0.1.17 aged out of the window. Both flows now discover a still-visible
  settings-tab feature from the live payload, and the blink assertion checks the anchor
  relationship rather than a fixed descendant selector.

### Verification
- Full `pytest` suite (including all e2e): exit 0.
- `mypy` bare, full scope: **546 source files, no issues**. `ruff check portfolio_dash tests
  scripts`: clean.
- Independent Decimal oracle, stress-audit phase 1: **ops=69 pass=1088 fail=0** (was 66/1060 —
  the H2 scenario adds 3 ops / 28 assertions).
- Real-browser width sweep: 16 widths on the dashboard + 11 pages × 3 widths → **0 pages with
  page-level horizontal scroll** (9 page-widths failed before).
- Fee-engine verification matrix: 4 account rule sets × buy/sell × ETF × daytrade × 9
  quantity/price cases = **162/162** agree with `compute_fees()`, driven by the EFFECTIVE
  config (defaults + settings-page overlay).
- Golden contract snapshot regenerated and diff-reviewed: four additive fields
  (`unrealized_pct`, `spot_delta`, `cash_basis_incomplete`, `kind`) plus the by-currency
  ordering stabilisation; **no existing value changed**.
- Audit artifacts: `docs/audit/full-system-audit-2026-07-25.html` (findings),
  `remediation-2026-07-26.html` (what changed + evidence), `rootcause-demo-2026-07-26.html`
  (the four root-cause divergences, demonstrated in real cascade).

## [v0.1.23] - 2026-07-24

Round-8 (owner UI feedback) + Round-8.1 (demo-verification follow-ups + a deep systemic
audit). Frontend + read-side API only — no money-of-record formula changed, no boot
migration.

### Added
- **Unified instrument register/edit surface.** The add-to-watchlist, quick-register and
  edit panels are one shared modal (`inst-quickadd.js` gains an `edit` mode; `instruments.js`
  `openEdit` is a thin caller) — fix one place, all entry points follow.
- **Single "AI 辨識" resolve.** The former 產業「AI 偵測」 and 代號「AI 判讀」 are merged into one
  action with a single result-application path that fills 代號/名稱/產業/產業細分 consistently.
- **Symbol drawer — 交易明細 + cross-account 部位摘要.** A new per-symbol activity list
  (opening + buys + sells + DRIP/配股) served from `/api/symbol/{symbol}/detail` (reusing
  `build_dashboard`), 10/page, with a reconciliation footer (期初＋買−賣 ＝ 部位摘要) and an
  account filter; 部位摘要 now shows a server-computed cross-account aggregate + per-account
  breakdown instead of the first account only.
- **Shared grouped symbol picker** (`web/sym-picker.js`) used by the manual, dividend and
  opening-inventory inputs: 已持有/未持有 groups, 股數+均價 annotation, per-account market
  filter, and a 「＋新增標的」 quick-register entry. Replaces the two copy-paste pickers + the
  opening datalist.
- **Rebate inbox — 當月累計預估.** A non-confirmable 「當月累計預估(未到期,次月退款)」 section
  surfaces the current-month 折讓 forecast immediately (`GET /api/rebates` gains `accruing`);
  the whole item bar toggles its 明細.
- **Holdings 合計 + exports follow the filter** (from the earlier Round-8 work): a
  server-computed `holdings_subtotals` field feeds a filter-aware 合計 footer; CSV/report
  exports carry the active account/market filter.

### Changed
- **Chart buy/sell markers** are colored labeled triangles (green ▲ below price / red ▼ above)
  with a legend; detail on hover. Equal original/adjusted average collapses to one 均價 line.
- **Symbol pickers filter only on keyboard input** — a focus/click open shows the full grouped
  list (no longer filtered down to the already-selected symbol).
- **Trade ledger UX:** the new-row highlight is a soft 8× pulse (reduced-motion guarded); a
  fully-successful commit auto-switches to the matching tab; 「扣款後現金」 → 「交易後現金」.
- **`ai_resolve` de-duplication:** an in-process fingerprint cache + a bare-symbol registry
  re-gate stop redundant LLM calls; the AI-input pane no longer re-runs the vision parse when
  a single symbol is registered (local re-validate, preserving checkbox state).

### Fixed
- **Candidate pick no longer wipes 名稱/產業.** Picking an AI candidate whose live re-validation
  misses used to blank the just-filled name/sector (pristine tracking ignored programmatic
  fills); AI-sourced fills are now pinned and 確認 stays usable.
- **交易明細 reconciles with 部位摘要** — the old transactions-only list omitted opening
  inventory and DRIP shares; a symbol held in two accounts no longer understates 部位摘要.
- **Rebate double-credit (F2d) closed.** Month suppression is keyed structurally on the
  movement date (not only its editable note), and a booked 折讓款 movement's kind/date are
  locked (frontend + a `routers/cash.py` guard), so a refund can never be confirmed twice.
- **Frontend money-math invariant restored (F10).** 未實現匯損益（合計） is no longer a JS float
  sum of two Decimal strings — a server-computed Decimal `unrealized_fx_total` is added to the
  FX wire and displayed as a string.
- **Archived instruments** no longer leak into the picker's 未持有 group (`/api/input/context`
  wires the `archived` flag); sidebar inbox badge + context refresh after a commit.

### Verification
- ruff clean; `mypy --strict` (bare, 543 files) clean; regression pytest 0 failures; full
  Playwright e2e 0 failures; stress-audit phase 1 `ops=66 pass=1060 fail=0`.
- Contract: `/api/dashboard` gains `unrealized_fx_total` — spec-17 golden + spec-18 round-trip
  updated. Iterated on the test site (demo) and behaviour-verified before promotion.

## [v0.1.22] - 2026-07-22

**Batch B — the Moomoo account merge.** `moomoo_my_us` + `moomoo_my_my` are merged
into ONE dual-market account `moomoo_my` (markets US+MY, settlement USD / funding
MYR, shared MYR pool, USD pool MYR-anchored), matching the physical brokerage
account. Blueprint (56-agent deep review, 45 confirmed findings) and decisions:
the Batch-B blueprint artifact; owner sign-off 2026-07-21. This is a LOCKED-model
change recorded here per `CLAUDE.md`: fee/tax/dividend rules now bind to the
**(account, market)** pair (accounts-table scalars remain as single-market
fallback); the accounting manual is revised to v1.4 accordingly.

### Added
- **(account, market) rule binding.** New `account_market_rules` table + idempotent
  seed; resolvers `fee_rule_for` / `dividend_model_for` / `allowed_markets` /
  `rule_sets_for` (`data_ingestion/rules_binding.py`) with accounts-scalar
  fallback; `Account.market_rules` carriage so pure `portfolio/` needs no conn.
- **One-time atomic boot migration** (`data_ingestion/moomoo_merge.py`): single
  `BEGIN IMMEDIATE` span; S0 + partial-release guards; opening-inventory PK
  collision → loud ABORT (never OR IGNORE/REPLACE); dividend-skip fingerprint
  rewrite; in-span self-check (zero legacy ids incl. TEXT-embedded fingerprints,
  per-currency cash-pool conservation, merged-row currency pins); `api/app.py`
  orchestrates exactly ONE `pre_migrate_` snapshot per actual migration.
  `DEFAULT_ACCOUNTS` reshaped 4→3 in the same change (seed can never resurrect
  the legacy accounts). Legacy account ids in imported CSVs alias to `moomoo_my`
  with a soft notice; downloadable templates re-anchored.
- **Per-market wire + dividend entry.** `/api/accounts` + `/api/input/context`
  gain an additive `markets` object ({fee_rules, div_model} per bound market);
  the manual dividend form and committed row `type` follow the entered SYMBOL's
  market on a multi-market account; `dividend_import` gains a type/market
  coherence `needs_confirm` guard. `ManualBody.market` + `422 market_required`
  for unregistered symbols on multi-market accounts (never guess the market).
- **Pre/post reconciliation harness** (`scripts/merge_reconcile.py`): read-only
  snapshots (URI mode=ro + write-denying authorizer) of engine + raw-SQL figures,
  alias-fold diff, copy-only `run` — the real-data continuity proof executed on a
  prod DB copy before deploy.
- 8 merged-account browser e2e flows (dual-market cash/fee split, US-DRIP+MY-NET
  dividend split, preview ccy/precision pins, migration-produced-DB serving,
  legacy-CSV alias, FX-form boot behavior, both cash-overdraft orderings).

### Changed
- All fee-rule lookups resolver-swapped (6 market-aware sites + 2 rebate sites via
  `rule_sets_for` any-bound-set-rebates semantics); dividend compute sites
  per-market; H1 + ledger-edit coherence guards accept allowed-market SETS
  (single-market behavior byte-identical, messages unchanged).
- Dashboard FX exposure counts only settlement-ccy holdings (a pre-merge no-op;
  prevents MYR holdings folding into the USD exposure of the merged account).
- AI input: accounts catalog renders multi-market accounts; `AiDraft.market`
  carries the model's market judgment to the preview; prompt v5 /
  `LIBRARY_VERSION` official-v11. Preview-card + opening-hint money labels follow
  the resolved instrument's currency (MYR + 3-dp for MY drafts).
- Frontend surfaces: `names.js` merged entry; `settings.html` static account
  panel regenerated to 3 cards (dual-currency Moomoo MY card, pinned by a
  source-scan test — the id-scan is blind to that file); `settings-fees.js`
  labels read as rule sets.
- Stress harness reshaped to the merged topology (oracle `fee_tax` per-market;
  permanent merged-account scenario: US+MY trades, MYR→USD conversion, both
  dividend models on one account); accounting manual v1.3 → **v1.4** (invariant
  I6 → (account, market); anchors reconciled to the regenerated merged evidence;
  two pre-existing example typos fixed).

### Removed
- Legacy per-account quote-source debris: `_ACCOUNT_MARKET` seed map, dead
  `account_chains`/`set_account_chains` APIs, per-account `data_source_fallbacks`
  seeding (quote routing has been per-market since 2026-07-03; the empty legacy
  table remains as recorded deferred debt).

### Verification
- Full pytest 0 failed / 0 errors; bare `mypy --strict --no-incremental` clean
  over 541 files; ruff clean; id-contract sweep baseline unchanged; stress-audit
  phase 1 `--ui` ops=66 **pass=1060 fail=0** (oracle independence intact); full
  e2e suite green. **Demo live-migration rehearsal passed**: boot auto-merged the
  demo ledger → 3 accounts, exactly one `pre_migrate_` snapshot, second boot
  no-op, `verify_live` ALL PASS. Money-of-record continuity: migration self-check
  + the reconciliation harness (prod-copy run at deploy); no formula changed.

## [v0.1.21] - 2026-07-21

Round-7 owner batch (Batch A, FU-D55..D60). Phase-1 plan + Senior Review:
`docs/reports/2026-07-21-batchA-phase1-plan.md`; decisions + traceability:
`docs/reports/2026-07-21-batchA-r7-minispec.md`. The Moomoo account merge (Batch B)
is deferred to its own round.

### Added
- **Bursa registry + MY offline verification (FU-D55).** New
  `pricing/bursa_registry.py` — 1,079 four-digit Bursa Malaysia codes → official
  short names, baked at dev time from the live directory (provenance + retrieval
  date in the module docstring; never model memory). `lookup_instrument` now
  verifies an unregistered MY code against the registry when the quote feed lacks
  the counter, so a correct AI resolve reaches `status:"resolved"` instead of being
  demoted to candidates; `pricing/names.py` resolves MY names registry-first.
  Consequence (by design): a registry-verified symbol can register price-less and
  shows as stale until a provider covers it.
- **Draft-preview cash-after (FU-D57).** `POST /input/manual/preview` emits
  `cash_after` = account cash pool + the signed trade total (same quote currency,
  dynamic label); rendered under 該帳戶現金. Display-only, null when unknown.
- **Old-vs-new position comparison (FU-D58).** `_position_preview` and
  `/api/whatif` emit `old_shares` / `old_original_avg` / `old_adjusted_avg`
  (+ `old_weight` and SELL `remaining_market_value`, floored at 0, on whatif);
  draft preview and the detail 試算 drawer render 舊 → 新 pairs.
- **News fetch observability + retry (FU-D56).** `FetchOutcome` classification
  (ok / http_error / non_html / too_short / salvaged / blocked_scheme / error),
  single WARNING log seam with the URL, `fetch_status` + `fetch_attempts` columns
  (ALTER-if-missing), and a bounded retry queue (14 days / 3 attempts / 10 per run)
  that re-fetches empty-body rows even after discovery stops surfacing them.

### Changed
- **MY prompt guidance to TW parity (FU-D55).** `AI_INSTRUMENT_RESOLVE_PROMPT` v2 and
  `AI_INPUT_PROMPT_BODY` v4: directory-verified name⇒code exemplars, the ACE-market
  leading-zero rule (0166, never 166), and the brand/mall→listed-parent rule;
  `LIBRARY_VERSION` → official-v10.
- **News fetcher HTTP layer (FU-D56).** Browser-like `Accept`/`Accept-Language`
  headers + cookie-carrying opener (consent redirects complete); byte cap raised
  200 KB → 1.5 MB; extraction falls back block-strip → JSON-LD/embedded-JSON →
  largest `<p>` cluster → `salvaged` text (LLM trims) instead of silently storing
  an empty body. Zero new dependencies.
- **試算 drawer is now backend-computed (FU-D58).** The detail-page 試算 section
  POSTs `/api/whatif` (debounced) and renders server Decimal strings only; ALL
  local fee/P&L math and the `window.pdFeeTax` mirror are deleted; on request
  failure it shows 試算暫不可用 (never fabricates).
- **Clear-on-success + real checkbox filtering (FU-D59).** The AI input commit
  writes ONLY checked rows (`_drafts_to_csv` newline-sanitizes notes so the
  one-line-per-draft mapping is robust; the warnings-ack recommit reuses the same
  filtered text); full success clears the AI inputs (banner in the new in-pane
  `#ai-result`) / the CSV paste; partial success keeps only unwritten rows (AI) or
  the entire raw paste (CSV).
- **Opening-inventory contract inverted (FU-D60).** Required columns are now
  `account,symbol,shares,original_cost_total,build_date`; `original_avg_cost` is
  optional/legacy (avg-only derives the total with a soft `opening_total_derived`
  issue; both-and-disagreeing raises soft `opening_cost_mismatch` beyond
  max(1 minor unit, 0.5% × total)). The form takes 股數＋原始總成本 with a live
  read-only 均價 hint and an instrument datalist on `#o-symbol`;
  `PUT /ledgers/openings/{acct}/{sym}` accepts authoritative `total` (legacy `avg`
  still derives).

### Removed
- **`opening_inventory.original_avg_cost` column (FU-D60)** — the repo's first
  destructive migration (idempotent `DROP COLUMN`, guarded by `pragma table_info`;
  required because the legacy column was NOT NULL). A rounded average is never
  stored as authority (domain-ledger rule); every reader — reports, tax, whatif,
  dashboard, symbol events, ledgers wire fields, the stress-audit oracle — now
  computes `original_avg = total / shares` on read. No money-of-record figure
  changes (cost basis / XIRR always keyed off `original_cost_total`).

### Verification
- Full pytest 0 failed / 0 errors; bare `mypy --strict --no-incremental` clean over
  527 files; ruff clean; stress-audit phase 1 `--ui` ops=66 **pass=1066 fail=0**
  (oracle independence verified); id-contract sweep: no new dead zones. Demo
  behavior probe 12/12 including a live-LLM resolve of "Inari Amertron" → 0166
  (`resolved`, GICS sector + industry). No money-of-record formula changed — the
  accounting manual is intentionally untouched.

## [v0.1.20] - 2026-07-20

Six follow-up rounds on the v0.1.19 surface (owner-driven; decision records FU-D1..D54 in
`docs/reports/2026-07-1[5-9]-v0119-followups*-minispec.md`, one mini-spec per round).

### Added
- **Unified AI instrument resolve (FU-D50):** ONE `POST /api/instruments/ai-resolve` + one
  code-owned prompt (`ai_instrument_resolve` v1; LIBRARY_VERSION official-v9) returning local
  exchange code + name + GICS sector (+ optional industry) at temperature=0; confidence-gated
  (high auto-fill / medium-low 2-5 candidates / honest not_found, never fabricated), always
  provider-verified before auto-fill; auto-triggers in the shared quick-add dialog (manual /
  AI-input / CSV entries) on format-fail or lookup miss; watchlist AI-sector button re-pointed
  (`sector_only`). `/ai-sector` route removed. New `shared/symbol_format.py` = single source of
  per-market code shapes (TW/US/MY).
- **GICS 2023 sector vocabulary + real migration (FU-D51):** 11 sectors + ETF bucket +
  Unclassified; Semiconductors→Information Technology, Shipping→Industrials (donut +
  `sector_weight` alert regroup — a new 資訊科技 concentration alert on real data is the
  expected consequence); idempotent boot-seam rewrite of stored sectors; new nullable
  `instruments.industry` column end-to-end; goldens re-baselined (slice merges verified).
- **Draft-preview what-if + account cash (FU-D53):** server-computed `position_preview`
  (sell: cost_removed / realized_pnl / remain_shares — bit-identical to the booked-sell
  arithmetic, cross-checked in tests; buy: new shares/averages) and a display-only
  `account_cash` line on the manual preview.
- **Trade-input aids (r5):** sell hints 可賣股數/持有均價 (click-fill, build_book replay),
  ledger live refresh after every successful commit, dividend symbol picker (held +
  show-closed toggle), AI-input vision + model picker + local-exchange-code prompt v3,
  inline quick registration with auto-resume.
- **資金管理 (r3-r6):** three tabs (pools/flows/fx); FX center balance display + click-to-fill
  + server-side latest-rate estimate (as-of captioned; ledger records actual amounts) + FX
  ledger list; hard no-overdraft guards for FX and withdrawals (422, incl. back-dated rows —
  ack_negative removed on those paths); account switch clears amounts; sell/buy currencies
  never equal; single-currency accounts get a disabled form with an inline reason (FU-D52).
- **Dashboard (r3-r5):** consolidated 股利總覽 section (TTM received, yearly bars, forecast-only
  estimate, ex-div calendar, payback strip — replaces the legacy dividend row + its frontend
  float math), TWR benchmark overlay (0050 / S&P 500), target-price crossing alerts,
  net-worth-incl-cash trend, per-account cash mini cards.
- **Scheduler feedback (FU-D36 + r5):** run-now 排入→執行中(progress)→成功/失敗 across all jobs,
  result detail modal (duration, output, honest LLM token/cost attribution), verified 前往 links.
- **Watchlist lifecycle (r2-r4):** accumulative soft-delete (archive; restore triggers
  background gap backfill), 3-tier deletion — permanent purge only for never-traded
  watch-only symbols with type-the-symbol confirmation; deep links in push notifications;
  資料中心 page (db stats); CSV import suite (per-kind templates, ambiguous-date chooser).
- **Fee-rule center (r1, FU-D1):** editable per-account fee-rule overrides
  (`fee_rule_overrides`, conn-aware `get_fee_rule_set` at every money path; reset = delete);
  費率明細 page now data-driven from the API. Historical rows still governed by their
  per-row `fee_rule_snapshot`.
- **Site-wide prompt registry (FU-D30):** `PROMPT_REGISTRY` typed index over code-owned vs
  user-editable prompts + completeness guard test pinning every LLM call site.
- **Multi-user prep Phase 0 (FU-D39/D54):** DB-open-surface guardrail test (5 opens / 3 files
  pinned); target blueprint revised to per-user folders (`user_trade/<UserLoginID>/ledger.db`
  = unit of backup/restore). Physical splits deferred to their own batch.

### Fixed
- **Symbol fuzzy-coercion trap (FU-D49):** `difflib` ratio on 4-digit exchange codes scores
  exactly 0.75 for any one-digit-apart pair, so unregistered codes coerced to registered
  neighbours (live bug: 2303 聯電→2330, 2883 開發金→2882 — the LLM output was correct; the
  local resolver rewrote it). Resolution is now EXACT-only for code-shaped input; the
  「視為…（模糊比對）」 coercion class is removed; name-shaped input yields non-binding
  NAME-only suggestions. Regression-pinned with the two real pairs.
- 配股 (stock-dividend) entry buttons were dead in trades.html (ids never existed — STOCK
  dividends silently booked as CASH); fixed + e2e-pinned (r3).
- e2e harness: flow modules' socket teardown clobbered the session loopback window under
  random ordering — self-healing `_assert_loopback_window()` at every socket-needing seam.
- FX-form confirm could be silently re-enabled on single-currency accounts by late async
  refreshes; `updFxBalance` now re-asserts the gate at every seam (invariant over ∨ single).

### Changed
- Contract changes recorded: FX conversions and withdrawals now HARD-block negative pools
  (was confirmable), incl. back-dated rows; watchlist delete became archive (r2 hard-delete
  superseded); `/api/instruments/ai-sector` merged into `/ai-resolve`; `ResolutionStatus.FUZZY`
  removed. MY code format tightened to `^\d{4}$` (non-matching Bursa suffixes fall to the AI
  flow; warning-only).

## [v0.1.19] - 2026-07-15

### Added
- **Daily close digest + weekly action list** (P3 batch 3C; rulings B3-D1..D4): deterministic
  assembly (day-change from last two closes, movers, alerts/signals of the day, data health;
  weekly action items with jump links), `digest_daily`/`digest_weekly` scheduler jobs —
  user-toggleable with adjustable times through the SAME `schedule_config` rows (settings
  「摘要與週報」 card, friendly pickers; raw cron stays in the scheduler tab), push via the
  notify fan-out carrying **counts/percentages only (never amounts)**, dashboard cards +
  paged history modal, optional LLM one-liner (default OFF, never blocks generation).
- **收件匣 standalone page** (dividend detection moved out of the trades page): nav count
  badge, skip AND un-skip, post-confirm 「已入帳」 undo strip; continuous re-detection
  semantics documented in-page.
- **Rebate (折讓款) forecast + pending-refund inbox** (FE-D1 — forecast-only, never in
  cost/P&L/XIRR): expected = floor(fee × rebate_rate) grouped per month, pending from the
  following month, confirm books an editable-amount cash movement (`kind='rebate'`); hints
  in the manual-trade preview and rebalance drawer/execution report.
- **Fee engine v2** from the owner-provided complete schedules
  (`docs/reference/broker-fee-schedules-2026-07.md`): TW **floor rounding** for fee AND tax
  (財政部 rule — supersedes the previous 四捨五入; owner sign-off 2026-07-15); Moomoo MY
  commission 0.03% min RM0.01 + platform RM3 + clearing + SST 8% of fees + step stamp duty
  `ceil(amt/1000)×RM1` with per-type caps and **ETF exemption**; Moomoo US per-component
  fees (commission/platform/settlement/CAT + SELL-only SEC 0.0000206 min $0.01 / TAF
  $0.000195/sh min $0.01 cap $9.79) + **FE-D2** MY stamp on US trades booked in USD with
  fx + MYR recorded in the snapshot; Schwab SELL-only regulatory fees; per-row snapshot
  regime (`engine:"v2"`) — historical rows keep their booked regime.
- **Cash management hardening** (audit C1-C7): per-(account,ccy) **statement with server
  running balance**, `opening` (期初資金) movement kind, negative-pool banner, currency↔
  account validation, date-aware running-min guard, reporting total skip-not-abort.
- **News manual fetch** (`POST /api/news/run`, all registered instruments or one symbol,
  409 in-flight guard, guest 403) + schedule hint linking the scheduler tab.
- **Accounting formula manual v1.3** (`docs/accounting-formula-manual.md`): **activated as
  the arbitration standard** (owner sign-off 2026-07-15, effective this version); adversarial
  completeness sweep filled 11 class-A formula gaps + §12.5 out-of-scope enumeration;
  English mirror `docs/accounting-formula-manual.en.md` (zh version is the authority);
  XIRR scalar anchored by the stress solver.
- **Permanent stress-audit harness** `scripts/stress_audit/` + `/stress-audit` skill:
  independent Decimal oracle (incl. its own XIRR solver, tolerance |Δ|≤1e-6), phase-1
  clean-room (1,063/1,063 on this release) + phase-2 live-site delta mode, accumulation
  rules (every found bug → pytest regression + permanent scenario op), ship checklist item 9.
- **`ledger_audit`** before-value capture on every ledger update/delete (audit M9) +
  honest correction copy; `daytrade` persisted on transactions (new column, migrated).

### Changed
- Alert bell gains per-alert **read state** (dot lights only for unseen ids; opening the
  panel marks current alerts read); `quota_low` gated on `ai_active` (no 額度偏低 when AI
  is off; stale events suppressed once; chip shows 「AI 未啟用」).
- Site-wide actionable-button **design language**: `.btn`/`.btn-sm` tiers, leading
  `.ico` icon convention (per-row all-or-none), `.toolbar`/`.spacer` rows; the drifted
  duplicate `.btn` in settings.css removed.
- **Trades ledger hardening** (deep audits, all findings CONFIRMED by probes):
  account↔market coherence (create + re-key edits; legacy rows stay editable in place),
  negative fee/tax rejected on all paths, orphan-dividend corrections 422 (was 500),
  overflow-sized input 400 (was 500), future-date soft confirm, edits recompute fee/tax +
  regenerate the snapshot unless explicitly overridden, duplicate-trade soft confirm,
  oversell replay scoped to introduced/worsened only, 4-dp price cap at the write seam,
  fuzzy-resolve threshold 0.75.
- **ETF sell tax now uses the instrument registry on every entry path** — manual/CSV
  entries were taxing ETF sells at 0.3% instead of 0.1% (stress-audit CONFIRMED finding);
  CSV gains a `daytrade` column; the manual form gains a 當沖 checkbox.
- Rebalance ruling date canonicalized **2026-07-13**; weights/return-rates ruled IN
  arbitration scope (owner, 2026-07-15).

### Fixed
- `cash.html` TDZ ReferenceError aborted form init (deposit/withdraw/FX buttons dead) —
  branch regression caught by the live-demo stress phase; cash page added to the e2e
  page smokes.
- Digest no longer resurfaces suppressed `quota_low` events while AI is inactive.
- Flow-server e2e files require the `_loopback_sockets` fixture — added to the rebate
  flow file (passed in isolation, failed only under full-suite ordering).


## [v0.1.18] - 2026-07-14

What's-new announcement system (four field-feedback rounds), the combined cross-account
rebalance engine, three print-optimized reports, and the reconciliation-grade CSV
migration. Every batch implemented by Opus subagents and gated by independent deep
reviews (two review rounds caught and fixed six defects green tests had missed).

### Added
- **✦ What's-new system (WP-WN):** topbar ✦ (crisp inline SVG) with an unseen dot +
  sidebar NEW pill; the panel lists recent versions' features with per-feature NEW
  state — a feature clears ONLY on 前往 / 知道了 / 全部標示已讀 (per-feature
  `whatsnew_seen` table; opening the panel acknowledges nothing; legacy version-level
  acks migrate on boot). 前往 deep-links to the exact page/tab, scrolls to a precise
  per-feature anchor, drops a dismissible in-page callout card, and blinks the target
  0.5s in / 0.5s out × 20 (canceled by any tab/page switch; re-armed only by 前往;
  30s marker freshness). `GET/POST /api/whats-new`; catalog in `shared/whatsnew.py`
  (every href-bearing feature must carry a resolvable `target` — test-enforced).
- **版本發佈資訊 history browser:** settings 一般 → button beside 系統版本; pages the
  FULL user-facing release story (`GET /api/whats-new/history?offset&limit`, catalog
  backfilled v0.1.0–v0.1.11) 5 versions at a time — the browser renders only loaded
  pages, so an ever-growing history cannot degrade performance.
- **Print reports (self-contained offline HTML, A4 print CSS, all dynamic strings
  escaped, shared `export/report_html.py` scaffold):** 再平衡試算執行指南
  (`POST /api/export/rebalance-report` — per-symbol summary + a per-ACCOUNT execution
  checklist with ☐ rows and per-currency subtotals), 持倉報告
  (`/api/export/holdings-report` — KPI grid, weight-sorted holdings with account
  labels + TOTAL row, sector/currency allocation), 帳本報告 (`/api/export/ledgers-report`
  — four ledgers over an optional date range with per-currency sums). Buttons on the
  持倉明細 head, the rebalance drawer, and the 交易帳本 toolbar.
- **Reconciliation-grade CSV endpoints:** `POST /api/export/ledger {kind,from,to}`
  (single-ledger CSV), `/api/export/realized`, `/api/export/ai-predictions`,
  `/api/export/symbol-detail {symbol}`.

### Changed
- **Rebalance preview is combined-aware (owner ruling 2026-07-14 recorded here, decided
  2026-07-13): target weights stay SYMBOL-level (D8 single source of truth) and apply to
  the COMBINED cross-account position; buys route to the most-shares account, sells
  allocate greedily most-shares-first bounded per account (a target of 0 liquidates
  every account; oversell is structurally impossible). Option 2 (per-account targets)
  was rejected.** Response gains `accounts[]` constituents + per-account `legs[]`
  (fees per that account's rule set, TW odd-lot hint) + `over_allocated` /
  `excluded_with_target` flags; the drawer shows ONE row per symbol with account chips
  and per-leg actions, and 目標合計 counts each symbol once.
- **Every 匯出 CSV now flows through the backend reconciliation channel (owner
  directive 2026-07-14):** seven surfaces rewired (holdings, realized, ledger tabs,
  ai-predictions, symbol dividends, llm-usage, job-runs) — full data sets at source
  precision, English snake_case headers; the client-side display-value dump framework
  (`web/export.js`) is retired to an inert frozen object.
- ship-version checklist item 5b: every shipped user-facing feature must add a what's-new
  catalog entry with an accurate `href` AND `target`.

### Fixed
- Rebalance: the engine previously computed a multi-account symbol against ONE account
  (wrong trade sizes; the drawer's duplicate rows orphaned the first row's cells so its
  target edits never computed); footer 目標合計 double-counted multi-account symbols.
- Deep review: `Content-Disposition` filenames are now sanitized (ASCII fallback +
  RFC 5987 `filename*` — a CJK symbol name previously 500'd the download and CRLF could
  inject headers); a zero-priced symbol no longer crashes the rebalance preview with
  `DivisionByZero` (excluded honestly per the no-fabrication rule).
- What's-new audit fixes: seen-version clamped to the running version; corrupt stored
  seen value degrades to never-seen; the open-ack no longer wipes the panel's own group
  NEW pills; the sidebar pill no longer stretches full-width; the arrival scroll
  re-asserts after async layout shifts and clears the sticky topbar.

## [v0.1.17] - 2026-07-13

Blueprint Phase 3 batch 2: alerts taxonomy v2 — proactive market-risk alerts, each
pushed to the phone via the v0.1.14 channels. Rule correctness gated by an independent
deep review.

### Added
- **Four new alert rules** (`strategy/alerts.py`, pure + fed; thresholds editable in
  the 預警規則 editor, all default-on for existing installs):
  - `drawdown_from_peak` (held **and** watched) — price vs the trailing 52-week high;
    risk at −20%, warn at half that (−10%), one editable knob. A minimum 30-session
    window guard prevents a freshly-registered symbol's thin series from firing a
    spurious peak drawdown.
  - `vol_spike` (held) — 30-day annualized volatility ≥ 1.8× the 90-day baseline.
  - `rebalance_drift` (held with a target) — Swedroe **5/25**: fires when the weight
    drift crosses the **tighter** of a 5-percentage-point absolute band or 25% of the
    target (whichever is hit first), so a small allocation gets a proportionally tight
    band. No target set → silent.
  - `consensus_change` (held **and** watched) — analyst rating worsened by ≥ 0.5
    (1=best…5=worst) or mean target price cut by ≥ 10%, latest snapshot vs the closest
    one ≥ 7 days older.
  Every rule flows through the existing engine → dashboard embed + `GET /api/alerts` +
  `alert_scan` → `alert_events` → push (24h-debounced, subscription-filtered); messages
  carry percentages/symbols only, never account amounts (push-boundary discipline).
- **Target-weights config (owner ruling D8):** `strategy/target_weights.py` store +
  `GET/PUT /api/target-weights` (each weight ∈ (0,1], Σ ≤ 1, registered symbols only,
  Decimal-string ratios) + a 目標配置 section in the 預警規則 tab (per-symbol % with a
  live sum indicator). The stored targets drive `rebalance_drift` AND prefill the
  existing rebalance preview — single source of truth.
- **Feeding seam** `api/alert_inputs.py` — one conn-bearing assembler computes the fed
  inputs (52-week position, 30/90-day vol, target weights, consensus deltas) for all
  three alert surfaces, preserving the single-source equality invariant (strategy/
  stays pure; the api layer feeds it, mirroring the calib_gap precedent). New
  `snapshots_store.snapshot_on_or_before` as-of reader for the consensus baseline.

### Fixed
- **Swedroe 5/25 band operator (deep review 2026-07-13):** `rebalance_drift` computed
  `max(absolute, 25%×target)`, which inverted the rule — the relative leg was dead code
  for small targets and *raised* the band above 5pp for large ones (an 8pp drift on a
  50% target was silent). Corrected to `min` (the tighter leg governs); the tests that
  had locked the wrong behavior were rewritten to the canonical cases.

## [v0.1.16] - 2026-07-12

Notification-channel setup guides (owner request: lower the setup barrier).

### Added
- **Per-channel 設定步驟 SOP** — each channel card gains a collapsible, numbered
  zh-TW guide: ntfy (install app → enable → copy topic → subscribe → test; explains
  that a successful test with a silent phone means the subscription/permission is
  missing, not the server), Telegram (@BotFather → @userinfobot numeric chat_id →
  the mandatory /start step → test; group = negative id), Email (Gmail example:
  smtp.gmail.com/587/STARTTLS, the app-password requirement — a normal password is
  rejected with 535 — and the port↔encryption pairing that otherwise times out).
  Panel subtitle now states when real alerts dispatch (weekday post-close scan,
  ~15:00 Taipei).

## [v0.1.15] - 2026-07-12

Hotfix for the v0.1.14 notification channels (owner field report, same day).

### Fixed
- **Enable toggles now persist on click.** The channel/quiet-hours toggles only
  flipped a CSS class until the separate 儲存 button was pressed, which read as
  "enabled but cannot be turned off". A click now sends a minimal
  `PUT {channel:{enabled}}` immediately (optimistic flip, revert + toast on
  failure); the save buttons still persist field edits.
- **Provider error reasons are surfaced.** A failing test-send showed only the
  bare HTTP status line; Telegram's response body carries the actionable reason
  (e.g. "Bad Request: chat not found" = the bot was never /start-ed or the
  chat_id is wrong). ntfy and Telegram errors now include the response-body
  description (bounded, still secret-redacted); `chat_id` is trimmed before
  sending; the Telegram card documents the /start requirement and where to find
  a numeric chat_id.

## [v0.1.14] - 2026-07-12

Blueprint Phase 3 batch 1: multi-channel push notifications — alerts and rule-signal
transition events finally reach the owner's phone. Security-audited (independent ★
deep review: secret handling probed clean across every exception path).

### Added
- **`ops/notify.py` leaf module** with three channels (owner decision D1, 2026-07-12
  — all verified free): **ntfy** (JSON publish endpoint, default https://ntfy.sh,
  auto-generated long random topic — the topic IS the read secret; `allow_redirects`
  disabled, 3xx = failure), **Telegram** (bot sendMessage, PLAIN text — no
  parse_mode, no Markdown-injection surface), **Email** (stdlib smtplib, zero new
  dependencies; STARTTLS/SSL/none). **Multi-channel fan-out:** every enabled channel
  receives each message; a failing channel is isolated (logged, never blocks the
  others or the scheduler). Timeouts on every call; every channel wraps errors
  through a redactor so tokens/passwords can never reach logs, run details, or API
  responses (probe-verified incl. requests exceptions that embed the token URL).
- **Dispatch pipeline:** `alert_events` gains `notified_at` + `notify_attempts`
  (additive migration; independent of the on_alert `consumed` path). The alert_scan
  tail pushes undispatched events (covers signal_scan's 14:55 events): subscription
  filter → quiet hours (Asia/Taipei, midnight-wrap aware; hold-then-release;
  malformed config fails OPEN — an alert system must not silently suppress) →
  zh-TW message (rule label + symbol, no amounts) → **atomic claim** per event
  (closes the cron-vs-manual double-send race) → fan-out → all-channels-fail
  releases the claim and bumps `notify_attempts`, giving up at 3 so a permanently
  broken channel can never starve newer alerts; cap 10 events/run (no
  post-outage flood). Idempotent: claimed events never resend.
- **Settings UI (canonical settings → 預警規則 tab):** 通知通道 section — three
  channel cards (enable / fields / save / 傳送測試訊息), quiet hours, per-rule
  subscriptions incl. the `signal_*` events (default all on). Secrets masked on
  read, placeholder-preserving on write (LLM-key convention); ntfy topic shown
  with a copy affordance and a "topic = password" hint.
- **API:** `GET/PUT /api/notify/config`, `POST /api/notify/test` (per-channel test
  send). **Guest-mode lockdown (security review):** on a guest instance (public
  demo) PUT/test return 403 and GET masks the ntfy topic — notification channels
  are configured on the protected production site only. PUT validates: no userinfo
  in the ntfy server URL, http(s) scheme only, strict SMTP host shape, 400 on junk.

### Fixed
- **Legacy-DB boot crash caught by the deploy gate:** the `notified_at` index lived
  inside the initial DDL script and ran before the column migration on live DBs
  whose `alert_events` predates the column — the demo instance crash-looped at
  startup. The index is now created after `_add_column_if_missing`, with a
  legacy-schema regression test (a fresh-DB suite cannot see this ordering class).

## [v0.1.13] - 2026-07-11

Blueprint P2 "technical-rules engine" release: local, stateful, auditable rule
signals for every held AND watched symbol — TechScore, transition events into the
alert stream, drawer signal chips, an LLM variable, and health-check v2.5 that
interprets (never computes) them. Three batches, each gated by an independent
deep-review audit.

### Added
- **Rules engine core `strategy/rules/`** (P2-2A): frozen `rules-v1` params; four
  evidence-based rules — MA200 trend filter (±2% hysteresis band + 2-day confirm),
  SMA50/200 golden/death cross with volume confirmation (×1.00 confirmed / ×0.75
  unconfirmed = 54/72 / ×0.85 unknown — never faked) and linear age decay,
  12-1 momentum (skip-month convention; flat dead-band forces score 0), RSI(14) +
  52-week context (halved magnitude by design); composite TechScore 0–100 with
  per-rule contribution audit trail, coverage renormalization over evaluable rules,
  and a deterministic zh-TW evaluation-context sentence. Pure Decimal, honest
  `None` on thin data, `params_version` stamped on every result (replay discipline).
  **Deep-review calibration (2026-07-10, recorded here): cross decay = 60 sessions**
  (cited death-cross evidence: ~random after ~30 days — half-weight at day 30),
  superseding the initial 120; unmeasured momentum is labelled honestly (dedicated
  uptrend/downtrend context labels — never called "weakening").
- **Signals API + transition events** (P2-2B): `GET /api/signals` (+ single-symbol
  variant) serving every registered instrument with a `held` flag; history window
  derived from params (260 sessions → 583 calendar days). `signal_states` derived
  cache (rebuildable; truth stays `prices`) detects regime transitions with HOLD
  semantics — trend fires only on confirmed up↔down, momentum only on
  positive↔negative, both holding their last direction through neutral/flat noise
  (without this the momentum event was unreachable); fresh golden/death crosses
  fire with a both-days_ago-present guard. Events land in `alert_events`
  (`signal_trend`/`signal_cross`/`signal_momentum`), first scan seeds silently
  (no event storm), same-day re-scans coalesce, `params_version` changes reseed
  silently. New `signal_scan` job (weekdays 14:55 Asia/Taipei, before alert_scan);
  the `'all'` alert-subscription wildcard deliberately EXCLUDES `signal_*`
  (explicit listing subscribes — AI-card cost stays opt-in).
- **Watchlist coverage** (owner decision 2026-07-11, recorded here): watched
  (registered but unheld) symbols get the full signal treatment — scan seeding,
  transition events, API rows (`held:false`), and drawer chips — a watched symbol
  is an entry candidate (a golden cross there is exactly the wanted alert). A
  sold-but-registered symbol stays tracked as a re-entry candidate.
- **Drawer 技術訊號 chips** (P2-2B/2C): TechScore + coverage, four rule chips with
  key evidence, and the engine's condition sentence, for held and watched symbols;
  neutral accent styling (signals are not P&L); honest 資料不足 empty state.
- **`rule_signals_json` variable** (P2-2C): registry grows 33→34 (price category);
  fed via `external_vars` from the SAME evaluation/serialization seam as
  `/api/signals` (byte-identical, test-pinned); honest partial pass-through
  (per-rule nulls) and thin-data degrade with an explicit reason.
- **Health-check strategy v2.5** (library `official-v5`): cites TechScore/coverage,
  each rule's state with its key evidence number, and the condition sentence
  verbatim — interpret only, never recompute; unheld symbols are framed as entry
  assessment (建倉評估) instead of add/trim; unavailable data is stated honestly.
  Task presets reference strategies by name — no preset change.
- **Opt-in task universe "holdings + watchlist"** (`mode:"all_registered"`):
  explicit wizard option with a per-symbol cost hint; default stays holdings-only.
  A registered symbol without prices takes the zero-LLM anomaly-card path.

### Fixed
- `volume_signal`'s trailing-gap trim (v0.1.12) is consumed by the cross rule's
  volume confirmation — an unknown-volume cross day degrades the confidence
  modifier to ×0.85 instead of raising or fabricating.

## [v0.1.12] - 2026-07-09

Blueprint P1 "data foundation" release: trading volume across all three markets,
5-year price history, and the analyst-consensus variable — the data bedrock for the
upcoming rules engine (P2) and backtest/decision-quality loop (P4).

### Added
- **Trading volume end to end** (P1-1A): the yfinance provider now reads the `Volume`
  column in history/latest fetches into the long-reserved `prices.volume` column
  (canonical integer strings — volume is not money, the 2dp rule never applied);
  `get_price_history`/`PriceRead` gain an additive `volume` field; the insight
  generation AND preview paths feed `VarContext.volumes` (aligned 1:1 with closes,
  probe-gated) so `technical_signals_json` now emits its volume section
  (`ratio_to_avg` + `surge`). Live-verified on the test site: 13 instruments at
  98.9–100% recent-window volume coverage after the deep backfill.
- **FinMind TW quote-history fallback** (P1-1A): `FinMindProvider` gains
  `QUOTE_HISTORY` support (`TaiwanStockPrice`: OHLC + `Trading_Volume`), appended
  after yfinance in the TW chain — removing the yfinance single point for price
  history. Token-gated exactly as the dividend path.
- **5-year price history** (P1-1B; owner decision 2026-07-08 supersedes the
  blueprint's 3-year recommendation, recorded here): new `history_backfill_days`
  setting (default 1825, env-overridable) replaces the two scattered 365-day
  literals (quick-register backfill + smart backfill windows; the
  extend-to-first-acquisition logic is unchanged). The 52-week position now reaches
  its full 252-session window.
- **Analyst-consensus variable `consensus_json`** (P1-1C): new
  `pricing/consensus_source.py` fetches yfinance's two light endpoints
  (`analyst_price_targets` + `recommendations_summary`; never the heavy
  `Ticker.info`) into idempotent per-symbol snapshots — target prices (Decimal
  strings under the 4dp float-noise cap), this/last-month rating distributions,
  and locally computed weighted `rating_score` (1–5) + `upside_vs_mean_pct`.
  Convention (invariant #1): consensus numbers are fetched from the finance API and
  computed locally — the LLM only interprets them. New `consensus_daily` job
  (09:10 Asia/Taipei; manual trigger via the existing
  `POST /api/scheduler/jobs/{id}/run`). Variable registry grows 32→33 across
  9→10 categories (new 分析師共識 category, mirrored in `web/vars.js`); symbols
  without coverage degrade honestly with an explicit no-coverage reason.
- **Health-check strategy v2.4** (template library `official-v4`): adds a consensus
  section — target range/mean vs current + upside, rating distribution and its
  month-over-month shift — and must state 無分析師覆蓋 explicitly when the variable
  is unavailable. Official byte-freeze / reset-to-official conventions unchanged;
  the task-preset pack references the strategy by name and needed no change.

### Fixed
- **`volume_signal` None-safety + trailing-gap trim** (caught by live verification,
  not by the green unit gates): the newest TW price row is written by the twse
  latest-quote provider, which carries no volume, so the volume signal would have
  degraded — or raised on an interior gap — on every TW/MY symbol daily. It now
  accepts None-padded series (`Sequence[Decimal | None]`), trims trailing
  volume-less sessions (the next history refresh heals them a day later), and
  degrades honestly on an interior-window gap; the type bridge cast in
  `llm_insight.variables` is gone.

## [v0.1.11] - 2026-07-08

The AI-input optimization program release: four feature batches + a full-system
deep-review remediation, bundled per the 2026-07-05 decision.

### Added
- **Official task pack** (`POST /api/insight-tasks/official-pack`): one-click creation of
  the three official insight tasks (持倉週報 Sat 09:00 · 個股健檢 Mon 09:00 · 市場週報
  Sat 09:30) with strategies + weekly crons; idempotent via a `preset_key` provenance
  column (rename-safe). Official template library v2/v3 (`official_templates.py`) with
  reset/from-template endpoints; system prompt v2; templates 持倉週報 v2.1 / 個股健檢
  v2.3 / 市場週報 v1.1.
- **per_market scope**: one insight card per held market (TW/US/MY) with a codified
  zero-leak guarantee — portfolio variables slice to the market
  (`portfolio/market_view.py`, `VarContext.market`); whole-portfolio vars carry honest
  scope notes; market cards strip model-emitted predictions at store time.
- **Technical signals** (`portfolio/technicals.py`, pure Decimal): Wilder RSI(14),
  MA20/60 golden/death cross + days-ago, 52-week position (honest window), swing trend
  structure, probe-gated volume; bundled as `technical_signals_json`. CNN Fear & Greed
  local five-zone classification + 7-day trend (`fear_greed_json`, standalone variable).
- **News content pipeline** (`portfolio_dash/news/`, new module): FinMind (中文) +
  yfinance (英文, incl. .TW/.TWO) + Yahoo-TW discovery → general HTML fetcher
  (http(s)-only, bounded, non-prose guard) → default-LLM organizer (editable official
  news prompt v2, `GET/PUT/POST /api/news-prompt(/reset)`) → **separate SQLite news DB**
  (`news.db` beside the ledger DB — decision 2026-07-06: larger text volume off the
  transactional DB, multi-account-share-ready; included in the daily backup) → precise
  per-symbol mentions index (held-universe allowlist) → `symbol_news_json` variable +
  個股健檢 news section. Nightly `news_daily` job (06:00). News library page
  (`news.html`): filters (stock/source/date/server keyword), row → full-summary modal.
- **Unified AI attribution** on every LLM surface (`fmt.aiAttrib`): model · token N ·
  $cost on insight cards, dashboard AI panel, and the news modal; `insights` gains
  `tokens_in/tokens_out`, `organized_news` gains `model`.
- **LLM request ledger**: `llm_usage` gains `cache_tokens` (provider-reported cached
  prompt tokens, captured defensively); `GET /api/llm/requests` (paged, agent/model/
  time-window filters, Taipei-normalized timestamps); Request 明細 panel on the AI 與額度
  tab; 執行歷史 LLM-kind rows deep-link 「查看 AI 請求」 to the run's request window.
- **Database statistics panel** (`GET /api/db-stats`): row counts for every table across
  BOTH SQLite files, grouped by category, with oldest-record dates and file sizes —
  the owner observes before deciding retention (no pruning built).
- **Site-wide pagination**: shared `web/pager.js` (windowed pages + jump); real paging on
  Request 明細, 執行歷史 (server job filter), 系統操作記錄, news, insight cards, AI 戰績,
  持倉健診 (server-side symbol grouping), trades ledgers ×4 (server account/date
  filters), cash movements. `/api/insights` now bounded ({rows,total_count}, default
  100/max 500); `/api/ai-score` rows paged; `/api/cash` gains limit/offset.
- **每頁筆數 setting**: backend-persisted `ui_prefs` (`GET/PUT /api/ui-prefs`,
  page_size ∈ {20,50,100,200}, default 50) editable in settings 一般; `window.pdPrefs`.
- Loop-2 scoring rubric v2 (direction 40 / citation 30 / scenario 20 / timeliness 10)
  and `price_at_create` baseline snapshot (decision Q1c): cards score against the close
  the model actually saw.

### Fixed
- **Deep-review remediation** (three parallel xHigh reviews, 2026-07-06/07): insight
  schedule bind/unbind now syncs the live APScheduler (new crons fire without restart;
  deleted tasks stop firing; invalid cron → 400); disabled/archived tasks are enforced
  at execution (cron skips with reason; manual run → 409); single Asia/Taipei day-anchor
  clock (`shared/clock.app_now`) across cron/API/backup/news (fixes the UTC/Taipei split
  that re-broke the day-anchored cache on the scheduler path); news mentions merge
  instead of being wiped on headline→organized upgrade; archived tasks excluded from
  Loop-2 scoring; cron overlap guard; preview/run technical close-window unified (400d).
- **Frontend attention surfaces**: news modal opaque (undefined CSS tokens); alert bell
  copy humanized (display-quantized %, account display names, zh); 產生洞察 button wired
  (run/menu/redirect); mobile CJK vertical collapse; CLS min-height reservations;
  pipeline-hub copy productized with live counts; legacy `ledger.html`/`input.html` and
  all standalone `settings-*.html` converted to redirect stubs (single canonical tabbed
  settings surface — ends the dual-surface drift class); stale localStorage auth copy
  replaced; scraper-garbage summaries prevented (fetcher non-prose guard + organizer
  prompt rule).
- **Stale-asset cache class bug**: static files now send `Cache-Control: no-cache`
  (ETag revalidation) and every local asset tag carries `?v=<version>` (rerunnable
  `scripts/stamp_asset_version.py`; guarded by `test_static_cache_discipline.py`) —
  a cached old `format.js` had blanked the insights page after deploy.
- **iOS Safari alert panel**: the bell panel is portaled to `document.body` (a
  `backdrop-filter` ancestor hijacks fixed-position containing blocks on Safari),
  anchored from the bell rect, `100dvh`-aware; mobile panel fully on-screen.
- LLM error taxonomy honesty (F2), day-anchored cache fingerprint (F5), accounts
  catalog for the parser (F7), and the first-run 4-call/zero-card failure (F1:
  in-prompt JSON schema contract + tolerant parse) — the ignition-round fixes.

### Changed
- `log_usage` timestamps now use the Asia/Taipei app clock (single-clock discipline).
- 用量與趨勢 chart regression on the tabbed settings path fixed (guarded wiring).
- mypy strict now also covers previously-untyped test files touched this cycle.

## [v0.1.10] - 2026-07-05

LLM pillar IGNITED: the first live batch runs on the test instance exposed seven
defects invisible to the mocked suite — all fixed with regression tests — followed
by the AI-input optimization program (audited all 7 LLM surfaces, shipped the
official-v2 template library) per the 2026-07-05 user directives.

### Fixed
- **Structured output was 100% broken on live providers**: LiteLLM's capability map
  returns False for every ``openrouter/*`` id, so ``response_format`` was never sent
  and nothing in the prompt asked for JSON — models replied prose and every insight
  run failed both role models. ``shared/llm.py`` now always appends a schema-derived
  JSON-only contract and tolerates fenced/prose-wrapped replies before failing.
- **Same-day cache never hit live**: the fingerprint hashed the LLM-facing render
  whose ``{{as_of}}``/``{{now}}`` carry seconds — every re-run billed a fresh call and
  stored a duplicate card. The fingerprint now hashes a day-anchored second render
  (same-day identical data reuses; an intra-day data change still regenerates); the
  R4 anomaly snapshot fallback drops to day granularity (no same-day duplicates).
- Mid-run LLM failures were all reported as ``budget_exhausted_mid_run`` — the reason
  now carries the exception kind (+ message in ``detail``).
- AI text input was unusable beyond the example account: the parse prompt never
  listed valid account ids (the model invented ``charles_schwab``). It now carries the
  live account catalog, a ``<today>`` anchor (yearless dates resolve to the most
  recent PAST occurrence), and a multi-transaction example.
- Run rows showed a blank status while running and finished with a UTC stamp next to
  a +08:00 start; ``{}`` preflight bodies shadowed the saved combo into a bogus R3.

### Added
- **Official template library** (``llm_insight/official_templates.py``, versioned;
  ``GET /api/prompt-templates`` / ``POST /api/system-prompt/reset`` /
  ``POST /api/strategy-prompts/from-template``): system prompt v2 (timeliness-first,
  output structure, tags vocabulary, confidence calibration), 持倉週報 v2.1 and
  個股健檢 v2.1 (prediction spec, multi-account-total + FX-magnitude guards found by
  the live A/B). Fresh installs seed the official system prompt.
- **Master scoring rubric v2**: four weighted dimensions (direction 40 / citation 30
  / scenario 20 / timeliness 10), score anchors, an explicit miss definition,
  evidence-required notes — the narrative score is Loop-3's learning signal and had
  no rubric. Calibration safety lock now has a concrete 600-word cap + a timeliness
  rule; miss samples join the failed card's own claim/prediction/outcome.
- **Data diet**: ``price_history_json`` sends the last 30 sessions daily + every 5th
  beyond (checkup input −33%); ``fx_json`` carries an in-band unit note.
- ``settings-prompts``: the strategy-card section — still a design stub with local-only
  saves — is now fully wired to ``/api/strategy-prompts`` (load/save/toggle/archive/
  add) plus 重置回官方版 / 從官方模板庫新增 buttons. ``settings-llm``: the add-model
  drawer prefills the provider's public ``api_base`` and reuses the last same-provider
  model's context/output/timeout/retry settings.

### Changed
- Preflight G1 (unscheduled) now WARNS instead of hard-failing the verdict — manual
  triggering is a legitimate mode and the pipeline hub's trigger node already said
  warn (human sign-off 2026-07-05; supersedes the §7.2 fail).
- Deleting an insight task hides its historical cards/evaluations from
  ``/api/insights``, the dashboard embed, and ``/api/ai-score`` (rows stay in the
  tables — spec 4.1 archive semantics).

## [v0.1.9] - 2026-07-03

Mobile (iPhone) layout pass — layout only, zero functional change, verified at
390×844 across every page.

### Added
- **Mobile layer (≤760 px)**: the fixed 196 px sidebar becomes an off-canvas
  drawer behind a topbar hamburger (backdrop / nav-click / Esc closes; desktop
  collapse state neutralized inside the drawer; the inbox badge rides along);
  every multi-column grid collapses to one column (KPIs keep two); tables
  scroll inside their own wrap with a 640 px readable minimum so the page body
  never scrolls sideways; iOS ergonomics — 16 px inputs (kills the focus
  auto-zoom), 38–40 px touch targets, safe-area bottom padding, full-width
  modals/toasts/search overlay.
- Probe-driven overflow fixes: 幣別報酬折分 and datasources source tables get
  their own scroll regions; 5-tab segmented bars wrap; KPI sublines wrap inside
  the card; ECharts hosts clip until their resize catches up. Result: body
  horizontal overflow 0 px on 9/10 pages (dashboard 5 px sub-perceptual
  residue), down from 260–957 px. Desktop breakpoints untouched.

## [v0.1.8] - 2026-07-03

Round 6 (all 8 user-approved items): the system now manages MONEY, not just
stocks — per-account cash pools with a dedicated 資金管理 page — plus monthly
KPI snapshots, an inbox badge, and a handful of daily-flow refinements.

### Added
- **Cash pools (item 7, user spec)** — the fifth ledger: ``cash_movements``
  (入金/出金) + pure ``portfolio/cash.py`` balances per (account, currency):
  deposits − withdrawals ± FX sides ± trade settlements + cash-family dividend
  nets; opening inventory deliberately cash-neutral (record an initial deposit
  to balance history); operational view only — XIRR untouched. New 資金管理
  page manages all of it in one place (balance cards with negative-pool
  highlighting, deposit/withdraw + FX forms, movements ledger with
  edit/delete); dashboard gains a 各帳戶現金 mini panel; 換匯 entry moved here
  from 交易輸入 (one guarded door; CSV bulk path unchanged).
- **Negative-pool guard (item 2)** — the cash analog of the oversell guard:
  any entry / FX conversion / edit / delete that would drive a pool below zero
  answers 422 ``negative_cash`` (pure delta check, nothing written) until
  explicitly acked; live-verified (schwab pool −184,000 → covering deposit →
  conversion passed → USD pool math exact).
- **月度 KPI 快照 (item 8)** — nightly job upserts the current month's row
  (total value / return / rate / XIRR / by-currency); the value standing at
  month rollover IS the month-end record; ``GET /api/snapshots`` + a 月度成績
  dashboard panel (table lookup, no history replay).
- **Inbox sidebar badge (item 4)** — pending dividend count on the 交易帳本
  nav item, visible from every page.

### Changed
- 交易帳本 gains the 代號/名稱 search + date-range filters (item 1); the
  sector donut legend no longer overlaps the chart (item 3); trade input
  remembers the last-used account (item 5); one-step add offers a 記一筆買入
  handoff with the symbol prefilled (item 6 — the reverse direction, entering
  a trade for an unlisted symbol, already auto-registers since v0.1.4).

## [v0.1.7] - 2026-07-03

Round 5: the dividend inbox goes all-market and self-feeding — and booking a MY
dividend for real exposed (and fixed) a core rebuild crash that had been latent
since the schema was born.

### Added
- **All-market dividend detection** (R5 item 1): the inbox books per the
  ACCOUNT dividend model — TW cash (as before) · **US DRIP** with 30%
  withholding and an ESTIMATED reinvest (price = last stored close ≤ pay/ex
  date, shares = net/price; clearly marked and ledger-editable; without a
  stored price the item is not confirmable 缺再投資價) · **MY single-tier
  NET** · **TW 配股** (股票股利 X 元面額制 → held × X/10 zero-cost shares;
  cash+stock of one event are independent items with per-family ledger
  suppression). Live-verified with real yfinance events: NVDA DRIP (withhold
  1.50, reinvest 0.016 sh @ the real backfilled close) and Maybank NET 990
  booked on the test instance.
- **dividend_inbox_scan job** (R5 item 2): daily post-close sweep (runner seam —
  scheduler never imports api) refreshing events for acquired symbols and
  reporting the pending count in the run history (`… · 待確認 N 筆`), so the
  inbox grows by itself.

### Fixed
- **CORE: `DividendType.NET` crashed every rebuild** — bookable via CSV since
  the schema existed, but `cost_basis` routed every non-CASH type to the
  shares-branch ("requires reinvest_shares" ValueError → dashboard 500), and
  trend/XIRR silently dropped NET cashflows. Per domain-ledger.md, NET is
  cash-family (reduces adjusted cost, counts as an XIRR inflow): ONE definition
  (`shared.models.enums.CASH_DIVIDEND_TYPES`) now feeds all three replay sites;
  regression tests book a NET row end-to-end (dashboard + recompute stay 200).

## [v0.1.6] - 2026-07-03

Round 4 (user decisions on the round-3 report): the auto-import inbox becomes
real, history backfill gets position-aware windows + FX history, export audits
consolidate into the action log. Live-verified with REAL FinMind data (a genuine
TSMC dividend detected, confirmed, and booked on the test instance) before
promote.

### Added
- **FinMind 配息偵測 → 待確認匯入 for real** (decision A). Detection window per
  TW symbol = its earliest acquisition date (first BUY or opening build) →
  today; entitlement = shares held going INTO the ex-date (new dated
  ``holdings.shares_on``, strictly-before rule). Items suppress themselves when
  the dividend ledger already has a row within ±45 days of the ex-date or when
  explicitly skipped (fingerprint persisted); the pending list is computed on
  read — self-healing, nothing auto-written, 絕不自動入帳. CONFIRM recomputes
  server-side and writes a CASH row (TW model: net = gross) dated
  pay-date-else-ex-date. Endpoints: ``GET /api/dividend-inbox[?refresh=1]``
  (targeted FinMind sweep) + bulk ``confirm``/``skip``; inbox UI groups by
  symbol (collapsible, per-group 全部確認) with a 重新偵測 progress flow.
  v1 scope: TW cash (US DRIP needs broker data; MY a small extension;
  stock-only events excluded) — recorded as roadmap.
- **Smart backfill windows** (item 2): prices default 12 months, extended per
  symbol to its first acquisition date when older (watch-only symbols keep the
  default); NEW FX-history backfill (USD/TWD, USD/MYR, MYR/TWD via yfinance
  ``fetch_fx_history`` + registry/refresh seams) from the earliest ledger flow
  date — the trend chart and XIRR now have a rate on-or-before every flow
  (live-verified: the demo trend went from empty to 179 points). Registration
  initial window 92d → 365d; ``backfill-history`` with explicit ``days`` keeps a
  uniform window.

### Changed
- **Exports audit only in 系統操作記錄** (item 3): ``log_export_run`` removed;
  ``GET /api/scheduler/runs`` filters legacy ``export:*`` rows — the 排程執行歷史
  is a pure scheduler view again (double-recording gone).
- **Datasource connection test retries once** (item 5, 1.5 s spacing): transient
  TWSE/twstock probe failures from the VM stop tripping the health light; the
  fetch chain itself always degraded correctly.
- Ledger symbol/account editability stays as-is (item 4, decision A): guarded by
  registration requirement + oversell replay + the in-modal impact warning, with
  every correction traceable in the action log.

## [v0.1.5] - 2026-07-03

Round 3 (user-directed, 12 items): input-center completion, ops observability
(system action log + run-history sources), per-market quote routing made real,
full-field instrument editing, float-noise price caps — verified live on the test
instance (23 API checks + 16 browser steps + full-site screenshot review, all
green) before promote.

### Added
- **Single-entry 股利/換匯/期初 write for real** — the three forms build a
  one-row CSV and commit through the SAME tested `/api/import` preview+commit
  seam (error rows block with the backend reason; warn rows go through the ack
  confirm). 配股 mode relabels the amount field to 配股股數 and hides Net. The
  CSV dropzone is a REAL client-side file upload (FileReader → textarea →
  preview; drag-drop; per-kind column hints).
- **系統操作記錄 (system action log)** — an app middleware records every
  mutating `/api` call (timestamp, actor, Chinese action label, endpoint, HTTP
  outcome, duration; never bodies); `GET /api/system-log` + a third panel on the
  排程 page. Previews/what-ifs excluded; newest 5000 kept.
- **Run history names its sources** — job detail now reads
  `12 ok, 0 failed [yfinance: 0056, 2330, …] failed: 8299` (one `_summarize`
  seam covers every quote/history/dividend job); scheduler page shows Chinese
  job names with ids beneath, full detail on hover.
- **Per-market quote order, stored and REAL** — `data_source_market_order` +
  `PUT /api/datasources/market-order`, consumed by `default_registry(conn)`
  (scheduler crons, manual refresh, quick-add alike). Settings page: three
  market cards, drag to reorder, ✕ remove, ＋ add capable source, health dots
  from the source list. Live-verified: putting yfinance first made the next TW
  refresh answer entirely from yfinance. Supersedes the per-ACCOUNT fallback
  chains, which were stored/editable but consumed by NOTHING (and keyed on the
  wrong concept — accounts decide fees/dividends, markets decide quote routing);
  that endpoint + wire fields are removed.
- **Instruments full-field edit** — 名稱/產業/板別(TW dropdown)/ETF/目標價
  editable for ALL markets (US included); ledger search matches 代號 first then
  名稱; tx edit modal warns that changing 代號/帳戶 moves the row to another
  position.

### Fixed
- **幣別組成 rendered 權重 NaN%** for any currency holding 2+ positions —
  Decimal-string weights were summed with `+` (string concatenation). Ratios are
  display-only; now coerced explicitly.
- **PUT /api/instruments target_low null now CLEARS the alert** — exclude_none
  silently dropped explicit nulls, so clearing never worked (exclude_unset now).
- **Dividend CSV type normalization** — a lowercase `cash` was stored raw and
  poisoned `DividendType()` readers; the importer now uppercases and
  hard-rejects unknown types.
- **Float-noise cap at the price write seam** (human sign-off 2026-07-03) —
  prices capped at 4 dp, FX rates at 6 dp, ROUND_HALF_UP, cap-never-pad
  (yfinance float tails like `305.364990234375` no longer stored); recorded in
  `data-and-pricing.md`.
- The AI-input design state-switcher is retired (degraded panels driven only by
  real API errors, doubling as usage-time hints when AI is enabled later); the
  AI screenshot dropzone honestly reports Vision is not yet wired.

## [v0.1.4] - 2026-07-02

Position-management UX round 2 (user-directed): one-step onboarding, ledger row
corrections, app-wide progress visibility, deploy build identity — plus three real
bugs the new tests/live-verification forced out. All changes verified on the live
test instance with real providers (16 API checks + 18 real-browser click-through
steps, all green) before promote.

### Added
- **One-step instrument add** — `POST /api/instruments/quick` (new shared
  `api/instrument_service.py`): probes the TW board, **requires a real fetched
  quote** before registering (typo guard; 422 `quote_not_found` → explicit
  user-confirmed `force` retry), auto-fills the display name (TW: twstock static
  code table → yfinance fallback; US/MY: yfinance), and **backfills ~92 days of
  daily closes** so the symbol drawer chart renders immediately. 觀察清單 add UI
  collapses probe→confirm→detail-form into symbol + market + one button (verified
  live: `2884` → 玉山金, TWSE 上市, 現價 33.70, history backfilled).
- **Trade input auto-registers unknown symbols** — the manual commit infers the
  market from the account's settlement currency (TWD→TW / USD→US / MYR→MY) and
  runs the same quick-register (same real-quote guard); an unregistrable symbol
  still writes **nothing** (400 `symbol_auto_register_failed`). Preview shows an
  info-severity note instead of a hard error; success responses carry
  `auto_registered {symbol, name, last}`.
- **Ledger row corrections** — `PUT/DELETE /api/ledgers/{transactions,dividends,fx}/{id}`
  and `/ledgers/openings/{account}/{symbol}` ("append-only in spirit": explicit
  user corrections, never silent mutation). Every mutation **replays the would-be
  ledger through build_book first**; a correction that would strand a later sell
  answers 422 `oversell` until explicitly acked (dashboard then shows the flagged
  賣超 state). Frontend rows gain 編輯/刪除 with per-kind modals + danger confirms.
- **Progress system (app-wide)** — `web/api.js` (the single fetch seam) tracks all
  in-flight requests and drives a global top progress bar (150 ms anti-flicker);
  `pdBusy()` spinner/disabled states on action buttons (double-click-safe);
  `toastProgress()` persistent toasts for long operations (更新報價 / 重算 /
  歷史回補) — no network wait can look frozen anymore.
- **Build identity** — `GET /api/health` now reports `{version, commit, release}`
  (short git hash + exact tag on HEAD or `"unreleased"`, via new
  `shared/buildinfo.py`; env-overridable, never raises). Sidebar shows
  `vX.Y.Z · hash` on every page with an amber 未發行 marker for non-tag builds;
  settings 一般 row carries the full string; `verify_live.py --expect-release`
  asserts a prod promote runs the tag.
- **`POST /api/actions/backfill-history {days}`** + 觀察清單「回補 3 個月歷史」
  button — gives existing instruments the 3-month chart window (new registrations
  get it automatically).

### Fixed
- **Trend replay could 500 the dashboard on an acked-oversold ledger** —
  `timeseries.daily_value_series` built its per-day books without
  `allow_oversell`, bypassing the 2026-06-18 degradation the main book already
  had. Now degrades: an oversold day marks the trend point `incomplete` instead of
  raising. (Never-500 degradations must cover EVERY replay call site — LESSONS.)
- **False oversell warnings for opening-backed positions** —
  `data_ingestion.holdings.current_shares` summed only the transactions table,
  ignoring opening inventory (期初) and stock/DRIP dividend shares; selling such a
  position raised bogus 賣超 warnings and `held` flags undercounted. Now counts
  all four share sources (same replay rule as `build_book`).
- **twstock was mis-grouped as a `[probe]` extra** — deployed venvs never had it,
  silently disabling both the TW quote-chain twstock fallback AND the TW name
  lookup (found live: names came back empty). Moved to runtime dependencies;
  `lookup_name` now degrades per-source (twstock failure still falls through to
  yfinance).
- **重新探測 now persists** — it probed and toasted but never saved, so an
  unresolved TW board stayed unresolved forever; it now PUTs the result and
  resolves `board_status`.
- **Input-center design-stub prefill retired** — the form booted with fake
  2026-06-11 / 2330 / 1000 / 612.5 values; now today + empty fields with a neutral
  pristine state (no red errors on an untouched form).

### Changed
- Classic `POST /api/instruments` delegates to the shared quick-register service
  with `force=true` (back-compatible: a provider outage never blocks an explicit
  registration) and gains the same name auto-fill + history backfill.

## [v0.1.3] - 2026-07-02

### Fixed
- **Core position management stabilized (2026-07-02)** — root-caused from live-prod evidence
  (registered symbol stuck on a stale close, zero successful ledger writes):
  - **Topbar 更新報價/重算 wired for real** (`web/shell.js`): both buttons were design-preview
    stubs (success toast, no API call) since v0.1.0 even though `POST /api/actions/refresh-quotes`
    and `/api/actions/recompute` existed and were tested. Now: busy-guarded call, result toast,
    auto-reload. On-demand quote freshness is restored (each market's cron remains the scheduled
    path). Verified by a real browser click against the live test site (~10 s for all 3 markets).
  - **Unregistered symbol is a HARD block at commit** (manual + CSV): `symbol_unresolved` was a
    soft issue that `confirm=True` bypassed, so a trade for a never-registered symbol could enter
    the ledger where it could never be priced (`build_worklist` reads `instruments`) and crashed
    `GET /api/dashboard` with a bare `KeyError` (same bug class as the acked-oversell 500).
    Commit now returns 400 with a register-first message; the existing sev=error frontend gate
    disables the commit button automatically.
  - **Dashboard never 500s over legacy unregistered rows:** their events are excluded from ALL
    computation (book, XIRR, trend, dividend summary — consistently) and surfaced in
    `freshness.unregistered_symbols`; `web/app.js` renders a warning banner with a register link.
    `POST /api/actions/recompute` pre-checks and returns 422 (was a KeyError 500).
  - **Cmd+K search reads the real registry:** the hardcoded 9-symbol design mock in `shell.js` is
    retired; search lazy-loads `GET /api/instruments` (cached, degrades to the register hint).

### Added
- **Instant first quote on registration:** `POST /api/instruments` now fetches the new symbol's
  latest quote (+ reporting FX pairs) immediately via `scheduler.jobs.refresh_instrument_quote`
  — best-effort, never fails the registration — so a newly added stock is priced right away
  instead of waiting for its market's post-close cron. Verified live: registering 2603 returned
  `last=185.50` (real TWSE close) in the same request.

### Changed
- Golden dashboard payload regenerated: `freshness` gains `unregistered_symbols` (all numeric
  values byte-identical; `by_currency` key order in the file is serialization noise only).

## [v0.1.2] - 2026-07-02

### Fixed
- **Data-source connection tests wired (2026-07-02):** the settings → 資料來源 "測試" buttons now
  run a real minimal probe for the primary live sources — `yfinance` (AAPL), `twse` (2330), `tpex`
  (5347 OTC), and `finmind` (a keyed dividend request) — instead of returning the neutral
  `尚未實作連線測試` stub. `fx_ecb` is reclassified `pending` (it has no adapter — the FX path is
  yfinance), so it honestly shows 待測試 rather than the stub. A regression test asserts that no
  `live` source can fall through to the not-implemented fallback. Verified against the live VM (all
  three 官方 sources reachable from the deploy IP; the FinMind key returns data). Real quote fetching
  was never affected — only the diagnostic button was unwired.

### Added
- **App version display + `/api/health` version (2026-07-02):** a single source of truth,
  `portfolio_dash.__version__`, now (a) drives the packaging version via pyproject `dynamic` version,
  (b) is served by the open `GET /api/health` as `{"status":"ok","version":"…"}` — a quick post-deploy
  check (`curl -s …/api/health`), and (c) is displayed in the UI: a version tag under the sidebar
  `portfolio-dash` brand (every page) and the settings → 帳戶與費率 → 一般 (唯讀) row. `web/shell.js`
  fetches `/api/health` once and fills both, so the two displays share the one source.

### Changed
- **mypy strict baseline restored to clean (chore, 2026-07-02):** the type gate had accumulated 65
  pre-existing errors, all in `tests/` (production code was clean). Fixed the real ones — missing
  parameter annotations, `dict`→`dict[str, Any]` generics, a Protocol parameter-name mismatch,
  `Page`/`Writer` argument types, two stale `# type: ignore`s, a `dict.__setitem__` value-context
  hack — and relaxed only `no_implicit_reexport` for test/monkeypatched modules (it adds no value
  there); also dropped the now-unused FinMind/freezegun `ignore_missing_imports`. `mypy --strict` is
  green across all source files again.

## [v0.1.1] - 2026-06-19

### Fixed
- **First-run bootstrap completeness — fresh 0-byte DB (2026-06-19):** the app lifespan now creates EVERY
  table the running app reads AND seeds the broker accounts, so a brand-new install works out of the box.
  `_lifespan` previously omitted `create_pricing_tables` (`prices`/`fx_rates`), `datasources_store.ensure_seeded`
  (`data_sources`/tiers/health), and `seed_accounts` — an empty DB looked fine (no holdings → no price query),
  but the FIRST transaction made `GET /api/dashboard` 500 with `no such table: prices`, and with zero accounts
  no trade could be entered at all (there is no add-account UI in v0.1.0). The bug hid because the whole test
  suite seeds via the harness (`init_golden_base`), never the real boot path. Accounts seed from the single
  canonical `DEFAULT_ACCOUNTS` (idempotent upsert — add a future account there and it auto-seeds next launch;
  when an add/edit-account UI lands, switch to a settings_meta-gated seed-once so launches don't clobber edits).
  New `tests/contract/test_first_run_bootstrap.py` drives `create_app()` through its REAL lifespan against a
  throwaway DB (table creation + account seed + a holding must not 500 the dashboard). All bootstrap steps are
  idempotent (`CREATE TABLE IF NOT EXISTS` / `ON CONFLICT`), safe to re-run on an existing DB.

## [v0.1.0] - 2026-06-19

### Added
- **Frontend wiring foundation — spec 19 Phase 0 (2026-06-16):** the static `web/` frontend's single
  fetch layer + a Playwright smoke harness, landed ahead of per-page wiring.
  - **`web/api.js` (`window.pdApi`, spec 19.1):** the ONE fetch seam — `{get, post, put, del, download,
    abortable}` + `window.PdApiError`. Parses the `api/errors.py` envelope `{error:{code,message,field,
    issues}}` into a structured `PdApiError`; **401** → `window.location.replace('login.html')` (the single
    redirect site) then throws; **402/409/503** → rethrow with NO redirect (the AI block renders a degraded
    state); response Decimal **strings pass through untouched** (no `parseFloat`/`Number`/`+` — the frontend
    never computes money); `credentials:'same-origin'` (carries `pd_session`); `abortable(key)` cancels a
    prior same-key in-flight request. No page calls `fetch` directly.
  - **Playwright smoke harness (`tests/e2e/conftest.py`, reuses the spec-17 golden seed):** a subprocess
    uvicorn serves the real `create_app()` (StaticFiles `web/` + `/api/*`) against an on-disk golden DB
    (DRY-reuse of `tests/conftest.py::_seed_golden`); headless chromium drives it; reusable
    `assert_page_ok(page, base_url, path, root_selector="body")` asserts **zero console errors + zero uncaught
    pageerrors** (catches Decimal-string `.toFixed` TypeErrors once pages bind to `/api`). The global
    `--disable-socket` ban is re-enabled **for loopback only, scoped to `tests/e2e`** (autouse
    `_e2e_loopback_socket`, restored on teardown) — external network stays banned. Baseline smokes for
    `login.html` + `index.html`; per-page smokes are added by Phase 2. (`playwright>=1.44` was already a
    declared dep; raw `playwright.sync_api` used — no `pytest-playwright` added.)
- **Backend completeness — spec 19 Phase 1 (2026-06-16):** ops/observability + dashboard-completeness so
  Phase-2 pages wire against a complete backend.
  - **Ops 保全 (spec 19.3):** new leaf `portfolio_dash/ops/backup.py` — `backup_database` (sqlite3 online
    `.backup` API → gzip → `data/backups/portfolio_{YYYY-MM-DD}.db.gz`, keep-30 rotation), `check_integrity`
    (`PRAGMA integrity_check`), `pre_write_snapshot` (prefixed one-off snapshots for CSV/AI commit + migrations).
    `scheduler/jobs.py` `backup_daily` job (default 01:30 Asia/Taipei): integrity-fail → error run + structured
    warn; logs recovery after a 3-consecutive-fail streak. Pairs with the Phase-0 `make restore` target.
  - **`/api/dashboard` freshness `last_backup_at` (spec 19.3):** `ops.backup.latest_backup_at()` (newest backup
    mtime as a UTC ISO string, or None); `FreshnessReport.last_backup_at`; router-fed after `to_wire`
    (build_dashboard stays pure).
  - **Structured JSON-lines logging (spec 19.4):** new leaf `shared/logging_config.py` (`JsonLinesFormatter`
    + idempotent `configure_logging`, RotatingFileHandler 10 MB×5 → `data/logs/app.log`), configured in the app
    lifespan; a catch-all `Exception` handler in `api/errors.py` logs the traceback + returns the generic 500
    envelope (no detail leak); one `llm_usage` structured log point in `shared/llm.py` (alias/tokens/cost,
    reconciled with the `llm_usage` row). stdlib only.
  - **`calib_gap` alert rule (spec 03/04 I1):** `AlertRules.calib_gap` (default **15 pp**, not a ratio); the
    pure `compute_alerts_from`/`compute_alerts` gain a fed `calib_gap: Decimal | None`; `evaluations_store.
    scored_confidence_hits` + the SINGLE-SOURCE `api/insight_service.calibration_gap(conn)` (global `min_samples`
    gate → `scoring.calibration_error`, in pp) feed BOTH the dashboard embed and `GET /api/alerts` (they cannot
    diverge). `calibration_regression` stays an `alert_events` event, not surfaced here. (`evolution_config.
    gap_alert_pp` is the separate spec-04c regression threshold — NOT this rule's threshold.)
  - **Dashboard embeds latest N real insight cards (spec 08/04 I3):** `insights_store.latest_cards(conn, n)`
    (`is_shadow=0`, newest-first, LIMIT n); the router overwrites `payload["insights"]` after `to_wire` with the
    latest 3 as `{id, title, summary, body_md, symbol, created_at, cost_usd}` (cost_usd stays the canonical
    Decimal string; empty table → `[]`). NOTE the field names differ from the older `web/mock-data.js` insight
    shape — reconciled when Phase 2 wires the dashboard page.
- **spec-17 full-stack regression — financial golden verification + E2E user flows (2026-06-17):**
  the final acceptance pass over the wired full stack.
  - **Multi-stock financial verification (`tests/contract/test_spec17_financials.py`, spec-17 §17.2):**
    a rich 8-instrument / 4-account / 3-currency scenario (`seed_full`) seeded through the REAL write paths
    and driven through `GET /api/dashboard`, asserted against **independent first-principles oracles** derived
    from `rules/domain-ledger.md` (NOT by re-calling the calc core). Covers weighted-average cost (2330), TW
    cash-dividend cost-reduction (2330), partial-sell realized P&L (0056), 配股 stock dividend (2603),
    missing-price degradation + XIRR all-or-nothing (00919), US DRIP $0-cost reinvest with 30% withholding
    (AAPL), age-stale-but-valued price (MSFT), MY cash dividend + 3-dp price fidelity (1155.KL), the
    cross-currency reporting blend at spot, and **invariant #6 — FX gain/loss is an attribution of the
    reporting total, never added on top** (`total_return == realized + unrealized`; realized FX 2,000 TWD
    hand-verified). A frozen `tests/golden/dashboard_full.json` snapshot (regenerated deliberately via
    `scripts/regen_golden_full.py`) pins the whole payload for regression (spec-17 §17.6.1). New reusable
    `tests/conftest.py::dashboard_client_factory` (+ extracted `init_golden_base`) builds a TestClient over a
    fresh, custom-seeded golden-base DB; the fixed subset `golden_db` (and the 1067 tests on it) are untouched.
  - **E2E user flows (`tests/e2e/test_flows_e1_e10.py`, spec-17 §17.5):** Playwright against per-flow ISOLATED
    uvicorn subprocesses (new `tests/e2e/conftest.py::flow_server` factory + `fresh_page` isolated context) so
    write/auth flows are order-independent. E1 dashboard (golden KPIs + 00919 缺價 badge + asof/stale chip), E2
    manual buy commit (form → preview → confirm 201 → position grows 1000→2000 in the API), E4 oversell soft
    warning (ack gates the confirm button, then writable), E6 login loop (protected mode: wrong pass 401 stays
    on /login.html → correct → dashboard). Expect-polling only, no sleeps (§17.7.4). Harness robustness
    added during a senior full-stack review (the suite is green — exit 0 — every run; these prevent rare
    real infra races, NOT a failing assertion): 60s readiness + Playwright ceilings (not 30s) absorb
    Windows subprocess cold-start contention (one genuine TimeoutError seen under review load);
    `flow_server` retries the spawn with a fresh port on early-exit (the `_free_port` bind→release→spawn
    TOCTOU race, amplified by spawning a server per flow); best-effort `fresh_page` / `_terminate`
    teardown so a passed test never errors on Playwright/subprocess cleanup. NOTE: the benign captured
    log `asyncio: Task was destroyed but it is pending!` (Playwright `Page._on_route` GC at close) is
    NOT a failure and only shows under `-rA`/`-rE`, never under the `-q` gate (see LESSONS_LEARNED).

### Fixed
- **Deterministic `/api/dashboard` freshness ordering (spec-17 regression, 2026-06-17):** `freshness.fx` and
  `freshness.missing_fx` iterated `RateResolver.reads`, whose order derives from set iteration over quote
  currencies (PYTHONHASHSEED-dependent across processes) — so the API list order was non-deterministic and a
  golden snapshot flapped between runs. `portfolio/dashboard.py` now sorts both by `(base, quote)`. (`prices`
  was already stable via `sorted(held_symbols)`.)
- **Oversold (賣超) ledger no longer 500s the dashboard (2026-06-18, human sign-off — lightweight, NOT short
  accounting):** an acked oversell (`POST /api/input/manual/commit` `side=sell` qty>held + `ack_oversell=true`)
  writes a sell exceeding holdings; the NEXT `GET /api/dashboard` then crashed (`build_book` raised
  `OversellError`, uncaught → 500). Surfaced by the spec-17 regression. Fix: `build_book(allow_oversell=True)`
  (the dashboard path) DEGRADES GRACEFULLY — nets the position to negative shares, drops its now-undefined cost
  basis, emits no realized row; the holding is flagged `oversold` with 待釐清 (null) value/P&L and is **excluded
  from portfolio aggregates** (auto via the existing `market_value is not None` gates). XIRR degrades to None
  with a reason when any position is oversold. The 重算/rebuild action (`actions.py`) and all input-time
  oversell warnings (preview/whatif detect it independently) are unchanged — `build_book` still raises by
  default. `Holding`/`HoldingRow` gain an `oversold` flag; the holdings table renders a **賣超** badge +
  tooltip prompting the user to record the missing opening inventory / buy. New
  `tests/contract/test_oversell_graceful.py` + an e2e display flow; full short-position accounting is
  deliberately out of scope (it would invert cost basis, dividend direction, weights/allocation/XIRR — over
  scope for a 1–2-user long-only tracker; revisit only if real short trades are needed).
- **`/api/health` exempt from the protected-mode auth gate (2026-06-17, human-approved):** the liveness probe is
  added to `auth_store._OPEN_PATHS` (alongside `/api/auth/login` + `/api/auth/session`). It returns only
  `{"status":"ok"}` (no data), so it must answer regardless of login — previously, once ≥1 user existed (protected
  mode) an unauthenticated Docker/k8s/monitoring liveness probe got a 401. Every OTHER `/api/*` path still requires a
  session in protected mode (regression test pins protected `/api/health`→200 AND `/api/dashboard`→401).
- **Makefile runs the full suite (spec 19 Phase 0, 2026-06-16):** `make test`/`make regress`/`make all` now
  run `pytest tests --ignore=tests/e2e` (the whole tree minus browser e2e) — previously `make test` targeted
  only `tests/unit tests/contract`, collecting **266 of 1012** tests, so `make all` was not real regression.
  `make e2e` is the explicit Playwright gate; the `e2e` pytest marker is registered in `pyproject.toml`.
  Added a guarded `make restore FILE=... [DB=...]` ops target (copies a backup over the live SQLite DB at
  `data/portfolio.db`).
- **Atomic batch import (#1 backend hardening, 2026-06-15):** CSV/broker batch import is now
  all-or-nothing on an unexpected error. `data_ingestion/preview.commit_preview` previously looped
  accepted rows calling writers that each `conn.commit()` per row, so a mid-batch unexpected exception
  left a partial ledger (rows 1..k committed, the rest not) — breaking 重算/append-only reproducibility.
  Now the writer loop runs in ONE transaction (a `commit: bool` param threaded through the four batch
  store inserts; batch passes `commit=False`), commits once at the end, and `rollback()`s + re-raises on
  any exception. The single-row/manual path is unchanged (default `commit=True`); intentional skips of
  hard-issue rows stay contract-level partial success (not a rollback trigger). New
  `tests/data_ingestion/test_preview_atomicity.py`.
- **pricing→data_ingestion cross-peer import removed (#2 layering, 2026-06-15):**
  `pricing/datasources_store.py` no longer imports `data_ingestion.config_seed.DEFAULT_ACCOUNTS`
  (architecture.md: pricing and data_ingestion are sibling lower layers). It now iterates the file's own
  local `_ACCOUNT_MARKET` map (already enumerating the 4 accounts) — byte-equivalent fallback-chain
  seeding. New `tests/pricing/test_layering.py` AST-guards that `pricing/**` imports no `data_ingestion`.

### Changed
- **Renamed `web/AI Pipeline Hub.html` → `web/pipeline-hub.html` (2026-06-19):** the only frontend page
  whose filename had spaces + Title Case, out of step with the lowercase-hyphenated convention
  (`index.html`, `settings-scheduler.html`, …). `git mv` + updated all LIVE references —
  `web/shell.js` (sidebar nav), `web/alerts.js` (`/pipeline` href map), `web/settings-prompts.html`
  (cross-link), and the e2e smoke (`/pipeline-hub.html`, dropping the `%20` URL-encoding). The frozen
  `docs/design-handoff/` export bundle (its own `AI Pipeline Hub.html` + shell.js + spec-07 reference)
  is left untouched — it is a self-consistent historical snapshot, not the served app.
- **spec 19 deferred follow-ups resolved (2026-06-16):** ① the 自我進化設定 panel is wired to `GET/PUT
  /api/evolution-config` (read-then-PUT preserves the non-panel knobs `horizon_basis`/`defer_limit_days`/
  `shadow_on_alert`; `gap_alert_pp` sent as a Decimal string; the `localStorage pd_evolution_cfg` path removed);
  ② removed the dead `window.PD_HISTORY` trend trade-marker code in `charts.js` (the E8 large-trade markers had no
  backend source for the portfolio-level trend after the mock deletion); ③ `rebalance.js` now derives trades/fees via
  the authoritative `POST /api/rebalance/preview` (debounced + `pdApi.abortable`) instead of a client-side estimate —
  the module computes NO money (`FX_TWD`/`pdFeeTax`-call/lot-snapping/turnover removed); ④ `api.js` `download()`'s
  401-redirect now carries the same `!endsWith('login.html')` guard as `_handle`; ⑤ `prompts.py` registry docstring
  26→29; ⑥ added `web/favicon.svg` (+ a `shell.js`-injected `<link>` and a login.html `<link>`) to retire the app-wide
  `/favicon.ico` 404. Each fix shipped with a per-change senior review + page smoke + an E2E Playwright flow
  (evolution-config round-trip, trend-chart mount, rebalance-preview round-trip, favicon presence). Suite now
  **1067 passed / 3 skipped + 33 e2e**.
- **Frontend wired to the live API — spec 19 Phase 2 (page wiring) + Phase 3 (cleanup) (2026-06-16):** every
  static `web/` page now consumes the real `/api/*` through the single `window.pdApi` fetch layer; ALL mock-data
  globals are retired and the mock FILES deleted. No framework, no build step (decision B). Per page (each: mock →
  `pdApi`, money via `fmt.*` [Decimal strings, never client-computed], async boot, Playwright page-smoke):
  - **shell.js** — async `GET /api/auth/session` guard (guest / signed-in / signed-out→`login.html`), replacing the
    localStorage guard; sync globals (`toast`/`confirmDialog`/`pdOpenSymbol`/search/nav) preserved; logout/lock via pdApi.
  - **dashboard** (index/app.js + charts.js + alerts.js) — one shared `window.pdDashboard = pdApi.get('/api/dashboard')`
    promise consumed by all three; sparkline from `spark_30d`; insight cards from the real `{summary,body_md,created_at,
    cost_usd}` shape; alert `href` mapped to static routes; the embedded `alerts`/`llm_quota` rendered (no client recompute).
  - **symbol detail drawer** — `GET /api/symbol/{symbol}/detail` + the shared dashboard promise; feeTax offline mirror
    kept (documented exception); 合計 consumes backend `unrealized_pnl` (no client money-sum).
  - **ledger** — `GET /api/ledgers/*` (implied_rate from the backend; account filter keys on `account_id`).
  - **instruments** — `GET /api/instruments` + probe/register/edit (`POST /probe`, `POST/PUT /instruments`).
  - **input center** — `GET /api/input/context` + manual/CSV/AI preview+commit (oversell + warnings ack-confirm flows);
    manual dividend/FX/opening forms are design-stage (no single-entry endpoint — CSV import is the path).
  - **settings** — LLM (`/api/llm/config`), scheduler (`/api/scheduler/jobs`+`/runs`), datasources (`/api/datasources`),
    prompts + vars (`/api/system-prompt`, `/api/prompt-vars`, `/api/prompts/{preview,test}`), users (`/api/users`),
    alert-rules editor (`GET/PUT /api/alert-rules`). Fixed the C2 bare-`.toFixed` money sites + war-game Finding 8
    (`cost_usd == null` nil-check). Retired the shell `setSession` transitional shim.
  - **alerts.js (I1)** — off-dashboard pages now read `GET /api/alerts` (bell) + `GET /api/llm/config` (quota chip);
    the client-side rule-compute orphan removed.
  - **login.html** — `POST /api/auth/login` (cookie session); api.js's 401-redirect is suppressed ON `login.html` so a
    wrong-password 401 surfaces in the form instead of self-reloading.
  - **insights + AI Pipeline Hub** — `/api/insights`, `/api/ai-score`, `/api/insight-tasks/{status,preflight,diagnose,
    runs}`, `/api/calibrations`; folded the 07 watch-items (`'off'`→`'idle'`, `fix.kind`→one-click buttons,
    `recent_skips` reason labels, calibration version chain).
  - **Phase 3 cleanup** — wired rebalance.js to the shared `/api/dashboard`; DELETED the 4 mock files
    (`mock-data.js`/`history-mock.js`/`input-mock-data.js`/`pipeline-data.js`); added `tests/contract/test_web_pdapi_only.py`
    asserting **no `web/*.js` except `api.js` calls `fetch(` directly** (single-fetch-layer guardrail, spec 19 §6).
  - **Backend fix exposed by the real-server e2e:** `shared/db.py` now opens the SQLite connection with
    `check_same_thread=False` — FastAPI runs the sync `get_conn` dependency in an anyio threadpool, so a per-request
    connection can be created on one worker thread and closed on another (no concurrent use); the default same-thread
    guard wrongly raised on close → a 500 under the real subprocess server (the in-process TestClient never hit it).
    Guarded by a cross-thread regression test.
  - **Test harness:** the Playwright smoke harness (spec 17 seed) now guards every wired page + key interactions
    (drawer, account filter, input preview, instrument probe, rebalance drawer, alert bell, login) — 29 e2e smokes,
    zero console/page errors per page. Suite: 1009 → **1067 passed / 3 skipped**.
  - **Deferred (tracked follow-ups, none ship-blocking for 1–2 users):** wire the 進化設定 panel to `GET/PUT
    /api/evolution-config` (backend already implemented; panel still uses localStorage); the dashboard trend's
    trade-event markers no longer render (`charts.js` `window.PD_HISTORY` is now dead code after the mock deletion —
    remove or source from `/api/dashboard` trend); rebalance.js authoritative result could use `POST /api/rebalance/preview`
    (currently a documented client what-if estimate); `prompts.py` docstring says "26 variables" (registry is 29).
- **Money/Decimal wire-string unification (#2c/M1 foundation hardening, 2026-06-15):** every Decimal
  now serializes to the JSON wire in ONE canonical form — `format(d, "f")` (fixed-point, full source
  precision, trailing zeros preserved, **never scientific notation**) — identical to the DB form
  (`money.to_db`). New `shared/wire.decimal_str`; `to_wire`'s Decimal branch routes through it (was
  `str(Decimal)`, which could emit `1E-7`-style sci-notation); `money.to_db` delegates to it
  (byte-identical, float/non-finite guards kept). All direct `str(<Decimal>)` wire bypasses migrated to
  the canonical encoder across `api/wire.py` + routers (dashboard `spark_30d`/`llm_quota`, input_center
  [**`_money_str`/`normalize()` removed**], symbol, ledgers, llm_settings, instruments, strategy,
  prompts, insights) and `api/insight_service.py`; `str()` on ints/ids/enums left untouched. Done
  **before frontend wiring** so the UI binds to a stable money format and formats for display itself
  (full precision stays on the wire; quantize only at display, per `data-and-pricing.md`). One spec-17
  golden value changed (a trailing zero now preserved: `612500.0`, not `612500`); spec-18 round-trip +
  a no-scientific-notation guard added. (+21 tests; 980 → 1001 passed.)
- **LLM budget model — single topup-cumulative (2026-06-13, human sign-off; senior-review
  finding I-1):** the USD budget is now one number — `budget_remaining = Σ top-ups − Σ usage`
  (`shared/llm_config`). `remaining <= 0` blocks (`check_budget` raises `LLMBudgetExceeded`),
  so an unfunded/$0 account is blocked even when fully configured; exhaustion coincides exactly
  with `Σ top-ups == Σ usage`. Top-ups ADD cumulatively (no reset). The gate, settings page
  (`GET /api/llm/config` `quota.remaining_usd`), dashboard chip (`GET /api/dashboard`
  `llm_quota.remaining_usd`), and the spec-16 `quota_remaining` alias all read this single value
  (`reset_budget` removed; `quota_remaining` delegates to `budget_remaining`). **Supersedes the
  earlier append-only "reset ledger" model** (remaining = latest reset − Σ usage since that reset;
  unset = no cap). End-to-end reconciliation proof: `tests/contract/test_quota_accounting.py`.
- **Web-layer architecture decision — option (B) (2026-06-13, human sign-off):** the web
  layer is now a **FastAPI JSON API (`portfolio_dash/api/*`) + a static vanilla-JS frontend
  (`web/`)**, superseding the originally-locked **Jinja2 + HTMX server-rendering** (CLAUDE.md
  locked decision #1 "no frontend/backend split / no JSON contract"; `stack.md`;
  `design-handoff.md` "convert to Jinja2 templates"). Rationale: the Claude-Design export is
  vanilla JS + ECharts CDN with **no framework and no build step** (the stack-drift guardrail
  is honored) and pushes **all computation to the backend** (the web layer still does not
  compute — invariant #4 intent preserved). The trade-off ("single codebase / no contract to
  drift") is mitigated by `mock-data.js` as the version-controlled contract and spec-17 golden
  payload + spec-18.4 string-serialization round-trip tests. Net upside: the JSON contract makes
  the automated regression loop machine-diffable (stronger than HTML-fragment assertions).
  `CLAUDE.md`/`stack.md` web rows to be amended; the HANDOFF.md CLAUDE.md template is
  **reconciled, not applied verbatim** (locked accounting/ledger/process rules preserved).
  Full reconciliation: `docs/design/spec-reconciliation-2026-06-13.md`.
- **Scope expansions adopted from design-handoff specs 01–19 (2026-06-13, human sign-off):**
  a new `api/` HTTP layer (08/19); `strategy/` alerts rule-engine + what-if + rebalance as
  pure functions (03, with config-row editable thresholds — a narrow, bounded step toward
  user-editable rules, explicitly NOT a DSL); a full `llm_insight/` self-evolution system —
  insight composers, calibration version chains, backtest scoring, a new `master` LLM role
  (04, far beyond the prior "batch insight cards"; invariant #1 preserved — quant hits are
  code, the LLM only writes narrative/calibration text); external-data ingest + an append-only
  `external_snapshots` store (06: FinMind chips/fundamentals/valuation, VIX, Fear&Greed,
  indices); auth/users via stdlib `hashlib.scrypt` (09, no new dependency); a full test/
  regression harness — `make all`, golden dataset, FastAPI TestClient contract tests,
  Playwright E2E, hypothesis/mutmut, pytest-socket network ban (17/18); SQLite backup/restore
  + structured logging (19). Schema migrations (additive, via `_add_column_if_missing` /
  `config_store`): `instruments += target_low/board_status/is_etf`, `transactions +=
  fee_snapshot`, `schedule_config += kind/payload`, `job_runs += payload/reason/cost_usd`, plus
  new tables for auth/datasources/external-snapshots/insight. Enum extensions: `DividendType +=
  NET`, `LLMRole += MASTER/MASTER_FALLBACK`. `FeeRuleSet` structural fixes (flat_fee, US/MY
  min_fee, stamp_duty_rate+cap) and US/MY fee-rate backfill (spec 18.0, pending real-statement
  confirmation). Build order in the reconciliation doc §6.
- **Accounting model decision (2026-06-06, human sign-off):** P&L now uses the
  adjusted-cost model — cash dividends fold into cost (no separate dividend-income line),
  realized/unrealized computed vs `adjusted_cost`; `original_cost` retained for the
  return-rate denominator and the capital-gain-vs-dividend split. Supersedes the prior
  original-cost-plus-separate-dividend rule in `domain-ledger.md`. The no-double-count
  principle is preserved (dividends still counted exactly once). Return-rate denominator
  stays original invested cost; cost basis is all-in (incl. buy fees+tax).

### Added
- **Insight pipeline-hub UX — status / preflight / diagnose (spec 07, Phase 4 — the observability
  layer):** read-only convergence over the spec-04 machinery — NO new tables, NO LLM calls, NO new
  business logic (03/04/06 reused). `GET /api/insight-tasks/status` returns a single source of truth:
  health (master_ok, quota_remaining, last_batch) + per-task 5-node states (trigger/input/assemble/exec/
  output, §7.1.1 derivation) aggregated to a level — the pure `llm_insight/pipeline_status.py`
  `derive_node_states` over facts gathered in `api/insight_service.py` (schedule_config, resolved
  universe, **reused dashboard freshness**, templates, budget/quota_low/master, last non-shadow run).
  `POST /api/insight-tasks/{id}/preflight` (also a draft `body` for the wizard's check-before-create) is
  a zero-cost dry run that calls the **SAME `gating.evaluate_gates` as execution** (the §7.2 hard rule —
  no "preflight passed, run failed"; asserted via a spy + an end-to-end demo) wrapped with G0/G1/G7,
  returns ordered gates + verdict (blocked/degraded/clean) + the spec-06 assembled preview + `fix.kind`
  one-click hints — never calls the LLM, never writes job_runs/llm_usage. `GET …/diagnose` adds
  first_blocker + recent_skips (single-enum reasons); `GET …/runs` is the task-view job_runs (is_shadow
  excluded). §7.0 naming: `/api/insight-tasks/*` is a full **alias of the same resource** as
  `/api/insight-types/*` (one `_dual` route registration, no logic duplication; old routes + table names
  kept). Senior review: APPROVE-WITH-NITS → fixed the R6 (quota) gate emitting a wrong `create_schedule`
  fix (quota has no one-click action in the enum). The 3 §7.6 failure demos reproduced. **This completes
  the 04→07 insight chain backend.** (+48 tests; 932 → 980 passed.)
- **AI self-evolution / Loop Engineering (spec 04, Phase 4 — the four-self loop):** the
  insight-composer + generation + backtest + calibration + shadow-promote system, built in three
  sub-phases (04a design/CRUD, 04b generation, 04c evolution) under the §4.10 locked decisions
  (mechanism reviewed + human-signed-off 2026-06-14). **04a** — composer tables
  (`strategy_prompts`/`insight_types`/`insight_type_strategies`/`calibration_prompts`) +
  `evolution_config`, CRUD/cascade (4.1)/schedule-binding (4.2 kind=insight)/active-calibration/
  evolution-config API, R1 create-time gate reusing `validate_tokens`. **04b (Loop 1 自運作)** —
  `InsightCard`+`Prediction` schema (confidence required with a prediction), `insights` table with
  fingerprint cache + trading-day `due_at`, layer assembly (system+strategies+active calibration via
  06a `render_prompt`), the single **R1–R8 runtime gate** (shared with spec-07 preflight),
  `run_insight_type` generation (default role, R4 zero-LLM anomaly card, R6 partial, cache hits),
  scheduler `kind=insight` dynamic dispatch via an injected `register_insight_runner` (no scheduler→api
  cycle), date variables (`now`/`card_created_at`/`eval_date`, ISO-8601 +08:00),
  `complete_structured` `response_format` enforcement w/ graceful fallback, and the `alert-scan` job +
  `alert_events` + on_alert (R7) trigger (24h debounce, ≤3-day horizon). **04c (Loops 2–4)** — the
  **master LLM role** completion path, `insight_evaluations` store + `/api/ai-score` aggregation,
  pure `score_quant`/calibration-binning/`decide_promotion`, the daily `evaluate_insights` job
  (objective quant_hit + master narrative_score, **pending_data anti-poison** → `undetermined` after
  `defer_limit_days`), the weekly `generate_calibrations` job (master writes a validated new version,
  `min_samples`-gated, append-only), shadow evaluation + auto-promote + `calibration_regression`
  alert, and the §4.8 calibration validator (keyword denylist + one master review). **Layering held:**
  `llm_insight/*` import no `pricing`/`data_ingestion`/`api` (the only price-reading seam is
  `api/insight_service.py`; the wire encoder moved to `shared/wire.py` to kill a pre-existing
  `llm_insight→api` import). LLM emits no numbers of record (quant_hit is code; master writes only
  narrative/calibration text); single budget governs all roles. Cross-module senior review:
  APPROVE-WITH-NITS → fixed insights.model provenance, the reverse import, shadow `job_runs`
  distinction (`is_shadow` column, excluded from user-facing runs), and single-enum skip reasons.
  Deferred v1 watch-items: `relative`/`volatility`/`portfolio_return` quant metrics (narrative-only for
  now, anti-poison-safe). New tables: `insights`, `insight_evaluations`, `alert_events`,
  `alert_dispatch_log` + the four composer tables; `job_runs += is_shadow`; `insight_types +=
  horizon_days/eval_prompt`. (+265 tests; 667 → 932 passed.)
- **Data-source catalog, provider expansion & external-snapshot ingest (spec 20, Phase 4 —
  absorbs the planned 06b):** the data layer that makes the chips/sentiment prompt variables
  live. **Two seams** (control plane = spec 14 settings/keys/health/fallback; data plane =
  spec 20): the existing `pricing/` registry + providers stays the single interface — adding a
  source = one adapter + one catalog row + one probe adapter. New `pricing/snapshots_store.py`
  (append-only `external_snapshots`: source/dataset/symbol/as_of/payload/fetched_at, latest
  `fetched_at` wins; created EMPTY in `golden_db` so every external var degrades and prior
  suites stay green); `pricing/finmind_datasets.py` (FinMind Free-tier client for
  institutional/margin/PER/monthly-revenue/financials, **always per-`data_id` → Free tier**);
  `pricing/sentiment_source.py` (VIX via yfinance `^VIX` + CNN Fear&Greed free JSON) +
  `index_source.py` (yfinance `^TWII`/`^GSPC`/`^KLSE`); 4 free quote fallbacks
  (`twstock`/`stockprices_dev`/`klsescreener`/`malaysiastock`) wired into
  `DEFAULT_PROVIDER_ORDER`; `portfolio/external_signals.py` (pure Decimal derivations —
  consecutive-buy-days, net-buy-sum, chg/yoy/mom with None on denom≤0, percentile, vix_zone —
  numbers of record stay out of `llm_insight`); `pricing/ingest.py` + 5 scheduler ingest jobs
  (TW universe via direct SQL — `scheduler` imports no `data_ingestion`; 3-consecutive-fail
  warn → `data_source_health`). Catalog (`datasources_store.SOURCE_INFO`) expanded to the full
  ~15-source matrix with `provides`/`status` (`live`/`pending`/`blocked`); token-gated adapters
  (alphavantage/finnhub/fred) catalogued `pending` + key-gated `supports` (inert until a key is
  entered — not in the fallback order); the 7 chips/sentiment variables flipped `available=true`,
  served from snapshots via `VarContext` (router-fed; `llm_insight` imports neither `pricing`
  nor `data_ingestion`), degrading to `{"unavailable": true}` when a snapshot is missing.
- **FinMind auth & tier-awareness (spec 20.15, per the official AI-agent manual):** both
  FinMind callers switched to `Authorization: Bearer {token}` (token still DB-resolved), added
  optional `end_date`. Per-source token tier marking — `data_sources.tier` (additive idempotent
  migration), `SourceInfo.tiers`, `TIER_ORDER`, `PUT /api/datasources/{id}/tier` (400 unknown
  tier / `auth:"none"`; 404 unknown id), `tier`/`tiers` on the GET wire. `DATASET_TIER` (all 5
  = `free`) + a **local tier preflight** that raises `FinMindTierError` BEFORE any network call
  when the marked token tier is too low; HTTP 402 / JSON `status==402` → `FinMindQuotaError`
  carrying FinMind's message; `fetch_quota` reads `user_info` (`user_count`/`api_request_limit`).
  `GET /api/prompt-vars` now carries `required_tier`/`tier_ok`/`tier_label` so the frontend greys
  out variables/panels needing a higher plan; ingest catching tier/quota errors writes no
  snapshot and records `data_source_health` (status=error, reason) → the variable degrade payload
  carries the `reason` (router-fed). Non-regression: under a free/unset token the 5 chips vars
  stay `tier_ok=true`. Probe harness extended (Bearer, `fetch_quota`, tier-from-limit) +
  bounded `docs/probes/` refresh; full source matrix authored in
  `docs/design-handoff/.../specs/20-data-source-catalog.md`.
- **Data-variable & prompt-rendering foundation (spec 06a, Phase 4 — the AI brain's base):**
  the prompt "Lego-block" layer that specs 04/07 build on. New module `portfolio_dash/llm_insight/`
  (`variables.py` = a **26-variable / 8-category registry** mirroring `web/vars.js` + `render_prompt`
  + `validate_tokens` — the SINGLE reusable validation core that spec 04 §4.9 R1 runtime gating and
  spec 07 §7.2 preflight will also call; `system_prompt.py` = one editable global system prompt via
  `config_store`, default seeded). New `portfolio_dash/portfolio/technicals.py` (pure Decimal: MA
  20/60/120 + deviation, sample-stdev annualized volatility via `Decimal.sqrt`, max drawdown,
  price-vs-cost) — the **LLM emits no numbers of record**, so every numeric variable value is
  computed by the calc core and only ASSEMBLED into JSON here. Endpoints (`api/routers/prompts.py`):
  `GET /api/prompt-vars`, `GET/PUT /api/system-prompt`, `POST /api/prompts/preview` (diagnostic —
  ALWAYS 200, lists `unknown_tokens`/`scope_violations`, REAL computed values, **never calls the
  LLM**, `est_tokens` heuristic), `POST /api/prompts/test` (execution path — **422** on unknown
  token or a `per_symbol` var in a `portfolio`-scope body = R1; else real LiteLLM via a new
  `shared/llm.complete_text`, records `llm_usage` agent=`prompt_test`, budget exhausted → 402,
  returns `quota_remaining`). Money/price/rate are Decimal **strings**.
  - **Availability:** position+price+dividend+fx+system (17 vars) are live now; chips+sentiment
    (7) are `available=false` until spec 06b external ingest; backtest/calibration (2) until spec 04
    (`web/vars.js` mislabels the `ai` category `ready` — corrected to `false`). Unavailable vars
    render `{"unavailable": true}`. (Reconciliation: the spec prose says "24" variables; its own
    table and `web/vars.js` enumerate **26** — the authoritative count.)
  - **Senior-review (Opus, APPROVE-WITH-NITS) fixes folded in before merge:** `fx_rates_json` now
    emits the real spot rate (was as_of/stale only — `freshness.fx` carries no rate; the router
    resolves it via `get_fx`); `dividends_json` is the per-event ledger list with currency (was a
    yearly summary, contradicting its contract); `price_vs_cost` returns each ratio independently so
    a non-positive `adjusted_avg` (high-yield payback, allowed by `domain-ledger.md`) no longer
    drops the valid original ratio; `to_wire` now transforms Mapping keys (defensive); +coverage
    (all available tokens render valid JSON, fx rate present, per-event dividends). Conn-bearing
    reads (FX rates, dividend rows) are resolved in the api router and fed into `VarContext` —
    `llm_insight` imports only `portfolio`/`shared`/`api.serialize` (one-way deps intact).
  - **Deferred to spec 06b** (intentional split): `external_snapshots` table + 5 ingest jobs
    (FinMind chips/fundamentals/valuation, VIX/Fear&Greed, indices) + derivations + flipping
    chips/sentiment vars to available. **Global system-prompt CRUD lands here** (neither spec 06
    nor 04 assigned the endpoint; it is foundational to rendering).
- **Scheduler management API (spec 15, Phase 3):** `portfolio_dash/api/routers/scheduler.py` over the
  existing in-process scheduler. `GET /api/scheduler/jobs` (config + latest run + next fire),
  `PUT /api/scheduler/jobs/{id}` (cron/tz/enabled subset-merge with live reschedule), `POST
  /api/scheduler/jobs/{id}/run` (async **202** + a daemon thread that opens its own `session()`;
  `409 already_running` when the latest run is unfinished), `GET /api/scheduler/runs` (history;
  `limit>500 → 400`). Cron/tz validated via `CronTrigger.from_crontab` — invalid → **400
  `invalid_cron`** with the `field` pointing at the real offender (tz checked separately from cron),
  and **no DB write**. Every route degrades gracefully when `app.state.scheduler` is `None`
  (`PD_DISABLE_SCHEDULER=1`, e.g. tests): `next` = null, reschedule is a no-op. `cost_usd`/`reason`
  are Decimal-string/null, never stringified. New `scheduler/runtime.py::reschedule_job` (None-safe)
  + `scheduler/jobs.py` helpers (`start_job_run`/`finish_job_run`/`latest_run_unfinished`/
  `run_job_func`). **§15.0 schema columns (SR 2026-06-13; specs 04/07 depend on these):**
  `schedule_config += kind ('system'|'insight'), payload`; `job_runs += payload, reason, cost_usd`,
  added idempotently in `create_scheduler_tables` via a **local** `_add_column_if_missing` (no
  `scheduler → data_ingestion` dependency). v1 lists `kind='system'` jobs only (no insight jobs yet).
- **Sessions & authorized users (spec 09, Phase 3):** stdlib-only auth (`hashlib.scrypt` +
  `secrets`; no new dependency). `portfolio_dash/api/auth_store.py` (table DDL, scrypt
  hash/verify with `hmac.compare_digest`, user/session CRUD, mode check) + routers `auth.py`
  (`POST /api/auth/login` sets an `HttpOnly; SameSite=Lax; Path=/` `pd_session` cookie; `GET
  /api/auth/session`; `POST /api/auth/logout`/`lock` → 204) and `users.py` (`GET/POST/DELETE
  /api/users`; 201 create / 409 `duplicate_username` / 400 short-or-empty). **Guest vs protected
  mode:** `auth_users` empty → everything open; ≥1 user → a global `require_session` dependency
  (wired into `create_app`, sharing `Depends(get_conn)` so it is test-overridable — NOT middleware)
  gates all `/api/*` except `login`/`session` → 401 without a valid, unlocked cookie. `golden_db`
  seeds no user (guest), so the entire pre-existing suite stays green. Stores only salted scrypt
  hashes; `password_hash` is never returned or logged; bad-username and bad-password are
  indistinguishable in status, body, **and timing** (a dummy scrypt verify equalizes the
  missing-user path — no username enumeration).
  - **`GET /api/auth/session` shape (additive to the spec's two literal examples):** not protected
    → `{"mode":"guest"}`; protected + valid/known cookie → `{"mode":"user", username, name, locked}`
    (a locked-but-known session reports `locked:true`); protected + absent/unknown cookie →
    `{"mode":"user", username:null, name:null, locked:false}` so the shell shows the login screen.
  - **Senior-review fixes folded in before merge:** equalized login timing (closes the
    username-enumeration side-channel); `PUT /scheduler/jobs` 400 `field` attribution (valid tz +
    bad cron now blames `cron`); `require_session` treats a missing `auth_users` table as guest
    (defensive, no 500 before lifespan); non-empty `username` validation; +coverage (authenticated
    request through the gate, `/api/users` gated when protected, valid-tz/bad-cron field).
    **Deferred (low risk for the 1–2-user localhost threat model, filed as follow-ups):** `/run`
    check-then-insert TOCTOU; cookie `Secure` flag (HTTPS only); `run_job_func` outer-except
    logging; last-user deletion silently reverting to guest mode.
- **Dividend projection in dashboard payload (spec 05, Phase 2):** `DashboardData.dividend_projection`
  — annual declared-dividend cash flow `{year, by_currency: {<ccy>: {declared_gross, declared_net,
  events}}, basis: "declared_only"}`, computed by the pure `portfolio/dividends.py::project_dividends`
  over the ex-dividend calendar + valued holdings. Net applies each holding account's dividend model
  via `apply_dividend_model` (drip_us → 30% US withholding; cash/cash_cost_reduction → net=gross).
  **Per-currency, never summed across currencies.** v1 is `declared_only` (events with `ex_date.year ==
  current year`); v2 `declared_plus_estimated` deferred. **Reconciliation:** the Moomoo-US per-dividend
  platform fee mentioned in the spec is NOT encoded (no per-dividend fee config; probe-pending) — v1 net
  applies withholding only.
  - **Account model: `dividend_model` is now a first-class field** (`shared/models/assets.py` +
    `list_accounts` SELECT). `project_dividends` reads it from the DB-sourced `accounts` param (single
    source of truth; fail-loud KeyError on an unknown account_id), resolving the prior split where the
    projection read config-as-code while `accounts.py` read the DB (senior-review finding).
- **strategy/ module: alerts, what-if, rebalance (spec 03, Phase 2):** a new
  `portfolio_dash/strategy/` consumer layer (pure functions over computed outputs; writes
  no ledger) + five endpoints. **Alert engine** — `compute_alerts_from(data, rules, *,
  quota_remaining, quota_threshold)` is the single source for both the dashboard payload's
  embedded `alerts` and `GET /api/alerts` (the dashboard path reuses its already-built
  `DashboardData`, no second build); six v1 rules (single_weight, sector_weight, stale_price,
  missing_price, fx_drift, exdiv_upcoming, quota_low — `quota_low` escalates warn→risk at
  remaining 0). `GET/PUT /api/alert-rules` — editable thresholds in a single-row JSON config
  (`alert_rules_config`), Decimal-as-string, bounds-validated (out-of-bounds → 400). **what-if**
  `POST /api/whatif` — buy/sell trade sim reusing the real `compute_fees` (compute, no write);
  `account_id` defaults to the most-shares account and is echoed; `oversell=true` still returns
  full numbers. **rebalance** `POST /api/rebalance/preview` — target-weight trades with integer
  shares (MY market rounds to 100-unit board lots), per-row fee/tax + `new_weight`, and a summary
  (turnover/fees in reporting ccy, cash_after, excluded). Missing-price symbols are excluded and
  missing FX leaves `new_weight` null — never fabricated.
  - **Reconciliations (recorded):** (R1) `calib_gap` / `calibration_regression` rules DEFERRED
    to spec 04 (their AI-calibration data source does not exist yet) — absent, not stubbed with
    fake data; (R2) `quota_low` threshold is sourced from spec-16's `llm_config.get_alert_threshold`
    (single source of truth), NOT stored in alert-rules; (R3) alerts single-sourced via
    `compute_alerts_from`; (R4) rebalance v1 acts only on symbols present in `targets` (held
    symbols absent from `targets` are left untouched).
- **Fixed — quota alert threshold default (spec-03 §3.1 SR):** `llm_config._DEFAULT_THRESHOLD`
  changed `0 → 1.00` so `quota_low` fires when remaining < 1.00 until the user sets their own
  threshold, matching the SR ("預設值 1.00"). Spec 16's contract is unaffected (it asserts the
  key's presence, not the default value).
- **Export endpoints (spec 02, Phase 2):** a new consumer-layer module `portfolio_dash/export/`
  + `POST /api/export/{holdings,ledgers,llm-usage,job-runs,tax-package}`. All output is
  reconciliation-grade: **raw `Decimal` strings** (no rounding/thousands separators), **UTF-8
  with BOM**, **CRLF**, `Content-Disposition: attachment`. holdings → 21-column snapshot CSV
  (incl. `reporting_ccy_value` via the promoted public `RateResolver`; blank on missing FX,
  never fabricated) + `# as_of/fx_rates/generated` footer; ledgers → zip of the four raw ledger
  CSVs + `fee_rules_snapshot.json` (Decimals as strings via `to_wire`) + `manifest.json`
  (counts/as_of/schema_version); llm-usage/job-runs → range-filtered raw CSV (`from>to` → 400
  `validation_error`); tax-package → annual zip (`realized_gains`/`dividends`/`fx_realized`/
  `summary.md`), **year-cut by trade date**, **per-currency never summed**, realized converted
  at **trade-date FX** with the rate recorded (blank when no stored rate). Each endpoint writes
  one `job_runs` audit row.
  - **Calc-core enrichment:** `RealizedRow.sell_date` (the sell transaction's trade date), so
    realized gains can be cut by tax year. Domain-model enrichment only — no accounting-semantics
    change.
  - **DRY:** `forex.fx_pnl.realized_fx_rows` is the single source of the realized-FX formula;
    `_realized_fx` now sums over it.
  - **Reconciliation — audit `kind`:** spec 02 §3 says the audit row carries `kind=export`, but
    `job_runs` has no `kind` column and spec 15.0 places `kind` on `schedule_config` (not
    `job_runs`). Implemented instead as a namespaced `job_id=export:<type>` via
    `scheduler.jobs.log_export_run`.
  - **Reconciliation — module map:** `portfolio_dash/export/` added as a consumer layer
    (`web_ui → export → {portfolio, forex, pricing, data_ingestion, scheduler, shared}`; nothing
    lower imports it; the router stays thin and computes no numbers of record).
- **Review fixes I-2 / I-3 (2026-06-13):** a single shared secret-masking helper
  `shared/masking.py::mask_secret` (`prefix•••suffix`, with a short-key guard that fully masks
  keys too short to safely reveal a prefix/suffix) — now the one masker for `api_key_masked` and
  data-source key views (I-2); and `default_registry(conn)` wiring the FinMind token from the
  `data_sources` DB into the provider chain (env/ctor fallback retained) so the configured key is
  actually used at runtime (I-3).
- **Instruments API (spec 10, Phase 1):** `GET /api/instruments` (list + held flag + latest
  price + `chg_pct` + target_low; TW board serialized `null` until confirmed),
  `POST /api/instruments/probe` (TW board probe via `probe_tw_board`),
  `POST/PUT /api/instruments` (register/update through `register_instrument`, with
  `duplicate_symbol` 409 / `validation_error` 400 / `not_found` 404 envelopes). Schema/model:
  `instruments += target_low/board_status/is_etf` (idempotent migration); `target_low`/`is_etf`
  on the `Instrument` model, `board_status` a registration-only column set by
  `register_instrument`; `is_etf` is the single source of truth for ETF (no `sector=="ETF"`).
- **Ledgers read API (spec 11, Phase 1):** `GET /api/ledgers/{transactions,dividends,fx,openings}`
  read-only over the four append-only ledgers — account-name join, account/symbol/date-range
  filters, desc pagination (`limit`/`offset`/`total_count`), the buy/sell `total` sign convention,
  `implied_rate`, and the **lowercase wire format** for `side`/`type` (Currency stays uppercase).
  Reuses the existing `transactions.fee_rule_snapshot` column (mapped to API `fee_snapshot`) — no
  new column; `openings` gets a synthetic display id (its PK is account_id+symbol). No write routes.
- **Input center — context + manual entry (spec 12a, Phase 1):** `GET /api/input/context`
  (accounts + mapped `div_model`, fee-rule serialization with label, instruments + `etf`,
  current holdings) and `POST /api/input/manual/{preview,commit}` over `enter_transaction`.
  New `api/wire.py` shared mappers: lowercase `side` in/out (`parse_side`), `Issue` →
  `{sev,code,text,field}` (`issue_wire`), `fee_rules_wire` (reused by spec 13), `div_model`
  mapping (`cash_cost_reduction→tw`/`drip_us→drip`/`cash→net`). Commit is **ack-gated**: hard
  issues → 400, unacked oversell → 422 `oversell_unacknowledged`, else append. (Known follow-up:
  unify API money-string formatting — `_money_str` trims trailing zeros in manual preview/commit
  while `to_wire`/ledgers use raw `str`; cosmetic, deferred to the frontend-wiring phase.)
- **Input center — CSV import + AI input (spec 12b, Phase 1):** `POST /api/import/{preview,commit}`
  (4 ledger kinds; preview → `{rows:[{n,status,reason,data}],summary}`; **commit re-derives from
  `csv_text`** and re-validates vs the current ledger, ack-gating warn rows → 422
  `warnings_unacknowledged`) and `POST /api/input/ai/preview` (LLM text → preview + `meta` +
  `csv_text`; degradation mapped `budget_exceeded`→402 / `ai_not_activated`→409 /
  `llm_unavailable`→503). `ai_agents_input` now returns `AiInputResult{preview, meta, csv_text}`
  (meta from the `llm_usage` row; `completer` default resolved at call time). Also fixed
  `build_transaction_preview` to catch `decimal.InvalidOperation` (a malformed number now yields a
  `parse_error` row instead of crashing — matching its siblings + docstring). Senior review added a
  soft `fuzzy_resolved` (ack-gated) issue so a fuzzy symbol match surfaces + writes the resolved
  symbol (no silent phantom-symbol writes), in both `txn_preview_row` and `enter_transaction`.
- **Top-bar actions (spec 08 §8.2–8.3, Phase 1 close-out):** `POST /api/actions/refresh-quotes`
  (triggers the per-market `quotes_*` jobs synchronously, returns their `job_runs` ids; unknown
  market → 400) and `POST /api/actions/recompute` (re-runs `build_book` over the ledgers to validate
  consistency, `OversellError` → 422; append-only, writes nothing). `run_job` now returns its run id.
  (Sync 200 instead of the spec's 202-background — the `GET /api/scheduler/runs` poll endpoint is
  spec 15, not yet built; `run_job` swallows provider errors so a failed fetch is a logged run, not a
  500. Revisit when spec 15 lands.) **Phase-1 core data flow (specs 08 / 10 / 11 / 12) backend complete.**
- **Settings batch — accounts/fees + datasources + LLM settings (specs 13 / 14 / 16, Phase 2; built as
  3 parallel worktree-isolated sub-projects):**
  - **spec 13:** `GET /api/accounts` (read-only) — accounts + dividend model + fee-rule serialization
    (reusing `api/wire.py`); `version.seeded_at` is `null` (accounts aren't recorded in `settings_meta`).
  - **spec 14:** data-source management — new `pricing/datasources_store.py` (config_store tables
    `data_sources` / `data_source_health` / `data_source_fallbacks`); `GET /api/datasources`,
    `PUT …/{id}/key`, `POST …/{id}/test`, `PUT …/fallbacks`; API keys write-only (masked
    `prefix•••suffix`); `FinMindProvider` reads its token from the DB via an injected getter
    (env/ctor fallback retained).
  - **spec 16:** `GET /api/llm/config` + model CRUD (`POST/PUT/DELETE /api/llm/models/{alias}`,
    api_key write-only, `model_in_use` 422) + `PUT /api/llm/roles` + quota topup/threshold + model
    connection-test; `LLMRole += MASTER / MASTER_FALLBACK` (spec 04 overlay); usage aggregation reads
    (`shared/llm_usage_reads.py`: by-model / by-agent / 30-day daily series).
  - Routers mounted in `api/app.py`; `golden_db` seeds the data_sources tables.
- `shared/` foundation layer: `Currency`/`Market` enums; `Decimal` money primitives
  (canonical TEXT persistence via `to_db`/`from_db`, per-currency `quantize_amount`
  with ROUND_HALF_UP, float + non-finite guards); single pure `fx.convert` helper
  (rejects non-positive / non-finite rates); env-driven `Settings` + cached
  `get_settings`; stdlib `sqlite3` `get_connection`/`session` (WAL, foreign keys on).
- Package + tooling bootstrap: `pyproject.toml` (pydantic, pydantic-settings; dev:
  mypy strict, ruff, pytest, pytest-asyncio; strict `asyncio_mode`); `portfolio_dash/`
  package with `py.typed`; `tests/` layout.
- `portfolio/` calculation core: chronological ledger replay (`build_book`) →
  holdings + realized P&L; `value_holdings` (unrealized vs adjusted, capital-gain vs
  original, stale-price flagging); `total_return` (per-currency + reporting blended);
  reporting-currency `xirr_reporting` (pyxirr); `sector_allocation`; `combined_view`.
- `shared/models/`: canonical domain models (`Account`, `Instrument`, `Transaction`,
  `Dividend`, `FXConversion`, `OpeningInventory`) + `Money` finite-Decimal type.
- Dependency: `pyxirr` (irregular-cashflow XIRR).
- `forex/` FX (換匯) P&L: per-account foreign-currency pool (weighted-avg acquisition
  rate from home→foreign conversions), reconstructed foreign cash balance, realized FX on
  reconversions, unrealized FX (stocks + cash) marked to spot; reporting-currency
  `FXSummary` rollup. Presented as an attribution decomposition of the portfolio return
  (asset + FX), never additive.
- Data-source availability probe (spike) under `scripts/probe/`: typed harness
  (`ProbeResult` model, `run_probe` runner + fixture recorder, markdown report renderer)
  + live adapters (yfinance, TWSE, TPEx, twstock, stockprices.dev, klsescreener; FinMind /
  AlphaVantage / Finnhub keyed). Produced a ranked primary/fallback recommendation per
  (data type × market) and recorded raw fixtures under `tests/pricing/fixtures/` for
  `pricing/` mock tests. Results + `pricing/` architecture recommendation:
  `docs/probes/2026-06-08-data-source-probe-results.md`. Key findings: yfinance is the
  US/MY/FX workhorse primary; TW latest quotes from TWSE/TPEx string sources for true tick
  precision; MY 3-dp verified via klsescreener (yfinance is float64 — convert via
  `Decimal(str(...))`); TW board (上市/上櫃) must be resolved per instrument; keyed sources
  (FinMind/AlphaVantage/Finnhub) and Schwab await keys/OAuth.
- FinMind **validated** (2026-06-08, trial token, 600/hr): 6 datasets confirmed (price,
  dividend/除權息, FX, financial statements, institutional, margin) with fixtures under
  `tests/pricing/fixtures/finmind/`. Added capability research notes under `docs/research/`
  for **Schwab Trader API** (enables US account/transaction auto-import for `data_ingestion/`)
  and **FinMind** — both feeding `pricing/` source selection, `llm_insight/` fundamentals, and
  the LLM self-backtest loop.
- `pricing/` market-data layer (A+B+C): config-driven, capability-aware provider chain
  (yfinance / TWSE / TPEx / FinMind-keyed) writing idempotent SQLite rows
  (`prices`/`fx_rates`/`dividend_events`) — the only writer of those tables. (A) latest quotes +
  FX, (B) historical daily backfill, (C) dividend/ex-dividend **reference** data (FinMind 除權息
  + yfinance fallback). Graceful degradation (last-known + staleness; never raises/fabricates),
  per-row source provenance, `Decimal(str())` precision, per-instrument TW board resolution.
  Read API (`get_latest_price`/`get_fx`/`get_price_history`/`get_dividend_events`) + orchestrators
  (`refresh_quotes`/`refresh_history`/`refresh_dividends`). Providers tested against the probe's
  recorded fixtures (no live network). Dividend events are reference-only — never the ledger,
  never in P&L. Plan: `docs/superpowers/plans/2026-06-08-pricing-market-data-layer.md`.
- `data_ingestion/` ledger input (the only ledger writer): SQLite schema for the four
  source-of-truth ledgers (`transactions`/`dividends`/`fx_conversions`/`opening_inventory`) +
  `instruments` registry + `accounts`/fee-rule/LLM-model config seed. Per-account **fee/tax
  engine** (config rules + per-row snapshot; TW 0.1425% / 0.3% / 0.1% / 0.15%, min NT$20, integer
  rounding; US/MY structures). Three input modes through one resolve→fee/tax→validate→
  **preview→confirm** pipeline: **manual**, **CSV import**, and **AI Agents Input** (natural
  language → LLM structured draft → confirm; the LLM never writes directly). Symbol resolution
  fuzzy → LLM-fallback → confirm; sell>holdings blocks until confirmed; per-account dividend
  models (TW cash / US DRIP 30% / MY cash). New `shared/llm.py` (LiteLLM client + structured
  output + model registry + `llm_usage` token/cost log + graceful degradation; `litellm` dep).
  Spec/plan: `docs/superpowers/{specs,plans}/2026-06-09-data-ingestion*`.
- LLM config management + token-budget governance (`shared/`): DB-backed model registry
  (`llm_models`; per-model provider / endpoint / key / `vision` flag / pricing / context-window /
  timeout / retries / enabled). Four **nullable** role-defaults (`default` / `default_fallback` /
  `vision` / `vision_fallback`) — all empty = AI cleanly **off** (first-launch seed). `complete_structured`
  now: budget gate → role selection → **runtime failover** to the fallback model on provider error →
  **image (vision)** input → cost logged from the *selected* model's registry pricing. Three
  degradation signals — `AINotActivated` / `LLMUnavailable` / `LLMBudgetExceeded` (all subclass
  `LLMError`) — surfaced to callers (mapped to issue `kind`), never crash or fabricate. **USD budget**
  as an append-only reset ledger (`llm_budget_events`): remaining = latest reset amount − Σ usage cost
  since that reset; **unset = no cap**; **remaining < 0 blocks** ("額度用盡"); per-model usage/trend from
  `llm_usage` is never reset (a reset is a fresh start line, not a counter overwrite). Reusable
  `config_store` create-always / seed-once settings framework; package-root `portfolio_dash/bootstrap.py`
  composition root (so `shared/` keeps importing nothing internal); `llm_usage` ownership moved from
  `data_ingestion/` to `shared/llm_config`. AI Agents Input rewired to the registry API (no
  caller-supplied pricing). The settings-page UI stays deferred to `web_ui/`. Spec/plan:
  `docs/superpowers/{specs,plans}/2026-06-09-llm-config-and-budget*`.
- `scheduler/` in-process job scheduling (APScheduler, **triggers-only**): an extensible `JobSpec`
  registry + DB-backed `schedule_config` (on the `config_store` framework; idempotent per-job seeding,
  so a newly-registered job auto-gets a default row while user edits are preserved) + a `job_runs` log.
  v1 jobs trigger `pricing.refresh_*`: per-market post-close quotes + FX (`quotes_tw` / `quotes_us` /
  `quotes_my`, editable cron defaults in each exchange's tz), plus daily `history_daily` +
  `dividends_daily` sweeps; a manual `trigger_job` shares the same `run_job` path (job_runs logging; a
  job failure is logged as `error`, never crashes the scheduler). `build_worklist` reads the
  `instruments` table — a new nullable **`instruments.board`** column (idempotent migration) carries the
  resolved TW board, falling back to the market default (US `""` / MY `.KL` / TW `TWSE`) when unset.
  New dependency: `APScheduler` (locked in `stack.md`), confined to `scheduler/runtime.py`. The
  Scheduler settings-page UI is deferred to `web_ui/`. Spec/plan:
  `docs/superpowers/{specs,plans}/2026-06-10-scheduler*`.
- TW board resolution at instrument registration (`data_ingestion/` + `pricing/` + `shared/`):
  `Instrument` gains a persisted **`board`** attribute (`store.py` reads/writes it). `pricing.probe_tw_board`
  guesses a TW instrument's board by trying TWSE then TPEx (injectable providers, graceful on a network
  error). `data_ingestion.register_instrument` fills the board — US `""` / MY `.KL` deterministic; TW via
  an **injected** prober (keeping `data_ingestion` decoupled from `pricing`) — and upserts on confirm,
  raising a soft `board_unresolved` flag (never blocking) when a TW probe finds nothing. Resolves the
  board once so the scheduler work-list picks the right `.TW`/`.TWO` source; the listing/confirm UI is
  deferred to `web_ui/`. Spec/plan: `docs/superpowers/{specs,plans}/2026-06-10-tw-board-resolution*`.
- `portfolio/dashboard.py` — the orchestration combiner: `build_dashboard(conn, now,
  reporting)` assembles one complete `DashboardData` (KPIs, enriched holdings, realized
  P&L, returns, sector allocation, currency view, FX P&L, dividend summary, ex-dividend
  calendar, daily-replay trend series, freshness report, insight placeholders) from the
  ledgers + stored prices/FX; the contract `web_ui` (and later `llm_insight`) binds to.
  Introduces the one-way dependency edge `portfolio -> forex` (spec
  2026-06-10-dashboard-combiner-design).
- `portfolio/timeseries.py` — pure daily ledger-replay valuation series (market value
  vs cumulative net invested, carry-forward prices/FX, honest `incomplete`/unavailable
  flags).
- `pricing/store.py` — `get_fx_on` (on-or-before point-in-time rate) and
  `get_fx_history` reads; `data_ingestion/store.py` — `list_accounts` read.
- **Phase 0 — web API foundation (decision B):** `portfolio_dash/api/` FastAPI app
  factory (lifespan boots DB + scheduler; serves static `web/` via StaticFiles; routers
  under `/api/*`), the common error envelope (incl. LLM 402/409/503 mapping), the
  Decimal→string wire serializer (`to_wire`), per-request `get_conn`/`get_now`/`get_reporting`
  dependencies, and `GET /api/health` + `GET /api/dashboard` (serialized `build_dashboard` +
  `spark_30d` + `llm_quota`). Spec-17 test harness: `golden_db` fixture (seeded via the real
  write paths), injected clock (`GOLDEN_NOW`), `api_client`, `pytest-socket` network ban, and
  a `Makefile` (`make all`). Fee engine (spec 18): `FeeRuleSet` gains `flat_fee` /
  `stamp_duty_rate` / `stamp_duty_cap` and US/MY `min_fee`; MY stamp duty books to `tax`;
  worked examples W1–W9; US/MY rates backfilled from the spec-18.0 truth table (pending
  real-statement confirmation). `DividendType += NET` (MY single-tier).

## [v0.0.0] - 2026-06-05

### Added
- Project bootstrap: `CLAUDE.md`; `.claude/rules/` (stack, architecture,
  domain-ledger, markets-and-fees, data-and-pricing, llm-insight, engineering-process,
  design-handoff); `.claude/skills/` (resume-dev, ship-version); README, this
  changelog, LESSONS_LEARNED, .gitignore.
- Locked technology selection (Python 3.12 monolith: FastAPI + Jinja2 + HTMX +
  Alpine + ECharts + SQLite + LiteLLM + APScheduler; mypy strict; pytest).
- Domain model: `account` as a first-class entity (TW broker · Charles Schwab US ·
  Moomoo MY US · Moomoo MY); three markets (TW / US / MY); multi-currency
  (TWD / USD / MYR) with a single-reporting-currency combined XIRR (trade-date FX)
  and a currency-exchange ledger.
- Numeric precision model: `Decimal` end to end; store at full source precision
  (MY prices up to 3 dp), quantize amounts per currency minor unit at settlement.

_No application code yet — conventions and specification scaffold only._
