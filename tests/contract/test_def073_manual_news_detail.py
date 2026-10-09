"""DEF-073 / DEF-067 (R6 leftover, owner ruling 2026-09-26): the MANUAL news run's detail.

``POST /api/news/run``'s worker (``api/routers/news.py::_news_run_worker``) wrote
「manual: organized 2, headline 1, skipped 3 over 2 symbol(s) (budget stop)」 under a 成功
chip — the English the nightly job had just lost, and a budget stop that read as success.
It now renders through the SAME ``scheduler.jobs.news_run_outcome`` as ``news_daily``.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.api import news_service
from portfolio_dash.api.routers import news as news_router
from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.scheduler.jobs import create_scheduler_tables, start_job_run

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=ZoneInfo("Asia/Taipei"))


@pytest.fixture
def conn(monkeypatch: pytest.MonkeyPatch) -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    create_scheduler_tables(c)

    @contextmanager
    def _session() -> Iterator[sqlite3.Connection]:
        yield c  # the worker opens its own session; hand it this one, never the real DB

    monkeypatch.setattr(news_router, "session", _session)
    yield c
    c.close()


def _run(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
         result: dict[str, Any]) -> sqlite3.Row:
    monkeypatch.setattr(news_service, "run_news_for", lambda c, universe, *, now: result)
    run_id = start_job_run(conn, "news_daily", now=NOW)
    news_router._news_run_worker([("2330", "TW"), ("AAPL", "US")], now=NOW, job_id="news_daily")
    row: sqlite3.Row = conn.execute(
        "SELECT status, detail FROM job_runs WHERE id = ?", (run_id,)).fetchone()
    return row


def test_a_manual_run_writes_the_nightly_sentence(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _run(conn, monkeypatch, {"organized": 2, "headline_only": 1, "skipped_existing": 3,
                                   "refetched": 0, "stopped_budget": False})
    assert (row["status"], row["detail"]) == (
        "ok", "2 檔標的：AI 整理 2 則，僅存標題 1 則，已收錄略過 3 則")


def test_a_manual_budget_stop_is_partial(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _run(conn, monkeypatch, {"organized": 0, "headline_only": 1, "skipped_existing": 0,
                                   "refetched": 0, "stopped_budget": True})
    assert row["status"] == "partial"
    assert row["detail"].endswith("；AI 額度用盡，提前結束")
