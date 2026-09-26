# Rule: Architecture & Module Boundaries

Monolith, but internally layered. Boundaries are enforced by discipline (and mypy),
not by separate processes.

## Dependency direction (one-way — never violate)

```
L6  api/           FastAPI JSON routers + *_service modules — the top layer and the composition
                   root: binds every injection seam (register_* in api/app.py, cash_pool_fn,
                   split_factor_fn via scheduler). Imports everything below. Serves web/ (static
                   vanilla JS — not a Python package: nothing imports it, it computes nothing).
L5  scheduler/   ──► pricing · llm_insight · strategy · ops   (triggers only)
                 ──► data_ingestion   (ONE authorised import — see below)
    export/      ──► portfolio · forex · pricing · strategy · llm_insight · data_ingestion.store
    news/        ──► llm_insight.official_templates (seeded prompt text) · shared
    bootstrap.py ──► data_ingestion.schema · shared   (first-run composition root)
L4  strategy/    ──► portfolio · data_ingestion (fees · rules_binding · store · fx_lookup)
    llm_insight/ ──► portfolio   (reads computed results only)
L3  portfolio/   ──► pricing · forex · data_ingestion (store · dividend_model)   ← calculation core
    forex/       ──► shared only
L2  data_ingestion/ ──► shared
                    ──► portfolio (FOUR authorised imports) · llm_insight (ONE authorised import)
L1  pricing/     ──► shared only   (D17 — the split ratio is INJECTED, never fetched)
    ops/         ──► shared only   (backup · notify · digest)
L0  shared/      ──► nothing internal
```

Every edge points DOWN this ladder except the two `data_ingestion` edges marked *authorised*,
which point UP and are guarded by `tests/architecture/test_layer_edges.py`. The scheduler's
single `data_ingestion` import points down but is allowlisted all the same, because scheduler
is a trigger layer that otherwise holds no ledger knowledge (D39). *(Redrawn 2026-09-10 from
the verified import graph. The original 2026-06-05 diagram named a `web_ui/` that decision (B)
of 2026-06-13 replaced with `api/` + `web/`, and it never gained `forex/`, `export/`, `news/`,
`ops/` or the `scheduler → strategy / ops` edges.)*

- `shared/` depends on nothing internal. Everything may import it.
- **`scheduler → data_ingestion`: exactly one import is authorised** (owner sign-off 2026-08-10,
  corporate-actions D39). `scheduler/jobs.py` may import `list_corporate_actions` from
  `data_ingestion/store.py`, and nothing else, so a scheduled price refresh can build the
  split-factor callable that `pricing/` needs but may not fetch for itself (D17: `pricing/` must
  not import anything above `shared/`, so the ratio is **injected**). The guard in
  `tests/scheduler/test_ingest_jobs.py` was **narrowed, not deleted** — any other `data_ingestion`
  import in `scheduler/jobs.py` still fails, and a second test asserts the allowlisted line is
  actually present, because an exception nobody uses is an exception nobody notices.
  Alternatives rejected: injecting from `api/app.py` (respects the diagram, but a missed
  registration degrades to a **silently wrong** price basis); a lookup in `shared/` (every edge
  legal, but a second SQL site for `corporate_actions` — the duplication `shared/ledger_registry.py`
  exists to remove). This edge is recorded here rather than left implicit in code: an edge that
  exists in the codebase but not in this diagram is the next audit finding.
- **A cross-layer read of another module's TABLE is done by direct SQL on the shared connection,
  not by an import** (established convention, written down 2026-08-11). `pricing/ingest.py`
  (`tw_universe`, `all_universe`) reads `instruments` — a `data_ingestion` table — straight from
  SQL and says so in its docstring; `data_ingestion/validate.py::_has_prices` reads `prices` the
  same way for E23/N3-price. What the layering constrains is the **import graph**: an import
  couples module *code* and creates a cycle risk, while both modules already hold one connection
  to one SQLite file. Two obligations come with it, because a direct SELECT is still a real
  dependency that no import guard can see: **(1)** name the borrowed table in the reading
  function's docstring, so the coupling is greppable; **(2)** degrade if the table is absent —
  `bootstrap_db` does not create `prices` (only `pricing.schema.create_tables` does), so a
  ledger-only database must read as "no rows", never as `OperationalError`.
