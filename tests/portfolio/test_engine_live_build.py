"""The live dashboard build on the new engine (PERF plan §5.1, "The live build uses the new
engine" and the SQL-count ceiling of "Light reads and bounded loads").

* **Replays:** one build replays the whole ledger ONCE — one ``iter_books`` pass yields the
  trend's books and then today's (post-v0.1.32 item 2, ``dashboard._replay_once``); before,
  today's book was a second whole replay (``build_book``). Past its bounds (more trend books,
  or more held entries, than it holds) a build keeps those two replays, and its payload.
  Measured before PERF: one replay per event day (461 on the prod-shaped ledger) plus one for
  the trend's refusals.
* **FX queries:** every FX read of a build goes through one cache, so ``fx_rates`` is asked
  at most once per directed pair — at most twice per pair read (measured before: 1,059
  queries per build).

Counted by code object (``sys.setprofile``) and by SQL trace, on every corpus, so an import
alias or a new call site cannot hide a replay or a query.
"""

from __future__ import annotations

import gc
import re
import sqlite3
import sys
import weakref
from collections import Counter
from collections.abc import Iterator
from datetime import date
from types import FrameType
from typing import Any

import pytest

from portfolio_dash.data_ingestion.store import load_ledger_bundle
from portfolio_dash.portfolio import cost_basis, dashboard
from portfolio_dash.portfolio.cost_basis import UnbookableLedgerError
from portfolio_dash.portfolio.dashboard import _build_dashboard
from portfolio_dash.portfolio.results import Book
from portfolio_dash.portfolio.timeseries import trend_book_days, trend_start, value_series
from portfolio_dash.shared.models.ledger import LedgerBundle
from tests.reference.corpora import CORPORA, CORPUS_NAMES, TEMPLATE_DIR_NAME, copy_corpus, open_db


@pytest.fixture(params=CORPUS_NAMES)
def corpus(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
           ) -> Iterator[tuple[str, sqlite3.Connection]]:
    name: str = request.param
    base = tmp_path_factory.getbasetemp() / TEMPLATE_DIR_NAME
    db = copy_corpus(name, base, tmp_path_factory.mktemp(f"live-{name}") / f"{name}.db")
    conn = open_db(db.path)
    try:
        yield name, conn
    finally:
        conn.close()


def _build(name: str, conn: sqlite3.Connection) -> Any:
    corpus = CORPORA[name]
    return _build_dashboard(conn, now=corpus.now, reporting=corpus.reporting)


def _replays(name: str, conn: sqlite3.Connection) -> tuple[Counter[str], str]:
    """The whole-ledger replays one build starts, by code object, and its payload."""
    codes = {cost_basis.build_book.__code__: "build_book",
             cost_basis.iter_books.__code__: "iter_books"}
    frames: list[FrameType] = []
    replays: Counter[str] = Counter()

    def profile(frame: FrameType, event: str, _arg: object) -> None:
        if event == "call" and frame.f_code in codes:
            if not any(f is frame for f in frames):  # a generator resumes the same frame
                frames.append(frame)
                replays[codes[frame.f_code]] += 1

    sys.setprofile(profile)
    try:
        data = _build(name, conn)
    finally:
        sys.setprofile(None)
    return replays, data.model_dump_json()


def _trend_days(name: str, conn: sqlite3.Connection) -> tuple[LedgerBundle, list[date]]:
    """The valuation cut and the trend's book days of a build with every day live
    (``PD_SETTLE=off``): what ``_build_dashboard`` hands ``_replay_once``."""
    as_of = CORPORA[name].now.date()
    bundle = load_ledger_bundle(conn)
    if bundle.unregistered_symbols:
        bundle = bundle.without_unregistered()
    cut = bundle.valued_as_of(as_of)
    first = trend_start(cut)
    return cut, [] if first is None else trend_book_days(cut, start=first, end=as_of)


