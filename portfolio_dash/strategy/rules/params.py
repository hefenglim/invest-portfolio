"""Frozen v1 rule parameters (``rules-v1``).

Every parameter the rule engine uses lives here as a **frozen dataclass**, so a run is
fully reproducible from ``(closes, volumes, params)`` and the parameter set can be
stamped onto every result (``PARAMS_VERSION``) — the same replay/rebuild discipline as
the per-transaction fee/tax snapshot (``domain-ledger.md``). v1 is deliberately frozen
in code; a UI-editable seam is deferred to P4 (do not pre-build it).

All numeric thresholds are :class:`~decimal.Decimal` (never ``float``) — the engine is
Decimal end to end (``data-and-pricing.md``).
"""

from dataclasses import dataclass, field
from decimal import Decimal

PARAMS_VERSION = "rules-v1"


@dataclass(frozen=True)
class TrendFilterParams:
    """MA(200) trend filter with a hysteresis band + N-day confirmation.

    ``band`` is a fractional half-width (``0.02`` = ±2%): a close must clear
    ``ma*(1+band)`` to read *above* and drop below ``ma*(1-band)`` to read *below*;
    in between is the neutral in-band zone. ``confirm_days`` sessions in the same raw
    zone are required before the state is *confirmed* (whipsaw suppression — Faber /
    Zakamulin: the value of MA rules is risk control, not out-performance).
    """

    ma: int = 200
    band: Decimal = Decimal("0.02")
    confirm_days: int = 2


@dataclass(frozen=True)
class MaCrossParams:
    """SMA(50) vs SMA(200) golden/death cross with volume confirmation + age decay.

    ``volume_confirm`` gates the confidence modifier (empirical base: high-volume
    confirmation ~72% vs low-volume ~54% — encoded as a score multiplier, see
    ``ma_cross.py``). ``cross_lookback`` bounds how far back a cross is detected; it
    equals ``decay_sessions`` by default so a *detected* cross always still carries a
    non-zero (un-decayed) contribution, and anything older reverts to the standing
    fast-vs-slow relationship instead of a stale cross.
    """

    fast: int = 50
    slow: int = 200
    volume_confirm: bool = True
    volume_window: int = 20
    cross_lookback: int = 120
    decay_sessions: int = 120


@dataclass(frozen=True)
class MomentumParams:
    """12-1 time-series momentum (Moskowitz TSMOM / Antonacci).

    ``lookback_sessions`` ≈ 12 months (252 trading sessions); ``skip_sessions`` ≈ 1
    month (21 sessions) skipped at the recent end to avoid the short-term reversal.
    ``flat_epsilon`` is the |return| band labelled *flat*; ``full_scale`` is the
    absolute 12-1 return that maps to a full ±1 score contribution.
    """

    lookback_sessions: int = 252
    skip_sessions: int = 21
    flat_epsilon: Decimal = Decimal("0.005")
    full_scale: Decimal = Decimal("0.30")


@dataclass(frozen=True)
class RsiRegimeParams:
    """RSI(14) regime + 52-week position — a CONTEXT rule (smallest weight).

    Overbought/oversold are context, not a standalone entry/exit signal, so the score
    magnitude is halved by design (see ``rsi_regime.py``).
    """

    period: int = 14
    overbought: Decimal = Decimal("70")
    oversold: Decimal = Decimal("30")
    week52_window: int = 252


@dataclass(frozen=True)
class CompositeWeights:
    """Frozen contribution weights for the composite TechScore (sum = 100)."""

    trend: Decimal = Decimal("30")
    cross: Decimal = Decimal("25")
    momentum: Decimal = Decimal("25")
    rsi_context: Decimal = Decimal("20")


@dataclass(frozen=True)
class RulesParams:
    """Bundle of every rule's parameters + the composite weights."""

    trend: TrendFilterParams = field(default_factory=TrendFilterParams)
    cross: MaCrossParams = field(default_factory=MaCrossParams)
    momentum: MomentumParams = field(default_factory=MomentumParams)
    rsi: RsiRegimeParams = field(default_factory=RsiRegimeParams)
    weights: CompositeWeights = field(default_factory=CompositeWeights)


def default_params() -> RulesParams:
    """The canonical frozen v1 parameter set stamped as :data:`PARAMS_VERSION`."""
    return RulesParams()
