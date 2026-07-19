"""Unit: pure TWR core (FU-D27) — hand-computed fixtures, exact Decimal strings.

Every index value is derived by hand from the chain-linked TWR formula
``r_t = (V_t - F_t - V_{t-1}) / V_{t-1}`` (``F_t = NI_t - NI_{t-1}``) and asserted as an
exact 4-dp string (the wire precision). Benchmark conversion/rebase is checked for FX
carry-forward, holiday-gap carry, and common-start rebase.
"""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from portfolio_dash.portfolio.dashboard_models import TrendPoint
from portfolio_dash.portfolio.timeseries import FxHistory
from portfolio_dash.portfolio.twr import (
    IndexPoint,
    build_overlay,
    convert_closes,
    twr_index,
)
from portfolio_dash.shared.enums import Currency

USD = Currency.USD
TWD = Currency.TWD

_Q4 = Decimal("0.0001")


def _q4(v: Decimal) -> str:
    """The wire form: quantize to 4 dp (ROUND_HALF_UP), canonical fixed-point string."""
    return format(v.quantize(_Q4, rounding=ROUND_HALF_UP), "f")


def _tp(d: date, v: str, ni: str, incomplete: bool = False) -> TrendPoint:
    return TrendPoint(date=d, total_value=Decimal(v), net_invested=Decimal(ni),
                      incomplete=incomplete)


def _idx_strings(points: list[IndexPoint]) -> list[str]:
    return [_q4(p.value) for p in points]


# --- twr_index -----------------------------------------------------------------


def test_twr_three_day_with_mid_flow() -> None:
    """Day0 anchors at 100; a +50 flow on day1 is removed from the return.

    d0: V=100 NI=100 -> index 100 (anchor).
    d1: V=165 NI=150 -> F=50, r=(165-50-100)/100=0.15 -> 115.
    d2: V=181.5 NI=150 -> F=0, r=(181.5-165)/165=0.1 -> 126.5.
    """
    points = [
        _tp(date(2026, 6, 1), "100", "100"),
        _tp(date(2026, 6, 2), "165", "150"),
        _tp(date(2026, 6, 3), "181.5", "150"),
    ]
    idx = twr_index(points)
    assert [p.date for p in idx] == [date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)]
    assert _idx_strings(idx) == ["100.0000", "115.0000", "126.5000"]


def test_twr_flow_on_first_day_does_not_create_a_return() -> None:
    """The anchor day's own flow (NI 0->200) must NOT move the index off 100."""
    points = [
        _tp(date(2026, 6, 1), "200", "200"),  # whole 200 invested day 0 -> still anchors 100
        _tp(date(2026, 6, 2), "220", "200"),  # F=0, r=(220-200)/200=0.1 -> 110
    ]
    assert _idx_strings(twr_index(points)) == ["100.0000", "110.0000"]


def test_twr_portfolio_empties_then_refunds_carries_without_a_jump() -> None:
    """A V=0 gap carries the index; the re-funding day restarts the chain with no jump.

    d0: V=100 NI=100 -> 100.
    d1: fully sold, proceeds 110 -> NI=-10, V=0. F=-110, r=(0+110-100)/100=0.1 -> 110.
    d2: still empty, V=0 -> carry 110.
    d3: re-buy 100 -> NI=90, V=100. base_v was 0 -> carry 110 (NO jump), restart base.
    d4: V=105 NI=90 -> F=0, r=(105-100)/100=0.05 -> 115.5.
    """
    points = [
        _tp(date(2026, 6, 1), "100", "100"),
        _tp(date(2026, 6, 2), "0", "-10"),
        _tp(date(2026, 6, 3), "0", "-10"),
        _tp(date(2026, 6, 4), "100", "90"),
        _tp(date(2026, 6, 5), "105", "90"),
    ]
    assert _idx_strings(twr_index(points)) == [
        "100.0000", "110.0000", "110.0000", "110.0000", "115.5000"]


def test_twr_incomplete_day_carries_and_next_day_bridges_the_gap() -> None:
    """An incomplete day carries the index (no fabricated return) and is never a base.

    d0: V=100 NI=100 -> 100.
    d1: incomplete, V=90 (untrustworthy) -> carry 100; base stays (100,100).
    d2: complete V=120 NI=100 -> bridges from d0: r=(120-100)/100=0.2 -> 120.
    """
    points = [
        _tp(date(2026, 6, 1), "100", "100"),
        _tp(date(2026, 6, 2), "90", "100", incomplete=True),
        _tp(date(2026, 6, 3), "120", "100"),
    ]
    assert _idx_strings(twr_index(points)) == ["100.0000", "100.0000", "120.0000"]


def test_twr_single_point_and_degenerate_series() -> None:
    """Single positive complete day -> [100]; incomplete / zero / empty -> []."""
    assert _idx_strings(twr_index([_tp(date(2026, 6, 1), "100", "100")])) == ["100.0000"]
    assert twr_index([_tp(date(2026, 6, 1), "50", "50", incomplete=True)]) == []
    assert twr_index([_tp(date(2026, 6, 1), "0", "0")]) == []
    assert twr_index([]) == []


