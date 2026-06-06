from decimal import Decimal

from portfolio_dash.portfolio.allocation import combined_view, sector_allocation
from portfolio_dash.portfolio.results import Holding
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument

INSTR = {
    "AAPL": Instrument(
        symbol="AAPL", market=Market.US, quote_ccy=Currency.USD, sector="Tech", name="Apple"
    ),
    "JPM": Instrument(
        symbol="JPM", market=Market.US, quote_ccy=Currency.USD, sector="Financials", name="JPMorgan"
    ),
}


def _spot(frm: Currency, to: Currency) -> Decimal:
    return Decimal("1") if frm is to else Decimal("32")


def _valued(symbol: str, ccy: Currency, value: str) -> Holding:
    v = Decimal(value)
    return Holding(
        account_id="a", symbol=symbol, quote_ccy=ccy, shares=Decimal("1"),
        original_avg=v, adjusted_avg=v, original_cost_total=v, adjusted_cost_total=v,
        dividend_portion=Decimal("0"), payback_ratio=Decimal("0"),
        market_price=v, market_value=v, unrealized_pnl=Decimal("0"),
        capital_gain=Decimal("0"), price_stale=False,
    )


def test_sector_allocation_weights() -> None:
    valued = [_valued("AAPL", Currency.USD, "300"), _valued("JPM", Currency.USD, "100")]
    sa = sector_allocation(valued, INSTR, _spot, Currency.USD)
    assert sa.by_sector["Tech"] == Decimal("300")
    assert sa.weights["Tech"] == Decimal("0.75")
    assert sa.weights["Financials"] == Decimal("0.25")


def test_combined_view_per_currency_and_reporting() -> None:
    valued = [_valued("AAPL", Currency.USD, "100")]
    cv = combined_view(valued, _spot, Currency.TWD)
    assert cv.by_currency_value[Currency.USD] == Decimal("100")
    assert cv.reporting_total_value == Decimal("3200")  # 100 * 32


def test_allocation_skips_stale_holdings() -> None:
    stale = _valued("AAPL", Currency.USD, "100").model_copy(
        update={"market_value": None, "price_stale": True}
    )
    sa = sector_allocation([stale], INSTR, _spot, Currency.USD)
    assert sa.by_sector == {}
