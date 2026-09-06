"""M1-03 / D21 — a SPINOFF child's 回本進度 names where its dividends came from.

D21 (owner-approved 2026-08-09, grill Q9) ruled the ARITHMETIC correct and the LABEL a lie:
§4.3 scales both totals by the same ``c``, so ``(c·orig − c·adj) / (c·orig)`` cancels ``c``
and a child that has never paid a dividend renders its parent's payback ratio — measured on
the QA fixture as KEMG ``0.1247608749896032604175330616``, byte-identical to KEMB, with zero
dividend rows on KEMG. The number stays; the position now carries its PROVENANCE:

* ``payback_from_symbol`` — the DIRECT parent whose SPINOFF carved this basis in;
* ``payback_carried_dividends`` — the dividend portion carved in AT THE SPINOFF, i.e.
  ``carved_original − carved_adjusted`` = the parent's ``dividend_portion × cost_carry``
  on the action date (KEMB: ``1500 × 0.18 = 270.00``). A HISTORICAL amount: it is money
  that moved once, so a later partial sell does not rescale it (the ratio the label is
  about does not move either);
* ``payback_own_dividends`` — cash dividends this position received itself, so the label
  can say 「自身配息 0.00」 rather than leave the reader to infer it.

Every position that no SPINOFF ever fed reports ``None`` for all three — the 反證 half of
this file — so nothing on an ordinary holding changes.
"""

from collections.abc import Sequence
from datetime import date, timedelta

from portfolio_dash.shared.corporate_actions import CorporateAction, CorporateActionKind
from portfolio_dash.shared.models.enums import DividendType
from portfolio_dash.shared.models.ledger import Dividend, LedgerBundle, Transaction
from tests.portfolio.test_corporate_actions import (
    ACTION_DAY,
    INSTR,
    LATER,
    D,
    _act,
    _book,
    _buy,
    _held,
    _sell,
)

_AFTER = LATER + timedelta(days=30)


def _cash(sym: str, net: str, day: date) -> Dividend:
    return Dividend(account_id="schwab", symbol=sym, date=day, type=DividendType.CASH,
                    gross=D(net), withholding=D("0"), net=D(net))


def _spinoff_bundle(*, extra_txs: Sequence[Transaction] = (),
                    extra_divs: Sequence[Dividend] = (),
                    extra_actions: Sequence[CorporateAction] = ()) -> LedgerBundle:
    """AAA: 100 @ 100 (orig 10,000), cash dividend 4,000 → SPINOFF 30% into BBB, 1-for-2.

    The child therefore starts with orig 3,000.00 / adj 1,800.00, so its dividend portion
    is 1,200.00 — none of which it ever received. Same figures as the D21 pin in
    ``test_corporate_actions.py``.
    """
    return LedgerBundle(
        [_buy("AAA", "100", "100"), *extra_txs],
        [_cash("AAA", "4000", date(2026, 3, 1)), *extra_divs],
        [], INSTR,
        actions=[_act(CorporateActionKind.SPINOFF, to="1", frm="2",
                      to_symbol="BBB", carry="0.30"), *extra_actions],
    )


# ------------------------------------------------------------------ the label's content


def test_child_names_its_parent_and_the_dividends_carved_in() -> None:
    book = _book(_spinoff_bundle())
    parent, child = _held(book, "AAA"), _held(book, "BBB")
    assert parent is not None and child is not None
    # The number is untouched (D21: the arithmetic was never the defect).
    assert child.payback_ratio == parent.payback_ratio == D("0.4")
    # The provenance is new.
    assert child.payback_from_symbol == "AAA"
    assert child.payback_carried_dividends == D("1200.00")
    assert child.payback_own_dividends == D("0")
    # And the parent carries none: it is the SOURCE, not a recipient.
    assert parent.payback_from_symbol is None
    assert parent.payback_carried_dividends is None
    assert parent.payback_own_dividends is None


def test_the_carried_amount_is_the_parents_dividend_portion_times_the_carry() -> None:
    """The figure is ``carved_original − carved_adjusted``, which is exactly the parent's
    pre-action ``dividend_portion × c`` — and, on a fresh child, its entire portion."""
    before = _held(_book(LedgerBundle([_buy("AAA", "100", "100")],
                                      [_cash("AAA", "4000", date(2026, 3, 1))],
                                      [], INSTR)), "AAA")
    child = _held(_book(_spinoff_bundle()), "BBB")
    assert before is not None and child is not None
    assert child.payback_carried_dividends == before.dividend_portion * D("0.30")
    assert child.payback_carried_dividends == child.dividend_portion


def test_a_later_own_dividend_keeps_the_label_and_is_counted_as_own() -> None:
    """The basis still ORIGINATES in the carve-out, so the label stays; the child's own
    payout lands in 自身配息 and the two add up to the portion the ratio is built on."""
    child = _held(_book(_spinoff_bundle(extra_divs=[_cash("BBB", "100", LATER)])), "BBB")
    assert child is not None
    assert child.payback_from_symbol == "AAA"
    assert child.payback_carried_dividends == D("1200.00")
    assert child.payback_own_dividends == D("100")
    assert child.dividend_portion == D("1300.00")
    assert child.payback_carried_dividends + child.payback_own_dividends == child.dividend_portion


# ------------------------------------------------------ the two semantic side-effects


