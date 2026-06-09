# scheduler/ Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An in-process APScheduler that triggers `pricing/` refresh jobs on a per-market, post-close cadence (editable defaults), with a DB-backed schedule config + run log + extensible job registry, plus a manual trigger.

**Architecture:** `scheduler/` triggers only — no business logic. A `jobs` module holds the `JobSpec` registry + DB schedule config + `job_runs` logging + the pricing-refresh job functions + the work-list builder (all importable without APScheduler). A separate `runtime` module wires APScheduler `CronTrigger`s from the schedule config. Reads the `instruments` table (now with a `board` column) for the work-list; calls `pricing.refresh_*` over `pricing.default_registry()`.

**Tech Stack:** Python 3.12, APScheduler (in-process), stdlib `sqlite3`, Pydantic v2, pytest, mypy strict, ruff. Run all gates with `./.venv/Scripts/python.exe`.

---

## File Structure

- Modify `portfolio_dash/data_ingestion/schema.py` — add nullable `board` column to `instruments` (CREATE TABLE + idempotent ALTER-if-missing migration).
- Create `portfolio_dash/scheduler/__init__.py` — empty package marker.
- Create `portfolio_dash/scheduler/jobs.py` — `JobSpec` + `JOBS` registry + default cron/tz constants; `schedule_config`/`job_runs` DDL; `ensure_scheduler_seeded`/`ensure_job_rows`; `build_worklist`; the pricing-refresh job functions; `run_job`/`trigger_job`. (No APScheduler import — fully unit-testable.)
- Create `portfolio_dash/scheduler/runtime.py` — `build_scheduler`/`start`/`shutdown` (APScheduler).
- Modify `pyproject.toml` — add `APScheduler` dependency + a mypy `ignore_missing_imports` override.
- Tests: `tests/data_ingestion/test_schema.py` (append), `tests/scheduler/conftest.py`, `tests/scheduler/test_seed.py`, `tests/scheduler/test_worklist.py`, `tests/scheduler/test_jobs.py`, `tests/scheduler/test_runtime.py`.

---

### Task 1: `instruments.board` column + idempotent migration

**Files:**
- Modify: `portfolio_dash/data_ingestion/schema.py`
- Test: `tests/data_ingestion/test_schema.py`

- [ ] **Step 1: Append the failing tests**

```python
# tests/data_ingestion/test_schema.py  (append)


def test_instruments_has_board_column() -> None:
    c = sqlite3.connect(":memory:")
    create_tables(c)
    cols = {r[1] for r in c.execute("PRAGMA table_info(instruments)")}
    assert "board" in cols


def test_board_migration_idempotent_on_legacy_table() -> None:
    c = sqlite3.connect(":memory:")
    c.execute(
        "CREATE TABLE instruments (symbol TEXT PRIMARY KEY, market TEXT NOT NULL, "
        "quote_ccy TEXT NOT NULL, sector TEXT, name TEXT)"
    )  # legacy schema, no board column
    create_tables(c)  # must ALTER-add board
    create_tables(c)  # must be idempotent (no error)
    cols = {r[1] for r in c.execute("PRAGMA table_info(instruments)")}
    assert "board" in cols
```

- [ ] **Step 2: Run, verify FAIL**

Run: `./.venv/Scripts/python.exe -m pytest tests/data_ingestion/test_schema.py -v`
Expected: FAIL — `board` not in columns.

- [ ] **Step 3: Implementation**

In `portfolio_dash/data_ingestion/schema.py`, add `board TEXT` to the instruments table in `_DDL`:
```python
CREATE TABLE IF NOT EXISTS instruments (
    symbol TEXT PRIMARY KEY, market TEXT NOT NULL, quote_ccy TEXT NOT NULL,
    sector TEXT, name TEXT, board TEXT
);
```
And replace `create_tables` with:
```python
def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, decl: str
) -> None:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def create_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    _add_column_if_missing(conn, "instruments", "board", "TEXT")  # migrate legacy DBs
    conn.commit()
```

