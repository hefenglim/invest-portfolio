from datetime import date
from decimal import Decimal

from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.portfolio.returns import XirrOutcome, xirr_reporting
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import Dividend, Transaction

US = Instrument(
    symbol="AAPL", market=Market.US, quote_ccy=Currency.USD, sector="Tech", name="Apple"
)
INSTR = {"AAPL": US}


def _fx_one(_d: date, frm: Currency, to: Currency) -> Decimal:
    return Decimal("1")  # USD reporting, single currency


def _spot_one(frm: Currency, to: Currency) -> Decimal:
    return Decimal("1")


def test_xirr_simple_doubling_in_one_year() -> None:
    txs = [Transaction(account_id="a", symbol="AAPL", side=Side.BUY, quantity=Decimal("1"),
                       price=Decimal("100"), fees=Decimal("0"), tax=Decimal("0"),
                       trade_date=date(2024, 1, 1))]
    book = build_book(txs, [], [], INSTR)
    out = xirr_reporting(txs, [], [], book.holdings, INSTR, _fx_one,
                         {"AAPL": Decimal("110")}, _spot_one, date(2025, 1, 1), Currency.USD)
    assert isinstance(out, XirrOutcome)
    assert out.rate is not None
    assert Decimal("0.09") < out.rate < Decimal("0.11")
    # Observation window: 2024-01-01 buy → 2025-01-01 as_of = 366 days (2024 is a leap year).
    assert out.window_days == 366


def test_xirr_cash_dividend_counts_as_inflow() -> None:
    txs = [Transaction(account_id="a", symbol="AAPL", side=Side.BUY, quantity=Decimal("1"),
                       price=Decimal("100"), fees=Decimal("0"), tax=Decimal("0"),
                       trade_date=date(2024, 1, 1))]
    divs = [Dividend(account_id="a", symbol="AAPL", date=date(2024, 7, 1),
                     type=DividendType.CASH, gross=Decimal("5"), withholding=Decimal("0"),
                     net=Decimal("5"))]
    book = build_book(txs, divs, [], INSTR)
    out = xirr_reporting(txs, divs, [], book.holdings, INSTR, _fx_one,
                         {"AAPL": Decimal("100")}, _spot_one, date(2025, 1, 1), Currency.USD)
    assert out.rate is not None
    assert out.rate > Decimal("0")
    # Earliest flow is still the 2024-01-01 buy (before the mid-year dividend).
    assert out.window_days == 366


def test_xirr_missing_price_returns_none_but_reports_window() -> None:
    txs = [Transaction(account_id="a", symbol="AAPL", side=Side.BUY, quantity=Decimal("1"),
                       price=Decimal("100"), fees=Decimal("0"), tax=Decimal("0"),
                       trade_date=date(2024, 1, 1))]
    book = build_book(txs, [], [], INSTR)
    out = xirr_reporting(txs, [], [], book.holdings, INSTR, _fx_one,
                         {}, _spot_one, date(2025, 1, 1), Currency.USD)
    assert out.rate is None
    # The window is a property of the cashflow series — reported even without a terminal.
    assert out.window_days == 366


def test_xirr_fully_closed_portfolio_uses_buy_sell_flows() -> None:
    # Buy then sell everything: no holdings, but the buy/sell series still solves.
    txs = [Transaction(account_id="a", symbol="AAPL", side=Side.BUY, quantity=Decimal("1"),
                       price=Decimal("100"), fees=Decimal("0"), tax=Decimal("0"),
                       trade_date=date(2024, 1, 1)),
           Transaction(account_id="a", symbol="AAPL", side=Side.SELL, quantity=Decimal("1"),
                       price=Decimal("150"), fees=Decimal("0"), tax=Decimal("0"),
                       trade_date=date(2025, 1, 1))]
    book = build_book(txs, [], [], INSTR)
    assert book.holdings == []
    out = xirr_reporting(txs, [], [], book.holdings, INSTR, _fx_one,
                         {}, _spot_one, date(2025, 1, 1), Currency.USD)
    assert out.rate is not None
    assert out.rate > Decimal("0")  # bought 100, sold 150 in a year
    assert out.window_days == 366


def test_xirr_same_date_conflicting_flows_returns_none() -> None:
    # Buy and sell the same day -> pyxirr yields a non-finite rate; must not leak it.
    txs = [Transaction(account_id="a", symbol="AAPL", side=Side.BUY, quantity=Decimal("1"),
                       price=Decimal("100"), fees=Decimal("0"), tax=Decimal("0"),
                       trade_date=date(2024, 1, 1)),
           Transaction(account_id="a", symbol="AAPL", side=Side.SELL, quantity=Decimal("1"),
                       price=Decimal("150"), fees=Decimal("0"), tax=Decimal("0"),
                       trade_date=date(2024, 1, 1))]
    book = build_book(txs, [], [], INSTR)
    out = xirr_reporting(txs, [], [], book.holdings, INSTR, _fx_one,
                         {}, _spot_one, date(2024, 1, 1), Currency.USD)
    assert out.rate is None
    # Same-day flows: earliest == as_of, so a zero-day window (still not None).
    assert out.window_days == 0


def test_xirr_all_outflows_returns_none() -> None:
    # Only a buy, no sell/holdings/dividends -> no sign change -> not computable.
    txs = [Transaction(account_id="a", symbol="AAPL", side=Side.BUY, quantity=Decimal("1"),
                       price=Decimal("100"), fees=Decimal("0"), tax=Decimal("0"),
                       trade_date=date(2024, 1, 1))]
    out = xirr_reporting(txs, [], [], [], INSTR, _fx_one,
                         {}, _spot_one, date(2025, 1, 1), Currency.USD)
    assert out.rate is None
    assert out.window_days == 366


def test_xirr_no_flows_window_is_none() -> None:
    # Nothing at all -> no rate AND no window (window is undefined without any flow).
    out = xirr_reporting([], [], [], [], INSTR, _fx_one,
                         {}, _spot_one, date(2025, 1, 1), Currency.USD)
    assert out.rate is None
    assert out.window_days is None
