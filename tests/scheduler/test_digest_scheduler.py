"""Scheduler-side tests for the digest jobs (P3 batch 3 · Wave 1): JobSpecs + runner seam.

The scheduler never imports ``api``: it dispatches to a runner the app registers at
startup. These tests assert the two static JobSpecs are registered with the spec'd
cron/tz/enabled, that ``ensure_scheduler_seeded`` gives each a ``schedule_config`` row,
and that the ``register_digest_runner`` seam routes ``digest_daily``/``digest_weekly`` to
the registered fn with the right ``kind`` (and no-ops safely when unregistered).
"""

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.scheduler import jobs

NOW = datetime(2026, 7, 14, 15, 0, tzinfo=ZoneInfo("Asia/Taipei"))


@pytest.fixture(autouse=True)
def _restore_runner() -> Iterator[None]:
    """Never leak a registered digest runner into other tests (module global)."""
    yield
    jobs.register_digest_runner(None)


def test_digest_jobs_registered_with_spec_cron() -> None:
    by_id = {j.id: j for j in jobs.JOBS}
    assert "digest_daily" in by_id and "digest_weekly" in by_id
    d = by_id["digest_daily"]
    assert d.default_cron == "10 15 * * mon-fri"
    assert d.default_timezone == "Asia/Taipei" and d.default_enabled is True
    w = by_id["digest_weekly"]
    assert w.default_cron == "0 17 * * sun"
    assert w.default_timezone == "Asia/Taipei" and w.default_enabled is True


def test_digest_jobs_get_schedule_rows(conn: sqlite3.Connection) -> None:
    jobs.ensure_scheduler_seeded(conn)
    ids = {r["job_id"] for r in conn.execute("SELECT job_id FROM schedule_config")}
    assert {"digest_daily", "digest_weekly"} <= ids


def test_runner_seam_routes_by_kind(conn: sqlite3.Connection) -> None:
    calls: list[str] = []

    def fake(c: sqlite3.Connection, kind: str, *, now: datetime) -> str:
        calls.append(kind)
        return f"ran {kind}"

    jobs.register_digest_runner(fake)
    assert jobs.digest_daily(conn, now=NOW) == "ran daily"
    assert jobs.digest_weekly(conn, now=NOW) == "ran weekly"
    assert calls == ["daily", "weekly"]


def test_no_runner_is_safe_noop(conn: sqlite3.Connection) -> None:
    jobs.register_digest_runner(None)
    assert jobs.digest_daily(conn, now=NOW) == "no digest runner registered"
    assert jobs.digest_weekly(conn, now=NOW) == "no digest runner registered"
