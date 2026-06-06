from collections.abc import Iterator
from pathlib import Path

import pytest

from portfolio_dash.shared import db
from portfolio_dash.shared.config import get_settings


@pytest.fixture
def tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(path))
    get_settings.cache_clear()
    yield path
    get_settings.cache_clear()


def test_get_connection_row_factory(tmp_db: Path) -> None:
    conn = db.get_connection()
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO t (name) VALUES ('x')")
        row = conn.execute("SELECT id, name FROM t").fetchone()
        assert row["name"] == "x"
    finally:
        conn.close()


def test_foreign_keys_pragma_on(tmp_db: Path) -> None:
    conn = db.get_connection()
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_session_commits_on_success(tmp_db: Path) -> None:
    with db.session() as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO t (id) VALUES (1)")
    conn2 = db.get_connection()
    try:
        assert conn2.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    finally:
        conn2.close()


def test_session_rolls_back_on_exception(tmp_db: Path) -> None:
    with db.session() as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    with pytest.raises(RuntimeError):
        with db.session() as conn:
            conn.execute("INSERT INTO t (id) VALUES (1)")
            raise RuntimeError("boom")
    conn2 = db.get_connection()
    try:
        assert conn2.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    finally:
        conn2.close()
