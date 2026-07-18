"""FU-D36: the in-flight registry wrapper marks 執行中 during a run and clears after.

The status endpoint keys 執行中 vs 成功/失敗 off :func:`running_job_ids`. These unit tests
pin the wrapper invariants both execution paths rely on:
  * ``run_job`` (cron/sync) marks the job WHILE its func runs and clears it AFTER the row is
    finalized (so a status poll never reads a finished run as 已排入),
  * the mark is cleared on the FAILURE path too (the ``finally``), and the error message is
    recorded to ``job_runs.detail``,
  * ``run_job_func`` (manual/async daemon target) marks then clears the same way.
"""

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

import portfolio_dash.scheduler.jobs as J
from portfolio_dash.scheduler.jobs import (
    create_scheduler_tables,
    run_job,
    run_job_func,
    running_job_ids,
    start_job_run,
)

NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    """Guarantee cross-test isolation of the process-global in-flight set."""
    J._INFLIGHT_JOBS.clear()
    yield
    J._INFLIGHT_JOBS.clear()


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    create_scheduler_tables(c)
    return c


def _one_spec(fn: Callable[..., str]) -> dict[str, J.JobSpec]:
    return {"probe": J.JobSpec("probe", fn, "0 0 * * *", "Asia/Taipei", True, "probe job")}


def test_run_job_marks_inflight_during_func_and_clears_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _conn()
    seen: dict[str, Any] = {}

    def fn(_c: sqlite3.Connection, *, now: datetime) -> str:
        seen["during"] = "probe" in running_job_ids()
        return "ok detail"

    monkeypatch.setattr(J, "_jobs_by_id", lambda: _one_spec(fn))
    assert running_job_ids() == set()  # idle before
    rid = run_job(conn, "probe", now=NOW)
    assert seen["during"] is True          # marked 執行中 while the func ran
    assert running_job_ids() == set()      # cleared after the row was finalized
    row = conn.execute(
        "SELECT status, detail, finished_at FROM job_runs WHERE id=?", (rid,)
    ).fetchone()
    assert row["status"] == "ok" and row["detail"] == "ok detail" and row["finished_at"]


def test_run_job_clears_inflight_and_records_message_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _conn()

    def fn(_c: sqlite3.Connection, *, now: datetime) -> str:
        raise RuntimeError("boom in the job")

    monkeypatch.setattr(J, "_jobs_by_id", lambda: _one_spec(fn))
    rid = run_job(conn, "probe", now=NOW)
    assert running_job_ids() == set()      # cleared even on failure (the finally)
    row = conn.execute("SELECT status, detail FROM job_runs WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "error" and "boom in the job" in row["detail"]


def test_run_job_func_marks_then_clears(monkeypatch: pytest.MonkeyPatch) -> None:
    """The manual/async daemon target finalizes the pre-inserted 'running' row + clears."""
    conn = _conn()
    start_job_run(conn, "probe", now=NOW)  # the 已排入 row the /run endpoint inserts
    seen: dict[str, Any] = {}

    def fn(_c: sqlite3.Connection, *, now: datetime) -> str:
        seen["during"] = "probe" in running_job_ids()
        return "done"

    @contextmanager
    def _fake_session() -> Iterator[sqlite3.Connection]:
        yield conn  # reuse the test conn (do NOT close — the assertions read it after)

    monkeypatch.setattr(J, "_jobs_by_id", lambda: _one_spec(fn))
    monkeypatch.setattr(J, "session", _fake_session)

    run_job_func("probe", now=NOW)
    assert seen["during"] is True
    assert running_job_ids() == set()
    row = conn.execute(
        "SELECT status, detail, finished_at FROM job_runs "
        "WHERE job_id='probe' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["status"] == "ok" and row["detail"] == "done" and row["finished_at"]


def test_running_job_ids_returns_a_snapshot_copy() -> None:
    """Callers get a copy — mutating it must not corrupt the live registry."""
    J._mark_running("x")
    try:
        snap = running_job_ids()
        snap.add("y")
        assert "y" not in running_job_ids()  # snapshot is isolated
        assert running_job_ids() == {"x"}
    finally:
        J._clear_running("x")
