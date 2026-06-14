"""Pure shadow / auto-promote / regression decisions (spec 04.6). Deterministic, no LLM.

Loop 4 (自升級) semantics:
- **Shadow detection** — when the user's manually-selected active calibration version is NOT
  the latest non-archived version, the latest automatically becomes the SHADOW. With no
  manual selection (active None), a shadow only exists once ≥2 versions exist (the latest is
  the candidate; the implicit active is the prior).
- **Promotion** — a shadow that has accumulated ≥ ``shadow_batches`` evaluations AND is NOT
  WORSE than the active wins (spec 4.6). "Not worse" = shadow miss-rate ≤ active miss-rate
  AND shadow avg narrative ≥ active avg narrative.
- **Regression** — the active version's recent rolling score worsening (n≥8) → an info alert
  (``calibration_regression``).

These are confined to deterministic code (spec 4.8: clustering + win/loss decisions are code;
the LLM only writes calibration text + narrative scores). PURE ``llm_insight``: stdlib only.
No money/price math; ratios are :class:`~decimal.Decimal` (never float).
"""

from decimal import Decimal
from typing import Any, Literal

PromotionVerdict = Literal["promote", "hold"]

# A regression fires only when BOTH the recent and the baseline window have at least this
# many samples (so a meaningful rolling comparison exists — the active version needs ≥8 total
# evaluations split into two halves of ≥4), and only when the recent miss rate exceeds the
# baseline by more than the margin (fractional; 0.20 = 20 percentage points).
_REGRESSION_MIN_WINDOW = 4
_REGRESSION_MARGIN = Decimal("0.20")


def shadow_version(*, active_version: int | None, latest_version: int | None) -> int | None:
    """The shadow calibration version, or None when there is none (spec 4.6).

    - active selected and < latest → the latest is the shadow;
    - active selected and == latest → no shadow (cost zero);
    - active None and ≥2 versions exist → the latest is the shadow candidate;
    - active None and ≤1 version → no shadow (nothing to compare).
    """
    if latest_version is None:
        return None
    if active_version is None:
        return latest_version if latest_version >= 2 else None
    return latest_version if latest_version > active_version else None


def _miss_rate(score: dict[str, Any]) -> Decimal:
    n = int(score.get("n", 0))
    if n == 0:
        return Decimal("0")
    return Decimal(int(score.get("miss_count", 0))) / Decimal(n)


def _avg_narrative(score: dict[str, Any]) -> Decimal:
    return Decimal(str(score.get("avg_narrative", "0")))


def _not_worse(active: dict[str, Any], shadow: dict[str, Any]) -> bool:
    """Whether the shadow is NOT worse than the active (lower/equal miss + ≥ narrative)."""
    return (
        _miss_rate(shadow) <= _miss_rate(active)
        and _avg_narrative(shadow) >= _avg_narrative(active)
    )


def decide_promotion(
    active_score: dict[str, Any], shadow_score: dict[str, Any], cfg: dict[str, Any]
) -> PromotionVerdict:
    """Whether to promote the shadow over the active (spec 4.6). Pure + deterministic.

    Promote when the shadow has ≥ ``shadow_batches`` evaluations AND is not worse than the
    active; otherwise hold. A shadow with no samples always holds.
    """
    shadow_n = int(shadow_score.get("n", 0))
    if shadow_n == 0:
        return "hold"
    if shadow_n < int(cfg.get("shadow_batches", 0)):
        return "hold"
    return "promote" if _not_worse(active_score, shadow_score) else "hold"


def is_regressing(
    *, recent_miss: int, recent_n: int, baseline_miss: int, baseline_n: int
) -> bool:
    """Whether the active version's recent rolling score has regressed (spec 4.6).

    Fires only when BOTH windows have ≥4 samples (an active version with ≥8 total evaluations
    split into recent/baseline halves) and the recent miss rate exceeds the baseline miss
    rate by more than the regression margin. Deterministic.
    """
    if recent_n < _REGRESSION_MIN_WINDOW or baseline_n < _REGRESSION_MIN_WINDOW:
        return False
    recent_rate = Decimal(recent_miss) / Decimal(recent_n)
    baseline_rate = Decimal(baseline_miss) / Decimal(baseline_n)
    return recent_rate - baseline_rate > _REGRESSION_MARGIN
