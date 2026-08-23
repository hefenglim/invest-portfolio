"""Per-day signal state history (W6, AI-D27/AI-D28) — the event-study substrate.

``signal_states`` keeps one row per symbol (the LATEST derived state); this table keeps one
row per symbol **per price-data date** — the full state vector (four rule states + scores,
tech_score, evaluation_context) so "how did TechScore get here" is answerable and the
event study (``portfolio/backtest.py``) has something to study. Rows are written by the
``signal_scan`` orchestration in the api seam (``api/signals_service.py``): the daily row
from the evaluation the scan already computed, and history depth from **replay** — the
rules engine is a pure, params-stamped function, so re-evaluating a historical date through
the same input assembly deterministically rebuilds the row the scan would have written that
day (AI-D27).

Like ``signal_states`` this is a **derived cache, not a source of truth**: the truth stays
in ``prices`` + ``corporate_actions``; deleting a symbol's rows and re-scanning reproduces
them. That is also the invalidation rule — a corporate action that re-expresses prices
deletes BOTH tables' rows for the symbol (the reconcile seam), because a state computed
under the old basis is not merely stale, it is wrong.

Known limitation (W6 review, 2026-08-21): the missing-set fill only fills ABSENT dates and
the head refresh only re-evaluates the last computable date, so a provider **revision**
mid-series (a corrected close on an already-stored date, or a filled gap that shifts the
trailing windows of later dates) leaves the downstream stored rows stale — nothing
automatic detects a changed close. The remedy is the derived-cache doctrine above:
``delete_symbol`` + the next scan rebuilds from the truth.

Layer note: mirrors ``strategy/signal_states.py`` — conn-bearing persistence lives directly
in ``strategy/``; this module imports only stdlib + the rule types; it does NOT import
``llm_insight`` / ``api`` / ``web`` (architecture.md #4). The orchestration (which dates to
fill, when to write) lives in the api seam.
"""

import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from portfolio_dash.strategy.rules.composite import RULE_ORDER
from portfolio_dash.strategy.rules.types import SymbolSignals


@dataclass(frozen=True)
class SignalHistoryRow:
    """One stored ``signal_history`` row: the full state vector for one price-data date.

    ``as_of`` is the **price-data date** the evaluation describes (the last close's date),
    never the scan's wall-clock date — a holiday re-scan rewrites the same natural key
    (AI-D28). Score columns carry canonical full-precision Decimal TEXT (a cache value,
    not money of record; ``format(d, "f")`` precedent from ``signal_states``). A rule that
    was not evaluable that day stores ``None`` state AND score — honest absence, and the
    event study reads missing signs as non-events.
    """

    symbol: str
    as_of: date
    trend_state: str | None
    trend_score: Decimal | None
    cross_state: str | None
    cross_score: Decimal | None
    momentum_state: str | None
    momentum_score: Decimal | None
    rsi_state: str | None
    rsi_score: Decimal | None
    tech_score: Decimal | None
    evaluation_context: str | None
    params_version: str
    updated_at: str


def row_from_signals(
    symbol: str,
    signals: SymbolSignals,
    *,
    as_of: date,
    updated_at: str,
) -> SignalHistoryRow:
    """Extract a history row from an engine evaluation (None-safe per rule).

    Unlike ``signal_states.extract_state`` this keeps ALL FOUR rules (rsi included) and the
    signed SCORES — the event study detects rule events purely numerically from the stored
    scores (sign vs the last non-zero sign), so the scores are the load-bearing columns.
    """
    def _rule(name: str) -> tuple[str | None, Decimal | None]:
        rule = signals.rules.get(name)
        return (rule.state, rule.score) if rule is not None else (None, None)

    trend_s, trend_sc = _rule("trend_filter")
    cross_s, cross_sc = _rule("ma_cross")
    momentum_s, momentum_sc = _rule("momentum_12_1")
    rsi_s, rsi_sc = _rule("rsi_regime")
    composite = signals.composite
    return SignalHistoryRow(
        symbol=symbol,
        as_of=as_of,
        trend_state=trend_s,
        trend_score=trend_sc,
        cross_state=cross_s,
        cross_score=cross_sc,
        momentum_state=momentum_s,
        momentum_score=momentum_sc,
        rsi_state=rsi_s,
        rsi_score=rsi_sc,
        tech_score=composite.tech_score if composite is not None else None,
        evaluation_context=(
            composite.evaluation_context if composite is not None else None
        ),
        params_version=signals.params_version,
        updated_at=updated_at,
    )


