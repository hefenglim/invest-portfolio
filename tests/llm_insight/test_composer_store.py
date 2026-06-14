"""Unit tests for the insight-composer store (spec 04.0 / 4.1 / 4.6).

In-memory connection; the store owns its own tables via ``config_store`` so the
fixture only needs ``ensure_seeded``. No LLM, no money math — pure CRUD + cascade.
"""

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
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


def test_archive_calibration_clears_active_when_it_was_active(conn: sqlite3.Connection) -> None:
    it = cs.create_insight_type(conn, name="N", scope="portfolio", now=NOW)
    c1 = cs.create_calibration(conn, it.id, body="v1", cause="seed", now=NOW)
    cs.set_active_calibration(conn, it.id, c1.version)
    cs.archive_calibration(conn, c1.id)
    assert cs.get_insight_type(conn, it.id).active_calibration_version is None  # type: ignore[union-attr]


def test_archive_calibration_unknown_returns_none(conn: sqlite3.Connection) -> None:
    assert cs.archive_calibration(conn, 999) is None


# --- delete cascade: strategy (spec 4.1) --------------------------------------


def test_delete_strategy_referenced_raises_in_use(conn: sqlite3.Connection) -> None:
    it = cs.create_insight_type(conn, name="Combo", scope="portfolio", now=NOW)
    sp = cs.create_strategy(conn, name="A", body="a", now=NOW)
    cs.set_strategies(conn, it.id, [(sp.id, 0)])
    with pytest.raises(cs.StrategyInUseError) as exc:
        cs.delete_strategy(conn, sp.id, now=LATER)
    assert exc.value.referencing_insight_type_ids == [it.id]
    # Still present (not deleted).
    assert cs.get_strategy(conn, sp.id) is not None


def test_delete_strategy_never_used_hard_deletes(conn: sqlite3.Connection) -> None:
    sp = cs.create_strategy(conn, name="Lonely", body="x", now=NOW)
    outcome = cs.delete_strategy(conn, sp.id, now=LATER)
    assert outcome == "deleted"
    assert cs.get_strategy(conn, sp.id) is None


def test_delete_strategy_with_history_archives(conn: sqlite3.Connection) -> None:
    # A strategy that was archived (has history) but is no longer referenced -> stays
    # archived (soft-deleted), not hard-deleted.
    sp = cs.create_strategy(conn, name="Old", body="x", now=NOW)
    cs.archive_strategy(conn, sp.id, now=NOW)
    outcome = cs.delete_strategy(conn, sp.id, now=LATER)
    assert outcome == "archived"
    got = cs.get_strategy(conn, sp.id)
    assert got is not None and got.archived is True


def test_delete_strategy_unknown_returns_none(conn: sqlite3.Connection) -> None:
    assert cs.delete_strategy(conn, 999, now=NOW) is None


# --- delete cascade: insight_type (spec 4.1) ----------------------------------


def test_delete_insight_type_archives_clears_job_and_calibrations(
    conn: sqlite3.Connection,
) -> None:
    it = cs.create_insight_type(conn, name="Combo", scope="portfolio", now=NOW)
    cs.set_job_id(conn, it.id, "insight:1")
    c1 = cs.create_calibration(conn, it.id, body="v1", cause="seed", now=NOW)
    archived = cs.delete_insight_type(conn, it.id, now=LATER)
    assert archived is not None
    assert archived.archived is True
    assert archived.job_id is None
    # Calibration chain archived but retained.
    chain = cs.list_calibrations(conn, it.id, include_archived=True)
    assert [c.version for c in chain] == [c1.version]
    assert chain[0].archived is True
    # Default list hides the archived insight_type.
    assert it.id not in {x.id for x in cs.list_insight_types(conn)}
    assert it.id in {x.id for x in cs.list_insight_types(conn, include_archived=True)}


def test_delete_insight_type_unknown_returns_none(conn: sqlite3.Connection) -> None:
    assert cs.delete_insight_type(conn, 999, now=NOW) is None


# --- evolution config (spec 4.6) ----------------------------------------------


def test_evolution_config_defaults(conn: sqlite3.Connection) -> None:
    cfg = cs.get_evolution_config(conn)
    assert cfg == {
        "auto_promote": False,
        "shadow_batches": 3,
        "min_samples": 8,
        "max_shadows": 2,
        "gap_alert_pp": "10",
        # spec 04.10 new fields.
        "defer_limit_days": 5,
        "horizon_basis": "trading_days",
        "shadow_on_alert": False,
    }


def test_evolution_config_set_roundtrip(conn: sqlite3.Connection) -> None:
    out = cs.set_evolution_config(
        conn,
        auto_promote=True,
        shadow_batches=5,
        min_samples=12,
        max_shadows=3,
        gap_alert_pp=Decimal("7.5"),
        defer_limit_days=7,
        horizon_basis="calendar_days",
        shadow_on_alert=True,
    )
    assert out["auto_promote"] is True
    assert out["shadow_batches"] == 5
    assert out["min_samples"] == 12
    assert out["max_shadows"] == 3
    # gap_alert_pp round-trips as an exact Decimal string (never float).
    assert out["gap_alert_pp"] == "7.5"
    assert isinstance(out["gap_alert_pp"], str)
    # spec 04.10 new fields round-trip.
    assert out["defer_limit_days"] == 7
    assert out["horizon_basis"] == "calendar_days"
    assert out["shadow_on_alert"] is True
    # Persisted: a fresh read returns the same.
    fresh = cs.get_evolution_config(conn)
    assert fresh["gap_alert_pp"] == "7.5"
    assert fresh["defer_limit_days"] == 7
    assert fresh["horizon_basis"] == "calendar_days"
    assert fresh["shadow_on_alert"] is True


