# Spec 15 — Scheduler Management API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development /
> test-driven-development. Steps use checkbox (`- [ ]`) tracking. You work in an isolated git
> worktree. You MAY edit `portfolio_dash/api/app.py` and `tests/conftest.py` to wire and test
> your feature. **Do NOT edit `CHANGELOG.md`** (the controller owns it — avoids a merge conflict
> with the parallel spec-09 worktree). Commit with scoped `git add` (never `-A`/`.`).

**Goal:** Expose the existing in-process scheduler over `/api/scheduler/*`: list jobs (config +
last run + next fire), edit cron/tz/enabled with live reschedule, fire a job now (async 202 +
background thread), and read run history — plus the SR-2026-06-13 §15.0 schema columns that
specs 04/07 depend on.

**Architecture:** Thin router (decision B) over the existing `scheduler/jobs.py` (registry,
`run_job`, schedule_config/job_runs tables) and a new dynamic-reschedule seam in
`scheduler/runtime.py`. The APScheduler singleton lives in `app.state.scheduler` (may be `None`
when `PD_DISABLE_SCHEDULER=1`, e.g. tests) — every route degrades gracefully when it is `None`.
Money/cost as Decimal **strings**; no business computation in the router.

**Tech stack:** FastAPI, sqlite3 (no ORM), APScheduler `CronTrigger`, pytest + TestClient.

---

## Reconciliations (read before coding)
1. **§15.0 schema columns** go on the tables owned by `scheduler/jobs.py`. Add them in
   `create_scheduler_tables` via a **local** `_add_column_if_missing` helper (copy the 6-line
   PRAGMA pattern from `data_ingestion/schema.py` into `jobs.py` — do NOT `import` it; `scheduler`
   must not gain a dependency on `data_ingestion`, see `architecture.md`). Columns:
   `schedule_config += kind TEXT NOT NULL DEFAULT 'system'`, `payload TEXT NULL`;
   `job_runs += payload TEXT NULL`, `reason TEXT NULL`, `cost_usd TEXT NULL`.
2. **kind=system only in v1.** No insight jobs exist yet (spec 04 unbuilt). The list query returns
   all `schedule_config` rows; `desc` resolves from the `JOBS` registry by `job_id`, falling back
   to `job_id` for any non-registry (future insight) row. Forward-compatible, not faked.
3. **Async 202 for `/run`** (reconciles the spec-08 §8.2 sync-200 deferral): insert the "running"
   `job_runs` row synchronously on the request conn to get `run_id`, return 202, then a background
   `threading.Thread` opens its **own** `session()` and executes + finalizes that row. The request
   conn is request-scoped and closes after the response, so the thread must not use it.
4. **`already_running`** = the job's latest `job_runs` row has `finished_at IS NULL`.
5. **Hermetic tests**: `/run` of a real quotes job would hit the network. Monkeypatch
   `portfolio_dash.scheduler.jobs.default_registry` to an empty `Registry` (as the spec-08 actions
   tests do), OR assert only the synchronous part (run row created + 202 + run_id) and the 404/409
   paths. Do not let a test touch the live network (pytest-socket is armed).
6. **`next` fire time** comes from `app.state.scheduler.get_job(job_id).next_run_time` when the
   scheduler is live; `None` when the scheduler is `None` (tests) or the job is disabled/unmounted.

---

### Task 1: §15.0 schema columns + `start_job_run` helper (`scheduler/jobs.py`)

**Files:** Modify `portfolio_dash/scheduler/jobs.py`; Test `tests/scheduler/test_schema_ext.py`,
`tests/scheduler/test_start_job_run.py`.

