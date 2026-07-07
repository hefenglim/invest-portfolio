"""H2 fix (decision Q2a): run_for_id enforces enabled/archived at the execution seam.

Every trigger path (cron dispatch, manual run, on_alert) flows through
``insight_service.run_for_id`` — a disabled or archived task must SKIP generation and
record a ``job_runs`` skip row (reason ``task_disabled`` / ``task_archived``), never
generate or bill. The LLM seam would raise if reached (no model configured).
"""

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from typing import cast
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.api import insight_service
from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.llm_insight import insights_store as istore
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.scheduler.jobs import create_scheduler_tables
from portfolio_dash.shared.llm_config import ensure_llm_seeded

NOW = datetime(2026, 6, 14, 9, 0, tzinfo=ZoneInfo("Asia/Taipei"))


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    create_pricing_tables(c)
    create_scheduler_tables(c)
    cs.ensure_seeded(c)
    istore.ensure_tables(c)
    ensure_llm_seeded(c)
    yield c
    c.close()


def _combo(conn: sqlite3.Connection, *, enabled: bool = True) -> int:
    sp = cs.create_strategy(conn, name="S", body="{{kpis_json}}", now=NOW)
    it = cs.create_insight_type(
        conn, name="Daily", scope="portfolio", enabled=enabled, now=NOW
    )
    cs.set_strategies(conn, it.id, [(sp.id, 0)])
    return it.id


def _last_run(conn: sqlite3.Connection, it_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT status, reason, cost_usd FROM job_runs WHERE job_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (f"insight:{it_id}",),
    ).fetchone()
    assert row is not None
    return cast(sqlite3.Row, row)


def test_run_for_id_disabled_task_skips_and_records(conn: sqlite3.Connection) -> None:
    it_id = _combo(conn, enabled=False)
    result = insight_service.run_for_id(conn, it_id, now=NOW)
    assert result.status == "skipped"
    assert result.reason == "task_disabled"
    assert result.cards_created == 0
    row = _last_run(conn, it_id)
    assert row["status"] == "skipped"
    assert row["reason"] == "task_disabled"
    assert row["cost_usd"] == "0"


def test_run_for_id_archived_task_skips_and_records(conn: sqlite3.Connection) -> None:
    it_id = _combo(conn)
    cs.delete_insight_type(conn, it_id, now=NOW)  # archive (spec 4.1 delete)
    result = insight_service.run_for_id(conn, it_id, now=NOW)
    assert result.status == "skipped"
    assert result.reason == "task_archived"
    row = _last_run(conn, it_id)
    assert row["reason"] == "task_archived"


def test_run_for_id_disabled_finalizes_preinserted_run_row(
    conn: sqlite3.Connection,
) -> None:
    # The async manual path pre-inserts a running row; the skip must finalize THAT row.
    from portfolio_dash.scheduler.jobs import start_insight_run

    it_id = _combo(conn, enabled=False)
    run_id = start_insight_run(conn, it_id, now=NOW)
    insight_service.run_for_id(conn, it_id, now=NOW, run_id=run_id)
    row = conn.execute(
        "SELECT status, reason, finished_at FROM job_runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert row["status"] == "skipped"
    assert row["reason"] == "task_disabled"
    assert row["finished_at"] is not None
