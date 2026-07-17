"""Scheduler: the daily history job + smart backfill include the FU-D27 benchmarks.

Benchmarks (0050 / ^GSPC) are fetched via the SAME ``refresh_history`` path as instruments
but are NOT in the instrument worklist — the jobs add ``benchmark_refs()`` explicitly. These
tests monkeypatch ``refresh_history`` to capture the refs each call receives and assert the
benchmark storage keys appear, and that a benchmark fetch failure degrades silently.
"""

import sqlite3
from datetime import UTC, date, datetime

import pytest

from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.pricing.results import RefreshSummary
from portfolio_dash.scheduler import jobs as jobs_mod
from portfolio_dash.scheduler.jobs import backfill_history_all, history_daily

_NOW = datetime(2026, 6, 3, tzinfo=UTC)
_BENCH = {"0050", "^GSPC"}


def _inst(conn: sqlite3.Connection, symbol: str, market: str) -> None:
    conn.execute(
        "INSERT INTO instruments (symbol, market, quote_ccy, sector, name, board) "
        "VALUES (?, ?, 'X', NULL, NULL, NULL)", (symbol, market))
    conn.commit()


def _fake_summary(instruments: list[InstrumentRef]) -> RefreshSummary:
    return RefreshSummary(ok={i.symbol: "stub" for i in instruments}, failed=[],
                          fetched_at=_NOW)


def test_history_daily_includes_benchmarks(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    _inst(conn, "2330", "TW")
    calls: list[list[str]] = []

    def fake_history(c, registry, instruments, start, *, now):  # type: ignore[no-untyped-def]
        calls.append(sorted(i.symbol for i in instruments))
        return _fake_summary(instruments)

    monkeypatch.setattr(jobs_mod, "default_registry", lambda conn=None: "REG")
    monkeypatch.setattr(jobs_mod, "refresh_history", fake_history)
    detail = history_daily(conn, now=_NOW)

    seen = {s for group in calls for s in group}
    assert _BENCH <= seen  # both benchmark keys were fetched
    assert "2330" in seen  # instruments still fetched
    assert "benchmarks:" in detail


def test_history_daily_benchmark_failure_degrades_silently(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    _inst(conn, "2330", "TW")

    def fake_history(c, registry, instruments, start, *, now):  # type: ignore[no-untyped-def]
        # Benchmarks route through the SAME function; blow up only for the benchmark call.
        if any(i.symbol in _BENCH for i in instruments):
            raise RuntimeError("boom")
        return _fake_summary(instruments)

    monkeypatch.setattr(jobs_mod, "default_registry", lambda conn=None: "REG")
    monkeypatch.setattr(jobs_mod, "refresh_history", fake_history)
    detail = history_daily(conn, now=_NOW)  # must NOT raise (instrument refresh protected)
    assert "benchmarks: error" in detail


def test_backfill_all_includes_benchmarks_smart_window(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    _inst(conn, "2330", "TW")
    hist_calls: list[list[str]] = []

    def fake_history(c, registry, instruments, start, *, now):  # type: ignore[no-untyped-def]
        hist_calls.append(sorted(i.symbol for i in instruments))
        return _fake_summary(instruments)

    def fake_fx(c, registry, pairs, start, *, now):  # type: ignore[no-untyped-def]
        return RefreshSummary(ok={}, failed=[], fetched_at=now)

    monkeypatch.setattr(jobs_mod, "default_registry", lambda conn=None: "REG")
    monkeypatch.setattr(jobs_mod, "refresh_history", fake_history)
    monkeypatch.setattr(jobs_mod, "refresh_fx_history", fake_fx)
    detail = backfill_history_all(conn, now=_NOW)  # days=None -> smart windows

    seen = {s for group in hist_calls for s in group}
    assert _BENCH <= seen
    assert "benchmarks(from" in detail


def test_backfill_all_includes_benchmarks_explicit_days(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    _inst(conn, "2330", "TW")
    hist_calls: list[list[str]] = []

    def fake_history(c, registry, instruments, start, *, now):  # type: ignore[no-untyped-def]
        hist_calls.append(sorted(i.symbol for i in instruments))
        assert start == date(2026, 5, 4)  # now - 30d, uniform window
        return _fake_summary(instruments)

    def fake_fx(c, registry, pairs, start, *, now):  # type: ignore[no-untyped-def]
        return RefreshSummary(ok={}, failed=[], fetched_at=now)

    monkeypatch.setattr(jobs_mod, "default_registry", lambda conn=None: "REG")
    monkeypatch.setattr(jobs_mod, "refresh_history", fake_history)
    monkeypatch.setattr(jobs_mod, "refresh_fx_history", fake_fx)
    detail = backfill_history_all(conn, now=_NOW, days=30)

    seen = {s for group in hist_calls for s in group}
    assert _BENCH <= seen
    assert "benchmarks:" in detail
