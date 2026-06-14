"""Tests for per-source token-tier marking in the data-source catalog (spec 20.15.2).

``data_sources`` gains a nullable ``tier`` column (migrated idempotently — an older
DB without the column still opens). ``set_tier``/``SourceState.tier`` round-trip the
marked tier; ``SourceInfo.tiers`` lists the selectable options per source;
``TIER_ORDER`` ranks them. No network.
"""

import sqlite3
from collections.abc import Iterator

import pytest

from portfolio_dash.pricing import datasources_store as store


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    store.create_tables(c)
    store.seed(c)
    yield c
    c.close()


def test_tier_order_constant() -> None:
    assert store.TIER_ORDER == {"free": 0, "backer": 1, "sponsor": 2, "sponsorpro": 3}


def test_source_info_tiers() -> None:
    by_id = store.SOURCE_INFO_BY_ID
    assert by_id["finmind"].tiers == ["free", "backer", "sponsor", "sponsorpro"]
    assert by_id["alphavantage"].tiers == ["free", "premium"]
    # auth:"none" sources have no selectable tiers.
    assert by_id["twse"].tiers is None
    assert by_id["yfinance"].tiers is None


def test_set_tier_round_trips(conn: sqlite3.Connection) -> None:
    # default (unset) tier reads as None.
    assert store.get_state(conn, "finmind") is not None
    assert store.get_state(conn, "finmind").tier is None  # type: ignore[union-attr]
    store.set_tier(conn, "finmind", "backer")
    state = store.get_state(conn, "finmind")
    assert state is not None and state.tier == "backer"
    # list_states carries the tier too.
    assert store.list_states(conn)["finmind"].tier == "backer"


def test_set_tier_clears_with_none(conn: sqlite3.Connection) -> None:
    store.set_tier(conn, "finmind", "sponsor")
    store.set_tier(conn, "finmind", None)
    state = store.get_state(conn, "finmind")
    assert state is not None and state.tier is None


def test_migration_idempotent_on_existing_db_without_column() -> None:
    """A legacy DB created WITHOUT the tier column still opens (additive migration)."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    # Simulate an old schema: data_sources without a tier column.
    c.executescript(
        "CREATE TABLE data_sources (id TEXT PRIMARY KEY, api_key TEXT, "
        "enabled INTEGER NOT NULL DEFAULT 1);"
    )
    c.execute("INSERT INTO data_sources (id, api_key, enabled) VALUES ('finmind', NULL, 1)")
    c.commit()
    # create_tables must add the missing column without dropping the existing row.
    store.create_tables(c)
    cols = {r[1] for r in c.execute("PRAGMA table_info(data_sources)")}
    assert "tier" in cols
    row = c.execute("SELECT id FROM data_sources WHERE id='finmind'").fetchone()
    assert row is not None
    c.close()
