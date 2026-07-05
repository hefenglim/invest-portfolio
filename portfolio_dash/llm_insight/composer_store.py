"""Insight-composer persistence: the static "design object" layer of spec 04.

Owns four composer tables + a single-row evolution config via :mod:`config_store`:

- ``strategy_prompts``        — pure design objects (body holds ``{{var}}`` tokens).
- ``insight_types``           — the composition (scope + 1..n strategies + toggles);
  the sole schedule/calibration mount point.
- ``insight_type_strategies`` — the ordered many-to-many link (insight_type → strategy).
- ``calibration_prompts``     — the 1:1 version chain per insight_type (archive = soft
  delete; never physically removed once it has history).
- ``evolution_config``        — single-row evolution knobs (spec 4.6).

This layer is pure persistence over its own tables: stdlib + ``shared``/``config_store``
only (NOT ``pricing``/``data_ingestion``/``api``/``scheduler`` — ``architecture.md``).
There is no LLM call, no money math, and no float here. Booleans persist as INTEGER;
``universe``/``alert_rules`` persist as JSON TEXT (parsed on read); timestamps are ISO
strings (the caller injects an Asia/Taipei ``now``). 04a is CRUD + cascade only — insight
generation, evaluation, and calibration generation are 04b/04c.
"""

import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from portfolio_dash.shared import config_store

CATEGORY = "evolution"

# --- Evolution config defaults (spec 4.6) -------------------------------------
# gap_alert_pp is percentage-points, served/stored as a Decimal STRING (never float).
_EVOLUTION_DEFAULTS: dict[str, Any] = {
    "auto_promote": False,
    "shadow_batches": 3,
    "min_samples": 8,
    "max_shadows": 2,
    "gap_alert_pp": "10",
    # spec 04.10 new fields:
    # defer_limit_days — pending_data anti-poison cap (trading days) before an
    #   unscoreable insight becomes ``undetermined`` (excluded from calibration/score).
    # horizon_basis — how a card's horizon advances ("trading_days" | "calendar_days").
    # shadow_on_alert — whether on_alert runs also produce a shadow card (default off).
    "defer_limit_days": 5,
    "horizon_basis": "trading_days",
    "shadow_on_alert": False,
}

# Allowed horizon-basis values (spec 04.10); the API rejects anything else with 400.
HORIZON_BASIS_VALUES = ("trading_days", "calendar_days")


# --- Errors -------------------------------------------------------------------


class StrategyInUseError(Exception):
    """A strategy_prompt delete was blocked because insight_types still reference it.

    Carries the referencing insight_type ids so the API can return them (spec 4.1 → 409).
    """

    def __init__(self, referencing_insight_type_ids: list[int]) -> None:
        self.referencing_insight_type_ids = referencing_insight_type_ids
        super().__init__(
            f"strategy referenced by insight_types {referencing_insight_type_ids}"
        )


# --- Pydantic models ----------------------------------------------------------


class StrategyPrompt(BaseModel):
    id: int
    name: str
    body: str
    enabled: bool
    archived: bool
    created_at: str
    updated_at: str


class StrategyRef(BaseModel):
    """A strategy as referenced by an insight_type, carrying its link position."""

    id: int
    name: str
    position: int


class InsightType(BaseModel):
    id: int
    name: str
    scope: str  # 'per_symbol' | 'portfolio' | 'on_alert'
    use_system_prompt: bool
    self_correct: bool
    universe: dict[str, Any] | list[Any] | str | None
    alert_rules: dict[str, Any] | list[Any] | str | None  # 'all' | [rule_ids] | None
    enabled: bool
    archived: bool
    job_id: str | None
    active_calibration_version: int | None
    horizon_days: int  # task-default prediction horizon (spec 04.10); cards may override
    eval_prompt: str | None  # optional custom self-evaluation prompt (spec 04.10)
    created_at: str
    updated_at: str


