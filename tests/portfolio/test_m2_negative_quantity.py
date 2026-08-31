"""QA-12 — a NEGATIVE-quantity transaction row, which the replay claimed "stays loud".

``build_book``'s zero-quantity skip is guarded by ``== 0`` and not ``<= 0``, and the comment
above it says why: 「A negative quantity is a different input class with no defined meaning on
either side, and quietly ignoring one would turn a loud data error into a plausible-looking
wrong number… It stays loud.」 It did not. Measured (``evidence/s8_negqty.py``):

* a negative **BUY** was **silently dropped** — ``cover = min(-100, 0) = -100`` is not ``> 0``
  so nothing is covered, then ``to_long = -100 - (-100) = 0`` is not ``> 0`` so nothing is
  added. No error, no flag, no effect;
* a negative **SELL** was **booked**, with ``frac = -100 / 1000 = -0.1``, which *increases*
  the position's cost (500,712 -> 550,783.2) and emits a realized row of **-9,928.8** —
  precisely the plausible-looking wrong number the comment refuses to accept;
* against a position holding nothing it was neither: ``frac = -100 / 0`` raised
  ``decimal.DivisionByZero``.

The row is not reachable through any product surface (manual/commit -> 400,
``PUT /api/ledgers/transactions/{id}`` -> 400, CSV import -> per-row error), so it is a
hand-edited database only.

★ **Both paths now REFUSE it** (I-3, 2026-08-29 — this file's second version). Wave 1 made the
strict path raise and the ``allow_oversell`` dashboard path SKIP, for one stated reason: at
that moment nothing caught an ``UnbookableLedgerError`` on the way out of ``GET /api/dashboard``
(measured: 500 through ``api/errors.py``'s catch-all), so raising would have taken down the one
page the owner lives on. Wave 3 registered the 422 handler for exactly this error class, so the
constraint is gone — and with it the reason to treat a corrupt row differently on the two paths.
Loud is right HERE, and skipping is right for the dividend-on-an-open-short next to it, because
the two inputs are not the same species: a dividend on a short is a LEGITIMATE ledger state the
owner may hold for months, while a negative quantity is DB corruption that no write door can
produce — silently continuing past it means the dashboard's numbers describe a ledger the owner
cannot see. ``== 0`` (booking and skipping give the same book) still stands on its own.
"""

from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.portfolio.cost_basis import UnbookableLedgerError, build_book
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.models.ledger import LedgerBundle, Transaction

_ZERO = Decimal("0")

_INSTRUMENTS = {
    "2330": Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                       sector="Semi", name="TSMC"),
}


def _tx(side: Side, qty: str, price: str, day: date, *, fees: str = "0",
        short: bool = False) -> Transaction:
    return Transaction(account_id="tw_broker", symbol="2330", side=side,
                       quantity=Decimal(qty), price=Decimal(price),
                       fees=Decimal(fees), tax=_ZERO, trade_date=day, short_sale=short)


def _bundle(*txs: Transaction) -> LedgerBundle:
    return LedgerBundle(transactions=list(txs), instruments=dict(_INSTRUMENTS))


_BUY = _tx(Side.BUY, "1000", "500", date(2026, 1, 5), fees="712")

#: BUY (was silent), SELL (was booked wrong), declared-short SELL (divides by the quantity).
_SHAPES = [(Side.BUY, False), (Side.SELL, False), (Side.SELL, True)]


# --- refused on BOTH paths, and it says which row -----------------------------------

@pytest.mark.parametrize("side,short", _SHAPES)
@pytest.mark.parametrize("allow_oversell", [False, True])
def test_a_negative_quantity_is_refused(
    side: Side, short: bool, allow_oversell: bool
) -> None:
    """Every shape × every path, because the BUY branch was the SILENT one — it never raised
    anything — and the dashboard path was the one that stayed silent longest."""
    bundle = _bundle(_BUY, _tx(side, "-100", "600", date(2026, 2, 1), short=short))
    with pytest.raises(UnbookableLedgerError):
        build_book(bundle, allow_oversell=allow_oversell)


