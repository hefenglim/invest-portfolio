"""Pure quant-scoring + miss + calibration-error tests (spec 04.4). Decimal, no float.

``score_quant`` is the objective, LLM-free verdict: program compares a card's prediction
against the actual measurement fed in by the api/pricing seam. Hand-checked cases.
"""

from decimal import Decimal

from portfolio_dash.llm_insight import scoring
from portfolio_dash.llm_insight.cards import Prediction
from portfolio_dash.llm_insight.scoring import ActualMeasurement


def _pred(metric: str, direction: str, target_pct: str | None = None, h: int = 5) -> Prediction:
    return Prediction(
        metric=metric,  # type: ignore[arg-type]
        direction=direction,  # type: ignore[arg-type]
        target_pct=None if target_pct is None else Decimal(target_pct),
        horizon_days=h,
    )


# --- actual unavailable → None (pending_data, NOT miss) -----------------------


def test_score_quant_actual_none_returns_none() -> None:
    assert scoring.score_quant(_pred("price_change", "up"), None) is None


def test_score_quant_actual_with_no_change_returns_none() -> None:
    # No measurable change available (e.g. price missing) → pending, not a miss.
    assert scoring.score_quant(
        _pred("price_change", "up"), ActualMeasurement(price_change_pct=None)
    ) is None


# --- price_change: direction only --------------------------------------------


def test_price_change_up_direction_hit() -> None:
    actual = ActualMeasurement(price_change_pct=Decimal("0.02"))  # +2%
    assert scoring.score_quant(_pred("price_change", "up"), actual) is True


def test_price_change_up_direction_miss_when_down() -> None:
    actual = ActualMeasurement(price_change_pct=Decimal("-0.01"))  # -1%
    assert scoring.score_quant(_pred("price_change", "up"), actual) is False


def test_price_change_down_direction_hit() -> None:
    actual = ActualMeasurement(price_change_pct=Decimal("-0.05"))
    assert scoring.score_quant(_pred("price_change", "down"), actual) is True


def test_price_change_flat_hit_within_band() -> None:
    # flat = |move| within the ±0.5% flat band.
    actual = ActualMeasurement(price_change_pct=Decimal("0.003"))
    assert scoring.score_quant(_pred("price_change", "flat"), actual) is True


def test_price_change_flat_miss_outside_band() -> None:
    actual = ActualMeasurement(price_change_pct=Decimal("0.02"))
    assert scoring.score_quant(_pred("price_change", "flat"), actual) is False


# --- price_change: with a target_pct magnitude -------------------------------


def test_price_change_up_target_hit_meets_threshold() -> None:
    # pred up/+3%, actual +3.02% → hit (met the magnitude in the right direction).
    pred = _pred("price_change", "up", "0.03")
    actual = ActualMeasurement(price_change_pct=Decimal("0.0302"))
    assert scoring.score_quant(pred, actual) is True


def test_price_change_up_target_miss_below_threshold() -> None:
    # pred up/+3%, actual +1% → miss (right direction but short of the target).
    pred = _pred("price_change", "up", "0.03")
    actual = ActualMeasurement(price_change_pct=Decimal("0.01"))
    assert scoring.score_quant(pred, actual) is False


def test_price_change_down_target_hit() -> None:
    pred = _pred("price_change", "down", "0.03")
    actual = ActualMeasurement(price_change_pct=Decimal("-0.04"))
    assert scoring.score_quant(pred, actual) is True


# --- volatility: regime match -------------------------------------------------


def test_volatility_up_regime_hit() -> None:
    # direction up = volatility rose vs the realized prior-window vol.
    actual = ActualMeasurement(vol_change_pct=Decimal("0.15"))
    assert scoring.score_quant(_pred("volatility", "up"), actual) is True


def test_volatility_down_regime_miss() -> None:
    actual = ActualMeasurement(vol_change_pct=Decimal("0.15"))
    assert scoring.score_quant(_pred("volatility", "down"), actual) is False


