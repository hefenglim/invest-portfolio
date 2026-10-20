"""``cost_basis.iter_books`` — one replay, the book of every requested day (PERF spec §8.1).

The obligation (spec §8 item 6, plan §5.1): for every corpus and every event day ``d``,
``iter_books`` yields exactly ``build_book(bundle.through(d), allow_oversell=True)`` as
v0.1.31 computed it — compared with the FROZEN v0.1.31 engine (``tests/reference``) by
:func:`tests.reference.canon.canon`, so a Decimal exponent or a field order counts.

Three kinds of pin:

* **the corpora** — the five reference ledgers, every event day;
* **crafted prologue ledgers** — the two shapes no corpus has (plan §5.1): a position opened
  AFTER an unreadable action row's date (final pass #8), and two unreadable rows out of date
  order in the list with a loop refusal dated between them;
* **the code itself** — ``iter_books`` carries ``build_book``'s event handling and holdings
  assembly statement for statement; the AST comparisons below fail on an edit to one that
  is not made to the other (``build_book`` stays the reference, plan P15).
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from collections.abc import Callable, Iterator
from datetime import date, timedelta
from decimal import Decimal
from functools import partial
from typing import Any

import pytest

from portfolio_dash.data_ingestion.store import load_ledger_bundle
from portfolio_dash.portfolio import cost_basis
from portfolio_dash.portfolio.cost_basis import iter_books
from portfolio_dash.shared.corporate_actions import (
    CorporateAction,
    CorporateActionKind,
    UnreadableAction,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import (
    Dividend,
    LedgerBundle,
    OpeningInventory,
    Transaction,
)
from tests.reference.canon import canon, first_difference
from tests.reference.corpora import CORPUS_NAMES, TEMPLATE_DIR_NAME, copy_corpus, open_db
from tests.reference.cost_basis_v0131 import ref_build_book
from tests.reference.dashboard_inputs import load_inputs

D = Decimal
USD, TWD = Currency.USD, Currency.TWD

INSTRUMENTS = {
    "AAA": Instrument(symbol="AAA", market=Market.US, quote_ccy=USD, sector="Tech",
                      name="AAA Corp"),
    "BBB": Instrument(symbol="BBB", market=Market.TW, quote_ccy=TWD, sector="Semis",
                      name="BBB Corp", board="TWSE"),
    "CCC": Instrument(symbol="CCC", market=Market.US, quote_ccy=USD, sector="Tech",
                      name="CCC Corp"),
}


# --- helpers -----------------------------------------------------------------------------


def _outcome(fn: Callable[[], Any]) -> Any:
    """``canon`` of a book, or of the refusal (class NAME + message: the frozen engine
    defines its own exception classes)."""
    try:
        return canon(fn())
    except Exception as exc:  # noqa: BLE001 - the refusal IS the outcome under comparison
        return ("raised", type(exc).__name__, str(exc))


def _reference(bundle: LedgerBundle, days: list[date]) -> list[Any]:
    def one(d: date) -> Any:
        return ref_build_book(bundle.through(d), allow_oversell=True)

    return [_outcome(partial(one, d)) for d in days]


def _engine(bundle: LedgerBundle, days: list[date]) -> list[Any]:
    """``iter_books``' outcome per day: its books, then the refusal it ended on (if any)."""
    out: list[Any] = []
    it = iter_books(bundle, days)
    while len(out) < len(days):
        try:
            out.append(canon(next(it)))
        except StopIteration:
            break
        except Exception as exc:  # noqa: BLE001 - compared like the reference's refusal
            out.append(("raised", type(exc).__name__, str(exc)))
            break
    return out


def _assert_equal_per_day(label: str, bundle: LedgerBundle, days: list[date]) -> None:
    expected = _reference(bundle, days)
    got = _engine(bundle, days)
    # The engine ends on the first refusal; the reference keeps refusing on every later day
    # (a prefix that holds a bad row holds it for good) — compare up to that day.
    assert len(got) <= len(expected)
    for d, e, g in zip(days, expected, got, strict=False):
        assert e == g, f"{label} {d}: {first_difference(e, g)}"
    if len(got) < len(expected):
        assert got and got[-1][0] == "raised", f"{label}: the engine stopped early"


def _every_day(first: date, last: date) -> list[date]:
    return [first + timedelta(days=n) for n in range((last - first).days + 1)]


# --- the corpora ---------------------------------------------------------------------------


