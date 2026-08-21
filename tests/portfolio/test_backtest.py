"""Hand-checked pins for the event-study backtest (W6 — AI-D29/AI-D30/AI-D32).

Every fixture is small enough to verify by hand; the guards (min sample, overlap,
right-censoring, hold semantics) are pinned at their boundaries, not their midpoints.
"""

from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal

from portfolio_dash.portfolio.backtest import (
    COMPOSITE_KIND,
    FORWARD_WINDOWS,
    EventStudy,
    HistoryPoint,
    detect_events,
    event_study,
)

D = Decimal
_HIGH = D("65")
_LOW = D("35")
_START = date(2026, 1, 1)


def _day(i: int) -> date:
    return _START + timedelta(days=i)


def _points(signs: Sequence[str | None], rule: str = "momentum_12_1") -> list[HistoryPoint]:
    return [
        HistoryPoint(
            as_of=_day(i),
            scores={rule: (None if s is None else D(s))},
            tech_score=None,
        )
        for i, s in enumerate(signs)
    ]


def _closes(n: int, first: int = 100) -> list[tuple[date, Decimal]]:
    """Ascending closes: day i → price ``first + i``."""
    return [(_day(i), D(first + i)) for i in range(n)]


# --- detect_events: rule sign changes with hold semantics ----------------------


def test_rule_sign_change_dead_band_dwell_fires_once() -> None:
    # + (silent seed) → 0 (hold) → + (same sign, silent) → − (FIRES bearish)
    # → 0 (hold) → − (same, silent) → + (FIRES bullish) → None (hold) → + (silent)
    signs = ["0.5", "0", "0.3", "-0.2", "0", "-0.4", "0.1", None, "0.9"]
    events = detect_events(_points(signs), band_high=_HIGH, band_low=_LOW)
    assert [(e.kind, e.direction, e.as_of) for e in events] == [
        ("momentum_12_1", "bearish", _day(3)),
        ("momentum_12_1", "bullish", _day(6)),
    ]


def test_first_sighting_is_silent_seed_for_rules() -> None:
    events = detect_events(_points(["0.5", "0.7", "0.9"]), band_high=_HIGH, band_low=_LOW)
    assert events == []


def test_composite_zone_entries_with_hold_anti_flap() -> None:
    scores = [D("50"), D("66"), D("70"), D("64"), D("68"), D("30"), D("50"), D("33"),
              D("70")]
    points = [
        HistoryPoint(as_of=_day(i), scores={}, tech_score=ts)
        for i, ts in enumerate(scores)
    ]
    events = detect_events(points, band_high=_HIGH, band_low=_LOW)
    # 50 (no zone) → 66 (enters high: SILENT seed) → 70 (in-zone) → 64 (mid HOLDS high)
    # → 68 (still held high — NO re-fire, the anti-flap pin) → 30 (FIRES bearish)
    # → 50 (hold) → 33 (in-zone, silent) → 70 (FIRES bullish)
    assert [(e.kind, e.direction, e.as_of) for e in events] == [
        (COMPOSITE_KIND, "bearish", _day(5)),
        (COMPOSITE_KIND, "bullish", _day(8)),
    ]


def test_detect_events_ignores_unknown_absent_rule_keys() -> None:
    # A rule missing from some points' dicts reads as hold, never as an event.
    points = [
        HistoryPoint(as_of=_day(0), scores={"trend_filter": D("1")}, tech_score=None),
        HistoryPoint(as_of=_day(1), scores={}, tech_score=None),
        HistoryPoint(as_of=_day(2), scores={"trend_filter": D("-1")}, tech_score=None),
    ]
    events = detect_events(points, band_high=_HIGH, band_low=_LOW)
    assert [(e.kind, e.direction, e.as_of) for e in events] == [
        ("trend_filter", "bearish", _day(2)),
    ]


# --- event_study: windows, guards, baseline -------------------------------------


def _study_for_signs(
    signs: Sequence[str | None],
    n_closes: int,
    *,
    windows: tuple[int, ...] = FORWARD_WINDOWS,
) -> EventStudy:
    return event_study(
        _points(signs), _closes(n_closes), band_high=_HIGH, band_low=_LOW,
        windows=windows,
    )


def test_index_based_forward_return_hand_checked() -> None:
    # One event at day 0 (first sighting is silent — force a flip: + then − fires at d1).
    study = _study_for_signs(["0.5", "-0.5"], 10)
    group = study.groups[0]
    assert (group.kind, group.direction, group.events) == (
        "momentum_12_1", "bearish", 1,
    )
    out20, out60, out120 = group.outcomes
    # Event at index 1 (price 101). +20 window → index 21 → censored on a 10-day series.
    assert out20.n == 0 and out20.n_censored == 1 and out20.stats is None
    # Baseline at +20 is likewise impossible here.
    assert study.baselines[0] is None


