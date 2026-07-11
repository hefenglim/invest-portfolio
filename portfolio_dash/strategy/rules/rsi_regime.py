"""Rule ④: RSI(14) regime + 52-week position — a CONTEXT rule (smallest weight).

Overbought / oversold is treated as situational context, NOT a standalone entry/exit
signal, so the score magnitude is HALVED by design: oversold ``+0.5`` (mild support),
overbought ``-0.5`` (mild froth), neutral ``0``. Reuses the audited pure functions in
``portfolio.technicals`` (``rsi`` and ``week52_position``) — this rule computes no RSI
math of its own (single source of truth). Needs ``period + 1`` closes for RSI, else
``None``.
"""

from decimal import Decimal

from portfolio_dash.portfolio import technicals
from portfolio_dash.strategy.rules.params import RsiRegimeParams
from portfolio_dash.strategy.rules.types import RuleState

_HALF = Decimal("0.5")
_ZERO = Decimal("0")


def evaluate(closes: list[Decimal], params: RsiRegimeParams) -> RuleState | None:
    """RSI regime state + 52-week position context. ``None`` when RSI is uncomputable."""
    rsi_value = technicals.rsi(closes, params.period)
    if rsi_value is None:
        return None

    if rsi_value >= params.overbought:
        state, score = "overbought", -_HALF
    elif rsi_value <= params.oversold:
        state, score = "oversold", _HALF
    else:
        state, score = "neutral", _ZERO

    w52 = technicals.week52_position(closes, params.week52_window)
    window_days = w52["window_days"]
    if not isinstance(window_days, int):
        window_days = 0

    evidence: dict[str, object] = {
        "rsi14": rsi_value,
        "overbought": params.overbought,
        "oversold": params.oversold,
        "pct_from_52w_high": w52["pct_from_high"],
        "pct_from_52w_low": w52["pct_from_low"],
        "week52_window_days": window_days,
    }
    return RuleState(state=state, score=score, evidence=evidence, window_days=window_days)
