from decimal import Decimal

from portfolio_dash.portfolio.pnl import value_holdings
from portfolio_dash.portfolio.results import Holding
from portfolio_dash.shared.enums import Currency


def _holding(symbol: str, shares: str, orig: str, adj: str) -> Holding:
    return Holding(
        account_id="a", symbol=symbol, quote_ccy=Currency.USD, shares=Decimal(shares),
        original_avg=Decimal(orig), adjusted_avg=Decimal(adj),
        original_cost_total=Decimal(shares) * Decimal(orig),
        adjusted_cost_total=Decimal(shares) * Decimal(adj),
        dividend_portion=Decimal("0"), payback_ratio=Decimal("0"),
    )


def test_value_holdings_unrealized_and_capital_gain() -> None:
    h = _holding("AAPL", "10", "100", "90")
    [valued] = value_holdings([h], {"AAPL": Decimal("120")})
    assert valued.market_value == Decimal("1200")
    assert valued.unrealized_pnl == Decimal("300")   # (120-90)*10
    assert valued.capital_gain == Decimal("200")      # (120-100)*10
    assert valued.price_stale is False


def test_value_holdings_missing_price_marks_stale() -> None:
    h = _holding("AAPL", "10", "100", "90")
    [valued] = value_holdings([h], {})
    assert valued.market_price is None
    assert valued.market_value is None
    assert valued.unrealized_pnl is None
    assert valued.capital_gain is None
    assert valued.price_stale is True