def test_volatility_none_returns_none() -> None:
    assert scoring.score_quant(
        _pred("volatility", "up"), ActualMeasurement(vol_change_pct=None)
    ) is None


def test_volatility_flat_hit_within_vol_band() -> None:
    # AI-D25: the vol flat band is ±5% — a 30-day vol estimator jitters by several percent
    # on a constant series, so the shared ±0.5% band would make every flat call an
    # automatic miss. +4% relative vol change is "stable".
    actual = ActualMeasurement(vol_change_pct=Decimal("0.04"))
    assert scoring.score_quant(_pred("volatility", "flat"), actual) is True


def test_volatility_flat_miss_outside_vol_band() -> None:
    # +6% relative vol change is a real regime move, not estimator noise.
    actual = ActualMeasurement(vol_change_pct=Decimal("0.06"))
    assert scoring.score_quant(_pred("volatility", "flat"), actual) is False


def test_volatility_flat_band_does_not_leak_into_price_change() -> None:
    # The wider band is scoped to volatility: a +2% price move is still NOT flat.
    actual = ActualMeasurement(price_change_pct=Decimal("0.02"))
    assert scoring.score_quant(_pred("price_change", "flat"), actual) is False


# --- relative: symbol vs benchmark -------------------------------------------


def test_relative_up_hit_when_outperforms() -> None:
    # up = symbol return > benchmark return.
    actual = ActualMeasurement(
        symbol_return_pct=Decimal("0.05"), benchmark_return_pct=Decimal("0.02")
    )
    assert scoring.score_quant(_pred("relative", "up"), actual) is True


def test_relative_up_miss_when_underperforms() -> None:
    actual = ActualMeasurement(
        symbol_return_pct=Decimal("0.01"), benchmark_return_pct=Decimal("0.04")
    )
    assert scoring.score_quant(_pred("relative", "up"), actual) is False


def test_relative_down_hit_when_underperforms() -> None:
    actual = ActualMeasurement(
        symbol_return_pct=Decimal("-0.02"), benchmark_return_pct=Decimal("0.01")
    )
    assert scoring.score_quant(_pred("relative", "down"), actual) is True


def test_relative_missing_benchmark_returns_none() -> None:
    actual = ActualMeasurement(symbol_return_pct=Decimal("0.05"), benchmark_return_pct=None)
    assert scoring.score_quant(_pred("relative", "up"), actual) is None


def test_relative_flat_band_stays_half_percent() -> None:
    # AI-D25 widened only the VOLATILITY band; excess return keeps the shared ±0.5%:
    # +0.4% excess is "in line with the benchmark" (flat hit), +0.6% is a real beat.
    tight = ActualMeasurement(
        symbol_return_pct=Decimal("0.054"), benchmark_return_pct=Decimal("0.05")
    )
    assert scoring.score_quant(_pred("relative", "flat"), tight) is True
    wide = ActualMeasurement(
        symbol_return_pct=Decimal("0.056"), benchmark_return_pct=Decimal("0.05")
    )
    assert scoring.score_quant(_pred("relative", "flat"), wide) is False


# --- decide_miss --------------------------------------------------------------


def test_decide_miss_quant_false_is_miss() -> None:
    # An objective quant failure is a miss regardless of narrative.
    assert scoring.decide_miss(quant_hit=False, narrative_score=90, threshold=60) is True


def test_decide_miss_quant_true_not_miss() -> None:
    assert scoring.decide_miss(quant_hit=True, narrative_score=10, threshold=60) is False


def test_decide_miss_narrative_below_threshold_is_miss() -> None:
    # Pure-narrative card (quant None): low narrative score → miss.
    assert scoring.decide_miss(quant_hit=None, narrative_score=40, threshold=60) is True


def test_decide_miss_narrative_at_threshold_not_miss() -> None:
    assert scoring.decide_miss(quant_hit=None, narrative_score=60, threshold=60) is False


