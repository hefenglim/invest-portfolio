"""Tests for the LLM config store: tables, seed, registry, roles, budget."""

import sqlite3
from collections.abc import Iterator

import pytest

from portfolio_dash.shared.llm_config import (
    LLMRole,
    create_llm_tables,
    ensure_llm_seeded,
    restore_llm_defaults,
)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def _tables(c: sqlite3.Connection) -> set[str]:
    return {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_create_tables_makes_all_four(conn: sqlite3.Connection) -> None:
    create_llm_tables(conn)
    assert {"llm_models", "llm_defaults", "llm_budget_events", "llm_usage"} <= _tables(conn)


def test_seed_is_ai_off_four_null_roles(conn: sqlite3.Connection) -> None:
    ensure_llm_seeded(conn)
    rows = {
        r["role"]: r["model_id"]
        for r in conn.execute("SELECT role, model_id FROM llm_defaults")
    }
    assert set(rows) == {r.value for r in LLMRole}
    assert all(v is None for v in rows.values())  # AI cleanly off
    assert conn.execute("SELECT COUNT(*) c FROM llm_models").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM llm_budget_events").fetchone()["c"] == 0


def test_restore_defaults_clears_roles(conn: sqlite3.Connection) -> None:
    ensure_llm_seeded(conn)
    conn.execute("UPDATE llm_defaults SET model_id = 'x' WHERE role = 'default'")
    restore_llm_defaults(conn)
    row = conn.execute("SELECT model_id FROM llm_defaults WHERE role='default'").fetchone()
    assert row["model_id"] is None
