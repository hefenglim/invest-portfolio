"""Insight-evaluations persistence + ai-score aggregation (spec 04.4 / 4.7).

Owns the ``insight_evaluations`` table — the AI master-model backtest record from which
each calibration version's accumulated score is derived (spec 4.4 step 4). An evaluation
row is APPEND-ONLY in spirit: every evaluate-pass on a due insight inserts a new row; a
``pending_data`` retry inserts a fresh pending row (the latest by id is authoritative).

Status (spec 04.10 anti-poison):
- ``pending_data`` — the actual value was unavailable on the evaluation day; deferred,
  NEVER force-judged a miss. ``defer_count`` carries the trading-day retry count.
- ``scored``       — quant and/or narrative scored; ``miss`` is the combined verdict.
- ``undetermined`` — exceeded the defer cap; excluded from calibration + the score, and
  NEVER counted as a miss (does not poison Loop 3).

This layer is pure persistence + deterministic SQL rollups over its own table + the
``insights`` table (read-only, for ``due_insights``): stdlib + pydantic + ``llm_insight``
only (NOT pricing / data_ingestion / api / scheduler — architecture.md). No LLM call, no
float — ``actual_value`` persists as a canonical Decimal string; ``narrative_score`` /
``confidence`` are ints; rates are Decimal strings.
"""

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel

EvalStatus = Literal["pending_data", "scored", "undetermined"]

# Confidence buckets for the calibration curve (spec 04.10 calibration_bins).
_BUCKETS: tuple[tuple[str, int, int], ...] = (
    ("0-20", 0, 20),
    ("20-40", 20, 40),
    ("40-60", 40, 60),
    ("60-80", 60, 80),
    ("80-100", 80, 100),
)


class Evaluation(BaseModel):
    """One ``insight_evaluations`` row."""

    id: int
    insight_id: int
    insight_type_id: int
    calibration_version: int | None
    is_shadow: bool
    status: EvalStatus
    quant_hit: bool | None
    narrative_score: int | None
    miss: bool
    actual_value: str | None  # Decimal STRING (never float)
    confidence: int | None
    defer_count: int
    notes: str | None
    evaluated_at: str


class DueInsight(BaseModel):
    """A due insight awaiting evaluation (a slim view over the ``insights`` row)."""

    insight_id: int
    insight_type_id: int
    symbol: str | None
    calibration_version: int | None
    is_shadow: bool
    confidence: int | None
    prediction: str | None  # the raw prediction JSON (parsed by the scorer)
    due_at: str | None
    created_at: str
    # The close the model saw at create time (M4 fix) — the preferred Loop-2 baseline;
    # None for legacy cards (they score on the old on-or-after-create basis).
    price_at_create: str | None = None


_DDL = """
CREATE TABLE IF NOT EXISTS insight_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    insight_id INTEGER NOT NULL,
    insight_type_id INTEGER NOT NULL,
    calibration_version INTEGER,
    is_shadow INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    quant_hit INTEGER,
    narrative_score INTEGER,
    miss INTEGER NOT NULL DEFAULT 0,
    actual_value TEXT,
    confidence INTEGER,
    defer_count INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    evaluated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evals_insight ON insight_evaluations (insight_id);
CREATE INDEX IF NOT EXISTS idx_evals_type ON insight_evaluations (insight_type_id);
"""


def ensure_tables(conn: sqlite3.Connection) -> None:
    """Create the ``insight_evaluations`` table + indexes idempotently (create-always)."""
    conn.executescript(_DDL)
    conn.commit()


# --- add + read ---------------------------------------------------------------


def _opt_bool(value: int | None) -> bool | None:
    return None if value is None else bool(value)


