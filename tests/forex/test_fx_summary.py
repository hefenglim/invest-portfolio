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
                 settlement_ccy=Currency.USD, funding_ccy=Currency.TWD,
                 dividend_model="drip_us")
MOOMOO = Account(account_id="moomoo_my", name="Moomoo", broker="Moomoo",
                 settlement_ccy=Currency.USD, funding_ccy=Currency.MYR,
                 dividend_model="drip_us")
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
    assert r.unrealized_fx_total == Decimal("11800")  # server-computed combined (stocks + cash)
    assert r.realized_fx == Decimal("0")
    assert summary.reporting_unrealized_fx == Decimal("11800")  # home TWD == reporting TWD
    assert summary.reporting_realized_fx == Decimal("0")


def test_fx_summary_unrealized_total_multi_account() -> None:
    """F10: ``unrealized_fx_total`` is the SERVER-computed combined unrealized FX per
    account (stocks + cash) — a Decimal string on the wire so the frontend never re-sums
    the two components with JS floats. None whenever either component is None. Verified
    across two FX-exposed accounts (one with a cost basis, one with none)."""
    def spot(frm: Currency, to: Currency) -> Decimal:
        if frm is to:
            return Decimal("1")
        rates = {
            (Currency.USD, Currency.TWD): Decimal("33"),
            (Currency.USD, Currency.MYR): Decimal("4.4"),
            (Currency.MYR, Currency.TWD): Decimal("7"),
        }
        return rates[(frm, to)]

    accts = {"schwab": SCHWAB, "moomoo_my": MOOMOO}
    # schwab: TWD->USD @ 32 avg; buy spends 9,000 USD -> cash 1,000; stock value 10,800.
    convs = [FXConversion(account_id="schwab", date=date(2025, 1, 1), from_ccy=Currency.TWD,
                          from_amount=Decimal("320000"), to_ccy=Currency.USD,
                          to_amount=Decimal("10000"))]
    txs = [Transaction(account_id="schwab", symbol="AAPL", side=Side.BUY, quantity=Decimal("90"),
                       price=Decimal("100"), fees=Decimal("0"), tax=Decimal("0"),
                       trade_date=date(2025, 1, 2))]
    # moomoo_my: NO conversions -> no avg_rate -> unrealized components (and total) are None.
    foreign_exposure = {
        "schwab": (Currency.USD, Decimal("10800")),
        "moomoo_my": (Currency.USD, Decimal("5000")),
    }
    summary = compute_fx_summary(
        accts, INSTR, txs, [], convs, foreign_exposure, spot, Currency.TWD
    )
    sch = summary.by_account["schwab"]
    assert sch.unrealized_fx_stocks == Decimal("10800")   # 10800 * (33 - 32)
    assert sch.unrealized_fx_cash == Decimal("1000")      # 1000 * (33 - 32)
    assert sch.unrealized_fx_total == Decimal("11800")    # server sum, not client-side
    assert sch.unrealized_fx_total == sch.unrealized_fx_stocks + sch.unrealized_fx_cash
    moo = summary.by_account["moomoo_my"]
    assert moo.unrealized_fx_stocks is None
    assert moo.unrealized_fx_cash is None
    assert moo.unrealized_fx_total is None                # None when components are None


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
