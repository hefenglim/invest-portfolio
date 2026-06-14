"""Wire-format serialization: Decimal -> string, datetime/date -> ISO, Enum -> value.

Lives in ``shared/`` so EVERY layer can use it without a reverse (lower→web) import
(architecture.md): lower layers never import the web layer. ``api/serialize.py``
re-exports :func:`to_wire` so existing ``api.serialize.to_wire`` callers keep working.

The API never emits money as a JSON number (precision); every Decimal is a string.
Currency enum values stay as-is (uppercase); Side/DividendType lowercasing is added with
the ledger/input specs that surface them.
"""

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any


def decimal_str(value: Decimal) -> str:
    """The ONE canonical wire form for a Decimal: ``format(value, "f")``.

    Fixed-point, full source precision (trailing zeros preserved as stored), NEVER
    scientific notation -- e.g. ``Decimal("1E-7")`` -> ``"0.0000001"`` and
    ``Decimal("1E+2")`` -> ``"100"``, while ``Decimal("0.10")`` -> ``"0.10"``. Identical
    to :func:`portfolio_dash.shared.money.to_db`; the wire keeps full precision and the
    frontend quantizes for display (data-and-pricing.md). The sign of a negative zero is
    preserved (``Decimal("-0.00")`` -> ``"-0.00"``) -- a faithful render of the stored value.
    """
    return format(value, "f")


def to_wire(value: Any) -> Any:
    """Recursively convert a model_dump()/dict tree into JSON-safe wire values."""
    if isinstance(value, Decimal):
        return decimal_str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        # Transform keys too: a Decimal/Enum/date key would otherwise leak its repr.
        # Plain str keys are unchanged; Currency/Market StrEnum keys become their value.
        return {to_wire(k): to_wire(v) for k, v in value.items()}
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence):
        return [to_wire(v) for v in value]
    return value
