# Lessons Learned (PEM)

Post-error / post-mortem notes. **Before solving a problem that feels familiar, check
here first.** Each entry: date · context · what went wrong · the rule or fix that
prevents recurrence.

## Standing reminders (carried over)

- After any `CHANGELOG.md` edit, verify with `grep -c "^## \[v" CHANGELOG.md`
  (structural edits have corrupted it before).
- Prefer **bounded-section rewrites** over surgical in-place edits on structured docs.
- **Never load large reference files in full** — read bounded sections only.
- Version heading dates are **real delivery dates**, never placeholders.

## Domain reminders (this project)

- **No double counting:** dividends enter total return once (P&L uses original cost);
  FX gain/loss is an attribution breakdown of the reporting-currency XIRR, not additive.
- **Decimal, not float**, for money/price/rate; store full precision, quantize at
  settlement/display. MY sub-RM1 prices need 3 dp — do not truncate to 2 dp.
- **Average cost is computed on read** from `total_cost / shares`, never stored as an
  authoritative rounded value.

## Implementation lessons

- **An audit finding's SYMPTOM and its ROOT CAUSE do not deserve the same confidence
  (2026-07-26):** the full-site audit measured every symptom on a running instance
  (a 1,257px page scroll width, a −1399.07% rendered percentage, a 593px overflow, a
  missing validation gate) and every one of those held up. But FOUR of its root causes,
  inferred by reading code, were wrong — and following them would have produced two
  non-fixes and one regression:
  - `.topbar` overflow blamed on flex `min-width:auto`; the real cause was a
    `.topbar { flex-wrap: nowrap }` declared LATER in the file, winning on order at equal
    specificity. Adding `min-width: 0` changed nothing (verified by reading computed style).
  - KPI clipping blamed on the same; the real cause was `.kpi-band.v2` (two classes)
    out-specifying `@media { .kpi-band }` — media queries add no specificity — so two
    breakpoints were DEAD for that band.
  - The 資料中心 overflow blamed on a missing table scroll container; the table already had
    one. The cause was a global `.panel-sub { white-space: nowrap }` applied to a body
    PARAGRAPH, where `overflow: hidden` is inert because the span is inline.
  - The dividend gate proposed as `gross − withholding == net`; that identity is the US DRIP
    model's only — TW 二代健保補充保費 / 匯費 and US ADR fees legitimately make net < gross
    without any withholding, so the rule would have rejected real rows.
  The two unreliable classes are specific and predictable: **CSS cascade resolution** (which
  declaration actually wins is not knowable by reading — only computed style answers it) and
  **domain-rule reality** (a legitimate exception leaves no trace in the code). Rules:
  (1) an audit finding states 症狀 as a MEASUREMENT and 根因 as 已驗證 or 推測待驗 — never
  both at one confidence; (2) before implementing a CSS fix, read the COMPUTED value of the
  property you intend to change, and re-read it after — the first M1 attempt looked right in
  the diff and did nothing; (3) before tightening a domain invariant, enumerate the legal
  exceptions from the market rules, not from the code; (4) when the implementation contradicts
  the audit, the correction goes in the CHANGELOG entry for that version — a one-off report is
  not in anyone's loop.

- **A test pinned to a release-windowed identifier rots on a schedule (2026-07-26):** both
  what's-new e2e flows located their subject by `data-wn-key="0.1.17:market-risk-alerts"`,
  with a comment claiming the key was "version-proof". The ✦ panel renders only the SIX most
  recent versions, so shipping v0.1.23 pushed v0.1.17 out of the window and the locator began
  timing out — the pair had been red since the previous release and nobody noticed, because a
  ship that runs a "regression subset" never sees it. Rules: (a) never pin a test to an
  identifier the product WINDOWS or PAGINATES — discover the subject from the live payload and
  assert the behaviour; (b) a comment asserting "future-proof" is a claim, so check what
  actually makes it age; (c) attribute a suspicious failure BEFORE debugging it — `git stash`
  and re-run on the clean tree (the first stash silently no-opped here and briefly implied the
  failure was mine; verify `git stash list` is non-empty and the tree is clean before trusting
  the result).

- **Subagents stall waiting on their own background tasks (2026-07-22):** three
  Batch-B agents stopped mid-verification "waiting for the monitor/background sweep"
  — a subagent's background children can never re-invoke it, so the agent ends and
  the task orphans. Rule: agent briefs mandate FOREGROUND verification (no
  run_in_background, no monitors inside subagents); when one stalls anyway, resume
  it with an explicit synchronous-finish order plus ownership-based failure triage.
  (Related: the orchestrator's own long gates DO use detached scripts + a monitor —
  that pattern is for the main loop only, where notifications re-invoke it.)

- **A migration's blast radius includes every WRITER of the old shape, not just
  readers (2026-07-22):** the Batch-B deep review swept readers exhaustively yet
  still missed that boot-time `seed_accounts` re-INSERTS the deleted legacy
  accounts every launch (config-as-code writer) and that `seed_demo.py` books
  transactions under legacy ids (script writer). Both were caught late (one by an
  adversarial verifier, one by an implementing agent's escalation). Rule: for any
  id/shape migration, grep for WRITERS (seeds, scripts, importers, config-as-code)
  as their own checklist section, and test the full boot lifecycle twice — the
  migration function in isolation proves nothing about the seam it lives in.

- **A UI-label change must enumerate every EXISTING e2e that pins the old label
  (2026-07-21):** W3 changed the draft-preview rows from single values to 舊→新 pairs
  and its brief listed only the contract tests it owned; a prior round's e2e
  (`test_sell_hints_ledger_refresh_flow.py`) still waited for the old 「剩餘股數」/
  「新原始均價」 labels and failed the central pytest gate, costing a full gate rerun.
  Rule: before dispatching a unit that renames/restructures visible UI text, grep
  `tests/e2e` (and contract HTML assertions) for the affected labels/selectors and put
  every hit in the unit's ownership + test list. (Same class as the 2026-07-20 bare-mypy
  lesson: agent-scoped verification is a smoke check, the tree-wide sweep is the gate.)