def test_decide_miss_no_signals_not_miss() -> None:
    # No quant and no narrative score (master skipped) → cannot judge a miss → not a miss.
    assert scoring.decide_miss(quant_hit=None, narrative_score=None, threshold=60) is False


# --- should_calibrate ---------------------------------------------------------


def test_should_calibrate_min_samples_gate() -> None:
    # below min_samples → never trigger, even with all misses.
    assert scoring.should_calibrate(
        resolved_samples=3, min_samples=8, consecutive_misses=3, miss_count=3,
        gap_alert_pp=Decimal("10"),
    ) is False


def test_should_calibrate_consecutive_misses() -> None:
    assert scoring.should_calibrate(
        resolved_samples=8, min_samples=8, consecutive_misses=3, miss_count=3,
        gap_alert_pp=Decimal("90"),  # miss-rate path off; streak path on
    ) is True


def test_should_calibrate_miss_rate_over_gap() -> None:
    # 5/8 = 62.5% miss rate > 10pp gap, but only 1 consecutive → miss-rate trigger.
    assert scoring.should_calibrate(
        resolved_samples=8, min_samples=8, consecutive_misses=1, miss_count=5,
        gap_alert_pp=Decimal("10"),
    ) is True


def test_should_calibrate_no_trigger() -> None:
    # enough samples, low miss rate, no streak → no trigger.
    assert scoring.should_calibrate(
        resolved_samples=8, min_samples=8, consecutive_misses=0, miss_count=0,
        gap_alert_pp=Decimal("10"),
    ) is False


# --- calibration_error --------------------------------------------------------


def test_calibration_error_pp() -> None:
    # rows of (confidence, hit). claimed avg = 80; actual hit rate = 50% → 30pp error.
    rows = [(80, True), (80, False), (80, True), (80, False)]
    assert scoring.calibration_error(rows) == Decimal("30")


def test_calibration_error_empty_is_zero() -> None:
    assert scoring.calibration_error([]) == Decimal("0")


def test_calibration_error_perfect() -> None:
    # claimed 100, all hit → 0pp.
    rows = [(100, True), (100, True)]
    assert scoring.calibration_error(rows) == Decimal("0")


# --- trust_tier (W7, AI-D36) ----------------------------------------------------


def test_trust_tier_insufficient_below_min_sample_regardless_of_quality() -> None:
    # A perfect record over 7 rows is still 樣本不足 — the same honesty gate as the
    # event study's MIN_SAMPLE and the rolling gap's <8.
    assert scoring.trust_tier(
        n=7, quant_n=7, quant_hit_rate=Decimal("1"), narrative_success_rate=None,
        calib_error_pp=Decimal("0"),
    ) == "樣本不足"


def test_trust_tier_boundaries_are_inclusive() -> None:
    # success exactly 0.6 AND error exactly 10pp → 可參考 (both bounds inclusive).
    assert scoring.trust_tier(
        n=8, quant_n=8, quant_hit_rate=Decimal("0.6"), narrative_success_rate=None,
        calib_error_pp=Decimal("10"),
    ) == "可參考"


def test_trust_tier_success_below_bar_is_early() -> None:
    assert scoring.trust_tier(
        n=8, quant_n=8, quant_hit_rate=Decimal("0.59"), narrative_success_rate=None,
        calib_error_pp=Decimal("3"),
    ) == "早期"


def test_trust_tier_calibration_above_bar_is_early() -> None:
    assert scoring.trust_tier(
        n=8, quant_n=8, quant_hit_rate=Decimal("0.9"), narrative_success_rate=None,
        calib_error_pp=Decimal("10.01"),
    ) == "早期"


def test_trust_tier_unknown_calibration_is_early_not_sufficient() -> None:
    # No calibration evidence (None) is NOT evidence of calibration.
    assert scoring.trust_tier(
        n=8, quant_n=8, quant_hit_rate=Decimal("0.9"), narrative_success_rate=None,
        calib_error_pp=None,
    ) == "早期"