- **A cross-layer CALL into another module's CALCULATION is done by INJECTION, not by an import**
  (established by D17, generalised 2026-08-12). The table convention above covers reading *rows*;
  this one covers reusing a *computation* that must keep exactly one owner. Instances:
  `scheduler/jobs.py::split_factor_fn` hands `pricing/` the split ratio it may not fetch for itself
  (D17); `api/routers/cash.py::cash_pool_fn` hands `data_ingestion/validate.py::
  validate_cash_movement` the cash-pool balance + date-ordered running minimum that live in
  `portfolio/cash.py`, so the CSV import door and the manual form run the **same** withdraw guard
  (audit C3); and `api/routers/input_center.py::import_batch_delete` binds BOTH required seams of
  `data_ingestion/provenance.py::delete_batch` — `delete_actions` (`ActionSetDeleter`, I-3
  2026-09-23: the 公司行動 tab's own delete with its linked fee / band / seed side effects) and
  `guard` (`BatchUndoGuard`, DEF-049 2026-09-25: `api/replay_guard.py`'s orphan / oversell /
  negative-cash decision), so undoing an import batch runs the same decision and writes the same
  `ledger_audit` rows as deleting each row by hand (recorded here, 2026-09-25, when the second
  seam was added — the first had been in the code since I-3 without an entry). The binder is always the layer already above **both**, and it binds **ONCE** per
  request/import, never per row. Three obligations: **(1)** the injected parameter has **no
  default** — D39 rejected injection from `api/app.py` because a *missed* registration degrades
  silently, and a **required** argument is exactly the difference: forgetting it is a mypy error
  and a `TypeError`, not a quiet loss of the guard; **(2)** the callable takes no connection — it
  closes over one ledger snapshot, so an N-row import pays for the reads once; **(3)** state the
  rejected alternatives at the seam, because the next reader's first instinct is the direct import.
  Rejected for the cash guard: **direct SQL** (a pool balance is not a row — it is movements ± FX
  legs ± trade settlements + cash dividends with a credits-before-debits ordering rule, so
  re-deriving it makes `data_ingestion` a second owner of the balance definition) and **leaving the
  guard in `api/`** (legal, but then the bulk door ships a weaker guard than the single-row form).
- **`data_ingestion → portfolio`: exactly four imports are authorised** (recorded 2026-08-12, in
  D39's form, after the edge was found undocumented). `data_ingestion/validate.py` and
  `data_ingestion/corporate_action_import.py` may import `portfolio.cost_basis.build_book` and
  `portfolio.results.Book`, and nothing else, so the four corporate-action rejections (E3 / E22 /
  E5 / E18) can read a **replayed** `Book` — re-deriving the replay inside `data_ingestion` would
  make it a second owner of the ledger replay. The edge was added by the corporate-actions W2/W4
  packages and reached 2026-08-12 with no diagram entry and no guard, which is the F-01 class this
  file's own preamble warns about; it surfaced only because the cash-movement importer's author
  declined to copy it.
  ⚠ **This edge plus the existing `portfolio → data_ingestion` (`portfolio/dashboard.py`,
  `portfolio/dividends.py`) is a package-level CYCLE.** It is not an *import-time* cycle only
  because `portfolio/cost_basis.py` and `portfolio/results.py` import nothing above `shared/`.
  Give either of them a `data_ingestion` import and the app fails to boot on a circular import.
  `tests/architecture/test_layer_edges.py` holds all three assertions: the allowlist, that the
  allowlisted imports are **actually present** (D39 — an exception nobody uses is an exception
  nobody notices), and — the load-bearing one — that those two leaf modules stay leaves.
  **Prefer injection for any new such need**; this entry authorises what exists, not a pattern.
- **`data_ingestion → llm_insight`: exactly one import is authorised** (recorded 2026-09-10, in
  D39's form, after the 2026-09-09 site architecture map found it undocumented and unguarded —
  its finding D-01). `data_ingestion/agents.py` may import `AI_INPUT_PROMPT_BODY` from
  `llm_insight/official_templates.py`, and nothing else. The edge exists because of FU-D20
  (2026-07-17): every shipped prompt has ONE home in `official_templates.py`, and FU-D30's
  registry test (`tests/llm_insight/test_prompt_registry.py`) traces every LLM call site back to
  it — the AI door is a call site, so it reads its code-owned prompt from there rather than
  owning a second copy. It had been in the code since FU-D20 with no diagram entry and no guard.
  ⚠ **This edge closes a SECOND package-level cycle:** `llm_insight → portfolio →
  data_ingestion → llm_insight`. It is not an import-time cycle only because
  `llm_insight/official_templates.py` and `llm_insight/__init__.py` import nothing above
  `shared/` — `official_templates.py` may not even import an `llm_insight` sibling, since a
  sibling reaches `portfolio` and the loop closes at boot. `tests/architecture/test_layer_edges.py`
  guards this edge exactly like the one above: allowlist, presence, and the two new leaves.
  Alternatives rejected: **injecting the prompt from the AI-door router** (`api/routers/
  input_center.py` is above both, but a prompt body is a constant, not a computation with an
  ownership problem — the injection convention exists for the latter — and 33 call sites would
  carry a string for no structural gain); **moving the constant to `shared/`** (breaks the
  one-home rule the registry test enforces, and `shared/` would then own LLM-facing text);
  **duplicating the text in `agents.py`** (FU-D20 existed to end exactly that drift).
- **Nothing imports `api/`** — it is the top of the ladder — and `web/` is not importable at
  all. Lower layers never reach up except through the guarded edges above.
- The web layer **reads** computed results. It does not compute. No cost-basis or
  return math in routers, and none in `web/` JS: the frontend receives every number as a
  Decimal **string** and only formats it (`tests/contract/test_frontend_never_computes_money.py`).
- `llm_insight` consumes the portfolio's *computed* numbers; it does not recompute
  them and does not produce numbers of its own (see `data-and-pricing.md` and
  `llm-insight.md`).

## Module responsibilities

- **shared/** — settings (env-driven), DB session/connection, Pydantic models used
  across layers, `Decimal`/currency helpers, FX-conversion helper, the cash-kind and
  corporate-action vocabularies, the alert-rule display names (`alert_rule_names.py`,
  DEF-062 2026-09-25: the ONE zh name per rule id, read by the API wire, push text, digest,
  gate messages and run details — a renderer never falls back to the id; and every alert
  TITLE names its rule in the rule's own words, DEF-076 2026-09-26: `tests/contract/
  test_def076_alert_titles_use_rule_vocabulary.py` derives the (rule, title) pairs from the
  code, and a different wording must be a reviewed entry with its reason), the LLM client.
  Pure, importable everywhere.
- **data_ingestion/** — manual transaction entry + CSV/broker/AI import, dividends, cash
  movements, corporate actions. Validates and normalizes into the canonical ledger models
  before persisting (`store.py` owns the ledger tables and their DDL). Rejects bad input
  loudly; never silently coerces.
- **pricing/** — fetch quotes + FX from finance APIs into SQLite via idempotent
  upserts. Owns the refresh cadence and the benchmark registry. The only module allowed to
  write price/FX rows.
- **portfolio/** — the calculation core: cost basis (weighted average per holding),
  realized & unrealized P&L, return rates, TWR and the net-worth series, sector allocation,
  the reporting-currency combined view, the split-basis read path. Pure functions over
  inputs where possible → trivially unit-testable.
- **forex/** — the FX-conversion ledger's cost basis: per-account foreign-cash pools,
  weighted-average acquisition rate, realized / unrealized FX P&L (`domain-ledger.md` Q12).
- **strategy/** — user-defined strategy logic as parameterized Python modules
  (condition params → signal/score), the rules engine + alerts, what-if, rebalance. Pure
  and pytest-tested. **Not** a user-facing rule builder (see below).
- **llm_insight/** — LiteLLM orchestration. Assembles portfolio summary + fetched
  qualitative info → structured insight cards (Pydantic schema) → cache → render. Also the
  one home of every shipped prompt (`official_templates.py`, FU-D20 / FU-D30).
- **news/** — the news library: fetch, store, and the LLM organizer prompt's seed.
- **export/** — report and CSV builders (holdings, ledgers, tax package, cash statement,
  print reports). Read-only over the calculation core; no money math of their own.
- **ops/** — backup, notification dispatch, digests. Talk to the filesystem and channels,
  never to the ledger.
- **api/** — FastAPI JSON API: routers call the core and serialize (Decimal → string), the
  `*_service` modules orchestrate cross-module flows, `app.py` is the composition root that
  binds the injection seams. No business logic; every mutating route has an action-log label.
  **A GET never writes** (DEF-065, 2026-09-25): no seed, no `CREATE`/`ALTER`, no write
  transaction — every table a read path touches is created (and seeded) at boot in
  `app.py`'s lifespan, and `shared/config_store.ensure_seeded` decides seed-once under the
  write lock. A read that wrote raced its twin (two first reads of `/api/ui-prefs` collided on
  `settings_meta` → 500) or queued behind any long write for the 5 s busy timeout.
  `tests/contract/test_def065_get_never_writes.py` boots a fresh database and calls every GET
  route with other connections holding the write lock. (`CREATE TABLE IF NOT EXISTS` on a
  table that exists is resolved at prepare time and takes no lock — the test proves it.)
- **web/** — the static vanilla-JS frontend (HTML/CSS/JS + vendored `echarts.min.js`), served
  by `api/` via `StaticFiles`. Not a Python package. Single fetch layer `web/api.js`.
  **Every secret input is `type="password"` + `autocomplete="new-password"`** (DEF-078,
  2026-09-26), and a stored secret is shown only as the server's `mask_secret` — first 3 +
  ••• + last 3 for EVERY kind of secret, API keys included (owner ruling 2026-09-26) — as
  text beside the field, never pre-filled as a readable value. The secret-input set is
  derived by scan in `tests/contract/test_def078_secret_inputs_and_masks.py`.
  **A state colour is a class with a stylesheet rule, never an inline style** (M10-02,
  generalised by DEF-077 R7, 2026-09-26): an inline colour cannot remove what the element's
  class paints by default (`.fresh-note` is amber, so 「（一致）」 stayed amber), and a branch
  that sets one without every other branch clearing it hands its colour to the next render.
  The few safe inline sites are reviewed entries in
  `tests/contract/test_def077_state_colour_is_a_class.py`; a colour a user sees is tested with
  `getComputedStyle`, never `element.style`.
- **scheduler/** — APScheduler job definitions only. Triggers pricing refresh, insight,
  alert, digest and backup runs. Holds no business logic itself. **A job that can finish
  without raising yet lose part of its work returns a `JobOutcome`** (DEF-067, 2026-09-26):
  every key lost → `error` (失敗), some → `partial` (部分), none → `ok`
  (`scheduler.jobs.sweep_outcome`) — the quote jobs included (owner, 2026-09-26: all lost is
  失敗; a held symbol lost is 部分). A bare string is reserved for a job whose only failure
  mode is an exception, because `_outcome_of` records it as `ok`: `dividends_daily` returned
  「0 檔事件已更新，10 檔失敗」 as 成功. A provider that ANSWERED with nothing (no dividend
  records, no bars in a holiday window) is not a lost key — but `history_daily` trusts an
  empty answer (`Registry.fetch_quote_history_explained`) only when the same run proves a
  provider of that market answered, because yfinance reports a network failure as an empty
  frame, not an exception; the multi-year backfills keep empty as failed. Quote jobs: every
  instrument of the market lost → `error`; a held one lost → `partial`; a watchlist-only or
  FX-only loss → `ok`. **`job_runs.detail` is always a
  zh sentence with full-width punctuation** (DEF-073): the only ASCII words allowed are
  identifiers (tickers / pairs, provider ids, exception class names, file names), and the
  machine enum belongs in `job_runs.reason`. `tests/contract/
  test_def073_job_details_speak_chinese.py` runs every job on its success and failure branch
  and scans what it wrote — the full-width punctuation guard could not see an all-English
  sentence, because there was no Chinese for a half-width mark to touch.
- **bootstrap.py** — first-run composition root: creates the ledger schema and seeds config.

## "Strategy logic self-defined" — current form

Implement strategies as **Python modules/functions** authored by Claude Code from
the human's spec — parameterized conditions returning signals or scores. Do **not**
build a user-facing rule-builder / DSL now. That is the smallest error surface and
the easiest to test with pytest. Upgrade to a config-file or DSL **only** if a
non-engineer user later needs to edit rules themselves — and record that decision
first.

## Testability is a design constraint

Because implementation is AI-driven, every module must be self-verifiable:
- `portfolio/` and `strategy/` → pure-function unit tests with fixed fixtures.
- `pricing/` and `llm_insight/` → mock the external API/LLM; test parsing,
  upsert idempotency, cache behavior, and graceful degradation.
- `api/` → httpx `TestClient` **JSON-contract** tests in `tests/contract` (shape, status codes,
  zh messages) plus the spec-17 golden payload (`tests/golden/dashboard_full.json`).
- `web/` → Playwright e2e in `tests/e2e` (one pytest session at a time), plus the static
  contract tests (JS parses, globals defined, no native dialogs, no remote scripts, never
  computes money).
- Layer edges themselves → `tests/architecture/` (allowlisted upward imports, leaf modules,
  required injection seams). *(Updated 2026-09-10 — the `web_ui/` bullet described a layer
  decision (B) retired on 2026-06-13.)*
