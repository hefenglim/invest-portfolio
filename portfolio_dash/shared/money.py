"""Decimal money primitives: TEXT persistence and per-currency quantization.

Money is never ``float``. Decimals are stored at full source precision as canonical
fixed-point strings and quantized to a currency's minor unit only at settlement/display.
"""

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

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


def cap_dp(value: Decimal, places: int) -> Decimal:
    """Round *value* to at most *places* decimals — **CAP, never pad**.

    The distinction from :func:`quantize_amount` is the whole point and it is a rule of
    ``data-and-pricing.md``, not a preference: a value already within the cap is returned
    **unchanged**, so it persists byte-identically. ``quantize`` would pad it — turning a
    stored ``60`` into ``60.0000`` — and stored TEXT is what D38's reversibility invariant
    and every golden-payload assertion compare. Equal Decimals with different exponents are
    ``==`` to Python and different to a byte comparison, which is exactly the class of
    change that passes its tests and fails its diff.

    One home for three callers that had independently written it: ``pricing/store``'s
    4 dp/6 dp float-noise caps, ``data_ingestion/store``'s transaction-price cap, and
    ``shared/corporate_actions.apply_ratio_to_price``.
    """
    exp = value.as_tuple().exponent
    if isinstance(exp, int) and exp < -places:
        return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return value


#: How a trade's price x quantity settles in each currency (owner 2026-09-30: 「量化方式由開發者
#: 依券商實務決定」). TWD: TWSE computes an odd-lot 交割價金 per order and price with 元以下捨去
#: (the 2024-04-01 rule; a board lot of 1,000 shares is always a whole NT$ amount anyway). USD /
#: MYR: the cent / sen, half up — the broker's confirmation states the principal in cents.
_SETTLEMENT_ROUNDING: dict[Currency, str] = {
    Currency.TWD: ROUND_DOWN,
    Currency.USD: ROUND_HALF_UP,
    Currency.MYR: ROUND_HALF_UP,
}


def settled_notional(quantity: Decimal, price: Decimal, currency: Currency) -> Decimal:
    """What a trade's shares x price moves, in *currency*'s minor unit — the 價金 a broker
    settles, before fees and tax.

    Owner ruling 2026-09-30 (the verifier's R8 note on B-06): the ledger booked the raw
    product, so 0.5 AAPL at 191.23 took 95.615 USD out of the pool — a sub-cent amount no
    account can hold — and a TW odd lot took 角 the exchange never settles. Every figure OF
    RECORD that a trade moves (the cash pool, the cost basis, realized proceeds, the XIRR and
    net-invested flows, the fee base, and every preview that mirrors them) takes the notional
    from here, so they cannot disagree. A VALUATION (price x shares held) is not a settlement
    and keeps full precision. The stored quantity and price are unchanged; this is computed
    on read, like every other figure (重算).

    A product already in the minor unit is returned AS IS, never re-quantized: quantizing
    rewrites ``45200.0`` as ``45200`` — equal, but a different TEXT in every figure built on
    it (the same short-circuit the split-basis seams take past the identity factor,
    data-and-pricing.md). Only a trade with a sub-unit remainder moves.
    """
    raw = quantity * price
    settled = raw.quantize(Decimal(1).scaleb(-MINOR_UNITS[currency]),
                           rounding=_SETTLEMENT_ROUNDING[currency])
    return raw if settled == raw else settled


def usd_display(value: Decimal) -> str:
    """「$-0.01」 — a USD amount the way every page prints one: ``$`` + 2 dp, half-up.

    The exact twin of the frontend's ``'$' + fmt.num(v, 2)`` (``web/format.js``: digit-string
    half-up, thousands separators, a rounded-away negative reads as zero), for the few
    sentences the SERVER writes with an amount in them — the R6 gate message, the budget
    refusal, the pipeline exec node. They printed the raw Decimal until DEF-085
    (「剩餘 $-0.01000」 beside the node's 「餘 $-0.01」 for the same balance). Display only;
    every comparison keeps full precision.
    """
    q = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if q == 0:
        q = abs(q)  # "-0.00" is noise, not information
    return f"${q:,.2f}"


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