- **The type gate must run BARE over the FULL scope, centrally (2026-07-20):** parallel
  implementation agents each ran `mypy portfolio_dash --strict` (package only, ~210 files)
  and reported clean; the central bare run (`mypy --no-incremental`, whole 522-file scope
  incl. tests/scripts) still found 2 real errors in test files. Two causes recur: (a)
  scoped runs skip tests/scripts entirely, (b) incremental caches mask errors after
  cross-file edits. Rule: agent-level mypy is a smoke check; the shipping verdict comes
  only from the orchestrator's bare, full-scope run.

- **Markup↔JS id contracts silently dead-zone — sweep them mechanically (2026-07-20,
  learned 2026-07-16):** JS that binds `#some-id` which no longer exists in the page
  markup fails SILENTLY (the feature is simply dead — the CSV drop zone and the 配股
  buttons both shipped dead this way). Unit and route tests cannot see it; only a
  mechanical sweep (extract every id the JS binds, diff against the markup) or a
  real-browser flow test catches the class. Rule: run the id-contract sweep before every
  ship, and give every new interactive element an e2e flow assertion, not just an API test.

- **Edit-distance similarity has no semantics for exchange codes — resolve exact-only
  (2026-07-19):** the symbol resolver fuzzy-matched an unregistered code against
  REGISTERED instruments with `difflib.SequenceMatcher` at a 0.75 threshold. Any two
  4-digit codes differing in ONE digit score EXACTLY `2*3/8 = 0.75` (2303 vs 2330,
  2883 vs 2882), so unrelated companies coerced into one another behind a 「視為」
  confirmation the user waved through — the LLM's correct output (2303 聯電) was
  overwritten by the local resolver. `ratio()` measures character overlap, which is
  meaningless for opaque identifiers where a one-symbol difference is a DIFFERENT
  entity, not a near-synonym. Rule: code-shaped input resolves EXACT-only and routes
  unregistered symbols to the register-first flow; name-similarity survives only as
  NON-BINDING suggestions (name-vs-name, never vs symbol), and the per-market code
  SHAPE lives in one source (`shared/symbol_format.py`) so the gate, the format
  warning, and the next-wave AI gate cannot drift. A regression test must pin the
  actual 0.75-tie pairs, not a generic near-miss.

- **Independently re-verify subagent gate claims; sanitize every user-derived HTTP
  header (2026-07-14):** one implementation agent reported "ruff clean" while 4 real
  violations existed (it likely ran the gate before its final edits) — a later agent
  caught it. Rule: the orchestrator re-runs cheap gates (ruff/mypy/targeted tests)
  itself on the final committed state; "green" is what YOU measured, not what an agent
  reported. Same review round: `Content-Disposition` interpolated a raw user-derived
  filename — a CJK symbol name 500'd every download (latin-1 header encode) and CRLF
  could inject headers. Rule: any user-derived value entering an HTTP header goes
  through a sanitizer (ASCII fallback + RFC 5987 `filename*`), and hostile-input
  probes (CJK, CRLF, quotes) belong in the contract tests.

- **Encode a named rule from the literature, not a plausible-looking variant, and don't
  let the test lock the variant in (2026-07-13):** the Swedroe 5/25 rebalance band was
  coded `max(5pp, 25%×target)` — plausible, and it passed review-of-its-own-tests
  because the tests asserted that behavior. But the canonical rule fires on whichever
  threshold is crossed FIRST = the tighter band = `min`; `max` made the relative leg
  dead code for small allocations and *loosened* the band for large ones. A rule with a
  named provenance (Swedroe/Faber/Moskowitz…) must be checked against the source's
  worked examples, and its tests must include a case where the candidate formulas
  DIVERGE (here: target 50%, 8pp drift — `min` fires, `max` is silent). An independent
  reviewer with the literature caught what the implementer + its own green tests did not.

- **A switch-shaped control must persist on interaction (2026-07-12):** the notify
  enable toggles flipped a CSS class and relied on a separate save button — the owner
  read it as "cannot be turned off" (toggled off, reloaded, it was back on). Anything
  that LOOKS like a switch must either write immediately (optimistic + revert on
  failure) or visibly mark unsaved state. Related: never swallow a provider's error
  body — Telegram's "chat not found" was the actionable reason, and only the bare
  status line was shown.

- **An index on a migrated column must be created AFTER the migration — and schema
  changes need a legacy-shape test, not just fresh-DB fixtures (2026-07-12):** adding
  `notified_at` to `alert_events` put the CREATE INDEX inside the initial DDL script,
  which runs before `_add_column_if_missing`. Every fresh-DB test passed (table
  created WITH the column → index fine), but the live demo DB had the pre-notify
  table shape → `no such column` → the app crash-looped at boot. The deploy gate
  (install + boot + /api/health) caught it — exactly its job. Rule: any DDL that
  references a column added by an additive migration must be ordered after that
  migration, and every schema change gets a regression test that seeds the LEGACY
  table shape first, then calls ensure_tables.

- **Transition detection over a dead-banded state needs HOLD memory, or the event is
  unreachable (2026-07-10):** the momentum reversal event compared consecutive raw
  states, but a 12-1 return on 252 sessions moves in small daily steps, so every real
  positive↔negative reversal dwells in the `flat` dead-band for ≥1 scan — and the
  intervening `flat` reset the stored sign, masking the flip. Every unit test was
  green (each pairwise hop is individually "correct"); only an adversarial
  SEQUENCE probe (pos→flat→neg across three scans) exposed that the feature was
  dead on arrival. Same class hit the trend detector as band-edge whipsaw
  (confirmed→neutral→confirmed emitted two events for noise the hysteresis rule
  exists to suppress). Rule: when detecting transitions over any state machine with
  a neutral/dead zone, store the last DIRECTIONAL state and treat neutral as a hold,
  never a reset — and always test transition detectors with multi-step sequences,
  not just adjacent pairs.

- **Cross-provider seam gaps only surface on live data — live-verify every data-pipeline
  batch before ship (2026-07-09):** the volume wiring was fully green on unit + contract
  tests, yet on the demo site every TW/MY volume signal degraded: the newest TW row is
  written by the twse latest-quote provider (no volume) while history rows come from
  yfinance (with volume) — a chain interaction no single-provider fixture models. Without
  the live pass, TW volume confirmation would have shipped permanently dead (or raising
  on interior gaps). Rule: when a batch touches the provider chain or data shape, the
  demo-site behavioral pass must assert on REAL fetched rows (coverage %, per-symbol
  signal output), not just health/e2e smoke; and multi-provider columns must be modeled
  as per-row-nullable from day one (`Sequence[Decimal | None]`, trim/degrade policy
  decided explicitly).

