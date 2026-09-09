"""Scheduler: the daily history job + smart backfill include the FU-D27 benchmarks.

Benchmarks (0050 / ^GSPC) are fetched via the SAME ``refresh_history`` path as instruments
but are NOT in the instrument worklist — the jobs add ``benchmark_refs()`` explicitly. These
tests monkeypatch ``refresh_history`` to capture the refs each call receives and assert the
benchmark storage keys appear, and that a benchmark fetch failure degrades silently.
"""

import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from portfolio_dash.data_ingestion.store import insert_corporate_action
from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.providers.base import ProviderBase
from portfolio_dash.pricing.refresh import refresh_history
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.pricing.registry import Registry
from portfolio_dash.pricing.results import PriceRow, RefreshSummary
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.scheduler import jobs as jobs_mod
from portfolio_dash.scheduler.jobs import backfill_history_all, history_daily, split_factor_fn
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Market

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

    def fake_history(c, registry, instruments, start, *, now, **_):  # type: ignore[no-untyped-def]
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

    def fake_history(c, registry, instruments, start, *, now, **_):  # type: ignore[no-untyped-def]
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

    def fake_history(c, registry, instruments, start, *, now, **_):  # type: ignore[no-untyped-def]
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

    def fake_history(c, registry, instruments, start, *, now, **_):  # type: ignore[no-untyped-def]
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


# --- D-11 (site-architecture map, 2026-09-10): the benchmark writer binds the ledger --------

_SPLIT_DAY = date(2026, 6, 1)
_PRE_SPLIT_DAY = date(2026, 5, 29)  # a session BEFORE the split, inside (as_of, fetched_at]


class _PostSplitProvider(ProviderBase):
    """A provider AFTER a 1→4 split of 0050: every historical close it serves is already in
    post-split terms, the way yfinance re-states history."""

    name = "fake"

    def supports(self, data_type: DataType, market: Market | None) -> bool:
        return True

    def fetch_quote_history(self, instrument: InstrumentRef, start: date) -> list[PriceRow]:
        return [PriceRow(instrument=instrument.symbol, market=instrument.market,
                         as_of=_PRE_SPLIT_DAY, close=Decimal("47.5"), source=self.name)]


def _stored(conn: sqlite3.Connection, key: str) -> tuple[str, str]:
    row = conn.execute(
        "SELECT close, split_basis FROM prices WHERE instrument = ? AND as_of_date = ?",
        (key, _PRE_SPLIT_DAY.isoformat()),
    ).fetchone()
    assert row is not None, key
    return (str(row[0]), str(row[1]))


def test_benchmark_writer_agrees_with_the_instrument_writer_on_a_held_split_etf(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    """0050 is a benchmark AND an ETF the owner may hold. With its SPLIT in the ledger the
    instrument sweep stores the pre-split session as traded (47.5 × 4, basis 4); the benchmark
    sweep, which runs LAST in both jobs, used to store the same row as 47.5 × 1 — reverting it
    after every deep backfill. Both writers must produce the identical row."""
    create_pricing_tables(conn)
    conn.execute(
        "INSERT INTO instruments (symbol, market, quote_ccy, sector, name, board) "
        "VALUES ('0050', 'TW', 'TWD', NULL, NULL, NULL)")
    insert_corporate_action(
        conn, account_id="tw_broker", action_date=_SPLIT_DAY, kind=CorporateActionKind.SPLIT,
        from_symbol="0050", to_symbol="0050", ratio_to=Decimal(4), ratio_from=Decimal(1),
    )
    provider = _PostSplitProvider()
    reg = Registry(providers={provider.name: provider},
                   order={(DataType.QUOTE_HISTORY, m): [provider.name] for m in Market})
    monkeypatch.setattr(jobs_mod, "default_registry", lambda conn=None: reg)

    # The instrument sweep's write, exactly as history_daily / backfill do it for a holding.
    ref = InstrumentRef(symbol="0050", market=Market.TW, board="TWSE")
    refresh_history(conn, reg, [ref], _PRE_SPLIT_DAY, now=_NOW, factor_of=split_factor_fn(conn))
    as_traded = _stored(conn, "0050")
    assert Decimal(as_traded[0]) == Decimal(190) and Decimal(as_traded[1]) == Decimal(4)

    # The benchmark sweep runs after it and must leave that row byte-identical.
    assert jobs_mod._refresh_benchmark_history(conn, _PRE_SPLIT_DAY, now=_NOW) != "error"
    assert _stored(conn, "0050") == as_traded
    # A true index is untouched by the binding: identity, basis "1".
    assert _stored(conn, "^GSPC") == ("47.5", "1")
