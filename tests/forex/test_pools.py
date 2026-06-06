from datetime import date
from decimal import Decimal

from portfolio_dash.forex.pools import average_acquisition_rate, foreign_cash_balance
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import Dividend, FXConversion, Transaction

AAPL = Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                  sector="Tech", name="Apple")
INSTR = {"AAPL": AAPL}


def _conv(frm: Currency, famt: str, to: Currency, tamt: str, d: date) -> FXConversion:
    return FXConversion(account_id="schwab", date=d, from_ccy=frm, from_amount=Decimal(famt),
                        to_ccy=to, to_amount=Decimal(tamt))


def test_average_acquisition_rate_weighted() -> None:
    convs = [_conv(Currency.TWD, "320000", Currency.USD, "10000", date(2025, 1, 1)),
             _conv(Currency.TWD, "330000", Currency.USD, "10000", date(2025, 2, 1))]
    assert average_acquisition_rate(convs, Currency.TWD, Currency.USD) == Decimal("32.5")


def test_average_acquisition_rate_none_when_no_conversions() -> None:
    assert average_acquisition_rate([], Currency.TWD, Currency.USD) is None


def test_foreign_cash_balance_reconstruction() -> None:
    convs = [_conv(Currency.TWD, "320000", Currency.USD, "10000", date(2025, 1, 1)),
             _conv(Currency.USD, "1000", Currency.TWD, "33000", date(2025, 6, 1))]
    txs = [
        Transaction(
            account_id="schwab", symbol="AAPL", side=Side.BUY, quantity=Decimal("90"),
            price=Decimal("100"), fees=Decimal("0"), tax=Decimal("0"),
            trade_date=date(2025, 1, 2),
        ),
        Transaction(
            account_id="schwab", symbol="AAPL", side=Side.SELL, quantity=Decimal("10"),
            price=Decimal("110"), fees=Decimal("0"), tax=Decimal("0"),
            trade_date=date(2025, 5, 1),
        ),
    ]
    divs = [Dividend(account_id="schwab", symbol="AAPL", date=date(2025, 3, 1),
                     type=DividendType.CASH, gross=Decimal("50"), withholding=Decimal("0"),
                     net=Decimal("50"))]
    # +10000 -9000 +50 +1100 -1000 = 1150
    assert foreign_cash_balance(txs, divs, convs, INSTR, Currency.USD) == Decimal("1150")


def test_foreign_cash_ignores_drip_dividends() -> None:
    convs = [_conv(Currency.TWD, "320000", Currency.USD, "10000", date(2025, 1, 1))]
    divs = [Dividend(
        account_id="schwab", symbol="AAPL", date=date(2025, 3, 1),
        type=DividendType.DRIP, gross=Decimal("100"), withholding=Decimal("30"),
        net=Decimal("70"), reinvest_shares=Decimal("0.5"), reinvest_price=Decimal("140"),
    )]
    assert foreign_cash_balance([], divs, convs, INSTR, Currency.USD) == Decimal("10000")
