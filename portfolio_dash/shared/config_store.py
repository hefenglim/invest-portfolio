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
    """Ensure *category*'s tables exist (always) and are seeded (once).

    Seed-once is decided UNDER the write lock (DEF-065). The check used to be a plain read
    followed by the seed and the ``settings_meta`` INSERT, so two connections reaching a
    category's first use together both read "not seeded", both seeded, and the second
    INSERT raised ``IntegrityError: UNIQUE constraint failed: settings_meta.category`` —
    a 500 out of a plain GET. ``settings.html`` fires three ``GET /api/ui-prefs`` at once
    (shell.js, settings-prefs.js, settings-llm.js), and ``ui_prefs`` was never seeded at
    boot, so the first load of that page on a fresh database raced (measured: 5 of 200
    rounds of 3 concurrent first reads). Now the unseeded path takes the write lock
    (``BEGIN IMMEDIATE``), reads again, and seeds only if the category is still unseeded:
    the loser waits for the winner's commit, finds the row and does nothing. The steady
    state — already seeded — is unchanged and takes no lock.

    A caller already inside a transaction has written, so it already holds the write lock
    and the re-read is safe as is; that path keeps the historic ``commit()``.
    """
    conn.execute(_META_DDL)
    create(conn)
    if _is_seeded(conn, category):
        return
    own_txn = not conn.in_transaction
    if own_txn:
        conn.execute("BEGIN IMMEDIATE")
    try:
        if not _is_seeded(conn, category):
            # Claim FIRST, then seed: several seeds commit on their own (``seed_llm_defaults``,
            # ``ensure_job_rows``), and a commit ends the IMMEDIATE transaction. Written in
            # this order the claim is committed with (or before) the seed's own commit, so
            # the lock is never released while the category still reads as unseeded. A
            # seed that raises before committing rolls the claim back with it.
            conn.execute(
                "INSERT INTO settings_meta (category, seeded_at) VALUES (?, ?)",
                (category, datetime.now(UTC).isoformat()),
            )
            seed(conn)
        conn.commit()
    except BaseException:
        if own_txn:
            conn.rollback()
        raise


def _is_seeded(conn: sqlite3.Connection, category: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM settings_meta WHERE category = ?", (category,)
    ).fetchone() is not None


def restore_defaults(conn: sqlite3.Connection, category: str, *, seed: SeedFn) -> None:
    """Re-apply *category*'s default state by re-running its idempotent *seed*."""
    seed(conn)
    conn.commit()
