"""`create_tables()` must survive two callers racing on a brand-new news.db.

`news.html` issues `GET /api/news` and `GET /api/news/filters` together. Each opens its
own connection and each calls `create_tables()`, so on a fresh `news.db` both can read a
column as absent and both can `ALTER TABLE … ADD COLUMN`. The loser used to raise
`sqlite3.OperationalError: duplicate column name: …` straight out of a plain GET — a 500
on the FIRST load of a fresh install that vanishes on reload. Three separate QA agents hit
it on three different columns (`model`, `fetch_status`, `fetch_attempts`); that spread is
the signature of a race, not of one bad column.

Reproducing it with N threads is unreliable — the ALTERs usually serialise such that every
PRAGMA already sees the previous one, and a test that only goes red sometimes is a test
that proves nothing. So the interleave is forced instead: `_RacingConnection` runs the
competing ALTER *inside* the PRAGMA call, which is exactly the window the bug lives in,
and makes the failure deterministic in both directions.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from portfolio_dash.news.store import _ADDED_COLUMNS, _add_column_if_missing, create_tables

_TABLE = "organized_news"


class _RacingConnection(sqlite3.Connection):
    """A connection whose first ``PRAGMA table_info`` answer is stale by construction.

    The PRAGMA is fully materialised and its read transaction released BEFORE the rival
    writes — an earlier draft of this test let the rival ALTER while the PRAGMA cursor was
    still open and got `database is locked`, i.e. it went red for the wrong reason. A red
    that comes from the wrong error proves nothing, so the lock is dropped first and the
    caller is then handed the rows as they were *before* the rival ran: exactly the view
    the loser of the real race holds when it decides to ALTER.
    """

    rival_path: str = ""
    rival_column: tuple[str, str] = ("", "")
    fired: bool = False

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        if self.fired or not sql.startswith("PRAGMA table_info"):
            return super().execute(sql, *args)
        self.fired = True
        rows = super().execute(sql, *args).fetchall()  # materialise, then…
        super().rollback()  # …release the read lock so the rival can write

        col, decl = self.rival_column
        rival = sqlite3.connect(self.rival_path)
        try:
            rival.execute(f"ALTER TABLE {_TABLE} ADD COLUMN {col} {decl}")
            rival.commit()
        finally:
            rival.close()

        # Hand back the PRE-rival rows. Kept on `self` so the scratch db outlives the call.
        self._stale = sqlite3.connect(":memory:")  # noqa: SLF001
        self._stale.row_factory = sqlite3.Row
        self._stale.execute("CREATE TABLE t(name TEXT)")
        self._stale.executemany(
            "INSERT INTO t(name) VALUES(?)", [(r["name"],) for r in rows]
        )
        return self._stale.execute("SELECT name FROM t")


def _fresh_db(tmp_path: Path) -> str:
    path = str(tmp_path / "news.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    create_tables(conn)  # table exists, every column present
    conn.close()
    return path


def _drop_column(path: str, col: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute(f"ALTER TABLE {_TABLE} DROP COLUMN {col}")
    conn.commit()
    conn.close()


@pytest.mark.parametrize("col,decl", _ADDED_COLUMNS, ids=[c for c, _ in _ADDED_COLUMNS])
def test_losing_the_add_column_race_is_not_an_error(tmp_path: Path, col: str, decl: str) -> None:
    """Every migrated column must tolerate a rival adding it first.

    Parametrised over the whole list rather than the three columns QA happened to observe:
    which column loses depends purely on which request arrives first, so pinning the
    observed three would certify three and let the next one fail on its own.
    """
    path = _fresh_db(tmp_path)
    _drop_column(path, col)

    conn = sqlite3.connect(path, factory=_RacingConnection)
    conn.row_factory = sqlite3.Row
    conn.rival_path = path
    conn.rival_column = (col, decl)
    try:
        _add_column_if_missing(conn, _TABLE, col, decl)  # must not raise
        conn.commit()
        names = {r["name"] for r in conn.execute(f"PRAGMA table_info({_TABLE})")}
    finally:
        conn.close()

    assert col in names, f"{col} is missing after losing the race — the post-condition broke"


def test_a_real_ddl_failure_still_raises(tmp_path: Path) -> None:
    """The race guard must not swallow anything but `duplicate column name`.

    Without this, a genuine schema error becomes a silent no-op and the column is simply
    absent forever — a far worse failure than the 500 being fixed.
    """
    path = _fresh_db(tmp_path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        with pytest.raises(sqlite3.OperationalError):
            _add_column_if_missing(conn, "no_such_table", "whatever", "TEXT")
    finally:
        conn.close()


def test_create_tables_is_idempotent_on_an_already_migrated_db(tmp_path: Path) -> None:
    """The ordinary path — no race — is unchanged: repeated calls stay clean."""
    path = _fresh_db(tmp_path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        create_tables(conn)
        create_tables(conn)
        names = {r["name"] for r in conn.execute(f"PRAGMA table_info({_TABLE})")}
    finally:
        conn.close()
    assert {c for c, _ in _ADDED_COLUMNS} <= names