class Calibration(BaseModel):
    id: int
    insight_type_id: int
    version: int
    archived: bool
    body: str
    cause: str | None
    created_at: str


# --- Schema -------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS strategy_prompts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    body TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS insight_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    scope TEXT NOT NULL,
    use_system_prompt INTEGER NOT NULL DEFAULT 1,
    self_correct INTEGER NOT NULL DEFAULT 0,
    universe TEXT,
    alert_rules TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    archived INTEGER NOT NULL DEFAULT 0,
    job_id TEXT,
    active_calibration_version INTEGER,
    horizon_days INTEGER NOT NULL DEFAULT 5,
    eval_prompt TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS insight_type_strategies (
    insight_type_id INTEGER NOT NULL,
    strategy_prompt_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (insight_type_id, strategy_prompt_id)
);
CREATE TABLE IF NOT EXISTS calibration_prompts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    insight_type_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0,
    body TEXT NOT NULL,
    cause TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evolution_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    auto_promote INTEGER NOT NULL,
    shadow_batches INTEGER NOT NULL,
    min_samples INTEGER NOT NULL,
    max_shadows INTEGER NOT NULL,
    gap_alert_pp TEXT NOT NULL,
    defer_limit_days INTEGER NOT NULL DEFAULT 5,
    horizon_basis TEXT NOT NULL DEFAULT 'trading_days',
    shadow_on_alert INTEGER NOT NULL DEFAULT 0
);
"""


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, decl: str
) -> None:
    """Add ``column`` to ``table`` if absent (additive, idempotent migration).

    A LOCAL copy of the scheduler/data_ingestion PRAGMA pattern, intentionally NOT
    imported: ``llm_insight`` must not gain a dependency on those layers
    (``architecture.md``). ``PRAGMA table_info`` row index 1 is the column name,
    which is row_factory-agnostic.
    """
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _create(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    # Additive §4.10 migration so legacy (pre-04b) insight_types tables gain the new
    # task-default horizon + optional custom eval-prompt columns.
    _add_column_if_missing(conn, "insight_types", "horizon_days", "INTEGER NOT NULL DEFAULT 5")
    _add_column_if_missing(conn, "insight_types", "eval_prompt", "TEXT")
    # Additive §4.10 migration so a legacy single-row evolution_config gains the new
    # defer-limit / horizon-basis / shadow-on-alert knobs (idempotent ALTER).
    _add_column_if_missing(
        conn, "evolution_config", "defer_limit_days", "INTEGER NOT NULL DEFAULT 5"
    )
    _add_column_if_missing(
        conn, "evolution_config", "horizon_basis", "TEXT NOT NULL DEFAULT 'trading_days'"
    )
    _add_column_if_missing(
        conn, "evolution_config", "shadow_on_alert", "INTEGER NOT NULL DEFAULT 0"
    )


def _seed(conn: sqlite3.Connection) -> None:
    """Seed the single evolution_config row (id=1) with defaults. Idempotent.

    The four composer tables start EMPTY (they hold user data); only the single-row
    config is seeded with defaults (spec 4.6).
    """
    d = _EVOLUTION_DEFAULTS
    conn.execute(
        "INSERT INTO evolution_config "
        "(id, auto_promote, shadow_batches, min_samples, max_shadows, gap_alert_pp, "
        "defer_limit_days, horizon_basis, shadow_on_alert) "
        "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
        (
            1 if d["auto_promote"] else 0,
            d["shadow_batches"],
            d["min_samples"],
            d["max_shadows"],
            d["gap_alert_pp"],
            d["defer_limit_days"],
            d["horizon_basis"],
            1 if d["shadow_on_alert"] else 0,
        ),
    )


def ensure_seeded(conn: sqlite3.Connection) -> None:
    """Create the composer tables (always) and seed the evolution config (once)."""
    config_store.ensure_seeded(conn, CATEGORY, create=_create, seed=_seed)


# --- strategy_prompts CRUD ----------------------------------------------------


def _strategy_from_row(row: sqlite3.Row) -> StrategyPrompt:
    return StrategyPrompt(
        id=row["id"],
        name=row["name"],
        body=row["body"],
        enabled=bool(row["enabled"]),
        archived=bool(row["archived"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def create_strategy(
    conn: sqlite3.Connection, *, name: str, body: str, now: datetime
) -> StrategyPrompt:
    """Insert a new (enabled, non-archived) strategy prompt; return the stored row."""
    ts = now.isoformat()
    cur = conn.execute(
        "INSERT INTO strategy_prompts (name, body, enabled, archived, created_at, "
        "updated_at) VALUES (?, ?, 1, 0, ?, ?)",
        (name, body, ts, ts),
    )
    conn.commit()
    sp = get_strategy(conn, int(cur.lastrowid or 0))
    assert sp is not None  # just inserted
    return sp


def get_strategy(conn: sqlite3.Connection, strategy_id: int) -> StrategyPrompt | None:
    """Return one strategy prompt by id (including archived), or None."""
    row = conn.execute(
        "SELECT * FROM strategy_prompts WHERE id = ?", (strategy_id,)
    ).fetchone()
    return _strategy_from_row(row) if row is not None else None


def list_strategies(
    conn: sqlite3.Connection, *, include_archived: bool = False
) -> list[StrategyPrompt]:
    """List strategy prompts ordered by id; excludes archived unless asked."""
    sql = "SELECT * FROM strategy_prompts"
    if not include_archived:
        sql += " WHERE archived = 0"
    sql += " ORDER BY id"
    return [_strategy_from_row(r) for r in conn.execute(sql)]


def update_strategy(
    conn: sqlite3.Connection,
    strategy_id: int,
    *,
    name: str,
    body: str,
    enabled: bool,
    now: datetime,
) -> StrategyPrompt | None:
    """Update a strategy's name/body/enabled + re-stamp ``updated_at``; None if absent.

    ``created_at`` is never re-stamped (history retained); ``archived`` is changed only
    via :func:`archive_strategy` / the delete cascade.
    """
    if get_strategy(conn, strategy_id) is None:
        return None
    conn.execute(
        "UPDATE strategy_prompts SET name = ?, body = ?, enabled = ?, updated_at = ? "
        "WHERE id = ?",
        (name, body, 1 if enabled else 0, now.isoformat(), strategy_id),
    )
    conn.commit()
    return get_strategy(conn, strategy_id)


def archive_strategy(
    conn: sqlite3.Connection, strategy_id: int, *, now: datetime
) -> StrategyPrompt | None:
    """Soft-delete a strategy (``archived=1``) + re-stamp ``updated_at``; None if absent."""
    if get_strategy(conn, strategy_id) is None:
        return None
    conn.execute(
        "UPDATE strategy_prompts SET archived = 1, updated_at = ? WHERE id = ?",
        (now.isoformat(), strategy_id),
    )
    conn.commit()
    return get_strategy(conn, strategy_id)


# --- insight_types CRUD -------------------------------------------------------


def _json_or_none(value: str | None) -> dict[str, Any] | list[Any] | str | None:
    if value is None:
        return None
    parsed: Any = json.loads(value)
    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, list):
        return parsed
    # ``alert_rules`` may be the scalar string ``"all"`` (spec 4.0); preserve it. Any other
    # scalar JSON value is not a valid universe/alert_rules shape and is dropped.
    if isinstance(parsed, str):
        return parsed
    return None


def _insight_type_from_row(row: sqlite3.Row) -> InsightType:
    return InsightType(
        id=row["id"],
        name=row["name"],
        scope=row["scope"],
        use_system_prompt=bool(row["use_system_prompt"]),
        self_correct=bool(row["self_correct"]),
        universe=_json_or_none(row["universe"]),
        alert_rules=_json_or_none(row["alert_rules"]),
        enabled=bool(row["enabled"]),
        archived=bool(row["archived"]),
        job_id=row["job_id"],
        active_calibration_version=row["active_calibration_version"],
        horizon_days=row["horizon_days"],
        eval_prompt=row["eval_prompt"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def create_insight_type(
    conn: sqlite3.Connection,
    *,
    name: str,
    scope: str,
    use_system_prompt: bool = True,
    self_correct: bool = False,
    universe: dict[str, Any] | list[Any] | str | None = None,
    alert_rules: dict[str, Any] | list[Any] | str | None = None,
    enabled: bool = True,
    horizon_days: int = 5,
    eval_prompt: str | None = None,
    now: datetime,
) -> InsightType:
    """Insert a new insight_type (the composition); return the stored row.

    ``universe``/``alert_rules`` are stored as JSON TEXT. The on_alert default-disabled
    rule (R7) is the API's concern; this is the raw write (the caller passes ``enabled``).
    ``horizon_days`` is the task-default prediction horizon (spec 04.10), ``eval_prompt``
    an optional custom self-evaluation prompt (NULL → standard master-scoring template).
    """
    ts = now.isoformat()
    cur = conn.execute(
        "INSERT INTO insight_types (name, scope, use_system_prompt, self_correct, "
        "universe, alert_rules, enabled, archived, job_id, active_calibration_version, "
        "horizon_days, eval_prompt, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?, ?, ?, ?)",
        (
            name,
            scope,
            1 if use_system_prompt else 0,
            1 if self_correct else 0,
            json.dumps(universe) if universe is not None else None,
            json.dumps(alert_rules) if alert_rules is not None else None,
            1 if enabled else 0,
            horizon_days,
            eval_prompt,
            ts,
            ts,
        ),
    )
    conn.commit()
    it = get_insight_type(conn, int(cur.lastrowid or 0))
    assert it is not None  # just inserted
    return it


def get_insight_type(conn: sqlite3.Connection, insight_type_id: int) -> InsightType | None:
    """Return one insight_type by id (including archived), or None."""
    row = conn.execute(
        "SELECT * FROM insight_types WHERE id = ?", (insight_type_id,)
    ).fetchone()
    return _insight_type_from_row(row) if row is not None else None


def list_insight_types(
    conn: sqlite3.Connection, *, include_archived: bool = False
) -> list[InsightType]:
    """List insight_types ordered by id; excludes archived unless asked."""
    sql = "SELECT * FROM insight_types"
    if not include_archived:
        sql += " WHERE archived = 0"
    sql += " ORDER BY id"
    return [_insight_type_from_row(r) for r in conn.execute(sql)]


def archived_type_ids(conn: sqlite3.Connection) -> set[int]:
    """The archived insight_type ids — the read-side exclusion set.

    Deleting a task is an ARCHIVE (spec 4.1: history is never physically removed),
    but its historical cards/evaluations must stop surfacing on the insights list,
    the dashboard embed, and the AI battle record. The api layer feeds this set to
    the store readers' ``exclude_type_ids``.
    """
    return {
        int(r["id"])
        for r in conn.execute("SELECT id FROM insight_types WHERE archived = 1")
    }


def update_insight_type(
    conn: sqlite3.Connection,
    insight_type_id: int,
    *,
    name: str,
    scope: str,
    use_system_prompt: bool = True,
    self_correct: bool = False,
    universe: dict[str, Any] | list[Any] | str | None = None,
    alert_rules: dict[str, Any] | list[Any] | str | None = None,
    enabled: bool = True,
    horizon_days: int = 5,
    eval_prompt: str | None = None,
    now: datetime,
) -> InsightType | None:
    """Update an insight_type's editable fields + re-stamp ``updated_at``; None if absent.

    Does not touch ``job_id``/``active_calibration_version``/``archived`` (those move via
    the schedule, active-calibration, and cascade helpers). ``horizon_days``/``eval_prompt``
    are overwritten on every update (spec 04.10), so a None ``eval_prompt`` clears it.
    """
    if get_insight_type(conn, insight_type_id) is None:
        return None
    conn.execute(
        "UPDATE insight_types SET name = ?, scope = ?, use_system_prompt = ?, "
        "self_correct = ?, universe = ?, alert_rules = ?, enabled = ?, horizon_days = ?, "
        "eval_prompt = ?, updated_at = ? WHERE id = ?",
        (
            name,
            scope,
            1 if use_system_prompt else 0,
            1 if self_correct else 0,
            json.dumps(universe) if universe is not None else None,
            json.dumps(alert_rules) if alert_rules is not None else None,
            1 if enabled else 0,
            horizon_days,
            eval_prompt,
            now.isoformat(),
            insight_type_id,
        ),
    )
    conn.commit()
    return get_insight_type(conn, insight_type_id)


def set_active_calibration(
    conn: sqlite3.Connection, insight_type_id: int, version: int | None
) -> None:
    """Set (or clear) an insight_type's manually-selected active calibration version."""
    conn.execute(
        "UPDATE insight_types SET active_calibration_version = ? WHERE id = ?",
        (version, insight_type_id),
    )
    conn.commit()


