"""Generic DB-backed settings framework: create-always, seed-once, restore-to-default.

Reusable across config categories (``llm`` first; fees / accounts / prompts /
data_sources migrate onto the same primitive later). ``create`` must use
``CREATE TABLE IF NOT EXISTS`` so it is safe to run on every startup; ``seed`` runs
exactly once per category (tracked in ``settings_meta``).
"""

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

CreateFn = Callable[[sqlite3.Connection], None]
SeedFn = Callable[[sqlite3.Connection], None]

_META_DDL = (
    "CREATE TABLE IF NOT EXISTS settings_meta "
    "(category TEXT PRIMARY KEY, seeded_at TEXT NOT NULL)"
)


def ensure_seeded(
    conn: sqlite3.Connection, category: str, *, create: CreateFn, seed: SeedFn
) -> None:
    """Ensure *category*'s tables exist (always) and are seeded (once)."""
    conn.execute(_META_DDL)
    create(conn)
    seeded = conn.execute(
        "SELECT 1 FROM settings_meta WHERE category = ?", (category,)
    ).fetchone()
    if seeded is None:
        seed(conn)
        conn.execute(
            "INSERT INTO settings_meta (category, seeded_at) VALUES (?, ?)",
            (category, datetime.now(UTC).isoformat()),
        )
        conn.commit()


def restore_defaults(conn: sqlite3.Connection, category: str, *, seed: SeedFn) -> None:
    """Re-apply *category*'s default state by re-running its idempotent *seed*."""
    seed(conn)
    conn.commit()