- [ ] **Step 4: Run, verify PASS**

Run: `./.venv/Scripts/python.exe -m pytest tests/data_ingestion/test_schema.py -v`
Expected: PASS.

- [ ] **Step 5: Gates + commit**

Run `./.venv/Scripts/python.exe -m pytest -q` (full suite green), `-m mypy`, `-m ruff check portfolio_dash/data_ingestion/schema.py tests/data_ingestion/test_schema.py`.
```bash
git add portfolio_dash/data_ingestion/schema.py tests/data_ingestion/test_schema.py
git commit -m "feat(data_ingestion): add nullable instruments.board column (+ idempotent migration)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: scheduler tables + JobSpec registry + seeding

**Files:**
- Create: `portfolio_dash/scheduler/__init__.py` (empty)
- Create: `portfolio_dash/scheduler/jobs.py`
- Create: `tests/scheduler/conftest.py`, `tests/scheduler/test_seed.py`

- [ ] **Step 1: Write the conftest + failing test**

```python
# tests/scheduler/conftest.py
import sqlite3
from collections.abc import Iterator

import pytest

from portfolio_dash.bootstrap import bootstrap_db


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)  # ledger + LLM tables (instruments incl. board)
    yield c
    c.close()
```

```python
# tests/scheduler/test_seed.py
import sqlite3

from portfolio_dash.scheduler.jobs import (
    JOBS,
    JobSpec,
    ensure_scheduler_seeded,
)