def _eval_from_row(row: sqlite3.Row) -> Evaluation:
    status = row["status"]
    assert status in ("pending_data", "scored", "undetermined")
    return Evaluation(
        id=row["id"],
        insight_id=row["insight_id"],
        insight_type_id=row["insight_type_id"],
        calibration_version=row["calibration_version"],
        is_shadow=bool(row["is_shadow"]),
        status=status,
        quant_hit=_opt_bool(row["quant_hit"]),
        narrative_score=row["narrative_score"],
        miss=bool(row["miss"]),
        actual_value=row["actual_value"],
        confidence=row["confidence"],
        defer_count=row["defer_count"],
        notes=row["notes"],
        evaluated_at=row["evaluated_at"],
    )


def add_evaluation(
    conn: sqlite3.Connection,
    *,
    insight_id: int,
    insight_type_id: int,
    calibration_version: int | None,
    is_shadow: bool,
    status: EvalStatus,
    quant_hit: bool | None,
    narrative_score: int | None,
    miss: bool,
    actual_value: Decimal | None,
    confidence: int | None,
    now: datetime,
    notes: str | None = None,
) -> Evaluation:
    """Append one evaluation row; return the stored record.

    A ``pending_data`` add starts the defer counter at 1 (this evaluation day is the first
    deferral). ``actual_value`` is stored as a canonical Decimal string (never float).
    """
    defer_count = 1 if status == "pending_data" else 0
    cur = conn.execute(
        "INSERT INTO insight_evaluations (insight_id, insight_type_id, calibration_version, "
        "is_shadow, status, quant_hit, narrative_score, miss, actual_value, confidence, "
        "defer_count, notes, evaluated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            insight_id,
            insight_type_id,
            calibration_version,
            1 if is_shadow else 0,
            status,
            None if quant_hit is None else (1 if quant_hit else 0),
            narrative_score,
            1 if miss else 0,
            None if actual_value is None else str(actual_value),
            confidence,
            defer_count,
            notes,
            now.isoformat(),
        ),
    )
    conn.commit()
    ev = get_evaluation(conn, int(cur.lastrowid or 0))
    assert ev is not None  # just inserted
    return ev


def get_evaluation(conn: sqlite3.Connection, eval_id: int) -> Evaluation | None:
    """Return one evaluation row by id, or None."""
    row = conn.execute(
        "SELECT * FROM insight_evaluations WHERE id = ?", (eval_id,)
    ).fetchone()
    return _eval_from_row(row) if row is not None else None


def latest_for_insight(conn: sqlite3.Connection, insight_id: int) -> Evaluation | None:
    """The most-recent evaluation row for an insight (the authoritative current state)."""
    row = conn.execute(
        "SELECT * FROM insight_evaluations WHERE insight_id = ? ORDER BY id DESC LIMIT 1",
        (insight_id,),
    ).fetchone()
    return _eval_from_row(row) if row is not None else None


# --- pending_data lifecycle (anti-poison) -------------------------------------


def bump_defer(
    conn: sqlite3.Connection, *, insight_id: int, insight_type_id: int,
    now: datetime | None = None,
) -> int:
    """Record another deferral for a still-unscoreable insight; return the new defer_count.

    Appends a fresh ``pending_data`` row carrying ``prior_max_defer + 1`` so the retry
    history is preserved (append-only). ``now`` is the injected clock (L7 fix — the
    evaluate pass threads its own ``now``); ``None`` falls back to wall-clock UTC.
    """
    row = conn.execute(
        "SELECT MAX(defer_count) AS m FROM insight_evaluations WHERE insight_id = ?",
        (insight_id,),
    ).fetchone()
    prior = int(row["m"]) if row is not None and row["m"] is not None else 0
    new_count = prior + 1
    stamp = (now if now is not None else datetime.now(UTC)).isoformat()
    conn.execute(
        "INSERT INTO insight_evaluations (insight_id, insight_type_id, calibration_version, "
        "is_shadow, status, quant_hit, narrative_score, miss, actual_value, confidence, "
        "defer_count, notes, evaluated_at) "
        "VALUES (?, ?, NULL, 0, 'pending_data', NULL, NULL, 0, NULL, NULL, ?, NULL, ?)",
        (insight_id, insight_type_id, new_count, stamp),
    )
    conn.commit()
    return new_count


