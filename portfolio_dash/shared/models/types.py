"""Shared annotated types for domain models."""

from decimal import Decimal
from typing import Annotated

from pydantic import AfterValidator


def _ensure_finite(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError(f"value must be finite, got {value!r}")
    return value


# A Decimal that rejects NaN / Infinity at the model boundary.
Money = Annotated[Decimal, AfterValidator(_ensure_finite)]
