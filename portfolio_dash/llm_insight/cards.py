"""Insight card + prediction schema (spec 04.10): the structured JSON an insight run emits.

The default LLM role returns one :class:`InsightCard` per combo run (per symbol for
per_symbol scope). A card MAY carry a verifiable :class:`Prediction`; pure-narrative cards
omit it. When a prediction is present, ``confidence`` (0–100) is REQUIRED — it anchors the
calibration error (calib_gap) and the confidence bins (calibration_bins) in Loop 3 (04c).

Pure schema only: stdlib + pydantic. No money in float — ``target_pct`` is a
:class:`~decimal.Decimal`, serialized to a string by the shared wire encoder
(``shared.wire.to_wire``); the LLM emits it as a JSON string, parsed back to Decimal.
"""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

PredictionMetric = Literal["price_change", "volatility", "relative"]
PredictionDirection = Literal["up", "down", "flat"]


class Prediction(BaseModel):
    """A verifiable forward-looking claim attached to an insight card.

    ``metric`` is the comparison kind the evaluator (04c) checks against actual prices:
    ``price_change`` (absolute move), ``volatility`` (realized vol), ``relative`` (vs a
    benchmark). ``target_pct`` is the optional magnitude (a Decimal ratio, e.g. 0.05 =
    +5%); ``horizon_days`` is the prediction window (overrides the task default).
    """

    metric: PredictionMetric
    direction: PredictionDirection
    target_pct: Decimal | None = None
    horizon_days: int = Field(gt=0)


class InsightCard(BaseModel):
    """One structured insight card (spec 04.10 forced-JSON shape).

    ``symbol`` is set for a per_symbol card, None for a portfolio card. ``confidence`` is
    0–100 and REQUIRED whenever ``prediction`` is present (the validator enforces this);
    pure-narrative cards leave both None.
    """

    title: str
    summary: str
    body_md: str
    tags: list[str] = Field(default_factory=list)
    symbol: str | None = None
    confidence: int | None = Field(default=None, ge=0, le=100)
    prediction: Prediction | None = None

    @model_validator(mode="after")
    def _confidence_required_with_prediction(self) -> "InsightCard":
        """A card carrying a prediction MUST state a confidence (spec 04.10)."""
        if self.prediction is not None and self.confidence is None:
            raise ValueError("confidence is required when a prediction is present")
        return self