def mark_undetermined(
    conn: sqlite3.Connection, *, insight_id: int, insight_type_id: int,
    now: datetime | None = None,
) -> None:
    """Append an ``undetermined`` terminal row (defer cap exceeded; excluded, never miss).

    ``now`` is the injected clock (L7 fix); ``None`` falls back to wall-clock UTC.
    """
    row = conn.execute(
        "SELECT MAX(defer_count) AS m FROM insight_evaluations WHERE insight_id = ?",
        (insight_id,),
    ).fetchone()
    prior = int(row["m"]) if row is not None and row["m"] is not None else 0
    stamp = (now if now is not None else datetime.now(UTC)).isoformat()
    conn.execute(
        "INSERT INTO insight_evaluations (insight_id, insight_type_id, calibration_version, "
        "is_shadow, status, quant_hit, narrative_score, miss, actual_value, confidence, "
        "defer_count, notes, evaluated_at) "
        "VALUES (?, ?, NULL, 0, 'undetermined', NULL, NULL, 0, NULL, NULL, ?, NULL, ?)",
        (insight_id, insight_type_id, prior, stamp),
    )
    conn.commit()


# --- due insights -------------------------------------------------------------


def due_insights(
    conn: sqlite3.Connection, *, now: datetime,
    exclude_type_ids: set[int] | None = None,
) -> list[DueInsight]:
    """Insights whose prediction has matured and which have NO terminal evaluation yet.

    A card is due when ``due_at`` is non-NULL and ``<= now`` and it has no ``scored`` or
    ``undetermined`` evaluation row (a ``pending_data`` row leaves it due — it must be
    retried). ``exclude_type_ids`` hides archived tasks' cards (L2 fix — the api layer
    feeds ``composer_store.archived_type_ids``, same pattern as :func:`ai_score`), so an
    archived task's matured cards stop consuming scoring (incl. master narrative cost).
    Reads the ``insights`` table read-only; tolerates its absence (returns []).
    """
    excl_sql = ""
    excl_params: list[Any] = []
    if exclude_type_ids:
        placeholders = ",".join("?" * len(exclude_type_ids))
        excl_sql = f"AND i.insight_type_id NOT IN ({placeholders}) "
        excl_params = list(exclude_type_ids)
    try:
        rows = conn.execute(
            "SELECT i.id AS id, i.insight_type_id AS insight_type_id, i.symbol AS symbol, "
            "i.calibration_version AS calibration_version, i.is_shadow AS is_shadow, "
            "i.confidence AS confidence, i.prediction AS prediction, i.due_at AS due_at, "
            "i.created_at AS created_at, i.price_at_create AS price_at_create "
            "FROM insights i "
            "WHERE i.due_at IS NOT NULL AND i.due_at <= ? "
            f"{excl_sql}"
            "AND NOT EXISTS (SELECT 1 FROM insight_evaluations e WHERE e.insight_id = i.id "
            "AND e.status IN ('scored', 'undetermined')) ORDER BY i.id",
            (now.isoformat(), *excl_params),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        DueInsight(
            insight_id=r["id"],
            insight_type_id=r["insight_type_id"],
            symbol=r["symbol"],
            calibration_version=r["calibration_version"],
            is_shadow=bool(r["is_shadow"]),
            confidence=r["confidence"],
            prediction=r["prediction"],
            due_at=r["due_at"],
            created_at=r["created_at"],
            price_at_create=r["price_at_create"],
        )
        for r in rows
    ]


# --- rollups (deterministic; spec 4.8 — LLM never decides these) --------------


def _ratio_str(num: int, den: int) -> str:
    """A num/den ratio as an exact 4-dp Decimal string (0 when den is 0)."""
    if den == 0:
        return "0"
    return str((Decimal(num) / Decimal(den)).quantize(Decimal("0.0001")))


