"""The dashboard through the result cache: a hit is the v0.1.31 answer, re-stamped per request.

PERF spec 2026-10-18 §12.1, plan W1-C. The cache keys on the DAY, so an entry computed at
09:00 answers a request at 23:59 — which is right only if every field that depends on
``now`` beyond its date is set per request on the reader's copy. :data:`SUB_DAY_FIELDS`
enumerates them, and the first test is the enumeration's proof: on all five corpora, a hit
re-stamped at a second clock the same day is EXACTLY (``canon``: Decimal exponents and
``exclude=True`` fields included) a full build at that clock, in both directions. A new
field that reads the hour fails it.

The second test is the byte-identity invariant (plan §0.1): ``GET /api/dashboard`` answered
from a cache hit is byte for byte the reference payload v0.1.31's own code wrote.

``_wire`` is the wiring the main session makes at the wave-1 merge (``build_dashboard``
reads ``result_cache.cached`` in place of ``flight.coalesce``, plan §5.4 step 2) applied
here by patching ``dashboard._build_dashboard``, so the cache is exercised before that
merge. After it, the patch nests a second, uncached layer (a key taken inside the outer
build's read snapshot is ``None``) and the main session drops it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

import pytest

from portfolio_dash.portfolio import dashboard
from portfolio_dash.portfolio.dashboard_models import DashboardData
from portfolio_dash.shared import result_cache
from portfolio_dash.shared.enums import Currency
from tests.reference.canon import canon, first_difference
from tests.reference.corpora import (
    CORPUS_NAMES,
    TAIPEI,
    TEMPLATE_DIR_NAME,
    CorpusDB,
    copy_corpus,
    open_db,
)
from tests.reference.payload_gen import dashboard_body, new_app

_PAYLOADS = Path(__file__).resolve().parents[1] / "reference" / "payloads"

#: Every ``DashboardData`` field that depends on ``now`` beyond its date. The wiring re-stamps
#: exactly these on each reader's copy; the test below proves the list is complete.
SUB_DAY_FIELDS: tuple[str, ...] = ("as_of",)


def restamp(data: DashboardData, now: datetime) -> DashboardData:
    """Set the sub-day fields of a reader's own copy to its clock."""
    for name in SUB_DAY_FIELDS:
        setattr(data, name, now)
    return data


Build = Callable[..., DashboardData]
CorpusFactory = Callable[[str], CorpusDB]


def cached_build(
    conn: sqlite3.Connection, *, now: datetime, reporting: Currency, real: Build | None = None,
) -> DashboardData:
    """``build_dashboard`` as the wave-1 merge wires it (``real`` = the uncached build)."""
    build: Build = real if real is not None else dashboard._build_dashboard

    def compute() -> DashboardData:
        return build(conn, now=now, reporting=reporting)

    def own_copy(data: DashboardData) -> DashboardData:
        return data.model_copy(deep=True)

    def stamp(data: DashboardData) -> DashboardData:
        return restamp(data, now)

    return result_cache.cached(
        conn, name="dashboard", now=now, reporting=reporting, compute=compute,
        copy=own_copy, restamp=stamp,
    )


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    result_cache.reset()
    yield
    result_cache.reset()


@pytest.fixture
def corpus(tmp_path_factory: pytest.TempPathFactory, tmp_path: Path) -> CorpusFactory:
    templates = tmp_path_factory.getbasetemp() / TEMPLATE_DIR_NAME

    def make(name: str) -> CorpusDB:
        return copy_corpus(name, templates, tmp_path / f"{name}.db")

    return make


def _clocks(day_of: datetime) -> tuple[datetime, datetime]:
    d = day_of.astimezone(TAIPEI).date()
    return (datetime(d.year, d.month, d.day, 0, 0, 1, tzinfo=TAIPEI),
            datetime(d.year, d.month, d.day, 23, 59, 59, 999999, tzinfo=TAIPEI))


@pytest.mark.parametrize("name", CORPUS_NAMES)
def test_a_hit_restamped_at_another_clock_is_a_full_build_at_that_clock(
    name: str, corpus: CorpusFactory,
) -> None:
    db: CorpusDB = corpus(name)
    early, late = _clocks(db.now)
    for first, second in ((early, late), (late, early)):
        result_cache.reset()
        conn = open_db(db.path)
        fresh = open_db(db.path)
        try:
            cached_build(conn, now=first, reporting=db.reporting)
            hit = cached_build(conn, now=second, reporting=db.reporting)
            assert result_cache.stats()["dashboard"].hits == 1, "the second clock missed"
            full = dashboard._build_dashboard(fresh, now=second, reporting=db.reporting)
        finally:
            conn.close()
            fresh.close()
        where = first_difference(canon(full), canon(hit))
        assert where is None, (
            f"{name}: a cache hit at {second:%H:%M:%S} differs from a full build at that clock "
            f"— a field depends on `now` beyond its date; add it to SUB_DAY_FIELDS ({where})")


@pytest.mark.enable_socket
@pytest.mark.parametrize("name", CORPUS_NAMES)
def test_dashboard_served_from_a_hit_is_the_v0131_reference(
    name: str, corpus: CorpusFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db: CorpusDB = corpus(name)
    real = dashboard._build_dashboard

    def wired(conn: sqlite3.Connection, *, now: datetime, reporting: Currency
              ) -> DashboardData:
        return cached_build(conn, now=now, reporting=reporting, real=real)

    monkeypatch.setattr(dashboard, "_build_dashboard", wired)
    expected = (_PAYLOADS / name / "dashboard.json").read_bytes()
    app = new_app()
    bodies: list[bytes] = []
    # The first GET on an unbooted corpus may seed a table (a commit: the next GET misses),
    # so read until a request is answered from a hit; every answer must be the reference.
    for _ in range(4):
        bodies.append(dashboard_body(app, db.path, db.now, db.reporting))
        if result_cache.stats()["dashboard"].hits:
            break
    assert result_cache.stats()["dashboard"].hits >= 1, "no request was answered from a hit"
    for i, body in enumerate(bodies):
        assert body == expected, f"{name}: response {i} differs from the v0.1.31 reference"
