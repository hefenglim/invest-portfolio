"""SQLite connection and session helpers (stdlib sqlite3, no ORM)."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .config import get_settings
from .flight import begin_write, end_write, note_write


def get_connection() -> sqlite3.Connection:
    """Open a SQLite connection to the configured db file.

    Ensures the parent directory exists, sets ``Row`` row factory, and enables
    foreign-key enforcement and WAL journaling.
    """
    settings = get_settings()
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    return connect_path(settings.db_path)


def connect_path(path: Path) -> sqlite3.Connection:
    """THE one raw open of the personal database file (``tests/architecture/
    test_db_open_surface.py`` pins it): ``Row`` rows, foreign keys, WAL.

    :func:`get_connection` opens the configured file through it; the result cache's dedicated
    connection and the settlement worker (which receives the path, not the settings) open
    theirs through it too (PERF, 2026-10-18), so every connection to the file is configured
    alike.
    """
    # check_same_thread=False: FastAPI runs the sync `get_conn` dependency in an anyio
    # threadpool, so a connection may be created on one worker thread and closed on
    # another (session() ENTER vs. finally: close() can straddle threads). Each request
    # gets its OWN connection — never shared or used concurrently across threads — so
    # only the create-vs-close thread can differ, with no concurrent access. Disabling
    # the same-thread guard is therefore safe and necessary here.
    conn = sqlite3.connect(path, check_same_thread=False)
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
        if conn.total_changes:
            # The commit runs inside a write window (`shared/flight.py`), so a caller arriving
            # just after the commit cannot join a build started before it. A request's
            # `get_conn` commits inside `api/app.py`'s window since PERF-S10 (function scope);
            # this window covers every session opened outside a request (threads, jobs).
            begin_write()
            try:
                conn.commit()
            finally:
                end_write()
        else:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        # A session that changed a row ends any sharing of a computation started before it
        # (`shared/flight.py`, 2026-10-13). Counted with or without the commit: a needless
        # bump only costs one shared build, a missed one could serve a pre-write answer.
        if conn.total_changes:
            note_write()
        conn.close()