- [ ] **Step 1 — failing test** `tests/scheduler/test_schema_ext.py`:
```python
import sqlite3
from portfolio_dash.scheduler.jobs import create_scheduler_tables

def _cols(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}

def test_new_columns_present():
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    create_scheduler_tables(conn)
    assert {"kind", "payload"} <= _cols(conn, "schedule_config")
    assert {"payload", "reason", "cost_usd"} <= _cols(conn, "job_runs")

def test_migration_idempotent_on_legacy_db():
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    # simulate a legacy DB: create base tables WITHOUT the new columns
    conn.executescript(
        "CREATE TABLE schedule_config (job_id TEXT PRIMARY KEY, enabled INTEGER, cron TEXT, timezone TEXT);"
        "CREATE TABLE job_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, started_at TEXT, "
        "finished_at TEXT, status TEXT, detail TEXT);")
    create_scheduler_tables(conn)  # must add columns, not crash
    create_scheduler_tables(conn)  # idempotent second run
    assert {"kind", "payload"} <= _cols(conn, "schedule_config")
    assert {"payload", "reason", "cost_usd"} <= _cols(conn, "job_runs")
```

- [ ] **Step 2 — run, expect FAIL.**

- [ ] **Step 3 — implement.** In `jobs.py` add a local migration helper and call it from
`create_scheduler_tables` (keep the existing `_DDL` executescript for fresh DBs, then add columns
for legacy DBs):
```python
def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def create_scheduler_tables(conn: sqlite3.Connection) -> None:
    """Create the scheduler tables idempotently and apply additive §15.0 migrations."""
    conn.executescript(_DDL)
    _add_column_if_missing(conn, "schedule_config", "kind", "TEXT NOT NULL DEFAULT 'system'")
    _add_column_if_missing(conn, "schedule_config", "payload", "TEXT")
    _add_column_if_missing(conn, "job_runs", "payload", "TEXT")
    _add_column_if_missing(conn, "job_runs", "reason", "TEXT")
    _add_column_if_missing(conn, "job_runs", "cost_usd", "TEXT")
    conn.commit()
```
Note: `PRAGMA table_info` rows are tuples unless `row_factory=Row`; guard by indexing name as
`r["name"] if isinstance(r, sqlite3.Row) else r[1]` OR require Row (callers in this repo set Row).
Use the `r[1]` form to be row_factory-agnostic and safe.

- [ ] **Step 4 — `start_job_run` helper test** `tests/scheduler/test_start_job_run.py`:
```python
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
from portfolio_dash.scheduler.jobs import create_scheduler_tables, start_job_run, latest_run_unfinished

NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))

def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    create_scheduler_tables(c); return c

def test_start_job_run_inserts_running_row():
    conn = _conn()
    rid = start_job_run(conn, "quotes_tw", now=NOW)
    row = conn.execute("SELECT * FROM job_runs WHERE id=?", (rid,)).fetchone()
    assert row["job_id"] == "quotes_tw" and row["started_at"] == NOW.isoformat()
    assert row["finished_at"] is None and row["status"] is None
    assert latest_run_unfinished(conn, "quotes_tw") is True

def test_latest_run_unfinished_false_when_finished():
    conn = _conn()
    rid = start_job_run(conn, "quotes_tw", now=NOW)
    conn.execute("UPDATE job_runs SET finished_at=?, status='ok' WHERE id=?", (NOW.isoformat(), rid))
    conn.commit()
    assert latest_run_unfinished(conn, "quotes_tw") is False
```