def _avg_str(total: int, n: int) -> str:
    if n == 0:
        return "0"
    return str((Decimal(total) / Decimal(n)).quantize(Decimal("0.01")))


def combo_score(
    conn: sqlite3.Connection, insight_type_id: int, *, is_shadow: bool = False
) -> dict[str, Any]:
    """Accumulated score for one combo's scored, non-{pending,undetermined} rows.

    Defaults to the ACTIVE (non-shadow) rows; pass ``is_shadow=True`` for the shadow tally
    (Loop 4 promotion). Excludes ``pending_data``/``undetermined`` (anti-poison). Returns
    counts plus Decimal-string rates (never float).
    """
    rows = conn.execute(
        "SELECT quant_hit, narrative_score, miss FROM insight_evaluations "
        "WHERE insight_type_id = ? AND is_shadow = ? AND status = 'scored'",
        (insight_type_id, 1 if is_shadow else 0),
    ).fetchall()
    n = len(rows)
    miss_count = sum(1 for r in rows if r["miss"])
    quant_rows = [r for r in rows if r["quant_hit"] is not None]
    quant_hit_count = sum(1 for r in quant_rows if r["quant_hit"])
    narrative_rows = [r for r in rows if r["narrative_score"] is not None]
    narrative_sum = sum(int(r["narrative_score"]) for r in narrative_rows)
    return {
        "insight_type_id": insight_type_id,
        "is_shadow": is_shadow,
        "n": n,
        "miss_count": miss_count,
        "miss_rate": _ratio_str(miss_count, n),
        "quant_hit_count": quant_hit_count,
        "quant_n": len(quant_rows),
        "quant_hit_rate": _ratio_str(quant_hit_count, len(quant_rows)),
        "narrative_sum": narrative_sum,
        "avg_narrative": _avg_str(narrative_sum, len(narrative_rows)),
    }


def resolved_sample_count(
    conn: sqlite3.Connection, insight_type_id: int, *, is_shadow: bool = False
) -> int:
    """Count of RESOLVED (scored) rows for a combo — the min_samples gate denominator.

    Excludes ``pending_data`` and ``undetermined`` (spec 04.10): small/unresolved samples
    must not trigger calibration or calib_gap.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM insight_evaluations WHERE insight_type_id = ? "
        "AND is_shadow = ? AND status = 'scored'",
        (insight_type_id, 1 if is_shadow else 0),
    ).fetchone()
    return int(row["c"]) if row is not None else 0


def consecutive_misses(
    conn: sqlite3.Connection, insight_type_id: int, *, is_shadow: bool = False
) -> int:
    """The run of trailing misses among a combo's scored rows (newest first)."""
    rows = conn.execute(
        "SELECT miss FROM insight_evaluations WHERE insight_type_id = ? AND is_shadow = ? "
        "AND status = 'scored' ORDER BY id DESC",
        (insight_type_id, 1 if is_shadow else 0),
    ).fetchall()
    streak = 0
    for r in rows:
        if r["miss"]:
            streak += 1
        else:
            break
    return streak


