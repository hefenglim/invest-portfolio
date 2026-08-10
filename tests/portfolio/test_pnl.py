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


# --- Which 待釐清 flag suppresses valuation, and which must NOT (audit F-49) -------------
# These two tests are a PAIR and only mean anything together: the first says the corporate-
# action flag suppresses, the second says the dividend flag does not. Either one alone can be
# satisfied by "treat all 待釐清 flags the same", which is exactly the mistake that produced
# F-49 (unbookable_action was wired to mirror unbookable_dividend, so it suppressed nothing).


def test_unbookable_action_suppresses_valuation_because_the_shares_are_wrong() -> None:
    """A skipped corporate action leaves `shares` in PRE-action terms against a POST-action
    price, so `price × shares` is wrong by the action's whole ratio. Null the value fields so
    every aggregate that gates on `market_value is not None` drops the position — the same
    mechanism 賣超 uses. The PRICE is kept for display: it is not the untrustworthy part."""
    h = _holding("AAPL", "10", "100", "90").model_copy(
        update={"unbookable_action": True})
    [valued] = value_holdings([h], {"AAPL": Decimal("120")})
    assert valued.market_price == Decimal("120")
    assert valued.market_value is None
    assert valued.unrealized_pnl is None
    assert valued.capital_gain is None
    assert valued.price_stale is False


def test_unbookable_dividend_does_NOT_suppress_valuation_because_the_shares_are_right() -> (
        None):
    """The mirror image, pinned so a later "tidy-up" cannot lump the two flags together.

    A dividend that landed on an open short was not booked, so `adjusted_cost_total` is short
    one payout — but the SHARE COUNT is correct, so `price × shares` is genuinely correct too.
    Suppressing it would hide a real, correctly-valued position from every aggregate over a
    cost-side gap. This flag takes the display mechanism ONLY."""
    h = _holding("AAPL", "10", "100", "90").model_copy(
        update={"unbookable_dividend": True})
    [valued] = value_holdings([h], {"AAPL": Decimal("120")})
    assert valued.market_value == Decimal("1200")
    assert valued.unrealized_pnl == Decimal("300")
    assert valued.capital_gain == Decimal("200")
