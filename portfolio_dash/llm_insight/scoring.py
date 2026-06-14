"""Pure quantitative scoring (spec 04.4): the LLM-free, objective verdict layer.

``score_quant`` compares a card's :class:`~llm_insight.cards.Prediction` against the actual
measurement FED IN by the api/pricing seam (``api/insight_service.py`` reads price-at-create
vs price-at-due, benchmark return, fx — never this layer). The function is total and pure:
given the same inputs it always returns the same verdict, so it is trivially unit-tested
with fixed fixtures (architecture.md).

Verdict semantics (spec 4.4 step 2):
- returns ``True``  → the prediction was objectively correct (a quant hit);
- returns ``False`` → objectively wrong (a quant miss);
- returns ``None``  → the actual value was unavailable → the caller defers as
  ``pending_data`` and NEVER force-judges a miss (spec 04.10 anti-poison).

``decide_miss`` folds the quant verdict with the master's narrative score into the combined
miss flag; ``calibration_error`` computes the confidence-vs-hit-rate gap in percentage
points. All math is :class:`~decimal.Decimal` (never float for a price/rate/ratio).
"""

from decimal import Decimal

from pydantic import BaseModel

from portfolio_dash.llm_insight.cards import Prediction

# The flat band: |move| within ±0.5% counts as "flat" for a direction=flat prediction.
_FLAT_BAND = Decimal("0.005")


class ActualMeasurement(BaseModel):
    """The objective measurement an evaluation needs, fed by the api/pricing seam.

    Only the field(s) a given metric needs are populated; the rest stay ``None``. A ``None``
    in the field a metric requires means "unavailable" → ``score_quant`` returns ``None``
    (pending_data, not a miss).

    - ``price_change_pct`` — fractional move create→due (0.03 = +3%); for ``price_change``.
    - ``vol_change_pct``   — fractional change in realized volatility; for ``volatility``.
    - ``symbol_return_pct`` / ``benchmark_return_pct`` — fractional returns; for ``relative``.
    """

    model_config = {"arbitrary_types_allowed": True}

    price_change_pct: Decimal | None = None
    vol_change_pct: Decimal | None = None
    symbol_return_pct: Decimal | None = None
    benchmark_return_pct: Decimal | None = None


def _direction_hit(direction: str, move: Decimal, *, band: Decimal = _FLAT_BAND) -> bool:
    """Whether a signed *move* matches a directional claim (with a flat band for ``flat``)."""
    if direction == "flat":
        return abs(move) <= band
    if direction == "up":
        return move > Decimal("0")
    return move < Decimal("0")  # down


def _score_price_change(pred: Prediction, m: ActualMeasurement) -> bool | None:
    move = m.price_change_pct
    if move is None:
        return None
    if pred.target_pct is not None and pred.direction in ("up", "down"):
        # Magnitude claim: the move must reach the target in the predicted direction.
        target = abs(pred.target_pct)
        if pred.direction == "up":
            return move >= target
        return move <= -target
    return _direction_hit(pred.direction, move)


def _score_volatility(pred: Prediction, m: ActualMeasurement) -> bool | None:
    change = m.vol_change_pct
    if change is None:
        return None
    # A volatility prediction is a regime call: up = vol rose, down = vol fell, flat = stable.
    return _direction_hit(pred.direction, change)


def _score_relative(pred: Prediction, m: ActualMeasurement) -> bool | None:
    if m.symbol_return_pct is None or m.benchmark_return_pct is None:
        return None
    excess = m.symbol_return_pct - m.benchmark_return_pct
    return _direction_hit(pred.direction, excess)


def score_quant(prediction: Prediction, actual: ActualMeasurement | None) -> bool | None:
    """Objectively verify a prediction against the fed actual measurement (spec 4.4).

    Returns ``True``/``False`` for a clear hit/miss, or ``None`` when the actual value is
    unavailable (→ caller defers as pending_data, never a miss). Pure + total.
    """
    if actual is None:
        return None
    if prediction.metric == "price_change":
        return _score_price_change(prediction, actual)
    if prediction.metric == "volatility":
        return _score_volatility(prediction, actual)
    return _score_relative(prediction, actual)


def decide_miss(
    *, quant_hit: bool | None, narrative_score: int | None, threshold: int
) -> bool:
    """Combine the objective quant verdict + the master narrative score into the miss flag.

    Rules (deterministic — the LLM never decides this; spec 4.8):
    - an objective quant miss (``quant_hit is False``) is a miss regardless of narrative;
    - a quant hit (``True``) is never a miss (the verifiable claim held);
    - with no quant signal (pure-narrative card), a narrative score below *threshold* is a
      miss; at/above *threshold* it is not;
    - no signal at all (quant None + narrative None — master skipped) cannot be judged → not
      a miss (anti-poison: an unjudgeable card never counts against the combo).
    """
    if quant_hit is False:
        return True
    if quant_hit is True:
        return False
    if narrative_score is None:
        return False
    return narrative_score < threshold


def should_calibrate(
    *,
    resolved_samples: int,
    min_samples: int,
    consecutive_misses: int,
    miss_count: int,
    gap_alert_pp: Decimal,
) -> bool:
    """Whether a self_correct combo should generate a new calibration version (spec 4.5).

    Gated FIRST by ``resolved_samples >= min_samples`` (spec 04.10 — small samples never
    trigger). Then ANY of: ≥3 consecutive misses, OR a miss rate exceeding ``gap_alert_pp``
    percentage points. (Output-rule violations are recorded by the validator on generation;
    they are not a separate pre-trigger here.) Pure + deterministic — the LLM never decides
    whether to calibrate, only what the new text is (spec 4.8).
    """
    if resolved_samples < min_samples or resolved_samples == 0:
        return False
    if consecutive_misses >= 3:
        return True
    miss_rate_pp = (Decimal(miss_count) / Decimal(resolved_samples)) * Decimal("100")
    return miss_rate_pp > gap_alert_pp


def calibration_error(rows: list[tuple[int, bool]]) -> Decimal:
    """Calibration error in percentage points: |avg claimed confidence − actual hit rate|.

    *rows* is ``[(confidence_0_100, hit_bool), ...]``. Empty → ``0``. The result is an exact
    Decimal (percentage points), never float.
    """
    if not rows:
        return Decimal("0")
    n = Decimal(len(rows))
    claimed = sum((Decimal(c) for c, _ in rows), Decimal("0")) / n
    hits = sum(1 for _, hit in rows if hit)
    actual = (Decimal(hits) / n) * Decimal("100")
    return abs(claimed - actual)
