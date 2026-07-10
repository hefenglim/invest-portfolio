"""Composite TechScore: hand-computed blends, honest coverage, context table.

TechScore = ``50 + Σ(score × renormalized_weight × 0.5)`` clamped to [0, 100]. Rules
are fed as fixed :class:`RuleState` objects so the arithmetic is exact and auditable.
"""

from decimal import Decimal

from portfolio_dash.strategy.rules import composite as C
from portfolio_dash.strategy.rules.params import CompositeWeights
from portfolio_dash.strategy.rules.types import RuleState

_W = CompositeWeights()


def _rule(state: str, score: str) -> RuleState:
    return RuleState(state=state, score=Decimal(score), evidence={}, window_days=1)


def test_all_bullish_maxes_to_expected() -> None:
    # trend +1(w30), cross +1(w25), momentum +1(w25), rsi +0.5(w20).
    # contributions: 15 + 12.5 + 12.5 + 5 = 45 -> 50 + 45 = 95.
    rules = {
        "trend_filter": _rule("above_confirmed", "1"),
        "ma_cross": _rule("golden", "1"),
        "momentum_12_1": _rule("positive", "1"),
        "rsi_regime": _rule("neutral", "0.5"),
    }
    comp = C.compose(rules, _W)
    assert comp is not None
    assert comp.tech_score == Decimal("95")
    assert comp.coverage == "4/4"
    assert comp.missing == ()
    assert comp.contributions["trend_filter"] == Decimal("15")
    assert comp.contributions["rsi_regime"] == Decimal("5")
    # Contributions sum to tech_score - 50.
    assert sum(comp.contributions.values()) == comp.tech_score - Decimal("50")


def test_all_neutral_is_fifty() -> None:
    rules = {name: _rule("neutral", "0") for name in C.RULE_ORDER}
    comp = C.compose(rules, _W)
    assert comp is not None
    assert comp.tech_score == Decimal("50")


def test_all_bearish_floor_direction() -> None:
    rules = {
        "trend_filter": _rule("below_confirmed", "-1"),
        "ma_cross": _rule("death", "-1"),
        "momentum_12_1": _rule("negative", "-1"),
        "rsi_regime": _rule("neutral", "-0.5"),
    }
    comp = C.compose(rules, _W)
    assert comp is not None
    assert comp.tech_score == Decimal("5")  # 50 - 45


def test_coverage_renormalizes_missing_rules() -> None:
    # Only trend +1(w30) and rsi +0.5(w20) present -> weights 30/20 renorm to 60/40.
    # contributions: 1*60*0.5=30 ; 0.5*40*0.5=10 -> 50 + 40 = 90.
    rules: dict[str, RuleState | None] = {
        "trend_filter": _rule("above_confirmed", "1"),
        "ma_cross": None,
        "momentum_12_1": None,
        "rsi_regime": _rule("oversold", "0.5"),
    }
    comp = C.compose(rules, _W)
    assert comp is not None
    assert comp.tech_score == Decimal("90")
    assert comp.coverage == "2/4"
    assert comp.missing == ("ma_cross", "momentum_12_1")
    assert comp.weights_applied == {
        "trend_filter": Decimal("60"), "rsi_regime": Decimal("40")}
    assert sum(comp.weights_applied.values()) == Decimal("100")


def test_fewer_than_two_rules_is_none() -> None:
    rules: dict[str, RuleState | None] = {
        "trend_filter": _rule("above_confirmed", "1"),
        "ma_cross": None, "momentum_12_1": None, "rsi_regime": None,
    }
    assert C.compose(rules, _W) is None


def test_score_is_clamped_to_bounds() -> None:
    # Deliberately out-of-range scores to exercise the clamp guard.
    high = {name: _rule("x", "2") for name in C.RULE_ORDER}
    low = {name: _rule("x", "-2") for name in C.RULE_ORDER}
    hc, lc = C.compose(high, _W), C.compose(low, _W)
    assert hc is not None and lc is not None
    assert hc.tech_score == Decimal("100")
    assert lc.tech_score == Decimal("0")


def test_evaluation_context_table() -> None:
    ev = C.evaluation_context
    mid = Decimal("50")
    assert ev("above_confirmed", "positive", mid)[0] == "strong_uptrend"
    assert ev("above_confirmed", "flat", mid)[0] == "uptrend_pullback"
    assert ev("above_confirmed", "negative", mid)[0] == "uptrend_pullback"
    assert ev("below_confirmed", "negative", mid)[0] == "strong_downtrend"
    assert ev("below_confirmed", "positive", mid)[0] == "downtrend_rally"
    assert ev("below_confirmed", "flat", mid)[0] == "downtrend_rally"
    assert ev("in_band", "positive", mid)[0] == "range_bound"
    assert ev("above_unconfirmed", "flat", mid)[0] == "range_bound"


def test_evaluation_context_falls_back_to_score_band_when_trend_missing() -> None:
    ev = C.evaluation_context
    assert ev(None, "positive", Decimal("80"))[0] == "strong_uptrend"
    assert ev(None, "flat", Decimal("80"))[0] == "uptrend_pullback"
    assert ev(None, "negative", Decimal("20"))[0] == "strong_downtrend"
    assert ev(None, "flat", Decimal("50"))[0] == "range_bound"


def test_context_notes_cover_every_label_in_zh_tw() -> None:
    labels = {"strong_uptrend", "uptrend_pullback", "range_bound",
              "downtrend_rally", "strong_downtrend", C.INSUFFICIENT_DATA}
    assert set(C.CONTEXT_NOTES) == labels
    for note in C.CONTEXT_NOTES.values():
        assert note and isinstance(note, str)
