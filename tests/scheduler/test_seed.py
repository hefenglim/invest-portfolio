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
    row = conn.execute("SELECT * FROM schedule_config WHERE job_id='quotes_tw'").fetchone()
    assert row["timezone"] == "Asia/Taipei" and row["enabled"] == 1


def test_seed_is_idempotent_and_preserves_edits(conn: sqlite3.Connection) -> None:
    ensure_scheduler_seeded(conn)
    conn.execute("UPDATE schedule_config SET cron='9 9 * * *', enabled=0 WHERE job_id='quotes_tw'")
    conn.commit()
    ensure_scheduler_seeded(conn)  # re-run must not clobber the edit
    row = conn.execute(
        "SELECT cron, enabled FROM schedule_config WHERE job_id='quotes_tw'"
    ).fetchone()
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
