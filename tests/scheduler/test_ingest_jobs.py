"""Tests for the spec-20.4 external-snapshot ingest scheduler jobs.

Each job is registered with the spec-15 runner; the symbol universe is read by
direct SQL (no data_ingestion import in scheduler/jobs.py). On 3 consecutive failed
runs a job upserts ``data_source_health`` to ``error`` and records a warn. No network.
"""

import sqlite3
from datetime import UTC, datetime

import pytest

from portfolio_dash.pricing import (
    consensus_source,
    datasources_store,
    sentiment_source,
    snapshots_store,
)
from portfolio_dash.scheduler import jobs as jobs_mod
from portfolio_dash.scheduler.jobs import JOBS, run_job

_NOW = datetime(2026, 6, 11, 18, 0, tzinfo=UTC)


@pytest.fixture
def conn(conn: sqlite3.Connection) -> sqlite3.Connection:  # extend scheduler conftest conn
    snapshots_store.ensure_tables(conn)
    datasources_store.create_tables(conn)
    return conn


def _add_tw(conn: sqlite3.Connection, symbol: str) -> None:
    conn.execute(
        "INSERT INTO instruments (symbol, market, quote_ccy, sector, name, board) "
        "VALUES (?, 'TW', 'TWD', NULL, NULL, NULL)",
        (symbol,),
    )
    conn.commit()


def _health_status(conn: sqlite3.Connection, source_id: str) -> str | None:
    """Read data_source_health.status directly (no data_sources row required)."""
    row = conn.execute(
        "SELECT status FROM data_source_health WHERE source_id = ?", (source_id,)
    ).fetchone()
    return row["status"] if row is not None else None


def test_ingest_jobs_registered() -> None:
    ids = {j.id for j in JOBS}
    assert {
        "finmind_chips_daily",
        "finmind_valuation_daily",
        "finmind_fundamentals_monthly",
        "sentiment_daily",
        "index_quotes_daily",
        "consensus_daily",
    } <= ids


