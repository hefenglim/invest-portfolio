"""Rule-engine entry point: ``evaluate_symbol`` runs all four rules and composes.

The public seam for the api layer (which reads prices, then calls this — mirroring how
``insight_service`` feeds ``closes`` into ``technicals``). Everything is Decimal end to
end and no quantization happens here (display/wire rounding is the variable layer's job,
batch 2C). The parameter version is stamped onto every result for replay/rebuild.
"""

from collections.abc import Sequence
from decimal import Decimal

from portfolio_dash.strategy.rules import ma_cross, momentum, rsi_regime, trend_filter
from portfolio_dash.strategy.rules.composite import compose
from portfolio_dash.strategy.rules.params import (
    PARAMS_VERSION,
    RulesParams,
    default_params,
)
from portfolio_dash.strategy.rules.types import RuleState, SymbolSignals


def evaluate_symbol(
    closes: list[Decimal],
    volumes: Sequence[Decimal | None] | None = None,
    params: RulesParams | None = None,
) -> SymbolSignals | None:
    """Evaluate the four v1 rules over ``closes`` (+ optional aligned ``volumes``).

    Returns a :class:`SymbolSignals` with each rule's state (or ``None`` where the
    series is too short), the composite (or ``None`` if fewer than 2 rules are
    evaluable), and the parameter-version stamp. Empty/absent ``closes`` → ``None``.
    """
    if not closes:
        return None
    resolved = params if params is not None else default_params()

    rules: dict[str, RuleState | None] = {
        "trend_filter": trend_filter.evaluate(closes, resolved.trend),
        "ma_cross": ma_cross.evaluate(closes, volumes, resolved.cross),
        "momentum_12_1": momentum.evaluate(closes, resolved.momentum),
        "rsi_regime": rsi_regime.evaluate(closes, resolved.rsi),
    }
    composite = compose(rules, resolved.weights)
    return SymbolSignals(
        rules=rules, composite=composite, params_version=PARAMS_VERSION
    )
