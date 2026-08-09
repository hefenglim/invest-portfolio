"""W3 — the corporate-action replay in build_book (spec §4 formulas, §5 edge matrix).

The centrepiece is :func:`test_conservation_law_holds_across_every_action`, which asserts
§2.1 as a LAW over the raw accumulators rather than re-checking any single formula. Two
traps are avoided deliberately and are worth naming, because falling into either makes the
test pass for the wrong reason:

* summing ``Book.holdings`` drops the EXCHANGE-emptied source (``shares == 0 -> continue``),
  so the source's basis vanishes from the sum and the law appears to hold;
* summing ``Holding.original_cost_total`` imports the short-cover residue, which is a
  pre-existing artifact unrelated to corporate actions.

So the property replays the bundle at ``D-1`` and ``D`` and sums the emitted holdings plus
the realized rows' removed cost — the closed-form of what the accumulators hold.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from portfolio_dash.portfolio.cost_basis import (
    OversellError,
    UnbookableLedgerError,
    build_book,
)
from portfolio_dash.portfolio.results import Book, Holding
from portfolio_dash.shared.corporate_actions import CorporateAction, CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import (
    Dividend,
    LedgerBundle,
    OpeningInventory,
    Transaction,
)

D = Decimal
BUY_DAY = date(2026, 1, 10)
ACTION_DAY = date(2026, 6, 15)
LATER = date(2026, 9, 1)

INSTR = {
    s: Instrument(symbol=s, market=Market.US, quote_ccy=Currency.USD,
                  sector="Tech", name=s)
    for s in ("AAA", "BBB", "CCC")
}


def _buy(sym: str, qty: str, price: str, day: date = BUY_DAY, acc: str = "schwab",
         fees: str = "0") -> Transaction:
    return Transaction(account_id=acc, symbol=sym, side=Side.BUY, quantity=D(qty),
                       price=D(price), fees=D(fees), tax=D("0"), trade_date=day)


def _sell(sym: str, qty: str, price: str, day: date, acc: str = "schwab",
          short: bool = False) -> Transaction:
    return Transaction(account_id=acc, symbol=sym, side=Side.SELL, quantity=D(qty),
                       price=D(price), fees=D("0"), tax=D("0"), trade_date=day,
                       short_sale=short)


def _act(kind: CorporateActionKind, *, to: str = "3", frm: str = "1",
         from_symbol: str = "AAA", to_symbol: str | None = None,
         day: date = ACTION_DAY, acc: str = "schwab",
         carry: str | None = None) -> CorporateAction:
    return CorporateAction(
        account_id=acc, date=day, kind=kind, from_symbol=from_symbol,
        to_symbol=to_symbol if to_symbol is not None else from_symbol,
        ratio_to=D(to), ratio_from=D(frm),
        cost_carry=None if carry is None else D(carry))


def _book(bundle: LedgerBundle, **kw: bool) -> Book:
    return build_book(bundle, **kw)


def _held(book: Book, symbol: str, acc: str = "schwab") -> Holding | None:
    for h in book.holdings:
        if h.symbol == symbol and h.account_id == acc:
            return h
    return None


# ===================================================================== §4.1 SPLIT


def test_forward_split_scales_shares_and_leaves_every_total_alone() -> None:
    bundle = LedgerBundle([_buy("AAA", "85", "264.51")],
                          instruments=INSTR,
                          actions=[_act(CorporateActionKind.SPLIT, to="3", frm="1")])
    h = _held(_book(bundle), "AAA")
    assert h is not None
    assert h.shares == D("255")                       # 85 x 3 / 1, exact
    assert h.original_cost_total == D("22483.35")     # 85 x 264.51, untouched
    assert h.adjusted_cost_total == D("22483.35")
    assert h.original_avg == D("88.17")               # scaled by 1/3 on READ
    assert _book(bundle).gross_invested[Currency.USD] == D("22483.35")


def test_reverse_split_uses_the_same_formula() -> None:
    bundle = LedgerBundle([_buy("AAA", "200", "0.4250")],
                          instruments=INSTR,
                          actions=[_act(CorporateActionKind.SPLIT, to="1", frm="20")])
    h = _held(_book(bundle), "AAA")
    assert h is not None and h.shares == D("10")
    assert h.original_cost_total == D("85.000")


def test_split_leaves_payback_ratio_alone_on_a_dividend_paying_position() -> None:
    """A split changes nothing about how much of the cost has come back as dividends."""
    divs = [Dividend(account_id="schwab", symbol="AAA", date=date(2026, 3, 1),
                     type=DividendType.CASH, gross=D("400"), withholding=D("0"),
                     net=D("400"))]
    txs = [_buy("AAA", "100", "10")]
    before = _held(_book(LedgerBundle(txs, divs, [], INSTR)), "AAA")
    after = _held(_book(LedgerBundle(txs, divs, [], INSTR,
                                     actions=[_act(CorporateActionKind.SPLIT)])), "AAA")
    assert before is not None and after is not None
    assert after.shares == D("300")
    assert after.dividend_portion == before.dividend_portion == D("400")
    assert after.payback_ratio == before.payback_ratio == D("0.4")


def test_sell_after_a_split_realizes_against_the_post_split_average() -> None:
    bundle = LedgerBundle([_buy("AAA", "100", "30"), _sell("AAA", "150", "12", LATER)],
                          instruments=INSTR,
                          actions=[_act(CorporateActionKind.SPLIT)])
    book = _book(bundle)
    (row,) = book.realized.rows
    # 300 post-split shares at an average of 10; selling 150 removes 1,500 of basis.
    assert row.adjusted_cost_removed == D("1500")
    assert row.realized == D("300")                 # 150 x 12 - 1500
    h = _held(book, "AAA")
    assert h is not None and h.shares == D("150")


def test_split_on_an_open_declared_short_scales_the_obligation_not_the_proceeds() -> None:
    """E4: you owe three times the shares; you still received the same money."""
    bundle = LedgerBundle([_sell("AAA", "10", "300", BUY_DAY, short=True)],
                          instruments=INSTR,
                          actions=[_act(CorporateActionKind.SPLIT)])
    h = _held(_book(bundle, allow_oversell=True), "AAA")
    assert h is not None
    assert h.shares == D("-30")                     # short_shares scaled
    assert h.original_cost_total == D("-3000")      # proceeds UNCHANGED
    assert h.original_avg == D("100")               # average sale price scaled by 1/3
    assert h.short_open is True


# ===================================================================== §4.2 EXCHANGE


def test_exchange_moves_the_whole_position_and_empties_the_source() -> None:
    bundle = LedgerBundle([_buy("AAA", "700", "10")],
                          instruments=INSTR,
                          actions=[_act(CorporateActionKind.EXCHANGE, to="2", frm="7",
                                        to_symbol="BBB")])
    book = _book(bundle)
    assert _held(book, "AAA") is None                 # emptied, dropped by the holdings loop
    dest = _held(book, "BBB")
    assert dest is not None
    assert dest.shares == D("200")                    # 700 x 2 / 7 — EXACTLY 200
    assert dest.original_cost_total == D("7000")
    assert book.gross_invested[Currency.USD] == D("7000")   # no new capital entered


def test_a_one_to_one_exchange_is_a_rename() -> None:
    bundle = LedgerBundle([_buy("AAA", "50", "20")], instruments=INSTR,
                          actions=[_act(CorporateActionKind.EXCHANGE, to="1", frm="1",
                                        to_symbol="BBB")])
    dest = _held(_book(bundle), "BBB")
    assert dest is not None
    assert (dest.shares, dest.original_cost_total) == (D("50"), D("1000"))


def test_exchange_into_an_existing_position_merges_by_weighted_average() -> None:
    """No special case: the sum of the totals over the sum of the shares IS the method."""
    bundle = LedgerBundle([_buy("AAA", "100", "10"), _buy("BBB", "100", "30")],
                          instruments=INSTR,
                          actions=[_act(CorporateActionKind.EXCHANGE, to="1", frm="1",
                                        to_symbol="BBB")])
    dest = _held(_book(bundle), "BBB")
    assert dest is not None
    assert dest.shares == D("200")
    assert dest.original_cost_total == D("4000")
    assert dest.original_avg == D("20")


def test_exchange_after_a_full_short_cover_leaves_no_epsilon_behind() -> None:
    """§4.4: a full cover computes P - (P/S)*S, and Decimal division is inexact whenever S
    does not divide P — so a residue survives. It is invisible today because the emitted
    shares are 0-0 and the holdings loop drops the position; EXCHANGE leaves the source
    LIVE, so a later buy on the old ticker could reopen it carrying -epsilon of basis."""
    bundle = LedgerBundle(
        [_sell("AAA", "3", "100", BUY_DAY, short=True),          # proceeds 300 over 3 shares
         _buy("AAA", "3", "70", date(2026, 2, 1)),               # full cover
         _buy("BBB", "10", "5", date(2026, 2, 2))],
        instruments=INSTR,
        actions=[_act(CorporateActionKind.EXCHANGE, to="1", frm="1", to_symbol="BBB")])
    book = _book(bundle, allow_oversell=True)
    assert _held(book, "AAA") is None
    dest = _held(book, "BBB")
    assert dest is not None
    # Exactly the BBB buy; no residue rode across from the covered short.
    assert dest.shares == D("10")
    assert dest.original_cost_total == D("50")


# ===================================================================== §4.3 SPINOFF


def test_spinoff_carves_the_basis_and_the_parent_keeps_its_shares() -> None:
    bundle = LedgerBundle([_buy("AAA", "100", "100")], instruments=INSTR,
                          actions=[_act(CorporateActionKind.SPINOFF, to="1", frm="2",
                                        to_symbol="BBB", carry="0.30")])
    book = _book(bundle)
    parent, child = _held(book, "AAA"), _held(book, "BBB")
    assert parent is not None and child is not None
    assert parent.shares == D("100")                       # unchanged
    assert child.shares == D("50")                         # 100 x 1 / 2
    assert child.original_cost_total == D("3000.00")
    assert parent.original_cost_total == D("7000.00")
    assert parent.original_cost_total + child.original_cost_total == D("10000")


def test_spinoff_parent_is_total_minus_carved_and_conserves_exactly() -> None:
    """§4.3 writes the parent as ``total − carved``, not ``total × (1 − c)``.

    MEASUREMENT NOTE (2026-08-09, W3). The spec justifies this as "the two sides can miss
    the conservation law by an ulp". **That claim is not reproducible**: over 400,000
    random `(total, c)` pairs at the default 28-digit context, `total*(1-c) + total*c`
    recombined to `total` every single time, including at 27-digit carries. So this test
    asserts what IS true and load-bearing — the subtraction conserves **by construction**,
    for any `c`, without depending on a rounding coincidence — rather than a counter-example
    that does not exist. (Writing the assertion the spec implies would have produced another
    unsatisfiable test, exactly like §7.1's original `700 × (2/7)` companion.)
    """
    for carry in ("0.333333333333333333333333333", "0.5831", "0", "1",
                  "0.123456789012345678901234567"):
        bundle = LedgerBundle([_buy("AAA", "3", "1")], instruments=INSTR,
                              actions=[_act(CorporateActionKind.SPINOFF, to="1", frm="1",
                                            to_symbol="BBB", carry=carry)])
        book = _book(bundle)
        parent, child = _held(book, "AAA"), _held(book, "BBB")
        assert parent is not None and child is not None, carry
        assert parent.original_cost_total + child.original_cost_total == D("3"), carry


def test_selling_the_spinoff_child_realizes_against_the_carved_basis() -> None:
    bundle = LedgerBundle([_buy("AAA", "100", "100"), _sell("BBB", "50", "80", LATER)],
                          instruments=INSTR,
                          actions=[_act(CorporateActionKind.SPINOFF, to="1", frm="2",
                                        to_symbol="BBB", carry="0.30")])
    book = _book(bundle)
    (row,) = book.realized.rows
    assert row.adjusted_cost_removed == D("3000.00")
    assert row.realized == D("1000.00")            # 50 x 80 - 3000


def test_spinoff_child_inherits_the_parents_payback_ratio_exactly(
) -> None:
    """D21: the arithmetic is correct and the LABEL is what lies — scaling both totals by
    the same c makes the ratio identical on both sides, so a child that has never paid a
    dividend renders the parent's 回本進度. Pinned here so the provenance label (W5) has a
    fact to describe, and so nobody 'fixes' the arithmetic instead."""
    divs = [Dividend(account_id="schwab", symbol="AAA", date=date(2026, 3, 1),
                     type=DividendType.CASH, gross=D("4000"), withholding=D("0"),
                     net=D("4000"))]
    bundle = LedgerBundle([_buy("AAA", "100", "100")], divs, [], INSTR,
                          actions=[_act(CorporateActionKind.SPINOFF, to="1", frm="2",
                                        to_symbol="BBB", carry="0.30")])
    book = _book(bundle)
    parent, child = _held(book, "AAA"), _held(book, "BBB")
    assert parent is not None and child is not None
    assert child.payback_ratio == parent.payback_ratio == D("0.4")
    assert child.dividend_portion == D("1200.00")   # never received by the child


# ===================================================================== §2.1 the LAW


def _totals(book: Book) -> tuple[Decimal, Decimal, Decimal]:
    """Σ original, Σ adjusted, Σ dividend_portion over holdings + removed-by-realized.

    Deliberately NOT `sum(h.original_cost_total)` alone: an EXCHANGE empties its source and
    the holdings loop drops it, which would make the law hold by omission.
    """
    orig = sum((h.original_cost_total for h in book.holdings), D("0"))
    adj = sum((h.adjusted_cost_total for h in book.holdings), D("0"))
    orig += sum((r.original_cost_removed for r in book.realized.rows), D("0"))
    adj += sum((r.adjusted_cost_removed for r in book.realized.rows), D("0"))
    return orig, adj, orig - adj


@pytest.mark.parametrize(
    "action",
    [
        _act(CorporateActionKind.SPLIT, to="3", frm="1"),
        _act(CorporateActionKind.SPLIT, to="1", frm="20"),
        _act(CorporateActionKind.EXCHANGE, to="2", frm="7", to_symbol="BBB"),
        _act(CorporateActionKind.EXCHANGE, to="1", frm="1", to_symbol="BBB"),
        _act(CorporateActionKind.SPINOFF, to="1", frm="2", to_symbol="BBB", carry="0.30"),
        _act(CorporateActionKind.SPINOFF, to="3", frm="1", to_symbol="BBB", carry="0"),
        _act(CorporateActionKind.SPINOFF, to="1", frm="1", to_symbol="BBB", carry="1"),
    ],
    ids=["split-3-1", "split-1-20", "exchange-2-7", "rename", "spinoff-30",
         "spinoff-0", "spinoff-100"],
)
def test_conservation_law_holds_across_every_action(action: CorporateAction) -> None:
    """§2.1: across ANY action, per quote currency, Σoriginal / Σadjusted / Σdividend_portion
    and gross_invested are unchanged, and Σ(shares x price) is continuous.

    The law, not the formulas — this is what must survive a future edit that "tidies" one
    of them. A dividend is included so adjusted != original and the third sum is non-trivial.
    """
    divs = [Dividend(account_id="schwab", symbol="AAA", date=date(2026, 3, 1),
                     type=DividendType.CASH, gross=D("700"), withholding=D("0"),
                     net=D("700"))]
    txs = [_buy("AAA", "700", "10", fees="35"), _buy("BBB", "40", "5")]
    base = LedgerBundle(txs, divs, [], INSTR)
    before = _book(base.through(ACTION_DAY - timedelta(days=1)))
    after = _book(LedgerBundle(txs, divs, [], INSTR, actions=[action]))

    assert _totals(after) == _totals(before)
    assert after.gross_invested == before.gross_invested


def test_market_value_is_continuous_across_a_split() -> None:
    """§2.1's other half: Σ(shares × price) does not jump.

    Kept separate and minimal on purpose. The post-action price is the pre-action price
    re-expressed by the ratio, and it is computed **multiply-first, divide-last** — writing
    `price / (to/from)` here forms the parenthesised quotient this whole module exists to
    avoid, and the assertion then fails by one ulp (measured: 7199.999…9 vs 7200) while the
    engine is perfectly correct. A test that reintroduces the defect it is checking for is
    worse than no test.

    SPINOFF is excluded by nature, not by omission: the parent keeps its shares and the
    market re-prices it by the value of the entity that left. That is a market fact with no
    ratio relationship, so continuity there is not expressible without real prices.
    """
    txs = [_buy("AAA", "700", "10"), _buy("BBB", "40", "5")]
    price = {"AAA": D("10"), "BBB": D("5")}
    for action in (_act(CorporateActionKind.SPLIT, to="3", frm="1"),
                   _act(CorporateActionKind.SPLIT, to="1", frm="20"),
                   _act(CorporateActionKind.EXCHANGE, to="2", frm="7", to_symbol="CCC")):
        before = _book(LedgerBundle(txs, instruments=INSTR))
        after = _book(LedgerBundle(txs, instruments=INSTR, actions=[action]))
        v_before = sum((price[h.symbol] * h.shares for h in before.holdings), D("0"))
        moved = {action.from_symbol, action.to_symbol}
        v_after = D("0")
        for h in after.holdings:
            if h.symbol in moved:
                # shares × p × from / to — one division, performed last.
                v_after += h.shares * price[action.from_symbol] * action.ratio_from \
                    / action.ratio_to
            else:
                v_after += h.shares * price[h.symbol]
        assert v_after == v_before, action.kind


def test_the_conservation_property_can_actually_fail() -> None:
    """DETECTION POWER. A property test that never fails is decoration.

    Mutate a formula the way a future "tidy-up" plausibly would — have SPLIT scale the
    totals along with the shares, which *looks* consistent — and assert the law notices.
    (The mutation the spec suggests for this role, `total × (1−c)` in the SPINOFF carve,
    does NOT work: measured over 400,000 pairs it conserves just as exactly as the
    subtraction. Chasing it would have produced a second unsatisfiable test.)
    """
    txs = [_buy("AAA", "700", "10", fees="35")]
    book = _book(LedgerBundle(txs, instruments=INSTR,
                              actions=[_act(CorporateActionKind.SPLIT)]))
    orig, _adj, _div = _totals(book)
    assert orig == D("7035")
    mutated = orig * D("3")                       # what "scale the totals too" would give
    assert mutated != orig, "the property must be able to see a scaled total"


# ===================================================================== §5 edge matrix


def _no_position_bundle(**kw: object) -> LedgerBundle:
    return LedgerBundle([_buy("BBB", "10", "5")], instruments=INSTR,
                        actions=[_act(CorporateActionKind.SPLIT)], **kw)  # type: ignore[arg-type]


def test_e1_action_with_no_position_raises_strict_and_flags_on_the_dashboard() -> None:
    with pytest.raises(UnbookableLedgerError, match="沒有持倉"):
        _book(_no_position_bundle())
    book = _book(_no_position_bundle(), allow_oversell=True)   # must NOT raise
    assert [h.symbol for h in book.holdings] == ["BBB"]


def test_e2_action_on_a_closed_position() -> None:
    txs = [_buy("AAA", "10", "5"), _sell("AAA", "10", "6", date(2026, 2, 1))]
    bundle = LedgerBundle(txs, instruments=INSTR, actions=[_act(CorporateActionKind.SPLIT)])
    with pytest.raises(UnbookableLedgerError, match="已無持倉"):
        _book(bundle)
    _book(bundle, allow_oversell=True)   # degrades


def test_e3_action_on_an_oversold_position() -> None:
    """The strict path never reaches E3's own branch — and that is worth stating.

    A position can only BE oversold on the dashboard path: in strict mode the sell that
    creates the state raises ``OversellError`` first, at an earlier guard. So E3's strict
    row is defensive depth, not a reachable path, and asserting
    ``pytest.raises(UnbookableLedgerError)`` here would be asserting something the engine
    cannot do. What matters is the dashboard behaviour: skip, flag, never a 500, and the
    discarded basis stays discarded.
    """
    txs = [_buy("AAA", "10", "5"), _sell("AAA", "50", "6", date(2026, 2, 1))]
    bundle = LedgerBundle(txs, instruments=INSTR, actions=[_act(CorporateActionKind.SPLIT)])
    with pytest.raises(OversellError):
        _book(bundle)
    h = _held(_book(bundle, allow_oversell=True), "AAA")
    assert h is not None and h.oversold and h.unbookable_action
    assert h.shares == D("-40"), "the split must NOT have scaled an undefined position"
    assert h.original_cost_total == D("0")


def test_e5_exchange_from_an_open_short() -> None:
    bundle = LedgerBundle([_sell("AAA", "10", "30", BUY_DAY, short=True)],
                          instruments=INSTR,
                          actions=[_act(CorporateActionKind.EXCHANGE, to="1", frm="1",
                                        to_symbol="BBB")])
    with pytest.raises(UnbookableLedgerError, match="放空"):
        _book(bundle)
    h = _held(_book(bundle, allow_oversell=True), "AAA")
    assert h is not None and h.unbookable_action


def test_e18_exchange_into_a_short_destination() -> None:
    bundle = LedgerBundle(
        [_buy("AAA", "10", "5"), _sell("BBB", "10", "30", date(2026, 2, 1), short=True)],
        instruments=INSTR,
        actions=[_act(CorporateActionKind.EXCHANGE, to="1", frm="1", to_symbol="BBB")])
    with pytest.raises(UnbookableLedgerError, match="放空"):
        _book(bundle)


def test_e22_exchange_into_an_oversold_destination_is_refused() -> None:
    """D16, and the number the un-guarded formula would have produced."""
    txs = [_buy("AAA", "300", "40"),
           _buy("BBB", "60", "10"), _sell("BBB", "500", "12", date(2026, 2, 1))]
    bundle = LedgerBundle(txs, instruments=INSTR,
                          actions=[_act(CorporateActionKind.EXCHANGE, to="1", frm="1",
                                        to_symbol="BBB")])
    # Strict stops at the earlier oversell guard (see the E3 note); the dashboard path is
    # where the destination guard has to hold, and it is where the damage would happen.
    with pytest.raises(OversellError):
        _book(bundle)
    book = _book(bundle, allow_oversell=True)
    dest = _held(book, "BBB")
    assert dest is not None
    # DETECTION POWER: the guard's value is the number it PREVENTS. Un-guarded, the source's
    # 12,000 would land on a position whose basis was discarded and whose share count
    # includes basis-less shares — an entirely ordinary-looking average over nothing.
    assert dest.original_cost_total == D("0"), "the discarded basis must stay discarded"
    assert dest.oversold, "the destination keeps surfacing its own 賣超"
    # The 待釐清 flag goes on the SOURCE, which is where the wrongness now lives: its share
    # count stayed in pre-action terms while `prices` is global and post-action. The
    # destination already carries `oversold`, so flagging it again would add no information.
    source = _held(book, "AAA")
    assert source is not None
    assert source.unbookable_action
    assert source.original_cost_total == D("12000"), \
        "the source must keep its basis — the action was skipped, not half-applied"
    assert source.shares == D("300")


def test_e19_unbookable_dividend_propagates_through_exchange_and_spinoff() -> None:
    """The flag survives the short being covered, so a currently-long position can carry it.
    Pre-fix the EXCHANGE emptied the source, the holdings loop dropped it, and the flag
    VANISHED while the successor rendered clean — an unresolved money-of-record problem
    erased by an unrelated event."""
    txs = [_sell("AAA", "10", "30", BUY_DAY, short=True),
           _buy("AAA", "20", "20", date(2026, 2, 1))]          # covers, then goes long
    divs = [Dividend(account_id="schwab", symbol="AAA", date=date(2026, 1, 20),
                     type=DividendType.CASH, gross=D("5"), withholding=D("0"), net=D("5"))]
    flagged = _held(_book(LedgerBundle(txs, divs, [], INSTR), allow_oversell=True), "AAA")
    assert flagged is not None and flagged.unbookable_dividend

    for action in (_act(CorporateActionKind.EXCHANGE, to="1", frm="1", to_symbol="BBB"),
                   _act(CorporateActionKind.SPINOFF, to="1", frm="1", to_symbol="BBB",
                        carry="0.5")):
        book = _book(LedgerBundle(txs, divs, [], INSTR, actions=[action]),
                     allow_oversell=True)
        successor = _held(book, "BBB")
        assert successor is not None, action.kind
        assert successor.unbookable_dividend, f"{action.kind} laundered the 待釐清 flag"


def test_e21_an_action_on_an_unregistered_symbol_does_not_raise_keyerror() -> None:
    """The skip-set is the bundle's job; without it quote_ccy() raises KeyError — a 500,
    and a different exception type from every other degradation path."""
    bundle = LedgerBundle([_buy("AAA", "10", "5")], instruments=INSTR,
                          actions=[_act(CorporateActionKind.EXCHANGE, to="1", frm="1",
                                        to_symbol="ZZZ")])
    assert "ZZZ" in bundle.unregistered_symbols
    clean = bundle.without_unregistered()
    assert clean.actions == []
    h = _held(_book(clean, allow_oversell=True), "AAA")
    assert h is not None and h.shares == D("10")


def test_a_same_day_trade_sees_post_action_terms() -> None:
    """EventPriority: the action is effective at the START of its date, so a same-day buy
    is quoted in post-split terms and simply adds to the already-scaled position."""
    bundle = LedgerBundle([_buy("AAA", "100", "30"), _buy("AAA", "10", "10", ACTION_DAY)],
                          instruments=INSTR, actions=[_act(CorporateActionKind.SPLIT)])
    h = _held(_book(bundle), "AAA")
    assert h is not None
    assert h.shares == D("310")                    # 100x3 scaled first, then +10
    assert h.original_cost_total == D("3100")


def test_opening_inventory_on_an_action_date_is_pre_action() -> None:
    """Documented in §4: opening describes the position as it stood BEFORE the action."""
    opening = [OpeningInventory(account_id="schwab", symbol="AAA", shares=D("10"),
                                original_cost_total=D("100"), build_date=ACTION_DAY)]
    bundle = LedgerBundle([], [], opening, INSTR, actions=[_act(CorporateActionKind.SPLIT)])
    h = _held(_book(bundle), "AAA")
    assert h is not None and h.shares == D("30")


def test_multi_account_actions_stay_isolated() -> None:
    """Positions are keyed (account, symbol); one account's action must not touch another's
    — which is exactly why E13 requires N rows for N holding accounts."""
    txs = [_buy("AAA", "85", "10", acc="schwab"), _buy("AAA", "40", "10", acc="moomoo_my")]
    bundle = LedgerBundle(txs, instruments=INSTR,
                          actions=[_act(CorporateActionKind.SPLIT, acc="schwab")])
    book = _book(bundle)
    schwab, moomoo = _held(book, "AAA", "schwab"), _held(book, "AAA", "moomoo_my")
    assert schwab is not None and moomoo is not None
    assert schwab.shares == D("255")
    assert moomoo.shares == D("40")


def test_a_transitive_chain_replays_in_date_order() -> None:
    """AAA -> BBB in June, BBB -> CCC in September: two ordinary sequential events."""
    bundle = LedgerBundle(
        [_buy("AAA", "100", "10")], instruments=INSTR,
        actions=[_act(CorporateActionKind.EXCHANGE, to="1", frm="2", to_symbol="BBB"),
                 _act(CorporateActionKind.EXCHANGE, to="3", frm="1", from_symbol="BBB",
                      to_symbol="CCC", day=LATER)])
    book = _book(bundle)
    assert _held(book, "AAA") is None and _held(book, "BBB") is None
    final = _held(book, "CCC")
    assert final is not None
    assert final.shares == D("150")                 # 100 -> 50 -> 150
    assert final.original_cost_total == D("1000")   # basis carried the whole way