def test_evolution_config_legacy_row_migration() -> None:
    """A pre-04c single-row evolution_config (no new columns) gains the §4.10 defaults.

    Builds the OLD-schema table + a seeded row, then runs ``ensure_seeded`` (whose
    create-always step applies the additive ALTERs). The legacy row must read back the
    documented defaults for the three new knobs (idempotent backfill).
    """
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    try:
        # Legacy (pre-04c) evolution_config: no defer/horizon/shadow_on_alert columns.
        c.execute(
            "CREATE TABLE evolution_config ("
            "id INTEGER PRIMARY KEY CHECK (id = 1), auto_promote INTEGER NOT NULL, "
            "shadow_batches INTEGER NOT NULL, min_samples INTEGER NOT NULL, "
            "max_shadows INTEGER NOT NULL, gap_alert_pp TEXT NOT NULL)"
        )
        c.execute(
            "INSERT INTO evolution_config "
            "(id, auto_promote, shadow_batches, min_samples, max_shadows, gap_alert_pp) "
            "VALUES (1, 1, 4, 6, 1, '15')"
        )
        c.commit()
        cs.ensure_seeded(c)  # create-always step applies the additive migration
        cfg = cs.get_evolution_config(c)
        # Pre-existing values preserved …
        assert cfg["auto_promote"] is True
        assert cfg["shadow_batches"] == 4
        assert cfg["gap_alert_pp"] == "15"
        # … and the new columns backfilled with their defaults.
        assert cfg["defer_limit_days"] == 5
        assert cfg["horizon_basis"] == "trading_days"
        assert cfg["shadow_on_alert"] is False
    finally:
        c.close()


# --- insight_types horizon_days + eval_prompt (spec 04.10) --------------------


def test_insight_type_horizon_days_defaults_to_five(conn: sqlite3.Connection) -> None:
    it = cs.create_insight_type(conn, name="Daily", scope="portfolio", now=NOW)
    assert it.horizon_days == 5
    assert it.eval_prompt is None


def test_insight_type_horizon_and_eval_prompt_roundtrip(conn: sqlite3.Connection) -> None:
    it = cs.create_insight_type(
        conn, name="Watch", scope="per_symbol", horizon_days=10,
        eval_prompt="自訂檢驗：{{now}}", now=NOW,
    )
    got = cs.get_insight_type(conn, it.id)
    assert got is not None
    assert got.horizon_days == 10
    assert got.eval_prompt == "自訂檢驗：{{now}}"


def test_update_insight_type_changes_horizon_and_eval_prompt(conn: sqlite3.Connection) -> None:
    it = cs.create_insight_type(conn, name="N", scope="portfolio", now=NOW)
    updated = cs.update_insight_type(
        conn, it.id, name="N", scope="portfolio", horizon_days=3,
        eval_prompt="自訂", now=LATER,
    )
    assert updated is not None
    assert updated.horizon_days == 3
    assert updated.eval_prompt == "自訂"
    # Clearing eval_prompt back to None is honoured.
    cleared = cs.update_insight_type(
        conn, it.id, name="N", scope="portfolio", eval_prompt=None, now=LATER,
    )
    assert cleared is not None
    assert cleared.eval_prompt is None


def test_legacy_insight_types_table_gains_new_columns(conn: sqlite3.Connection) -> None:
    """A pre-04b insight_types table (no horizon_days/eval_prompt) is migrated additively."""
    # Simulate a legacy DB: drop the new columns by recreating the table without them.
    conn.execute("DROP TABLE insight_types")
    conn.execute(
        "CREATE TABLE insight_types ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, scope TEXT NOT NULL, "
        "use_system_prompt INTEGER NOT NULL DEFAULT 1, self_correct INTEGER NOT NULL DEFAULT 0, "
        "universe TEXT, alert_rules TEXT, enabled INTEGER NOT NULL DEFAULT 1, "
        "archived INTEGER NOT NULL DEFAULT 0, job_id TEXT, "
        "active_calibration_version INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO insight_types (name, scope, created_at, updated_at) "
        "VALUES ('Legacy', 'portfolio', ?, ?)",
        (NOW.isoformat(), NOW.isoformat()),
    )
    conn.commit()
    cs.ensure_seeded(conn)  # additive migration must add the two columns
    cols = {r[1] for r in conn.execute("PRAGMA table_info(insight_types)")}
    assert {"horizon_days", "eval_prompt"} <= cols
    row = conn.execute("SELECT * FROM insight_types WHERE name = 'Legacy'").fetchone()
    it = cs.get_insight_type(conn, row["id"])
    assert it is not None
    assert it.horizon_days == 5  # migrated rows default to 5
    assert it.eval_prompt is None
