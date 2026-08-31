"""I-1 — the never-500 re-typing has ONE owner, and it is ``build_book`` itself.

A decimal fault on ledger data is an ``ArithmeticError``, **not** a ``ValueError``, so it
slips past the whole ``except (ValueError, KeyError)`` degradation family that
``UnbookableLedgerError`` was deliberately made a ``ValueError`` in order to reach. Until
2026-08-29 exactly one call site re-typed it — ``portfolio/dashboard.py`` — so the dashboard
degraded and the three STRICT doors (重算 / 稅務套件 / 試算) each answered **500** on the same
row (measured by wave 3: ``POST /api/actions/recompute``, ``POST /api/export/tax-package`` and
``POST /api/whatif`` all 500 with a ``quantity='1E+999999'`` row planted by direct SQL, while
``GET /api/dashboard`` answered 422).

Fixing it at four call sites is four chances to miss one — and it had already been missed
three times. The re-typing therefore lives in ``build_book``, where the fault is raised, so
all twelve call sites inherit it.

Two shapes are pinned here, because the replay has two places arithmetic happens:

* **inside the event loop** — ``ev.quantity * ev.price`` overflows (the wave-3 shape). The
  offending event is known, so the message names account / symbol / date;
* **after the event loop** — the holdings assembly divides ``original_total / shares``, which
  overflows for a position whose total is colossal and whose share count is tiny (a row with
  ``quantity=1E-999999`` and ``fees=9E+999999``, measured at ``cost_basis.py:704``). No single
  ledger row owns that fault, so the message must NOT name one.

Both shapes are reachable only by editing the database — every write door validates — which is
why the guard is about never going down, not about a workflow.
"""

from datetime import date
from decimal import Decimal, Overflow

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


def _tx(side: Side, qty: str, price: str, day: date, *, fees: str = "0") -> Transaction:
    return Transaction(account_id="tw_broker", symbol="2330", side=side,
                       quantity=Decimal(qty), price=Decimal(price), fees=Decimal(fees),
                       tax=_ZERO, trade_date=day)


def _bundle(*txs: Transaction) -> LedgerBundle:
    return LedgerBundle(transactions=list(txs), instruments=dict(_INSTRUMENTS))


#: Overflows at ``cost = ev.quantity * ev.price + ...``, INSIDE the event loop.
_LOOP_FAULT = _tx(Side.BUY, "1E+999999", "600", date(2026, 2, 1))
#: Books fine, then overflows at ``original_avg = original_total / shares``, AFTER the loop.
_TAIL_FAULT = _tx(Side.BUY, "1E-999999", "1", date(2026, 2, 1), fees="9E+999999")


# --- the class is re-typed, on BOTH paths ---------------------------------------------

@pytest.mark.parametrize("allow_oversell", [False, True])
def test_a_decimal_fault_in_the_event_loop_becomes_an_unbookable_ledger(
    allow_oversell: bool,
) -> None:
    """Both paths, one owner: the dashboard path is not a special case any more.

    ``allow_oversell`` decides whether a ledger STATE is tolerated (an acked 賣超). A decimal
    fault is not a state — it is a row that cannot be computed at all — so both paths get the
    same answer, and the callers differ only in what they do with it.
    """
    with pytest.raises(UnbookableLedgerError):
        build_book(_bundle(_LOOP_FAULT), allow_oversell=allow_oversell)


@pytest.mark.parametrize("allow_oversell", [False, True])
def test_the_raw_arithmetic_error_never_escapes(allow_oversell: bool) -> None:
    """The negative half — the one that actually produced the 500s.

    ``decimal.Overflow`` is a SIBLING of ``InvalidOperation``, not a subclass, so a caller
    catching the leaf (``strategy/whatif.py`` did) is bypassed by it.
    """
    try:
        build_book(_bundle(_LOOP_FAULT), allow_oversell=allow_oversell)
    except UnbookableLedgerError:
        pass
    except ArithmeticError:  # pragma: no cover - the regression this test exists to catch
        pytest.fail("a raw ArithmeticError escaped build_book: the never-500 rule is broken")


def test_the_refusal_lands_on_the_value_error_channel() -> None:
    """``UnbookableLedgerError`` is a ``ValueError`` on purpose: every existing
    ``except (ValueError, KeyError)`` degradation site inherits the fix for free."""
    with pytest.raises(UnbookableLedgerError) as exc:
        build_book(_bundle(_LOOP_FAULT))
    assert isinstance(exc.value, ValueError)


def test_the_original_fault_is_kept_as_the_cause() -> None:
    """``from exc``: the traceback still shows WHICH decimal operation failed, so a genuine
    engine bug is still debuggable — only the message the owner reads is replaced."""
    with pytest.raises(UnbookableLedgerError) as exc:
        build_book(_bundle(_LOOP_FAULT))
    assert isinstance(exc.value.__cause__, Overflow)


# --- what the message says -------------------------------------------------------------

def test_the_message_names_the_offending_row_and_speaks_zh() -> None:
    """Each strict seam renders ``str(exc)`` VERBATIM into its 4xx envelope, so the sentence
    is the whole of what the owner gets: it must name the row and be in their language."""
    with pytest.raises(UnbookableLedgerError) as exc:
        build_book(_bundle(_LOOP_FAULT))
    message = str(exc.value)
    assert "2330" in message and "tw_broker" in message and "2026-02-01" in message
    assert any("一" <= ch <= "鿿" for ch in message), message


@pytest.mark.parametrize("allow_oversell", [False, True])
def test_a_fault_after_the_event_loop_is_re_typed_too(allow_oversell: bool) -> None:
    """★ The second half of the wrap. Measured at ``cost_basis.py:704``
    (``original_avg = original_total / shares``): the event loop books this row without
    complaint and the holdings assembly overflows on it afterwards, so a guard around the
    loop alone still hands a raw ``decimal.Overflow`` to twelve call sites."""
    with pytest.raises(UnbookableLedgerError):
        build_book(_bundle(_TAIL_FAULT), allow_oversell=allow_oversell)


def test_a_fault_with_no_owning_row_does_not_invent_one() -> None:
    """The aggregate is what failed, not one ledger line. Naming a row the replay did not
    fail on sends the owner to edit the wrong thing — the 待釐清 vocabulary's whole point."""
    with pytest.raises(UnbookableLedgerError) as exc:
        build_book(_bundle(_TAIL_FAULT))
    message = str(exc.value)
    assert any("一" <= ch <= "鿿" for ch in message), message
    assert "2026-" not in message, f"a date was invented for an aggregate fault: {message}"


# --- control ----------------------------------------------------------------------------

def test_an_ordinary_ledger_is_completely_unaffected() -> None:
    """The wrap adds no branch to the happy path: buy 1,000 @500 with 712 fees, sell 300
    @600 → 300/1000 × 500,712 = 150,213.6 removed, realized 180,000 − 150,213.6 = 29,786.4."""
    book = build_book(_bundle(
        _tx(Side.BUY, "1000", "500", date(2026, 1, 5), fees="712"),
        _tx(Side.SELL, "300", "600", date(2026, 3, 1)),
    ))
    (row,) = book.realized.rows
    assert row.realized == Decimal("29786.4")
    (holding,) = book.holdings
    assert holding.shares == Decimal("700")
    assert holding.original_cost_total == Decimal("350498.4")
