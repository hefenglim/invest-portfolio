from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import (
    Dividend,
    FXConversion,
    OpeningInventory,
    Transaction,
)


def test_transaction_construction() -> None:
    tx = Transaction(
        account_id="tw",
        symbol="2330.TW",
        side=Side.BUY,
        quantity=Decimal("1000"),
        price=Decimal("600"),
        fees=Decimal("85"),
        tax=Decimal("0"),
        trade_date=date(2025, 1, 2),
    )
    assert tx.side is Side.BUY
    assert tx.quantity == Decimal("1000")


def test_transaction_rejects_nan_price() -> None:
    with pytest.raises(ValidationError):
        Transaction(
            account_id="tw",
            symbol="2330.TW",
            side=Side.BUY,
            quantity=Decimal("1000"),
            price=Decimal("NaN"),
            fees=Decimal("0"),
            tax=Decimal("0"),
            trade_date=date(2025, 1, 2),
        )


def test_dividend_drip_optional_fields() -> None:
    dv = Dividend(
        account_id="schwab",
        symbol="AAPL",
        date=date(2025, 2, 1),
        type=DividendType.DRIP,
        gross=Decimal("100"),
        withholding=Decimal("30"),
        net=Decimal("70"),
        reinvest_shares=Decimal("0.5"),
        reinvest_price=Decimal("140"),
    )
    assert dv.type is DividendType.DRIP
    assert dv.reinvest_shares == Decimal("0.5")


def test_fx_conversion_construction() -> None:
    fx = FXConversion(
        account_id="schwab",
        date=date(2025, 1, 1),
        from_ccy=Currency.TWD,
        from_amount=Decimal("320000"),
        to_ccy=Currency.USD,
        to_amount=Decimal("10000"),
    )
    assert fx.from_ccy is Currency.TWD


def test_opening_inventory_construction() -> None:
    oi = OpeningInventory(
        account_id="tw",
        symbol="2330.TW",
        shares=Decimal("2000"),
        original_avg_cost=Decimal("500"),
        original_cost_total=Decimal("1000000"),
        build_date=date(2024, 12, 31),
    )
    assert oi.original_cost_total == Decimal("1000000")