@pytest.fixture(scope="module", params=CORPUS_NAMES)
def corpus(request: pytest.FixtureRequest,
           tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Any, Any]]:
    name: str = request.param
    base = tmp_path_factory.getbasetemp() / TEMPLATE_DIR_NAME
    db = copy_corpus(name, base, tmp_path_factory.mktemp(f"iter-{name}") / f"{name}.db")
    conn = open_db(db.path)
    try:
        uncut = load_ledger_bundle(conn).without_unregistered()
        yield name, load_inputs(conn, db.now, db.reporting), uncut
    finally:
        conn.close()


def test_every_event_day_equals_the_prefix_replay(corpus: tuple[str, Any, Any]) -> None:
    name, inputs, _uncut = corpus
    days = sorted(inputs.bundle.counts_on_dates())
    assert days, f"{name}: a corpus with no event day pins nothing"
    _assert_equal_per_day(name, inputs.bundle, days)


def test_the_uncut_ledger_and_non_event_days_too(corpus: tuple[str, Any, Any]) -> None:
    """The whole stored ledger (future rows included), on every calendar day from one week
    before the first event to one week after the last — non-event days, days before any
    event and days after the last one all yield the prefix replay."""
    name, _inputs, uncut = corpus
    events = sorted(uncut.counts_on_dates())
    days = _every_day(events[0] - timedelta(days=7), events[-1] + timedelta(days=7))
    _assert_equal_per_day(f"{name} uncut", uncut, days)


# --- crafted prologue ledgers ------------------------------------------------------------------


def _tx(day: date, side: Side, qty: str, price: str, symbol: str = "AAA",
        account: str = "schwab") -> Transaction:
    return Transaction(account_id=account, symbol=symbol, side=side, quantity=D(qty),
                       price=D(price), fees=D("1"), tax=D("0"), trade_date=day)


def _unreadable(day: date, symbol: str, *, to: str | None = None,
                account: str = "schwab", ident: int = 1) -> UnreadableAction:
    return UnreadableAction(account_id=account, date=day, kind="SPLIT", from_symbol=symbol,
                            to_symbol=to or symbol, reason=f"unreadable row #{ident}",
                            id=ident)


def test_a_position_opened_after_an_unreadable_row_is_flagged_from_its_first_day() -> None:
    """Final pass #8: build_book's epilogue flags every position an unreadable row names
    AT THE END of the replay — including one first bought after the row's date. An in-loop
    flag set on the row's date would miss it; the per-day copy does not."""
    bundle = LedgerBundle(
        transactions=[_tx(date(2026, 3, 2), Side.BUY, "10", "100", symbol="BBB",
                          account="tw_broker"),
                      _tx(date(2026, 3, 9), Side.BUY, "10", "100"),
                      _tx(date(2026, 3, 16), Side.BUY, "5", "104"),
                      _tx(date(2026, 3, 23), Side.SELL, "3", "110")],
        instruments=INSTRUMENTS,
        unreadable_actions=[_unreadable(date(2026, 3, 5), "AAA")],
    )
    days = _every_day(date(2026, 3, 1), date(2026, 3, 31))
    _assert_equal_per_day("opened after", bundle, days)
    # …and the shape really is the one named: flagged from the buy on, a ledger later.
    book = ref_build_book(bundle.through(date(2026, 3, 9)), allow_oversell=True)
    assert [h.unbookable_action for h in book.holdings if h.symbol == "AAA"] == [True]
    assert [h.unbookable_action for h in book.holdings if h.symbol == "BBB"] == [False]


def test_unreadable_rows_out_of_date_order_around_a_loop_refusal() -> None:
    """Two unreadable rows listed LATER-dated first, and a readable action the loop refuses
    dated between them: each day's ``unapplied_actions`` is the unreadable rows dated by
    that day in LIST order, then the loop's refusals — as the prefix bundle records them."""
    refused = CorporateAction(  # a SPLIT on a symbol never held → E1, recorded by the loop
        account_id="schwab", date=date(2026, 3, 12), kind=CorporateActionKind.SPLIT,
        from_symbol="CCC", to_symbol="CCC", ratio_to=D("2"), ratio_from=D("1"), id=7)
    bundle = LedgerBundle(
        transactions=[_tx(date(2026, 3, 2), Side.BUY, "10", "100"),
                      _tx(date(2026, 3, 20), Side.BUY, "4", "51", symbol="CCC")],
        instruments=INSTRUMENTS,
        actions=[refused],
        unreadable_actions=[_unreadable(date(2026, 3, 18), "AAA", ident=2),
                            _unreadable(date(2026, 3, 6), "AAA", ident=1)],
    )
    days = _every_day(date(2026, 3, 1), date(2026, 3, 25))
    _assert_equal_per_day("out of order", bundle, days)
    last = ref_build_book(bundle.through(date(2026, 3, 25)), allow_oversell=True)
    assert [u.action_id for u in last.unapplied_actions] == [2, 1, 7]
    mid = ref_build_book(bundle.through(date(2026, 3, 12)), allow_oversell=True)
    assert [u.action_id for u in mid.unapplied_actions] == [1, 7]