# --- conn-bearing store (derived cache; rebuildable) --------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS signal_history (
    symbol TEXT NOT NULL,
    as_of TEXT NOT NULL,
    trend_state TEXT,
    trend_score TEXT,
    cross_state TEXT,
    cross_score TEXT,
    momentum_state TEXT,
    momentum_score TEXT,
    rsi_state TEXT,
    rsi_score TEXT,
    tech_score TEXT,
    evaluation_context TEXT,
    params_version TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (symbol, as_of)
);
"""

_CONTENT_COLUMNS = (
    "trend_state", "trend_score", "cross_state", "cross_score",
    "momentum_state", "momentum_score", "rsi_state", "rsi_score",
    "tech_score", "evaluation_context", "params_version",
)

_COLUMNS = (
    "symbol, as_of, " + ", ".join(_CONTENT_COLUMNS) + ", updated_at"
)


def ensure_table(conn: sqlite3.Connection) -> None:
    """Create the ``signal_history`` derived-cache table if missing (idempotent)."""
    conn.executescript(_DDL)
    conn.commit()


def _fmt(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _row_params(row: SignalHistoryRow) -> tuple[object, ...]:
    return (
        row.symbol, row.as_of.isoformat(),
        row.trend_state, _fmt(row.trend_score),
        row.cross_state, _fmt(row.cross_score),
        row.momentum_state, _fmt(row.momentum_score),
        row.rsi_state, _fmt(row.rsi_score),
        _fmt(row.tech_score), row.evaluation_context,
        row.params_version, row.updated_at,
    )


_UPSERT_SQL = (
    f"INSERT INTO signal_history ({_COLUMNS}) "
    f"VALUES ({', '.join('?' for _ in _COLUMNS.split(','))}) "
    "ON CONFLICT(symbol, as_of) DO UPDATE SET "
    + ", ".join(f"{c}=excluded.{c}" for c in _CONTENT_COLUMNS)
    + ", updated_at=excluded.updated_at"
)


def upsert_rows(conn: sqlite3.Connection, rows: list[SignalHistoryRow]) -> int:
    """Batch-upsert rows keyed on ``(symbol, as_of)`` with ONE commit for the batch.

    The replay backfill writes ~1,300 rows per symbol — the per-row commit of
    ``signal_states.upsert_state`` would turn that into ~1,300 fsyncs. Values are
    deterministic given the ledgers, so a rewrite after a ledger change is the rebuild
    path working as designed. Returns the number of rows written.
    """
    if not rows:
        return 0
    conn.executemany(_UPSERT_SQL, [_row_params(r) for r in rows])
    conn.commit()
    return len(rows)


def upsert_row_if_changed(conn: sqlite3.Connection, row: SignalHistoryRow) -> bool:
    """Write the daily row only when its CONTENT differs from the stored row.

    Compare-then-skip (the ``reconcile_prices`` discipline): the cron scans every weekday
    and a re-scan with unchanged inputs must be a no-op — including ``updated_at`` — so an
    idempotency assertion can compare the FULL row, provenance included. Returns True when
    a write happened.
    """
    existing = conn.execute(
        f"SELECT {_COLUMNS} FROM signal_history WHERE symbol = ? AND as_of = ?",
        (row.symbol, row.as_of.isoformat()),
    ).fetchone()
    if existing is not None:
        new = _row_params(row)
        stored = tuple(existing)
        if all(new[i] == stored[i] for i in range(2, 2 + len(_CONTENT_COLUMNS))):
            return False
    conn.execute(_UPSERT_SQL, _row_params(row))
    conn.commit()
    return True


def _to_row(row: sqlite3.Row) -> SignalHistoryRow:
    def _dec(key: str) -> Decimal | None:
        value = row[key]
        return Decimal(value) if value is not None else None

    return SignalHistoryRow(
        symbol=row["symbol"],
        as_of=date.fromisoformat(row["as_of"]),
        trend_state=row["trend_state"],
        trend_score=_dec("trend_score"),
        cross_state=row["cross_state"],
        cross_score=_dec("cross_score"),
        momentum_state=row["momentum_state"],
        momentum_score=_dec("momentum_score"),
        rsi_state=row["rsi_state"],
        rsi_score=_dec("rsi_score"),
        tech_score=_dec("tech_score"),
        evaluation_context=row["evaluation_context"],
        params_version=row["params_version"],
        updated_at=row["updated_at"],
    )


def list_rows(
    conn: sqlite3.Connection,
    symbol: str,
    *,
    params_version: str | None = None,
) -> list[SignalHistoryRow]:
    """All stored rows for ``symbol``, ascending by ``as_of``.

    ``params_version`` filters to one rule vintage — the event study passes the CURRENT
    version so a future ``rules-v2`` recalibration never mixes two vintages into one study
    (the scan prunes stale-version rows; the filter is the second belt).
    """
    sql = f"SELECT {_COLUMNS} FROM signal_history WHERE symbol = ?"
    params: list[object] = [symbol]
    if params_version is not None:
        sql += " AND params_version = ?"
        params.append(params_version)
    rows = conn.execute(sql + " ORDER BY as_of ASC", params).fetchall()
    return [_to_row(r) for r in rows]


def as_of_set(conn: sqlite3.Connection, symbol: str) -> set[date]:
    """The stored ``as_of`` dates for ``symbol`` — the scan's missing-set subtraction."""
    rows = conn.execute(
        "SELECT as_of FROM signal_history WHERE symbol = ?", (symbol,)
    ).fetchall()
    return {date.fromisoformat(r["as_of"]) for r in rows}


