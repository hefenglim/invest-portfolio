# Rule: Architecture & Module Boundaries

Monolith, but internally layered. Boundaries are enforced by discipline (and mypy),
not by separate processes.

## Dependency direction (one-way — never violate)

```
L6  api/           FastAPI JSON routers + *_service modules — the top layer and the composition
                   root: binds every injection seam (every scheduler register_* in
                   api/composition.bind_runtime(), cash_pool_fn, split_factor_fn via scheduler).
                   Imports everything below. Serves web/ (static vanilla JS — not a Python
                   package: nothing imports it, it computes nothing).
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
  **Read once, hand the snapshot to every guard** (CR-12 / CR-13 / R10, 2026-10-04): a door that
  runs more than one cash guard over the same pools loads the pool ledgers once
  (`api/routers/cash.py::load_cash_ledgers`) and passes that snapshot to each — `bind_cash_pool`
  for the hard probe, `_pool_dip` for the ack-able check; the scoped `caused_dip` comparison has
  ONE home, `_pool_dip`, which `api/replay_guard.py` reaches for the batch undo and the
  corporate-action delete. A replay guard reads the stored ledgers once per call
  (`replay_guard._read_ledgers`) and builds its before, its after and any cash snapshot from that
  read; a correction door builds its would-be list from that same read and hands it to its
  validators and to the guard (`_replay_guard` / `action_change_guard` `read=`, S6); a by-id
  store lookup asks `WHERE id=?`, it never lists the table. Measured before: one
  cross-account FX edit issued 27 full-table SELECTs; a batch undo read the trades 3 times.
  `tests/contract/test_cr12_cash_guards_read_the_ledgers_once.py` pins the ceilings. The
  snapshot also carries the replay's dividend refusals (⑪, 2026-10-05): `load_cash_ledgers`
  replays once per snapshot (or takes a guard's own book), so the pool the page shows and the
  pool a withdrawal is judged against exclude the same refused payouts — and `data_ingestion`
  never imports `portfolio` for it (the binder of `cash_pool_fn` does the filtering).
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
- **`scheduler → settlement`: two seams, never an import** (PERF plan P13, 2026-10-18).
  `scheduler/` reaches `api/settlement_service.py` only through
  `jobs.register_settlement_runner` (→ `settlement_service.scheduled_run`: the
  `settlement_nightly` run at 06:40 Taipei and the `settlement_selfcheck` run on Sundays at 04:30)
  and `jobs.register_after_job_hook` (→ `settlement_service.after_job_hook`). The hook is two-phase:
  it is asked before a static job runs — for a job worker child, when the heavy gate grants the
  run its turn (`child_runner`'s `on_start`, `jobs._run_static_in_child`), never before the
  wait — and called once the job's row is final, exactly when the mark was taken (including a
  child that died, overran its limit, or never started after the mark). It starts an
  automatic `dirty` run (reason `job:<id>`) only when the JOB'S OWN trail rows reach before the
  live window. The mark sums every UNSETTLED price / FX counter per key, claimed ones included,
  so a cancelled run's put-back after the mark is not the job's change, and the owner's
  changes during a gate wait are not either (code review of v0.1.32, 2026-10-20). Residual: an
  owner's price / FX change made WHILE the job runs is still indistinguishable (counters carry
  no actor). A direct import would point up the ladder and cycle through `start_job_run` /
  `finish_job_run`. Both seams are bound, with every other scheduler seam, by ONE function,
  `api/composition.py::bind_runtime()`. The lifespan calls it, and so does the job worker child
  (`api/job_worker.py`), so a job runs against the same runners in either process.
  `tests/scheduler/test_jobs_in_child.py::test_bind_runtime_calls_every_register_seam_of_the_scheduler`
  fails when a `register_*` of `scheduler/jobs.py` is not called there or is unbound afterwards.
  Two calls stay in the lifespan because no job reaches them: the live scheduler
  (`settlement_service.bind_scheduler`) and the boot run (`start_after_boot`). The job worker child
  clears the after-job hook right after binding, because the hook belongs to the web process,
  where the settlement service lives (P7) — pinned by
  `tests/scheduler/test_jobs_in_child.py::test_the_worker_runs_its_job_with_the_after_job_hook_cleared`.
- **The settle worker's import limits** (spec 2026-10-18 §9.2). `portfolio/settle_worker.py`
  runs as `python -m portfolio_dash.portfolio.settle_worker --run <id>`. It imports only
  `portfolio/`, `pricing/`, `forex/`, `data_ingestion/` and `shared/`. It never imports `fastapi`,
  `litellm`, `yfinance` or `pandas`, nor any `api` / `scheduler` / `export` / `strategy` / `news`
  / `ops` / `llm_insight` module. `tests/settlement/test_contracts.py::
  test_the_worker_imports_no_web_llm_or_provider_module` inspects `sys.modules`. This is why
  `portfolio/ledger_findings.py` exists: the run's 檢查帳本 phase cannot call the 重算 route,
  which imports `fastapi` and `scheduler.jobs`. The worker writes the settle tables only; the run's
  `job_runs` row is created and finalised by the parent (P7).
- **Borrowed tables of the settlement** (the direct-SQL convention above, both obligations
  met). `portfolio/settlement_store.py::cutover_snapshots` reads `portfolio_snapshots`, a table
  owned by `api/snapshots.py`. This is an UPWARD read, done once, for the go-live restatement only.
  It is absent-safe: a fresh database never creates the table. `api/settlement_service.py` reads
  `job_runs` and `schedule_config` (scheduler-owned) for `final_through`, and reads the settle
  tables for the after-job hook's `trail_mark`. `settlement_store._undo_deletes` reads
  `ledger_audit` (absent-safe) to tell an import batch's undo from hand deletes in a restatement
  reason, spelling the undo's tag with `data_ingestion/store.py::undo_source` (the one spelling,
  re-exported by `provenance`).
- **Reviewed-reader guards list the settlement's readers.** Three contract tests find every reader
  of a figure by scan, and require each one to apply the rule or to be a reviewed entry with its
  reason:
  - `tests/contract/test_ruling11_refused_dividends_move_no_cash.py` (⑪: a refused dividend moves
    no cash);
  - `test_x1_refused_dividends_are_marked_in_every_listing.py` (X1);
  - `test_options_excluded_from_stock_readers.py` (OPT-D10).

  The settlement's readers are entries there, each checked at its call site:
  - `portfolio/period_close.py::cash_at` / `kpi_at`;
  - `portfolio/settle_worker.py::_valuate`, `_plan`, `_load` and `_Run._close`;
  - `portfolio/timeseries.py::_reporting_flows` and `first_unconvertible_flow` (the one owner of
    "which flow cannot convert");
  - `api/snapshots.py::period_detail`.

  Each reads booked dividends only, and each tags positions with `asset_type`. A new settlement
  reader that is not listed fails there.
- The web layer **reads** computed results. It does not compute. No cost-basis or
  return math in routers, and none in `web/` JS: the frontend receives every number as a
  Decimal **string** and only formats it (`tests/contract/test_frontend_never_computes_money.py`).
  The last exception, the trend tooltip's `total_value − net_invested`, was removed 2026-10-22: the
  server sends `TrendPoint.pnl` (`dashboard_models.trend_pnl`: display-quantized, null on an
  incomplete day), the guard scans `TrendPoint` with no known violation left, and the trend card
  formats every figure in the series' `reporting_currency`.
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
  code, and a different wording must be a reviewed entry with its reason), the market exchange
  timezones (`enums.MARKET_TIMEZONE`, the split window's bound), the LLM client.
  Pure, importable everywhere. **The exceptions to "pure"** are the modules that hold
  process-wide state or reach the OS:
  - **`flight.py`** (2026-10-13) lets callers asking for the SAME computation while it RUNS
    share it. Its one user is `result_cache`. It keeps nothing once the computation returns.
    A write moves the write generation through two seams that must stay wired:
    - `api/app.py`'s write window around every request that is not a GET / HEAD / OPTIONS or a
      declared read-only POST;
    - `shared/db.session()`, which commits any session that changed a row inside a window of its
      own.

    Since PERF-S10 a request's own commit runs inside the HTTP window. The session's window
    still covers threads and jobs. A connection holding uncommitted changes always computes
    alone, and each joiner receives its own deep copy. A key built on the generation alone
    (`write_generation()`, `None` while a window is open) must not share during a window;
    `result_cache` keys on `generation()` plus `PRAGMA data_version`, which moves at the commit
    itself, so its readers keep caching and sharing while another request's window is open
    (post-v0.1.32, the code review's backlog item 1). `tests/shared/test_flight.py` +
    `tests/portfolio/test_dashboard_shared_build.py`.
  - **`result_cache.py`** (PERF, 2026-10-18) KEEPS results: "nothing is kept" is no longer true
    of the app, only of `flight`. It holds the latest result per name until the database
    changes, and delegates its in-flight sharing to `flight.coalesce`. The names are
    `dashboard`, `alerts`, `twr:<benchmark>:<window>`, `dividend_refusals` and
    `periods:<kind>`.
    - **The key** is `(name, db path, write generation, PRAGMA data_version, day, reporting,
      extra)`. `data_version` is read on ONE dedicated connection per file. That connection is
      read-only (`connect_path(read_only=True)`: a `mode=ro` URI and no pragma) and in
      autocommit, and it is read BEFORE the computation's read snapshot opens. So any other
      connection's commit moves the version: another request, a job or settle child, a script.
      A commit during the computation only makes the entry unreachable — except for a
      computation that opens its own read snapshot after a slow step (the closes of
      `api/snapshots.py`, after the dashboard build): it opens it with `marked_snapshot` and
      passes `mark=`, and when no commit, window edge or own write happened since that snapshot
      opened, the entry is filed under the CURRENT key instead of the one taken before the
      computation. The dashboard build those closes redo, uncached, inside their snapshot
      (review G #8) is filed for the next `/api/dashboard` by the same rule, once the snapshot
      has closed (`result_cache.file_marked` via `dashboard.file_snapshot_build`, backlog
      item 4), and not at all otherwise. `day` is `now.date()`, so no entry outlives its day.
    - **Scope.** It caches the app's OWN database only (`get_settings().db_path`). Any other file
      computes uncached and gets no version connection. Nothing is cached inside a transaction or
      for a database with no file. Another request's write window does not stop caching or
      sharing: its commit moves `data_version` and its edges move the generation, so nothing
      cached before or during it is served after it.
    - **Copies.** Every reader receives a deep copy. The caller then re-stamps the fields that
      depend on `now` beyond its date: `portfolio/dashboard.py::SUB_DAY_FIELDS`, today `as_of`.
      A miss runs inside ONE read transaction (PERF-S12).
    - **Pre-warm** (`PD_PREWARM=1`): a debounced daemon timer computes today's dashboard and
      alerts (`api/alert_inputs.prewarm_today`). It fires 3 s after the last write window closes
      (`api/app.py::WRITE_PREWARM_DELAY_S`), 1 s after a settlement run ends, and at once after
      a recorded session touch, which itself waited for the page load to settle (`api/` →
      the session touch).
    - **Shutdown.** The lifespan calls `result_cache.shutdown()` once it has cancelled the
      pending session touches (`auth_store.cancel_pending_touches`) and stopped every child
      process it spawned (`child_runner.stop_all`, below). It closes the cache for the rest of
      the lifespan (no pre-warm starts, no version connection opens, no entry is kept), waits
      up to `SHUTDOWN_PREWARM_WAIT_S` (15 s) for a pre-warm already running — whose read
      snapshot would keep a TRUNCATE checkpoint from finishing and which would reopen a version
      connection — and closes the version connections. THEN the lifespan runs
      `PRAGMA wal_checkpoint(TRUNCATE)` (`api/app.py::checkpoint_on_stop`, result on
      `app.state.stop_checkpoint`). A read-only connection cannot checkpoint, and a clean stop
      must leave the database in one file. A pre-warm that outlives the wait (a full live build
      while the store is unusable) and a busy checkpoint are logged and the stop goes on;
      `-wal` / `-shm` may then remain, and the `-wal` holds committed changes. The lifespan
      calls `result_cache.reset()` at START, so a second lifespan in one process caches again.

    `tests/shared/test_result_cache*.py`.
  - **`flags.py`** reads the five `PD_*` process flags from `os.environ` on every call, never
    cached: `PD_SETTLE`, `PD_SETTLE_MODE`, `PD_JOB_MODE`, `PD_PREWARM` and `PD_STATIC_IMMUTABLE`
    (`data-and-pricing.md` → the settlement store). Unset means the prod default; any other value
    outside the vocabulary raises. The lifespan validates all five at boot (`flags.current()`).
    It imports nothing internal.
  - **`proc_lock.py`** holds cross-process exclusive locks on files in the data folder:
    `fcntl.flock` on POSIX, never `lockf`, and `msvcrt.locking` on Windows. The locks signal
    liveness, never data, and the OS frees a lock the moment its holder dies. The lock files:
    - `settlement.lock` — the settle child, for its whole life;
    - `heavy-child.lock` — held by `child_runner`'s gate while a heavy child lives;
    - `job-<run_id>.lock` — a job child.

    A probe is a brief acquisition. So a holder that a status read may probe takes its lock with
    `lock(path, timeout)`, never `try_lock`.
  - **`child_runner.py`** is the ONE way to run a Python child. `spawn` runs
    `sys.executable -m <module>`, and a waiter thread calls `on_exit` exactly once.
    - **The heavy gate.** `heavy=True` queues: one heavy child at a time, in FIFO order, across
      the settle child and every job child. While the child lives the gate holds
      `heavy-child.lock`, so two processes sharing a data folder never run two at once either.
    - **The child runs this checkout's code.** `cwd` and the front of `PYTHONPATH` are the
      package's own root, and `DB_PATH` is made absolute. It never uses `-I`, which would drop
      both.
    - `terminate_and_wait` returns once the process is gone AND `on_exit` has returned.
    - **`stop_all`** is the app's stop (backlog item 2, 2026-10-22). The lifespan calls it after
      `settlement_service.shutdown()` and the scheduler's, before the result cache's shutdown and
      the WAL checkpoint, with ONE bound, `api/app.py::STOP_CHILDREN_WAIT_S` (30 s, which fits
      under systemd's default 90 s stop timeout with the 15 s pre-warm wait). It cancels every
      live child of the runner first — so none that is queued starts as the gate passes the turn
      on — terminates the running ones (killed `STOP_KILL_AFTER_S` later if still alive), and
      waits until each is gone and its `on_exit` has returned (the runner keeps every child
      until then). Each result carries `stopped`, and its owner records the run as a restart
      leaves it: a settle run 中斷 (「系統停止時結算尚未完成」, which boot resumes), a job row
      `error` 「系統停止時此工作尚未完成」 with reason `interrupted`. A child left running held the
      database through the checkpoint (its `-wal` / `-shm` stayed), and outside systemd's
      `KillMode=control-group` (a dev server's Ctrl+C) it went on running after the app stopped.
      From the call on, a child spawned anyway (a job thread already past the scheduler's
      shutdown) never starts its process and is recorded `stopped`; the lifespan's start
      reopens the runner (`child_runner.reopen`).
    - **`on_start`** (optional) runs on the waiter thread once the child's turn has come (after
      the heavy gate, for a heavy child) and before its process starts, unless the child was
      cancelled by then. A cancel landing during or right after it still keeps the process from
      starting, so a caller pairs it with `on_exit`, which fires either way. A raise is logged
      and the child still starts.

    The runner knows nothing about settlement or jobs.
  - **`db.py`'s change actor.** `install_actor` registers a connection-local function and a
    TEMP trigger, and every settlement trail row that connection writes is stamped with the
    actor. It is installed on mutating-request, job and boot connections only, never on a GET
    (DEF-065).
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
  inputs where possible → trivially unit-testable. **The settlement store** (PERF, spec
  `docs/spec/2026-10-18-settlement-store.md`; `data-and-pricing.md` → the settlement store) is
  four modules here:
  - **`settlement_store.py`** owns every settle table. It holds the DDL and the documented reset
    (`drop_store` drops the triggers FIRST), the trigger set, the change trail and the run's
    claim on it, the run reports, the generations and the swap, and the read helpers
    (`store_state`, `live_from`, `stale_range`, `settled_prefix`). It reads the borrowed ledgers
    by direct SQL.
  - **`settle_worker.py`** is one settlement run, in a child (`PD_SETTLE_MODE=child`) or in
    process (`inline`: synchronous, never on a request thread). It runs the import limits above.
    It values every day from ONE `iter_books` pass, and every close with the dashboard's own
    `kpis_as_of`.
  - **`period_close.py`** holds the month and year closes as pure functions
    (`domain-ledger.md` → month and year closes). It is also the ONE home of the tax sheets' row
    selection (`realized_rows`, `post_close_dividend_rows`, `dividend_rows`, `fx_realized_rows`),
    which `export/tax.py` and a close's activity both read, so the two tie out by construction.
  - **`ledger_findings.py`** is the run's 檢查帳本 phase. It runs the dashboard's TOLERANT
    replay and lists every 待釐清 record it keeps, each dated, in non-blocking wording. Findings
    never stop a run.

  `cost_basis.py` and `results.py` stay leaves.
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
  `*_service` modules orchestrate cross-module flows, `app.py` is the composition root (its
  lifespan, its middlewares) and `composition.py::bind_runtime()` binds every scheduler seam.
  No business logic; every mutating route has an action-log label.
  `instrument_service.restate_price_basis` is the ONE price-basis restatement seam: every
  corporate-action door and `scripts/check_split_window.py` call it, and it owns the W6 signal
  invalidation (`data-and-pricing.md` → the stored price basis).
  **A GET never writes** (DEF-065, 2026-09-25): no seed, no `CREATE`/`ALTER`, no write
  transaction — every table a read path touches is created (and seeded) at boot in
  `app.py`'s lifespan, and `shared/config_store.ensure_seeded` decides seed-once under the
  write lock. A read that wrote raced its twin (two first reads of `/api/ui-prefs` collided on
  `settings_meta` → 500) or queued behind any long write for the 5 s busy timeout.
  `tests/contract/test_def065_get_never_writes.py` boots a fresh database and calls every GET
  route with other connections holding the write lock. (`CREATE TABLE IF NOT EXISTS` on a
  table that exists is resolved at prepare time and takes no lock — the test proves it.)
  The settlement store is no exception. A GET never writes it, never creates or recreates a
  trigger, and never gets the change actor's TEMP trigger. A status read never starts,
  resumes or ends a run. The store's triggers are installed and verified at boot only.
  **The one reviewed exception: the session touch** (owner ruling 2026-10-22, review L5). Login
  sessions expire after `auth_store.SESSION_IDLE_EXPIRY` (30 d) without use. `require_session`
  only reads: an expired (idle > 30 d, a stamp more than `SESSION_CLOCK_SKEW` in the future, or an
  unreadable stamp), locked or unknown session is refused, and the login it lets through goes on
  `request.state` (`LOGIN_STATE_ATTR`) for the action log. A use is recorded by
  `auth_store.run_touch`, never by the gate itself. When the stored `last_seen_at` is
  `SESSION_TOUCH_INTERVAL` (24 h) old and the in-process guard has no touch of that session in
  flight or recorded within the interval, the gate ARMS the session's pending touch
  (`schedule_touch`; owner ruling 2026-10-22, 「續期延後，記完立刻預熱」). It is a daemon timer, one
  per session, that fires once the page load has settled: `SESSION_TOUCH_SETTLE_S` (3 s, equal to
  `WRITE_PREWARM_DELAY_S`) after the session's LATEST gated request, each request pushing it back,
  and never later than `SESSION_TOUCH_MAX_DEFER_S` (30 s) after the request that armed it. The
  touch runs on its own connection: a 100 ms busy timeout, one autocommit compare-and-set UPDATE
  (`TOUCH_SQL`), skipped when the lock is busy (the next request arms it again), a WARNING (and a
  10 min back-off) on any other failure. A recorded touch moves `data_version`, so it schedules
  `alert_inputs.prewarm_today` AT ONCE (delay 0, the write window's debounced timer). Until the
  touch commits, the page load and a navigation inside the settle window still hit the previous
  cache entry; a navigation that lands while the pre-warm runs joins it in flight, since the
  pre-warm and `GET /api/dashboard` build under one cache key. The owner chose 24 h + pre-warm
  over 6 h for the cache's sake; a session lapses 29–30 days after its last use. The lifespan's
  stop cancels the pending touches (`cancel_pending_touches`, before `result_cache.shutdown()`;
  it waits up to 2 s for one already running). A cancelled use is recorded on the session's first
  request after the boot, since its stamp did not move. The request's own connection still writes
  nothing and takes no write lock: `tests/contract/test_def065_get_never_writes.py` section 5
  holds the touch to its statement, its own connection and once per interval. `/api/auth/logout`
  is an open path, so an expired cookie can be cleared. The boot (`prepare_sessions_at_boot`)
  dates unstamped sessions from the boot and purges expired, locked and orphaned ones.
  **`get_conn` is function-scoped** (PERF-S10, 2026-10-18). Every route declares
  `Depends(get_conn, scope="function")`, and so does the gate, `auth_store.require_session`. A
  request therefore holds ONE cached connection, and its session commits when the path function
  returns, BEFORE the response starts. A client that received the answer reads its own write on
  the next request, and a failed commit or teardown answers 500 instead of a silent 200.
  `tests/contract/test_s10_conn_scope.py` scans every site. `pyproject.toml` floors FastAPI at
  `>=0.121.1`, the first version with `Depends(scope=)`, so no deploy installs one that ignores
  it. On a mutating request the connection also carries the change actor (`user:<login>`, or
  `user:guest` in guest mode).
  **The read-only POSTs** (`api/read_only.py::READ_ONLY_POSTS`, plan P2) are `POST /api/whatif`,
  `/api/rebalance/preview` and `/api/prompts/preview`. They POST only because their input does
  not fit a query string, and they write nothing, not even an action-log row. `is_read_only()`
  treats them exactly like a GET / HEAD / OPTIONS:
  - the write-window middleware skips them, so they move no write generation and leave every
    cached entry reachable;
  - `get_conn` installs no actor on them;
  - `action_log` excludes them.

  `web/api.js` mirrors the list in its own `READ_ONLY_POSTS`, so they dispatch no
  `pd-mutated`. `tests/contract/test_read_only_posts.py` runs each one under a held write lock
  with the DEF-065 trace, and `tests/contract/test_perf_web_transport.py` holds the two lists
  equal. `POST /api/insight-tasks/{id}/preflight` is deliberately NOT one: it writes an
  action-log row.
  **The settlement modules:**
  - `settlement_service.py` is the PARENT of every run. It computes `final_through` and the
    run request from `job_runs` and the schedule (P12). It writes the run's `job_runs` row and
    its `preparing` report in ONE commit, then spawns the worker. Its waiter finalises what the
    child did not, sends the 「重新結算失敗」 push of a failed automatic run (P4; held through quiet
    hours and sent once they end, Q-P1 = (a)), calls `flight.note_write()`, pre-warms, and
    starts the follow-up (chained, resume or queued). `shutdown()` stops every new start:
    `start_run` raises `Stopping`, `start_automatic` starts nothing, and a waiter finishing
    during the stop leaves its follow-up to the next boot (`on_boot` opens it again) and
    pre-warms nothing. The lifespan then terminates a running child (`child_runner.stop_all`):
    its waiter ends the run 中斷 「系統停止時結算尚未完成」, with no failure push, and boot resumes
    its range. It also owns cancel, the boot hook
    (`on_boot` plans inside the boot session; `start_after_boot` starts once that session has
    committed) and the chip's `status`, which only reads. **Every settlement read is one
    snapshot** (2026-10-22, found by the stress audit): `status` / `runs_page` / `run_report` /
    `diff_rows` / `changes_page` read inside `result_cache.read_snapshot` (a deferred BEGIN — a
    read transaction no writer blocks, so DEF-065 holds). Built from autocommit SELECTs, a status
    mixed a swap into its answer (the old generation with the run's claimed day as a stale range
    and no run active — a state the database never holds). The settle lock is outside the
    database: `status` probes it before the snapshot opens and after its reads, and a `running`
    report reads 已中斷 only when both probes find it free
    (`tests/settlement/test_service_status_snapshot.py`).
  - `snapshots.py` serves 月度成績 and the period routes from ONE rule: a past close whose end
    day is before the build's `live_from` comes from the store, every other one is computed live
    with the same `period_close` functions. It no longer writes `portfolio_snapshots`.
  - `composition.py` holds `bind_runtime()`, described above.
  - `job_worker.py` is the job worker child (see **scheduler/**).
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
  **An acknowledgement of server warnings echoes the server's per-issue `ack_key`, never the
  displayed text** (CR-07 / R1, 2026-10-04): `api.js` rewrites every `{account:<id>}` token in
  every response, so the text the browser holds is not the text the server wrote, and a
  text-bound ack of an account-naming warning could never match. The corporate-action doors
  send `acked_keys` (one helper, `window.pdActionAckKeys`), reset the tick whenever the warnings
  change, and the server refuses `warnings_changed` when its recomputed warnings are not all
  acknowledged.
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
  **Job worker children** (PERF-D4, spec 2026-10-18 §16). With `PD_JOB_MODE=child` and the
  worker module registered (`register_job_worker`; `scheduler/` never names an `api` module),
  every scheduled job runs in `python -m portfolio_dash.api.job_worker <job_id> <run_id> --now
  <iso> --trigger schedule|manual`. That covers its cron fire and a manual 立即執行 of it. The
  run uses the row the parent inserted and committed, and goes through the body of
  `run_job_outcome`, so `job_runs`, `usage_tally` and `JobOutcome` are the in-process run's.
  Every job child is HEAVY: it waits its turn at `child_runner`'s gate, behind the settle child
  and every other job, and the 排程中心 shows it as 已排入 (`QUEUED_BEHIND_GATE`) while it
  waits. Inside the child:
  - nested dispatch stays in-process (`PD_JOB_MODE=inline`);
  - it holds `job-<run_id>.lock` for its whole life;
  - its progress goes to a `job-<run_id>.progress.json` sidecar, which the web process merges
    into `running_progress()`;
  - its log records are APPENDED to `app.log`, never rotated.

  **What stays in the web process** (plan P7):
  - the settlement jobs (`WEB_PROCESS_JOB_IDS` = `settlement_nightly`, `settlement_selfcheck`).
    They only start a settle child, and a job child would hold the heavy gate while waiting
    for it. The settle child itself is spawned by the service, never by a job child;
  - the interactive doors (PERF-S13): the refresh-quotes door, the insight task door, the AI
    door and the news door;
  - the after-job hook. It is asked before the spawn and called after the child's row is final.

  **Limits and clean-up:**
  - **`JOB_TIME_LIMIT_S` = 60 minutes** (owner ruling 2026-10-20, 「時間上限最多 60 分鐘」). It is
    counted from the child's process start; the gate wait does not count. Past it the child is
    terminated, with detail 「工作執行逾時（超過 60 分鐘），已中止」 and reason `timeout`. The
    settle child has no limit.
  - A child that dies without finishing its row is finalised by the parent as
    「工作子程序異常結束」.
  - A child the app's stop ended (`child_runner.stop_all`: terminated, or cancelled while it
    waited for the gate) is finalised by the parent as `error` 「系統停止時此工作尚未完成」, reason
    `interrupted` — the reason a restart gives.
  - At boot, `close_abandoned_runs` closes every unfinished non-`settlement` `job_runs` row whose
    lock nobody holds, as `error` 「系統重新啟動時此工作尚未完成」, reason `interrupted`. The first
    boot of v0.1.32 closes the rows left stuck by older versions this way. Settlement rows are
    the service's: `on_boot` ends them with their reports.

  **`RETIRED_JOB_IDS`** = {`snapshot_monthly`} (plan P11). `build_scheduler`, the 排程 listing,
  `dispatch_job` and the child dispatcher skip EXACTLY these ids. Skipping "anything not in
  `JOBS`" would also drop the dynamic `insight:<id>` rows. `ensure_job_rows` never deletes, so
  the retired 23:50 月度快照 keeps its `schedule_config` row and its readable history.
- **bootstrap.py** — first-run composition root: creates the ledger schema and seeds config.

**US equity options (2026-10-09, spec `docs/spec/2026-10-09-options-ledger.md`).**
`shared/options.py` imports NOTHING internal — not even another `shared` module — so every layer
reaches the OCC symbol, the multiplier, the standard-adjustment check, the §6.1 vocabulary and
the ONE side classifier (`classify_option_rows`) without an edge (`tests/shared/test_options.py`
guards it). `portfolio/options.py` is calc core (last-trade valuation, the `options` block);
`portfolio/cost_basis.py` and `portfolio/results.py` stay leaves. The expiry inbox detector lives
in `api/` because it reads the BOOK. The Schwab converter stays pure: the ledger's open option
positions reach it as a REQUIRED `known_positions` argument bound by
`api/routers/broker_import.py` (the injection convention above; the CLI passes `{}`).

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
- The settlement store → `tests/settlement/` and `tests/reference/` (added 2026-10-20):
  - the service with the REAL worker, inline and in a child, including a kill in a phase;
  - every input column of every trigger table;
  - the prefix property on both engines;
  - byte identity against reference payloads generated by v0.1.31's OWN code, never against the
    new code with the store switched off.

  The two pre-existing defects PERF-F2 and the price-basis split edge, pinned as
  `xfail(strict=True)` through v0.1.32, were fixed after it; `tests/portfolio/test_engine_repro.py`
  keeps their regression tests, and `tests/reference/perf_f2.py` owns PERF-F2's intended
  payload difference.
