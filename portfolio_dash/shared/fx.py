"""The single FX-conversion helper. All currency conversion goes through here.

This is a pure function: the caller supplies the rate (rate selection by date/source is
a domain concern, not a shared concern). ``rate`` is expressed as: 1 unit of the source
currency = ``rate`` units of the target currency.
"""

from decimal import Decimal

from .enums import Currency
from .money import quantize_amount


def convert(
    amount: Decimal, rate: Decimal, *, to_currency: Currency | None = None
) -> Decimal:
    """Convert ``amount`` by ``rate``.

    Returns ``amount * rate`` at full precision. When ``to_currency`` is given, the
    result is quantized to that currency's minor unit (settlement). ``amount`` may be
    negative (cashflow signs); ``rate`` must be positive.
    """
    if rate <= 0:
        raise ValueError(f"FX rate must be positive, got {rate}")
    result = amount * rate
    if to_currency is not None:
        return quantize_amount(result, to_currency)
    return result
