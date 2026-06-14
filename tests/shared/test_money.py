from decimal import Decimal, InvalidOperation

import pytest

from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.money import MINOR_UNITS, from_db, quantize_amount, to_db
from portfolio_dash.shared.wire import decimal_str


def test_to_db_preserves_trailing_zeros() -> None:
    assert to_db(Decimal("38.50")) == "38.50"


def test_to_db_agrees_with_canonical_decimal_str() -> None:
    # to_db and the wire encoder are ONE canonical form (format(d, "f")).
    for d in [Decimal("38.50"), Decimal("0.005"), Decimal("1E+2"), Decimal("1E-7")]:
        assert to_db(d) == decimal_str(d)


def test_to_db_three_dp_my_price() -> None:
    assert to_db(Decimal("0.005")) == "0.005"


def test_to_db_no_scientific_notation() -> None:
    assert to_db(Decimal("1E+2")) == "100"


def test_to_db_rejects_float() -> None:
    with pytest.raises(TypeError):
        to_db(38.50)  # type: ignore[arg-type]


def test_roundtrip_high_precision_fx() -> None:
    rate = Decimal("4.512345")
    assert from_db(to_db(rate)) == rate


def test_from_db_invalid_raises() -> None:
    with pytest.raises(InvalidOperation):
        from_db("not-a-number")


def test_quantize_twd_zero_dp_half_up() -> None:
    assert quantize_amount(Decimal("1234.5"), Currency.TWD) == Decimal("1235")


def test_quantize_usd_two_dp_half_up() -> None:
    assert quantize_amount(Decimal("1.005"), Currency.USD) == Decimal("1.01")


def test_quantize_myr_two_dp() -> None:
    assert quantize_amount(Decimal("2.345"), Currency.MYR) == Decimal("2.35")


def test_minor_units_mapping() -> None:
    assert MINOR_UNITS == {Currency.TWD: 0, Currency.USD: 2, Currency.MYR: 2}


def test_to_db_rejects_non_finite() -> None:
    with pytest.raises(ValueError, match="non-finite"):
        to_db(Decimal("NaN"))
    with pytest.raises(ValueError, match="non-finite"):
        to_db(Decimal("Infinity"))


def test_quantize_rejects_non_finite() -> None:
    with pytest.raises(ValueError, match="non-finite"):
        quantize_amount(Decimal("NaN"), Currency.USD)


def test_quantize_unknown_currency_raises() -> None:
    with pytest.raises(ValueError, match="unknown currency"):
        quantize_amount(Decimal("1.00"), "XXX")  # type: ignore[arg-type]


def test_quantize_twd_negative_half_up() -> None:
    assert quantize_amount(Decimal("-1234.5"), Currency.TWD) == Decimal("-1235")