def set_job_id(conn: sqlite3.Connection, insight_type_id: int, job_id: str | None) -> None:
    """Record (or clear) the insight_type's schedule job_id mirror (spec 4.2)."""
    conn.execute(
        "UPDATE insight_types SET job_id = ? WHERE id = ?", (job_id, insight_type_id)
    )
    conn.commit()


# --- insight_type_strategies (ordered link) -----------------------------------


def set_strategies(
    conn: sqlite3.Connection, insight_type_id: int, links: list[tuple[int, int]]
) -> None:
    """Replace an insight_type's whole ordered strategy set with ``links`` [(sp_id, pos)]."""
    conn.execute(
        "DELETE FROM insight_type_strategies WHERE insight_type_id = ?",
        (insight_type_id,),
    )
    for sp_id, position in links:
        conn.execute(
            "INSERT INTO insight_type_strategies (insight_type_id, strategy_prompt_id, "
            "position) VALUES (?, ?, ?)",
            (insight_type_id, sp_id, position),
        )
    conn.commit()


def get_strategies(conn: sqlite3.Connection, insight_type_id: int) -> list[StrategyRef]:
    """Return an insight_type's strategies in ``position`` order (name joined in)."""
    rows = conn.execute(
        "SELECT s.id AS id, s.name AS name, l.position AS position "
        "FROM insight_type_strategies l JOIN strategy_prompts s "
        "ON s.id = l.strategy_prompt_id WHERE l.insight_type_id = ? "
        "ORDER BY l.position",
        (insight_type_id,),
    ).fetchall()
    return [StrategyRef(id=r["id"], name=r["name"], position=r["position"]) for r in rows]


