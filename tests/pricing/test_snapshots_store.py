"""Tests for the append-only external_snapshots store (spec 20.4).

Hermetic: an in-memory sqlite connection with row_factory = Row; no network.
Covers idempotent DDL, append-only writes (latest fetched_at wins), latest-N
series ordering, and missing-key degradation.
"""

import sqlite3
from collections.abc import Iterator
from datetime import date, datetime

import pytest

from portfolio_dash.pricing import snapshots_store as S


@pytest.fixture
def tmp_conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def test_ensure_tables_idempotent(tmp_conn: sqlite3.Connection) -> None:
    S.ensure_tables(tmp_conn)
    S.ensure_tables(tmp_conn)  # second call must not raise
    rows = tmp_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='external_snapshots'"
    ).fetchall()
    assert len(rows) == 1


def test_add_and_latest(tmp_conn: sqlite3.Connection) -> None:
    S.ensure_tables(tmp_conn)
    S.add_snapshot(
        tmp_conn, source="finmind", dataset="institutional", symbol="2330",
        as_of=date(2026, 6, 11), payload={"net": "1200"},
        fetched_at=datetime(2026, 6, 11, 18, 0),
    )
    got = S.latest_snapshot(tmp_conn, source="finmind", dataset="institutional", symbol="2330")
    assert got is not None
    assert got.payload == {"net": "1200"}
    assert got.as_of == date(2026, 6, 11)
    assert got.symbol == "2330"
    # A different dataset key has no row.
    assert (
        S.latest_snapshot(tmp_conn, source="finmind", dataset="margin", symbol="2330") is None
    )


def test_append_only_latest_fetched_at_wins(tmp_conn: sqlite3.Connection) -> None:
    S.ensure_tables(tmp_conn)
    # Two writes for the SAME (source, dataset, symbol, as_of): both rows persist;
    # the newer fetched_at wins on read.
    S.add_snapshot(
        tmp_conn, source="finmind", dataset="institutional", symbol="2330",
        as_of=date(2026, 6, 11), payload={"net": "100"},
        fetched_at=datetime(2026, 6, 11, 18, 0),
    )
    S.add_snapshot(
        tmp_conn, source="finmind", dataset="institutional", symbol="2330",
        as_of=date(2026, 6, 11), payload={"net": "200"},
        fetched_at=datetime(2026, 6, 11, 20, 0),
    )
    count = tmp_conn.execute(
        "SELECT COUNT(*) FROM external_snapshots WHERE dataset='institutional'"
    ).fetchone()[0]
    assert count == 2  # append-only: nothing overwritten
    got = S.latest_snapshot(tmp_conn, source="finmind", dataset="institutional", symbol="2330")
    assert got is not None and got.payload == {"net": "200"}


def test_latest_snapshot_symbol_none(tmp_conn: sqlite3.Connection) -> None:
    S.ensure_tables(tmp_conn)
    S.add_snapshot(
        tmp_conn, source="sentiment", dataset="vix", symbol=None,
        as_of=date(2026, 6, 11), payload={"close": "14.2"},
        fetched_at=datetime(2026, 6, 11, 18, 0),
    )
    got = S.latest_snapshot(tmp_conn, source="sentiment", dataset="vix", symbol=None)
    assert got is not None and got.symbol is None and got.payload == {"close": "14.2"}
    # A symbol-keyed lookup must NOT match the symbol-NULL row.
    assert (
        S.latest_snapshot(tmp_conn, source="sentiment", dataset="vix", symbol="X") is None
    )


def test_latest_series_orders_by_as_of_desc(tmp_conn: sqlite3.Connection) -> None:
    S.ensure_tables(tmp_conn)
    for d, net in [(9, "10"), (10, "20"), (11, "30")]:
        S.add_snapshot(
            tmp_conn, source="finmind", dataset="institutional", symbol="2330",
            as_of=date(2026, 6, d), payload={"net": net},
            fetched_at=datetime(2026, 6, d, 18, 0),
        )
    series = S.latest_series(
        tmp_conn, source="finmind", dataset="institutional", symbol="2330", n=2
    )
    assert [s.as_of for s in series] == [date(2026, 6, 11), date(2026, 6, 10)]
    assert [s.payload["net"] for s in series] == ["30", "20"]


def test_latest_series_one_row_per_as_of_newest_fetch(tmp_conn: sqlite3.Connection) -> None:
    S.ensure_tables(tmp_conn)
    # Same as_of fetched twice -> series collapses to the newest fetch for that as_of.
    S.add_snapshot(
        tmp_conn, source="finmind", dataset="margin", symbol="2330",
        as_of=date(2026, 6, 11), payload={"bal": "1"},
        fetched_at=datetime(2026, 6, 11, 18, 0),
    )
    S.add_snapshot(
        tmp_conn, source="finmind", dataset="margin", symbol="2330",
        as_of=date(2026, 6, 11), payload={"bal": "2"},
        fetched_at=datetime(2026, 6, 11, 20, 0),
    )
    series = S.latest_series(
        tmp_conn, source="finmind", dataset="margin", symbol="2330", n=5
    )
    assert len(series) == 1
    assert series[0].payload["bal"] == "2"


def test_missing_returns_empty(tmp_conn: sqlite3.Connection) -> None:
    S.ensure_tables(tmp_conn)
    assert (
        S.latest_snapshot(tmp_conn, source="finmind", dataset="institutional", symbol="9999")
        is None
    )
    assert (
        S.latest_series(tmp_conn, source="finmind", dataset="institutional", symbol="9999", n=5)
        == []
    )
