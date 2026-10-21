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

import re
import sqlite3
import sys
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
from portfolio_dash.portfolio.timeseries import trend_book_days, trend_start
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


def test_one_build_replays_the_ledger_once(corpus: tuple[str, sqlite3.Connection]) -> None:
    name, conn = corpus
    replays, _payload = _replays(name, conn)
    assert dict(replays) == {"iter_books": 1}, name


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


def test_a_replay_failing_while_held_raises_where_the_trends_replay_raised(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 2 keeps the order of errors: when the one replay raises while its books are held,
    today's book is left to ``kpis_as_of`` (which replays and raises what it raised before),
    and the trend gets the books held so far and THEN the same exception — raised when it
    asks for the book of the day that failed, as its own replay raised it before."""
    base = tmp_path_factory.getbasetemp() / TEMPLATE_DIR_NAME
    db = copy_corpus("golden", base, tmp_path_factory.mktemp("held-fail") / "golden.db")
    conn = open_db(db.path)
    try:
        bundle = load_ledger_bundle(conn)
    finally:
        conn.close()
    as_of = db.now.date()
    cut = bundle.valued_as_of(as_of)
    first = trend_start(cut)
    assert first is not None
    days = trend_book_days(cut, start=first, end=as_of)
    assert len(days) >= 2
    boom = UnbookableLedgerError("第二天的持倉無法組成")
    real = cost_basis.iter_books

    def fails_on_the_second_day(bundle: LedgerBundle, asked: list[date]) -> Iterator[Book]:
        assert asked == [*days, as_of]
        books = real(bundle, asked)
        yield next(books)
        raise boom

    monkeypatch.setattr(dashboard, "iter_books", fails_on_the_second_day)
    today, trend = dashboard._replay_once(cut, days, as_of)
    assert today is None
    seen: list[date] = []
    with pytest.raises(UnbookableLedgerError) as raised:
        for day, _book in trend:
            seen.append(day)
    assert raised.value is boom
    assert seen == days[:1]


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