def _fits_one_replay(cut: LedgerBundle, days: list[date]) -> bool:
    """The oracle of ``_replay_once``'s two bounds, restated: at most
    ``_ONE_REPLAY_MAX_BOOKS`` trend books, and the held entries (holdings + opened positions)
    projected from the books read so far never past ``_ONE_REPLAY_HELD_ENTRIES``."""
    if len(days) > dashboard._ONE_REPLAY_MAX_BOOKS:
        return False
    entries = 0
    for i, book in enumerate(cost_basis.iter_books(cut, days)):
        remaining = len(days) - (i + 1)
        size = len(book.holdings) + len(book.opened_positions)
        entries += size
        if remaining and entries + size * remaining > dashboard._ONE_REPLAY_HELD_ENTRIES:
            return False
    return True


def test_one_build_replays_the_ledger_once(corpus: tuple[str, sqlite3.Connection],
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """One replay while the trend's books fit ``_replay_once``'s bounds; the documented
    fallback — today's book replayed apart, two replays — past them (a corpus that grows past
    64 event days takes it). Every corpus fits today; golden must, or this pins nothing."""
    name, conn = corpus
    monkeypatch.setenv("PD_SETTLE", "off")
    cut, days = _trend_days(name, conn)
    fits = _fits_one_replay(cut, days)
    assert fits or name != "golden", "golden no longer fits: the one-replay path is untested"
    replays, _payload = _replays(name, conn)
    expected = {"iter_books": 1} if fits else {"build_book": 1, "iter_books": 1}
    assert dict(replays) == expected, (name, len(days))


@pytest.mark.parametrize("bound", ["_ONE_REPLAY_MAX_BOOKS", "_ONE_REPLAY_HELD_ENTRIES"])
def test_past_a_bound_a_build_keeps_its_two_replays_and_its_payload(
    corpus: tuple[str, sqlite3.Connection], bound: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A trend with more books than ``_replay_once`` holds streams them, and today's book is
    replayed apart, as before item 2 — the same payload, byte for byte. With no book allowed
    the stream starts at once; with no entry allowed it starts after the first book held
    (the rest of the SAME replay is read lazily: still two replays, never three)."""
    name, conn = corpus
    _once, held = _replays(name, conn)
    monkeypatch.setattr(dashboard, bound, 0)
    replays, streamed = _replays(name, conn)
    assert streamed == held, name
    assert dict(replays) == {"build_book": 1, "iter_books": 1}, name


@pytest.fixture
def golden_cut(tmp_path_factory: pytest.TempPathFactory
               ) -> tuple[LedgerBundle, list[date], date]:
    """Golden's valuation cut, its trend's book days (every day live) and its day."""
    base = tmp_path_factory.getbasetemp() / TEMPLATE_DIR_NAME
    db = copy_corpus("golden", base, tmp_path_factory.mktemp("held") / "golden.db")
    conn = open_db(db.path)
    try:
        cut, days = _trend_days("golden", conn)
    finally:
        conn.close()
    assert len(days) >= 3
    return cut, days, db.now.date()


def test_the_trends_books_are_released_once_it_has_read_them(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of item 2: the trend's held books were a list the build kept until it
    returned — through the cash series, the allocation and the dividends of every concurrent
    miss build, ~21 MB near the bound. Each is now released as the trend reads it, and the
    iterator itself once the trend returned: by the next step of the build, no book the
    trend read is alive."""
    base = tmp_path_factory.getbasetemp() / TEMPLATE_DIR_NAME
    db = copy_corpus("doubled_demo", base, tmp_path_factory.mktemp("rel") / "dd.db")
    monkeypatch.setenv("PD_SETTLE", "off")
    read: list[weakref.ref[Book]] = []
    real_series = value_series

    def watching(*a: Any, books: Iterator[tuple[date, Book]], **kw: Any) -> Any:
        def watched() -> Iterator[tuple[date, Book]]:
            for day, book in books:
                read.append(weakref.ref(book))
                yield day, book

        return real_series(*a, books=watched(), **kw)

    alive: list[int] = []
    real_state = dashboard._history_state

    def counting(*a: Any, **kw: Any) -> Any:   # the build's next step after the trend
        gc.collect()
        alive.append(sum(1 for ref in read if ref() is not None))
        return real_state(*a, **kw)

    monkeypatch.setattr(dashboard, "value_series", watching)
    monkeypatch.setattr(dashboard, "_history_state", counting)
    conn = open_db(db.path)
    try:
        _build("doubled_demo", conn)
    finally:
        conn.close()
    assert len(read) >= 10            # the trend read its books from the one replay
    assert alive == [0]


def _fake_replay(real_books: Any, calls: list[list[date]], *,
                 fail_first: BaseException | type[BaseException],
                 fail_again: BaseException | type[BaseException] | None, after: int,
                 yielded: list[weakref.ref[Book]]) -> Any:
    """An ``iter_books`` whose FIRST call yields *after* real books and raises *fail_first*;
    a later call does the same with *fail_again*, or replays for real when it is ``None``."""
    def fake(bundle: LedgerBundle, asked: list[date]) -> Iterator[Book]:
        calls.append(list(asked))
        failure = fail_first if len(calls) == 1 else fail_again
        books = real_books(bundle, asked)
        if failure is None:
            yield from books
            return
        for _ in range(after):
            book = next(books)
            yielded.append(weakref.ref(book))
            yield book
        raise failure

    return fake


def test_a_ledger_error_while_held_falls_back_to_the_two_replays(
    golden_cut: tuple[LedgerBundle, list[date], date], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replay that raises while its books are held drops them and takes the two-replay
    path from scratch: today's book is left to ``kpis_as_of`` (which replays and raises what
    it raised before), and the trend's own replay starts only when the trend asks — so the
    error is raised again on the same day, in the same sentence, as before item 2."""
    cut, days, as_of = golden_cut
    boom = UnbookableLedgerError("第二天的持倉無法組成")
    calls: list[list[date]] = []
    yielded: list[weakref.ref[Book]] = []
    monkeypatch.setattr(dashboard, "iter_books", _fake_replay(
        cost_basis.iter_books, calls, fail_first=boom, fail_again=boom, after=1,
        yielded=yielded))
    today, trend = dashboard._replay_once(cut, days, as_of)
    assert today is None
    assert calls == [[*days, as_of]]               # the trend's replay has not started yet
    seen: list[date] = []
    with pytest.raises(UnbookableLedgerError) as raised:
        for day, _book in trend:
            seen.append(day)
    assert raised.value is boom
    assert seen == days[:1]
    assert calls == [[*days, as_of], days]


def test_a_memory_error_while_held_sheds_the_hold(
    golden_cut: tuple[LedgerBundle, list[date], date], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of item 2: a ``MemoryError`` — which the hold itself may cause — must shed the
    hold, not keep it alive while ``kpis_as_of`` replays again. The books held when it struck
    are released at once, and the trend's own replay (which holds at most two) gives the
    build the chance it had before item 2."""
    cut, days, as_of = golden_cut
    calls: list[list[date]] = []
    yielded: list[weakref.ref[Book]] = []
    monkeypatch.setattr(dashboard, "iter_books", _fake_replay(
        cost_basis.iter_books, calls, fail_first=MemoryError, fail_again=None, after=2,
        yielded=yielded))
    today, trend = dashboard._replay_once(cut, days, as_of)
    gc.collect()
    assert today is None and len(yielded) == 2
    assert [ref() for ref in yielded] == [None, None]      # the hold is shed
    assert [day for day, _book in trend] == days            # the trend's own replay
    assert calls == [[*days, as_of], days]


_PAIR = re.compile(r"base='(\w+)' AND quote='(\w+)'")


def test_one_build_asks_fx_rates_once_per_directed_pair(corpus: tuple[str, sqlite3.Connection]
                                                        ) -> None:
    name, conn = corpus
    queries: list[str] = []
    conn.set_trace_callback(queries.append)
    try:
        _build(name, conn)
    finally:
        conn.set_trace_callback(None)
    fx = [q for q in queries if "fx_rates" in q]
    assert fx, f"{name}: the build read no FX at all"
    pairs: Counter[tuple[str, str]] = Counter()
    for q in fx:
        found = _PAIR.search(q)
        assert found is not None, f"an fx_rates query not keyed by its pair: {q[:120]}"
        pairs[(found[1], found[2])] += 1
    assert all(n == 1 for n in pairs.values()), (name, dict(pairs))
    unordered = Counter(frozenset(p) for p in pairs.elements())
    assert all(n <= 2 for n in unordered.values()), (name, dict(unordered))