- [ ] **Step 5 — implement** in `jobs.py`:
```python
def start_job_run(conn: sqlite3.Connection, job_id: str, *, now: datetime) -> int:
    """Insert a 'running' job_runs row (finished_at NULL) and return its id.

    Used by POST /api/scheduler/jobs/{id}/run to obtain the run id synchronously
    (on the request conn) before the background thread finalizes the row.
    """
    cur = conn.execute(
        "INSERT INTO job_runs (job_id, started_at) VALUES (?, ?)", (job_id, now.isoformat())
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def finish_job_run(conn: sqlite3.Connection, run_id: int, *, status: str, detail: str) -> None:
    conn.execute(
        "UPDATE job_runs SET finished_at = ?, status = ?, detail = ? WHERE id = ?",
        (datetime.now(UTC).isoformat(), status, detail, run_id),
    )
    conn.commit()


def latest_run_unfinished(conn: sqlite3.Connection, job_id: str) -> bool:
    row = conn.execute(
        "SELECT finished_at FROM job_runs WHERE job_id = ? ORDER BY id DESC LIMIT 1", (job_id,)
    ).fetchone()
    return row is not None and row["finished_at"] is None


def run_job_func(job_id: str, *, now: datetime) -> None:
    """Execute a job in a fresh session, finalizing its latest running row.

    For the async /run endpoint: the request handler already inserted the running row
    via start_job_run; this opens its OWN connection (the request conn is closed by then)
    and finalizes. Exceptions are swallowed + logged as status='error' (never crash).
    """
    with session() as conn:
        rid = conn.execute(
            "SELECT id FROM job_runs WHERE job_id=? AND finished_at IS NULL ORDER BY id DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        if rid is None:
            return
        try:
            detail = _jobs_by_id()[job_id].func(conn, now=now)
            status = "ok"
        except Exception as exc:  # noqa: BLE001
            detail, status = str(exc), "error"
        finish_job_run(conn, int(rid["id"]), status=status, detail=detail)
```
Keep the existing `run_job` (sync, used by refresh-quotes action) unchanged.

- [ ] **Step 6 — gates** (`.venv/Scripts/python -m pytest tests/scheduler -q`, mypy, ruff).
- [ ] **Step 7 — commit:** `git add portfolio_dash/scheduler/jobs.py tests/scheduler/test_schema_ext.py tests/scheduler/test_start_job_run.py && git commit -m "feat(scheduler): §15.0 schema columns + start/finish/run_job_func helpers (spec 15)"`

---

### Task 2: dynamic reschedule seam (`scheduler/runtime.py`)

**Files:** Modify `portfolio_dash/scheduler/runtime.py`; Test `tests/scheduler/test_runtime_reschedule.py`.

- [ ] **Step 1 — failing test** (drive a real `BackgroundScheduler` without starting it):
```python
from apscheduler.schedulers.background import BackgroundScheduler
from portfolio_dash.scheduler.runtime import reschedule_job

def test_reschedule_adds_and_updates_job():
    sch = BackgroundScheduler()
    reschedule_job(sch, "quotes_tw", cron="0 14 * * mon-fri", tz="Asia/Taipei", enabled=True)
    assert sch.get_job("quotes_tw") is not None
    reschedule_job(sch, "quotes_tw", cron="30 17 * * mon-fri", tz="Asia/Kuala_Lumpur", enabled=True)
    assert sch.get_job("quotes_tw") is not None  # replaced, still present

def test_reschedule_disabled_removes_job():
    sch = BackgroundScheduler()
    reschedule_job(sch, "quotes_tw", cron="0 14 * * mon-fri", tz="Asia/Taipei", enabled=True)
    reschedule_job(sch, "quotes_tw", cron="0 14 * * mon-fri", tz="Asia/Taipei", enabled=False)
    assert sch.get_job("quotes_tw") is None

def test_reschedule_none_scheduler_is_noop():
    reschedule_job(None, "quotes_tw", cron="0 14 * * mon-fri", tz="Asia/Taipei", enabled=True)  # no raise
```

- [ ] **Step 2 — run, FAIL.**
- [ ] **Step 3 — implement** in `runtime.py`:
```python
from apscheduler.schedulers.base import BaseScheduler
from portfolio_dash.scheduler.jobs import trigger_job

def reschedule_job(
    scheduler: BaseScheduler | None, job_id: str, *, cron: str, tz: str, enabled: bool
) -> None:
    """Apply a schedule change to the live scheduler immediately.

    A no-op when ``scheduler`` is None (e.g. PD_DISABLE_SCHEDULER=1 in tests / when the
    scheduler is not running). Disabled jobs are removed; enabled jobs are (re)added with
    replace_existing so an existing trigger is updated in place.
    """
    if scheduler is None:
        return
    if not enabled:
        if scheduler.get_job(job_id) is not None:
            scheduler.remove_job(job_id)
        return
    trigger = CronTrigger.from_crontab(cron, timezone=tz)
    scheduler.add_job(trigger_job, trigger, args=[job_id], id=job_id, replace_existing=True)
```
(`CronTrigger` is already imported in runtime.py.) Add mypy types; `BaseScheduler` import keeps the
signature precise.

