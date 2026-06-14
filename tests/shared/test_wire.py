"""Canonical wire encoder: every Decimal renders as ``format(d, "f")``.

The ONE wire form for a Decimal is fixed-point, full source precision (trailing zeros
preserved as stored), NEVER scientific notation. ``decimal_str`` is the single encoder;
``to_wire``'s Decimal branch routes through it, and it agrees byte-for-byte with
``money.to_db`` for every finite Decimal (data-and-pricing.md: full precision on the
wire, the frontend quantizes at display).
"""

from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.money import to_db
from portfolio_dash.shared.wire import decimal_str, to_wire


def test_decimal_str_tiny_rate_is_fixed_point_not_scientific() -> None:
    assert decimal_str(Decimal("1E-7")) == "0.0000001"


def test_decimal_str_integer_no_dot() -> None:
    assert decimal_str(Decimal("100")) == "100"


def test_decimal_str_preserves_trailing_zero() -> None:
    assert decimal_str(Decimal("0.10")) == "0.10"


def test_decimal_str_positive_exponent_expands() -> None:
    assert decimal_str(Decimal("1E+2")) == "100"


def test_decimal_str_negative_zero_keeps_sign_documented() -> None:
    # format(d, "f") preserves the sign of a negative zero ("-0.00"). Documented:
    # the encoder is a faithful fixed-point render of the stored Decimal, sign included.
    assert decimal_str(Decimal("-0.00")) == "-0.00"


def test_decimal_str_my_three_dp_price() -> None:
    assert decimal_str(Decimal("0.005")) == "0.005"


def test_decimal_str_high_precision_fx_preserved() -> None:
    assert decimal_str(Decimal("4.512345")) == "4.512345"


def test_decimal_str_matches_to_db_for_finite_decimals() -> None:
    samples = [
        Decimal("0"),
        Decimal("100"),
        Decimal("0.10"),
        Decimal("38.50"),
        Decimal("0.005"),
        Decimal("4.512345"),
        Decimal("1E-7"),
        Decimal("1E+2"),
        Decimal("-1234.50"),
        Decimal("639600"),
    ]
    for d in samples:
        assert decimal_str(d) == to_db(d)


def test_to_wire_decimal_branch_is_fixed_point_not_scientific() -> None:
    assert to_wire(Decimal("1E-7")) == "0.0000001"
    assert to_wire(Decimal("1E+2")) == "100"


def test_to_wire_decimal_branch_equals_decimal_str() -> None:
    for d in [Decimal("0.10"), Decimal("1E-7"), Decimal("1E+2"), Decimal("639600")]:
        assert to_wire(d) == decimal_str(d)


def test_to_wire_nested_decimals_use_canonical_form() -> None:
    tree = {"price": Decimal("1E-7"), "rows": [Decimal("0.10"), {"x": Decimal("1E+2")}]}
    assert to_wire(tree) == {"price": "0.0000001", "rows": ["0.10", {"x": "100"}]}


def test_to_wire_non_decimal_types_unchanged() -> None:
    # Datetime/date -> ISO, Enum -> value, str/int/None unchanged (no regression).
    assert to_wire(datetime(2026, 6, 11, 14, 30)) == "2026-06-11T14:30:00"
    assert to_wire(date(2026, 6, 11)) == "2026-06-11"
    assert to_wire(Currency.USD) == "USD"
    assert to_wire("already") == "already"
    assert to_wire(7) == 7
    assert to_wire(None) is None


def test_no_wire_decimal_is_scientific_notation() -> None:
    # Round-trip safety net: the canonical encoder never emits "E"/"e" for any scale.
    for exp in range(-10, 11):
        rendered = decimal_str(Decimal(1).scaleb(exp))
        assert "E" not in rendered and "e" not in rendered

    class _Side(Enum):
        BUY = "BUY"

    assert to_wire(_Side.BUY) == "BUY"
