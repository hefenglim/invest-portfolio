"""Insights persistence (spec 04.10): append-only cards + fingerprint cache + due_at.

Owns the ``insights`` table. A generated card is keyed by a deterministic input
fingerprint (``sha256(insight_type_id + assembled prompt + input-snapshot digest +
prompt_version)``); because the snapshot digest carries the snapshot DATE, the fingerprint
is naturally distinct each trading day, so re-triggering the SAME inputs on the SAME day is
a cache hit (zero LLM, spec 04.10). ``due_at`` is the prediction maturity date (trading- or
calendar-day horizon from the card's prediction, overriding the task default); a
pure-narrative card has no prediction → ``due_at`` is NULL.

Pure persistence over its own table: stdlib + pydantic + ``llm_insight.cards`` only (NOT
pricing / data_ingestion / api / scheduler — ``architecture.md``). No money in float —
``cost_usd`` and ``target_pct`` persist as canonical Decimal strings via the wire encoder.
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel

from portfolio_dash.llm_insight.cards import InsightCard, Prediction
from portfolio_dash.shared.wire import to_wire

HorizonBasis = Literal["trading_days", "calendar_days"]


class InsightRecord(BaseModel):
    """A stored insight row: the card payload plus its persistence metadata."""

    id: int
    insight_type_id: int
    symbol: str | None
    is_shadow: bool
    calibration_version: int | None
    fingerprint: str
    card: InsightCard
    horizon_days: int
    due_at: str | None
    input_snapshot: str
    model: str
    cost_usd: str
    created_at: str


_DDL = """
CREATE TABLE IF NOT EXISTS insights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    insight_type_id INTEGER NOT NULL,
    symbol TEXT,
    is_shadow INTEGER NOT NULL DEFAULT 0,
    calibration_version INTEGER,
    fingerprint TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    body_md TEXT NOT NULL,
    tags TEXT NOT NULL,
    confidence INTEGER,
    prediction TEXT,
    horizon_days INTEGER NOT NULL,
    due_at TEXT,
    input_snapshot TEXT NOT NULL,
    model TEXT NOT NULL,
    cost_usd TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_insights_fingerprint ON insights (fingerprint);