def test_full_exit_and_rebuy_starts_a_new_position_without_the_label() -> None:
    """``build_book`` keeps the ``_Position`` object in its map after a full exit — only
    the holdings loop skips ``shares == 0`` — so the label must be cleared BY the exit,
    not assumed gone. A re-bought BBB is a new position with no carve-out in it."""
    bundle = _spinoff_bundle(extra_txs=[_sell("BBB", "50", "80", LATER),
                                        _buy("BBB", "10", "70", _AFTER)])
    child = _held(_book(bundle), "BBB")
    assert child is not None
    assert child.shares == D("10")
    assert child.payback_ratio == D("0")
    assert child.payback_from_symbol is None
    assert child.payback_carried_dividends is None
    assert child.payback_own_dividends is None


def test_a_partial_sell_keeps_the_amount_carried_at_the_spinoff() -> None:
    """Historical, not rescaled: half the position sold leaves the ratio at 0.4 and the
    carried amount at what actually moved on the action date."""
    child = _held(_book(_spinoff_bundle(extra_txs=[_sell("BBB", "25", "80", LATER)])), "BBB")
    assert child is not None
    assert child.shares == D("25")
    assert child.payback_ratio == D("0.4")
    assert child.dividend_portion == D("600.0000")
    assert child.payback_from_symbol == "AAA"
    assert child.payback_carried_dividends == D("1200.00")


# ------------------------------------------------------------------- chained actions


def test_a_second_spinoff_records_the_direct_parent_only() -> None:
    """BBB → CCC at 50%: CCC's basis was carved from BBB, so the label says BBB and the
    amount is BBB's portion × c — not recursed back to AAA. BBB keeps its own record."""
    bundle = _spinoff_bundle(extra_actions=[
        _act(CorporateActionKind.SPINOFF, to="1", frm="1", from_symbol="BBB",
             to_symbol="CCC", carry="0.5", day=LATER)])
    book = _book(bundle)
    child, grandchild = _held(book, "BBB"), _held(book, "CCC")
    assert child is not None and grandchild is not None
    assert grandchild.payback_from_symbol == "BBB"
    assert grandchild.payback_carried_dividends == D("600.000")      # (3000 − 1800) × 0.5
    assert grandchild.payback_carried_dividends == grandchild.dividend_portion
    assert child.payback_from_symbol == "AAA"
    assert child.payback_carried_dividends == D("1200.00")


def test_an_exchange_carries_the_label_with_the_position() -> None:
    """A rename must not launder the label (the same rule as QA-06's flag): CCC IS the
    carved-out position, so it still says 承接自 AAA with the same amount."""
    bundle = _spinoff_bundle(extra_actions=[
        _act(CorporateActionKind.EXCHANGE, to="1", frm="1", from_symbol="BBB",
             to_symbol="CCC", day=LATER)])
    book = _book(bundle)
    assert _held(book, "BBB") is None
    renamed = _held(book, "CCC")
    assert renamed is not None
    assert renamed.payback_from_symbol == "AAA"
    assert renamed.payback_carried_dividends == D("1200.00")
    assert renamed.payback_ratio == D("0.4")


def test_a_split_leaves_the_provenance_alone() -> None:
    bundle = _spinoff_bundle(extra_actions=[
        _act(CorporateActionKind.SPLIT, to="3", frm="1", from_symbol="BBB", day=LATER)])
    child = _held(_book(bundle), "BBB")
    assert child is not None
    assert child.shares == D("150")
    assert child.payback_from_symbol == "AAA"
    assert child.payback_carried_dividends == D("1200.00")
    assert child.payback_own_dividends == D("0")


# --------------------------------------------------------- the basis-discarding exits


def test_an_oversell_discards_the_label_with_the_basis() -> None:
    child = _held(_book(_spinoff_bundle(extra_txs=[_sell("BBB", "60", "80", LATER)]),
                        allow_oversell=True), "BBB")
    assert child is not None and child.oversold
    assert child.payback_from_symbol is None
    assert child.payback_carried_dividends is None


def test_a_declared_short_past_the_long_lot_clears_the_label() -> None:
    """The long lot is sold out first (a full exit) and a short opens on the remainder —
    a short holds proceeds, not a carved-in basis."""
    child = _held(_book(_spinoff_bundle(
        extra_txs=[_sell("BBB", "60", "80", LATER, short=True)])), "BBB")
    assert child is not None and child.short_open
    assert child.payback_from_symbol is None
    assert child.payback_carried_dividends is None


# ------------------------------------------------------------------------ the 反證


def test_positions_no_spinoff_ever_fed_carry_nothing() -> None:
    """An ordinary dividend-paying position, and one that only ever saw a SPLIT: the
    provenance fields are None and the existing figures are exactly what they were."""
    plain = LedgerBundle([_buy("AAA", "100", "10")],
                         [_cash("AAA", "400", date(2026, 3, 1))], [], INSTR)
    split = LedgerBundle([_buy("AAA", "100", "10")],
                         [_cash("AAA", "400", date(2026, 3, 1))], [], INSTR,
                         actions=[_act(CorporateActionKind.SPLIT, day=ACTION_DAY)])
    for bundle in (plain, split):
        h = _held(_book(bundle), "AAA")
        assert h is not None
        assert h.dividend_portion == D("400")
        assert h.payback_ratio == D("0.4")
        assert h.payback_from_symbol is None
        assert h.payback_carried_dividends is None
        assert h.payback_own_dividends is None