def delete_symbol(conn: sqlite3.Connection, symbol: str) -> int:
    """Delete every history row for ``symbol``; returns the count deleted.

    Called from the corporate-action reconcile seam (a restated price basis makes every
    stored evaluation of the symbol wrong, not merely stale) and from instrument purge.
    Degrades to ``0`` on a ledger-only database where the table was never created
    (architecture.md cross-layer-read obligation #2 — a missing table is "no rows", never
    an OperationalError).
    """
    try:
        cur = conn.execute("DELETE FROM signal_history WHERE symbol = ?", (symbol,))
    except sqlite3.OperationalError:
        return 0
    conn.commit()
    return cur.rowcount


def prune_params_version(conn: sqlite3.Connection, current: str) -> int:
    """Delete rows of any OTHER params vintage; returns the count deleted.

    A future ``rules-v2`` recalibration changes the state/score of the SAME dates; keeping
    the old rows would silently mix two rule vintages into one event study. The scan calls
    this before computing the missing set, so pruned dates refill from replay on the same
    pass. A no-op while ``rules-v1`` is the only vintage.
    """
    cur = conn.execute(
        "DELETE FROM signal_history WHERE params_version != ?", (current,)
    )
    conn.commit()
    return cur.rowcount


__all__ = [
    "RULE_ORDER",
    "SignalHistoryRow",
    "as_of_set",
    "delete_symbol",
    "ensure_table",
    "list_rows",
    "prune_params_version",
    "row_from_signals",
    "upsert_row_if_changed",
    "upsert_rows",
]