def test_the_epilogue_flag_never_travels_through_a_later_action() -> None:
    """The epilogue's flag is set AFTER the prefix replay, so an EXCHANGE dated after the
    unreadable row does not carry it to the destination (its `|=` runs during the loop, when
    the source is still unflagged). Setting the flag on the live state instead of a copy
    would carry it — this ledger tells the two apart."""
    exchange = CorporateAction(account_id="schwab", date=date(2026, 3, 16),
                               kind=CorporateActionKind.EXCHANGE, from_symbol="AAA",
                               to_symbol="CCC", ratio_to=D("1"), ratio_from=D("2"), id=5)
    bundle = LedgerBundle(
        transactions=[_tx(date(2026, 3, 2), Side.BUY, "10", "100")],
        instruments=INSTRUMENTS,
        actions=[exchange],
        unreadable_actions=[_unreadable(date(2026, 3, 6), "AAA")],
    )
    _assert_equal_per_day("travel", bundle, _every_day(date(2026, 3, 1), date(2026, 3, 20)))
    book = ref_build_book(bundle.through(date(2026, 3, 20)), allow_oversell=True)
    assert [(h.symbol, h.unbookable_action) for h in book.holdings] == [("CCC", False)]


def test_a_rich_ledger_on_every_calendar_day() -> None:
    """Every shape the trend's per-day book takes (the fixture of
    ``test_trend_rebuilds_only_on_event_days.py``, plus an opening, a refused dividend and
    a same-day buy / sell pair), every calendar day."""
    split = CorporateAction(account_id="schwab", date=date(2026, 4, 13),
                            kind=CorporateActionKind.SPLIT, from_symbol="AAA",
                            to_symbol="AAA", ratio_to=D("2"), ratio_from=D("1"))
    bundle = LedgerBundle(
        transactions=[
            _tx(date(2026, 3, 2), Side.BUY, "10", "100"),
            _tx(date(2026, 3, 9), Side.BUY, "5", "104"),
            _tx(date(2026, 3, 9), Side.SELL, "2", "105"),
            _tx(date(2026, 3, 16), Side.SELL, "4", "110"),
            _tx(date(2026, 3, 23), Side.SELL, "30", "50", symbol="CCC"),   # 賣超 (acked)
            _tx(date(2026, 4, 6), Side.BUY, "40", "51", symbol="CCC"),
            _tx(date(2026, 4, 20), Side.BUY, "1000", "60", symbol="BBB", account="tw_broker"),
        ],
        dividends=[
            Dividend(account_id="schwab", symbol="AAA", date=date(2026, 3, 27),
                     type=DividendType.CASH, gross=D("6"), withholding=D("1.8"),
                     net=D("4.2")),
            Dividend(account_id="tw_broker", symbol="BBB", date=date(2026, 4, 28),
                     ex_date=date(2026, 4, 24), type=DividendType.STOCK, gross=D("0"),
                     withholding=D("0"), net=D("0"), reinvest_shares=D("50")),
            Dividend(account_id="schwab", symbol="CCC", date=date(2026, 3, 3),
                     type=DividendType.CASH, gross=D("2"), withholding=D("0.6"),
                     net=D("1.4")),                                          # no position yet
        ],
        opening=[OpeningInventory(account_id="tw_broker", symbol="BBB", shares=D("2000"),
                                  original_cost_total=D("100000"),
                                  build_date=date(2026, 3, 4))],
        instruments=INSTRUMENTS,
        actions=[split],
        unreadable_actions=[_unreadable(date(2026, 4, 1), "CCC", ident=9)],
    )
    _assert_equal_per_day("rich", bundle, _every_day(date(2026, 2, 25), date(2026, 5, 5)))


# --- semantics of `days` -------------------------------------------------------------------


def _small() -> LedgerBundle:
    return LedgerBundle(transactions=[_tx(date(2026, 3, 2), Side.BUY, "10", "100"),
                                      _tx(date(2026, 3, 9), Side.SELL, "4", "110")],
                        instruments=INSTRUMENTS)


def test_a_repeated_day_yields_the_same_book_and_no_day_yields_nothing() -> None:
    days = [date(2026, 3, 2), date(2026, 3, 2), date(2026, 3, 9)]
    _assert_equal_per_day("repeat", _small(), days)
    assert list(iter_books(_small(), [])) == []


