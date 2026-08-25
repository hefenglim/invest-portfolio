"""R6 counter-evidence: a 配股 was worth −9% for a month, once a year (review ⑧).

The `dividends` ledger carries ONE date. For a TW stock dividend the two that matter are far
apart: the share price adjusts on the **ex-date**, and the shares arrive on the **payment
date**, typically about a month later. With only the payment date recorded, the replay held
the OLD share count against the ALREADY-ADJUSTED price for that whole month — a 10% 配股 read
as a ~9% loss, every year, on every TW holding that pays one.

`dividends.ex_date` is added NULLABLE and existing rows stay NULL: the ex-date of a dividend
booked years ago is not recoverable, and guessing it is exactly what this project forbids. A
row with no ex_date behaves **byte-identically to today** — the regression test below is the
load-bearing one, because this change touches money of record on every ledger that has a stock
dividend.

⚠ **Only STOCK moves.** The rule is about what you actually own on a given day:

* **STOCK (配股)** — an entitlement that attaches on the ex-date. You own the shares from then;
  the price says so too. Effective date = ``ex_date or date``.
* **DRIP** — the cash is paid on the payment date and the reinvestment is BOUGHT then, at the
  recorded reinvest price. Those shares genuinely do not exist before that. Unchanged.
* **CASH / NET** — the price drops on the ex-date and the cost reduces when the money arrives,
  so a transient dip does exist. It is left alone deliberately: unlike the stock case it is
  HONEST — the position really is worth less and you really have not been paid yet.
"""

from datetime import date
from decimal import Decimal

from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import Dividend, LedgerBundle, Transaction

D = Decimal
EX = date(2026, 7, 1)
PAY = date(2026, 7, 31)
MID = date(2026, 7, 15)          # inside the gap — where the phantom lived
BUY_DAY = date(2026, 1, 5)

_INSTRUMENTS = {
    "2330": Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                       sector="Semiconductors", name="TSMC", board="TWSE"),
}


def _bundle(dv: Dividend) -> LedgerBundle:
    return LedgerBundle(
        transactions=[Transaction(
            account_id="tw_broker", symbol="2330", side=Side.BUY, quantity=D("1000"),
            price=D("500"), fees=D("0"), tax=D("0"), trade_date=BUY_DAY,
        )],
        dividends=[dv], opening=[], actions=[], instruments=_INSTRUMENTS,
    )


def _stock_dividend(ex_date: date | None) -> Dividend:
    return Dividend(
        account_id="tw_broker", symbol="2330", date=PAY, ex_date=ex_date,
        type=DividendType.STOCK, gross=D("0"), withholding=D("0"), net=D("0"),
        reinvest_shares=D("100"), reinvest_price=None,
    )


def _shares_on(bundle: LedgerBundle, day: date) -> Decimal:
    book = build_book(bundle.through(day), allow_oversell=True)
    return next(h.shares for h in book.holdings if h.symbol == "2330")


# --- the defect ----------------------------------------------------------------------


def test_shares_arrive_on_the_ex_date_when_it_is_known() -> None:
    """Mid-gap the price has already adjusted, so the share count must have too."""
    b = _bundle(_stock_dividend(EX))
    assert _shares_on(b, EX - __import__("datetime").timedelta(days=1)) == D("1000")
    assert _shares_on(b, EX) == D("1100")
    assert _shares_on(b, MID) == D("1100"), "the month-long phantom"
    assert _shares_on(b, PAY) == D("1100")


def test_without_an_ex_date_the_behaviour_is_exactly_what_it_was() -> None:
    """THE regression pin. Every existing row is NULL, and none of them may move."""
    b = _bundle(_stock_dividend(None))
    assert _shares_on(b, EX) == D("1000")
    assert _shares_on(b, MID) == D("1000")
    assert _shares_on(b, PAY) == D("1100")


def test_the_final_position_is_identical_either_way() -> None:
    """The ex-date changes WHEN, never HOW MUCH. Cost basis and share count both end equal."""
    with_ex = build_book(_bundle(_stock_dividend(EX)), allow_oversell=True)
    without = build_book(_bundle(_stock_dividend(None)), allow_oversell=True)
    a = next(h for h in with_ex.holdings if h.symbol == "2330")
    z = next(h for h in without.holdings if h.symbol == "2330")
    assert (a.shares, a.original_cost_total, a.adjusted_cost_total) == (
        z.shares, z.original_cost_total, z.adjusted_cost_total)


# --- only STOCK moves ----------------------------------------------------------------


def test_a_drip_does_not_move_to_the_ex_date() -> None:
    """The reinvestment is a PURCHASE made when the cash lands; those shares do not exist
    before the payment date even though the ex-date has passed."""
    dv = Dividend(
        account_id="tw_broker", symbol="2330", date=PAY, ex_date=EX,
        type=DividendType.DRIP, gross=D("1000"), withholding=D("300"), net=D("700"),
        reinvest_shares=D("100"), reinvest_price=D("7"),
    )
    assert _shares_on(_bundle(dv), MID) == D("1000")
    assert _shares_on(_bundle(dv), PAY) == D("1100")


def test_a_cash_dividend_reduces_cost_on_the_payment_date_not_the_ex_date() -> None:
    """Deliberate: the price drop before the money arrives is an HONEST dip — the position
    really is worth less and really has not paid yet. Unlike the stock case, nothing about
    what you OWN changed on the ex-date."""
    dv = Dividend(
        account_id="tw_broker", symbol="2330", date=PAY, ex_date=EX,
        type=DividendType.CASH, gross=D("5000"), withholding=D("0"), net=D("5000"),
    )
    b = _bundle(dv)
    mid = build_book(b.through(MID), allow_oversell=True)
    assert next(h for h in mid.holdings if h.symbol == "2330").adjusted_cost_total == D("500000")
    end = build_book(b.through(PAY), allow_oversell=True)
    assert next(h for h in end.holdings if h.symbol == "2330").adjusted_cost_total == D("495000")
