from datetime import date
from decimal import Decimal

from portfolio_dash.forex.fx_pnl import compute_fx_summary
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.portfolio.pnl import value_holdings
from portfolio_dash.portfolio.returns import total_return
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Account, Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.models.ledger import FXConversion, Transaction

SCHWAB = Account(account_id="schwab", name="Schwab", broker="Schwab",
                 settlement_ccy=Currency.USD, funding_ccy=Currency.TWD)
AAPL = Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                  sector="Tech", name="Apple")
INSTR = {"AAPL": AAPL}
ACCTS = {"schwab": SCHWAB}


def _spot(frm: Currency, to: Currency) -> Decimal:
    if frm is to:
        return Decimal("1")
    rates = {(Currency.USD, Currency.TWD): Decimal("33")}
    return rates[(frm, to)]


def test_fx_summary_rollup_and_worked_example() -> None:
    convs = [FXConversion(account_id="schwab", date=date(2025, 1, 1), from_ccy=Currency.TWD,
                          from_amount=Decimal("320000"), to_ccy=Currency.USD,
                          to_amount=Decimal("10000"))]
    txs = [Transaction(account_id="schwab", symbol="AAPL", side=Side.BUY, quantity=Decimal("90"),
                       price=Decimal("100"), fees=Decimal("0"), tax=Decimal("0"),
                       trade_date=date(2025, 1, 2))]
    foreign_exposure = {"schwab": (Currency.USD, Decimal("10800"))}  # 90 sh @ 120
    summary = compute_fx_summary(
        ACCTS, INSTR, txs, [], convs, foreign_exposure, _spot, Currency.TWD
    )
    r = summary.by_account["schwab"]
    assert r.unrealized_fx_stocks == Decimal("10800")
    assert r.unrealized_fx_cash == Decimal("1000")
    assert r.realized_fx == Decimal("0")
    assert summary.reporting_unrealized_fx == Decimal("11800")  # home TWD == reporting TWD
    assert summary.reporting_realized_fx == Decimal("0")


def test_decomposition_identity_no_double_count() -> None:
    convs = [FXConversion(account_id="schwab", date=date(2025, 1, 1), from_ccy=Currency.TWD,
                          from_amount=Decimal("320000"), to_ccy=Currency.USD,
                          to_amount=Decimal("10000"))]
    txs = [Transaction(account_id="schwab", symbol="AAPL", side=Side.BUY, quantity=Decimal("90"),
                       price=Decimal("100"), fees=Decimal("0"), tax=Decimal("0"),
                       trade_date=date(2025, 1, 2))]
    book = build_book(txs, [], [], INSTR)
    valued = value_holdings(book.holdings, {"AAPL": Decimal("120")})
    rs = total_return(book, valued, _spot, Currency.TWD)
    one_total = rs.reporting_total_return

    foreign_exposure = {"schwab": (Currency.USD, Decimal("10800"))}
    summary = compute_fx_summary(
        ACCTS, INSTR, txs, [], convs, foreign_exposure, _spot, Currency.TWD
    )
    cash_unreal = summary.by_account["schwab"].unrealized_fx_cash or Decimal("0")
    stock_fx = summary.reporting_unrealized_fx - cash_unreal
    cash_fx = cash_unreal + summary.reporting_realized_fx

    # Independently hand-computed expected values (not re-derived from the summary):
    # ① unrealized 90*(120-100)=1800 USD -> *33 = 59400 TWD; stock_fx = 10800 USD*(33-32);
    # cash_fx = 1000 USD*(33-32). These pin the actual production outputs, not just algebra.
    assert one_total == Decimal("59400")
    assert stock_fx == Decimal("10800")
    assert cash_fx == Decimal("1000")

    asset = one_total - stock_fx
    total_fx = stock_fx + cash_fx
    grand_total = one_total + cash_fx
    assert asset == Decimal("48600")        # 59400 - 10800: stock local gain in TWD terms
    assert grand_total == Decimal("60400")  # ① (59400) + cash FX (1000)
    assert asset + total_fx == grand_total  # no double count: asset + FX == grand total