def test_trust_tier_narrative_only_combo_uses_the_miss_rate_leg() -> None:
    # quant_n == 0 → the "0" quant_hit_rate must NOT be consumed (combo_score reports
    # "0" for 0/0 — reading it as a real 0% would demote every narrative-only task).
    assert scoring.trust_tier(
        n=8, quant_n=0, quant_hit_rate=Decimal("0"),
        narrative_success_rate=Decimal("0.75"), calib_error_pp=Decimal("5"),
    ) == "可參考"


def test_trust_tier_no_success_evidence_at_all_is_early() -> None:
    assert scoring.trust_tier(
        n=8, quant_n=0, quant_hit_rate=None, narrative_success_rate=None,
        calib_error_pp=Decimal("5"),
    ) == "早期"


# --- confidence_ceiling (W7.1) -------------------------------------------------------
# The anchoring law's arithmetic moved into code after the first live run had 0 of 13 cards
# obey it; the RULE is unchanged and still lives in the prompt (AI-D33: no code-side clamp).


def _bin(bucket: str, n: int, actual: str) -> dict[str, object]:
    return {"bucket": bucket, "n": n, "actual_pct": actual}


_LIVE_BINS = [_bin("40-60", 35, "17.14"), _bin("60-80", 10, "0.00")]


def test_confidence_ceiling_is_the_largest_self_consistent_value() -> None:
    """The law is circular — a value's cap depends on its own bucket — so 39 is the answer
    for the demo's real bins: every value in 40-60 exceeds 17.14+5, every value in 60-80
    exceeds 0+5, and 20-40 has never been scored so it caps at 70."""
    assert scoring.confidence_ceiling(_LIVE_BINS, gap=None) == 39


def test_confidence_ceiling_subtracts_a_negative_gap_in_points() -> None:
    """The prompt's own worked example: gap −0.050 → −5."""
    assert scoring.confidence_ceiling(_LIVE_BINS, gap=Decimal("-0.05")) == 34


def test_confidence_ceiling_floors_at_zero_and_does_not_invent_one() -> None:
    """DISPROOF of a hidden floor: the demo's −0.466 gap drives the ceiling to exactly 0.

    A floor here would be this layer quietly overruling the owner's law; 0 is the honest
    reading ("your record supports asserting nothing") and the prompt says so in words.
    """
    assert scoring.confidence_ceiling(_LIVE_BINS, gap=Decimal("-0.466")) == 0


def test_confidence_ceiling_ignores_a_positive_gap() -> None:
    assert scoring.confidence_ceiling(_LIVE_BINS, gap=Decimal("0.30")) == 39


def test_confidence_ceiling_with_no_history_is_the_no_data_cap() -> None:
    assert scoring.confidence_ceiling([], gap=None) == scoring.CEILING_NO_DATA


def test_confidence_ceiling_below_the_sample_gate_does_not_anchor() -> None:
    """A bucket with n<8 caps at 70 even when its measured hit rate is 0 — the same honesty
    gate as the event study: a handful of rows is not evidence."""
    assert scoring.confidence_ceiling([_bin("60-80", 3, "0.00")], gap=None) == 70


def test_confidence_ceiling_rewards_a_well_calibrated_record() -> None:
    """72% actual in the 60-80 bucket admits 77 (= 72+5); 78/79 would exceed their own cap
    and 80+ falls into an unscored bucket capped at 70."""
    assert scoring.confidence_ceiling([_bin("60-80", 40, "72.00")], gap=None) == 77


def test_confidence_ceiling_tolerates_a_malformed_bucket_label() -> None:
    """A bin is data, not a contract — an unparseable label is skipped, never raised on."""
    assert scoring.confidence_ceiling(
        [{"bucket": "n/a", "n": 99, "actual_pct": "5.00"}], gap=None
    ) == scoring.CEILING_NO_DATA
