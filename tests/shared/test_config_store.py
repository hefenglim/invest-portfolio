"""Tests for the generic create-always / seed-once settings framework."""

import sqlite3
from collections.abc import Iterator

import pytest

from portfolio_dash.shared.config_store import ensure_seeded, restore_defaults


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def _create(c: sqlite3.Connection) -> None:
    c.execute("CREATE TABLE IF NOT EXISTS demo (k TEXT PRIMARY KEY, v TEXT)")


def test_seed_runs_once_create_runs_always(conn: sqlite3.Connection) -> None:
    seeds = {"n": 0}

    def seed(c: sqlite3.Connection) -> None:
        seeds["n"] += 1
        c.execute("INSERT INTO demo (k, v) VALUES ('a', 'default')")

    ensure_seeded(conn, "demo", create=_create, seed=seed)
    ensure_seeded(conn, "demo", create=_create, seed=seed)  # second call: create yes, seed no

    assert seeds["n"] == 1
    rows = list(conn.execute("SELECT k, v FROM demo"))
    assert len(rows) == 1 and rows[0]["v"] == "default"


def test_restore_defaults_reapplies_seed(conn: sqlite3.Connection) -> None:
    def seed(c: sqlite3.Connection) -> None:
        c.execute("INSERT INTO demo (k, v) VALUES ('a', 'default') "
                  "ON CONFLICT(k) DO UPDATE SET v='default'")

    ensure_seeded(conn, "demo", create=_create, seed=seed)
    conn.execute("UPDATE demo SET v='changed' WHERE k='a'")
    restore_defaults(conn, "demo", seed=seed)
    assert conn.execute("SELECT v FROM demo WHERE k='a'").fetchone()["v"] == "default"