- **A no-build-step frontend still needs explicit cache control (2026-07-07):**
  Starlette StaticFiles sends ETag/Last-Modified but NO `Cache-Control`, so browsers
  apply HEURISTIC freshness (~10% of asset age) and serve cached `web/*.js` for days
  without revalidating. Deploying HTML that calls a new helper (`fmt.aiAttrib`) paired
  fresh insights.html with the owner's stale cached format.js → `f.aiAttrib is not a
  function` → the graceful-degrade `.catch` wiped every AI card to empty states with
  ZERO console signal, on insights + news + dashboard. A fresh-browser Playwright pass
  cannot see this class — reproduce stale-cache bugs by route-intercepting the old
  asset. Fixes: `Cache-Control: no-cache` on the static mount (ETag revalidation every
  use) + `?v=<version>` stamps on all local asset tags (poison flush; rerun
  `scripts/stamp_asset_version.py` on version bump; contract-tested). Also: degrade
  paths must at least `console.warn` the swallowed error.
- **Desktop Chromium at 390px is NOT iOS Safari (2026-07-07):** `position: fixed`
  inside `.topbar` is hijacked on Safari/iOS because a `backdrop-filter` ancestor
  becomes the containing block for fixed descendants (per spec; Chromium is lenient,
  and a sticky-topbar-at-origin geometry can make the bug invisible even in Playwright
  WebKit). Overlays that must be viewport-fixed are PORTALED to `<body>` (no filtered/
  transformed ancestor), positioned by JS from the anchor's rect; use `dvh` alongside
  `vh` for iOS dynamic-toolbar height. Real-device confirmation stays with the owner.
- **A mocked seam hides a missing REQUEST contract — put the contract in-band
  (2026-07-05):** the LLM structured-output path was 100% broken on the live test
  site (every Loop-1 run: 4 provider calls, $0.036 spent, zero cards) while 1,000+
  mocked tests stayed green. Two stacked causes: (1) `response_format` was only sent
  when `litellm.supports_response_schema()` said yes — and that capability map
  returns **False for every `openrouter/*` id** — and (2) nothing in the assembled
  prompt itself asked for JSON, so models returned beautiful Chinese prose and
  `model_validate_json` failed on both role models. The mocks always returned valid
  JSON, so no test could see it. Rules: (1) at an external seam, never rely on an
  out-of-band capability flag for a REQUIRED output contract — put the contract
  in-band (schema appended to the prompt) so it holds for every provider; (2) parse
  defensively (fence strip / object slice) before failing an attempt; (3) a feature
  gated on external behavior is unverified until a LIVE Loop-1 pass runs on the test
  instance — schedule nothing before that.
- **Don't hardcode one failure reason on a multi-cause path (2026-07-05):** every
  mid-run `LLMError` in `generate.run_insight_type` was reported as
  `budget_exhausted_mid_run`, so a provider/parse failure told the operator to top
  up a budget that had $4.96 remaining. When one except-branch covers several
  exception kinds, the recorded reason must carry the kind (`exc.kind`), and the
  human `detail` must carry the message.

- **Design-handoff stubs look wired — audit them against the real API surface
  (2026-07-02):** the topbar 更新報價/重算 buttons shipped v0.1.0→v0.1.2 as
  design-preview stubs (toast only) even though the real endpoints
  (`/api/actions/refresh-quotes|recompute`) existed and were tested — the user saw a
  success toast and nothing happened, and no test failed because the CONTRACT tests hit
  the endpoint while the SMOKE tests only asserted "page renders clean". A stub that
  *renders* fine is invisible to both suites. Rule: after any design-handoff
  integration round, grep the frontend for `設計預覽/後端接線後/mock` and reconcile each
  hit against the router table before calling a page "wired"; a page is wired only when
  its **actions**, not just its renders, hit the backend (assert with expect_request /
  expect_response in at least one flow test).
- **A write path must not accept what the read path cannot represent (2026-07-02):**
  committing a transaction for an unregistered symbol passed (soft issue bypassed on
  confirm) but the dashboard could only KeyError on it — the same class of bug as the
  earlier acked-oversell 500. Invariant now enforced from both sides (hard issue at
  commit + graceful exclusion with `freshness.unregistered_symbols` on read). When a
  "needs confirmation" issue has NO valid confirm semantics downstream, it must be a
  hard block, not a soft warn.

- **A never-500 degradation must cover EVERY replay call site (2026-07-02):** the
  acked-oversell dashboard fix passed `allow_oversell=True` to the MAIN
  `build_book` call, but `timeseries.daily_value_series` builds its OWN per-day
  books and still raised `OversellError` → the dashboard 500'd through the trend
  path anyway. Found only when a new mutation test asserted `GET /api/dashboard`
  is 200 after producing the state. Rules: (1) when adding a degradation
  flag/behavior, grep ALL callers of the guarded function and audit each; (2) any
  test that creates a degraded-but-legal ledger state should end by asserting the
  dashboard still answers 200.

- **An enum member nobody exercised end-to-end is a landmine (2026-07-03):**
  ``DividendType.NET`` (馬股單層淨額) existed since the schema, was bookable via
  CSV import, and CRASHED every rebuild (cost_basis routed non-CASH to the
  shares-branch → "requires reinvest_shares" ValueError → dashboard 500) — plus
  trend/XIRR silently dropped NET cashflows. Found only when the dividend inbox
  expansion booked one for real. Fixes: ONE definition
  (``shared.models.enums.CASH_DIVIDEND_TYPES``) used by all three replay sites;
  rule: when adding an enum member, grep every ``is Enum.X`` dispatch over that
  enum and cover the new member with an end-to-end (book → rebuild) test.

- **JS `+` on Decimal-string wire values concatenates, then renders NaN
  (2026-07-03):** the currency-composition panel summed `holdings[].weight`
  (Decimal STRINGS) with `+` — one holding per currency parsed by luck, two+
  concatenated into garbage → 權重 NaN% on the live dashboard, invisible to every
  hermetic suite (golden held one holding per currency). Rules: display-only
  RATIO aggregation must coerce explicitly (`Number()` + isFinite guard, the
  documented non-money exception); when a panel aggregates a wire array, test it
  with 2+ rows per group; full-site screenshot review catches what selector
  assertions miss.

- **A derived quantity needs ONE definition, not per-caller reimplementations
  (2026-07-02):** `data_ingestion.holdings.current_shares` re-derived "shares
  held" as buys−sells over the transactions table only, silently drifting from
  `build_book`'s four-source replay (opening inventory + buys − sells + stock/DRIP
  reinvest shares). Result: FALSE oversell warnings when selling opening-backed
  positions and wrong `held` flags — a core position-management basic broken for
  any user whose holdings predate the app. When two modules answer the same
  domain question, either share the implementation or add a test pinning them to
  each other.

- **`StrEnum` + Pydantic v2 serialization (2026-06-06):** `Currency`/`Market` are
  `enum.StrEnum` (ruff UP042 prefers this over `(str, Enum)` on 3.11+). A `StrEnum`
  member *is* a `str` (`isinstance` is `True`, SQLite binds it as TEXT, `json.dumps`
  and `model_dump(mode="json")`/`model_dump_json()` emit a bare string). **But**
  Pydantic v2 `model_dump()` in the default *python* mode returns the **member object**,
  not a bare string — so `type(x) is str` is `False` even though `isinstance(x, str)` is
  `True`. When serializing settings/models for the web layer, use json mode (or
  `isinstance`, never `type() is str`).
- **sqlite3 DDL is not transactional under the legacy isolation model (2026-06-06):**
  `shared/db.session()` commits/rolls back DML correctly, but Python's default
  `isolation_level=""` runs standalone DDL (CREATE/DROP TABLE, etc.) *outside* a
  transaction — a `rollback()` after pure DDL is a no-op and the schema change sticks.
  DML that follows DDL in the same session *is* transactional (Python 3.12 no longer
  auto-commits before DDL). Keep schema migrations out of plain DML sessions, or handle
  this explicitly.
- **Dev gates need the repo `.venv` interpreter (2026-06-09):** runtime deps + tooling live only in
  `.venv` (`./.venv/Scripts/python.exe`); the bare `python` resolves to a system interpreter without
  them, so `python -m pytest` / `-m mypy` report spurious missing-module / missing-stub errors that
  look like regressions. Always run gates via the venv; instruct subagents to do the same.
- **Fix the test, not the production code, when the test is wrong (2026-06-09):** a flawed budget
  test (far-future-dated usage rows vs. wall-clock reset timestamps) tempted an implementer to make
  `reset_budget` scan `llm_usage` and advance its timestamp — bending real behavior to satisfy a
  broken test. The correct fix was the opposite: keep `reset_budget` a plain `now()` event and make
  the test deterministic with explicit timestamps. When a test forces awkward production logic,
  suspect the test first.
- **Verify a "flaky test" against the EXIT CODE, not a grep of `-rA` output (2026-06-18):** chasing an
  intermittent e2e "1 ERROR", I grepped pytest `-rA` output for `^ERROR` — which matched a benign
  **captured-log line** `ERROR  asyncio: Task was destroyed but it is pending!` (Playwright's internal
  `Page._on_route` task GC'd at page close on the Windows ProactorEventLoop), NOT a pytest ERROR
  *outcome*. The suite was green the whole time (exit 0; zero `^(FAILED|ERROR) tests::` lines). Cost
  ~15 needless multi-minute e2e runs + three wrong-location "fixes". Rule: confirm flakiness with the
  process **exit code** (and `^(FAILED|ERROR) (tests/|at )` for real outcomes); ERROR-*level* log lines
  are not test failures. The asyncio log only appears under `-rA`/`-rE` (passed-test captured logs);
  the real `make e2e` (`-q`) gate never shows it. (The harness hardening it prompted — 60s readiness/
  Playwright ceilings, `flow_server` spawn-retry on the `_free_port` TOCTOU race, best-effort teardown —
  is still valid robustness and was kept.)
- **Test the REAL first-run boot path, not just a harness-seeded DB (2026-06-19):** the entire suite
  built its DB via the test harness (`tests/conftest.py::init_golden_base` / `_build_golden_db`), so the
  app's actual `_lifespan` bootstrap was NEVER exercised — it silently omitted `create_pricing_tables`
  (`prices`/`fx_rates`), `datasources_store.ensure_seeded`, and `seed_accounts`. A fresh 0-byte DB looked
  fine because an empty portfolio never queries the (missing) `prices` table; the FIRST transaction made
  `GET /api/dashboard` 500 with `no such table: prices`. Lesson: a green suite that always seeds via a
  helper can hide a broken production bootstrap — add at least one test that drives `create_app()` through
  its real `lifespan` against a throwaway DB (`tests/contract/test_first_run_bootstrap.py`). Keep all
  bootstrap steps idempotent (`CREATE TABLE IF NOT EXISTS` / `ON CONFLICT`) so re-running on an existing
  DB (e.g. the e2e server re-bootstrapping the harness-built golden DB) is safe.

- **2026-07-08 — Duplicated static surfaces drift; keep ONE canonical page.** The settings
  area shipped as a tabbed `settings.html` PLUS five standalone `settings-*.html` sharing
  the same JS but duplicating markup. They drifted in BOTH directions (a new panel added
  only to the standalone; a panel that only ever existed on the standalone; a stale label
  on the tab) and an unguarded `getElementById(...).addEventListener` for a node present
  on one surface threw on the other, silently killing later wiring. Fixes: standalone
  pages became redirect stubs (same as `ledger.html`/`input.html`); all per-node wiring
  is guarded on element existence; e2e walks the REAL nav path (tab click), not the
  convenient standalone URL. Lesson: when two pages share JS, either they share ONE
  markup source or one of them redirects — never hand-sync.

- **2026-07-15 — A fee/flag feature the engine supports but no entry path passes is a
  silent money bug the engine's own unit tests cannot see.** `compute_fees` supported
  `is_etf`/`daytrade` (unit-tested green) but manual + CSV entry never threaded the
  instrument's ETF flag in, so every real ETF sell was taxed 0.3% instead of 0.1% —
  found only by the adversarial stress oracle recomputing fees per trade. Lessons:
  (a) flags that alter money must be REGISTRY-authoritative and resolved at the entry
  seam, never trusted to input defaults; (b) coverage must include one end-to-end
  assertion PER ENTRY PATH (manual API, CSV, AI), not just the pure engine; (c) the
  independent-oracle stress audit (`/stress-audit`) is the class of test that catches
  "engine right, wiring wrong" — run it whenever money-of-record code changes.

- **2026-07-15 — Flow-server e2e files MUST carry the `_loopback_sockets` autouse
  fixture; in-isolation green is not proof.** pytest-socket re-bans sockets before each
  test, so a new e2e file that spawns the flow server passes when run alone (a prior
  fixture left sockets enabled) but fails with `SocketBlockedError` under full-suite
  ordering. Copy the fixture from `test_whatsnew_flow.py` into every new flow-server
  e2e file, and treat "passes alone, fails in suite" as an ordering/isolation smell,
  not flakiness to retry.

- **2026-07-24 — "A shown total and the detail beside it must come from ONE definition"
  recurred a THIRD time.** The symbol drawer's 交易明細 read the transactions ledger only
  (no opening inventory, no DRIP shares) while 部位摘要 used the four-source `build_book`,
  so their share sums disagreed — the exact class of the 2026-07-02 `current_shares`
  bug. Fix: the drawer's activity list and summary are now both derived from the same
  authoritative combiner (`build_dashboard`/symbol endpoint), with a visible
  reconciliation footer. Rule: whenever a UI puts an aggregate next to its detail rows,
  they must share a single server-side source; add a reconciliation assertion, and when
  this smell appears grep for OTHER aggregate/detail pairs (dashboard 合計 vs holdings,
  section footers vs lists) in the same pass.

- **2026-07-24 — "field is pristine" tracking that keys on DOM input/change events is
  blind to programmatic AND user-pick fills.** The quick-add candidate pick wrote
  名稱/產業 with `el.value = …` (no event), so the `namePristine`/`sectorPristine` flags
  stayed true; a subsequent re-validation miss then blanked the freshly-picked data as
  "stale". Rule: when you fill a field programmatically, also update whatever guard
  protects it (pin it, or clear/set the pristine flag) — never assume a value assignment
  and a user keystroke leave the same state. A user's explicit pick is NOT "untouched".

- **2026-07-24 — long gate runs (`pytest tests/e2e`, full regression) get killed as
  harness background jobs; run them OS-detached + poll with Monitor.** `Bash
  run_in_background` on a ~20-min suite was terminated mid-run twice (killed at ~27%);
  the same suite launched via `Start-Process -WindowStyle Hidden` writing an
  `*_exit.txt`, watched by a `Monitor` until-loop, completes reliably. Rule: for any gate
  longer than a couple minutes, use the detached `.cmd` + exit-file + Monitor pattern —
  exit codes are authoritative (ignore the trailing Windows ProactorEventLoop teardown
  noise and the missing pytest summary line).
  - **2026-08-09 addendum — the exit file is not optional, and this entry was re-learned
    by skipping it.** `tests/e2e` was launched as a `Bash run_in_background` job first and
    killed again (this lesson exists precisely for that), then relaunched detached via
    `Start-Process` — but with only `-RedirectStandardOutput`, no exit file. The run
    finished cleanly and the summary line raced with process exit, so the log ended at
    `-- Docs: …` with **no count line at all**: the Monitor's `grep "passed"` never
    matched and it reported "exited without a pytest summary" on a fully green run. The
    verdict had to be reconstructed by counting progress characters (131 outcome chars,
    zero `F`/`E`/`s`). That reconstruction is sound but it is evidence-by-inference on a
    release gate. Rule: `Start-Process` **plus** `$p.ExitCode | Set-Content <file>`, and
    make the Monitor watch the exit file — never the summary line, which this entry
    already warned is unreliable.

- **2026-07-24 — an idempotency/suppression key must be structural, never editable
  free-text.** The 折讓款 inbox suppressed an already-booked month by matching the cash
  movement's editable `note`; editing that note re-surfaced the month → a second confirm
  double-credited the refund. Fix: suppress on the movement DATE (structural) as the
  primary key and lock the anchor fields (kind/date) of a booked rebate movement
  (frontend + backend guard). Rule: anything that prevents a double-write must hinge on
  data the user cannot silently edit.

- **2026-07-29 — "the site is down" is two different faults; diagnose the ingress layer
  separately from the app, and trust only the AUTHORITATIVE DNS answer.** The test site
  was unreachable from the internet while prod was fine. Every app-level signal was
  green — the service had been `active (running)` for 20 h, `127.0.0.1:<port>/api/health`
  returned the expected version, and the tunnel daemon's own `serve status` printed
  "Funnel on". The real fault was one layer up: the ingress provider's control plane had
  **stopped publishing the public DNS record** for that node (authoritative NS returned
  NXDOMAIN with the `aa` flag, while the sibling node kept its A records). Two traps:
  (a) the local `serve status` reports the node's *intent*, not what the control plane
  actually accepted — it says "on" even when the name no longer resolves; (b) a client-side
  lookup can't distinguish a withdrawn record from a cached negative answer, so query the
  authoritative nameserver directly (`dig … @<auth-ns>`) before concluding anything.
  Fix was to force re-advertisement (`serve reset` + re-apply), not to restart or reinstall
  the app. Rule: when a service is healthy on localhost but dead from outside, walk the
  path outward — app → local port → tunnel/proxy intent → *published* DNS → TLS — and stop
  at the first layer whose **externally observable** state disagrees with its local state.

- **2026-07-30 — a full-replace PUT plus a new optional column is a silent data-loss bug,
  not a display gap.** Adding `cash_movements.acq_home_amount` and wiring it into the POST
  looked complete; the movement EDIT dialog still sent the old field set, and PUT is a full
  replace — so editing an unrelated field (a note) NULLed a recorded cost basis with no
  error, no warning, and no way to notice. Nothing in the type system or the tests pointed
  at it; it surfaced only by asking "who else writes this row?". Rule: when a column is
  added, enumerate every WRITE surface (POST, PUT, importers, merge/relabel jobs, seeders)
  before calling it done, and pin the round-trip with a test that edits a DIFFERENT field
  and asserts the new one survived. Corollary from the same change: a flag derived from an
  AMOUNT (`gap != 0`) goes quiet whenever the amount collapses to zero for an unrelated
  reason — the pool being empty — while the underlying condition is still true; derive
  disclosure flags from the CAUSE (a ratio, a count) and use the amount only as detail.

- **2026-07-30 — let the project config decide a gate's scope; hand-picking paths breaks it
  in BOTH directions.** Running `mypy --strict portfolio_dash scripts tests` reported 243
  errors and looked like a catastrophic regression; every one came from
  `scripts/stress_audit`, which `pyproject.toml`'s `files = [...]` deliberately excludes
  (it is an untyped independent oracle). The mirror of the 2026-07-20 lesson, where a
  too-NARROW hand-picked scope hid real errors. `ruff check .` has the same failure mode:
  it lints untracked tooling that is not part of the shipped source. Rule: run the gates
  **bare** (`mypy`, and ruff over the tracked source roots) so the repo config — the same
  thing CI and `/ship-version` read — defines the verdict; a path argument is a debugging
  aid, never the verdict.

- **2026-07-30 — a scenario whose premise is a hard-coded constant will silently MUTATE an
  accumulating environment once that premise expires.** The stress scenario's oversell probe
  sold a fixed 100 shares expecting a 422 block, with the comment "NOT force-written, demo
  stays clean". On the accumulating test site an earlier run had left a larger net position,
  so 100 was no longer an oversell: the app correctly accepted it, and the "guard test" wrote
  a back-dated sell that permanently destroyed that symbol's cost basis (average 6,100 USD
  vs a 379 market price). The check reported a failure, which read as an app regression when
  the real event was the fixture writing bad data. Rule: in an environment that accumulates,
  every destructive-path probe must derive its quantity/premise from the state the app
  reports AT RUN TIME (here: `held + 1`), and skip itself when the premise cannot be
  established — never assert a fixed constant against a moving baseline.

- **2026-07-30 — an oracle agreeing with the app proves CONSISTENCY, not VALIDITY.** After
  the bad row above, the independent replay and the app agreed to the last digit on a
  position whose average cost was off by a factor of 16, because both faithfully replayed
  the same impossible transaction. Reconciliation can only catch computation errors; it is
  blind to a defective INPUT CONTROL. Rule: pair every reconciliation with plausibility
  assertions the ledger cannot satisfy when an impossible row got in (average cost within a
  sane band of market price; a position that never goes negative at ANY date; sale proceeds
  in cash always matched by a realized row), and treat "all reconciled" as a statement about
  arithmetic only.

- **2026-08-01 — a fix for a money bug is itself a change that needs auditing; two
  independent passes each caught a defect the FIX introduced, and both times the type
  checker and the reconciliation were green.** Round 1: making the FX pool admit foreign
  deposits was arithmetically right, but five consumers assumed a positive basis — a
  dividend during a short booked as income, a DRIP that made a position vanish, a profitable
  short shown as a loss, every short badged 已回本, and the trend dropping the liability
  while cash kept the proceeds. Round 2: raising on the newly-unbookable case was right, but
  three STRICT `build_book` call sites (重算 / what-if / tax export) caught only
  `OversellError`, so a user-reachable button returned 500. Neither round was visible to
  mypy (it cannot know an `except` clause is missing one of two types) or to reconciliation
  (both engine and oracle replayed the same rows identically). Rule: after fixing a
  money-of-record calculation, enumerate the CONSUMERS of every value whose domain you
  widened (can it now be negative? zero? raise?) and re-audit — the fix is a change like any
  other, and "the tests that were green before are still green" is not evidence about it.

- **2026-08-01 — subclass a new exception from the one the codebase already degrades on.**
  Adding `UnbookableLedgerError(ValueError)` rather than a bare `Exception` meant every call
  site that already wrote `except (ValueError, KeyError)` kept its exact behaviour, while the
  strict sites could catch the new type precisely and answer 4xx. A sibling of `Exception`
  would have silently bypassed those existing guards and turned a graceful degradation into a
  500 in places nobody edited. Rule: when introducing an exception into an established
  hierarchy, pick the base class from what existing handlers ALREADY catch, so the blast
  radius of the new type is exactly the sites you intend to change.

- **2026-08-02 — on Windows, `gcloud` is a *batch file*, so cmd.exe re-parses your remote
  command and eats its shell operators.** `scripts/vm_exec.py` ran fine for several remote
  commands containing `&&` and `|`, then failed on one containing
  `|| echo "(unset -> default 90 days)" ... | cut -f1` with the *local* Windows error
  `'cut' is not recognized as an internal or external command`. Mechanism: `subprocess`
  quotes the whole `--command=…` argument, but `gcloud` resolves to `gcloud.cmd`, so cmd.exe
  parses the line — and the **embedded double quotes close cmd's quoting context**, exposing
  the following `|`, `||` and `>` (inside `->`!) as cmd operators. The earlier commands
  survived only because their quotes happened to leave no metacharacter outside a quoted
  span; it was luck, not correctness. Fix: base64-encode the remote script and send
  `echo <payload> | base64 -d | bash`, so nothing but `[A-Za-z0-9+/=]` is ever parsed by a
  shell other than the remote one. Rule: when a command crosses a `.cmd`/`.bat` shim, do not
  try to quote your way out — remove the metacharacters from the command line entirely.

- **2026-08-02 — a gate that goes quiet reads exactly like a gate that passed; twice in one
  session, two different mechanisms.** (a) `pyproject.toml`'s `addopts` already carries `-q`,
  so passing `-q` again on the command line makes `-qq`, and pytest at that level **drops the
  final `N passed` summary line entirely**. Two full 25-minute runs produced no verdict line
  and sent me hunting an `Exception ignored in: BaseEventLoop.__del__` teardown traceback that
  was pre-existing, harmless (Python explicitly ignores GC-time exceptions; exit code stayed 0)
  and completely unrelated. Check `addopts` before adding verbosity flags — and read the
  process **exit code**, which no verbosity setting can suppress. (b) PowerShell's `*>`
  redirection writes **UTF-16LE**; `grep`/`Select-String` from Git Bash then match nothing
  because every character is separated by a NUL byte. A layout probe that had actually found
  68 clean results was reported as "0 overflows" from a log the grep could not read at all —
  the same string of characters as a genuine pass. Decode explicitly (`raw.decode("utf-16")`
  when the BOM is `\xff\xfe`) or write UTF-8. **Rule for both: before believing a zero, prove
  the measurement ran** — count the probes, assert the denominator, read the exit code. A
  count of findings is only meaningful next to a count of things examined.

- **2026-08-05 — a remediation applied at one call site is not applied.** Audit H1
  (2026-07-26) diagnosed a percentage that flipped sign when its denominator went negative,
  added a server-computed `unrealized_pct` with `abs()` in the denominator, wrote a careful
  comment explaining the trap, and moved the **drawer** onto it. The **holdings table** kept
  its own `unrealized_pnl / adjusted_cost_total` divide and therefore kept the bug — for
  eleven days, three feet from a comment describing it exactly. Nothing caught it because the
  API payload was already correct: contract tests read the payload, the layout guard reads
  geometry, and neither reads what the cell says. Rule: when a fix replaces a *derivation*,
  grep for the derivation, not for the symptom — the second call site is the default, not the
  exception. And when the fix is "the server now computes this", the regression test has to
  assert on the **rendered** value, because the payload was never the thing that was wrong.

- **2026-08-05 — an invalid CSS value is discarded, not clamped, and the element silently
  falls back to its stylesheet rule.** `fill.style.width = '-2.29%'` does not produce a
  zero-width bar; the CSSOM rejects the declaration outright, leaving `style.width` empty, so
  `.mini-bar .fill` — which declares no width and is `display: block` — filled its entire
  track. A −2.29% weight drew a bar identical to the 99.33% holding. The lie is worse than a
  crash: it is the most visually salient element in the cell and it claims the opposite of the
  number printed beside it. Rule: any computed CSS length must be clamped in JS to a range the
  property accepts. Never rely on the browser to sanitise it, and never write a fallback rule
  that is a *plausible* value (a full bar) when the honest fallback is an empty one.

- **2026-08-05 — the states between "empty" and "populated" are the ones no fixture covers.**
  Every fixture in this repo starts with history; the demo site carries 62 transactions across
  months. So 0 rows and N rows were both well tested, and **exactly 1** was not — which is
  precisely where a divide-by-zero, an `index [0]`, a single-point chart, or an annualization
  over a one-day window lives. Climbing a fresh ledger one row at a time (21 rungs, four
  independent detectors per rung) found four display defects that 2,700 passing tests, a
  whole-site layout sweep and a 2,287-control wiring sweep had all missed. Rule: when a system
  is tested at zero and at scale, test the transition — and note that the transition is not a
  contrived edge case, it is *literally* the first hour of every real user's experience.

- **2026-08-05 — write the check, not the corrections.** Twenty-one English strings, four
  unmapped reason codes and three JS money-divides were each individually trivial to fix and
  would each have come back: the codebase had *already* converged on Chinese messages (every
  test asserting on `.message` asserts on Chinese) and *already* locked "the frontend never
  computes money" in CLAUDE.md — neither rule was enforced by anything. Three guards now
  enforce them, each derived from a live source (the Pydantic wire models, the `LLMError`
  subclasses, the module's own constants) so it cannot go stale, and each proven to FAIL on
  the pre-fix tree before being trusted. Rule: when a fix list is long and boring, the finding
  is the missing control, not the items — and an allowlist that needs a new entry every
  release is the same missing control wearing a disguise.

- **2026-08-06 — an exact `==` between two sums of the same Decimals is a bug when the
  summation ORDERS differ.** The symbol drawer's reconciliation footer flagged 對帳不一致 on a
  perfectly consistent demo ledger. `_reconcile` computed `95 + (a+b+c)`; `build_book` computed
  `((95+a)+c) + (35+b)` — the same three DRIP reinvest shares, which are `net / price`
  quotients carrying ~28 significant digits. At the default 28-digit context, adding a tiny
  value to a large running total truncates the tail on *every* addition, so the two orders
  disagreed in the 26th decimal place. Decimal is exact per operation, **not associative across
  a magnitude gap**. Rule: compare quantities derived by different paths with a
  **difference test** against a domain tolerance, never `==`. And not quantize-then-compare
  either — truncating both sides to 6 dp preserves the same bug class, because two values 1E-27
  apart can still straddle the 6-dp boundary and truncate to different results (demonstrated
  before choosing the fix).

- **2026-08-06 — a proof rendered at a precision that cannot show what it proves is not a
  proof.** The same footer printed 「期初 0 ＋買 95 −賣 0 ＋配股/DRIP 0 ＝ 部位摘要 95 股」 beside
  its own ⚠, because every term went through `f.num`, whose default is **0 dp**. The equation
  looked perfect and the flag looked insane. The dangerous direction is the mirror image: a
  REAL sub-0.5-share break would also have printed as a balanced equation. Worse, the fix
  exposed a second-order rule — raising the drawer's precision alone would have made 部位摘要
  read 95.045712 in the footer and 95 in the stat three lines above it. Rule: a display
  precision is a property of the QUANTITY (shares: 6 dp, trimmed), not of the widget; put it in
  one formatter (`f.shares`) and route every surface through it. Before this there were four
  precisions for the same number (0 dp, 2 dp, 4 dp, and two hand-rolled "4 dp if it has a
  fraction" branches) across the dashboard, drawer, ledger, inbox and picker.

- **2026-08-06 — `kill -0 <pid>` from Git Bash cannot see a native Windows process.** A monitor
  watching the full pytest run reported "PYTEST EXITED" while the run was still going: MSYS
  `kill` resolves its own PID namespace, so the liveness probe returned false immediately.
  Meanwhile the real suite had genuinely stalled — six orphaned `uvicorn` processes from
  sessions six days earlier were still holding ports, and the e2e fixtures waited on them
  forever. Two rules: check Windows process liveness with `Get-Process`, not `kill -0`; and
  before blaming a slow suite, list the stray servers (`Get-CimInstance Win32_Process`) — a
  previous session's "instances stopped" claim is not evidence they stopped.

- **2026-08-09 — PowerShell's escape character is a BACKTICK, so `\"` does not escape a
  quote — it ends the string.** A remote command for `vm_exec.py` was written as
  `--cmd "… c.execute(\"SELECT COUNT(*) FROM \"+t) …"`. Each `\"` closed the PowerShell
  string, leaving `COUNT(*)` outside it, and PowerShell tried to run `*` as a command:
  *"The term '*' is not recognized"* — an error that points at the SQL and says nothing
  about quoting, on a command that would have been valid in bash. The payload crosses
  three parsers (PowerShell → gcloud/base64 → remote bash), and per-layer escaping is
  unmaintainable at that depth. Rule: build any multi-line or quote-bearing remote command
  as a **single-quoted here-string** (`$cmd = @'` … `'@` with the terminator at column 0)
  and pass it as `--cmd $cmd`. Nothing inside is interpolated or escaped, so the remote
  shell receives exactly what you wrote — nested `'…'` for awk and `"…"` for `python -c`
  both survive untouched.

- **2026-08-11 — "as of when?" is the question a validation reading a REPLAYED state must
  answer, and the answer is almost never "the end of the ledger".** `validate_corporate_action`
  replayed the whole ledger, and four hard rejections read it (E3 oversold source, E22 oversold
  destination, E5/E18 short). On a bulk import the post-action trades are already loaded — that
  is what a broker export *is* — so the position was **already 賣超 when its own action was
  validated**, and E3 rejected the split that made those trades legal, advising 「請先補登缺少的
  買進或期初庫存」: *fabricate a buy instead of recording the split*. Measured on the feature's
  own headline case (buy 100, 7-for-1, sell 400). This is the **third** occurrence of one shape
  — a guard evaluated against a state the action itself would fix (F-08's naive share count;
  D13's ⚠ that provably never fired; now this) — and the third time it survived review, because
  the **primary** entry door is the one door that cannot exhibit it: at the 賣超 confirm dialog
  the sell is not committed yet, so the future the guard wrongly reads does not exist. Two
  rules. **(1)** When a check consumes a replay, the cut is part of the check's specification —
  write it down beside the rule, not in the caller. **(2)** Take the **inputs**, not the
  computed state: the parameter changed from `book` to `bundle`, so a caller now *cannot* hand
  in a wrongly-scoped replay. A hoist that caches the wrong object is worse than no hoist.

- **2026-08-11 — a degradation is per-CALL-SITE and per-EXCEPTION-TYPE; handling one class and
  leaving its sibling is invisible.** `strategy/whatif.py` caught `UnbookableLedgerError` around
  `build_book`, but `OversellError` is a **separate hierarchy** (`Exception`, not `ValueError`),
  so it escaped as a 500 — and the symbol drawer posts 試算 on open, so ONE undeclared oversell
  anywhere made *every* symbol's drawer 500, across other accounts and other markets. Separately,
  `store.load_ledger_bundle` raised on a malformed corporate-action row from **above**
  `build_book`'s graceful path, taking down every page. The 2026-06 lesson said "apply never-500
  degradation at EVERY `build_book` call site"; both of these obeyed the letter. Extend it: also
  at every **layer above** it that can raise first, and for every exception type the call can
  produce — when you add a catch, `grep` the exception hierarchy rather than the one name you
  came for.

- **2026-08-11 — the third option for an unreadable row is neither "raise" nor "drop".** Three
  copies of the stored-row → domain-model conversion all raised, each with a docstring correctly
  arguing against *dropping* ("a silently omitted action leaves a share count wrong by the ratio
  and looking entirely normal") and then choosing the only worse option. **Record and flag** was
  already the codebase's answer for "this exists and cannot be trusted" — the row became an
  `UnappliedAction` with a zh reason, which blanks XIRR with a named cause and marks the position
  待釐清. No new vocabulary was needed. Corollary: a conversion open-coded three times is three
  chances for one copy to keep raising after the others learned not to, and the divergence is
  invisible until a bad row happens to arrive through that particular path — collapse it to one
  owner while you are there.

- **2026-08-11 — a scenario list is a specification of DETECTION POWER, not of coverage.** The
  spec's own stress-audit scenario list named a "2-for-7 exchange (the exactness case)" — but
  `700 × (2/7)` is **exactly 200** at 28 digits, a fact the document had itself measured two days
  earlier and then left the sentence pointing at. Every other ratio in the list also had a
  terminating quotient, so an oracle built literally to the list was blind to the rounded-ratio
  trap the whole feature exists to prevent; and every action in it sat alone on its date, so a
  mis-ordered event priority produced identical numbers. Found only because the oracle's author
  mutated the app and watched the list stay green. Rule: **every entry in a scenario list should
  name the mutation it is the only one to catch** — an entry that names none is decoration.

- **2026-08-11 — put the private-data prohibition in EVERY subagent brief, not only the one that
  obviously touches it.** One brief carried an explicit 🔒 rule for `sample-trade-data/` /
  `broker-statements/` / `docs/human_noted/`; a sibling brief said "you may READ anything". The
  sibling's agent read the real broker export and wrote a 245-line derived-statistics document
  into `docs/spec/`. No ticker, amount or position leaked and nothing reached version control —
  but the boundary was crossed by omission, not by disagreement. A prohibition that appears in
  one of N parallel briefs is not a control; it is a coin flip on which agent picks up the task.

- **2026-08-11 — "the baseline run was green" is weak evidence against an intermittent
  failure, and I used it as if it were strong.** Chasing an e2e failure that hit **2 of 135**
  browser tests per run — a different pair each time, always "every static asset on the page
  404s" — I cited the previous full run (2,994 passed, 0 failed) as evidence the session's
  own changes had introduced it. Do the arithmetic before leaning on that: at ~1.5% per test,
  `P(zero failures across 135) = 0.985^135 ≈ 13%`. A clean run was **quite likely even if the
  flake already existed**, so the baseline established almost nothing. Rule: before treating
  "it passed last time" as a bisect result, compute the probability that it would have passed
  anyway — for a low-rate intermittent, one green run is a coin flip, not a control.
