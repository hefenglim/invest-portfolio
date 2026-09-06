"""M10-02 — a quote refresh that lost a HELD symbol is ``partial``, never ``ok``.

Owner rulings (2026-09-06):
  * a partial failure is not a success — the quote jobs reuse ``job_runs``' existing
    ``partial`` status (the insight runner already writes it; the DDL has no CHECK);
  * the threshold is "any HELD instrument failed" — a lost FX pair or a watchlist-only
    symbol is written into the detail but does not change the verdict;
  * the ``failed[:8]`` truncation in ``_summarize`` goes — ``detail`` is TEXT and the
    scheduler center already puts the full text in a tooltip.

The held set is a ``data_ingestion``/``portfolio`` computation that ``scheduler/`` may not
import (``architecture.md``; ``tests/scheduler/test_ingest_jobs.py`` guards the import
graph), so it reaches the job through a registered seam exactly like the fundamentals
runner. Unregistered, the verdict degrades CONSERVATIVELY (every instrument counts): a
process that forgot the wiring over-reports, it never hides a lost holding.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from portfolio_dash.scheduler import jobs as jobs_mod
from portfolio_dash.scheduler.jobs import (
    JobOutcome,
    JobSpec,
    _summarize,
    refresh_quotes_for,
    register_held_symbols_fn,
    run_job,
    run_job_func,
    start_job_run,
)
from portfolio_dash.shared.enums import Market

_NOW = datetime(2026, 9, 6, tzinfo=UTC)


class _Summary:
    """The shape ``refresh_quotes`` returns — ``ok`` keyed by symbol / pair, ``failed`` a
    list that (in the real thing) mixes bare keys with zh refusal lines."""

    def __init__(self, ok: dict[str, str], failed: list[str]) -> None:
        self.ok = ok
        self.failed = failed
        self.fetched_at = _NOW


def _add(conn: sqlite3.Connection, symbol: str, market: str) -> None:
    conn.execute(
        "INSERT INTO instruments (symbol, market, quote_ccy, sector, name, board) "
        "VALUES (?, ?, 'X', NULL, NULL, NULL)",
        (symbol, market),
    )
    conn.commit()


@pytest.fixture(autouse=True)
def _reset_seams(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(jobs_mod, "default_registry", lambda conn=None: "REG")
    register_held_symbols_fn(None)
    jobs_mod._INFLIGHT_JOBS.clear()
    yield
    register_held_symbols_fn(None)
    jobs_mod._INFLIGHT_JOBS.clear()


def _fake_refresh(monkeypatch: pytest.MonkeyPatch, summary: _Summary) -> None:
    def fake(c, registry, instruments, fx_pairs, *, now, **_):  # type: ignore[no-untyped-def]
        return summary

    monkeypatch.setattr(jobs_mod, "refresh_quotes", fake)


# --- the verdict ---------------------------------------------------------------


def test_partial_when_a_held_symbol_fails(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    _add(conn, "AAPL", "US")
    _add(conn, "NVDA", "US")
    register_held_symbols_fn(lambda c: {"AAPL"})
    _fake_refresh(monkeypatch, _Summary(ok={"NVDA": "yfinance", "USDTWD": "yfinance"},
                                        failed=["AAPL"]))
    out = refresh_quotes_for(conn, Market.US, now=_NOW)
    assert isinstance(out, JobOutcome)
    assert out.status == "partial"
    assert out.results["held_failed"] == ["AAPL"]
    assert "failed: AAPL" in out.detail


def test_ok_when_only_an_fx_pair_fails(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    """The threshold is a HELD INSTRUMENT: a lost pair is detail, not verdict."""
    _add(conn, "AAPL", "US")
    register_held_symbols_fn(lambda c: {"AAPL"})
    _fake_refresh(monkeypatch, _Summary(
        ok={"AAPL": "yfinance", "USDTWD": "y", "MYRTWD": "y"}, failed=["USDMYR"]))
    out = refresh_quotes_for(conn, Market.US, now=_NOW)
    assert out.status == "ok"
    assert out.results["held_failed"] == []
    assert out.results["fx_failed"] == ["USDMYR"]
    assert "failed: USDMYR" in out.detail  # still disclosed in the detail


def test_ok_when_only_a_watchlist_symbol_fails(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    _add(conn, "AAPL", "US")
    _add(conn, "WATCH", "US")
    register_held_symbols_fn(lambda c: {"AAPL"})
    _fake_refresh(monkeypatch, _Summary(ok={"AAPL": "yfinance"}, failed=["WATCH"]))
    out = refresh_quotes_for(conn, Market.US, now=_NOW)
    assert out.status == "ok"
    assert out.results["instruments_failed"] == ["WATCH"]
    assert out.results["held_failed"] == []


def test_ok_when_nothing_fails(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    """Counter-proof: an all-green run is still ``ok`` — not everything became partial."""
    _add(conn, "AAPL", "US")
    register_held_symbols_fn(lambda c: {"AAPL"})
    _fake_refresh(monkeypatch, _Summary(
        ok={"AAPL": "yfinance", "USDTWD": "y", "USDMYR": "y", "MYRTWD": "y"}, failed=[]))
    out = refresh_quotes_for(conn, Market.US, now=_NOW)
    assert out.status == "ok"
    assert out.results["instruments_failed"] == [] and out.results["fx_failed"] == []


def test_unregistered_held_seam_counts_every_instrument(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    """No held-fn wired → conservative: a lost instrument is partial (never hidden)."""
    _add(conn, "WATCH", "US")
    _fake_refresh(monkeypatch, _Summary(ok={}, failed=["WATCH"]))
    out = refresh_quotes_for(conn, Market.US, now=_NOW)
    assert out.status == "partial"
    assert out.results["held_failed"] == ["WATCH"]


def test_held_failed_comes_from_the_worklist_not_the_detail_string(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    """A refused close lands in ``failed`` as a zh LINE, not a key; the verdict must still
    see the symbol — it is derived from the worklist minus ``ok``, never parsed."""
    _add(conn, "2330", "TW")
    register_held_symbols_fn(lambda c: {"2330"})
    _fake_refresh(monkeypatch, _Summary(
        ok={"USDTWD": "y"}, failed=["2330：收盤價非正數（0），已拒絕寫入"]))
    out = refresh_quotes_for(conn, Market.TW, now=_NOW)
    assert out.status == "partial" and out.results["held_failed"] == ["2330"]


# --- the record -----------------------------------------------------------------


def _register(monkeypatch: pytest.MonkeyPatch, spec: JobSpec) -> None:
    monkeypatch.setattr(jobs_mod, "JOBS", [*jobs_mod.JOBS, spec])


def test_run_job_records_partial_status(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    spec = JobSpec("p_job", lambda c, *, now: JobOutcome("partial", "1 ok, 1 failed"),
                   "0 0 * * *", "UTC", True, "")
    _register(monkeypatch, spec)
    run_job(conn, "p_job", now=_NOW)
    row = conn.execute(
        "SELECT status, detail, finished_at FROM job_runs WHERE job_id='p_job'"
    ).fetchone()
    assert row["status"] == "partial" and row["detail"] == "1 ok, 1 failed"
    assert row["finished_at"] is not None


def test_run_job_plain_string_is_still_ok(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    """Every other job keeps returning a bare string and keeps logging ``ok``."""
    _register(monkeypatch, JobSpec("s_job", lambda c, *, now: "did 3", "0 0 * * *", "UTC",
                                   True, ""))
    run_job(conn, "s_job", now=_NOW)
    row = conn.execute("SELECT status, detail FROM job_runs WHERE job_id='s_job'").fetchone()
    assert row["status"] == "ok" and row["detail"] == "did 3"


def test_run_job_func_records_partial_status(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    jobs_mod.create_scheduler_tables(conn)
    start_job_run(conn, "probe", now=_NOW)

    def fn(_c: sqlite3.Connection, *, now: datetime) -> JobOutcome:
        return JobOutcome("partial", "0 ok, 1 failed failed: AAPL")

    @contextmanager
    def _fake_session() -> Iterator[sqlite3.Connection]:
        yield conn

    spec: dict[str, Any] = {"probe": JobSpec("probe", fn, "0 0 * * *", "UTC", True, "")}
    monkeypatch.setattr(jobs_mod, "_jobs_by_id", lambda: spec)
    monkeypatch.setattr(jobs_mod, "session", _fake_session)
    run_job_func("probe", now=_NOW)
    row = conn.execute(
        "SELECT status, detail FROM job_runs WHERE job_id='probe' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["status"] == "partial" and row["detail"] == "0 ok, 1 failed failed: AAPL"


# --- the detail -----------------------------------------------------------------


def test_summarize_keeps_the_whole_failed_list() -> None:
    """18 failures used to store 8 names and an ellipsis — the other 10 were never
    written anywhere. ``detail`` is TEXT; the tooltip shows the full text."""
    from portfolio_dash.pricing.results import RefreshSummary

    failed = [f"SYM{i:02d}" for i in range(18)]
    out = _summarize(RefreshSummary(ok={"AAPL": "yfinance"}, failed=failed, fetched_at=_NOW))
    assert "…" not in out.split("failed: ")[1]
    for key in failed:
        assert key in out