@pytest.mark.parametrize("allow_oversell", [False, True])
def test_the_refusal_names_the_account_the_symbol_and_the_trade_date(
    allow_oversell: bool,
) -> None:
    """A refusal the owner cannot act on is a crash with better manners; and each seam
    (重算 / tax package / 試算 / the dashboard's 422 handler) renders ``str(exc)`` VERBATIM."""
    bundle = _bundle(_BUY, _tx(Side.SELL, "-100", "600", date(2026, 2, 1)))
    with pytest.raises(UnbookableLedgerError) as exc:
        build_book(bundle, allow_oversell=allow_oversell)
    message = str(exc.value)
    assert "2330" in message and "tw_broker" in message and "2026-02-01" in message
    assert "-100" in message
    assert any("一" <= ch <= "鿿" for ch in message), message


def test_a_negative_quantity_alone_is_refused_even_with_no_position() -> None:
    """The refusal precedes ``setdefault``, so no phantom position is created first."""
    with pytest.raises(UnbookableLedgerError):
        build_book(_bundle(_tx(Side.SELL, "-100", "600", date(2026, 2, 1))))


def test_nothing_is_booked_before_the_refusal() -> None:
    """★ ORACLE, hand-derived, for the row the QA evidence measured.

    Buy 1,000 @500 with 712 fees -> ``original_total = 1000*500 + 712 = 500,712``.
    Booking a -100 SELL literally gave ``frac = -0.1``, ``original_removed = -50,071.2`` and
    therefore ``original_total = 550,783.2``, with a realized row of
    ``-60,000 - (-50,071.2) = -9,928.8``.

    No book is returned at all now, on either path, so neither figure can be displayed —
    which is the point: the fabricated cost was WORSE than no answer, and a skipped row that
    the dashboard could not name was only marginally better.
    """
    bundle = _bundle(_BUY, _tx(Side.SELL, "-100", "600", date(2026, 2, 1)))
    with pytest.raises(UnbookableLedgerError):
        build_book(bundle, allow_oversell=True)


def test_the_two_paths_give_the_owner_the_same_sentence() -> None:
    """One row, one explanation. A dashboard that says something different from 重算 about
    the same row is the 「one app must not show three answers」 trap."""
    bundle = _bundle(_BUY, _tx(Side.SELL, "-100", "600", date(2026, 2, 1)))
    with pytest.raises(UnbookableLedgerError) as strict:
        build_book(bundle)
    with pytest.raises(UnbookableLedgerError) as dashboard:
        build_book(bundle, allow_oversell=True)
    assert str(strict.value) == str(dashboard.value)


# --- the neighbouring rows are untouched --------------------------------------------

def test_a_zero_quantity_row_is_still_skipped_not_refused() -> None:
    """The ``== 0`` branch below the guard keeps its own rule: a zero-quantity row moves no
    shares and no money, so booking it and skipping it produce the SAME book. Widening the
    refusal to ``<= 0`` would silence a corrupt row with a comment about a harmless one."""
    book = build_book(_bundle(_BUY, _tx(Side.SELL, "0", "600", date(2026, 2, 1))),
                      allow_oversell=True)
    (holding,) = book.holdings
    assert holding.shares == Decimal("1000")
    assert holding.original_cost_total == Decimal("500712")
    assert book.realized.rows == []


def test_a_positive_quantity_is_completely_unaffected() -> None:
    """Control: the guard is ``< 0``, so an ordinary sell books exactly as before.

    ORACLE: 300/1000 of 500,712 = 150,213.6 removed; proceeds 300*600 = 180,000;
    realized = 180,000 - 150,213.6 = 29,786.4.
    """
    book = build_book(_bundle(_BUY, _tx(Side.SELL, "300", "600", date(2026, 3, 1))))
    (row,) = book.realized.rows
    assert row.realized == Decimal("29786.4")
    (holding,) = book.holdings
    assert holding.shares == Decimal("700")
    assert holding.original_cost_total == Decimal("350498.4")
