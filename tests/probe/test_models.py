from decimal import Decimal

from scripts.probe.models import DataType, ProbeResult, Verdict


def test_probe_result_minimal_and_defaults() -> None:
    r = ProbeResult(
        source="yfinance",
        data_type=DataType.QUOTE_LATEST,
        market="US",
        requires_key=False,
        verdict=Verdict.PRIMARY,
    )
    assert r.coverage_hits == 0
    assert r.coverage_misses == []
    assert r.verdict is Verdict.PRIMARY
    assert r.sample_value is None


def test_probe_result_rejects_non_finite_sample() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ProbeResult(
            source="x", data_type=DataType.FX, market="FX",
            requires_key=False, verdict=Verdict.UNUSABLE,
            sample_value=Decimal("NaN"),
        )