def test_days_must_not_decrease() -> None:
    it = iter_books(_small(), [date(2026, 3, 9), date(2026, 3, 2)])
    next(it)
    with pytest.raises(ValueError, match="must not decrease"):
        next(it)


def test_a_yielded_book_is_not_moved_by_later_events() -> None:
    books = list(iter_books(_small(), [date(2026, 3, 2), date(2026, 3, 9)]))
    first = canon(books[0])
    assert first == canon(ref_build_book(_small().through(date(2026, 3, 2)),
                                         allow_oversell=True))
    assert books[0].realized.rows == [] and len(books[1].realized.rows) == 1


def test_an_uncomputable_row_ends_the_iteration_with_the_same_sentence() -> None:
    """``build_book``'s ArithmeticError retype, on the day the prefix replay first meets
    the row — the row is named (a fault while booking it)."""
    bundle = LedgerBundle(
        transactions=[_tx(date(2026, 3, 2), Side.BUY, "10", "100"),
                      _tx(date(2026, 3, 9), Side.BUY, "1E+999999", "2")],
        instruments=INSTRUMENTS)
    days = _every_day(date(2026, 3, 1), date(2026, 3, 12))
    _assert_equal_per_day("uncomputable row", bundle, days)
    got = _engine(bundle, days)
    assert got[-1][0] == "raised" and got[-1][1] == "UnbookableLedgerError"
    assert "AAA" in got[-1][2]


def test_an_uncomputable_assembly_names_no_row() -> None:
    """A fault in a day's holdings assembly (``total / shares`` overflowing) belongs to no
    single row — the message names none, as build_book's does."""
    bundle = LedgerBundle(
        opening=[OpeningInventory(account_id="schwab", symbol="AAA", shares=D("1E-999990"),
                                  original_cost_total=D("9E+999990"),
                                  build_date=date(2026, 3, 2))],
        instruments=INSTRUMENTS)
    days = [date(2026, 3, 1), date(2026, 3, 2), date(2026, 3, 3)]
    _assert_equal_per_day("uncomputable assembly", bundle, days)
    got = _engine(bundle, days)
    assert got[-1][0] == "raised" and "AAA" not in got[-1][2]


# --- the code is build_book's own ----------------------------------------------------------


def _function(name: str) -> ast.FunctionDef:
    tree = ast.parse(textwrap.dedent(inspect.getsource(getattr(cost_basis, name))))
    fn = tree.body[0]
    assert isinstance(fn, ast.FunctionDef)
    return fn


def _try(fn: ast.FunctionDef) -> ast.Try:
    found = [n for n in fn.body if isinstance(n, ast.Try)]
    assert len(found) == 1
    return found[0]


def _dump(stmts: list[ast.stmt]) -> list[str]:
    return [ast.dump(s) for s in stmts]


def _event_list_block(fn: ast.FunctionDef) -> list[ast.stmt]:
    """The statements from ``events: list[...] = []`` to ``events.sort(...)``."""
    start = next(i for i, s in enumerate(fn.body)
                 if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)
                 and s.target.id == "events")
    end = next(i for i, s in enumerate(fn.body)
               if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call)
               and isinstance(s.value.func, ast.Attribute) and s.value.func.attr == "sort")
    return fn.body[start:end + 1]


def test_the_event_handling_is_build_books_own_code() -> None:
    reference = _try(_function("build_book")).body[0]
    assert isinstance(reference, ast.For)
    day_loop = _try(_function("iter_books")).body[0]
    assert isinstance(day_loop, ast.For)
    event_loop = next(s for s in day_loop.body if isinstance(s, ast.While))
    # the while loop's first two statements take the next event; the rest is the body
    assert _dump(event_loop.body[2:]) == _dump(reference.body)


def test_the_event_order_is_build_books_own_code() -> None:
    assert (_dump(_event_list_block(_function("iter_books")))
            == _dump(_event_list_block(_function("build_book"))))


def test_the_quote_currency_lookup_is_build_books_own_code() -> None:
    def nested(fn: ast.FunctionDef) -> str:
        return next(ast.dump(s) for s in fn.body
                    if isinstance(s, ast.FunctionDef) and s.name == "quote_ccy")
    assert nested(_function("iter_books")) == nested(_function("build_book"))


def test_the_book_assembly_is_build_books_own_code() -> None:
    reference = _try(_function("build_book")).body
    start = next(i for i, s in enumerate(reference)
                 if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)
                 and s.target.id == "holdings")
    assembly = _function("_assemble_book").body
    assert isinstance(assembly[0], ast.Expr)  # the docstring
    assert _dump(assembly[1:]) == _dump(reference[start:])
