"""DEF-065: a GET never creates a table, seeds a row or takes SQLite's write lock.

The verifier's e2e runs failed 2 of 3 times on one console line — ``500 GET /api/ui-prefs``,
``internal_error`` — while every functional assertion passed. The traceback, reproduced
off-browser: ``IntegrityError: UNIQUE constraint failed: settings_meta.category`` at
``shared/config_store.py:33`` (1ee7771). ``get_ui_prefs`` called ``ensure_ui_prefs_seeded``
on every read; ``ui_prefs`` was never seeded at boot; ``settings.html`` fires three
``GET /api/ui-prefs`` at once (shell.js, settings-prefs.js, settings-llm.js); and
``config_store.ensure_seeded`` decided seed-once with a plain read before its INSERT, so two
first reads both seeded and the second INSERT collided. Measured: 5 of 200 rounds of three
concurrent first reads raised it.

The class, scanned by ENTRY POINT (every GET route, booted database, every SQL statement
traced on every connection): 22 of 81 GET routes wrote or created a table on their first
call, 15 took the write lock on EVERY call (``/api/dashboard`` among them: the strategy-
prompt version backfill ran two ``INSERT … SELECT`` + ``commit`` per read). Three guards:

* the one the verifier asked for — ``GET /api/ui-prefs`` answers (defaults / stored value)
  while another connection holds the write lock, on a database whose ui_prefs table does
  not exist yet (a GET that tried to create it would wait out the busy timeout, then 500);
* the primitive — two connections reaching a category's FIRST seed together, interleaved
  deterministically at the worst point, both succeed and the seed runs once;
* the class — boot a fresh database through the REAL lifespan, then call every GET route
  (enumerated from the route table, so a new route is covered the day it ships): none
  writes, none changes the schema, and each still answers (< 500) while other connections
  hold the write lock on BOTH database files.

``CREATE TABLE IF NOT EXISTS`` on a table that exists is still executed by 40 GET routes
(the ``ensure_*`` helpers they call). It is resolved at prepare time and takes no lock —
the third guard is exactly the proof, since it runs them under a held write lock.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pytest_socket import disable_socket, enable_socket

from portfolio_dash.api.app import create_app
from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.shared import config_store
from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.ui_prefs import set_ui_prefs
from tests.conftest import GOLDEN_NOW, _seed_golden

_WRITE = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP)\b", re.I)
_NOOP_DDL = re.compile(r"^\s*CREATE\s+(TABLE|INDEX|UNIQUE\s+INDEX)\s+IF\s+NOT\s+EXISTS\b", re.I)
_BUSY_S = 0.5  # app connections give up this fast in these tests (production: 5 s)


@pytest.fixture
def db_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    db = tmp_path / "portfolio.db"
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("PD_DISABLE_SCHEDULER", "1")
    get_settings.cache_clear()
    enable_socket()
    try:
        yield db
    finally:
        disable_socket(allow_unix_socket=True)
        get_settings.cache_clear()


def _short_busy_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every connection the app opens gives up on a held lock after ``_BUSY_S``."""
    real = sqlite3.connect

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        kwargs["timeout"] = _BUSY_S
        conn: sqlite3.Connection = real(*args, **kwargs)
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect)


class _WriteLock:
    """Hold SQLite's write lock (``BEGIN IMMEDIATE``) on each file for the with-block."""

    def __init__(self, *paths: Path) -> None:
        self._conns = [sqlite3.connect(str(p), isolation_level=None) for p in paths if p.exists()]

    def __enter__(self) -> _WriteLock:
        for c in self._conns:
            c.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, *exc: object) -> None:
        for c in self._conns:
            c.execute("ROLLBACK")
            c.close()


def _schema(*paths: Path) -> set[str]:
    out: set[str] = set()
    for p in paths:
        if not p.exists():
            continue
        c = sqlite3.connect(str(p))
        try:
            rows = c.execute("SELECT name, sql FROM sqlite_master").fetchall()
            out |= {f"{p.name}:{r[0]}:{r[1]}" for r in rows}
        finally:
            c.close()
    return out


# --- 1. the verifier's route ---------------------------------------------------------------