def calibration_bins(
    conn: sqlite3.Connection, insight_type_id: int | None = None
) -> list[dict[str, Any]]:
    """Confidence-bucket calibration curve (claimed vs actual hit rate → calibration error).

    For each non-empty 0-20..80-100 confidence bucket: the average claimed confidence
    (``claimed_pct``) vs the actual hit rate (``actual_pct``, miss == not-hit), and their
    absolute difference in percentage points (``calibration_error_pp``). Active (non-shadow)
    scored rows with a stated confidence only. All percentages are Decimal STRINGS.
    """
    where = "is_shadow = 0 AND status = 'scored' AND confidence IS NOT NULL"
    params: tuple[Any, ...] = ()
    if insight_type_id is not None:
        where += " AND insight_type_id = ?"
        params = (insight_type_id,)
    rows = conn.execute(
        f"SELECT confidence, miss FROM insight_evaluations WHERE {where}", params
    ).fetchall()
    out: list[dict[str, Any]] = []
    for label, lo, hi in _BUCKETS:
        # Upper bound inclusive only for the top bucket so each row falls in exactly one.
        bucket = [
            r for r in rows
            if lo <= int(r["confidence"]) < hi or (hi == 100 and int(r["confidence"]) == 100)
        ]
        if not bucket:
            continue
        n = len(bucket)
        hit_count = sum(1 for r in bucket if not r["miss"])
        claimed = sum(int(r["confidence"]) for r in bucket) / n
        actual = (hit_count / n) * 100
        claimed_dec = Decimal(str(claimed)).quantize(Decimal("0.01"))
        actual_dec = Decimal(str(actual)).quantize(Decimal("0.01"))
        out.append({
            "bucket": label,
            "n": n,
            "hit_count": hit_count,
            "claimed_pct": str(claimed_dec),
            "actual_pct": str(actual_dec),
            "calibration_error_pp": str(abs(claimed_dec - actual_dec)),
        })
    return out


def scored_confidence_hits(conn: sqlite3.Connection) -> list[tuple[int, bool]]:
    """Portfolio-wide scored ``(confidence, hit)`` pairs feeding ``scoring.calibration_error``.

    Active (non-shadow), ``status = 'scored'`` rows with a stated confidence only; a row's
    hit is ``not miss``. This is the calibration-error input for the global ``calib_gap``
    alert rule (spec 03/04 I1) — the same anti-poison filter as ``calibration_bins``, but
    flattened across all combos (no per-bucket grouping). Returns ``[]`` when there are no
    such rows.
    """
    rows = conn.execute(
        "SELECT confidence, miss FROM insight_evaluations "
        "WHERE status = 'scored' AND is_shadow = 0 AND confidence IS NOT NULL"
    ).fetchall()
    return [(int(r["confidence"]), not bool(r["miss"])) for r in rows]


def miss_samples_for_version(
    conn: sqlite3.Connection, *, insight_type_id: int, version: int
) -> list[dict[str, Any]]:
    """The miss-evaluation samples recorded under a calibration version (spec 4.5 / 4.7).

    These are the failures that drive (or drove) a calibration version. Returns the raw
    sample dicts the master uses for the next version + the frontend's version manager.
    """
    from portfolio_dash.llm_insight import insights_store as istore  # no cycle: one-way

    istore.ensure_tables(conn)  # the LEFT JOIN needs the insights table to exist
    rows = conn.execute(
        "SELECT e.id, e.insight_id, e.narrative_score, e.quant_hit, e.confidence, "
        "e.actual_value, e.notes, e.evaluated_at, "
        "i.symbol AS card_symbol, i.title AS card_title, i.summary AS card_summary, "
        "i.prediction AS card_prediction "
        "FROM insight_evaluations e LEFT JOIN insights i ON i.id = e.insight_id "
        "WHERE e.insight_type_id = ? AND e.calibration_version = ? "
        "AND e.status = 'scored' AND e.miss = 1 ORDER BY e.id",
        (insight_type_id, version),
    ).fetchall()
    # Card context (symbol/title/summary/prediction) rides along so the Loop-3 master
    # rewrites rules from the ACTUAL failed claims, not just second-hand notes
    # (2026-07-05 audit §2.5: thin samples made calibration a game of telephone).
    return [
        {
            "id": r["id"],
            "insight_id": r["insight_id"],
            "narrative_score": r["narrative_score"],
            "quant_hit": _opt_bool(r["quant_hit"]),
            "confidence": r["confidence"],
            "actual_value": r["actual_value"],
            "notes": r["notes"],
            "evaluated_at": r["evaluated_at"],
            "card_symbol": r["card_symbol"],
            "card_title": r["card_title"],
            "card_summary": r["card_summary"],
            "card_prediction": r["card_prediction"],
        }
        for r in rows
    ]


