import sqlite3

from portfolio_dash.scheduler.jobs import create_scheduler_tables


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_new_columns_present() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_scheduler_tables(conn)
    assert {"kind", "payload"} <= _cols(conn, "schedule_config")
    assert {"payload", "reason", "cost_usd", "is_shadow"} <= _cols(conn, "job_runs")


def test_migration_idempotent_on_legacy_db() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # simulate a legacy DB: create base tables WITHOUT the new columns
    conn.executescript(
        "CREATE TABLE schedule_config (job_id TEXT PRIMARY KEY, enabled INTEGER, "
        "cron TEXT, timezone TEXT);"
        "CREATE TABLE job_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, "
        "started_at TEXT, finished_at TEXT, status TEXT, detail TEXT);"
    )
    create_scheduler_tables(conn)  # must add columns, not crash
    create_scheduler_tables(conn)  # idempotent second run
    assert {"kind", "payload"} <= _cols(conn, "schedule_config")
    assert {"payload", "reason", "cost_usd", "is_shadow"} <= _cols(conn, "job_runs")
