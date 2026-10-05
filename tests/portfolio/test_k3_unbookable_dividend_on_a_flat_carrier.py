"""K3 (owner ruling 2026-10-05): a refused dividend's 待釐清 flag must not die with a FLAT carrier.

The replay refuses a dividend on a position it HAD opened in two shapes — the position was an
open declared SHORT when it landed (a short pays the dividend in lieu, and this ledger has no
debit row for that), or an EXCHANGE had moved the ticker away (E24 / D32). On the dashboard path
both set ``unbookable_dividend`` on a position: the short itself, or the live successor
``_follow_exchange_chain`` walks to. That position can be FLAT — the successor sold out, the
short covered back to exactly zero — and ``build_book`` drops every flat position from
``holdings``, flag and all: the CR-11 class (``Book.oversold_positions``), one flag over.

The refusal is now recorded on the BOOK (``Book.unbookable_dividends``): the row, why it was
refused, and which position carries the flag. These tests drive the replay itself; the readers
(the trend, the FX-complete reason, the drawer) are driven through their doors in
``tests/contract/test_k3_unbookable_dividend_reaches_every_reader.py``.
"""

from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.portfolio.cost_basis import UnbookableLedgerError, build_book
from portfolio_dash.portfolio.results import Book
from portfolio_dash.shared.corporate_actions import CorporateAction, CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import Dividend, LedgerBundle, Transaction

D = Decimal
_ACCT = "schwab"
_SYMS = ("OLD", "NEW", "NEWER", "TSLA")
_INSTR = {s: Instrument(symbol=s, market=Market.US, quote_ccy=Currency.USD, sector="Tech",
                        name=s) for s in _SYMS}


def _tx(symbol: str, side: Side, qty: str, day: date, *, short: bool = False) -> Transaction:
    return Transaction(account_id=_ACCT, symbol=symbol, side=side, quantity=D(qty),
                       price=D("50"), fees=D("0"), tax=D("0"), trade_date=day,
                       short_sale=short)


def _exchange(src: str, dst: str, day: date, action_id: int) -> CorporateAction:
    return CorporateAction(id=action_id, account_id=_ACCT, date=day,
                           kind=CorporateActionKind.EXCHANGE, from_symbol=src, to_symbol=dst,
                           ratio_to=D("1"), ratio_from=D("1"))


def _div(symbol: str, day: date, kind: DividendType = DividendType.NET) -> Dividend:
    if kind in (DividendType.DRIP, DividendType.STOCK):
        return Dividend(account_id=_ACCT, symbol=symbol, date=day, type=kind, gross=D("0"),
                        withholding=D("0"), net=D("0"), reinvest_shares=D("2"),
                        reinvest_price=D("35") if kind is DividendType.DRIP else None)
    return Dividend(account_id=_ACCT, symbol=symbol, date=day, type=kind, gross=D("70"),
                    withholding=D("0"), net=D("70"))


def _vacated_then_sold(*, chain: bool = False,
                       kind: DividendType = DividendType.NET) -> LedgerBundle:
    """OLD → NEW (→ NEWER), the last one SOLD OUT, then a payout on OLD — refused (E24)."""
    actions = [_exchange("OLD", "NEW", date(2026, 2, 1), 1)]
    last = "NEW"
    if chain:
        actions.append(_exchange("NEW", "NEWER", date(2026, 2, 15), 2))
        last = "NEWER"
    return LedgerBundle(
        [_tx("OLD", Side.BUY, "100", date(2026, 1, 10)),
         _tx(last, Side.SELL, "100", date(2026, 3, 1))],
        dividends=[_div("OLD", date(2026, 4, 1), kind)],
        instruments=_INSTR, actions=actions,
    )


