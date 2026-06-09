import sqlite3
from collections.abc import Iterator

import pytest

from portfolio_dash.bootstrap import bootstrap_db


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def test_bootstrap_creates_ledgers_and_llm_and_seeds_ai_off(conn: sqlite3.Connection) -> None:
    bootstrap_db(conn)
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"transactions", "accounts", "llm_models", "llm_defaults", "llm_usage"} <= names
    # AI off after bootstrap
    roles = list(conn.execute("SELECT model_id FROM llm_defaults"))
    assert roles and all(r["model_id"] is None for r in roles)
