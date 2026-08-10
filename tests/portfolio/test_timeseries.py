import inspect
import sys
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from types import FrameType
from typing import Any

from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.portfolio.timeseries import daily_value_series
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

USD = Currency.USD
TWD = Currency.TWD

INSTRUMENTS = {
    "AAA": Instrument(symbol="AAA", market=Market.US, quote_ccy=USD,
                      sector="Tech", name="AAA Corp"),
    "BBB": Instrument(symbol="BBB", market=Market.TW, quote_ccy=TWD,
                      sector="Semis", name="BBB Corp", board="TWSE"),
}

# A second USD name, so an EXCHANGE below moves a position WITHIN one currency (a
# cross-currency EXCHANGE would confound the arithmetic these tests are asserting on).
INSTRUMENTS_USD_PAIR = {
    **INSTRUMENTS,
    "CCC": Instrument(symbol="CCC", market=Market.US, quote_ccy=USD,
                      sector="Tech", name="CCC Corp"),
}


def _tx(day: date, side: Side, qty: str, price: str, fees: str = "1",
        symbol: str = "AAA") -> Transaction:
    return Transaction(account_id="schwab", symbol=symbol, side=side,
                       quantity=Decimal(qty), price=Decimal(price),
                       fees=Decimal(fees), tax=Decimal("0"), trade_date=day)


def test_carry_forward_values_and_net_invested() -> None:
    txs = [_tx(date(2026, 6, 1), Side.BUY, "10", "100")]
    prices = {"AAA": [(date(2026, 6, 1), Decimal("100")),
                      (date(2026, 6, 3), Decimal("110"))]}
    fx = {(USD, TWD): [(date(2026, 6, 1), Decimal("30"))]}
    series = daily_value_series(LedgerBundle(txs, instruments=INSTRUMENTS), prices, fx, TWD,
                                end=date(2026, 6, 4))
    assert series.available is True
    assert [p.date for p in series.points] == [
        date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3), date(2026, 6, 4)]
    assert [p.total_value for p in series.points] == [
        Decimal("30000"), Decimal("30000"), Decimal("33000"), Decimal("33000")]
    # net invested = (10*100 + 1 fee) * 30 on every day after the buy
    assert all(p.net_invested == Decimal("30030") for p in series.points)
    assert all(p.incomplete is False for p in series.points)


def test_missing_early_price_flags_incomplete() -> None:
    txs = [_tx(date(2026, 6, 1), Side.BUY, "10", "100")]
    prices = {"AAA": [(date(2026, 6, 2), Decimal("100"))]}  # nothing on day 1
    fx = {(USD, TWD): [(date(2026, 6, 1), Decimal("30"))]}
    series = daily_value_series(LedgerBundle(txs, instruments=INSTRUMENTS), prices, fx, TWD,
                                end=date(2026, 6, 2))
    assert series.points[0].incomplete is True
    assert series.points[0].total_value == Decimal("0")
    assert series.points[1].incomplete is False
    assert series.points[1].total_value == Decimal("30000")


def test_inverse_pair_fallback() -> None:
    txs = [_tx(date(2026, 6, 1), Side.BUY, "10", "100", fees="0")]
    prices = {"AAA": [(date(2026, 6, 1), Decimal("100"))]}
    fx = {(TWD, USD): [(date(2026, 6, 1), Decimal("0.03125"))]}  # 1/0.03125 = 32
    series = daily_value_series(LedgerBundle(txs, instruments=INSTRUMENTS), prices, fx, TWD,
                                end=date(2026, 6, 1))
    assert series.available is True
    assert series.points[0].total_value == Decimal("32000")
    assert series.points[0].net_invested == Decimal("32000")


