from decimal import Decimal

from portfolio_dash.portfolio.results import Book, Holding, RealizedPnL
from portfolio_dash.portfolio.returns import total_return
from portfolio_dash.shared.enums import Currency


def _fx(frm: Currency, to: Currency) -> Decimal:
    if frm is to:
        return Decimal("1")
    rates = {(Currency.USD, Currency.TWD): Decimal("32")}
    return rates[(frm, to)]


def _valued(symbol: str, ccy: Currency, shares: str, adj: str, price: str) -> Holding:
    sh, a, p = Decimal(shares), Decimal(adj), Decimal(price)
    return Holding(
        account_id="a", symbol=symbol, quote_ccy=ccy, shares=sh,
        original_avg=a, adjusted_avg=a, original_cost_total=sh * a,
        adjusted_cost_total=sh * a, dividend_portion=Decimal("0"),
        payback_ratio=Decimal("0"), market_price=p, market_value=p * sh,
        unrealized_pnl=(p - a) * sh, capital_gain=(p - a) * sh, price_stale=False,
    )


def test_total_return_per_currency_and_blended() -> None:
    book = Book(
        holdings=[],
        realized=RealizedPnL(rows=[], by_currency={Currency.USD: Decimal("100")}),
        gross_invested={Currency.USD: Decimal("1000")},
    )
    valued = [_valued("AAPL", Currency.USD, "10", "100", "120")]  # unrealized 200
    rs = total_return(book, valued, _fx, Currency.TWD)
    usd = rs.by_currency[Currency.USD]
    assert usd.realized == Decimal("100")
    assert usd.unrealized == Decimal("200")
    assert usd.total_return == Decimal("300")
    assert usd.rate == Decimal("0.3")  # 300 / 1000
    assert rs.reporting_total_return == Decimal("9600")  # 300 USD * 32
    assert rs.reporting_currency is Currency.TWD


def test_total_return_zero_gross_rate_none() -> None:
    book = Book(holdings=[], realized=RealizedPnL(rows=[], by_currency={}),
               gross_invested={Currency.USD: Decimal("0")})
    rs = total_return(book, [], _fx, Currency.USD)
    assert rs.by_currency[Currency.USD].rate is None
