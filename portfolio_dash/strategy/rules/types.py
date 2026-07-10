"""Result types for the rule engine — frozen, Decimal, auditable.

Every rule returns a :class:`RuleState` (or ``None`` when the series is too short to
judge honestly). The composite bundles the evaluable rules into a :class:`Composite`,
and :func:`engine.evaluate_symbol` returns one :class:`SymbolSignals` per symbol with
the parameter-version stamp attached (replay/rebuild discipline).

``evidence`` deliberately carries the *actual numbers used* (Decimals, not display
strings) so an auditor can reproduce the state/score from the recorded inputs. No
quantization happens here — display/wire rounding is the variable layer's concern
(batch 2C), mirroring the ``technicals`` ``_q`` pattern.
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class RuleState:
    """One rule's verdict for a symbol.

    * ``state`` — a rule-specific label (e.g. ``"above_confirmed"``, ``"golden"``).
    * ``score`` — the SIGNED contribution in ``[-1, +1]`` BEFORE weighting. Some rules
      cap their magnitude below 1 by design (RSI context ±0.5, no-cross relationship
      ±0.4); this is intentional, not a bug.
    * ``evidence`` — the auditable numbers the state/score were derived from.
    * ``window_days`` — the number of trailing sessions this verdict depends on.
    """

    state: str
    score: Decimal
    evidence: dict[str, object]
    window_days: int


@dataclass(frozen=True)
class Composite:
    """Weighted composite of the evaluable rules.

    * ``tech_score`` — ``50 + Σ(score × renormalized_weight × 0.5)`` clamped to
      ``[0, 100]`` (all-bullish → 100, neutral → 50, all-bearish → 0).
    * ``contributions`` — per-rule signed contribution actually applied (post
      renormalization) — the audit trail that sums to ``tech_score - 50``.
    * ``weights_applied`` — the renormalized weight each rule received (sums to 100
      to Decimal precision; a non-dividing weight subset can be an ulp short).
    * ``coverage`` — ``"3/4"`` = evaluable / total rules.
    * ``missing`` — names of rules excluded for insufficient data.
    * ``evaluation_context`` / ``context_note`` — a deterministic label + one-line
      zh-TW condition sentence for the health-check add/trim framework.
    """

    tech_score: Decimal
    contributions: dict[str, Decimal]
    weights_applied: dict[str, Decimal]
    coverage: str
    missing: tuple[str, ...]
    evaluation_context: str
    context_note: str


@dataclass(frozen=True)
class SymbolSignals:
    """The full signal set for one symbol, with the parameter-version stamp."""

    rules: dict[str, RuleState | None]
    composite: Composite | None
    params_version: str
