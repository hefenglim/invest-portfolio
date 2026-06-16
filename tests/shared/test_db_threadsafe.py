"""Regression test for the FastAPI-threadpool cross-thread close bug (spec 19).

FastAPI runs sync dependencies (the `get_conn` dependency) in an anyio threadpool, so a
connection's create (``session()`` ENTER -> ``get_connection()``) and close
(``finally: conn.close()``) can run on DIFFERENT threadpool worker threads. With
sqlite3's default ``check_same_thread=True`` guard, closing on another thread raises
``sqlite3.ProgrammingError`` -> a 500 on every endpoint under the real subprocess
server. ``get_connection()`` opens with ``check_same_thread=False`` so create and close
may straddle threads safely (each request still owns its connection; no concurrent use).

These tests would raise ``sqlite3.ProgrammingError`` WITHOUT the fix and pass with it.
"""

import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from portfolio_dash.shared import db
from portfolio_dash.shared.config import get_settings


@pytest.fixture
def tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "threadsafe.db"
    monkeypatch.setenv("DB_PATH", str(path))
    get_settings.cache_clear()
    yield path
    get_settings.cache_clear()


def test_connection_closes_cleanly_on_different_thread(tmp_db: Path) -> None:
    """Open on the main thread, close on a worker thread: must NOT raise.

    This is the exact shape of the FastAPI-threadpool bug — without
    ``check_same_thread=False`` the close on another thread raises
    ``sqlite3.ProgrammingError``.
    """
    conn = db.get_connection()
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO t (id) VALUES (1)")

        with ThreadPoolExecutor(max_workers=1) as pool:
            # .result() re-raises any exception (incl. sqlite3.ProgrammingError) from
            # the worker thread, so a regression fails this test deterministically.
            pool.submit(conn.close).result()
    except sqlite3.ProgrammingError as exc:  # pragma: no cover - only without the fix
        pytest.fail(f"cross-thread close raised ProgrammingError: {exc}")


def test_session_straddling_threads_commits_and_closes(tmp_db: Path) -> None:
    """A session whose ENTER and EXIT run on different threads commits and closes cleanly.

    Mirrors how the anyio threadpool can run ``session()`` ENTER (connect) and the
    ``finally: conn.close()`` on different worker threads.
    """
    cm = db.session()

    # ENTER on a worker thread (connect + yield happen there).
    with ThreadPoolExecutor(max_workers=1) as enter_pool:
        conn = enter_pool.submit(cm.__enter__).result()
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO t (id) VALUES (1)")

    # EXIT (commit + close) on a DIFFERENT worker thread: must not raise.
    with ThreadPoolExecutor(max_workers=1) as exit_pool:
        try:
            exit_pool.submit(cm.__exit__, None, None, None).result()
        except sqlite3.ProgrammingError as exc:  # pragma: no cover - only without fix
            pytest.fail(f"cross-thread session exit raised ProgrammingError: {exc}")

    # The commit on the straddling exit must have persisted the row.
    verify = db.get_connection()
    try:
        assert verify.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    finally:
        verify.close()
