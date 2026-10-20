"""The dashboard through the result cache: a hit is the v0.1.31 answer, re-stamped per request.

PERF spec 2026-10-18 §12.1, plan W1-C. The cache keys on the DAY, so an entry computed at
09:00 answers a request at 23:59 — which is right only if every field that depends on
``now`` beyond its date is set per request on the reader's copy. The PRODUCTION list,
``portfolio.dashboard.SUB_DAY_FIELDS``, enumerates them, and the first test is the
enumeration's proof through the production ``build_dashboard``: on all five corpora, a hit at
a second clock the same day is EXACTLY (``canon``: Decimal exponents and ``exclude=True``
fields included) a full build at that clock, in both directions. A new field that reads the
hour — or a field dropped from the list — fails it.

The second test is the byte-identity invariant (plan §0.1): ``GET /api/dashboard`` answered
from a cache hit is byte for byte the reference payload v0.1.31's own code wrote. The third
pins the reporting currency in the key: a TWD build is never served to a USD request.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

import pytest

from portfolio_dash.portfolio import dashboard
from portfolio_dash.portfolio.dashboard import SUB_DAY_FIELDS, build_dashboard
from portfolio_dash.shared import result_cache
from portfolio_dash.shared.config import get_settings
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

CorpusFactory = Callable[[str], CorpusDB]


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    result_cache.reset()
    yield
    result_cache.reset()


@pytest.fixture
def corpus(tmp_path_factory: pytest.TempPathFactory, tmp_path: Path,
           monkeypatch: pytest.MonkeyPatch) -> Iterator[CorpusFactory]:
    """A private copy of a corpus, made the application's database (``DB_PATH``): the result
    cache serves only that file."""
    templates = tmp_path_factory.getbasetemp() / TEMPLATE_DIR_NAME

    def make(name: str) -> CorpusDB:
        db = copy_corpus(name, templates, tmp_path / f"{name}.db")
        monkeypatch.setenv("DB_PATH", str(db.path))
        get_settings.cache_clear()
        return db

    try:
        yield make
    finally:
        get_settings.cache_clear()


def _clocks(day_of: datetime) -> tuple[datetime, datetime]:
    d = day_of.astimezone(TAIPEI).date()
    return (datetime(d.year, d.month, d.day, 0, 0, 1, tzinfo=TAIPEI),
            datetime(d.year, d.month, d.day, 23, 59, 59, 999999, tzinfo=TAIPEI))


def test_the_production_list_names_as_of() -> None:
    assert "as_of" in SUB_DAY_FIELDS


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
            build_dashboard(conn, now=first, reporting=db.reporting)
            hit = build_dashboard(conn, now=second, reporting=db.reporting)
            assert result_cache.stats()["dashboard"].hits == 1, "the second clock missed"
            full = dashboard._build_dashboard(fresh, now=second, reporting=db.reporting)
        finally:
            conn.close()
            fresh.close()
        where = first_difference(canon(full), canon(hit))
        assert where is None, (
            f"{name}: a cache hit at {second:%H:%M:%S} differs from a full build at that clock "
            "— a field depends on `now` beyond its date; add it to "
            f"portfolio.dashboard.SUB_DAY_FIELDS ({where})")


@pytest.mark.enable_socket
@pytest.mark.parametrize("name", CORPUS_NAMES)
def test_dashboard_served_from_a_hit_is_the_v0131_reference(
    name: str, corpus: CorpusFactory,
) -> None:
    db: CorpusDB = corpus(name)
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


def test_a_build_in_one_reporting_currency_is_never_served_in_another(
    corpus: CorpusFactory,
) -> None:
    """``usd_reporting`` reads in USD and in TWD alike; the second currency must be a build of
    its own, exactly the uncached one (the reporting currency is part of the key)."""
    db: CorpusDB = corpus("usd_reporting")
    conn = open_db(db.path)
    fresh = open_db(db.path)
    try:
        twd = build_dashboard(conn, now=db.now, reporting=Currency.TWD)
        usd = build_dashboard(conn, now=db.now, reporting=Currency.USD)
        assert (twd.reporting_currency, usd.reporting_currency) == (Currency.TWD, Currency.USD)
        assert result_cache.stats()["dashboard"].misses == 2
        full = dashboard._build_dashboard(fresh, now=db.now, reporting=Currency.USD)
    finally:
        conn.close()
        fresh.close()
    assert first_difference(canon(full), canon(usd)) is None