def referencing_insight_type_ids(
    conn: sqlite3.Connection, strategy_id: int
) -> list[int]:
    """Insight_type ids that currently link the given strategy (for the delete cascade)."""
    rows = conn.execute(
        "SELECT DISTINCT insight_type_id FROM insight_type_strategies "
        "WHERE strategy_prompt_id = ? ORDER BY insight_type_id",
        (strategy_id,),
    ).fetchall()
    return [int(r["insight_type_id"]) for r in rows]


# --- calibration_prompts ------------------------------------------------------


def _calibration_from_row(row: sqlite3.Row) -> Calibration:
    return Calibration(
        id=row["id"],
        insight_type_id=row["insight_type_id"],
        version=row["version"],
        archived=bool(row["archived"]),
        body=row["body"],
        cause=row["cause"],
        created_at=row["created_at"],
    )


def next_version(conn: sqlite3.Connection, insight_type_id: int) -> int:
    """The next calibration version for an insight_type (max+1, or 1 when none exist)."""
    row = conn.execute(
        "SELECT MAX(version) AS m FROM calibration_prompts WHERE insight_type_id = ?",
        (insight_type_id,),
    ).fetchone()
    return int(row["m"]) + 1 if row["m"] is not None else 1


def create_calibration(
    conn: sqlite3.Connection,
    insight_type_id: int,
    *,
    body: str,
    cause: str | None,
    now: datetime,
) -> Calibration:
    """Append a new calibration version (N+1) for an insight_type; return the stored row."""
    version = next_version(conn, insight_type_id)
    cur = conn.execute(
        "INSERT INTO calibration_prompts (insight_type_id, version, archived, body, "
        "cause, created_at) VALUES (?, ?, 0, ?, ?, ?)",
        (insight_type_id, version, body, cause, now.isoformat()),
    )
    conn.commit()
    cal = get_calibration(conn, int(cur.lastrowid or 0))
    assert cal is not None  # just inserted
    return cal