def test_min_sample_boundary_seven_vs_eight() -> None:
    # Alternating signs: bullish events at days 2, 4, 6, 8, 10, 12, 14, 16 (8 total).
    signs = ["0.5"] + ["-0.5" if i % 2 else "0.5" for i in range(1, 18)]
    closes = _closes(40)
    study8 = event_study(
        _points(signs), closes, band_high=_HIGH, band_low=_LOW, windows=(2,)
    )
    bullish = next(
        g for g in study8.groups
        if g.kind == "momentum_12_1" and g.direction == "bullish"
    )
    assert bullish.events == 8
    out = bullish.outcomes[0]
    assert out.n == 8 and out.stats is not None
    assert out.stats.n == 8
    # Hand-check one leg: events at indices 2,4,...,16 (the seed at 0 is silent, the
    # first flip at 1 is bearish); window 2 → i+2 each; closes day i = 100+i →
    # return = 2/(100+i).
    expected_mean = sum(D(2) / D(100 + i) for i in range(2, 17, 2)) / 8
    assert out.stats.mean == expected_mean
    assert out.stats.pct_positive == D(1)  # strictly ascending series

    # Same fixture gated at 9 → 不足以判斷: counts shown, numbers withheld.
    study9 = event_study(
        _points(signs), closes, band_high=_HIGH, band_low=_LOW,
        windows=(2,), min_sample=9,
    )
    out9 = next(
        g for g in study9.groups
        if g.kind == "momentum_12_1" and g.direction == "bullish"
    ).outcomes[0]
    assert out9.n == 8 and out9.stats is None


def test_overlap_annotation_counts_shared_path_events() -> None:
    # Bullish events at indices 2, 4, 6 (alternating signs).
    signs = ["0.5", "-0.5", "0.5", "-0.5", "0.5", "-0.5", "0.5"]
    study = _study_for_signs(signs, 20, windows=(5, 1))
    group = next(g for g in study.groups if g.direction == "bullish")
    out5, out1 = group.outcomes
    # window 5: gaps of 2 and 2 sessions → both successors overlap.
    assert out5.n == 3 and out5.n_overlapping == 2
    # window 1: gaps of 2 ≥ 1 → no overlap.
    assert out1.n == 3 and out1.n_overlapping == 0


def test_right_censoring_excludes_and_counts_per_window() -> None:
    # Daily flips over 10 days: bearish events at 1,3,5,7,9; bullish at 2,4,6,8.
    signs = ["0.5"] + [("-0.5" if i % 2 else "0.5") for i in range(1, 10)]
    study = _study_for_signs(signs, 10, windows=(5,))
    bullish = next(g for g in study.groups if g.direction == "bullish")
    out = bullish.outcomes[0]
    # window 5 needs i+5 ≤ 9 → indices 2, 4 eligible; 6, 8 censored.
    assert out.n == 2 and out.n_censored == 2
    assert out.stats is None  # 2 < 8 → 不足以判斷


def test_baseline_is_the_unconditional_same_symbol_distribution() -> None:
    study = _study_for_signs(["0.5", "-0.5"], 11, windows=(2,))
    baseline = study.baselines[0]
    assert baseline is not None
    # closes day i = 100+i, window 2 → valid starts i ∈ 0..8 → 9 samples, all positive.
    assert baseline.n == 9
    assert baseline.pct_positive == D(1)
    # returns r_i = 2/(100+i), decreasing → sorted median (n=9) = r_4 = 2/104.
    assert baseline.median == D(2) / D(104)
    expected_mean = sum(D(2) / D(100 + i) for i in range(9)) / 9
    assert baseline.mean == expected_mean


def test_event_on_a_date_without_a_close_is_counted_not_placed() -> None:
    points = _points(["0.5", "-0.5"])  # flips at day 1
    closes = [(_day(0), D("100")), (_day(2), D("102"))]  # day 1 missing
    study = event_study(points, closes, band_high=_HIGH, band_low=_LOW, windows=(2,))
    assert study.events_total == 1
    assert study.events_without_price == 1
    assert study.groups == ()


def test_zero_base_close_is_excluded_from_stats_and_baseline() -> None:
    signs = ["0.5", "-0.5"]
    closes = [(_day(i), D(100 + i)) for i in range(10)]
    closes[1] = (_day(1), D("0"))  # the event's own base is defective
    study = event_study(_points(signs)[0:2], closes, band_high=_HIGH, band_low=_LOW,
                        windows=(2,))
    group = study.groups[0]
    assert group.outcomes[0].n == 0 and group.outcomes[0].n_censored == 1
    # The baseline also skips the zero-base index: valid starts 0..7 minus i=1 → 7.
    assert study.baselines[0] is not None
    assert study.baselines[0].n == 7


def test_decimal_discipline_end_to_end() -> None:
    signs = ["0.5"] + [("-0.5" if i % 2 else "0.5") for i in range(1, 18)]
    study = _study_for_signs(signs, 40, windows=(2,))
    group = next(g for g in study.groups if g.direction == "bullish")
    stats = group.outcomes[0].stats
    assert stats is not None
    for field_value in (stats.mean, stats.median, stats.pct_positive):
        assert isinstance(field_value, Decimal)
    baseline = study.baselines[0]
    assert baseline is not None
    assert isinstance(baseline.mean, Decimal)
