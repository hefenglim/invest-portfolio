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
