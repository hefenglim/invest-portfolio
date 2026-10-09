from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Account, Instrument


def test_account_construction() -> None:
    acc = Account(
        account_id="schwab",
        name="Charles Schwab",
        broker="Schwab",
        settlement_ccy=Currency.USD,
        funding_ccy=Currency.TWD,
        dividend_model="cash_cost_reduction",
    )
    assert acc.settlement_ccy is Currency.USD
    assert acc.funding_ccy is Currency.TWD
    assert acc.dividend_model == "cash_cost_reduction"


def test_instrument_construction() -> None:
    inst = Instrument(
        symbol="AAPL",
        market=Market.US,
        quote_ccy=Currency.USD,
        sector="Information Technology",
        name="Apple Inc.",
    )
    assert inst.market is Market.US
    assert inst.quote_ccy is Currency.USD
    # R6: the GICS industry column defaults to None (filled by the next wave's AI service).
    assert inst.industry is None


def test_instrument_industry_optional_free_text() -> None:
    inst = Instrument(
        symbol="NVDA",
        market=Market.US,
        quote_ccy=Currency.USD,
        sector="Information Technology",
        name="NVIDIA Corp.",
        industry="Semiconductors",
    )
    assert inst.industry == "Semiconductors"


def test_an_instrument_is_a_stock_unless_registered_as_an_option() -> None:
    """Options spec §3 / OPT-D22: ``asset_type`` is stored; the terms are parsed from the
    symbol, never stored a second time."""
    from datetime import date
    from decimal import Decimal

    from portfolio_dash.shared.enums import Currency, Market
    from portfolio_dash.shared.models.assets import Instrument

    stock = Instrument(symbol="XYZ", market=Market.US, quote_ccy=Currency.USD,
                       sector="", name="XYZ Corp")
    assert stock.asset_type == "stock" and not stock.is_option and stock.option is None

    put = Instrument(symbol="XYZ261218P00050000", market=Market.US, quote_ccy=Currency.USD,
                     sector="", name="XYZ 2026-12-18 50 Put", asset_type="option")
    assert put.is_option
    assert put.option is not None
    assert (put.option.underlying, put.option.expiry, put.option.strike, put.option.right) == (
        "XYZ", date(2026, 12, 18), Decimal("50"), "P")
    # the type decides, not the shape: an OCC-shaped row stored as a stock stays a stock
    legacy = put.model_copy(update={"asset_type": "stock"})
    assert not legacy.is_option and legacy.option is None
    # computed, never serialised: the instrument wire is built field by field
    assert "option" not in put.model_dump() and "is_option" not in put.model_dump()
