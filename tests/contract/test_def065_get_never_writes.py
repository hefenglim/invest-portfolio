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

Section 4 keeps the settlement store's change actor off every read connection. Section 5
(session idle expiry, owner ruling 2026-10-22) repeats the class guard in PROTECTED mode,
where the gate reads a session on every request: the request's connection still writes
nothing and takes no write lock, and the one reviewed exception — recording the session's
use (``auth_store.touch_after_response``) — is held to its exact statement, its own
connection, after the response, and once per touch interval.
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
from portfolio_dash.api.read_only import READ_ONLY_POSTS
from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.portfolio.settlement_store import install_or_verify
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
_QUERY = {"/api/cash/statement": {"account": "tw_broker"},
          # the recognition form (owner ruling 2026-10-10) reads the registry; the compose
          # form with no terms is refused before it reads anything
          "/api/input/option-symbol": {"text": "XYZ 12/18/2026 50.00 P"}}


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


# --- 4. the settlement store's change actor (spec 2026-10-18 §7, plan W1-B) ----------------

_TEMP_TRIGGERS = "SELECT COUNT(*) FROM temp.sqlite_master WHERE type = 'trigger'"
_READ_ONLY_BODIES: dict[str, dict[str, Any]] = {
    "/api/whatif": {"symbol": "2330", "side": "buy", "shares": "100", "price": "600",
                    "account_id": "tw_broker"},
    "/api/rebalance/preview": {"targets": {"2330": "0.30", "AAPL": "0.70"}},
    "/api/prompts/preview": {"body": "{{dividends_json}}", "scope": "portfolio",
                             "symbol": None},
}


