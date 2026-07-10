"""End-to-end ``evaluate_symbol``: the four fixture families + purity/edge guards.

Families (blueprint): monotonic rise, monotonic fall, whipsaw, insufficient data,
and volume present/absent. Plus determinism and property-style edges (empty, single
point, all-equal, one spike).
"""

from decimal import Decimal

from portfolio_dash.strategy.rules import default_params
from portfolio_dash.strategy.rules.engine import evaluate_symbol


def _s(vals: list[float]) -> list[Decimal]:
    return [Decimal(str(v)) for v in vals]


def _rising(n: int = 320) -> list[Decimal]:
    return _s([100.0 + 2.0 * i for i in range(n)])


def _falling(n: int = 320) -> list[Decimal]:
    return _s([100.0 + 2.0 * (n - i) for i in range(n)])


def test_monotonic_rise_is_strong_uptrend() -> None:
    ss = evaluate_symbol(_rising())
    assert ss is not None
    assert ss.params_version == "rules-v1"
    assert ss.rules["trend_filter"] is not None
    assert ss.rules["trend_filter"].state == "above_confirmed"
    assert ss.rules["momentum_12_1"] is not None
    assert ss.rules["momentum_12_1"].state == "positive"
    assert ss.composite is not None
    assert ss.composite.coverage == "4/4"
    assert ss.composite.tech_score > Decimal("50")
    assert ss.composite.evaluation_context == "strong_uptrend"


def test_monotonic_fall_is_strong_downtrend() -> None:
    ss = evaluate_symbol(_falling())
    assert ss is not None
    assert ss.rules["trend_filter"] is not None
    assert ss.rules["trend_filter"].state == "below_confirmed"
    assert ss.composite is not None
    assert ss.composite.tech_score < Decimal("50")
    assert ss.composite.evaluation_context == "strong_downtrend"


def test_whipsaw_stays_range_bound() -> None:
    # 300 flat then a ±1.5% sawtooth inside the band -> neutral trend -> range_bound.
    closes = _s([100.0] * 300 + [101.5 if i % 2 == 0 else 98.5 for i in range(20)])
    ss = evaluate_symbol(closes)
    assert ss is not None
    assert ss.rules["trend_filter"] is not None
    assert ss.rules["trend_filter"].state == "in_band"
    assert ss.composite is not None
    assert ss.composite.evaluation_context == "range_bound"


def test_confirmed_uptrend_with_unknown_momentum_says_so() -> None:
    # 220 sessions: momentum (needs 253) is missing. The label must NOT claim a
    # momentum direction it never measured — the pre-fix behavior labelled this
    # "uptrend_pullback / 動能轉弱", a fabricated claim (deep review 2026-07-10).
    ss = evaluate_symbol(_s([100.0 + i for i in range(220)]))
    assert ss is not None
    trend = ss.rules["trend_filter"]
    assert trend is not None and trend.state == "above_confirmed"
    assert ss.rules["momentum_12_1"] is None
    assert ss.composite is not None
    assert ss.composite.evaluation_context == "uptrend"
    assert "動能樣本不足" in ss.composite.context_note
    assert "轉弱" not in ss.composite.context_note


def test_insufficient_data_family() -> None:
    # 220 sessions: trend/cross/rsi evaluable, momentum (needs 253) missing.
    ss = evaluate_symbol(_s([100.0 + (i % 13) for i in range(220)]))
    assert ss is not None
    assert ss.rules["momentum_12_1"] is None
    assert ss.composite is not None
    assert ss.composite.coverage == "3/4"
    assert "momentum_12_1" in ss.composite.missing

    # 16 sessions: only rsi evaluable -> fewer than 2 rules -> no composite.
    thin = evaluate_symbol(_s([100.0 + (i % 3) for i in range(16)]))
    assert thin is not None
    assert thin.composite is None
    assert thin.rules["rsi_regime"] is not None


def test_volume_present_vs_absent_only_changes_cross_confidence() -> None:
    closes = _rising()
    # A synthetic aligned volume series (all present) vs None.
    with_vol = evaluate_symbol(closes, [Decimal("1000")] * len(closes))
    without_vol = evaluate_symbol(closes, None)
    assert with_vol is not None and without_vol is not None
    # Rising series never crosses -> ma_cross is the standing relationship either way;
    # volume confirmation only matters when there IS a cross, so both agree here.
    cross_with = with_vol.rules["ma_cross"]
    cross_without = without_vol.rules["ma_cross"]
    assert cross_with is not None and cross_without is not None
    assert cross_with.state == cross_without.state


def test_determinism_same_inputs_same_output() -> None:
    closes = _rising()
    assert evaluate_symbol(closes) == evaluate_symbol(closes)
    # Explicit default params produce the same result as the implicit default.
    assert evaluate_symbol(closes, None, default_params()) == evaluate_symbol(closes)


def test_all_equal_series_scores_fifty() -> None:
    ss = evaluate_symbol([Decimal("100")] * 300)
    assert ss is not None
    assert ss.composite is not None
    assert ss.composite.tech_score == Decimal("50")
    assert ss.composite.evaluation_context == "range_bound"


def test_empty_returns_none_single_point_degrades_honestly() -> None:
    # Empty/absent closes -> None (the engine contract).
    assert evaluate_symbol([]) is None
    # A single non-empty point is not enough for ANY rule: every rule is honestly
    # None and there is no composite, but the object itself is still returned.
    ss = evaluate_symbol([Decimal("100")])
    assert ss is not None
    assert all(rule is None for rule in ss.rules.values())
    assert ss.composite is None
    assert ss.params_version == "rules-v1"


def test_constant_plus_one_spike_never_raises() -> None:
    closes = [Decimal("100")] * 300
    closes[150] = Decimal("100000")
    ss = evaluate_symbol(closes)  # must not raise
    assert ss is not None
    assert ss.rules["rsi_regime"] is not None
