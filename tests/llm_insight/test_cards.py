"""Unit tests for the insight card + prediction schema (spec 04.10).

The card is the structured JSON the default LLM role emits via complete_structured. A
``prediction`` is optional (pure-narrative cards omit it); when present, ``confidence``
is required (it drives calib_gap / calibration_bins). Money/target_pct is a Decimal —
never float — serialized as a string via the API wire encoder.
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from portfolio_dash.api.serialize import to_wire
from portfolio_dash.llm_insight.cards import InsightCard, Prediction


def test_prediction_minimal() -> None:
    p = Prediction(metric="price_change", direction="up", horizon_days=5)
    assert p.metric == "price_change"
    assert p.direction == "up"
    assert p.target_pct is None
    assert p.horizon_days == 5


def test_prediction_target_pct_is_decimal() -> None:
    p = Prediction(
        metric="price_change", direction="up", target_pct=Decimal("0.05"), horizon_days=3
    )
    assert isinstance(p.target_pct, Decimal)
    assert p.target_pct == Decimal("0.05")


def test_prediction_metric_literal_rejects_unknown() -> None:
    with pytest.raises(ValidationError):
        Prediction(metric="moon", direction="up", horizon_days=5)  # type: ignore[arg-type]


def test_prediction_metric_accepts_three_kinds() -> None:
    for metric in ("price_change", "volatility", "relative"):
        p = Prediction(metric=metric, direction="up", horizon_days=5)  # type: ignore[arg-type]
        assert p.metric == metric


def test_card_narrative_only_no_prediction() -> None:
    c = InsightCard(
        title="台積電觀察", summary="量縮整理", body_md="**2330** 量縮整理，無明確方向。",
        tags=["TW", "semiconductor"],
    )
    assert c.prediction is None
    assert c.confidence is None
    assert c.symbol is None
    assert c.tags == ["TW", "semiconductor"]


def test_card_with_prediction_requires_confidence() -> None:
    # confidence is mandatory when a prediction is present (spec 04.10).
    with pytest.raises(ValidationError):
        InsightCard(
            title="t", summary="s", body_md="b", tags=[],
            prediction=Prediction(metric="price_change", direction="up", horizon_days=5),
        )


def test_card_with_prediction_and_confidence_ok() -> None:
    c = InsightCard(
        title="t", summary="s", body_md="b", tags=["x"], symbol="2330", confidence=70,
        prediction=Prediction(
            metric="price_change", direction="up", target_pct=Decimal("0.05"),
            horizon_days=5,
        ),
    )
    assert c.confidence == 70
    assert c.symbol == "2330"
    assert c.prediction is not None and c.prediction.target_pct == Decimal("0.05")


def test_confidence_range_0_to_100() -> None:
    for bad in (-1, 101):
        with pytest.raises(ValidationError):
            InsightCard(title="t", summary="s", body_md="b", tags=[], confidence=bad)
    # boundaries are valid
    assert InsightCard(title="t", summary="s", body_md="b", tags=[], confidence=0).confidence == 0
    hi = InsightCard(title="t", summary="s", body_md="b", tags=[], confidence=100)
    assert hi.confidence == 100


def test_card_json_roundtrip_decimal_as_string() -> None:
    c = InsightCard(
        title="t", summary="s", body_md="b", tags=["a"], symbol="AAPL", confidence=80,
        prediction=Prediction(
            metric="volatility", direction="down", target_pct=Decimal("0.12"),
            horizon_days=10,
        ),
    )
    wire = to_wire(c.model_dump())
    assert wire["prediction"]["target_pct"] == "0.12"  # Decimal -> string
    assert isinstance(wire["prediction"]["target_pct"], str)
    # round-trips back into the model from the wire dict
    back = InsightCard.model_validate(wire)
    assert back.prediction is not None
    assert back.prediction.target_pct == Decimal("0.12")
    assert back.confidence == 80


def test_card_parses_from_llm_json_string() -> None:
    raw = (
        '{"title":"t","summary":"s","body_md":"b","tags":["x"],"symbol":"2330",'
        '"confidence":65,"prediction":{"metric":"price_change","direction":"up",'
        '"target_pct":"0.03","horizon_days":5}}'
    )
    c = InsightCard.model_validate_json(raw)
    assert c.prediction is not None
    assert c.prediction.target_pct == Decimal("0.03")  # string -> Decimal
    assert c.confidence == 65