def test_twr_skips_leading_pre_anchor_days_then_anchors() -> None:
    """Days before the first positive complete day emit nothing; the anchor is 100."""
    points = [
        _tp(date(2026, 5, 31), "0", "0"),          # nothing held yet -> skipped
        _tp(date(2026, 6, 1), "100", "100"),       # anchor
        _tp(date(2026, 6, 2), "110", "100"),       # +10% -> 110
    ]
    idx = twr_index(points)
    assert [p.date for p in idx] == [date(2026, 6, 1), date(2026, 6, 2)]
    assert _idx_strings(idx) == ["100.0000", "110.0000"]


# --- convert_closes (FX carry-forward) -----------------------------------------


def test_convert_closes_carries_fx_forward_and_drops_pre_fx_dates() -> None:
    """Each close converts at the on-or-before rate; a pre-first-rate close is dropped."""
    closes = [
        (date(2026, 5, 31), Decimal("90")),   # before the first FX -> dropped
        (date(2026, 6, 1), Decimal("100")),
        (date(2026, 6, 2), Decimal("110")),   # no 06-02 rate -> carry 06-01's 30
        (date(2026, 6, 3), Decimal("120")),
    ]
    fx: FxHistory = {(USD, TWD): [(date(2026, 6, 1), Decimal("30")),
                                  (date(2026, 6, 3), Decimal("31"))]}
    out = convert_closes(closes, fx, USD, TWD)
    assert out == [
        (date(2026, 6, 1), Decimal("3000")),   # 100 * 30
        (date(2026, 6, 2), Decimal("3300")),   # 110 * 30 (carried)
        (date(2026, 6, 3), Decimal("3720")),   # 120 * 31
    ]


def test_convert_closes_identity_when_quote_equals_reporting() -> None:
    """quote == reporting -> rate is 1 (no FX history needed), closes pass through."""
    closes = [(date(2026, 6, 1), Decimal("100")), (date(2026, 6, 2), Decimal("101"))]
    assert convert_closes(closes, {}, TWD, TWD) == closes


# --- build_overlay (holiday carry + common-start rebase) -----------------------


def _ip(d: date, v: str) -> IndexPoint:
    return IndexPoint(date=d, value=Decimal(v))


def test_build_overlay_carries_benchmark_over_a_holiday_gap() -> None:
    """A benchmark trading-day gap (06-03 missing) repeats the last close on the axis."""
    port = [_ip(date(2026, 6, d), str(99 + d)) for d in range(1, 6)]  # 100..104
    bench = [
        (date(2026, 6, 1), Decimal("200")),
        (date(2026, 6, 2), Decimal("210")),
        (date(2026, 6, 4), Decimal("220")),  # 06-03 is a holiday
        (date(2026, 6, 5), Decimal("230")),
    ]
    ov = build_overlay(port, bench, window_start=date(2026, 6, 1), window_end=date(2026, 6, 5))
    assert ov.available is True
    assert [p.date for p in ov.points] == [date(2026, 6, d) for d in range(1, 6)]
    by_date = {p.date: p for p in ov.points}
    # 06-03 carries 06-02's benchmark close (210 -> rebased 105), portfolio keeps its own 102.
    assert by_date[date(2026, 6, 3)].benchmark == Decimal("105")
    assert by_date[date(2026, 6, 3)].portfolio == Decimal("102")
    assert by_date[date(2026, 6, 5)].benchmark == Decimal("115")


def test_build_overlay_rebases_both_to_100_at_the_common_start() -> None:
    """Benchmark starts later than the portfolio -> both rebase to 100 at that later day."""
    port = [_ip(date(2026, 6, 1), "100"), _ip(date(2026, 6, 2), "110"),
            _ip(date(2026, 6, 3), "121"), _ip(date(2026, 6, 4), "133.1"),
            _ip(date(2026, 6, 5), "146.41")]
    bench = [(date(2026, 6, 3), Decimal("50")), (date(2026, 6, 4), Decimal("55")),
             (date(2026, 6, 5), Decimal("60"))]
    ov = build_overlay(port, bench, window_start=date(2026, 6, 1), window_end=date(2026, 6, 5))
    assert ov.available is True
    # common start = max(portfolio 06-01, benchmark 06-03) = 06-03; both = 100 there.
    assert [p.date for p in ov.points] == [date(2026, 6, 3), date(2026, 6, 4), date(2026, 6, 5)]
    assert [p.portfolio for p in ov.points] == [Decimal("100"), Decimal("110"), Decimal("121")]
    assert [p.benchmark for p in ov.points] == [Decimal("100"), Decimal("110"), Decimal("120")]


def test_build_overlay_degrades_when_portfolio_window_empty() -> None:
    port = [_ip(date(2026, 6, 1), "100"), _ip(date(2026, 6, 2), "110")]
    bench = [(date(2026, 6, 1), Decimal("200"))]
    ov = build_overlay(port, bench, window_start=date(2026, 6, 10), window_end=date(2026, 6, 20))
    assert ov.available is False
    assert ov.points == []
    assert ov.reason is not None and "投資組合" in ov.reason


def test_build_overlay_degrades_when_benchmark_missing() -> None:
    port = [_ip(date(2026, 6, 1), "100"), _ip(date(2026, 6, 2), "110")]
    ov = build_overlay(port, [], window_start=date(2026, 6, 1), window_end=date(2026, 6, 5))
    assert ov.available is False
    assert ov.points == []
    assert ov.reason is not None and "基準" in ov.reason
