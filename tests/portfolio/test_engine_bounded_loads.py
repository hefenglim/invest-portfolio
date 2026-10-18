"""Bounded history loads keep every carried-forward value (PERF spec §8.5, §18 "Read path").

Every series reader takes the latest row ON OR BEFORE its day, so a bounded load must start at
the last row on or before its boundary — never at the boundary. Three boundaries, each on a
Saturday, on a holiday and inside a suspension of the symbol:

* the trend's closes, from its first ledger day — ``build_dashboard``'s trend must equal the
  FROZEN v0.1.31 pipeline fed by unbounded (1900-) loads (``tests/reference``);
* the benchmark's closes, from each market's first flow — the counterfactual equal likewise;
* ``GET /api/performance/twr``'s benchmark closes and FX, from the window start — equal to
  the overlay the unbounded v0.1.31 reads give, on every corpus and window.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from portfolio_dash.api.routers.performance import performance_twr
from portfolio_dash.data_ingestion.holdings import load_action_index
from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.portfolio.dashboard import _build_dashboard
from portfolio_dash.portfolio.dashboard_models import TrendSeries
from portfolio_dash.portfolio.price_basis import series_in
from portfolio_dash.portfolio.timeseries import FxHistory
from portfolio_dash.portfolio.twr import build_overlay, convert_closes, twr_index
from portfolio_dash.pricing.benchmarks import BENCHMARKS, get_benchmark
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import get_fx_history, get_price_history, upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.wire import decimal_str, to_wire
from tests.reference import trend_v0131 as ref
from tests.reference.canon import canon, first_difference
from tests.reference.corpora import (
    CORPORA,
    CORPUS_NAMES,
    TAIPEI,
    TEMPLATE_DIR_NAME,
    copy_corpus,
    open_db,
)
from tests.reference.cost_basis_v0131 import ref_build_book
from tests.reference.dashboard_inputs import DashboardInputs, benchmark_closes, load_inputs

TWD = Currency.TWD
_EPOCH = date(1900, 1, 1)


def _frozen_trend(i: DashboardInputs) -> TrendSeries:
    """``build_dashboard`` steps 9 / 9b on the frozen v0.1.31 functions and 1900- loads."""
    trend = ref.ref_daily_value_series(i.bundle, i.price_history, i.trend_fx_history,
                                       i.reporting, end=i.as_of,
                                       cash_movements=i.cash_movements)
    if not trend.available:
        return trend
    cash = ref.ref_daily_cash_series(i.cash_movements, i.cash_convs, i.cash_txs, i.cash_divs,
                                     i.instruments, i.cash_fx_history, i.reporting,
                                     end=i.as_of)
    return ref.ref_compose_net_worth(trend, cash)


def _frozen_benchmark(conn: sqlite3.Connection, i: DashboardInputs) -> dict[str, Any] | None:
    book = ref_build_book(i.bundle, allow_oversell=True)
    flows = ref.ref_build_reporting_flows(i.bundle, i.trend_fx_history, i.reporting,
                                          cash_movements=i.cash_movements,
                                          refused_dividends=book.refused_dividends)
    if flows is None:
        return None
    cf = ref.ref_counterfactual(flows, benchmark_closes(conn, i, flows))
    wire: dict[str, Any] = to_wire({
        "available": cf.available, "reason": cf.reason, "terminal_value": cf.terminal_value,
        "net_invested": cf.net_invested, "benchmark_return": cf.benchmark_return,
        "uncovered_markets": list(cf.uncovered_markets), "uncovered_ratio": cf.uncovered_ratio})
    return wire


# --- crafted boundaries ----------------------------------------------------------------------

_NOW = datetime(2026, 6, 11, 14, 30, tzinfo=TAIPEI)


def _weekdays(start: date, end: date, *, skip: set[date] | None = None,
              gap: tuple[date, date] | None = None) -> list[date]:
    out, day = [], start
    while day <= end:
        if (day.weekday() < 5 and day not in (skip or set())
                and not (gap is not None and gap[0] <= day <= gap[1])):
            out.append(day)
        day += timedelta(days=1)
    return out


# (shape, the ledger's first day, the days the symbol and the index have a close)
_SHAPES = {
    # a Saturday; the last close is the Friday before
    "weekend": (date(2026, 1, 3), _weekdays(date(2025, 12, 22), date(2026, 3, 31))),
    # New Year's Day, a Thursday with no close; the last one is Dec 31
    "holiday": (date(2026, 1, 1), _weekdays(date(2025, 12, 22), date(2026, 3, 31),
                                            skip={date(2026, 1, 1)})),
    # inside a five-week suspension; the last close is weeks before the first day
    "suspended": (date(2025, 12, 15), _weekdays(date(2025, 10, 1), date(2026, 3, 31),
                                                gap=(date(2025, 11, 10), date(2025, 12, 19)))),
}


@pytest.fixture(params=sorted(_SHAPES))
def crafted(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
            ) -> Iterator[tuple[str, sqlite3.Connection, date]]:
    shape: str = request.param
    first_day, closes = _SHAPES[shape]
    base = tmp_path_factory.getbasetemp() / TEMPLATE_DIR_NAME
    db = copy_corpus("golden", base, tmp_path_factory.mktemp(f"bound-{shape}") / "g.db")
    conn = open_db(db.path)
    try:
        upsert_instrument(conn, Instrument(symbol="BND", market=Market.TW, quote_ccy=TWD,
                                           sector="Utilities", name="Bounded Co",
                                           board="TWSE"))
        insert_transaction(conn, account_id="tw_broker", symbol="BND", side=Side.BUY,
                           quantity=Decimal("2000"), price=Decimal("40"), fees=Decimal("20"),
                           tax=Decimal("0"), trade_date=first_day)
        rows = [PriceRow(instrument=sym, market=Market.TW, as_of=d,
                         close=Decimal(40) + Decimal(n % 9) / Decimal(4), source="test")
                for n, d in enumerate(closes) for sym in ("BND", "0050")]
        upsert_prices(conn, rows, fetched_at=datetime(2026, 4, 1, 12, 0))
        conn.commit()
        yield shape, conn, first_day
    finally:
        conn.close()


def test_the_trend_carries_the_last_close_into_its_first_day(
    crafted: tuple[str, sqlite3.Connection, date],
) -> None:
    shape, conn, first_day = crafted
    data = _build_dashboard(conn, now=_NOW, reporting=TWD)
    i = load_inputs(conn, _NOW, TWD)
    frozen = _frozen_trend(i)
    a, b = canon(frozen), canon(data.trend)
    assert a == b, f"{shape}: {first_difference(a, b)}"
    first = data.trend.points[0]
    assert first.date == first_day and not first.incomplete, shape
    assert first.total_value > 0, f"{shape}: the carried-forward close was lost"


def test_the_benchmark_places_the_first_flow_on_the_last_close(
    crafted: tuple[str, sqlite3.Connection, date],
) -> None:
    shape, conn, _first_day = crafted
    data = _build_dashboard(conn, now=_NOW, reporting=TWD)
    expected = _frozen_benchmark(conn, load_inputs(conn, _NOW, TWD))
    assert expected is not None
    assert data.benchmark is not None
    got = to_wire(data.benchmark.model_dump())
    assert {k: got[k] for k in expected} == expected, shape


# --- the performance route ---------------------------------------------------------------------


def _v0131_overlay(conn: sqlite3.Connection, now: datetime, reporting: Currency,
                   benchmark: str, window: str) -> dict[str, Any]:
    """v0.1.31's /api/performance/twr benchmark leg: closes and FX from 1900."""
    out = performance_twr(benchmark=benchmark, window=window, conn=conn, now=now,
                          reporting=reporting)
    if not out["available"] and not out.get("points") and out["reason"] and (
            "投資組合每日淨值" in out["reason"]):
        return out  # the portfolio leg refused before any benchmark read
    bench = get_benchmark(benchmark)
    assert bench is not None
    data = _build_dashboard(conn, now=now, reporting=reporting)
    port_index = twr_index(data.trend.points)
    as_of = now.date()
    window_start = (port_index[0].date if window == "all"
                    else as_of - timedelta(days={"1y": 365, "3y": 1095}[window]))
    closes = [(p.as_of, p.value) for p in series_in(
        load_action_index(conn), bench.storage_key,
        get_price_history(conn, bench.storage_key, _EPOCH, as_of), valued_on=as_of)]
    fx: FxHistory = {}
    if bench.quote_ccy != reporting:
        for base, other in ((bench.quote_ccy, reporting), (reporting, bench.quote_ccy)):
            rows = get_fx_history(conn, base, other, _EPOCH, as_of)
            if rows:
                fx[(base, other)] = [(r.as_of, r.rate) for r in rows]
    overlay = build_overlay(port_index, convert_closes(closes, fx, bench.quote_ccy, reporting),
                            window_start=window_start, window_end=as_of)
    if not overlay.available:
        return {**out, "available": False, "points": [], "reason": overlay.reason}
    q = Decimal("0.0001")
    return {**out, "available": True, "reason": None, "points": [
        {"date": p.date.isoformat(),
         "portfolio": decimal_str(p.portfolio.quantize(q, rounding="ROUND_HALF_UP")),
         "benchmark": decimal_str(p.benchmark.quantize(q, rounding="ROUND_HALF_UP"))}
        for p in overlay.points]}


