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
    # The last close the model actually saw at generation time (M4 fix, decision Q1c):
    # Loop-2 scores from THIS baseline instead of the first close AFTER create (a number
    # the model never reasoned about). Decimal STRING; None for legacy/portfolio cards.
    price_at_create: str | None = None
    # Per-card token usage (AI attribution, 2026-07-07): one LLM call = one card, so the
    # call's usage maps 1:1. Zero for legacy cards (the UI omits the token segment then).
    tokens_in: int = 0
    tokens_out: int = 0


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
    created_at TEXT NOT NULL,
    price_at_create TEXT
);
CREATE INDEX IF NOT EXISTS idx_insights_fingerprint ON insights (fingerprint);
CREATE INDEX IF NOT EXISTS idx_insights_type_symbol ON insights (insight_type_id, symbol);
"""


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, decl: str
) -> None:
    """Add ``column`` to ``table`` if absent (additive, idempotent migration).

    A LOCAL copy of the composer_store PRAGMA pattern (``llm_insight`` layering keeps
    these stores import-light). ``PRAGMA table_info`` row index 1 is the column name.
    """
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def ensure_tables(conn: sqlite3.Connection) -> None:
    """Create the ``insights`` table + indexes idempotently (create-always, no seed)."""
    conn.executescript(_DDL)
    # M4 fix (decision Q1c, 2026-07-07): additive migration — the seen-at-create price.
    _add_column_if_missing(conn, "insights", "price_at_create", "TEXT")
    # AI attribution (2026-07-07): per-card token usage for the unified model/token/cost line.
    _add_column_if_missing(conn, "insights", "tokens_in", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "insights", "tokens_out", "INTEGER NOT NULL DEFAULT 0")
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
    price_at_create: Decimal | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
) -> InsightRecord:
    """Append one generated card; compute ``due_at``; return the stored record.

    Append-only (spec 04 invariant): every run inserts a new row, never mutating history.
    ``cost_usd`` is stored as a canonical Decimal string. ``due_at`` is the prediction's
    maturity date (trading- or calendar-day horizon) or NULL for a narrative card.
    ``price_at_create`` (M4 fix) is the last close the model saw in its inputs — the
    Loop-2 scoring baseline; None when no close series was fed (portfolio/market card).
    """
    due_at = _compute_due_at(
        card, horizon_days=horizon_days, now=now, horizon_basis=horizon_basis
    )
    cur = conn.execute(
        "INSERT INTO insights (insight_type_id, symbol, is_shadow, calibration_version, "
        "fingerprint, title, summary, body_md, tags, confidence, prediction, "
        "horizon_days, due_at, input_snapshot, model, cost_usd, created_at, "
        "price_at_create, tokens_in, tokens_out) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            None if price_at_create is None else str(price_at_create),
            tokens_in,
            tokens_out,
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
        # Tolerate a pre-migration row shape (a reader hitting an old DB before
        # ensure_tables ran its ALTER).
        price_at_create=(
            row["price_at_create"] if "price_at_create" in row.keys() else None
        ),
        tokens_in=(row["tokens_in"] or 0) if "tokens_in" in row.keys() else 0,
        tokens_out=(row["tokens_out"] or 0) if "tokens_out" in row.keys() else 0,
    )


def _get(conn: sqlite3.Connection, insight_id: int) -> InsightRecord | None:
    row = conn.execute("SELECT * FROM insights WHERE id = ?", (insight_id,)).fetchone()
    return _record_from_row(row) if row is not None else None


def find_by_fingerprint(
    conn: sqlite3.Connection, fingerprint: str, *, is_shadow: bool = False
) -> InsightRecord | None:
    """Return the most-recent card stored under *fingerprint* IN THE GIVEN LANE, or None.

    A hit means identical inputs were already generated (same trading day) → reuse it,
    no LLM call (spec 04.10). Append-only history may hold several rows with the same
    fingerprint across days; the latest is returned. ``is_shadow`` scopes the lookup
    (L4 fix): the shadow lane (Loop 4) and the active lane must never cache-hit each
    other — a shadow card is a hidden calibration trial, not a substitute for the
    user-facing card (and vice versa).
    """
    row = conn.execute(
        "SELECT * FROM insights WHERE fingerprint = ? AND is_shadow = ? "
        "ORDER BY id DESC LIMIT 1",
        (fingerprint, 1 if is_shadow else 0),
    ).fetchone()
    return _record_from_row(row) if row is not None else None


def _exclusion_clause(
    exclude_type_ids: set[int] | None,
) -> tuple[str, list[Any]]:
    """SQL fragment + params hiding archived tasks' cards (empty set → no clause)."""
    if not exclude_type_ids:
        return "", []
    placeholders = ",".join("?" * len(exclude_type_ids))
    return f"insight_type_id NOT IN ({placeholders})", list(exclude_type_ids)


# Wire convention (2026-07-05 per_market spec): a per_market card stores the MARKET
# CODE in its symbol column, so "portfolio-style" cards = symbol NULL or a market code
# and "per-symbol" cards = any other non-null symbol. The scope filter below encodes
# that convention ONCE so paginated reads stay honest (WPE, 2026-07-07).
_MARKET_CODES = ("TW", "US", "MY")


