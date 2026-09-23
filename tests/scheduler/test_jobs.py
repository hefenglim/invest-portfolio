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

    monkeypatch.setattr(jobs_mod, "default_registry", lambda conn=None: "REG")

    def fake_refresh(c, registry, instruments, fx_pairs, *, now, **_):  # type: ignore[no-untyped-def]
        captured["registry"] = registry
        captured["symbols"] = [i.symbol for i in instruments]
        captured["fx"] = len(fx_pairs)
        return _Summary()

    monkeypatch.setattr(jobs_mod, "refresh_quotes", fake_refresh)
    detail = quotes_us(conn, now=_NOW).detail  # M10-02: the job returns a JobOutcome
    assert captured["registry"] == "REG"
    assert captured["symbols"] == ["AAPL"]  # only US, not TW
    assert detail.startswith("1 項已更新") and "失敗" not in detail


def test_refresh_quotes_for_filters_by_market(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    _add(conn, "2330", "TW")
    monkeypatch.setattr(jobs_mod, "default_registry", lambda conn=None: "REG")
    seen: dict[str, object] = {}

    def fake_refresh(c, registry, instruments, fx_pairs, *, now, **_):  # type: ignore[no-untyped-def]
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


def test_summarize_names_sources_and_failures() -> None:
    """Item 8 (2026-07-03): the run detail must say WHICH source answered WHAT."""
    from datetime import UTC, datetime

    from portfolio_dash.pricing.results import RefreshSummary
    from portfolio_dash.scheduler.jobs import _summarize

    s = RefreshSummary(ok={"2330": "twse", "2603": "twse", "AAPL": "yfinance"},
                       failed=["8299"], fetched_at=datetime(2026, 7, 3, tzinfo=UTC))
    out = _summarize(s)
    # DEF-030 follow-up (2026-09-23, coordinator ruling): the zh sentence, full-width marks.
    assert out == "3 項已更新，1 項失敗（來源 twse：2330、2603；yfinance：AAPL）；失敗：8299"


def test_summarize_prints_a_recorded_reason_and_never_invents_one() -> None:
    """A failed key carries its reason when the fetch recorded one (DEF-015's field); a key
    without one is printed bare — never 「原因不明」 or any other invented text."""
    from datetime import UTC, datetime

    from portfolio_dash.pricing.results import RefreshSummary
    from portfolio_dash.scheduler.jobs import _summarize

    s = RefreshSummary(ok={}, failed=["TSLA", "AAPL"],
                       failed_reasons={"TSLA": "yfinance 逾時"},
                       fetched_at=datetime(2026, 9, 23, tzinfo=UTC))
    assert _summarize(s) == "0 項已更新，2 項失敗；失敗：AAPL；TSLA：yfinance 逾時"
    clean = RefreshSummary(ok={f"S{i:02d}": "twse" for i in range(10)},
                           fetched_at=datetime(2026, 9, 23, tzinfo=UTC))
    out = _summarize(clean)
    assert out.startswith("10 項已更新（來源 twse：S00、S01") and out.endswith(" 等 10 項）")
    assert not any(c in out for c in ",;:[]"), f"a half-width mark survived: {out!r}"