CREATE INDEX IF NOT EXISTS idx_insights_type_symbol ON insights (insight_type_id, symbol);
"""


def ensure_tables(conn: sqlite3.Connection) -> None:
    """Create the ``insights`` table + indexes idempotently (create-always, no seed)."""
    conn.executescript(_DDL)
    conn.commit()


# --- fingerprint --------------------------------------------------------------


def fingerprint(
    insight_type_id: int, assembled: str, snapshot_digest: str, prompt_version: str
) -> str:
    """Deterministic cache key for a generation run (spec 04.10).

    ``sha256`` over the combo id + the fully-assembled prompt + the input-snapshot digest +
    the prompt version. The components are joined with a NUL separator so distinct field
    boundaries cannot collide. Returns a 64-char lowercase hex digest.
    """
    raw = "\x00".join((str(insight_type_id), assembled, snapshot_digest, prompt_version))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def snapshot_digest(snapshot: str) -> str:
    """A short stable digest of an input-snapshot JSON string (for the fingerprint)."""
    return hashlib.sha256(snapshot.encode("utf-8")).hexdigest()


# --- trading-day horizon ------------------------------------------------------


def add_trading_days(start: datetime, days: int) -> datetime:
    """Return *start* advanced by *days* trading days (weekday skip; Mon–Fri only).

    Market holidays are out of scope for v1 (documented): a holiday-aware calendar would
    require a per-market holiday table, deferred. A non-positive *days* returns *start*.
    """
    if days <= 0:
        return start
    current = start
    added = 0
    while added < days:
        current = current + timedelta(days=1)
        if current.weekday() < 5:  # 0=Mon .. 4=Fri
            added += 1
    return current


def _compute_due_at(
    card: InsightCard, *, horizon_days: int, now: datetime, horizon_basis: HorizonBasis
) -> str | None:
    """The maturity timestamp for a card's prediction, or None for a narrative card.

    The card's ``prediction.horizon_days`` overrides the task default when present.
    """
    if card.prediction is None:
        return None
    h = card.prediction.horizon_days or horizon_days
    if horizon_basis == "calendar_days":
        return (now + timedelta(days=h)).isoformat()
    return add_trading_days(now, h).isoformat()


# --- add + read ---------------------------------------------------------------


def _prediction_json(prediction: Prediction | None) -> str | None:
    if prediction is None:
        return None
    return json.dumps(to_wire(prediction.model_dump()), ensure_ascii=False)


def add_card(
    conn: sqlite3.Connection,
    *,
    insight_type_id: int,
    card: InsightCard,
    fingerprint: str,
    calibration_version: int | None,
    horizon_days: int,
    input_snapshot: str,
    model: str,
    cost_usd: Decimal,
    now: datetime,
    is_shadow: bool = False,
    horizon_basis: HorizonBasis = "trading_days",
) -> InsightRecord:
    """Append one generated card; compute ``due_at``; return the stored record.

    Append-only (spec 04 invariant): every run inserts a new row, never mutating history.
    ``cost_usd`` is stored as a canonical Decimal string. ``due_at`` is the prediction's
    maturity date (trading- or calendar-day horizon) or NULL for a narrative card.
    """
    due_at = _compute_due_at(
        card, horizon_days=horizon_days, now=now, horizon_basis=horizon_basis
    )
    cur = conn.execute(
        "INSERT INTO insights (insight_type_id, symbol, is_shadow, calibration_version, "
        "fingerprint, title, summary, body_md, tags, confidence, prediction, "
        "horizon_days, due_at, input_snapshot, model, cost_usd, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            insight_type_id,
            card.symbol,
            1 if is_shadow else 0,
            calibration_version,
            fingerprint,
            card.title,
            card.summary,
            card.body_md,
            json.dumps(card.tags, ensure_ascii=False),
            card.confidence,
            _prediction_json(card.prediction),
            horizon_days,
            due_at,
            input_snapshot,
            model,
            str(cost_usd),
            now.isoformat(),
        ),
    )
    conn.commit()
    rec = _get(conn, int(cur.lastrowid or 0))
    assert rec is not None  # just inserted
    return rec


def _card_from_row(row: sqlite3.Row) -> InsightCard:
    prediction = (
        Prediction.model_validate_json(row["prediction"])
        if row["prediction"] is not None
        else None
    )
    return InsightCard(
        title=row["title"],
        summary=row["summary"],
        body_md=row["body_md"],
        tags=json.loads(row["tags"]),
        symbol=row["symbol"],
        confidence=row["confidence"],
        prediction=prediction,
    )


def _record_from_row(row: sqlite3.Row) -> InsightRecord:
    return InsightRecord(
        id=row["id"],
        insight_type_id=row["insight_type_id"],
        symbol=row["symbol"],
        is_shadow=bool(row["is_shadow"]),
        calibration_version=row["calibration_version"],
        fingerprint=row["fingerprint"],
        card=_card_from_row(row),
        horizon_days=row["horizon_days"],
        due_at=row["due_at"],
        input_snapshot=row["input_snapshot"],
        model=row["model"],
        cost_usd=row["cost_usd"],
        created_at=row["created_at"],
    )


def _get(conn: sqlite3.Connection, insight_id: int) -> InsightRecord | None:
    row = conn.execute("SELECT * FROM insights WHERE id = ?", (insight_id,)).fetchone()
    return _record_from_row(row) if row is not None else None


def find_by_fingerprint(conn: sqlite3.Connection, fingerprint: str) -> InsightRecord | None:
    """Return the most-recent card stored under *fingerprint*, or None (cache miss).

    A hit means identical inputs were already generated (same trading day) → reuse it,
    no LLM call (spec 04.10). Append-only history may hold several rows with the same
    fingerprint across days; the latest is returned.
    """
    row = conn.execute(
        "SELECT * FROM insights WHERE fingerprint = ? ORDER BY id DESC LIMIT 1",
        (fingerprint,),
    ).fetchone()
    return _record_from_row(row) if row is not None else None


def list_cards(
    conn: sqlite3.Connection,
    *,
    insight_type_id: int | None = None,
    symbol: str | None = None,
) -> list[InsightRecord]:
    """List stored cards (newest first), optionally filtered by type and/or symbol."""
    clauses: list[str] = []
    params: list[Any] = []
    if insight_type_id is not None:
        clauses.append("insight_type_id = ?")
        params.append(insight_type_id)
    if symbol is not None:
        clauses.append("symbol = ?")
        params.append(symbol)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM insights{where} ORDER BY id DESC", tuple(params)
    ).fetchall()
    return [_record_from_row(r) for r in rows]