- [ ] **Step 4 — gates. Step 5 — commit:** `git add portfolio_dash/scheduler/runtime.py tests/scheduler/test_runtime_reschedule.py && git commit -m "feat(scheduler): reschedule_job dynamic seam (None-safe) (spec 15)"`

---

### Task 3: router `scheduler.py` + wire into app + contract tests

**Files:** Create `portfolio_dash/api/routers/scheduler.py`; Modify `portfolio_dash/api/app.py`
(include the router); Modify `tests/conftest.py` only if needed (golden_db already calls
`create_scheduler_tables`, which now adds the columns — likely no change); Test
`tests/contract/test_scheduler_api.py`.

**Scheduler access dependency** (in `scheduler.py`):
```python
from fastapi import Request
def get_scheduler(request: Request) -> object | None:
    return getattr(request.app.state, "scheduler", None)
```
Note: in `api_client` tests there is no lifespan, so `app.state.scheduler` is unset → `getattr`
returns `None` → routes degrade gracefully (no live reschedule, `next` = null).

**Endpoints** (all under `/api`, router prefix none; paths include `/scheduler/...`):

- `GET /scheduler/jobs` → `{"jobs": [ {id, desc, cron, tz, enabled, last, next} ]}`
  - `last` = latest job_runs row for that job_id mapped to `{status, at: started_at, detail,
    duration_s}` or `null` if none. `duration_s` = (finished−started) seconds as float, or null
    when unfinished. `desc` from `JOBS` registry by id (fallback to id). `next` = scheduler
    get_job next_run_time isoformat or null.
- `PUT /scheduler/jobs/{id}` body `{cron?, tz?, enabled?}` (subset merge over current row)
  - 404 unknown id (not in schedule_config). Validate `CronTrigger.from_crontab(cron, timezone=tz)`
    → on `ValueError`/exception return **400** `invalid_cron` (field "cron"; if the failure is tz,
    field "tz") and **do NOT write DB**. On success: UPDATE schedule_config, call
    `reschedule_job(get_scheduler(...), id, cron, tz, enabled)`, return the full updated row
    (same shape as one element of 15.1, with recomputed `next`).
- `POST /scheduler/jobs/{id}/run` → 404 unknown id; 409 `already_running` if
  `latest_run_unfinished`; else `start_job_run` (request conn) → spawn
  `threading.Thread(target=jobs.run_job_func, kwargs={"job_id": id, "now": now}, daemon=True)`,
  start it, return **202** `{run_id, job_id}`. Use `get_now` for the started_at.
- `GET /scheduler/runs?job_id&limit=50&offset=0` → `{"rows":[...], "total_count": N}`.
  - limit > 500 → 400 `validation_error`. Rows: `{id, job_id, started_at, finished_at, status,
    detail, duration_s, cost_usd}`; `cost_usd` raw string or null; keep `status` null when
    unfinished (do not stringify). Order by id DESC. total_count = COUNT with the same job_id filter.

