"""Unit tests for the insight-composer store (spec 04.0 / 4.1 / 4.6).

In-memory connection; the store owns its own tables via ``config_store`` so the
fixture only needs ``ensure_seeded``. No LLM, no money math — pure CRUD + cascade.
"""

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.llm_insight import composer_store as cs

NOW = datetime(2026, 6, 14, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))
LATER = datetime(2026, 6, 14, 11, 0, tzinfo=ZoneInfo("Asia/Taipei"))


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    cs.ensure_seeded(c)
    yield c
    c.close()


def _tables(c: sqlite3.Connection) -> set[str]:
    return {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


# --- ensure_seeded ------------------------------------------------------------


def test_ensure_seeded_creates_tables_and_is_idempotent(conn: sqlite3.Connection) -> None:
    expected = {
        "strategy_prompts",
        "insight_types",
        "insight_type_strategies",
        "calibration_prompts",
        "evolution_config",
    }
    assert expected <= _tables(conn)
    # Empty by design (golden_db parity).
    assert conn.execute("SELECT COUNT(*) FROM strategy_prompts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM insight_types").fetchone()[0] == 0
    cs.ensure_seeded(conn)  # second call must not raise or duplicate
    assert expected <= _tables(conn)


# --- strategy_prompts CRUD ----------------------------------------------------


def test_strategy_crud_roundtrip(conn: sqlite3.Connection) -> None:
    sp = cs.create_strategy(conn, name="Momentum", body="watch {{kpis_json}}", now=NOW)
    assert sp.id > 0
    assert sp.name == "Momentum"
    assert sp.body == "watch {{kpis_json}}"
    assert sp.enabled is True
    assert sp.archived is False
    assert sp.created_at == NOW.isoformat()
    assert sp.updated_at == NOW.isoformat()

    got = cs.get_strategy(conn, sp.id)
    assert got is not None
    assert got.name == "Momentum"

    rows = cs.list_strategies(conn)
    assert [r.id for r in rows] == [sp.id]

    updated = cs.update_strategy(
        conn, sp.id, name="Momentum v2", body="b2", enabled=False, now=LATER
    )
    assert updated is not None
    assert updated.name == "Momentum v2"
    assert updated.body == "b2"
    assert updated.enabled is False
    assert updated.updated_at == LATER.isoformat()
    assert updated.created_at == NOW.isoformat()  # never re-stamped


def test_list_strategies_excludes_archived_by_default(conn: sqlite3.Connection) -> None:
    keep = cs.create_strategy(conn, name="Keep", body="x", now=NOW)
    gone = cs.create_strategy(conn, name="Gone", body="y", now=NOW)
    cs.archive_strategy(conn, gone.id, now=LATER)

    default = cs.list_strategies(conn)
    assert [r.id for r in default] == [keep.id]

    all_rows = cs.list_strategies(conn, include_archived=True)
    assert {r.id for r in all_rows} == {keep.id, gone.id}
    archived = cs.get_strategy(conn, gone.id)
    assert archived is not None and archived.archived is True


def test_get_unknown_strategy_returns_none(conn: sqlite3.Connection) -> None:
    assert cs.get_strategy(conn, 999) is None
    assert cs.update_strategy(conn, 999, name="x", body="y", enabled=True, now=NOW) is None


# --- insight_types CRUD + ordered strategies ----------------------------------


def test_insight_type_create_defaults(conn: sqlite3.Connection) -> None:
    it = cs.create_insight_type(conn, name="Daily", scope="portfolio", now=NOW)
    assert it.id > 0
    assert it.name == "Daily"
    assert it.scope == "portfolio"
    assert it.use_system_prompt is True
    assert it.self_correct is False
    assert it.enabled is True
    assert it.archived is False
    assert it.universe is None
    assert it.alert_rules is None
    assert it.job_id is None
    assert it.active_calibration_version is None


def test_insight_type_universe_alert_rules_roundtrip_as_json(conn: sqlite3.Connection) -> None:
    it = cs.create_insight_type(
        conn,
        name="Watch",
        scope="per_symbol",
        universe={"mode": "custom", "symbols": ["2330", "AAPL"]},
        alert_rules=None,
        now=NOW,
    )
    got = cs.get_insight_type(conn, it.id)
    assert got is not None
    assert got.universe == {"mode": "custom", "symbols": ["2330", "AAPL"]}
    assert got.alert_rules is None

    alert_it = cs.create_insight_type(
        conn, name="Alert", scope="on_alert", alert_rules=["fx_drift"], now=NOW
    )
    got2 = cs.get_insight_type(conn, alert_it.id)
    assert got2 is not None
    assert got2.alert_rules == ["fx_drift"]


def test_set_and_get_strategies_ordered(conn: sqlite3.Connection) -> None:
    it = cs.create_insight_type(conn, name="Combo", scope="portfolio", now=NOW)
    a = cs.create_strategy(conn, name="A", body="a", now=NOW)
    b = cs.create_strategy(conn, name="B", body="b", now=NOW)
    cs.set_strategies(conn, it.id, [(b.id, 0), (a.id, 1)])
    ordered = cs.get_strategies(conn, it.id)
    assert [(s.id, s.position) for s in ordered] == [(b.id, 0), (a.id, 1)]
    # Re-set replaces the whole ordering.
    cs.set_strategies(conn, it.id, [(a.id, 0)])
    assert [(s.id, s.position) for s in cs.get_strategies(conn, it.id)] == [(a.id, 0)]


def test_update_insight_type(conn: sqlite3.Connection) -> None:
    it = cs.create_insight_type(conn, name="N", scope="portfolio", now=NOW)
    updated = cs.update_insight_type(
        conn, it.id, name="N2", scope="portfolio", self_correct=True,
        use_system_prompt=False, enabled=False, now=LATER,
    )
    assert updated is not None
    assert updated.name == "N2"
    assert updated.self_correct is True
    assert updated.use_system_prompt is False
    assert updated.enabled is False


def test_active_calibration_version_set_and_clear(conn: sqlite3.Connection) -> None:
    it = cs.create_insight_type(conn, name="N", scope="portfolio", now=NOW)
    cs.set_active_calibration(conn, it.id, 3)
    assert cs.get_insight_type(conn, it.id).active_calibration_version == 3  # type: ignore[union-attr]
    cs.set_active_calibration(conn, it.id, None)
    assert cs.get_insight_type(conn, it.id).active_calibration_version is None  # type: ignore[union-attr]


# --- calibration_prompts ------------------------------------------------------


def test_calibration_versions_increment_and_list(conn: sqlite3.Connection) -> None:
    it = cs.create_insight_type(conn, name="N", scope="portfolio", now=NOW)
    c1 = cs.create_calibration(conn, it.id, body="v1", cause="seed", now=NOW)
    c2 = cs.create_calibration(conn, it.id, body="v2", cause="miss", now=LATER)
    assert c1.version == 1
    assert c2.version == 2
    assert c1.insight_type_id == it.id
    assert c1.archived is False
    versions = [c.version for c in cs.list_calibrations(conn, it.id)]
    assert versions == [1, 2]


def test_calibration_next_version_starts_at_one(conn: sqlite3.Connection) -> None:
    it = cs.create_insight_type(conn, name="N", scope="portfolio", now=NOW)
    assert cs.next_version(conn, it.id) == 1
    cs.create_calibration(conn, it.id, body="v1", cause="seed", now=NOW)
    assert cs.next_version(conn, it.id) == 2


def test_archive_calibration_hidden_by_default(conn: sqlite3.Connection) -> None:
    it = cs.create_insight_type(conn, name="N", scope="portfolio", now=NOW)
    c1 = cs.create_calibration(conn, it.id, body="v1", cause="seed", now=NOW)
    cs.archive_calibration(conn, c1.id)
    assert cs.list_calibrations(conn, it.id) == []
    incl = cs.list_calibrations(conn, it.id, include_archived=True)
    assert [c.version for c in incl] == [1]
    assert incl[0].archived is True