def test_get_ui_prefs_answers_under_a_held_write_lock_without_creating_its_table(
    db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = sqlite3.connect(str(db_file))
    conn.execute("PRAGMA journal_mode = WAL")  # as every app connection leaves the file
    bootstrap_db(conn)  # a database that has never been booted by this version: no ui_prefs
    conn.close()
    _short_busy_timeout(monkeypatch)
    # no lifespan: nothing seeds ui_prefs behind our back; a server error is a 500, not a raise
    client = TestClient(create_app(), raise_server_exceptions=False)
    before = _schema(db_file)
    with _WriteLock(db_file):
        r = client.get("/api/ui-prefs")
    assert r.status_code == 200, r.text
    assert r.json() == {"page_size": 50, "auto_ai_resolve": True}
    assert _schema(db_file) == before, "a GET created a table"
    # A stored value is read back the same way — also while another connection writes.
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    set_ui_prefs(conn, page_size=100, auto_ai_resolve=False, now=GOLDEN_NOW)
    conn.close()
    with _WriteLock(db_file):
        r = client.get("/api/ui-prefs")
    assert r.status_code == 200, r.text
    assert r.json() == {"page_size": 100, "auto_ai_resolve": False}


# --- 2. the primitive ----------------------------------------------------------------------


def test_two_first_seeds_at_once_both_succeed_and_seed_once(tmp_path: Path) -> None:
    """Thread A is stopped INSIDE its seed — after it read "not seeded", the old code's race
    window — while thread B runs the whole ensure_seeded. The old code let B seed and commit
    and then A's settings_meta INSERT raised IntegrityError; now A holds the write lock from
    its re-read on, so B waits (up to its busy timeout) and then finds the category seeded."""
    db = tmp_path / "race.db"
    sqlite3.connect(str(db)).close()
    calls: list[str] = []
    a_in_seed = threading.Event()
    release_a = threading.Event()
    errors: dict[str, BaseException] = {}

    def create(c: sqlite3.Connection) -> None:
        c.execute("CREATE TABLE IF NOT EXISTS demo_config (id INTEGER PRIMARY KEY, v TEXT)")

    def seed_for(name: str) -> config_store.SeedFn:
        def seed(c: sqlite3.Connection) -> None:
            calls.append(name)
            if name == "A":
                a_in_seed.set()
                release_a.wait(timeout=10)
            c.execute("INSERT INTO demo_config (id, v) VALUES (1, ?) "
                      "ON CONFLICT(id) DO NOTHING", (name,))
        return seed

    def run(name: str) -> None:
        c = sqlite3.connect(str(db), timeout=10, check_same_thread=False)
        try:
            config_store.ensure_seeded(c, "demo", create=create, seed=seed_for(name))
        except BaseException as exc:  # noqa: BLE001 - recorded and asserted below
            errors[name] = exc
        finally:
            c.close()

    ta = threading.Thread(target=run, args=("A",))
    ta.start()
    assert a_in_seed.wait(timeout=10)
    tb = threading.Thread(target=run, args=("B",))
    tb.start()
    tb.join(timeout=0.5)  # old code: B finishes here; new code: B is waiting for A's lock
    release_a.set()
    ta.join(timeout=15)
    tb.join(timeout=15)
    assert not errors, {k: repr(v) for k, v in errors.items()}
    assert calls == ["A"], f"the seed ran {len(calls)} times: {calls}"
    c = sqlite3.connect(str(db))
    try:
        assert c.execute("SELECT COUNT(*) FROM settings_meta WHERE category = 'demo'"
                         ).fetchone()[0] == 1
    finally:
        c.close()


# --- 3. the class --------------------------------------------------------------------------

_PARAMS = {"symbol": "2330", "account": "tw_broker", "job_id": "alert_scan"}
_QUERY = {"/api/cash/statement": {"account": "tw_broker"}}


def _get_routes(app: Any) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or "GET" not in route.methods:
            continue
        if not route.path.startswith("/api"):
            continue
        path = route.path
        for name in re.findall(r"{(\w+)(?::\w+)?}", path):
            path = re.sub(r"{" + name + r"(?::\w+)?}", _PARAMS.get(name, "1"), path)
        out.append((route.path, path))
    return out


def test_no_get_route_writes_or_creates_after_boot_and_all_answer_under_a_write_lock(
    db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    news_db = db_file.parent / "news.db"
    traced: dict[str, list[str]] = defaultdict(list)
    current = ["boot"]
    real = sqlite3.connect

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        kwargs["timeout"] = _BUSY_S
        conn: sqlite3.Connection = real(*args, **kwargs)
        conn.set_trace_callback(lambda sql: traced[current[0]].append(sql))
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect)
    app = create_app()
    # The REAL lifespan (the first-run boot); a server error answers 500 so every route is seen.
    with TestClient(app, raise_server_exceptions=False) as client:
        seed = real(str(db_file))
        seed.row_factory = sqlite3.Row
        _seed_golden(seed)
        seed.commit()
        seed.close()
        routes = _get_routes(app)
        assert len(routes) >= 60, f"route enumeration broke: {len(routes)}"
        writers: list[str] = []
        failing: list[str] = []
        before = _schema(db_file, news_db)
        for template, path in routes:
            current[0] = template
            with _WriteLock(db_file, news_db):
                r = client.get(path, params=_QUERY.get(template, {}))
            if r.status_code >= 500:
                failing.append(f"{template} -> {r.status_code} {r.text[:160]}")
            writes = [s.strip()[:120] for s in traced[template]
                      if _WRITE.match(s) and not _NOOP_DDL.match(s)]
            if writes:
                writers.append(f"{template}: {writes[:3]}")
        after = _schema(db_file, news_db)
    assert not writers, "GET routes that write:\n" + "\n".join(writers)
    assert after == before, f"GET routes changed the schema: {sorted(after ^ before)[:8]}"
    assert not failing, "GET routes that fail while another connection writes:\n" + "\n".join(
        failing)
