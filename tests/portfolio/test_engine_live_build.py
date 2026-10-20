"""The live dashboard build on the new engine (PERF plan §5.1, "The live build uses the new
engine" and the SQL-count ceiling of "Light reads and bounded loads").

* **Replays:** one build replays the whole ledger at most TWICE — the book (``build_book``)
  and the trend's one ``iter_books`` pass. Measured before: one replay per event day (461 on
  the prod-shaped ledger) plus one for the trend's refusals.
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
from types import FrameType
from typing import Any

import pytest

from portfolio_dash.portfolio import cost_basis
from portfolio_dash.portfolio.dashboard import _build_dashboard
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


def test_one_build_replays_the_ledger_at_most_twice(corpus: tuple[str, sqlite3.Connection]
                                                    ) -> None:
    name, conn = corpus
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
        _build(name, conn)
    finally:
        sys.setprofile(None)
    assert sum(replays.values()) <= 2, (name, dict(replays))
    assert replays["build_book"] == 1, (name, dict(replays))


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
