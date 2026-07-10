"""signal_scan job registration + runner-seam dispatch (P2 batch 2).

The scan LOGIC lives in the api seam (api/signals_service.scan_signals); the scheduler only
triggers it through the registered runner (scheduler never imports api). These tests assert
the job exists, is callable via run_job, and dispatches to whatever runner the app wired.
"""

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.scheduler import jobs

NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    jobs.create_scheduler_tables(c)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _clear_runner() -> Iterator[None]:
    jobs.register_signal_scan_runner(None)
    yield
    jobs.register_signal_scan_runner(None)


def test_signal_scan_is_a_registered_job() -> None:
    assert any(j.id == "signal_scan" for j in jobs.JOBS)


def test_signal_scan_no_runner_is_safe_noop(conn: sqlite3.Connection) -> None:
    detail = jobs.signal_scan(conn, now=NOW)
    assert "no signal scan runner" in detail


def test_signal_scan_dispatches_to_registered_runner(conn: sqlite3.Connection) -> None:
    calls: list[datetime] = []

    def runner(c: sqlite3.Connection, *, now: datetime) -> str:
        calls.append(now)
        return "1 symbol(s), 1 seeded, 0 transition event(s)"

    jobs.register_signal_scan_runner(runner)
    detail = jobs.signal_scan(conn, now=NOW)
    assert calls == [NOW]
    assert "seeded" in detail


def test_signal_scan_runs_via_run_job(conn: sqlite3.Connection) -> None:
    jobs.ensure_job_rows(conn)
    jobs.register_signal_scan_runner(lambda c, *, now: "ok summary")
    run_id = jobs.run_job(conn, "signal_scan", now=NOW)
    row = conn.execute(
        "SELECT status, detail FROM job_runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert row["status"] == "ok"
    assert row["detail"] == "ok summary"
