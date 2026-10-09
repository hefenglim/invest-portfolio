"""UI preferences (WPC, 2026-07-07): a tiny DB-backed single-row config.

Backend-persisted global display preferences — ``page_size`` (every pager surface clamps
against its endpoint's own max) and, since 2026-09-16, ``auto_ai_resolve`` (demo audit
L13, owner ruling 3(b)+(d): whether the 觀察清單 quick-add fires the paid AI resolver by
itself when a NAME-like input finds no quote; a code-like input never fires it either way).
Follows the ``config_store`` create-always/seed-once pattern (same shape as
``system_prompt_config``: one row, id=1). Lives in ``shared/`` (imports nothing internal
beyond ``config_store``) so any layer may read it; only the api router writes it.
"""

import sqlite3
from datetime import datetime
from typing import Any

from portfolio_dash.shared import config_store

_CATEGORY = "ui_prefs"

DEFAULT_PAGE_SIZE = 50
ALLOWED_PAGE_SIZES = (20, 50, 100, 200)
DEFAULT_AUTO_AI_RESOLVE = True

_DDL = (
    "CREATE TABLE IF NOT EXISTS ui_prefs_config "
    "(id INTEGER PRIMARY KEY CHECK (id = 1), page_size INTEGER NOT NULL, "
    "updated_at TEXT NOT NULL, auto_ai_resolve INTEGER NOT NULL DEFAULT 1)"
)


def _create(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)
    # Additive migration for a table created before the column existed (create-always
    # means this runs on every boot; ALTER only when the column is genuinely missing).
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(ui_prefs_config)")}
    if "auto_ai_resolve" not in cols:
        conn.execute(
            "ALTER TABLE ui_prefs_config ADD COLUMN auto_ai_resolve INTEGER NOT NULL DEFAULT 1"
        )


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO ui_prefs_config (id, page_size, updated_at, auto_ai_resolve) "
        "VALUES (1, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
        (DEFAULT_PAGE_SIZE, datetime(2026, 7, 7).isoformat(), int(DEFAULT_AUTO_AI_RESOLVE)),
    )


def ensure_ui_prefs_seeded(conn: sqlite3.Connection) -> None:
    """Create the single-row table (always) and seed the default (once)."""
    config_store.ensure_seeded(conn, _CATEGORY, create=_create, seed=_seed)


def get_ui_prefs(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return ``{"page_size": N, "auto_ai_resolve": bool}``; defaults when the row is absent.

    READ-ONLY (DEF-065): no DDL, no seed, no commit. It used to call
    :func:`ensure_ui_prefs_seeded` first, so the FIRST read of a database — three at once
    on ``settings.html`` — created the table and seeded the row from a GET, and two of them
    racing ended in a 500. The table is created and seeded at boot (``api/app.py``'s
    lifespan) and by the write path (:func:`set_ui_prefs`); a database that has neither
    (a hermetic test, a legacy file before its first boot) reads the defaults.
    """
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ui_prefs_config'"
    ).fetchone() is None:
        return {"page_size": DEFAULT_PAGE_SIZE, "auto_ai_resolve": DEFAULT_AUTO_AI_RESOLVE}
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(ui_prefs_config)")}
    auto_col = "auto_ai_resolve" if "auto_ai_resolve" in cols else "NULL AS auto_ai_resolve"
    row = conn.execute(
        f"SELECT page_size, {auto_col} FROM ui_prefs_config WHERE id = 1"
    ).fetchone()
    page_size = int(row["page_size"]) if row is not None else DEFAULT_PAGE_SIZE
    if page_size not in ALLOWED_PAGE_SIZES:  # defensive: legacy/hand-edited value
        page_size = DEFAULT_PAGE_SIZE
    auto_ai = (bool(row["auto_ai_resolve"])
               if row is not None and row["auto_ai_resolve"] is not None
               else DEFAULT_AUTO_AI_RESOLVE)
    return {"page_size": page_size, "auto_ai_resolve": auto_ai}


def set_ui_prefs(
    conn: sqlite3.Connection,
    *,
    page_size: int | None = None,
    auto_ai_resolve: bool | None = None,
    now: datetime,
) -> dict[str, Any]:
    """Persist the given fields (subset merge); the caller validates ``page_size``."""
    ensure_ui_prefs_seeded(conn)  # the WRITE path creates what the read path only reads
    current = get_ui_prefs(conn)
    ps = int(current["page_size"]) if page_size is None else page_size
    auto = bool(current["auto_ai_resolve"]) if auto_ai_resolve is None else auto_ai_resolve
    conn.execute(
        "INSERT INTO ui_prefs_config (id, page_size, updated_at, auto_ai_resolve) "
        "VALUES (1, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET page_size = excluded.page_size, "
        "updated_at = excluded.updated_at, auto_ai_resolve = excluded.auto_ai_resolve",
        (ps, now.isoformat(), int(auto)),
    )
    conn.commit()
    return {"page_size": ps, "auto_ai_resolve": bool(auto)}


def set_page_size(conn: sqlite3.Connection, page_size: int, *, now: datetime) -> dict[str, Any]:
    """Persist ``page_size`` (caller validates against :data:`ALLOWED_PAGE_SIZES`)."""
    return set_ui_prefs(conn, page_size=page_size, now=now)
