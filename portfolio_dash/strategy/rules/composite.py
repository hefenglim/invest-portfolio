"""Composite TechScore + evaluation context — the auditable weighted blend.

Formula (trivially auditable):

    tech_score = 50 + Σ( rule.score × renormalized_weight × 0.5 )   clamped to [0, 100]

With every rule scoring ``+1`` and weights summing to 100 the score is 100; all ``-1``
→ 0; all neutral → 50. Rules with insufficient data (``None``) are excluded and the
REMAINING weights are renormalized back to 100 (to Decimal precision — a non-dividing
weight subset sums to 100 minus an ulp, never materially off), so an under-covered
symbol is scored honestly on what is available. Fewer than 2 evaluable rules → no
composite (``None``): too thin to score.

``evaluation_context`` is a deterministic label (health-check add/trim vocabulary) with
a one-line zh-TW condition sentence, derived only from the trend + momentum states and
the score band via a plain, fully unit-tested table.
"""

from collections.abc import Mapping
from decimal import Decimal

from portfolio_dash.strategy.rules.params import CompositeWeights
from portfolio_dash.strategy.rules.types import Composite, RuleState

_ZERO = Decimal("0")
_HALF = Decimal("0.5")
_FIFTY = Decimal("50")
_HUNDRED = Decimal("100")
_BAND_HIGH = Decimal("65")
_BAND_LOW = Decimal("35")

# Fixed evaluation order; also the total for the coverage fraction.
RULE_ORDER: tuple[str, ...] = ("trend_filter", "ma_cross", "momentum_12_1", "rsi_regime")

# Health-check add/trim framework vocabulary + one-line zh-TW condition sentences.
# ``uptrend``/``downtrend`` are the momentum-UNKNOWN variants (deep review 2026-07-10):
# when the momentum rule has too little history to evaluate, the label must not claim
# a momentum direction it never measured — "動能轉弱" for unmeasured momentum is false.
INSUFFICIENT_DATA = "insufficient_data"
CONTEXT_NOTES: dict[str, str] = {
    "strong_uptrend": "強勢上升趨勢——價格站穩 MA200 且動能為正，回測不破帶前可續抱觀察。",
    "uptrend_pullback": "上升趨勢但動能轉弱——回檔或整理，留意 MA200 帶是否守住。",
    "uptrend": "上升趨勢確立——動能樣本不足暫不判讀；回測不破 MA200 帶前可續抱觀察。",
    "range_bound": "區間震盪——趨勢未定，等待方向突破，避免追高殺低。",
    "downtrend_rally": "下降趨勢中的反彈——尚未轉勢，反彈至均線帶宜保守。",
    "downtrend": "下降趨勢確立——動能樣本不足暫不判讀；價格位於 MA200 帶下，控制風險優先。",
    "strong_downtrend": "強勢下降趨勢——跌破 MA200 且動能為負，控制風險優先。",
    INSUFFICIENT_DATA: "資料不足——歷史長度不夠，無法形成可信的技術判斷。",
}


def _weight_of(name: str, weights: CompositeWeights) -> Decimal:
    return {
        "trend_filter": weights.trend,
        "ma_cross": weights.cross,
        "momentum_12_1": weights.momentum,
        "rsi_regime": weights.rsi_context,
    }[name]


def _clamp(value: Decimal, lo: Decimal, hi: Decimal) -> Decimal:
    return lo if value < lo else hi if value > hi else value


def _trend_direction(trend_state: str | None, tech_score: Decimal) -> str:
    """up / down / neutral. Falls back to the score band when the trend rule is absent."""
    if trend_state is None:
        if tech_score >= _BAND_HIGH:
            return "up"
        if tech_score <= _BAND_LOW:
            return "down"
        return "neutral"
    if trend_state == "above_confirmed":
        return "up"
    if trend_state == "below_confirmed":
        return "down"
    return "neutral"  # in_band / above_unconfirmed / below_unconfirmed


def _momentum_direction(momentum_state: str | None) -> str:
    """pos / neg / flat / unknown (a MISSING momentum rule is unknown, never "flat" —
    labelling unmeasured momentum as weakening would be fabrication)."""
    if momentum_state is None:
        return "unknown"
    if momentum_state == "positive":
        return "pos"
    if momentum_state == "negative":
        return "neg"
    return "flat"


def evaluation_context(
    trend_state: str | None, momentum_state: str | None, tech_score: Decimal
) -> tuple[str, str]:
    """Deterministic (label, zh-TW note) from trend + momentum states and the score band."""
    tdir = _trend_direction(trend_state, tech_score)
    mdir = _momentum_direction(momentum_state)
    if tdir == "up":
        if mdir == "pos":
            label = "strong_uptrend"
        elif mdir == "unknown":
            label = "uptrend"
        else:
            label = "uptrend_pullback"
    elif tdir == "down":
        if mdir == "neg":
            label = "strong_downtrend"
        elif mdir == "unknown":
            label = "downtrend"
        else:
            label = "downtrend_rally"
    else:
        label = "range_bound"
    return label, CONTEXT_NOTES[label]


def compose(
    rules: Mapping[str, RuleState | None], weights: CompositeWeights
) -> Composite | None:
    """Weighted composite of the evaluable rules, or ``None`` if fewer than 2 evaluable."""
    present: list[tuple[str, RuleState]] = []
    for name in RULE_ORDER:
        state = rules.get(name)
        if state is not None:
            present.append((name, state))
    missing = tuple(name for name in RULE_ORDER if rules.get(name) is None)

    if len(present) < 2:
        return None

    weight_sum = sum((_weight_of(name, weights) for name, _ in present), _ZERO)
    if weight_sum == _ZERO:
        return None

    contributions: dict[str, Decimal] = {}
    weights_applied: dict[str, Decimal] = {}
    running = _ZERO
    for name, state in present:
        applied = _weight_of(name, weights) / weight_sum * _HUNDRED
        contribution = state.score * applied * _HALF
        weights_applied[name] = applied
        contributions[name] = contribution
        running += contribution

    tech_score = _clamp(_FIFTY + running, _ZERO, _HUNDRED)

    trend_rule = rules.get("trend_filter")
    momentum_rule = rules.get("momentum_12_1")
    trend_state = trend_rule.state if trend_rule is not None else None
    momentum_state = momentum_rule.state if momentum_rule is not None else None
    label, note = evaluation_context(trend_state, momentum_state, tech_score)

    return Composite(
        tech_score=tech_score,
        contributions=contributions,
        weights_applied=weights_applied,
        coverage=f"{len(present)}/{len(RULE_ORDER)}",
        missing=missing,
        evaluation_context=label,
        context_note=note,
    )
