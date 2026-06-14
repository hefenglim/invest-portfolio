"""Schedule-binding helpers for insight_types (spec 4.2).

04a only persists the ``schedule_config`` binding (kind=insight, payload=insight_type_id)
and returns a deterministic job_id; the RUNTIME dispatch of kind=insight is 04b. No
APScheduler here — pure schedule_config row writes/deletes.
"""

import sqlite3

from portfolio_dash.scheduler.jobs import (
    JOBS,
    bind_insight_schedule,
    unbind_insight_schedule,
)


def test_bind_creates_insight_schedule_row(conn: sqlite3.Connection) -> None:
    job_id = bind_insight_schedule(conn, 7, cron="0 8 * * *")
    assert job_id == "insight:7"
    row = conn.execute(
        "SELECT job_id, enabled, cron, timezone, kind, payload FROM schedule_config "
        "WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert row["job_id"] == "insight:7"
    assert row["enabled"] == 1
    assert row["cron"] == "0 8 * * *"
    assert row["timezone"] == "Asia/Taipei"
    assert row["kind"] == "insight"
    assert row["payload"] == "7"


def test_bind_default_timezone_is_taipei(conn: sqlite3.Connection) -> None:
    bind_insight_schedule(conn, 1, cron="30 9 * * mon-fri")
    row = conn.execute(
        "SELECT timezone FROM schedule_config WHERE job_id = 'insight:1'"
    ).fetchone()
    assert row["timezone"] == "Asia/Taipei"


def test_bind_custom_timezone(conn: sqlite3.Connection) -> None:
    bind_insight_schedule(conn, 2, cron="0 8 * * *", tz="America/New_York")
    row = conn.execute(
        "SELECT timezone FROM schedule_config WHERE job_id = 'insight:2'"
    ).fetchone()
    assert row["timezone"] == "America/New_York"


def test_rebind_updates_cron_in_place(conn: sqlite3.Connection) -> None:
    bind_insight_schedule(conn, 3, cron="0 8 * * *")
    bind_insight_schedule(conn, 3, cron="0 20 * * *")
    rows = conn.execute(
        "SELECT cron FROM schedule_config WHERE job_id = 'insight:3'"
    ).fetchall()
    assert len(rows) == 1  # updated in place, not duplicated
    assert rows[0]["cron"] == "0 20 * * *"


def test_unbind_deletes_row(conn: sqlite3.Connection) -> None:
    bind_insight_schedule(conn, 4, cron="0 8 * * *")
    unbind_insight_schedule(conn, 4)
    row = conn.execute(
        "SELECT 1 FROM schedule_config WHERE job_id = 'insight:4'"
    ).fetchone()
    assert row is None


def test_unbind_absent_is_noop(conn: sqlite3.Connection) -> None:
    # Deleting a binding that was never created must not raise.
    unbind_insight_schedule(conn, 999)
    assert conn.execute(
        "SELECT 1 FROM schedule_config WHERE job_id = 'insight:999'"
    ).fetchone() is None


def test_insight_jobs_not_in_static_registry(conn: sqlite3.Connection) -> None:
    # Insight bindings are dynamic (payload-dispatched in 04b), never static JOBS.
    bind_insight_schedule(conn, 5, cron="0 8 * * *")
    assert all(j.id != "insight:5" for j in JOBS)