def test_no_get_or_read_only_post_connection_carries_the_actor_trigger(
    db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The change actor's TEMP trigger exists only on a MUTATING request's connection: every
    connection a GET route or a declared read-only POST opens closes with an empty
    ``sqlite_temp_master``. A mutating request is the positive control (the probe sees it)."""
    seen: dict[str, list[int]] = defaultdict(list)
    current = ["boot"]
    real = sqlite3.connect

    class _Probe(sqlite3.Connection):
        def close(self) -> None:
            try:
                seen[current[0]].append(int(self.execute(_TEMP_TRIGGERS).fetchone()[0]))
            except sqlite3.Error:
                pass
            super().close()

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        kwargs.setdefault("factory", _Probe)
        conn: sqlite3.Connection = real(*args, **kwargs)
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect)
    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        seed = real(str(db_file))
        seed.row_factory = sqlite3.Row
        _seed_golden(seed)
        install_or_verify(seed, calc_version_changed=False)  # the trail and its triggers live
        seed.commit()
        seed.close()
        assert set(_READ_ONLY_BODIES) == {path for _m, path in READ_ONLY_POSTS}
        for template, path in _get_routes(app):
            current[0] = template
            client.get(path, params=_QUERY.get(template, {}))
        for method, path in sorted(READ_ONLY_POSTS):
            current[0] = f"{method} {path}"
            assert client.post(path, json=_READ_ONLY_BODIES[path]).status_code < 500
        current[0] = "control"
        r = client.post("/api/cash/movements", json={
            "account_id": "tw_broker", "date": "2026-02-02", "kind": "deposit", "ccy": "TWD",
            "amount": "1000"})
        assert r.status_code == 201, r.text
    carriers = sorted(k for k, counts in seen.items()
                      if k not in ("boot", "control") and any(counts))
    assert not carriers, f"read connections carrying the actor trigger: {carriers}"
    assert len(seen) > 60, "the probe saw too few requests"
    assert any(seen["control"]), "the probe must see the trigger on a mutating request"


# --- 5. protected mode: the session touch, the ONE reviewed write a GET may cause ----------
#
# Reviewed exception (owner ruling 2026-10-22, review finding L5 — sessions expire after 30
# days without use). Sliding expiry must record a use, and most uses are GETs. The record is
# the "touch" (`auth_store.touch_after_response`), and it is allowed ONLY in this shape:
#   * this exact statement (`_REVIEWED_TOUCH`), nothing else;
#   * on a connection that runs nothing but its pragmas and that statement — never the
#     request's connection, which still writes nothing and takes no write lock;
#   * after the response (a background task), at most once per SESSION_TOUCH_INTERVAL per
#     session, giving up after SESSION_TOUCH_BUSY_TIMEOUT_MS when the write lock is held.
# The guest-mode class test (3.) above has no exception at all: with no authorised user the
# gate reads no session and nothing is touched.

#: ``auth_store.TOUCH_SQL`` as the trace reports it (parameters expanded).
_REVIEWED_TOUCH = re.compile(
    r"^UPDATE auth_sessions SET last_seen_at = '[^']*' "
    r"WHERE token = '[^']*' AND locked = 0 AND last_seen_at IS ('[^']*'|NULL)$")
_LOCK_TAKER = re.compile(r"^\s*BEGIN\s+(IMMEDIATE|EXCLUSIVE)\b", re.I)
_OPEN = {"/api/auth/session", "/api/health"}  # outside the gate: no session is read


def test_protected_get_routes_write_nothing_and_the_touch_is_the_reviewed_exception(
    db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import timedelta

    from portfolio_dash.api import auth_store
    from portfolio_dash.shared.clock import app_now

    news_db = db_file.parent / "news.db"
    traced: list[tuple[str, int, str]] = []  # (route, connection number, statement)
    current = ["boot"]
    opened = [0]
    real = sqlite3.connect

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        kwargs["timeout"] = _BUSY_S
        conn: sqlite3.Connection = real(*args, **kwargs)
        number = opened[0]
        opened[0] += 1
        conn.set_trace_callback(lambda sql: traced.append((current[0], number, sql)))
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect)

    class _HeldLock(_WriteLock):
        """The lock holders, opened untraced: their BEGIN IMMEDIATE is the test's own."""

        def __init__(self, *paths: Path) -> None:
            self._conns = [real(str(p), isolation_level=None) for p in paths if p.exists()]

    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        seed = real(str(db_file))
        seed.row_factory = sqlite3.Row
        _seed_golden(seed)
        auth_store.create_user(seed, name="owner", username="owner", password="pw-123456",
                               now=GOLDEN_NOW)
        seed.commit()
        current[0] = "login"
        assert client.post("/api/auth/login", json={
            "username": "owner", "password": "pw-123456"}).status_code == 200
        due = (app_now() - timedelta(hours=7)).isoformat()  # a touch is due on every request
        seed.execute("UPDATE auth_sessions SET last_seen_at = ?", (due,))
        seed.commit()
        routes = _get_routes(app)
        assert len(routes) >= 60, f"route enumeration broke: {len(routes)}"
        failing: list[str] = []
        answered: set[str] = set()  # gated routes that answered 2xx: each one schedules a touch
        for template, path in routes:
            current[0] = template
            with _HeldLock(db_file, news_db):
                r = client.get(path, params=_QUERY.get(template, {}))
            if r.status_code >= 500 or r.status_code == 401:
                failing.append(f"{template} -> {r.status_code} {r.text[:160]}")
            elif r.status_code < 400 and template not in _OPEN:
                answered.add(template)
        stamp_under_lock = seed.execute("SELECT last_seen_at FROM auth_sessions").fetchone()[0]
        # with the lock released, one GET records the use, and the next ones do not
        current[0] = "free"
        assert client.get("/api/accounts").status_code == 200
        current[0] = "throttled"
        assert client.get("/api/accounts").status_code == 200
        assert client.get("/api/dashboard").status_code == 200
        stamp_after = seed.execute("SELECT last_seen_at FROM auth_sessions").fetchone()[0]
        seed.close()

    by_conn: dict[tuple[str, int], list[str]] = defaultdict(list)
    for route, number, sql in traced:
        by_conn[(route, number)].append(sql.strip())
    offenders: list[str] = []
    touch_conns: dict[str, int] = defaultdict(int)
    for (route, number), stmts in by_conn.items():
        if route in ("boot", "login"):
            continue
        writes = [s for s in stmts if _WRITE.match(s) and not _NOOP_DDL.match(s)]
        takers = [s for s in stmts if _LOCK_TAKER.match(s)]
        if not writes and not takers:
            continue
        reviewed = (not takers and all(_REVIEWED_TOUCH.match(w) for w in writes)
                    and all(_REVIEWED_TOUCH.match(s) or s.upper().startswith("PRAGMA")
                            for s in stmts))
        if reviewed:
            touch_conns[route] += 1
        else:
            offenders.append(f"{route} (connection {number}): {stmts[:4]}")
    assert not failing, "protected GET routes that fail while another connection writes:\n" + (
        "\n".join(failing))
    assert not offenders, "protected GET routes that write outside the reviewed touch:\n" + (
        "\n".join(offenders))
    # positive control: every gated route that answered attempted the touch under the lock
    # (once, on one connection of its own), and every attempt gave up
    assert len(answered) >= 50, f"too few routes answered: {sorted(answered)}"
    attempted = {route: n for route, n in touch_conns.items() if route in answered}
    assert attempted == dict.fromkeys(answered, 1), (
        f"routes without exactly one touch: {sorted(answered ^ set(attempted))} {attempted}")
    assert stamp_under_lock == due, "a touch went through while the write lock was held"
    assert touch_conns.get("free") == 1 and stamp_after > due
    assert "throttled" not in touch_conns, "a second touch inside the touch interval"