def _scope_clause(scope: str | None) -> str | None:
    """SQL fragment for the ``scope`` filter ('portfolio' | 'symbol' | None)."""
    codes = ", ".join(f"'{c}'" for c in _MARKET_CODES)
    if scope == "portfolio":
        return f"(symbol IS NULL OR symbol IN ({codes}))"
    if scope == "symbol":
        return f"(symbol IS NOT NULL AND symbol NOT IN ({codes}))"
    return None


def _filters(
    *,
    insight_type_id: int | None,
    symbol: str | None,
    exclude_type_ids: set[int] | None,
    scope: str | None = None,
) -> tuple[str, list[Any]]:
    """Shared WHERE builder for the list/count reads (one filter definition)."""
    clauses: list[str] = []
    params: list[Any] = []
    if insight_type_id is not None:
        clauses.append("insight_type_id = ?")
        params.append(insight_type_id)
    if symbol is not None:
        clauses.append("symbol = ?")
        params.append(symbol)
    scope_sql = _scope_clause(scope)
    if scope_sql:
        clauses.append(scope_sql)
    excl_sql, excl_params = _exclusion_clause(exclude_type_ids)
    if excl_sql:
        clauses.append(excl_sql)
        params.extend(excl_params)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def list_cards(
    conn: sqlite3.Connection,
    *,
    insight_type_id: int | None = None,
    symbol: str | None = None,
    exclude_type_ids: set[int] | None = None,
    scope: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[InsightRecord]:
    """List stored cards (newest first), optionally filtered by type and/or symbol.

    ``exclude_type_ids`` hides archived tasks' history from read surfaces (the api
    layer feeds ``composer_store.archived_type_ids``); the rows stay in the table
    (spec 4.1 archive semantics — never physically removed). ``scope``/``limit``/
    ``offset`` (WPE): 'portfolio' keeps portfolio + per-market cards, 'symbol' keeps
    per-symbol health cards; ``limit=None`` returns everything (legacy callers).
    """
    where, params = _filters(
        insight_type_id=insight_type_id, symbol=symbol,
        exclude_type_ids=exclude_type_ids, scope=scope,
    )
    page = ""
    if limit is not None:
        page = " LIMIT ? OFFSET ?"
        params = [*params, limit, offset]
    rows = conn.execute(
        f"SELECT * FROM insights{where} ORDER BY id DESC{page}", tuple(params)
    ).fetchall()
    return [_record_from_row(r) for r in rows]


def count_cards(
    conn: sqlite3.Connection,
    *,
    insight_type_id: int | None = None,
    symbol: str | None = None,
    exclude_type_ids: set[int] | None = None,
    scope: str | None = None,
) -> int:
    """COUNT over the same filter set as :func:`list_cards` (honest pager totals)."""
    where, params = _filters(
        insight_type_id=insight_type_id, symbol=symbol,
        exclude_type_ids=exclude_type_ids, scope=scope,
    )
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM insights{where}", tuple(params)
    ).fetchone()
    return int(row["n"])


def list_symbol_groups(
    conn: sqlite3.Connection,
    *,
    history_limit: int,
    limit: int,
    offset: int,
    exclude_type_ids: set[int] | None = None,
) -> tuple[list[tuple[str, int, list[InsightRecord]]], int]:
    """持倉健診 grouping (WPE): per-symbol latest + capped history, paged over SYMBOLS.

    Returns ``([(symbol, symbol_total, cards<=history_limit newest-first)], total_symbols)``.
    Symbols are ordered by their LATEST card (id desc) so the most recently diagnosed
    holding leads. Grouping lives server-side because pagination is over symbols —
    a client slice of a flat card feed cannot know symbol boundaries honestly.
    """
    where, params = _filters(
        insight_type_id=None, symbol=None,
        exclude_type_ids=exclude_type_ids, scope="symbol",
    )
    total_row = conn.execute(
        f"SELECT COUNT(DISTINCT symbol) AS n FROM insights{where}", tuple(params)
    ).fetchone()
    total_symbols = int(total_row["n"])
    sym_rows = conn.execute(
        f"SELECT symbol, MAX(id) AS latest_id, COUNT(*) AS total FROM insights{where} "
        "GROUP BY symbol ORDER BY latest_id DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    groups: list[tuple[str, int, list[InsightRecord]]] = []
    for sr in sym_rows:
        cards = list_cards(
            conn, symbol=str(sr["symbol"]), exclude_type_ids=exclude_type_ids,
            scope="symbol", limit=history_limit, offset=0,
        )
        groups.append((str(sr["symbol"]), int(sr["total"]), cards))
    return groups, total_symbols


def latest_cards(
    conn: sqlite3.Connection, n: int, *, exclude_type_ids: set[int] | None = None
) -> list[InsightRecord]:
    """The latest *n* non-shadow cards, newest first (spec 4.6 / 08 dashboard embed).

    Ordered ``created_at`` desc with an ``id`` desc tiebreak for determinism (two cards
    stamped from the same run share a ``created_at``). Shadow cards (``is_shadow = 1``)
    are excluded — they are hidden calibration trials, never surfaced on the dashboard
    (spec 4.6) — as are archived tasks' cards via ``exclude_type_ids``. An empty table
    (or non-positive *n*) yields an empty list.
    """
    excl_sql, excl_params = _exclusion_clause(exclude_type_ids)
    where = "is_shadow = 0" + (f" AND {excl_sql}" if excl_sql else "")
    rows = conn.execute(
        f"SELECT * FROM insights WHERE {where} "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (*excl_params, n),
    ).fetchall()
    return [_record_from_row(r) for r in rows]