def get_calibration(conn: sqlite3.Connection, calibration_id: int) -> Calibration | None:
    """Return one calibration by id (including archived), or None."""
    row = conn.execute(
        "SELECT * FROM calibration_prompts WHERE id = ?", (calibration_id,)
    ).fetchone()
    return _calibration_from_row(row) if row is not None else None


def list_calibrations(
    conn: sqlite3.Connection, insight_type_id: int, *, include_archived: bool = False
) -> list[Calibration]:
    """List an insight_type's calibration chain by version; excludes archived unless asked."""
    sql = "SELECT * FROM calibration_prompts WHERE insight_type_id = ?"
    if not include_archived:
        sql += " AND archived = 0"
    sql += " ORDER BY version"
    return [_calibration_from_row(r) for r in conn.execute(sql, (insight_type_id,))]


def archive_calibration(conn: sqlite3.Connection, calibration_id: int) -> Calibration | None:
    """Soft-delete a calibration version (``archived=1``); None if absent.

    If the version was its insight_type's active version, the active selection is cleared
    (spec 4.1). The cause/body are retained (attribution chain never breaks).
    """
    cal = get_calibration(conn, calibration_id)
    if cal is None:
        return None
    conn.execute(
        "UPDATE calibration_prompts SET archived = 1 WHERE id = ?", (calibration_id,)
    )
    it = get_insight_type(conn, cal.insight_type_id)
    if it is not None and it.active_calibration_version == cal.version:
        set_active_calibration(conn, cal.insight_type_id, None)
    conn.commit()
    return get_calibration(conn, calibration_id)


