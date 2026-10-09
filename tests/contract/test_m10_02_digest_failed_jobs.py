"""M10-02 — the digests' ``failed_jobs`` counts a ``partial`` run (owner ruling #4).

Both digest chores blocks counted ``status = 'error'`` only, so a quote refresh that lost
every holding was a clean bill of health. A partial run is not a success; it is counted.
Counter-proof: with no partial rows the number is unchanged.
"""

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from portfolio_dash.api.digest_service import _chores, _data_health
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.shared.enums import Currency

_NOW = datetime(2026, 6, 11, 18, 0, tzinfo=ZoneInfo("Asia/Taipei"))


def _run(conn: sqlite3.Connection, job_id: str, status: str, started: str) -> None:
    conn.execute(
        "INSERT INTO job_runs (job_id, started_at, finished_at, status, detail) "
        "VALUES (?, ?, ?, ?, 'x')",
        (job_id, started, started, status),
    )
    conn.commit()


def test_data_health_counts_partial_and_error(golden_db: sqlite3.Connection) -> None:
    _run(golden_db, "quotes_us", "ok", "2026-06-11T17:30:00+08:00")
    assert _data_health(golden_db, [], now=_NOW)["failed_jobs"] == 0
    _run(golden_db, "quotes_tw", "error", "2026-06-11T14:00:00+08:00")
    assert _data_health(golden_db, [], now=_NOW)["failed_jobs"] == 1
    _run(golden_db, "quotes_my", "partial", "2026-06-11T17:40:00+08:00")
    assert _data_health(golden_db, [], now=_NOW)["failed_jobs"] == 2


def test_data_health_ignores_skipped_running_and_other_days(
    golden_db: sqlite3.Connection,
) -> None:
    _run(golden_db, "quotes_us", "skipped", "2026-06-11T17:30:00+08:00")
    _run(golden_db, "quotes_us", "running", "2026-06-11T17:31:00+08:00")
    _run(golden_db, "quotes_us", "partial", "2026-06-10T17:31:00+08:00")  # yesterday
    assert _data_health(golden_db, [], now=_NOW)["failed_jobs"] == 0


def test_chores_counts_partial_in_the_week(golden_db: sqlite3.Connection) -> None:
    data = build_dashboard(golden_db, now=_NOW, reporting=Currency.TWD)
    assert _chores(golden_db, data, now=_NOW)["failed_jobs"] == 0
    _run(golden_db, "quotes_us", "partial", "2026-06-09T17:30:00+08:00")
    assert _chores(golden_db, data, now=_NOW)["failed_jobs"] == 1
    _run(golden_db, "quotes_us", "partial", "2026-05-20T17:30:00+08:00")  # outside 7d
    assert _chores(golden_db, data, now=_NOW)["failed_jobs"] == 1
