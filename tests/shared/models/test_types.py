from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError

from portfolio_dash.shared.models.types import Money


def test_money_accepts_finite() -> None:
    assert TypeAdapter(Money).validate_python(Decimal("1.50")) == Decimal("1.50")


def test_money_rejects_nan() -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(Money).validate_python(Decimal("NaN"))


def test_money_rejects_infinity() -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(Money).validate_python(Decimal("Infinity"))