def test_dividend_and_sell_reduce_net_invested() -> None:
    txs = [_tx(date(2026, 6, 1), Side.BUY, "10", "100"),
           _tx(date(2026, 6, 3), Side.SELL, "5", "120")]
    divs = [Dividend(account_id="schwab", symbol="AAA", date=date(2026, 6, 2),
                     type=DividendType.CASH, gross=Decimal("50"),
                     withholding=Decimal("0"), net=Decimal("50"))]
    prices = {"AAA": [(date(2026, 6, 1), Decimal("100")),
                      (date(2026, 6, 3), Decimal("120"))]}
    fx = {(USD, TWD): [(date(2026, 6, 1), Decimal("30"))]}
    series = daily_value_series(LedgerBundle(txs, divs, instruments=INSTRUMENTS),
                                prices, fx, TWD, end=date(2026, 6, 3))
    # day1: +1001*30 = 30030 ; day2: -50*30 -> 28530 ; day3: -(600-1)*30 -> 10560
    assert [p.net_invested for p in series.points] == [
        Decimal("30030"), Decimal("28530"), Decimal("10560")]
    assert series.points[2].total_value == Decimal("18000")  # 5 sh * 120 * 30


def test_opening_inventory_counts_as_invested() -> None:
    opening = [OpeningInventory(account_id="tw_broker", symbol="BBB",
                                shares=Decimal("10"),
                                original_cost_total=Decimal("900"),
                                build_date=date(2026, 6, 1))]
    prices = {"BBB": [(date(2026, 6, 1), Decimal("100"))]}
    series = daily_value_series(LedgerBundle(opening=opening, instruments=INSTRUMENTS),
                                prices, {}, TWD, end=date(2026, 6, 1))
    assert series.available is True  # TWD->TWD needs no FX rows
    assert series.points[0].total_value == Decimal("1000")
    assert series.points[0].net_invested == Decimal("900")


def test_missing_flow_fx_makes_series_unavailable() -> None:
    txs = [_tx(date(2026, 6, 1), Side.BUY, "10", "100")]
    prices = {"AAA": [(date(2026, 6, 1), Decimal("100"))]}
    series = daily_value_series(LedgerBundle(txs, instruments=INSTRUMENTS), prices, {}, TWD,
                                end=date(2026, 6, 2))
    assert series.available is False
    assert series.points == []


def test_empty_ledgers_unavailable() -> None:
    series = daily_value_series(LedgerBundle(instruments=INSTRUMENTS), {}, {}, TWD,
                                end=date(2026, 6, 1))
    assert series.available is False
    assert series.points == []


def test_skipped_corporate_action_makes_the_day_incomplete() -> None:
    """A position whose corporate action was SKIPPED must not be valued (audit F-05).

    ``prices`` is a GLOBAL, post-action series while a skipped action leaves ``shares`` in
    PRE-action terms, so ``price * shares`` is wrong by the whole ratio. Before the flag was
    wired into the guard the product landed in the trend and the net-worth series as though
    valid, and unflagged — the exact wrongness the flag was introduced to make visible.

    The ledger below is E18: an EXCHANGE whose DESTINATION holds an open declared short.
    ``build_book`` refuses it and flags the SOURCE, which stays an ordinary long position —
    ``oversold`` / ``short_open`` / ``unbookable_dividend`` are all False on it, so nothing
    but ``unbookable_action`` can be what marks the day.
    """
    txs = [
        _tx(date(2026, 6, 1), Side.BUY, "10", "100", fees="0"),          # AAA long, USD
        Transaction(account_id="schwab", symbol="BBB", side=Side.SELL,   # BBB short, TWD
                    quantity=Decimal("5"), price=Decimal("50"),
                    fees=Decimal("0"), tax=Decimal("0"),
                    trade_date=date(2026, 6, 1), short_sale=True),
    ]
    actions = [CorporateAction(account_id="schwab", date=date(2026, 6, 2),
                               kind=CorporateActionKind.EXCHANGE,
                               from_symbol="AAA", to_symbol="BBB",
                               ratio_to=Decimal("1"), ratio_from=Decimal("1"))]
    prices = {"AAA": [(date(2026, 6, 1), Decimal("100"))],
              "BBB": [(date(2026, 6, 1), Decimal("50"))]}
    fx = {(USD, TWD): [(date(2026, 6, 1), Decimal("30"))]}
    bundle = LedgerBundle(txs, instruments=INSTRUMENTS, actions=actions)

    # Sanity: the replay really does flag the SOURCE and nothing else about it.
    flagged = next(h for h in build_book(bundle, allow_oversell=True).holdings
                   if h.symbol == "AAA")
    assert flagged.unbookable_action is True
    assert (flagged.oversold, flagged.short_open, flagged.unbookable_dividend) == (
        False, False, False)
    assert flagged.shares == Decimal("10")  # still PRE-action

    series = daily_value_series(bundle, prices, fx, TWD, end=date(2026, 6, 2))
    assert series.available is True
    day1, day2 = series.points

    # 6/1 — the action is not in scope yet, so the day is ordinary and fully valued:
    # AAA 10 x 100 USD x 30 = 30,000, plus the short's negative 5 x 50 = -250.
    assert day1.incomplete is False
    assert day1.total_value == Decimal("29750")

    # 6/2 — the action was skipped, so AAA's 30,000 must NOT be counted and the day must
    # SAY so. Only the (unaffected) short remains.
    assert day2.incomplete is True
    assert day2.total_value == Decimal("-250")


