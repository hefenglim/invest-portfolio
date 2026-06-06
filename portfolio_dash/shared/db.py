"""SQLite connection and session helpers (stdlib sqlite3, no ORM)."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from .config import get_settings


def get_connection() -> sqlite3.Connection:
    """Open a SQLite connection to the configured db file.

    Ensures the parent directory exists, sets ``Row`` row factory, and enables
    foreign-key enforcement and WAL journaling.
    """
    settings = get_settings()
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")  # must be set per connection
    # WAL persists per database file; re-setting it per connection is harmless. It
    # falls back to 'memory' journaling for an in-memory db, which is fine in tests.
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    """Yield a connection that commits on success, rolls back on error, always closes.

    Note: Python's sqlite3 legacy transaction model (``isolation_level=""``) runs
    standalone DDL (CREATE/DROP TABLE, etc.) outside any transaction, so a
    ``rollback()`` after pure DDL is a no-op — those schema changes are permanent
    even if the session raises. DML that follows DDL within the same session *is*
    transactional (Python 3.12 no longer auto-commits before DDL). Use sessions for
    repository DML; treat schema migrations with this caveat in mind.
    """
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
