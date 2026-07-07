"""UI preferences (WPC, 2026-07-07): a tiny DB-backed single-row config.

Backend-persisted global display preferences shared by every pager surface —
currently just ``page_size``. Follows the ``config_store`` create-always/seed-once
pattern (same shape as ``system_prompt_config``: one row, id=1). Lives in
``shared/`` (imports nothing internal beyond ``config_store``) so any layer may
read it; only the api router writes it.
"""

import sqlite3
from datetime import datetime

from portfolio_dash.shared import config_store

_CATEGORY = "ui_prefs"

DEFAULT_PAGE_SIZE = 50
ALLOWED_PAGE_SIZES = (20, 50, 100, 200)

_DDL = (
    "CREATE TABLE IF NOT EXISTS ui_prefs_config "
    "(id INTEGER PRIMARY KEY CHECK (id = 1), page_size INTEGER NOT NULL, "
    "updated_at TEXT NOT NULL)"
)


def _create(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO ui_prefs_config (id, page_size, updated_at) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO NOTHING",
        (DEFAULT_PAGE_SIZE, datetime(2026, 7, 7).isoformat()),
    )


def ensure_ui_prefs_seeded(conn: sqlite3.Connection) -> None:
    """Create the single-row table (always) and seed the default (once)."""
    config_store.ensure_seeded(conn, _CATEGORY, create=_create, seed=_seed)


def get_ui_prefs(conn: sqlite3.Connection) -> dict[str, int]:
    """Return ``{"page_size": N}``; falls back to the default when the row is absent."""
    ensure_ui_prefs_seeded(conn)
    row = conn.execute("SELECT page_size FROM ui_prefs_config WHERE id = 1").fetchone()
    page_size = int(row["page_size"]) if row is not None else DEFAULT_PAGE_SIZE
    if page_size not in ALLOWED_PAGE_SIZES:  # defensive: legacy/hand-edited value
        page_size = DEFAULT_PAGE_SIZE
    return {"page_size": page_size}


def set_page_size(conn: sqlite3.Connection, page_size: int, *, now: datetime) -> dict[str, int]:
    """Persist ``page_size`` (caller validates against :data:`ALLOWED_PAGE_SIZES`)."""
    ensure_ui_prefs_seeded(conn)
    conn.execute(
        "INSERT INTO ui_prefs_config (id, page_size, updated_at) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET page_size = excluded.page_size, "
        "updated_at = excluded.updated_at",
        (page_size, now.isoformat()),
    )
    conn.commit()
    return {"page_size": page_size}