# --- the two refusals that leave NO holding to flag (audit F-47 / F-49) ---------------
#
# `_reject` writes `unbookable_action` onto the SOURCE position. The source is `None` when
# it never existed, and a ZERO-share position — which the holdings loop drops — when an
# earlier action already emptied it. So two of the three refusal paths reach the per-holding
# check with nothing flagged at all, and only `Book.unapplied_actions` sees them. The test
# above covers the third (the source survives and IS flagged).


def test_a_refused_action_with_no_position_to_flag_still_marks_the_day() -> None:
    """E1 — the source never existed, so NOTHING is flagged, and the total is wrong.

    A split needs one action row per ACCOUNT that holds the symbol. Book it against the
    wrong account (an ordinary slip) and ``_apply_action`` refuses at E1 with ``source is
    None`` — there is no position to write ``unbookable_action`` onto, so every holding in
    the book comes back clean.

    The damage is not hypothetical: ``price_history`` is GLOBAL and already POST-split, so
    the account that really holds the stock is valued at PRE-split shares against a
    post-split price. 6/3 below reports 15,000 when the position is worth 30,000 — half the
    truth, and before this gate it was published ``incomplete=False``.
    """
    txs = [_tx(date(2026, 6, 1), Side.BUY, "10", "100", fees="0")]     # schwab holds AAA
    actions = [CorporateAction(account_id="moomoo_my",                 # ...but not here
                               date=date(2026, 6, 3),
                               kind=CorporateActionKind.SPLIT,
                               from_symbol="AAA", to_symbol="AAA",
                               ratio_to=Decimal("2"), ratio_from=Decimal("1"))]
    prices = {"AAA": [(date(2026, 6, 1), Decimal("100")),
                      (date(2026, 6, 3), Decimal("50"))]}              # post-split price
    fx = {(USD, TWD): [(date(2026, 6, 1), Decimal("30"))]}
    bundle = LedgerBundle(txs, instruments=INSTRUMENTS, actions=actions)

    # Sanity: the refusal is recorded on the BOOK and nowhere else. If this ever starts
    # flagging a holding, the per-holding check below would catch the day on its own and
    # this test would stop testing what it says it tests.
    book = build_book(bundle, allow_oversell=True)
    assert len(book.unapplied_actions) == 1
    assert [h.symbol for h in book.holdings] == ["AAA"]
    assert not any(h.unbookable_action or h.oversold or h.unbookable_dividend
                   or h.short_open for h in book.holdings)

    series = daily_value_series(bundle, prices, fx, TWD, end=date(2026, 6, 4))
    assert series.available is True
    # DATE SCOPING — the whole reason a book-level flag is safe here: `book` is rebuilt per
    # day from `bundle.through(day)`, whose `actions` filter is `a.date <= day`, so the
    # refusal dated 6/3 does NOT reach back and mark 6/1 or 6/2.
    assert [p.incomplete for p in series.points] == [False, False, True, True]
    assert series.points[1].total_value == Decimal("30000")   # 6/2, pre-action: correct
    assert series.points[2].total_value == Decimal("15000")   # 6/3: HALF the truth


