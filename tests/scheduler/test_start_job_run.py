import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from portfolio_dash.scheduler.jobs import (
    create_scheduler_tables,
    latest_run_unfinished,
    start_job_run,
)

NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    create_scheduler_tables(c)
    return c


def test_start_job_run_inserts_running_row() -> None:
    conn = _conn()
    rid = start_job_run(conn, "quotes_tw", now=NOW)
    row = conn.execute("SELECT * FROM job_runs WHERE id=?", (rid,)).fetchone()
    assert row["job_id"] == "quotes_tw" and row["started_at"] == NOW.isoformat()
    # The row IS the 'running' marker (2026-07-05 fix: it used to insert status NULL,
    # so the runs API showed a blank status while a run was in flight).
    assert row["finished_at"] is None and row["status"] == "running"
    assert latest_run_unfinished(conn, "quotes_tw") is True


def test_latest_run_unfinished_false_when_finished() -> None:
    conn = _conn()
    rid = start_job_run(conn, "quotes_tw", now=NOW)
    conn.execute(
        "UPDATE job_runs SET finished_at=?, status='ok' WHERE id=?", (NOW.isoformat(), rid)
    )
    conn.commit()
    assert latest_run_unfinished(conn, "quotes_tw") is False
