import contextlib
import sqlite3
from collections.abc import Iterator

import pytest

from portfolio_dash.scheduler import runtime
from portfolio_dash.scheduler.jobs import ensure_scheduler_seeded


def test_build_scheduler_adds_only_enabled_jobs(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    ensure_scheduler_seeded(conn)
    conn.execute("UPDATE schedule_config SET enabled=0 WHERE job_id='quotes_my'")
    conn.commit()

    @contextlib.contextmanager
    def fake_session() -> Iterator[sqlite3.Connection]:
        yield conn

    monkeypatch.setattr(runtime, "session", fake_session)
    scheduler = runtime.build_scheduler()
    ids = {j.id for j in scheduler.get_jobs()}
    assert "quotes_tw" in ids and "quotes_us" in ids
    assert "quotes_my" not in ids  # disabled row -> no trigger