def _row_wire(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"],
        "insight_id": r["insight_id"],
        "insight_type_id": r["insight_type_id"],
        "calibration_version": r["calibration_version"],
        "is_shadow": bool(r["is_shadow"]),
        "status": r["status"],
        "quant_hit": _opt_bool(r["quant_hit"]),
        "narrative_score": r["narrative_score"],
        "miss": bool(r["miss"]),
        "actual_value": r["actual_value"],
        "confidence": r["confidence"],
        "evaluated_at": r["evaluated_at"],
    }


def ai_score(
    conn: sqlite3.Connection,
    *,
    exclude_type_ids: set[int] | None = None,
    rows_limit: int | None = None,
    rows_offset: int = 0,
) -> dict[str, Any]:
    """The battle-record table (spec 4.7): ``{totals, by_combo[], calibration_bins[], rows[]}``.

    ``totals``/``by_combo`` reflect the DISPLAYED active score (non-shadow scored rows);
    shadow rows are excluded from the displayed totals but kept in ``rows`` (and reachable
    via ``combo_score(is_shadow=True)`` for promotion). ``exclude_type_ids`` hides archived
    tasks' history from the displayed record (spec 4.1 archive: rows stay in the table);
    ``calibration_bins`` stays global — it is a calibration diagnostic, not a scoreboard.
    Empty DB → zeroed/[] (the contract shape the frontend consumes).

    WPE (2026-07-07): ``rows`` pages via ``rows_limit``/``rows_offset`` (applied AFTER
    the archived-task exclusion so page boundaries are honest); the AGGREGATES always
    cover the whole set. ``rows_total_count`` reports the filtered total.
    ``rows_limit=None`` keeps the legacy everything-in-one shape for internal callers.
    """
    excluded = exclude_type_ids or set()
    combo_ids = [
        int(r["insight_type_id"])
        for r in conn.execute(
            "SELECT DISTINCT insight_type_id FROM insight_evaluations "
            "WHERE is_shadow = 0 AND status = 'scored' ORDER BY insight_type_id"
        )
        if int(r["insight_type_id"]) not in excluded
    ]
    by_combo = [combo_score(conn, cid) for cid in combo_ids]
    total_n = sum(c["n"] for c in by_combo)
    total_miss = sum(c["miss_count"] for c in by_combo)
    total_quant_hit = sum(c["quant_hit_count"] for c in by_combo)
    total_quant_n = sum(c["quant_n"] for c in by_combo)
    total_narr_sum = sum(c["narrative_sum"] for c in by_combo)
    total_narr_n = sum(
        1 for r in conn.execute(
            "SELECT insight_type_id FROM insight_evaluations WHERE is_shadow = 0 "
            "AND status = 'scored' AND narrative_score IS NOT NULL"
        )
        if int(r["insight_type_id"]) not in excluded
    )
    totals = {
        "n": total_n,
        "miss_count": total_miss,
        "miss_rate": _ratio_str(total_miss, total_n),
        "quant_hit_count": total_quant_hit,
        "quant_hit_rate": _ratio_str(total_quant_hit, total_quant_n),
        "avg_narrative": _avg_str(total_narr_sum, total_narr_n),
    }
    all_rows = [
        _row_wire(r)
        for r in conn.execute(
            "SELECT * FROM insight_evaluations ORDER BY id DESC"
        )
        if int(r["insight_type_id"]) not in excluded
    ]
    rows = all_rows if rows_limit is None else all_rows[rows_offset:rows_offset + rows_limit]
    return {
        "totals": totals,
        "by_combo": by_combo,
        "calibration_bins": calibration_bins(conn),
        "rows": rows,
        "rows_total_count": len(all_rows),
    }
