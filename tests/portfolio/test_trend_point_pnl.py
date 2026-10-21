"""The trend point's 累計總損益（含匯兌） is the SERVER's figure (owner ruling 2026-10-22).

The trend tooltip printed ``total_value − net_invested`` computed in JS floats
(``web/charts.js``: ``const spread = pt.total_value - pt.net_invested;``) — the one known
violation of the locked decision that the frontend never computes money
(``tests/contract/test_frontend_never_computes_money.py``). The owner ruled that the server
sends a COMPACT per-point field instead: a short name, ``pnl``, the value at the reporting
currency's display precision (``quantize_amount``: TWD 0 dp, USD / MYR 2 dp, ROUND_HALF_UP),
and the tooltip only formats it.

``trend_pnl`` owns the value. It is set on the series the dashboard SERVES only, once per
build: on the live days by ``with_trend_pnl`` and on each day from the settlement store as it
is built (``_stored_point``). Internal series (the replay, the settlement worker) leave it None.
The served-payload side — every point of every reference corpus, stored days included — is
pinned by ``tests/reference/trend_pnl.py``.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from portfolio_dash.portfolio import dashboard
from portfolio_dash.portfolio.dashboard_models import (
    DashboardData,
    DividendSummary,
    FreshnessReport,
    KpiSummary,
    TrendPoint,
    TrendSeries,
    trend_pnl,
    with_trend_pnl,
)
from portfolio_dash.portfolio.results import RealizedPnL
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.wire import to_wire

D = Decimal


def _point(day: int, total: str, invested: str, *, net_worth: str | None = None,
           incomplete: bool = False) -> TrendPoint:
    return TrendPoint(date=date(2026, 6, day), total_value=D(total), net_invested=D(invested),
                      incomplete=incomplete, net_worth=None if net_worth is None else D(net_worth))


def _served(ccy: Currency, *points: TrendPoint) -> TrendSeries:
    return with_trend_pnl(TrendSeries(points=list(points), reporting_currency=ccy))


def _wire_pnl(series: TrendSeries) -> list[str]:
    return [p["pnl"] for p in to_wire(series.model_dump())["points"]]


@pytest.mark.parametrize(("ccy", "total", "invested", "expected"), [
    # TWD: whole NT$, ROUND_HALF_UP — ties away from zero on both sides
    (Currency.TWD, "1234567.8", "1000000.3", "234568"),
    (Currency.TWD, "100", "100.5", "-1"),
    (Currency.TWD, "639600", "528000", "111600"),
    (Currency.TWD, "500000.0", "520000.49", "-20000"),
    # USD / MYR: the cent / sen
    (Currency.USD, "1000.005", "0", "1000.01"),
    (Currency.USD, "0", "1000.005", "-1000.01"),
    (Currency.USD, "20000", "18000", "2000.00"),
    (Currency.MYR, "4082.123", "4000", "82.12"),
])
def test_every_served_point_carries_pnl_at_the_reporting_minor_unit(
        ccy: Currency, total: str, invested: str, expected: str) -> None:
    series = _served(ccy, _point(1, total, invested))
    assert _wire_pnl(series) == [expected]
    assert trend_pnl(D(total), D(invested), ccy) == series.points[0].pnl == D(expected)


@pytest.mark.parametrize(("ccy", "expected"), [(Currency.TWD, "0"), (Currency.USD, "0.00")])
def test_a_loss_rounded_away_reads_zero_never_minus_zero(ccy: Currency, expected: str) -> None:
    """``Decimal("-0.3").quantize(1)`` is ``-0``; on the wire that would be a minus sign in
    front of a zero, coloured as a loss by any sign test. A rounded-away value is zero."""
    series = _served(ccy, _point(1, "100", "100.003"), _point(2, "100", "100"))
    assert _wire_pnl(series) == [expected, expected]


def test_the_28_digit_operands_of_a_usd_reporting_trend_quantize() -> None:
    """``usd_reporting`` carries 28 significant digits in ``net_invested`` (1 / 31.9 does not
    terminate); the difference still quantizes to the cent."""
    invested = "18264.88401253918495297805643"
    series = _served(Currency.USD, _point(1, "20313.47962382445141065830721", invested))
    assert _wire_pnl(series) == ["2048.60"]


def test_pnl_is_the_last_key_and_every_other_key_is_unchanged() -> None:
    """Additive and last: a served point's other keys keep their order and their bytes."""
    p = _point(3, "639600", "528000", net_worth="701234.5", incomplete=True)
    dumped = to_wire(_served(Currency.TWD, p).model_dump())["points"][0]
    assert list(dumped) == ["date", "total_value", "net_invested", "incomplete", "net_worth",
                            "pnl"]
    assert dumped["pnl"] == "111600"
    assert {k: v for k, v in dumped.items() if k != "pnl"} == {
        k: v for k, v in to_wire(p.model_dump()).items() if k != "pnl"}


