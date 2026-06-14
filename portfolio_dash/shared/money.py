"""Decimal money primitives: TEXT persistence and per-currency quantization.

Money is never ``float``. Decimals are stored at full source precision as canonical
fixed-point strings and quantized to a currency's minor unit only at settlement/display.
"""

from decimal import ROUND_HALF_UP, Decimal

from .enums import Currency
from .wire import decimal_str

# Minor-unit decimal places per currency (settlement precision).
MINOR_UNITS: dict[Currency, int] = {
    Currency.TWD: 0,  # whole NT$
    Currency.USD: 2,  # cent
    Currency.MYR: 2,  # sen
}


def to_db(value: Decimal) -> str:
    """Serialize a Decimal to a canonical fixed-point TEXT string.

    Rejects ``float`` to enforce the no-float-money invariant, and rejects non-finite
    Decimals (NaN / Infinity) so a computation bug cannot silently enter the ledger.
    Preserves significant trailing zeros and never emits scientific notation, so the
    value round-trips losslessly via :func:`from_db`.
    """
    if isinstance(value, float):
        raise TypeError("money must be Decimal, not float")
    if not isinstance(value, Decimal):
        raise TypeError(f"expected Decimal, got {type(value).__name__}")
    if not value.is_finite():
        raise ValueError(f"cannot store non-finite Decimal: {value!r}")
    return decimal_str(value)  # ONE canonical fixed-point form, shared with the wire


def from_db(text: str) -> Decimal:
    """Parse a TEXT-stored Decimal. Raises on an invalid string (no silent coercion)."""
    return Decimal(text)


def quantize_amount(
    value: Decimal, currency: Currency, rounding: str = ROUND_HALF_UP
) -> Decimal:
    """Quantize an amount to ``currency``'s minor unit (settlement precision).

    TWD -> 0 dp, USD/MYR -> 2 dp, using ROUND_HALF_UP (四捨五入). Call only at
    settlement/display — prices and FX rates are stored at full precision. Rejects
    non-finite Decimals (NaN / Infinity) rather than letting them propagate silently.
    """
    if not value.is_finite():
        raise ValueError(f"cannot quantize non-finite Decimal: {value!r}")
    try:
        minor = MINOR_UNITS[currency]
    except KeyError as exc:
        raise ValueError(f"unknown currency: {currency!r}") from exc
    exponent = Decimal(1).scaleb(-minor)
    return value.quantize(exponent, rounding=rounding)
