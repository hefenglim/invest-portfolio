"""R3 / QA-12 — a zero-quantity transaction must not crash the ledger replay.

``build_book`` divides by ``ev.quantity`` (the declared-short branch) and by
``pos.shares`` (the ordinary-sell branch). A stored row with ``quantity == 0`` reaches
both with a zero denominator and raises ``decimal.InvalidOperation`` — on the STRICT path
AND on the ``allow_oversell=True`` dashboard path, so the never-500 degradation the
dashboard relies on does not cover it (it catches ``KeyError``, and the replay's own
refusal channel raises ``OversellError`` / ``UnbookableLedgerError``).

The correct behaviour is not a new refusal channel: a zero-quantity row moves no shares
and no money, so booking it and skipping it are the SAME book. It is therefore skipped —
which is what makes this safe, and is also why the guard is ``== 0`` and not ``<= 0``: a
NEGATIVE quantity is a different input class whose meaning is unknown, and silently
ignoring one would turn a data error into a quietly wrong number instead of a loud one.
"""

from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.models.ledger import LedgerBundle, Transaction

_ZERO = Decimal("0")

_INSTRUMENTS = {
    "2330": Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                       sector="Semi", name="TSMC"),
}


def _tx(side: Side, qty: str, price: str, day: date, *, short: bool = False) -> Transaction:
    return Transaction(account_id="tw_broker", symbol="2330", side=side,
                       quantity=Decimal(qty), price=Decimal(price),
                       fees=_ZERO, tax=_ZERO, trade_date=day, short_sale=short)


def _bundle(*txs: Transaction) -> LedgerBundle:
    return LedgerBundle(transactions=list(txs), instruments=dict(_INSTRUMENTS))


# --- the crash, on every path that reaches a zero denominator -----------------------

@pytest.mark.parametrize("allow_oversell", [False, True])
def test_zero_quantity_sell_does_not_crash_the_replay(allow_oversell: bool) -> None:
    """Ordinary SELL, ``pos.shares == 0``: ``frac = 0 / 0`` (cost_basis.py:499).

    Parametrised over BOTH paths because the dashboard path is the one that must never
    500, and the strict path is the one an import runs.
    """
    bundle = _bundle(_tx(Side.SELL, "0", "600", date(2026, 1, 5)))
    book = build_book(bundle, allow_oversell=allow_oversell)
    assert book.holdings == []
    assert book.realized.rows == []


@pytest.mark.parametrize("allow_oversell", [False, True])
def test_zero_quantity_declared_short_sell_does_not_crash_the_replay(
    allow_oversell: bool,
) -> None:
    """Declared short SELL: ``per_share_net = (…) / ev.quantity`` (cost_basis.py:452)."""
    bundle = _bundle(_tx(Side.SELL, "0", "600", date(2026, 1, 5), short=True))
    book = build_book(bundle, allow_oversell=allow_oversell)
    assert book.holdings == []
    assert book.realized.rows == []


def test_zero_quantity_sell_against_a_live_position_does_not_crash() -> None:
    """The same row on a position that DOES hold shares — no zero-value realized row."""
    bundle = _bundle(
        _tx(Side.BUY, "1000", "500", date(2026, 1, 5)),
        _tx(Side.SELL, "0", "600", date(2026, 2, 1)),
    )
    book = build_book(bundle)
    assert [r.shares_sold for r in book.realized.rows] == []
    assert len(book.holdings) == 1 and book.holdings[0].shares == Decimal("1000")


def test_zero_quantity_buy_is_already_harmless_and_stays_so() -> None:
    """Control: the BUY branch never divided, so this one must be unchanged."""
    book = build_book(_bundle(_tx(Side.BUY, "0", "500", date(2026, 1, 5))))
    assert book.holdings == []
    assert book.gross_invested.get(Currency.TWD, _ZERO) == _ZERO


# --- the property that makes skipping safe -----------------------------------------

@pytest.mark.parametrize("side,short", [
    (Side.BUY, False), (Side.SELL, False), (Side.SELL, True),
])
def test_a_zero_quantity_row_is_a_no_op_on_a_real_ledger(side: Side, short: bool) -> None:
    """A zero-quantity row of ANY shape leaves the book byte-for-byte as it was.

    This is the whole justification for ``continue``: the row is not being discarded,
    it is being recognised as contributing nothing. Compared against the replay of the
    SAME ledger without it, so the assertion cannot drift with the fixture.
    """
    real = (
        _tx(Side.BUY, "1000", "500", date(2026, 1, 5)),
        _tx(Side.SELL, "300", "600", date(2026, 3, 1)),
    )
    without = build_book(_bundle(*real), allow_oversell=True)
    with_zero = build_book(
        _bundle(real[0], _tx(side, "0", "600", date(2026, 2, 1), short=short), real[1]),
        allow_oversell=True,
    )
    assert with_zero.model_dump() == without.model_dump()
