import sqlite3
from datetime import UTC, datetime

import pytest

from portfolio_dash.scheduler import jobs as jobs_mod
from portfolio_dash.scheduler.jobs import quotes_us, refresh_quotes_for
from portfolio_dash.shared.enums import Market

_NOW = datetime(2026, 6, 10, tzinfo=UTC)


class _Summary:
    def __init__(self) -> None:
        self.ok = {"AAPL": "yfinance"}
        self.failed: list[str] = []


def _add(conn: sqlite3.Connection, symbol: str, market: str) -> None:
    conn.execute(
        "INSERT INTO instruments (symbol, market, quote_ccy, sector, name, board) "
        "VALUES (?, ?, 'X', NULL, NULL, NULL)",
        (symbol, market),
    )
    conn.commit()


def test_quotes_job_passes_market_worklist(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    _add(conn, "AAPL", "US")
    _add(conn, "2330", "TW")
    captured: dict[str, object] = {}

    monkeypatch.setattr(jobs_mod, "default_registry", lambda: "REG")

    def fake_refresh(c, registry, instruments, fx_pairs, *, now):  # type: ignore[no-untyped-def]
        captured["registry"] = registry
        captured["symbols"] = [i.symbol for i in instruments]
        captured["fx"] = len(fx_pairs)
        return _Summary()

    monkeypatch.setattr(jobs_mod, "refresh_quotes", fake_refresh)
    detail = quotes_us(conn, now=_NOW)
    assert captured["registry"] == "REG"
    assert captured["symbols"] == ["AAPL"]  # only US, not TW
    assert "1 ok" in detail and "0 failed" in detail


def test_refresh_quotes_for_filters_by_market(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    _add(conn, "2330", "TW")
    monkeypatch.setattr(jobs_mod, "default_registry", lambda: "REG")
    seen: dict[str, object] = {}

    def fake_refresh(c, registry, instruments, fx_pairs, *, now):  # type: ignore[no-untyped-def]
        seen["symbols"] = [i.symbol for i in instruments]
        return _Summary()

    monkeypatch.setattr(jobs_mod, "refresh_quotes", fake_refresh)
    refresh_quotes_for(conn, Market.TW, now=_NOW)
    assert seen["symbols"] == ["2330"]


from portfolio_dash.scheduler.jobs import JobSpec, run_job  # noqa: E402


def _register(monkeypatch: pytest.MonkeyPatch, spec: JobSpec) -> None:
    monkeypatch.setattr(jobs_mod, "JOBS", [*jobs_mod.JOBS, spec])


def test_run_job_logs_ok(monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection) -> None:
    spec = JobSpec("ok_job", lambda c, *, now: "did 3", "0 0 * * *", "UTC", True, "")
    _register(monkeypatch, spec)
    run_job(conn, "ok_job", now=_NOW)
    row = conn.execute(
        "SELECT status, detail, finished_at FROM job_runs WHERE job_id='ok_job'"
    ).fetchone()
    assert row["status"] == "ok" and row["detail"] == "did 3" and row["finished_at"] is not None


def test_run_job_swallows_and_logs_error(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    def boom(c: sqlite3.Connection, *, now: datetime) -> str:
        raise RuntimeError("provider exploded")

    _register(monkeypatch, JobSpec("bad_job", boom, "0 0 * * *", "UTC", True, ""))
    run_job(conn, "bad_job", now=_NOW)  # must NOT raise
    row = conn.execute("SELECT status, detail FROM job_runs WHERE job_id='bad_job'").fetchone()
    assert row["status"] == "error" and "provider exploded" in row["detail"]


def test_run_job_returns_run_id(conn: sqlite3.Connection) -> None:
    rid = run_job(conn, "history_daily", now=datetime(2026, 6, 11, tzinfo=UTC))
    assert isinstance(rid, int) and rid > 0
    assert conn.execute("SELECT id FROM job_runs WHERE id=?", (rid,)).fetchone() is not None