**Error shape:** raise `HTTPException(404, ...)` for not-found (handler maps to `not_found`). For
400 `invalid_cron` and 409 `already_running`, return an explicit
`JSONResponse(status_code=…, content=error_body(code, msg, field=…))` (the `_STATUS_CODE` table has
no 409 and the 400 needs a custom code — mirror `instruments.py`'s explicit-JSONResponse pattern).

- [ ] **Step 1 — failing contract tests** `tests/contract/test_scheduler_api.py` (use `api_client`
+ `golden_db`; seed runs directly into golden_db where needed):
```python
def test_list_jobs_shape(api_client, golden_db):
    r = api_client.get("/api/scheduler/jobs")
    assert r.status_code == 200
    jobs = r.json()["jobs"]
    ids = {j["id"] for j in jobs}
    assert {"quotes_tw", "quotes_us", "quotes_my"} <= ids
    tw = next(j for j in jobs if j["id"] == "quotes_tw")
    assert tw["cron"] and tw["tz"] == "Asia/Taipei" and tw["enabled"] is True
    assert tw["next"] is None  # no live scheduler in tests
    assert "last" in tw

def test_put_invalid_cron_400_no_write(api_client, golden_db):
    r = api_client.put("/api/scheduler/jobs/quotes_tw", json={"cron": "not a cron"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_cron"
    row = golden_db.execute("SELECT cron FROM schedule_config WHERE job_id='quotes_tw'").fetchone()
    assert row["cron"] == "0 14 * * mon-fri"  # unchanged

def test_put_updates_row(api_client, golden_db):
    r = api_client.put("/api/scheduler/jobs/quotes_tw",
                       json={"cron": "30 17 * * mon-fri", "tz": "Asia/Kuala_Lumpur"})
    assert r.status_code == 200
    body = r.json()
    assert body["cron"] == "30 17 * * mon-fri" and body["tz"] == "Asia/Kuala_Lumpur"

def test_put_unknown_404(api_client):
    assert api_client.put("/api/scheduler/jobs/nope", json={"enabled": False}).status_code == 404

def test_run_already_running_409(api_client, golden_db):
    golden_db.execute("INSERT INTO job_runs (job_id, started_at) VALUES ('quotes_tw','2026-06-11T14:00:00+08:00')")
    golden_db.commit()
    r = api_client.post("/api/scheduler/jobs/quotes_tw/run")
    assert r.status_code == 409 and r.json()["error"]["code"] == "already_running"

def test_run_unknown_404(api_client):
    assert api_client.post("/api/scheduler/jobs/nope/run").status_code == 404

def test_run_202_inserts_row(api_client, golden_db, monkeypatch):
    # hermetic: stub the job func so the bg thread does no network
    import portfolio_dash.scheduler.jobs as J
    monkeypatch.setattr(J, "_jobs_by_id",
                        lambda: {"quotes_tw": J.JobSpec("quotes_tw", lambda conn, *, now: "stub",
                                                        "0 14 * * mon-fri", "Asia/Taipei", True, "d")})
    r = api_client.post("/api/scheduler/jobs/quotes_tw/run")
    assert r.status_code == 202
    body = r.json()
    assert body["job_id"] == "quotes_tw" and isinstance(body["run_id"], int)
    row = golden_db.execute("SELECT * FROM job_runs WHERE id=?", (body["run_id"],)).fetchone()
    assert row is not None and row["started_at"]

def test_runs_history_and_limit(api_client, golden_db):
    r = api_client.get("/api/scheduler/runs?limit=10")
    assert r.status_code == 200 and "rows" in r.json() and "total_count" in r.json()
    assert api_client.get("/api/scheduler/runs?limit=501").status_code == 400
```
NOTE: the §15.3 background thread opens its OWN `session()` (the throwaway file DB), so the test
asserts only the synchronous running-row insert on `golden_db` + the 202 — not the thread's
completion (covered by `run_job_func` unit coverage if you add it). Keep `daemon=True` so the test
process can exit.

- [ ] **Step 2 — run, FAIL.** **Step 3 — implement router + add
`app.include_router(scheduler.router, prefix="/api")` to `create_app()` (and the import).**
- [ ] **Step 4 — run contract tests green. Step 5 — full suite + mypy + ruff via `.venv`.**
- [ ] **Step 6 — commit:** `git add portfolio_dash/api/routers/scheduler.py portfolio_dash/api/app.py tests/contract/test_scheduler_api.py && git commit -m "feat(api): scheduler management /api/scheduler/* (jobs/put-reschedule/run/runs) (spec 15)"`

## Self-review checklist
Decimal cost_usd as raw string/null; `status`/`next`/`last` null preserved (not stringified);
invalid cron/tz → 400 + no DB write; 404/409 explicit JSONResponse; `/run` async-202 + own-session
thread (daemon); scheduler-None degradation everywhere; no business computation in the router;
`scheduler` does not import `data_ingestion`.
