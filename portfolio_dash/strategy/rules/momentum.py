"""Rule ③: 12-1 momentum (skip-month convention per Jegadeesh-Titman / Antonacci;
Moskowitz TSMOM itself uses the FULL trailing-12-month return — both are legitimate,
the skip variant avoids the short-term reversal).

The 12-month return that SKIPS the most recent ~1 month, the factor family with the
strongest long-run evidence. Return is measured over price anchors:

    r = closes[-(skip+1)] / closes[-(lookback+1)] - 1

i.e. the recent anchor is ``skip`` sessions back (default 21 ≈ 1 month) and the base
anchor is ``lookback`` sessions back (default 252 ≈ 12 months).

Score = ``sign(r) × min(1, |r| / full_scale)`` — capped linear scaling where a
``full_scale`` (default 30%) absolute 12-1 return earns a full ±1 contribution. State
labels the sign with a ``flat_epsilon`` dead-band, and a ``flat`` state forces the
score to exactly 0 (state/score consistency — the dead-band IS the no-directional-
signal zone; deep review 2026-07-10). Pure Decimal; needs ``lookback + 1`` closes
else ``None``.
"""

from decimal import Decimal

from portfolio_dash.strategy.rules.params import MomentumParams
from portfolio_dash.strategy.rules.types import RuleState

_ZERO = Decimal("0")
_ONE = Decimal("1")


def evaluate(closes: list[Decimal], params: MomentumParams) -> RuleState | None:
    """12-1 momentum state + capped-linear score. Needs ``params.lookback_sessions+1``."""
    lookback, skip = params.lookback_sessions, params.skip_sessions
    if lookback <= 0 or skip < 0 or skip >= lookback or len(closes) < lookback + 1:
        return None

    recent = closes[-(skip + 1)]
    base = closes[-(lookback + 1)]
    if base == _ZERO:
        return None
    r = recent / base - _ONE

    if r > params.flat_epsilon:
        state = "positive"
    elif r < -params.flat_epsilon:
        state = "negative"
    else:
        state = "flat"

    if state == "flat":
        score = _ZERO  # dead-band = no directional signal; score must agree with state
    else:
        sign = _ONE if r > _ZERO else -_ONE
        magnitude = (
            min(_ONE, abs(r) / params.full_scale) if params.full_scale > _ZERO else _ZERO
        )
        score = sign * magnitude

    evidence: dict[str, object] = {
        "return_12_1": r,
        "recent_price": recent,
        "base_price": base,
        "recent_offset_sessions": skip,
        "base_offset_sessions": lookback,
        "flat_epsilon": params.flat_epsilon,
        "full_scale": params.full_scale,
    }
    return RuleState(state=state, score=score, evidence=evidence, window_days=lookback + 1)
