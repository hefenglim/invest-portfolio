from decimal import Decimal

from portfolio_dash.portfolio.results import (
    Book,
    CombinedView,
    CurrencyReturn,
    Holding,
    RealizedPnL,
    RealizedRow,
    ReturnSummary,
    SectorAllocation,
)
from portfolio_dash.shared.enums import Currency


def test_holding_defaults_market_fields_none() -> None:
    h = Holding(
        account_id="tw",
        symbol="2330.TW",
        quote_ccy=Currency.TWD,
        shares=Decimal("1000"),
        original_avg=Decimal("600"),
        adjusted_avg=Decimal("580"),
        original_cost_total=Decimal("600000"),
        adjusted_cost_total=Decimal("580000"),
        dividend_portion=Decimal("20000"),
        payback_ratio=Decimal("0.0333"),
    )
    assert h.market_price is None
    assert h.price_stale is False


def test_book_holds_components() -> None:
    book = Book(
        holdings=[],
        realized=RealizedPnL(rows=[], by_currency={}),
        gross_invested={Currency.TWD: Decimal("0")},
    )
    assert book.gross_invested[Currency.TWD] == Decimal("0")


def test_return_summary_optional_xirr() -> None:
    rs = ReturnSummary(
        by_currency={
            Currency.USD: CurrencyReturn(
                realized=Decimal("0"),
                unrealized=Decimal("100"),
                total_return=Decimal("100"),
                gross_invested=Decimal("1000"),
                rate=Decimal("0.1"),
            )
        },
        reporting_currency=Currency.TWD,
        reporting_total_return=Decimal("3200"),
    )
    assert rs.xirr is None
    assert rs.by_currency[Currency.USD].rate == Decimal("0.1")


def test_realized_row_and_allocation_models() -> None:
    row = RealizedRow(
        account_id="tw",
        symbol="2330.TW",
        quote_ccy=Currency.TWD,
        shares_sold=Decimal("500"),
        proceeds_net=Decimal("310000"),
        adjusted_cost_removed=Decimal("290000"),
        realized=Decimal("20000"),
    )
    assert row.realized == Decimal("20000")
    sa = SectorAllocation(
        by_sector={"Tech": Decimal("100")},
        weights={"Tech": Decimal("1")},
        reporting_currency=Currency.TWD,
    )
    cv = CombinedView(
        by_currency_value={Currency.TWD: Decimal("100")},
        reporting_total_value=Decimal("100"),
        reporting_currency=Currency.TWD,
    )
    assert sa.weights["Tech"] == Decimal("1")
    assert cv.reporting_total_value == Decimal("100")