# --- evolution_config (single-row) --------------------------------------------


def get_evolution_config(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return the evolution knobs (spec 4.6); defaults when the row is absent.

    ``gap_alert_pp`` is a percentage-points Decimal served as a STRING (never float).
    """
    ensure_seeded(conn)
    row = conn.execute(
        "SELECT auto_promote, shadow_batches, min_samples, max_shadows, gap_alert_pp, "
        "defer_limit_days, horizon_basis, shadow_on_alert FROM evolution_config WHERE id = 1"
    ).fetchone()
    if row is None:
        return dict(_EVOLUTION_DEFAULTS)
    # The §4.10 columns are NOT NULL with defaults; a legacy table is backfilled by the
    # additive ALTER in ``_create`` (run on every ensure_seeded), so a direct read is safe.
    return {
        "auto_promote": bool(row["auto_promote"]),
        "shadow_batches": int(row["shadow_batches"]),
        "min_samples": int(row["min_samples"]),
        "max_shadows": int(row["max_shadows"]),
        "gap_alert_pp": str(row["gap_alert_pp"]),
        "defer_limit_days": int(row["defer_limit_days"]),
        "horizon_basis": str(row["horizon_basis"]),
        "shadow_on_alert": bool(row["shadow_on_alert"]),
    }


def set_evolution_config(
    conn: sqlite3.Connection,
    *,
    auto_promote: bool,
    shadow_batches: int,
    min_samples: int,
    max_shadows: int,
    gap_alert_pp: Decimal,
    defer_limit_days: int = 5,
    horizon_basis: str = "trading_days",
    shadow_on_alert: bool = False,
) -> dict[str, Any]:
    """Upsert the single evolution_config row; return the stored (serialized) view.

    ``gap_alert_pp`` arrives as a :class:`Decimal` and is stored as its canonical string
    (the 2-dp rule never applies to a percentage-points knob; it is exact at any scale).
    ``horizon_basis`` is validated by the caller (API) against
    :data:`HORIZON_BASIS_VALUES`; the new §4.10 knobs default to their documented values
    so older callers stay back-compatible.
    """
    ensure_seeded(conn)
    conn.execute(
        "INSERT INTO evolution_config "
        "(id, auto_promote, shadow_batches, min_samples, max_shadows, gap_alert_pp, "
        "defer_limit_days, horizon_basis, shadow_on_alert) "
        "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
        "auto_promote = excluded.auto_promote, shadow_batches = excluded.shadow_batches, "
        "min_samples = excluded.min_samples, max_shadows = excluded.max_shadows, "
        "gap_alert_pp = excluded.gap_alert_pp, "
        "defer_limit_days = excluded.defer_limit_days, "
        "horizon_basis = excluded.horizon_basis, "
        "shadow_on_alert = excluded.shadow_on_alert",
        (
            1 if auto_promote else 0,
            shadow_batches,
            min_samples,
            max_shadows,
            str(gap_alert_pp),
            defer_limit_days,
            horizon_basis,
            1 if shadow_on_alert else 0,
        ),
    )
    conn.commit()
    return get_evolution_config(conn)


# --- delete cascades (spec 4.1) -----------------------------------------------


def delete_strategy(
    conn: sqlite3.Connection, strategy_id: int, *, now: datetime
) -> str | None:
    """Spec-4.1 strategy delete cascade. Returns the outcome, or None if absent.

    - Referenced by any insight_type → raise :class:`StrategyInUseError` (→ API 409).
    - Otherwise, if it has history (proxy: already ``archived``) → keep archived
      ("archived"); else soft-delete it now → ("archived").
    - Never-used + never-archived strategies are hard-deleted → ("deleted").

    "Has history" proxy: a strategy is only ever archived after being referenced (the
    in-use path blocks deletes while linked), so the ``archived`` flag is a sufficient
    stand-in for "was ever used" without a separate usage-marker column.
    """
    sp = get_strategy(conn, strategy_id)
    if sp is None:
        return None
    refs = referencing_insight_type_ids(conn, strategy_id)
    if refs:
        raise StrategyInUseError(refs)
    if sp.archived:
        return "archived"  # already soft-deleted; leave the history row in place
    conn.execute("DELETE FROM strategy_prompts WHERE id = ?", (strategy_id,))
    conn.commit()
    return "deleted"


def delete_insight_type(
    conn: sqlite3.Connection, insight_type_id: int, *, now: datetime
) -> InsightType | None:
    """Spec-4.1 insight_type delete cascade. Returns the archived row, or None if absent.

    Sets ``archived=1``, clears the schedule binding mirror (``job_id=NULL``), and
    archives the WHOLE calibration chain (history rows are retained, just soft-deleted).
    The actual scheduler row removal (``unbind_insight_schedule``) is the API router's
    concern (api → scheduler is allowed; this layer must not import scheduler).
    """
    it = get_insight_type(conn, insight_type_id)
    if it is None:
        return None
    conn.execute(
        "UPDATE insight_types SET archived = 1, job_id = NULL, updated_at = ? WHERE id = ?",
        (now.isoformat(), insight_type_id),
    )
    conn.execute(
        "UPDATE calibration_prompts SET archived = 1 WHERE insight_type_id = ?",
        (insight_type_id,),
    )
    conn.commit()
    return get_insight_type(conn, insight_type_id)
