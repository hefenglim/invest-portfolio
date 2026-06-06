"""Shared annotated types for domain models."""

from decimal import Decimal
from typing import Annotated

from pydantic import Field

# A Decimal constrained to finite values (no NaN / Infinity). pydantic's Decimal core
# schema already rejects non-finite values; the explicit constraint documents the money
# invariant at the type level. Negative and zero values are allowed (e.g. adjusted cost
# may go <= 0 after cumulative dividends exceed cost).
Money = Annotated[Decimal, Field(allow_inf_nan=False)]
