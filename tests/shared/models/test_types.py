from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError

from portfolio_dash.shared.models.types import Money


def test_money_accepts_finite() -> None:
    assert TypeAdapter(Money).validate_python(Decimal("1.50")) == Decimal("1.50")


def test_money_accepts_negative() -> None:
    # adjusted cost may go <= 0 once cumulative dividends exceed cost.
    assert TypeAdapter(Money).validate_python(Decimal("-100.00")) == Decimal("-100.00")


def test_money_accepts_zero() -> None:
    assert TypeAdapter(Money).validate_python(Decimal("0")) == Decimal("0")


def test_money_rejects_nan() -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(Money).validate_python(Decimal("NaN"))


def test_money_rejects_infinity() -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(Money).validate_python(Decimal("Infinity"))


def test_money_rejects_negative_infinity() -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(Money).validate_python(Decimal("-Infinity"))
