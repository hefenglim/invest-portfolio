"""Pure time-weighted-return (TWR) core for the benchmark overlay (FU-D27).

Three pure functions, no DB handle, ``Decimal`` end to end (quantize only at the wire):

- :func:`twr_index` — chain-linked daily TWR index (100-based) from the SAME
  ``daily_value_series`` :class:`TrendPoint`s the dashboard trend card plots.
- :func:`convert_closes` — a benchmark's daily closes converted to the reporting
  currency at daily carry-forward FX (so the comparison embeds FX exactly like the
  portfolio's own trend does — it reuses ``timeseries``'s carry-forward rate helper).
- :func:`build_overlay` — align the portfolio TWR series and the benchmark series on
  one shared daily axis, carry the benchmark forward over holiday gaps, and rebase BOTH
  to 100 at the common start (the later of the two series' first available dates).

This is an ANALYSIS metric, not money-of-record: nothing here touches the oracle or the
accounting manual. Numbers stay full-precision ``Decimal``; the API router quantizes to
4 dp at the JSON boundary.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

# Reuse timeseries' carry-forward helpers so the benchmark's FX carry is byte-identical
# to the portfolio trend's (data-and-pricing.md: one FX helper, no ad-hoc rate math). The
# leading-underscore names are internal to timeseries but shared within the portfolio
# layer here on purpose — a single carry-forward definition guarantees "embeds FX exactly
# like the portfolio does" holds by construction, not by parallel re-implementation.
from portfolio_dash.portfolio.dashboard_models import TrendPoint
from portfolio_dash.portfolio.timeseries import FxHistory, _at_or_before, _fx_at
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.fx import convert

_ZERO = Decimal("0")
_ONE = Decimal("1")
_HUNDRED = Decimal("100")


@dataclass(frozen=True)
class IndexPoint:
    """One day on a 100-based index series (portfolio TWR, or a converted benchmark)."""

    date: date
    value: Decimal


@dataclass(frozen=True)
class OverlayPoint:
    """One aligned, rebased comparison point: portfolio index vs benchmark index."""

    date: date
    portfolio: Decimal
    benchmark: Decimal


@dataclass(frozen=True)
class Overlay:
    """The comparison result. ``available=False`` carries a zh ``reason`` and no points."""

    points: list[OverlayPoint]
    available: bool
    reason: str | None = None


def twr_index(points: list[TrendPoint]) -> list[IndexPoint]:
    """Chain-linked daily time-weighted-return index, anchored at 100.

    Convention (FU-D27): the day-t external flow is ``F_t = NI_t - NI_{t-1}`` (net capital
    added ``+`` / withdrawn ``-`` that day, in reporting currency). The daily return is::

        r_t = (V_t - F_t - V_{t-1}) / V_{t-1}     for V_{t-1} > 0

    i.e. the flow is removed from the day's value change so injected/withdrawn capital never
    counts as investment return, and the return is measured on the opening capital
    ``V_{t-1}``. The index compounds ``r_t`` multiplicatively and starts at 100 on the first
    day with ``V > 0``.

    Robustness (all documented, all fixture-tested):

    - ``incomplete`` days (a held symbol had no price → ``V_t`` is a partial, untrustworthy
      sum) carry the index forward with NO fabricated return and are NOT used as a base;
      the next complete day bridges the gap against the last trustworthy ``(V, NI)`` pair,
      so the cumulative flow across the gap is still removed exactly once.
    - a portfolio that empties to ``V=0`` and is later re-funded carries the index across
      the ``V=0`` gap and restarts the chain from the re-funding day WITHOUT a fabricated
      jump.
    - first-day flows are inherent to the anchor (index is set to 100 regardless of that
      day's ``NI``); a single-point / never-positive series returns ``[]`` honestly.

    Full-precision ``Decimal`` throughout (the API quantizes to 4 dp at the wire).
    """
    out: list[IndexPoint] = []
    running: Decimal | None = None  # current index value; None until anchored
    base_v: Decimal | None = None  # last trustworthy value used as the return base
    base_ni: Decimal | None = None  # net-invested on that same base day
    for p in points:
        v = p.total_value
        ni = p.net_invested
        if running is None:
            # Anchor on the first complete day with a positive value.
            if not p.incomplete and v > _ZERO:
                running = _HUNDRED
                base_v, base_ni = v, ni
                out.append(IndexPoint(date=p.date, value=running))
            continue
        if p.incomplete:
            # Untrustworthy value → carry the index; do NOT advance the base.
            out.append(IndexPoint(date=p.date, value=running))
            continue
        if base_v is None or base_v <= _ZERO:
            # V=0 gap (or base not yet re-established): carry the index and restart the
            # segment from the first positive value WITHOUT a jump.
            out.append(IndexPoint(date=p.date, value=running))
            if v > _ZERO:
                base_v, base_ni = v, ni
            continue
        # Normal return day: base_v > 0 (base_ni set together with base_v — never None here).
        assert base_ni is not None
        flow = ni - base_ni
        r = (v - flow - base_v) / base_v
        running = running * (_ONE + r)
        out.append(IndexPoint(date=p.date, value=running))
        base_v, base_ni = v, ni
    return out


def convert_closes(
    closes: list[tuple[date, Decimal]],
    fx_history: FxHistory,
    quote: Currency,
    reporting: Currency,
) -> list[tuple[date, Decimal]]:
    """Convert benchmark closes (quote ccy) to the reporting ccy at carry-forward FX.

    ``closes`` is ascending ``(date, close)``. Each close is converted at the point-in-time
    carry-forward rate (``timeseries._fx_at``: identity → direct pair → inverted pair) for
    ``quote → reporting`` — the SAME rule the portfolio trend uses, so the benchmark embeds
    FX identically. A close whose date has no on-or-before rate is dropped (an honest gap;
    the overlay's holiday carry-forward fills the shared axis from the remaining closes).
    Returns ascending ``(date, converted_close)``.
    """
    out: list[tuple[date, Decimal]] = []
    for d, close in closes:
        rate = _fx_at(fx_history, d, quote, reporting)
        if rate is None:
            continue
        out.append((d, convert(close, rate)))
    return out


def build_overlay(
    portfolio_index: list[IndexPoint],
    benchmark_reporting: list[tuple[date, Decimal]],
    *,
    window_start: date,
    window_end: date,
) -> Overlay:
    """Align + rebase the portfolio TWR and benchmark series onto one shared daily axis.

    ``portfolio_index`` is the dense daily TWR series (from :func:`twr_index`, already
    100-anchored at the portfolio's own start). ``benchmark_reporting`` is the benchmark's
    reporting-currency closes on trading days only (from :func:`convert_closes`). The
    portfolio series defines the axis (it is daily and dense); the benchmark is carried
    forward onto every axis date (holiday/weekend gaps repeat the last close). Both series
    are then rebased to 100 at the **common start** — the later of the portfolio's first
    windowed date and the benchmark's first available date — so the two lines start
    together. Returns ``available=False`` + a zh reason (never raises) when either series is
    insufficient inside the window.
    """
    pw = [p for p in portfolio_index if window_start <= p.date <= window_end]
    if not pw:
        return Overlay(
            points=[], available=False,
            reason="投資組合每日淨值資料不足，無法計算時間加權報酬",
        )

    # Benchmark carried forward onto the (dense, daily) portfolio axis.
    axis_bench: list[tuple[date, Decimal | None]] = [
        (p.date, _at_or_before(benchmark_reporting, p.date)) for p in pw
    ]
    bench_first = next((d for d, val in axis_bench if val is not None), None)
    if bench_first is None:
        return Overlay(
            points=[], available=False,
            reason="基準指數尚無可用歷史報價（含匯率換算），請稍後再試或先執行歷史回補",
        )

    # bench_first is one of the (dense) axis dates and axis starts at pw[0].date, so
    # bench_first >= pw[0].date and the common start is always a real portfolio axis date.
    common_start = max(pw[0].date, bench_first)
    port_by_date = {p.date: p.value for p in pw}
    port_base = port_by_date[common_start]
    bench_base = _at_or_before(benchmark_reporting, common_start)
    if bench_base is None or bench_base == _ZERO or port_base == _ZERO:
        return Overlay(
            points=[], available=False,
            reason="基準比較基準值為零，無法重定基準",
        )

    points: list[OverlayPoint] = []
    for d, bench_val in axis_bench:
        if d < common_start or bench_val is None:
            continue
        points.append(
            OverlayPoint(
                date=d,
                portfolio=_HUNDRED * port_by_date[d] / port_base,
                benchmark=_HUNDRED * bench_val / bench_base,
            )
        )
    return Overlay(points=points, available=True, reason=None)
