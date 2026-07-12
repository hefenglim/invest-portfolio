"""Per-symbol target-weight config (Blueprint P3 batch 2, owner ruling D8 2026-07-12).

A single-row JSON config (``target_weights_config``) mapping a REGISTERED symbol to its
target reporting-currency weight as a **Decimal-string RATIO** (``"0.25"`` = 25%). This is
the SINGLE source of truth for two consumers: the ``rebalance_drift`` alert rule
(strategy/alerts) and the rebalance-preview drawer's default prefill (both are FED the
same stored ratios — never a second copy).

Weights are ratios, not money, so the 2-dp money rule never applies — they are stored at
4-dp ratio precision (``data-and-pricing.md``). An absent / empty map means "no targets
set"; a symbol absent from the map is simply un-targeted (the drift rule stays silent for
it). Validation (each weight ∈ (0,1], Σ ≤ 1, symbol is registered) lives at the API write
seam — this store is pure persistence and imports only ``shared.config_store`` (no
pricing / llm_insight), mirroring ``strategy.rules_config``.
"""

import json
import sqlite3
from datetime import datetime
from decimal import Decimal

from portfolio_dash.shared import config_store

_CATEGORY = "target_weights"
_DDL = (
    "CREATE TABLE IF NOT EXISTS target_weights_config "
    "(id INTEGER PRIMARY KEY CHECK (id = 1), weights_json TEXT NOT NULL, updated_at TEXT)"
)


def _create(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO target_weights_config (id, weights_json, updated_at) "
        "VALUES (1, '{}', NULL)"
    )


def ensure_target_weights_seeded(conn: sqlite3.Connection) -> None:
    """Create the single-row target-weights table (always) and seed it empty (once)."""
    config_store.ensure_seeded(conn, _CATEGORY, create=_create, seed=_seed)


def load_target_weights(conn: sqlite3.Connection) -> dict[str, Decimal]:
    """Read the stored target weights as ``{symbol: Decimal ratio}`` ({} when unset).

    Degrades to ``{}`` when the table is absent (a fresh DB before bootstrap seeds it) so a
    dashboard/alerts read never crashes — matches the ``account_display_names`` discipline.
    """
    try:
        row = conn.execute(
            "SELECT weights_json FROM target_weights_config WHERE id = 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return {}
    if row is None or row[0] is None:
        return {}
    raw = json.loads(row[0])
    return {str(sym): Decimal(str(val)) for sym, val in raw.items()}


def save_target_weights(
    conn: sqlite3.Connection, weights: dict[str, Decimal], *, now: datetime
) -> None:
    """Persist *weights* (single-row upsert). Values stored as canonical Decimal strings.

    The caller has already validated (registered symbols, each ∈ (0,1], Σ ≤ 1); this only
    writes. An empty map is a valid state (clears all targets).
    """
    ensure_target_weights_seeded(conn)
    payload = {sym: str(w) for sym, w in weights.items()}
    conn.execute(
        "INSERT INTO target_weights_config (id, weights_json, updated_at) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET weights_json = excluded.weights_json, "
        "updated_at = excluded.updated_at",
        (json.dumps(payload), now.isoformat()),
    )
    conn.commit()


def get_updated_at(conn: sqlite3.Connection) -> str | None:
    """ISO timestamp of the last save, or None when never saved / table absent."""
    try:
        row = conn.execute(
            "SELECT updated_at FROM target_weights_config WHERE id = 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or row[0] is None:
        return None
    return str(row[0])
