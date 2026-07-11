"""Technical-rules engine (Blueprint P2, batch 2A).

Parameterized *pure-function* rule modules that turn a close-price series (plus an
optional aligned volume series) into **signals of record**: per-rule state + signed
score + auditable evidence, composed into a 0–100 ``TechScore`` and an evaluation
context label. These numbers are computed LOCALLY (invariant #1: the LLM never emits
numbers of record — it only interprets what this engine produces).

Layering (``rules/architecture.md``): ``strategy/`` may import ``portfolio/`` and
``shared/``; it MUST NOT import ``llm_insight``, ``api``, ``web``, or ``scheduler``.
This batch is pure calculation — no DB, no routes, no persisted variables.

Public entry point: :func:`portfolio_dash.strategy.rules.engine.evaluate_symbol`.
"""

from portfolio_dash.strategy.rules.engine import evaluate_symbol
from portfolio_dash.strategy.rules.params import (
    PARAMS_VERSION,
    RulesParams,
    default_params,
)
from portfolio_dash.strategy.rules.types import (
    Composite,
    RuleState,
    SymbolSignals,
)

__all__ = [
    "PARAMS_VERSION",
    "Composite",
    "RuleState",
    "RulesParams",
    "SymbolSignals",
    "default_params",
    "evaluate_symbol",
]