def test_with_trend_pnl_returns_a_copy_and_keeps_the_series_fields() -> None:
    base = TrendSeries(points=[_point(1, "800", "900")], reporting_currency=Currency.USD,
                       options_value_excluded=True)
    served = with_trend_pnl(base)
    assert base.points[0].pnl is None, "the input series is not mutated"
    assert served.points[0].pnl == D("-100.00")
    assert served.reporting_currency is Currency.USD
    assert served.options_value_excluded is True
    assert with_trend_pnl(TrendSeries(points=[], reporting_currency=Currency.TWD,
                                      available=False)).points == []


def test_an_internal_series_leaves_it_unset() -> None:
    """The replay and the settlement worker build points without it: only the served series
    carries it, so the engine stays equal to its frozen v0.1.31 copy field for field."""
    assert _point(1, "1", "2").pnl is None


def test_the_dashboard_still_round_trips() -> None:
    """spec-18 round trip: a dump with ``pnl`` validates back to the same model."""
    data = DashboardData(
        as_of=datetime(2026, 6, 10, 12, 0),
        reporting_currency=Currency.TWD,
        kpis=KpiSummary(reporting_currency=Currency.TWD, total_market_value=D("639600")),
        holdings=[],
        realized=RealizedPnL(rows=[], by_currency={}),
        returns=None,
        allocation=None,
        currency_view=None,
        fx=None,
        dividends=DividendSummary(by_year=[], total_by_currency={}),
        ex_dividend_calendar=[],
        trend=_served(Currency.TWD, _point(1, "639600", "528000")),
        freshness=FreshnessReport(prices=[], fx=[], any_stale=False,
                                  missing_prices=[], missing_fx=[]),
    )
    dumped = data.model_dump()
    assert dumped["trend"]["points"][0]["pnl"] == D("111600")
    assert DashboardData.model_validate(dumped) == data


def test_the_dashboard_build_is_the_only_caller() -> None:
    """One owner (``trend_pnl``), and the served series is the only place it is set: the
    dashboard combiner, on its live days (``with_trend_pnl``, once) and on each day it serves
    from the settlement store (``_stored_point``). Nothing else in the package sets it."""
    import inspect
    from pathlib import Path

    import portfolio_dash

    root = Path(portfolio_dash.__file__).parent
    callers = sorted(
        str(path.relative_to(root)).replace("\\", "/")
        for path in root.rglob("*.py")
        if "trend_pnl(" in path.read_text(encoding="utf-8")
        and path.name != "dashboard_models.py")
    assert callers == ["portfolio/dashboard.py"], callers
    assert inspect.getsource(dashboard).count("with_trend_pnl(") == 1
    assert "trend_pnl(" in inspect.getsource(dashboard._stored_point)


@pytest.mark.parametrize(("ccy", "expected"), [(Currency.TWD, "-20000"),
                                               (Currency.USD, "-20000.49")])
def test_a_stored_day_is_served_with_its_pnl(ccy: Currency, expected: str) -> None:
    """A day from the settlement store is built with it — the store keeps no such column; it is
    derived when the day is served, in the reader's reporting currency."""
    import dataclasses

    from portfolio_dash.portfolio.settlement_store import SettledDay

    assert "pnl" not in {f.name for f in dataclasses.fields(SettledDay)}
    day = SettledDay(day=date(2026, 6, 1), total_value=D("500000.0"),
                     net_invested=D("520000.49"), net_worth=D("1"), cash_total=D("0"),
                     cash_incomplete=False, incomplete=False, options_open=False)
    point = dashboard._stored_point(day, ccy)
    assert point.pnl == D(expected)
    assert (point.total_value, point.net_invested, point.net_worth) == (
        day.total_value, day.net_invested, day.net_worth)
