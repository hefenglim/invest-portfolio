from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Account, Instrument


def test_account_construction() -> None:
    acc = Account(
        account_id="schwab",
        name="Charles Schwab",
        broker="Schwab",
        settlement_ccy=Currency.USD,
        funding_ccy=Currency.TWD,
    )
    assert acc.settlement_ccy is Currency.USD
    assert acc.funding_ccy is Currency.TWD


def test_instrument_construction() -> None:
    inst = Instrument(
        symbol="AAPL",
        market=Market.US,
        quote_ccy=Currency.USD,
        sector="Technology",
        name="Apple Inc.",
    )
    assert inst.market is Market.US
    assert inst.quote_ccy is Currency.USD
