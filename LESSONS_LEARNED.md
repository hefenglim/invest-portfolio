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
