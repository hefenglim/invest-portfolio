import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from portfolio_dash.scheduler.jobs import create_scheduler_tables, log_export_run


def test_log_export_run_writes_namespaced_job_runs_row() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_scheduler_tables(conn)
    now = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))
    run_id = log_export_run(conn, "holdings", now=now, detail="rows=2 bytes=128")
    row = conn.execute("SELECT * FROM job_runs WHERE id = ?", (run_id,)).fetchone()
    assert row["job_id"] == "export:holdings"
    assert row["status"] == "ok"
    assert row["started_at"] == now.isoformat()
    assert row["finished_at"] == now.isoformat()
    assert row["detail"] == "rows=2 bytes=128"
