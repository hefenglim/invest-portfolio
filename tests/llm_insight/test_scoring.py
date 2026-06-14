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