@pytest.fixture(params=CORPUS_NAMES)
def corpus(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
           ) -> Iterator[tuple[str, sqlite3.Connection]]:
    name: str = request.param
    base = tmp_path_factory.getbasetemp() / TEMPLATE_DIR_NAME
    db = copy_corpus(name, base, tmp_path_factory.mktemp(f"twr-{name}") / f"{name}.db")
    conn = open_db(db.path)
    try:
        yield name, conn
    finally:
        conn.close()


@pytest.mark.parametrize("window", ["1y", "3y", "all"])
def test_the_performance_route_reads_the_same_overlay(
    corpus: tuple[str, sqlite3.Connection], window: str,
) -> None:
    name, conn = corpus
    c = CORPORA[name]
    for bench in BENCHMARKS:
        got = performance_twr(benchmark=bench.key, window=window, conn=conn, now=c.now,
                              reporting=c.reporting)
        assert got == _v0131_overlay(conn, c.now, c.reporting, bench.key, window), (
            name, bench.key, window)


def test_the_performance_route_on_the_crafted_boundaries(
    crafted: tuple[str, sqlite3.Connection, date],
) -> None:
    shape, conn, _first_day = crafted
    for window in ("1y", "all"):
        got = performance_twr(benchmark="0050", window=window, conn=conn, now=_NOW,
                              reporting=TWD)
        assert got["available"], (shape, window, got["reason"])
        assert got == _v0131_overlay(conn, _NOW, TWD, "0050", window), (shape, window)
