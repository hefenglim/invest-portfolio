from decimal import Decimal

import pytest

from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.fx import convert


def test_convert_full_precision_no_quantize() -> None:
    # 100 USD at 32.125 TWD per USD; no target currency -> full precision kept.
    assert convert(Decimal("100"), Decimal("32.125")) == Decimal("3212.5")


def test_convert_quantizes_to_target_currency() -> None:
    # to TWD -> 0 dp, ROUND_HALF_UP (3212.5 -> 3213).
    assert convert(Decimal("100"), Decimal("32.125"), to_currency=Currency.TWD) == Decimal("3213")


def test_convert_quantizes_usd_two_dp() -> None:
    assert convert(Decimal("10"), Decimal("0.03125"), to_currency=Currency.USD) == Decimal("0.31")


def test_convert_negative_amount_allowed() -> None:
    assert convert(Decimal("-100"), Decimal("32")) == Decimal("-3200")


def test_convert_rejects_zero_rate() -> None:
    with pytest.raises(ValueError):
        convert(Decimal("100"), Decimal("0"))


def test_convert_rejects_negative_rate() -> None:
    with pytest.raises(ValueError):
        convert(Decimal("100"), Decimal("-1"))
