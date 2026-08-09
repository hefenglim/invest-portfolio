"""LedgerBundle — the one argument every replay call site takes.

The three helpers here used to be open-coded at the call sites: the per-day filter in
``timeseries.py`` and the unregistered-symbol skip in ``dashboard.py`` / the ledgers
router. Each is now in one place, so each needs its own test — including the boundary
(on-or-before, never strictly-before) that a trend point's value depends on.
"""

from datetime import date
from decimal import Decimal

from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import (
    Dividend,
    LedgerBundle,
    OpeningInventory,
    Transaction,
)

AAA = Instrument(symbol="AAA", market=Market.US, quote_ccy=Currency.USD,
                 sector="Tech", name="AAA Corp")
INSTR = {"AAA": AAA}
D = Decimal


def _tx(symbol: str, day: date) -> Transaction:
    return Transaction(account_id="schwab", symbol=symbol, side=Side.BUY,
                       quantity=D("10"), price=D("100"), fees=D("0"), tax=D("0"),
                       trade_date=day)


def _div(symbol: str, day: date) -> Dividend:
    return Dividend(account_id="schwab", symbol=symbol, date=day,
                    type=DividendType.CASH, gross=D("5"), withholding=D("0"), net=D("5"))


def _open(symbol: str, day: date) -> OpeningInventory:
    return OpeningInventory(account_id="schwab", symbol=symbol, shares=D("1"),
                            original_cost_total=D("100"), build_date=day)


def test_empty_bundle_is_valid_and_carries_nothing() -> None:
    b = LedgerBundle()
    assert (b.transactions, b.dividends, b.opening, b.instruments) == ([], [], [], {})


def test_through_is_inclusive_of_the_day_itself() -> None:
    """On-or-before, not strictly-before: a trade dated on the valuation day counts."""
    day = date(2026, 6, 2)
    b = LedgerBundle(
        [_tx("AAA", date(2026, 6, 1)), _tx("AAA", day), _tx("AAA", date(2026, 6, 3))],
        [_div("AAA", day), _div("AAA", date(2026, 6, 3))],
        [_open("AAA", day), _open("AAA", date(2026, 6, 3))],
        INSTR,
    )
    cut = b.through(day)
    assert [t.trade_date for t in cut.transactions] == [date(2026, 6, 1), day]
    assert [d.date for d in cut.dividends] == [day]
    assert [o.build_date for o in cut.opening] == [day]


def test_through_leaves_the_original_untouched_and_keeps_instruments() -> None:
    b = LedgerBundle([_tx("AAA", date(2026, 6, 3))], instruments=INSTR)
    cut = b.through(date(2026, 6, 1))
    assert cut.transactions == []
    assert len(b.transactions) == 1, "through() must not mutate the bundle it filters"
    assert cut.instruments is b.instruments


def test_unregistered_symbols_spans_every_ledger_and_is_sorted() -> None:
    b = LedgerBundle(
        [_tx("ZZZ", date(2026, 6, 1))],
        [_div("BBB", date(2026, 6, 1))],
        [_open("CCC", date(2026, 6, 1))],
        INSTR,
    )
    assert b.unregistered_symbols == ["BBB", "CCC", "ZZZ"]


def test_unregistered_symbols_is_empty_when_everything_is_registered() -> None:
    b = LedgerBundle([_tx("AAA", date(2026, 6, 1))], instruments=INSTR)
    assert b.unregistered_symbols == []


def test_without_unregistered_drops_those_rows_from_all_three_ledgers() -> None:
    b = LedgerBundle(
        [_tx("AAA", date(2026, 6, 1)), _tx("ZZZ", date(2026, 6, 1))],
        [_div("AAA", date(2026, 6, 1)), _div("ZZZ", date(2026, 6, 1))],
        [_open("AAA", date(2026, 6, 1)), _open("ZZZ", date(2026, 6, 1))],
        INSTR,
    )
    clean = b.without_unregistered()
    assert [t.symbol for t in clean.transactions] == ["AAA"]
    assert [d.symbol for d in clean.dividends] == ["AAA"]
    assert [o.symbol for o in clean.opening] == ["AAA"]
    assert clean.unregistered_symbols == []
    assert len(b.transactions) == 2, "without_unregistered() must not mutate its source"