def _short_covered_to_flat() -> LedgerBundle:
    """A declared short of 100, a payout while it is open (refused), then a cover of 100."""
    return LedgerBundle(
        [_tx("TSLA", Side.SELL, "100", date(2026, 1, 10), short=True),
         _tx("TSLA", Side.BUY, "100", date(2026, 3, 1))],
        dividends=[_div("TSLA", date(2026, 2, 1))],
        instruments=_INSTR,
    )


def _book(bundle: LedgerBundle) -> Book:
    return build_book(bundle, allow_oversell=True)


def test_a_vacated_dividend_on_a_flat_successor_is_recorded_on_the_book() -> None:
    book = _book(_vacated_then_sold())
    assert book.holdings == []           # both positions are flat — no Holding carries a flag
    (rec,) = book.unbookable_dividends
    assert (rec.dividend.symbol, rec.cause, rec.carrier_symbol, rec.vacated_to) == (
        "OLD", "vacated", "NEW", "NEW")
    assert "換股為 NEW" in rec.reason and "改以現金收支登錄" in rec.reason
    # One refusal channel for XIRR / 股利總覽 still sees it; it is not an orphan.
    assert book.refused_dividends == [rec.dividend]
    assert book.orphan_dividends == []


def test_the_chain_is_followed_to_its_flat_end() -> None:
    """OLD → NEW → NEWER, NEWER sold out: the flag lands on NEWER, which is then dropped."""
    (rec,) = _book(_vacated_then_sold(chain=True)).unbookable_dividends
    assert (rec.carrier_symbol, rec.vacated_to) == ("NEWER", "NEW")


def test_a_short_covered_back_to_flat_keeps_its_refused_dividend() -> None:
    """The open-short refusal had the same hole: the cover nets the position to 0 and the
    holdings loop dropped it, together with its ``unbookable_dividend``."""
    book = _book(_short_covered_to_flat())
    assert book.holdings == []
    (rec,) = book.unbookable_dividends
    assert (rec.dividend.symbol, rec.cause, rec.carrier_symbol, rec.vacated_to) == (
        "TSLA", "short", "TSLA", None)
    assert "放空部位" in rec.reason


def test_a_held_carrier_is_listed_as_well() -> None:
    """The list is the WHOLE answer, held or flat — a reader never merges two sources."""
    bundle = LedgerBundle(
        [_tx("OLD", Side.BUY, "100", date(2026, 1, 10))],
        dividends=[_div("OLD", date(2026, 4, 1))],
        instruments=_INSTR, actions=[_exchange("OLD", "NEW", date(2026, 2, 1), 1)],
    )
    book = _book(bundle)
    (held,) = book.holdings
    assert held.symbol == "NEW" and held.unbookable_dividend is True
    assert [r.carrier_symbol for r in book.unbookable_dividends] == ["NEW"]


@pytest.mark.parametrize("make", [_vacated_then_sold, _short_covered_to_flat],
                         ids=["vacated", "short"])
def test_the_strict_path_raises_the_recorded_sentence(
    make: Callable[[], LedgerBundle],
) -> None:
    """One refusal, one sentence: the dashboard path records exactly what 重算 raises."""
    bundle = make()
    (rec,) = _book(bundle).unbookable_dividends
    with pytest.raises(UnbookableLedgerError) as exc:
        build_book(bundle, allow_oversell=False)
    assert str(exc.value) == rec.reason


def test_a_reinvest_dividend_on_a_vacated_ticker_is_recorded_without_a_ghost() -> None:
    book = _book(_vacated_then_sold(kind=DividendType.STOCK))
    assert book.holdings == []           # no $0-cost resurrection of OLD
    (rec,) = book.unbookable_dividends
    assert rec.dividend.type is DividendType.STOCK and rec.cause == "vacated"


def test_a_clean_ledger_records_nothing() -> None:
    bundle = LedgerBundle([_tx("OLD", Side.BUY, "100", date(2026, 1, 10))],
                          dividends=[_div("OLD", date(2026, 4, 1))], instruments=_INSTR)
    assert _book(bundle).unbookable_dividends == []
