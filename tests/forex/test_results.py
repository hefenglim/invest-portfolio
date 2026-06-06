from decimal import Decimal

from portfolio_dash.forex.results import AccountFXResult, FXSummary
from portfolio_dash.shared.enums import Currency


def test_account_fx_result_optional_fields() -> None:
    r = AccountFXResult(
        account_id="schwab",
        home_ccy=Currency.TWD,
        foreign_ccy=Currency.USD,
        avg_rate=None,
        current_spot=None,
        foreign_cash=Decimal("0"),
        foreign_stock_value=Decimal("0"),
        realized_fx=None,
        unrealized_fx_stocks=None,
        unrealized_fx_cash=None,
    )
    assert r.avg_rate is None
    assert r.realized_fx is None


def test_fx_summary_holds_accounts() -> None:
    r = AccountFXResult(
        account_id="schwab",
        home_ccy=Currency.TWD,
        foreign_ccy=Currency.USD,
        avg_rate=Decimal("32"),
        current_spot=Decimal("33"),
        foreign_cash=Decimal("1000"),
        foreign_stock_value=Decimal("10800"),
        realized_fx=Decimal("0"),
        unrealized_fx_stocks=Decimal("10800"),
        unrealized_fx_cash=Decimal("1000"),
    )
    s = FXSummary(
        by_account={"schwab": r},
        reporting_currency=Currency.TWD,
        reporting_realized_fx=Decimal("0"),
        reporting_unrealized_fx=Decimal("11800"),
    )
    assert s.by_account["schwab"].unrealized_fx_stocks == Decimal("10800")
    assert s.reporting_unrealized_fx == Decimal("11800")