def test_consensus_job_writes_snapshot(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    from datetime import date

    _add_tw(conn, "2330")

    def fake_fetch(yf_sym: str, *, as_of: date) -> dict[str, object]:
        assert yf_sym == "2330.TW"  # yf mapping reused
        return {"as_of": as_of.isoformat(), "ratings": {"total": 33},
                "rating_score": "1.76", "source": "yfinance"}

    monkeypatch.setattr(consensus_source, "fetch_consensus", fake_fetch)
    rid = run_job(conn, "consensus_daily", now=_NOW)
    assert rid > 0
    row = conn.execute("SELECT status FROM job_runs WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "ok"
    snap = snapshots_store.latest_snapshot(
        conn, source="yfinance", dataset="consensus", symbol="2330"
    )
    assert snap is not None and snap.payload["rating_score"] == "1.76"


# The ONE authorised ``data_ingestion`` import in ``scheduler/jobs.py``.
#
# W6a/D17 (corporate-actions spec §5.1, "Consequences for the plan"): the price write
# seam needs the corporate-action ratio, ``pricing/`` may not import ``data_ingestion``
# at all, and the two rejected alternatives were worse — option A adds a
# ``pricing → data_ingestion`` edge that ``architecture.md`` does not have, option B puts
# ledger SQL in ``shared/``, which holds no I/O and would break §6.0's "no module writes
# its own SELECT". Option C injects a callable, and this layer is where the ledger and
# the seam can legally meet.
#
# The spec author accepted this edge explicitly ("it is not free, and the implementer
# should not be surprised by it") but did not know THIS test existed. The guard is
# therefore NARROWED, not deleted: any other data_ingestion import still fails, so the
# exception stays one line wide and visible in a diff.
_AUTHORISED_DATA_INGESTION_IMPORTS = {
    "from portfolio_dash.data_ingestion.store import list_corporate_actions",
}


def test_scheduler_jobs_no_unauthorised_data_ingestion_import() -> None:
    import inspect

    src = inspect.getsource(jobs_mod)
    # Layering (architecture.md): no IMPORT of data_ingestion (a doc-comment mention is ok)
    # beyond the single W6a/D17 exception recorded above.
    import_lines = [
        ln.strip() for ln in src.splitlines()
        if ln.lstrip().startswith(("import ", "from ")) and "data_ingestion" in ln
    ]
    unauthorised = [ln for ln in import_lines if ln not in _AUTHORISED_DATA_INGESTION_IMPORTS]
    assert not unauthorised, (
        f"scheduler/jobs.py must not import data_ingestion: {unauthorised}"
    )


def test_the_authorised_import_is_actually_present() -> None:
    """The allowlist must not outlive what it authorises.

    An exception nobody uses is an exception nobody notices — it would silently widen
    the guard for whatever import happens to match it next."""
    import inspect

    src = inspect.getsource(jobs_mod)
    lines = {ln.strip() for ln in src.splitlines()}
    assert _AUTHORISED_DATA_INGESTION_IMPORTS <= lines


def test_sentiment_job_writes_snapshot(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    from decimal import Decimal

    monkeypatch.setattr(sentiment_source, "fetch_vix", lambda: Decimal("14.2"))
    monkeypatch.setattr(
        sentiment_source, "fetch_fear_greed",
        lambda: {"score": Decimal("60"), "rating": "greed"},
    )
    rid = run_job(conn, "sentiment_daily", now=_NOW)
    assert rid > 0
    row = conn.execute(
        "SELECT status, detail FROM job_runs WHERE id=?", (rid,)
    ).fetchone()
    assert row["status"] == "ok"
    assert snapshots_store.latest_snapshot(
        conn, source="sentiment", dataset="vix", symbol=None
    ) is not None


def test_chips_job_uses_direct_sql_universe(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    _add_tw(conn, "2330")
    seen: list[str] = []

    def fake_dataset(c: sqlite3.Connection, *, dataset: str, data_id: str,
                     start_date: str) -> list[dict[str, object]]:
        seen.append(data_id)
        return [{"date": "2026-06-11", "buy": 1, "sell": 0}]

    monkeypatch.setattr(jobs_mod.ingest, "fetch_dataset", fake_dataset)
    run_job(conn, "finmind_chips_daily", now=_NOW)
    assert "2330" in seen


def test_three_consecutive_failures_warns_and_marks_health(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    # Force the underlying ingest to raise so each run records an error.
    def boom(*a: object, **k: object) -> int:
        raise RuntimeError("finmind down")

    monkeypatch.setattr(jobs_mod.ingest, "ingest_chips", boom)

    import logging

    caplog.set_level(logging.WARNING)
    # First two failures: no health-error escalation yet.
    run_job(conn, "finmind_chips_daily", now=_NOW)
    run_job(conn, "finmind_chips_daily", now=_NOW)
    assert _health_status(conn, "finmind") != "error"

    # Third consecutive failure escalates: health -> error + a warn is logged.
    run_job(conn, "finmind_chips_daily", now=_NOW)
    assert _health_status(conn, "finmind") == "error"
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


def test_tier_error_marks_health_immediately_no_snapshot(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    """A FinMindTierError on the first run (no 3-streak needed) marks finmind health
    error with the reason, writes NO snapshot, and records the job failure."""
    from portfolio_dash.pricing.finmind_datasets import FinMindTierError

    _add_tw(conn, "2330")

    def tier_fail(c: sqlite3.Connection, *, dataset: str, data_id: str,
                  start_date: str) -> list[dict[str, object]]:
        raise FinMindTierError("需要 Backer 方案", required_tier="backer")

    monkeypatch.setattr(jobs_mod.ingest, "fetch_dataset", tier_fail)
    rid = run_job(conn, "finmind_chips_daily", now=_NOW)
    # job_runs records the failure.
    row = conn.execute("SELECT status, detail FROM job_runs WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "error"
    # health is marked error immediately (no 3-streak), carrying the reason.
    assert _health_status(conn, "finmind") == "error"
    detail = conn.execute(
        "SELECT detail FROM data_source_health WHERE source_id='finmind'"
    ).fetchone()["detail"]
    assert "Backer" in detail
    # NO snapshot was written.
    assert snapshots_store.latest_snapshot(
        conn, source="finmind", dataset="institutional", symbol="2330"
    ) is None


def test_quota_error_marks_health_immediately(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    from portfolio_dash.pricing.finmind_datasets import FinMindQuotaError

    _add_tw(conn, "2330")

    def quota_fail(c: sqlite3.Connection, *, dataset: str, data_id: str,
                   start_date: str) -> list[dict[str, object]]:
        raise FinMindQuotaError(
            "Requests reach the upper limit. https://finmindtrade.com/"
        )

    monkeypatch.setattr(jobs_mod.ingest, "fetch_dataset", quota_fail)
    run_job(conn, "finmind_chips_daily", now=_NOW)
    assert _health_status(conn, "finmind") == "error"
    detail = conn.execute(
        "SELECT detail FROM data_source_health WHERE source_id='finmind'"
    ).fetchone()["detail"]
    assert "upper limit" in detail


def test_failure_streak_resets_on_success(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    # Two failures, then a success, then two more failures => no escalation
    # (the success broke the streak so we never reach 3 consecutive).
    calls = {"n": 0}

    def flaky(c: sqlite3.Connection, *, now: datetime, fetch_dataset: object = None) -> int:
        calls["n"] += 1
        if calls["n"] == 3:
            return 0  # success on the 3rd call
        raise RuntimeError("flaky")

    monkeypatch.setattr(jobs_mod.ingest, "ingest_chips", flaky)
    for _ in range(5):
        run_job(conn, "finmind_chips_daily", now=_NOW)
    assert _health_status(conn, "finmind") != "error"


# --- fundamentals union jobs (W3, AI-D16) --------------------------------------------


def test_fundamentals_jobs_registered() -> None:
    ids = {j.id for j in JOBS}
    assert {"fundamentals_daily", "fundamentals_av_weekly"} <= ids


def test_fundamentals_daily_runs_yfinance_and_finnhub_only(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    """The daily leg is yfinance + finnhub across the whole universe (AV is the weekly)."""
    seen: dict[str, object] = {}

    def fake_union(conn: sqlite3.Connection, *, now: datetime, **kwargs: object) -> int:
        seen.update(kwargs)
        return 2

    monkeypatch.setattr(jobs_mod.ingest, "ingest_fundamentals_union", fake_union)
    rid = run_job(conn, "fundamentals_daily", now=_NOW)
    assert rid > 0
    assert seen.get("sources") == ("yfinance", "finnhub")
    row = conn.execute("SELECT status FROM job_runs WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "ok"


def test_fundamentals_av_weekly_no_runner_is_a_safe_noop(conn: sqlite3.Connection) -> None:
    jobs_mod.register_fundamentals_runner(None)
    rid = run_job(conn, "fundamentals_av_weekly", now=_NOW)
    assert rid > 0
    row = conn.execute("SELECT status FROM job_runs WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "ok"  # a missing runner is a no-op, not an error


def test_fundamentals_av_weekly_dispatches_the_registered_runner(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    called = {"n": 0}

    def stub_runner(conn: sqlite3.Connection, *, now: datetime) -> int:
        called["n"] += 1
        return 5

    jobs_mod.register_fundamentals_runner(stub_runner)
    try:
        rid = run_job(conn, "fundamentals_av_weekly", now=_NOW)
    finally:
        jobs_mod.register_fundamentals_runner(None)
    assert called["n"] == 1
    row = conn.execute("SELECT status, detail FROM job_runs WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "ok"
    assert "5 snapshot" in (row["detail"] or "")