def test_a_duplicated_action_row_whose_flag_is_dropped_still_marks_the_day() -> None:
    """E2 — the source is already empty, so its flag is dropped with its carrier.

    The same EXCHANGE entered twice: the first moves AAA→CCC and leaves AAA at zero shares;
    the second hits ``source.shares == _ZERO`` and is refused. ``_reject`` DOES flag AAA
    this time — but AAA has no shares left, so ``build_book``'s ``if shares == _ZERO:
    continue`` drops the holding and the flag goes with it. CCC (which received the
    position before the flag was set) is clean, so the day looks entirely ordinary.
    """
    txs = [_tx(date(2026, 6, 1), Side.BUY, "10", "100", fees="0")]
    dupe = CorporateAction(account_id="schwab", date=date(2026, 6, 3),
                           kind=CorporateActionKind.EXCHANGE,
                           from_symbol="AAA", to_symbol="CCC",
                           ratio_to=Decimal("1"), ratio_from=Decimal("1"))
    prices = {"AAA": [(date(2026, 6, 1), Decimal("100"))],
              "CCC": [(date(2026, 6, 1), Decimal("100"))]}
    fx = {(USD, TWD): [(date(2026, 6, 1), Decimal("30"))]}
    bundle = LedgerBundle(txs, instruments=INSTRUMENTS_USD_PAIR, actions=[dupe, dupe])

    book = build_book(bundle, allow_oversell=True)
    assert len(book.unapplied_actions) == 1
    assert [h.symbol for h in book.holdings] == ["CCC"]       # AAA dropped at zero shares
    assert not any(h.unbookable_action or h.oversold or h.unbookable_dividend
                   or h.short_open for h in book.holdings)

    series = daily_value_series(bundle, prices, fx, TWD, end=date(2026, 6, 4))
    assert series.available is True
    assert [p.incomplete for p in series.points] == [False, False, True, True]
    assert [p.total_value for p in series.points] == [Decimal("30000")] * 4


def test_a_refused_action_changes_only_the_flag_never_a_number() -> None:
    """Containment: the gate withholds a CERTIFICATION, it does not restate a total.

    The same ledger with and without the (refused) action row must produce byte-identical
    ``total_value`` / ``net_invested`` / ``net_worth`` on every single day — the ONLY
    difference is ``incomplete``. A gate that also moved a number would be a second
    money-of-record path, which is what D38 invariant 1 exists to prevent.
    """
    txs = [_tx(date(2026, 6, 1), Side.BUY, "10", "100", fees="0")]
    actions = [CorporateAction(account_id="moomoo_my", date=date(2026, 6, 3),
                               kind=CorporateActionKind.SPLIT,
                               from_symbol="AAA", to_symbol="AAA",
                               ratio_to=Decimal("2"), ratio_from=Decimal("1"))]
    prices = {"AAA": [(date(2026, 6, 1), Decimal("100")),
                      (date(2026, 6, 3), Decimal("50"))]}
    fx = {(USD, TWD): [(date(2026, 6, 1), Decimal("30"))]}

    def numbers(acts: list[CorporateAction]) -> list[tuple[Any, ...]]:
        s = daily_value_series(
            LedgerBundle(txs, instruments=INSTRUMENTS, actions=acts),
            prices, fx, TWD, end=date(2026, 6, 4))
        return [(p.date, p.total_value, p.net_invested, p.net_worth) for p in s.points]

    assert numbers(actions) == numbers([])

    flags = daily_value_series(LedgerBundle(txs, instruments=INSTRUMENTS, actions=actions),
                               prices, fx, TWD, end=date(2026, 6, 4))
    clean = daily_value_series(LedgerBundle(txs, instruments=INSTRUMENTS),
                               prices, fx, TWD, end=date(2026, 6, 4))
    assert [p.incomplete for p in flags.points] == [False, False, True, True]
    assert [p.incomplete for p in clean.points] == [False] * 4


