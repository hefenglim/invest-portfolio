"""Scheduler kind=insight dynamic dispatch (spec 04.2).

A ``schedule_config`` row with ``kind=insight`` is dispatched by running the REGISTERED
insight runner against its ``payload`` (the insight_type_id) — not via the static JOBS map.
The runner is registered by the app at startup (``register_insight_runner``); the scheduler
holds no api import. These tests register a fake runner and assert the dispatch wiring.
"""

import sqlite3
from datetime import UTC, datetime

import pytest

from portfolio_dash.scheduler import jobs


@pytest.fixture(autouse=True)
def _clear_runner() -> None:
    jobs.register_insight_runner(None)


def test_kind_insight_row_dispatches_to_registered_runner(conn: sqlite3.Connection) -> None:
    seen: list[int] = []

    def runner(c: sqlite3.Connection, insight_type_id: int, *, now: datetime) -> None:
        seen.append(insight_type_id)

    jobs.register_insight_runner(runner)
    jobs.bind_insight_schedule(conn, 42, cron="0 8 * * *")
    jobs.dispatch_job(conn, "insight:42", now=datetime(2026, 6, 11, tzinfo=UTC))
    assert seen == [42]


def test_dispatch_static_job_uses_registry(conn: sqlite3.Connection) -> None:
    # A non-insight (static) job still runs through the JOBS registry via run_job.
    run_id = jobs.dispatch_job(conn, "quotes_tw", now=datetime(2026, 6, 11, tzinfo=UTC))
    assert run_id is not None
    row = conn.execute(
        "SELECT job_id FROM job_runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert row["job_id"] == "quotes_tw"


def test_dispatch_insight_without_runner_is_noop(conn: sqlite3.Connection) -> None:
    # No runner registered (e.g. scheduler started before app wiring) → no crash.
    jobs.bind_insight_schedule(conn, 9, cron="0 8 * * *")
    jobs.dispatch_job(conn, "insight:9", now=datetime(2026, 6, 11, tzinfo=UTC))  # must not raise


def test_dispatch_unknown_job_id_is_logged_noop(conn: sqlite3.Connection) -> None:
    # H1 fix: a stale live trigger firing after its schedule row was deleted (task
    # deleted between restarts) must be a logged no-op, never a KeyError.
    assert jobs.dispatch_job(
        conn, "insight:404", now=datetime(2026, 6, 11, tzinfo=UTC)
    ) is None
    assert jobs.dispatch_job(
        conn, "totally_unknown_job", now=datetime(2026, 6, 11, tzinfo=UTC)
    ) is None


def test_dispatch_overlap_guard_skips_when_run_in_flight(conn: sqlite3.Connection) -> None:
    # M5 fix: a cron fire while a (manual) run of the same task is still unfinished
    # skips with a job_runs 'skipped' row (reason already_running) — no duplicate batch.
    seen: list[int] = []

    def runner(c: sqlite3.Connection, insight_type_id: int, *, now: datetime) -> None:
        seen.append(insight_type_id)

    jobs.register_insight_runner(runner)
    jobs.bind_insight_schedule(conn, 7, cron="0 8 * * *")
    # an in-flight run: latest row has finished_at NULL (the manual 202 path's shape).
    jobs.start_insight_run(conn, 7, now=datetime(2026, 6, 11, 8, 0, tzinfo=UTC))

    jobs.dispatch_job(conn, "insight:7", now=datetime(2026, 6, 11, 9, 0, tzinfo=UTC))

    assert seen == []  # the runner was NOT invoked
    row = conn.execute(
        "SELECT status, reason FROM job_runs WHERE job_id = 'insight:7' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["status"] == "skipped"
    assert row["reason"] == "already_running"


def test_trigger_job_uses_taipei_day_anchor_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    # M1 fix (decision Q6): the cron path's clock is Asia/Taipei — the SAME day anchor
    # as the API's get_now — so a cron run and a manual run of one Taipei trading day
    # produce identical day-anchored cache fingerprints.
    from datetime import timedelta

    captured: list[datetime] = []

    def fake_dispatch(c: sqlite3.Connection, job_id: str, *, now: datetime) -> None:
        captured.append(now)

    monkeypatch.setattr(jobs, "dispatch_job", fake_dispatch)
    jobs.trigger_job("quotes_tw")
    assert len(captured) == 1
    assert captured[0].tzinfo is not None
    assert captured[0].utcoffset() == timedelta(hours=8)  # Asia/Taipei, never UTC


def test_insight_runner_records_job_run_via_dispatch(conn: sqlite3.Connection) -> None:
    # The runner is responsible for the job_runs row; dispatch just invokes it.
    def runner(c: sqlite3.Connection, insight_type_id: int, *, now: datetime) -> None:
        c.execute(
            "INSERT INTO job_runs (job_id, started_at, finished_at, status, payload) "
            "VALUES (?, ?, ?, 'ok', ?)",
            (f"insight:{insight_type_id}", now.isoformat(), now.isoformat(),
             str(insight_type_id)),
        )
        c.commit()

    jobs.register_insight_runner(runner)
    jobs.bind_insight_schedule(conn, 3, cron="0 8 * * *")
    jobs.dispatch_job(conn, "insight:3", now=datetime(2026, 6, 11, tzinfo=UTC))
    row = conn.execute(
        "SELECT status, payload FROM job_runs WHERE job_id = 'insight:3'"
    ).fetchone()
    assert row["status"] == "ok"
    assert row["payload"] == "3"
