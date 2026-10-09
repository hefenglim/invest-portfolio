"""A cash dividend paid AFTER the position closed is realized income (audit H2, 2026-07-26).

TW and MY pay weeks after the ex-date, so being entitled on the ex-date and flat by the
payment date is ordinary. Before this fix the payout was subtracted from a zero-share
position's ``adjusted_total`` and then discarded with that position by the holdings filter —
so 股利總覽 (which walks the dividend ledger) and the XIRR cashflow series both counted it,
while 總報酬 / 已實現 / 未實現 did not. One payout, three disagreeing answers.

The rule now: no cost basis left to reduce -> the net is a realized row (``kind="dividend"``),
booked exactly once. Everything about an in-position dividend is unchanged.
"""

from datetime import date
from decimal import Decimal

from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import Dividend, LedgerBundle, Transaction

_INSTRUMENTS = {
    "0056": Instrument(symbol="0056", market=Market.TW, quote_ccy=Currency.TWD,
                       name="TW ETF", sector="ETF"),
    "1155": Instrument(symbol="1155", market=Market.MY, quote_ccy=Currency.MYR,
                       name="MY Bank", sector="Financials"),
}


def _buy(symbol: str, qty: str, price: str, day: date) -> Transaction:
    return Transaction(account_id="a1", symbol=symbol, side=Side.BUY,
                       quantity=Decimal(qty), price=Decimal(price),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=day)


def _sell(symbol: str, qty: str, price: str, day: date) -> Transaction:
    return Transaction(account_id="a1", symbol=symbol, side=Side.SELL,
                       quantity=Decimal(qty), price=Decimal(price),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=day)


def _cash_div(symbol: str, net: str, day: date,
              dtype: DividendType = DividendType.CASH) -> Dividend:
    return Dividend(account_id="a1", symbol=symbol, date=day, type=dtype,
                    gross=Decimal(net), withholding=Decimal("0"), net=Decimal(net))


def test_dividend_after_full_sale_is_realized_income() -> None:
    """Sell everything, then receive the dividend: it must reach realized P&L."""
    book = build_book(LedgerBundle(
        [_buy("0056", "1000", "40", date(2026, 1, 10)),
         _sell("0056", "1000", "45", date(2026, 6, 1))],
        [_cash_div("0056", "3000", date(2026, 7, 10))],
        instruments=_INSTRUMENTS,
    ))
    assert not book.holdings  # position is closed

    sale = [r for r in book.realized.rows if r.kind == "sale"]
    div = [r for r in book.realized.rows if r.kind == "dividend"]
    assert len(sale) == 1 and len(div) == 1

    # the sale is untouched by the dividend (it settled first)
    assert sale[0].realized == Decimal("5000")     # 45,000 proceeds - 40,000 cost
    # the payout is booked at its net, with no shares and no cost removed
    assert div[0].realized == Decimal("3000")
    assert div[0].proceeds_net == Decimal("3000")
    assert div[0].shares_sold == Decimal("0")
    assert div[0].original_cost_removed == Decimal("0")
    assert div[0].adjusted_cost_removed == Decimal("0")
    assert div[0].sell_date == date(2026, 7, 10)   # the PAYMENT date

    # and it lands in the per-currency total exactly once
    assert book.realized.by_currency[Currency.TWD] == Decimal("8000")


def test_my_net_dividend_after_close_uses_the_same_path() -> None:
    """The MY single-tier NET type is cash-family too — same rule, different market."""
    book = build_book(LedgerBundle(
        [_buy("1155", "1000", "9", date(2026, 2, 1)),
         _sell("1155", "1000", "10", date(2026, 5, 5))],
        [_cash_div("1155", "250", date(2026, 6, 20), DividendType.NET)],
        instruments=_INSTRUMENTS,
    ))
    assert book.realized.by_currency[Currency.MYR] == Decimal("1250")  # 1,000 + 250
    assert [r.kind for r in book.realized.rows] == ["sale", "dividend"]


def test_dividend_while_still_held_is_unchanged() -> None:
    """The normal path must not move: an in-position dividend reduces adjusted cost only."""
    book = build_book(LedgerBundle(
        [_buy("0056", "1000", "40", date(2026, 1, 10))],
        [_cash_div("0056", "3000", date(2026, 7, 10))],
        instruments=_INSTRUMENTS,
    ))
    (holding,) = book.holdings
    assert holding.original_cost_total == Decimal("40000")
    assert holding.adjusted_cost_total == Decimal("37000")   # reduced by the dividend
    assert holding.dividend_portion == Decimal("3000")
    assert not book.realized.rows                            # NOT realized while held


def test_partial_sale_then_dividend_still_reduces_cost() -> None:
    """Only a FULLY closed position takes the realized path; a partial close does not."""
    book = build_book(LedgerBundle(
        [_buy("0056", "1000", "40", date(2026, 1, 10)),
         _sell("0056", "400", "45", date(2026, 6, 1))],
        [_cash_div("0056", "1800", date(2026, 7, 10))],
        instruments=_INSTRUMENTS,
    ))
    (holding,) = book.holdings
    assert holding.shares == Decimal("600")
    # 24,000 remaining cost - 1,800 dividend
    assert holding.adjusted_cost_total == Decimal("22200")
    assert [r.kind for r in book.realized.rows] == ["sale"]


def test_dividend_between_two_holding_periods_reduces_cost_of_the_rebuy() -> None:
    """Closed, then re-bought BEFORE the payment date: the position exists again, so the
    payout reduces the new cost basis (it is not income) — the ordering rule, not a
    special case for 'this symbol was once closed'."""
    book = build_book(LedgerBundle(
        [_buy("0056", "1000", "40", date(2026, 1, 10)),
         _sell("0056", "1000", "45", date(2026, 6, 1)),
         _buy("0056", "500", "44", date(2026, 6, 20))],
        [_cash_div("0056", "1500", date(2026, 7, 10))],
        instruments=_INSTRUMENTS,
    ))
    (holding,) = book.holdings
    assert holding.adjusted_cost_total == Decimal("20500")   # 22,000 - 1,500
    assert [r.kind for r in book.realized.rows] == ["sale"]  # no dividend row