# --- D38 invariant 1: the new branch must not EXECUTE for an action-free ledger --------


_GATE_BODY_MARKER = "無法套用的公司行動"
# Deliberately just the attribute read, not the whole ``if`` line: the probe must still
# find the gate after a rewrite (``if not ...``, ``incomplete = bool(...)``) so that such a
# rewrite FAILS this test instead of silently disarming it.
_GATE_TEST_MARKER = "book.unapplied_actions"


def _line_of(marker: str) -> int:
    """Absolute line number of the unique ``marker`` line inside ``daily_value_series``.

    Located by source text rather than hard-coded, so the probe survives edits above it —
    a line-number literal would rot into a test that silently checks the wrong line.
    """
    lines, first = inspect.getsourcelines(daily_value_series)
    hits = [first + i for i, text in enumerate(lines) if marker in text]
    assert len(hits) == 1, f"expected exactly one {marker!r} line in the source, got {hits}"
    return hits[0]


def _lines_executed(call: Callable[[], object]) -> set[int]:
    """Line numbers executed inside ``daily_value_series``' OWN code object during *call*."""
    target = daily_value_series.__code__
    seen: set[int] = set()

    def _trace(frame: FrameType, event: str, arg: Any) -> Any:
        if frame.f_code is not target:
            return None
        if event == "line":
            seen.add(frame.f_lineno)
        return _trace

    previous = sys.gettrace()
    sys.settrace(_trace)
    try:
        call()
    finally:
        sys.settrace(previous)
    return seen


def test_the_unapplied_action_branch_never_executes_for_an_action_free_ledger() -> None:
    """D38 invariant 1, proved structurally rather than by equality of results.

    "Code that does not execute cannot drift; code that computes an equal answer can." So
    this does not assert that the new gate produces the same numbers — it asserts the gate's
    BODY is never reached at all when the ledger carries no corporate action, which is the
    only claim strong enough to guarantee identical behaviour for every future input too.

    The probe is self-proving in both directions, so it cannot pass vacuously:
      * the gate's CONDITION line must be executed (else the tracer is watching nothing);
      * the gate's BODY line must NOT be, for an action-free ledger;
      * and the SAME probe must see that body line when an action IS refused.
    """
    txs = [_tx(date(2026, 6, 1), Side.BUY, "10", "100", fees="0")]
    prices = {"AAA": [(date(2026, 6, 1), Decimal("100"))]}
    fx = {(USD, TWD): [(date(2026, 6, 1), Decimal("30"))]}
    clean = LedgerBundle(txs, instruments=INSTRUMENTS)
    refused = LedgerBundle(
        txs, instruments=INSTRUMENTS,
        actions=[CorporateAction(account_id="moomoo_my", date=date(2026, 6, 1),
                                 kind=CorporateActionKind.SPLIT,
                                 from_symbol="AAA", to_symbol="AAA",
                                 ratio_to=Decimal("2"), ratio_from=Decimal("1"))],
    )
    end = date(2026, 6, 2)
    body, condition = _line_of(_GATE_BODY_MARKER), _line_of(_GATE_TEST_MARKER)

    executed = _lines_executed(
        lambda: daily_value_series(clean, prices, fx, TWD, end=end))
    assert condition in executed, "the tracer never reached the gate — probe is broken"
    assert body not in executed, (
        "the unapplied-action branch RAN for a ledger with no corporate action — "
        "containment is broken (D38 invariant 1)")

    detonated = _lines_executed(
        lambda: daily_value_series(refused, prices, fx, TWD, end=end))
    assert body in detonated, (
        "the probe cannot see the branch even when it must run — it proves nothing")
