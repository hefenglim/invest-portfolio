"""The in-process readers of history while it is stale (PERF spec §10 step 6), and the
``/api/performance/twr`` overlay cache (§12.2; plan §7.1, package G).

- ``strategy/alerts.py``'s ``portfolio_drawdown`` — a peak over the whole series, which
  pushes — is skipped (with a note in the log) while ``history_state`` is stale;
- ``api/insight_service._portfolio_return`` — which writes ``insight_evaluations`` and feeds
  promotion — returns its ``pending_data`` result when ``[created, due]`` overlaps the stale
  range, and scores as before when it does not;
- the TWR route answers from the cached build and keeps one answer per (benchmark, window).

Stores are built by the real worker (inline) on the reference corpora.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api import insight_service
from portfolio_dash.api.app import create_app
from portfolio_dash.api.deps import get_conn, get_now, get_reporting
from portfolio_dash.api.routers import performance
from portfolio_dash.portfolio import settlement_store as st
from portfolio_dash.portfolio.dashboard import _build_dashboard
from portfolio_dash.portfolio.dashboard_models import DashboardData, HistoryState
from portfolio_dash.shared import result_cache
from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.db import connect_path
from portfolio_dash.strategy.alerts import compute_alerts_from
from portfolio_dash.strategy.rules_config import DEFAULT_RULES, Rule
from tests.reference.corpora import CorpusDB, open_db
from tests.settlement.conftest import CorpusFactory
from tests.settlement.test_worker import prepare, start_run

pytestmark = pytest.mark.enable_socket


def _settled(corpus_db: CorpusFactory, name: str) -> CorpusDB:
    db = corpus_db(name)
    prepare(db)
    assert start_run(db, 1) == "published"
    return db


def _make_stale(db: CorpusDB) -> tuple[date, date]:
    """A close dated ~40 days before the live window, restated and put back: the triggers
    mark the store dirty from that day — ``[Dd, W − 1]`` is served as the previous generation
    (stale) — while the data, and so the true series, is unchanged. A consumer that skips or
    defers here does so because the history is LABELLED stale, not because it differs."""
    window = st.window_start(db.now.date())
    conn = open_db(db.path)
    try:
        row = conn.execute(
            "SELECT instrument, as_of_date, close, close_raw FROM prices WHERE as_of_date "
            "BETWEEN ? AND ? AND instrument IN (SELECT symbol FROM transactions) "
            "ORDER BY as_of_date, instrument LIMIT 1",
            ((window - timedelta(days=45)).isoformat(),
             (window - timedelta(days=30)).isoformat())).fetchone()
        assert row is not None
        conn.execute("UPDATE prices SET close = '1', close_raw = '1' WHERE instrument = ? "
                     "AND as_of_date = ?", (row[0], row[1]))
        conn.execute("UPDATE prices SET close = ?, close_raw = ? WHERE instrument = ? "
                     "AND as_of_date = ?", (row[2], row[3], row[0], row[1]))
        conn.commit()
    finally:
        conn.close()
    return date.fromisoformat(row[1]), window - timedelta(days=1)


def _build(db: CorpusDB) -> DashboardData:
    conn = open_db(db.path)
    try:
        return _build_dashboard(conn, now=db.now, reporting=db.reporting)
    finally:
        conn.close()


# ---------------------------------------------------------------------------------------------
# portfolio_drawdown
# ---------------------------------------------------------------------------------------------

_SENSITIVE = DEFAULT_RULES.model_copy(update={
    "portfolio_drawdown": Rule(enabled=True, value=Decimal("0.0001"))})


def _drawdown(data: DashboardData) -> list[Any]:
    alerts = compute_alerts_from(data, _SENSITIVE, quota_remaining=Decimal("100"),
                                 quota_threshold=Decimal("1"))
    return [a for a in alerts if a.rule == "portfolio_drawdown"]


def test_the_portfolio_drawdown_rule_is_skipped_while_history_is_stale(
        corpus_db: CorpusFactory, caplog: pytest.LogCaptureFixture) -> None:
    db = _settled(corpus_db, "rich")
    stale_from, stale_to = _make_stale(db)
    data = _build(db)
    assert (data.history_state.stale_from, data.history_state.stale_to) == (stale_from,
                                                                            stale_to)
    with caplog.at_level(logging.INFO, logger="portfolio_dash.strategy.alerts"):
        assert _drawdown(data) == []
    assert any("portfolio_drawdown skipped" in r.getMessage() for r in caplog.records)
    # the same series, its history not stale: the (deliberately sensitive) rule fires — so
    # the silence above is the skip, not a quiet series
    fresh = data.model_copy(update={"history_state": HistoryState()})
    assert _drawdown(fresh), "the probe series has no drawdown to skip"


def test_the_drawdown_rule_reads_a_clean_store_as_before(corpus_db: CorpusFactory) -> None:
    db = _settled(corpus_db, "rich")
    data = _build(db)
    assert data.history_state.live_from == db.now.date() and not data.history_state.stale
    assert _drawdown(data) == _drawdown(data.model_copy(
        update={"history_state": HistoryState()}))


# ---------------------------------------------------------------------------------------------
# _portfolio_return (insight scoring)
# ---------------------------------------------------------------------------------------------


def test_portfolio_return_defers_while_its_window_overlaps_the_stale_range(
        corpus_db: CorpusFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _settled(corpus_db, "rich")   # now 2026-06-10
    stale_from, stale_to = _make_stale(db)
    conn = open_db(db.path)
    try:
        def score(created: date, due: date) -> Decimal | None:
            return insight_service._portfolio_return(conn, created, due, now=db.now,
                                                     reporting=db.reporting)

        inside = (stale_from + timedelta(days=3), stale_to - timedelta(days=3))
        across = (stale_from - timedelta(days=20), stale_from + timedelta(days=2))
        after = (stale_to + timedelta(days=5), db.now.date() - timedelta(days=2))
        before = (stale_from - timedelta(days=60), stale_from - timedelta(days=20))
        assert score(*inside) is None
        assert score(*across) is None
        assert score(*after) is not None
        assert score(*before) is not None
        # the same windows with every day live: all four are scored
        monkeypatch.setenv("PD_SETTLE", "off")
        assert all(score(*w) is not None for w in (inside, across, after, before))
    finally:
        conn.close()


# ---------------------------------------------------------------------------------------------
# /api/performance/twr
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def twr_client(corpus_db: CorpusFactory, monkeypatch: pytest.MonkeyPatch
               ) -> Iterator[tuple[TestClient, CorpusDB]]:
    """``rich`` settled, as the application's database (the cache serves only that file)."""
    db = _settled(corpus_db, "rich")
    monkeypatch.setenv("DB_PATH", str(db.path))
    get_settings.cache_clear()
    result_cache.reset()
    app = create_app()

    def conn_per_request() -> Iterator[sqlite3.Connection]:
        conn = connect_path(db.path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    app.dependency_overrides[get_conn] = conn_per_request
    app.dependency_overrides[get_now] = lambda: db.now
    app.dependency_overrides[get_reporting] = lambda: db.reporting
    try:
        yield TestClient(app), db
    finally:
        app.dependency_overrides.clear()
        result_cache.reset()
        get_settings.cache_clear()


def _twr(client: TestClient, benchmark: str, window: str) -> Any:
    r = client.get("/api/performance/twr", params={"benchmark": benchmark, "window": window})
    assert r.status_code == 200, r.text
    return r.json()


def test_the_overlay_is_kept_per_benchmark_and_window(
        twr_client: tuple[TestClient, CorpusDB], monkeypatch: pytest.MonkeyPatch) -> None:
    client, db = twr_client
    calls: list[tuple[str, str]] = []
    real = performance._overlay

    def counted(conn: sqlite3.Connection, bench: Any, window: str, **kw: Any) -> Any:
        calls.append((bench.key, window))
        return real(conn, bench, window, **kw)

    monkeypatch.setattr(performance, "_overlay", counted)
    first = _twr(client, "0050", "1y")
    other = _twr(client, "sp500", "all")
    assert first["available"] and other["available"]
    assert _twr(client, "0050", "1y") == first
    assert _twr(client, "sp500", "all") == other   # two entries, neither evicted the other
    assert calls == [("0050", "1y"), ("sp500", "all")]
    stats = result_cache.stats()
    assert stats["twr:0050:1y"].hits == 1 and stats["twr:sp500:all"].hits == 1
    # a commit from another connection moves the database's version: computed afresh
    conn = open_db(db.path)
    try:
        conn.execute("INSERT INTO job_runs (job_id, started_at, status) "
                     "VALUES ('probe', '2026-06-10T00:00:00+08:00', 'ok')")
        conn.commit()
    finally:
        conn.close()
    assert _twr(client, "0050", "1y") == first
    assert calls[-1] == ("0050", "1y") and len(calls) == 3


def test_the_overlay_over_the_store_is_the_live_one(
        twr_client: tuple[TestClient, CorpusDB], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _db = twr_client
    stored = {(b, w): _twr(client, b, w) for b in ("0050", "sp500", "klci")
              for w in ("1y", "3y", "all")}
    monkeypatch.setenv("PD_SETTLE", "off")
    for (b, w), answer in stored.items():
        assert _twr(client, b, w) == answer, (b, w)