def _tables(c: sqlite3.Connection) -> set[str]:
    return {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_seed_creates_tables_and_one_row_per_job(conn: sqlite3.Connection) -> None:
    ensure_scheduler_seeded(conn)
    assert {"schedule_config", "job_runs"} <= _tables(conn)
    ids = {r["job_id"] for r in conn.execute("SELECT job_id FROM schedule_config")}
    assert ids == {j.id for j in JOBS}
    # every seeded row carries the JobSpec defaults
    row = conn.execute("SELECT * FROM schedule_config WHERE job_id='quotes_tw'").fetchone()
    assert row["timezone"] == "Asia/Taipei" and row["enabled"] == 1


def test_seed_is_idempotent_and_preserves_edits(conn: sqlite3.Connection) -> None:
    ensure_scheduler_seeded(conn)
    conn.execute("UPDATE schedule_config SET cron='9 9 * * *', enabled=0 WHERE job_id='quotes_tw'")
    conn.commit()
    ensure_scheduler_seeded(conn)  # re-run must not clobber the edit
    row = conn.execute("SELECT cron, enabled FROM schedule_config WHERE job_id='quotes_tw'").fetchone()
    assert row["cron"] == "9 9 * * *" and row["enabled"] == 0


def test_newly_registered_job_gets_default_row(conn: sqlite3.Connection) -> None:
    ensure_scheduler_seeded(conn)
    extra = JobSpec(
        id="probe_x", func=lambda c, *, now: "x", default_cron="0 5 * * *",
        default_timezone="UTC", default_enabled=True, description="test job",
    )
    JOBS.append(extra)
    try:
        ensure_scheduler_seeded(conn)  # idempotent ensure adds the new row
        row = conn.execute("SELECT job_id FROM schedule_config WHERE job_id='probe_x'").fetchone()
        assert row is not None
    finally:
        JOBS.remove(extra)
```

- [ ] **Step 2: Run, verify FAIL**

Run: `./.venv/Scripts/python.exe -m pytest tests/scheduler/test_seed.py -v`
Expected: FAIL — `ModuleNotFoundError: portfolio_dash.scheduler.jobs`.

- [ ] **Step 3: Implementation**

Create `portfolio_dash/scheduler/__init__.py` (empty file).

Create `portfolio_dash/scheduler/jobs.py` (this step adds the registry + seeding; later tasks append the worklist/job-funcs/run_job):
```python
"""Scheduler job registry, DB schedule config, and run log.

`scheduler/` triggers `pricing` (and later `llm_insight`) only — it holds no
business logic. This module is import-safe without APScheduler so it is fully
unit-testable; the APScheduler wiring lives in ``runtime.py``.
"""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from portfolio_dash.shared import config_store

# A job does its own trigger+wiring and returns a short run summary for job_runs.detail.
JobFunc = Callable[..., str]


@dataclass(frozen=True)
class JobSpec:
    id: str
    func: JobFunc
    default_cron: str
    default_timezone: str
    default_enabled: bool
    description: str


_DDL = """
CREATE TABLE IF NOT EXISTS schedule_config (
    job_id TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    cron TEXT NOT NULL,
    timezone TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT,
    detail TEXT
);
"""


def create_scheduler_tables(conn: sqlite3.Connection) -> None:
    """Create the scheduler tables idempotently."""
    conn.executescript(_DDL)
    conn.commit()


def ensure_job_rows(conn: sqlite3.Connection) -> None:
    """Insert a default ``schedule_config`` row for any registered job that lacks one.

    Idempotent (``INSERT OR IGNORE``): seeds all jobs on first run and adds rows for
    newly-registered jobs on later runs, while leaving existing (possibly user-edited)
    rows untouched.
    """
    for job in JOBS:
        conn.execute(
            "INSERT OR IGNORE INTO schedule_config (job_id, enabled, cron, timezone) "
            "VALUES (?, ?, ?, ?)",
            (job.id, 1 if job.default_enabled else 0, job.default_cron, job.default_timezone),
        )
    conn.commit()


def ensure_scheduler_seeded(conn: sqlite3.Connection) -> None:
    """Create scheduler tables (once) and ensure a default row per registered job (always)."""
    config_store.ensure_seeded(conn, "scheduler", create=create_scheduler_tables, seed=ensure_job_rows)
    ensure_job_rows(conn)  # also run unconditionally so newly-registered jobs get their row


# --- Job registry -------------------------------------------------------------
# Default cron times fall after each exchange's close; users override per job later.
# Placeholder funcs are replaced with the real pricing-refresh functions in Task 4.
def _todo(conn: sqlite3.Connection, *, now: datetime) -> str:  # pragma: no cover
    raise NotImplementedError


JOBS: list[JobSpec] = [
    JobSpec("quotes_tw", _todo, "0 14 * * mon-fri", "Asia/Taipei", True, "TW quotes + FX (post-close)"),
    JobSpec("quotes_us", _todo, "30 16 * * mon-fri", "America/New_York", True, "US quotes + FX (post-close)"),
    JobSpec("quotes_my", _todo, "30 17 * * mon-fri", "Asia/Kuala_Lumpur", True, "MY quotes + FX (post-close)"),
    JobSpec("history_daily", _todo, "0 2 * * *", "Asia/Taipei", True, "Daily history backfill (recent window)"),
    JobSpec("dividends_daily", _todo, "0 3 * * *", "Asia/Taipei", True, "Daily dividend/ex-div sweep"),
]
```

> Task 4 replaces each `JobSpec`'s `_todo` with the real function. Keeping `_todo` now lets Task 2/3 land and stay green independently.

- [ ] **Step 4: Run, verify PASS**

Run: `./.venv/Scripts/python.exe -m pytest tests/scheduler/test_seed.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Gates + commit**

`-m pytest -q` (full green), `-m mypy`, `-m ruff check portfolio_dash/scheduler/ tests/scheduler/`.
```bash
git add portfolio_dash/scheduler/__init__.py portfolio_dash/scheduler/jobs.py tests/scheduler/conftest.py tests/scheduler/test_seed.py
git commit -m "feat(scheduler): JobSpec registry + schedule_config/job_runs tables + seeding

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: `build_worklist` (instruments → InstrumentRef + FX pairs)

**Files:**
- Modify: `portfolio_dash/scheduler/jobs.py`
- Test: `tests/scheduler/test_worklist.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/scheduler/test_worklist.py
import sqlite3

from portfolio_dash.scheduler.jobs import build_worklist
from portfolio_dash.shared.enums import Market


def _add(conn: sqlite3.Connection, symbol: str, market: str, board: str | None) -> None:
    conn.execute(
        "INSERT INTO instruments (symbol, market, quote_ccy, sector, name, board) "
        "VALUES (?, ?, 'X', NULL, NULL, ?)",
        (symbol, market, board),
    )
    conn.commit()


def test_worklist_board_default_by_market(conn: sqlite3.Connection) -> None:
    _add(conn, "AAPL", "US", None)
    _add(conn, "3182", "MY", None)
    _add(conn, "2330", "TW", None)
    instruments, _ = build_worklist(conn, None)
    by_symbol = {i.symbol: i.board for i in instruments}
    assert by_symbol == {"AAPL": "", "3182": ".KL", "2330": "TWSE"}


def test_worklist_uses_stored_board(conn: sqlite3.Connection) -> None:
    _add(conn, "8299", "TW", "TPEx")  # resolved earlier and stored
    instruments, _ = build_worklist(conn, Market.TW)
    assert [(i.symbol, i.board) for i in instruments] == [("8299", "TPEx")]


def test_worklist_market_filter_and_fx_pairs(conn: sqlite3.Connection) -> None:
    _add(conn, "AAPL", "US", None)
    _add(conn, "2330", "TW", None)
    instruments, fx_pairs = build_worklist(conn, Market.US)
    assert [i.symbol for i in instruments] == ["AAPL"]
    assert fx_pairs  # the reporting-currency pairs are always returned
```

- [ ] **Step 2: Run, verify FAIL**

Run: `./.venv/Scripts/python.exe -m pytest tests/scheduler/test_worklist.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_worklist'`.

- [ ] **Step 3: Implementation — APPEND to `portfolio_dash/scheduler/jobs.py`**

Add imports at the top (merge into the existing import block, correctly sorted):
```python
from portfolio_dash.pricing.refs import FxPair, InstrumentRef
from portfolio_dash.shared.enums import Currency, Market
```
Append:
```python
_DEFAULT_BOARD: dict[Market, str] = {Market.US: "", Market.MY: ".KL", Market.TW: "TWSE"}

# Reporting-currency FX pairs needed for the combined view (reporting ccy = TWD).
_FX_PAIRS: list[FxPair] = [
    FxPair(base=Currency.USD, quote=Currency.TWD),
    FxPair(base=Currency.USD, quote=Currency.MYR),
    FxPair(base=Currency.MYR, quote=Currency.TWD),
]


def build_worklist(
    conn: sqlite3.Connection, market: Market | None
) -> tuple[list[InstrumentRef], list[FxPair]]:
    """Build the pricing work-list from the ``instruments`` table.

    Board comes from the stored ``instruments.board`` column when set, else the
    deterministic market default (US ``""`` / MY ``".KL"`` / TW ``"TWSE"``). FX pairs
    are the fixed reporting-currency set.
    """
    sql = "SELECT symbol, market, board FROM instruments"
    params: tuple[str, ...] = ()
    if market is not None:
        sql += " WHERE market = ?"
        params = (market.value,)
    refs: list[InstrumentRef] = []
    for row in conn.execute(sql, params):
        mkt = Market(row["market"])
        board = row["board"] or _DEFAULT_BOARD[mkt]
        refs.append(InstrumentRef(symbol=row["symbol"], market=mkt, board=board))
    return refs, _FX_PAIRS
```

- [ ] **Step 4: Run, verify PASS**

Run: `./.venv/Scripts/python.exe -m pytest tests/scheduler/test_worklist.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Gates + commit**

`-m pytest -q`, `-m mypy`, `-m ruff check portfolio_dash/scheduler/ tests/scheduler/`.
```bash
git add portfolio_dash/scheduler/jobs.py tests/scheduler/test_worklist.py
git commit -m "feat(scheduler): build_worklist (instruments+board -> InstrumentRef + FX pairs)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: pricing-refresh job functions

**Files:**
- Modify: `portfolio_dash/scheduler/jobs.py`
- Test: `tests/scheduler/test_jobs.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/scheduler/test_jobs.py
import sqlite3
from datetime import UTC, datetime

import pytest

from portfolio_dash.scheduler import jobs as jobs_mod
from portfolio_dash.scheduler.jobs import quotes_us, refresh_quotes_for
from portfolio_dash.shared.enums import Market

_NOW = datetime(2026, 6, 10, tzinfo=UTC)


class _Summary:
    def __init__(self) -> None:
        self.ok = {"AAPL": "yfinance"}
        self.failed: list[str] = []


def _add(conn: sqlite3.Connection, symbol: str, market: str) -> None:
    conn.execute(
        "INSERT INTO instruments (symbol, market, quote_ccy, sector, name, board) "
        "VALUES (?, ?, 'X', NULL, NULL, NULL)",
        (symbol, market),
    )
    conn.commit()


def test_quotes_job_passes_market_worklist(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    _add(conn, "AAPL", "US")
    _add(conn, "2330", "TW")
    captured: dict[str, object] = {}

    monkeypatch.setattr(jobs_mod, "default_registry", lambda: "REG")

    def fake_refresh(c, registry, instruments, fx_pairs, *, now):  # type: ignore[no-untyped-def]
        captured["registry"] = registry
        captured["symbols"] = [i.symbol for i in instruments]
        captured["fx"] = len(fx_pairs)
        return _Summary()

    monkeypatch.setattr(jobs_mod, "refresh_quotes", fake_refresh)
    detail = quotes_us(conn, now=_NOW)
    assert captured["registry"] == "REG"
    assert captured["symbols"] == ["AAPL"]  # only US, not TW
    assert "1 ok" in detail and "0 failed" in detail


def test_refresh_quotes_for_filters_by_market(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    _add(conn, "2330", "TW")
    monkeypatch.setattr(jobs_mod, "default_registry", lambda: "REG")
    seen: dict[str, object] = {}

    def fake_refresh(c, registry, instruments, fx_pairs, *, now):  # type: ignore[no-untyped-def]
        seen["symbols"] = [i.symbol for i in instruments]
        return _Summary()

    monkeypatch.setattr(jobs_mod, "refresh_quotes", fake_refresh)
    refresh_quotes_for(conn, Market.TW, now=_NOW)
    assert seen["symbols"] == ["2330"]
```

- [ ] **Step 2: Run, verify FAIL**

Run: `./.venv/Scripts/python.exe -m pytest tests/scheduler/test_jobs.py -v`
Expected: FAIL — `ImportError: cannot import name 'quotes_us'`.

- [ ] **Step 3: Implementation**

Add imports to `portfolio_dash/scheduler/jobs.py` (merge into the import block, sorted):
```python
from datetime import datetime, timedelta

from portfolio_dash.pricing.defaults import default_registry
from portfolio_dash.pricing.refresh import refresh_dividends, refresh_history, refresh_quotes
from portfolio_dash.pricing.results import RefreshSummary
```
(The existing `from datetime import datetime` line — replace it with `from datetime import datetime, timedelta`.)

Append the job functions, and REPLACE the `_todo` placeholders in `JOBS` with these:
```python
_HISTORY_LOOKBACK_DAYS = 7


def _summarize(summary: RefreshSummary) -> str:
    return f"{len(summary.ok)} ok, {len(summary.failed)} failed"


def refresh_quotes_for(conn: sqlite3.Connection, market: Market, *, now: datetime) -> str:
    """Refresh latest quotes + FX for one market's instruments."""
    instruments, fx_pairs = build_worklist(conn, market)
    summary = refresh_quotes(conn, default_registry(), instruments, fx_pairs, now=now)
    return _summarize(summary)


def quotes_tw(conn: sqlite3.Connection, *, now: datetime) -> str:
    return refresh_quotes_for(conn, Market.TW, now=now)


def quotes_us(conn: sqlite3.Connection, *, now: datetime) -> str:
    return refresh_quotes_for(conn, Market.US, now=now)


def quotes_my(conn: sqlite3.Connection, *, now: datetime) -> str:
    return refresh_quotes_for(conn, Market.MY, now=now)


def history_daily(conn: sqlite3.Connection, *, now: datetime) -> str:
    """Backfill a recent history window for all instruments (deep backfill is manual)."""
    instruments, _ = build_worklist(conn, None)
    start = (now - timedelta(days=_HISTORY_LOOKBACK_DAYS)).date()
    summary = refresh_history(conn, default_registry(), instruments, start, now=now)
    return _summarize(summary)


def dividends_daily(conn: sqlite3.Connection, *, now: datetime) -> str:
    """Sweep dividend/ex-div events for all instruments."""
    instruments, _ = build_worklist(conn, None)
    summary = refresh_dividends(conn, default_registry(), instruments, now=now)
    return _summarize(summary)
```
Then change the `JOBS` list so each `JobSpec` references the real function instead of `_todo` (e.g. `JobSpec("quotes_tw", quotes_tw, ...)`), and delete the now-unused `_todo` placeholder. (Move the `JOBS` definition below these functions so they are defined first.)

- [ ] **Step 4: Run, verify PASS**

Run: `./.venv/Scripts/python.exe -m pytest tests/scheduler/test_jobs.py tests/scheduler/test_seed.py -v`
Expected: PASS.

- [ ] **Step 5: Gates + commit**

`-m pytest -q`, `-m mypy`, `-m ruff check portfolio_dash/scheduler/ tests/scheduler/`.
```bash
git add portfolio_dash/scheduler/jobs.py tests/scheduler/test_jobs.py
git commit -m "feat(scheduler): pricing-refresh job functions (per-market quotes, history, dividends)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: `run_job` (logging + swallow-on-error) + `trigger_job`

**Files:**
- Modify: `portfolio_dash/scheduler/jobs.py`
- Test: `tests/scheduler/test_jobs.py` (append)

- [ ] **Step 1: Append the failing tests**

```python
# tests/scheduler/test_jobs.py  (append)
from portfolio_dash.scheduler.jobs import JobSpec, run_job  # noqa: E402


def _register(monkeypatch: pytest.MonkeyPatch, spec: JobSpec) -> None:
    monkeypatch.setattr(jobs_mod, "JOBS", [*jobs_mod.JOBS, spec])


def test_run_job_logs_ok(monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection) -> None:
    _register(monkeypatch, JobSpec("ok_job", lambda c, *, now: "did 3", "0 0 * * *", "UTC", True, ""))
    run_job(conn, "ok_job", now=_NOW)
    row = conn.execute("SELECT status, detail, finished_at FROM job_runs WHERE job_id='ok_job'").fetchone()
    assert row["status"] == "ok" and row["detail"] == "did 3" and row["finished_at"] is not None


def test_run_job_swallows_and_logs_error(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    def boom(c: sqlite3.Connection, *, now: datetime) -> str:
        raise RuntimeError("provider exploded")

    _register(monkeypatch, JobSpec("bad_job", boom, "0 0 * * *", "UTC", True, ""))
    run_job(conn, "bad_job", now=_NOW)  # must NOT raise
    row = conn.execute("SELECT status, detail FROM job_runs WHERE job_id='bad_job'").fetchone()
    assert row["status"] == "error" and "provider exploded" in row["detail"]
```

- [ ] **Step 2: Run, verify FAIL**

Run: `./.venv/Scripts/python.exe -m pytest tests/scheduler/test_jobs.py -k run_job -v`
Expected: FAIL — `ImportError: cannot import name 'run_job'`.

- [ ] **Step 3: Implementation — APPEND to `portfolio_dash/scheduler/jobs.py`**

Add to the import block (sorted): `from datetime import UTC` → the datetime import becomes
`from datetime import UTC, datetime, timedelta`. Add `from portfolio_dash.shared.db import session`.
Append:
```python
def _jobs_by_id() -> dict[str, JobSpec]:
    return {j.id: j for j in JOBS}


def run_job(conn: sqlite3.Connection, job_id: str, *, now: datetime) -> None:
    """Execute one job, logging start/finish to ``job_runs``.

    A job exception is caught and logged as ``status="error"`` (never re-raised), so
    one failing job cannot crash the scheduler or other jobs.
    """
    spec = _jobs_by_id()[job_id]
    cur = conn.execute(
        "INSERT INTO job_runs (job_id, started_at) VALUES (?, ?)",
        (job_id, now.isoformat()),
    )
    run_id = cur.lastrowid
    conn.commit()
    try:
        detail = spec.func(conn, now=now)
        status = "ok"
    except Exception as exc:  # noqa: BLE001 — swallow + log; never crash the scheduler
        detail, status = str(exc), "error"
    conn.execute(
        "UPDATE job_runs SET finished_at = ?, status = ?, detail = ? WHERE id = ?",
        (datetime.now(UTC).isoformat(), status, detail, run_id),
    )
    conn.commit()


def trigger_job(job_id: str) -> None:
    """Manual ad-hoc run of a job (used by the scheduler and a future manual-trigger route)."""
    with session() as conn:
        run_job(conn, job_id, now=datetime.now(UTC))
```

- [ ] **Step 4: Run, verify PASS**

Run: `./.venv/Scripts/python.exe -m pytest tests/scheduler/test_jobs.py -v`
Expected: PASS.

- [ ] **Step 5: Gates + commit**

`-m pytest -q`, `-m mypy`, `-m ruff check portfolio_dash/scheduler/ tests/scheduler/`.
```bash
git add portfolio_dash/scheduler/jobs.py tests/scheduler/test_jobs.py
git commit -m "feat(scheduler): run_job (job_runs logging, swallow-and-log) + manual trigger_job

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: APScheduler dependency + `runtime.py`

**Files:**
- Modify: `pyproject.toml`
- Create: `portfolio_dash/scheduler/runtime.py`
- Test: `tests/scheduler/test_runtime.py`

- [ ] **Step 1: Add the dependency + install**

In `pyproject.toml`, add to `[project].dependencies`: `"APScheduler>=3.10,<4"`.
Add an `ignore_missing_imports` override — extend the existing override module list to include `"apscheduler.*"`:
```toml
[[tool.mypy.overrides]]
module = ["yfinance.*", "FinMind.*", "twstock.*", "bs4.*", "pandas.*", "numpy.*", "litellm.*", "apscheduler.*"]
ignore_missing_imports = true
```
Install into the venv:
```bash
./.venv/Scripts/python.exe -m pip install "APScheduler>=3.10,<4"
```
Verify: `./.venv/Scripts/python.exe -c "from apscheduler.schedulers.background import BackgroundScheduler; from apscheduler.triggers.cron import CronTrigger; print('ok')"`

- [ ] **Step 2: Write the failing test**

```python
# tests/scheduler/test_runtime.py
import sqlite3

from portfolio_dash.scheduler import runtime
from portfolio_dash.scheduler.jobs import ensure_scheduler_seeded


def test_build_scheduler_adds_only_enabled_jobs(
    conn: sqlite3.Connection, monkeypatch: "object"
) -> None:
    ensure_scheduler_seeded(conn)
    conn.execute("UPDATE schedule_config SET enabled=0 WHERE job_id='quotes_my'")
    conn.commit()

    # Feed this connection to build_scheduler instead of opening a real DB session.
    import contextlib

    @contextlib.contextmanager
    def fake_session():  # type: ignore[no-untyped-def]
        yield conn

    monkeypatch.setattr(runtime, "session", fake_session)  # type: ignore[attr-defined]
    scheduler = runtime.build_scheduler()
    ids = {j.id for j in scheduler.get_jobs()}
    assert "quotes_tw" in ids and "quotes_us" in ids
    assert "quotes_my" not in ids  # disabled row -> no trigger
```

(Type the fixture/param properly: `monkeypatch: pytest.MonkeyPatch` with `import pytest`.)

- [ ] **Step 3: Run, verify FAIL**

Run: `./.venv/Scripts/python.exe -m pytest tests/scheduler/test_runtime.py -v`
Expected: FAIL — `ModuleNotFoundError: portfolio_dash.scheduler.runtime`.

- [ ] **Step 4: Implementation**

Create `portfolio_dash/scheduler/runtime.py`:
```python
"""APScheduler wiring: build cron-triggered jobs from the DB schedule config.

Triggers only. Each scheduled action calls ``trigger_job`` (which opens its own
DB connection), so jobs are independent and a single failure is logged, not fatal.
"""

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from portfolio_dash.scheduler.jobs import ensure_scheduler_seeded, trigger_job
from portfolio_dash.shared.db import session


def build_scheduler() -> BackgroundScheduler:
    """Build a scheduler with a cron trigger per enabled ``schedule_config`` row."""
    scheduler = BackgroundScheduler()
    with session() as conn:
        ensure_scheduler_seeded(conn)
        rows = conn.execute(
            "SELECT job_id, enabled, cron, timezone FROM schedule_config"
        ).fetchall()
    for row in rows:
        if not row["enabled"]:
            continue
        trigger = CronTrigger.from_crontab(row["cron"], timezone=row["timezone"])
        scheduler.add_job(
            trigger_job,
            trigger,
            args=[row["job_id"]],
            id=row["job_id"],
            replace_existing=True,
        )
    return scheduler


def start() -> BackgroundScheduler:
    """Build and start the background scheduler."""
    scheduler = build_scheduler()
    scheduler.start()
    return scheduler


def shutdown(scheduler: BackgroundScheduler) -> None:
    """Stop the scheduler."""
    scheduler.shutdown(wait=False)
```

- [ ] **Step 5: Run, verify PASS**

Run: `./.venv/Scripts/python.exe -m pytest tests/scheduler/test_runtime.py -v`
Expected: PASS.

- [ ] **Step 6: Gates + commit**

`-m pytest -q` (full green), `-m mypy`, `-m ruff check .`.
```bash
git add pyproject.toml portfolio_dash/scheduler/runtime.py tests/scheduler/test_runtime.py
git commit -m "feat(scheduler): APScheduler runtime (enabled cron triggers, start/shutdown) + dep

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Final review (after all tasks)

- [ ] Holistic review of the branch diff: scheduler imports only `pricing` + `shared` (+ reads the `instruments` table via SQL), no compute, no ledger writes, no `web_ui`; APScheduler confined to `runtime.py`; job failures are logged not fatal; `board` migration is idempotent.
- [ ] `./.venv/Scripts/python.exe -m pytest -q`, `-m mypy`, `-m ruff check .` — all green.
- [ ] `CHANGELOG.md` `[Unreleased]` entry for `scheduler/` (note the `APScheduler` dependency added + `instruments.board` column); `grep -c "^## \[v" CHANGELOG.md` still `1`.
- [ ] `LESSONS_LEARNED.md` updated if anything was learned the hard way.
- [ ] Then use **superpowers:finishing-a-development-branch**.
