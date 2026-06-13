"""Wire-format serialization: Decimal -> string, datetime/date -> ISO, Enum -> value.

The API layer never emits money as a JSON number (precision); every Decimal is a string.
Currency enum values stay as-is (uppercase); Side/DividendType lowercasing is added with
the ledger/input specs that surface them.
"""

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any


def to_wire(value: Any) -> Any:
    """Recursively convert a model_dump()/dict tree into JSON-safe wire values."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {k: to_wire(v) for k, v in value.items()}
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence):
        return [to_wire(v) for v in value]
    return value
