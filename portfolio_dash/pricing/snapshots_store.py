"""Append-only ``external_snapshots`` store (spec 20.4).

Chips / sentiment / index data are *decision-support signals*, not numbers of
record. Their raw provider responses are persisted here append-only (re-fetching
never deletes or overwrites; the newest ``fetched_at`` wins on read) so a backtest
can reproduce "the inputs as seen at the time". Derivation into variable values is
done by pure functions in ``portfolio/external_signals.py`` — never here.

The table lives in ``pricing/`` because that is the module that fetches external
data. Reads and writes are idempotent-safe; payloads are stored as canonical JSON
strings (``Decimal`` discipline is applied in the derivation layer, not at storage).
"""

import json
import sqlite3
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel

_DDL = """
CREATE TABLE IF NOT EXISTS external_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  dataset TEXT NOT NULL,
  symbol TEXT,
  as_of TEXT NOT NULL,
  payload TEXT NOT NULL,
  fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_external_snapshots_lookup
  ON external_snapshots (source, dataset, symbol, as_of);
"""


class Snapshot(BaseModel):
    """One persisted external snapshot row (payload parsed back to a dict)."""

    source: str
    dataset: str
    symbol: str | None
    as_of: date
    payload: dict[str, Any]
    fetched_at: datetime


def ensure_tables(conn: sqlite3.Connection) -> None:
    """Create the ``external_snapshots`` table + lookup index idempotently."""
    conn.executescript(_DDL)
    conn.commit()


def add_snapshot(
    conn: sqlite3.Connection,
    *,
    source: str,
    dataset: str,
    symbol: str | None,
    as_of: date,
    payload: dict[str, Any],
    fetched_at: datetime,
) -> None:
    """Append one snapshot row (never deletes/overwrites; latest fetched_at wins on read)."""
    conn.execute(
        "INSERT INTO external_snapshots (source, dataset, symbol, as_of, payload, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            source,
            dataset,
            symbol,
            as_of.isoformat(),
            json.dumps(payload, ensure_ascii=False),
            fetched_at.isoformat(),
        ),
    )
    conn.commit()


def _row_to_snapshot(row: sqlite3.Row) -> Snapshot:
    return Snapshot(
        source=row["source"],
        dataset=row["dataset"],
        symbol=row["symbol"],
        as_of=date.fromisoformat(row["as_of"]),
        payload=json.loads(row["payload"]),
        fetched_at=datetime.fromisoformat(row["fetched_at"]),
    )


def _symbol_clause(symbol: str | None) -> tuple[str, tuple[Any, ...]]:
    """SQL fragment + params matching ``symbol`` exactly (NULL-safe)."""
    if symbol is None:
        return "symbol IS NULL", ()
    return "symbol = ?", (symbol,)


def latest_snapshot(
    conn: sqlite3.Connection, *, source: str, dataset: str, symbol: str | None
) -> Snapshot | None:
    """Newest-fetched snapshot for the (source, dataset, symbol) key, or None."""
    clause, params = _symbol_clause(symbol)
    row = conn.execute(
        f"SELECT * FROM external_snapshots "
        f"WHERE source = ? AND dataset = ? AND {clause} "
        f"ORDER BY fetched_at DESC, id DESC LIMIT 1",
        (source, dataset, *params),
    ).fetchone()
    return _row_to_snapshot(row) if row is not None else None


def snapshot_on_or_before(
    conn: sqlite3.Connection, *, source: str, dataset: str, symbol: str | None, as_of: date
) -> Snapshot | None:
    """Newest snapshot whose ``as_of`` is on-or-before *as_of* for the key, or None.

    Ordered by ``as_of`` DESC (then newest ``fetched_at`` to collapse re-fetches), so this
    answers "the consensus as it stood at-or-before date D". Used by the ``consensus_change``
    alert to fetch both the LATEST snapshot (``as_of = today``) and the closest baseline
    ``>= 7 days`` older (``as_of = latest.as_of - 7d``); ISO date strings sort chronologically.
    """
    clause, params = _symbol_clause(symbol)
    row = conn.execute(
        f"SELECT * FROM external_snapshots "
        f"WHERE source = ? AND dataset = ? AND {clause} AND as_of <= ? "
        f"ORDER BY as_of DESC, fetched_at DESC, id DESC LIMIT 1",
        (source, dataset, *params, as_of.isoformat()),
    ).fetchone()
    return _row_to_snapshot(row) if row is not None else None


def latest_series(
    conn: sqlite3.Connection, *, source: str, dataset: str, symbol: str | None, n: int
) -> list[Snapshot]:
    """Up to ``n`` newest distinct ``as_of`` snapshots, newest first.

    Collapses re-fetches: one row per ``as_of`` (its newest ``fetched_at``). Used by
    derivations that need a trailing window (e.g. last-N daily net buy).
    """
    if n <= 0:
        return []
    clause, params = _symbol_clause(symbol)
    rows = conn.execute(
        f"SELECT * FROM external_snapshots AS e "
        f"WHERE source = ? AND dataset = ? AND {clause} "
        f"AND fetched_at = ("
        f"  SELECT MAX(e2.fetched_at) FROM external_snapshots AS e2 "
        f"  WHERE e2.source = e.source AND e2.dataset = e.dataset "
        f"  AND e2.as_of = e.as_of AND e2.symbol IS e.symbol"
        f") "
        f"ORDER BY as_of DESC LIMIT ?",
        (source, dataset, *params, n),
    ).fetchall()
    return [_row_to_snapshot(r) for r in rows]
