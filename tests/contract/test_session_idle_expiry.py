"""Session idle expiry (owner ruling 2026-10-22, review finding L5).

A login session expires after 30 days without use (sliding: every use extends it), and the
boot purges expired sessions. Login sessions never expired before this change.

The design constraint is DEF-065 — a GET never writes, on its own connection or by taking the
write lock. The expiry check is therefore a pure read inside the gate
(``auth_store.require_session``), and a USE is recorded by a separate step, the touch:

* at most once per ``SESSION_TOUCH_INTERVAL`` (24 h) per session — the gate arms it only
  when the stored stamp is older than that;
* once the page load has SETTLED (owner ruling 2026-10-22, 「續期延後，記完立刻預熱」): the gate
  arms the session's pending touch, which fires ``SESSION_TOUCH_SETTLE_S`` after the
  session's latest gated request (capped at ``SESSION_TOUCH_MAX_DEFER_S``), on its OWN
  short-lived connection with a ``SESSION_TOUCH_BUSY_TIMEOUT_MS`` busy timeout, as one
  autocommit compare-and-set UPDATE — and a recorded touch pre-warms at once;
* skipped when the write lock is held (a later request arms it again), and never raising.

Hermetic half: the golden in-memory client (``api_client``), whose injected ``get_conn`` the
touch reuses exactly as the action log does. File half: a real database file, where the
touch opens its own connection. Both drive the pending touch with ``touch_clock``
(``tests/conftest.py``), which fires it on the test's thread when the test moves the clock.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable, Iterator, MutableMapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_socket import disable_socket, enable_socket

from portfolio_dash.api import action_log
from portfolio_dash.api import auth_store as A
from portfolio_dash.api.app import create_app
from portfolio_dash.api.deps import get_now
from portfolio_dash.shared import db as shared_db
from portfolio_dash.shared.clock import app_now
from portfolio_dash.shared.config import get_settings
from tests.conftest import GOLDEN_NOW, TouchClock, init_golden_base

_USER, _PASS = "chiaming", "password123"
_DAY = timedelta(days=1)


# --- hermetic: the golden client -------------------------------------------------------------


def _login(client: TestClient) -> str:
    """Create the first user (guest mode lets it), log in at the client's clock; the token."""
    assert client.post("/api/users", json={"name": "家明", "username": _USER,
                                           "password": _PASS}).status_code == 201
    r = client.post("/api/auth/login", json={"username": _USER, "password": _PASS})
    assert r.status_code == 200, r.text
    token = r.cookies.get("pd_session")
    assert isinstance(token, str) and token
    return token


def _at(client: TestClient, now: datetime) -> None:
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_now] = lambda: now


def _seen(conn: sqlite3.Connection, token: str) -> str | None:
    row = conn.execute("SELECT last_seen_at FROM auth_sessions WHERE token = ?",
                       (token,)).fetchone()
    return None if row is None else row[0]


def test_an_expired_session_is_refused_with_the_zh_message(
    api_client: TestClient, golden_db: sqlite3.Connection, touch_clock: TouchClock
) -> None:
    _login(api_client)
    _at(api_client, GOLDEN_NOW + 29 * _DAY + timedelta(hours=23))
    assert api_client.get("/api/accounts").status_code == 200
    touch_clock.settle()
    token = api_client.cookies.get("pd_session")
    assert token is not None
    # 29 d 23 h recorded a use; measure the next 30 days from it
    seen = datetime.fromisoformat(_seen(golden_db, token) or "")
    assert seen == GOLDEN_NOW + 29 * _DAY + timedelta(hours=23)
    _at(api_client, seen + 30 * _DAY + timedelta(seconds=1))
    r = api_client.get("/api/accounts")
    assert r.status_code == 401
    assert r.json() == {"error": {"code": "unauthorized",
                                  "message": "登入已逾期（30 天未使用），請重新登入"}}
    touch_clock.settle()
    # the expired row is treated as gone: no use is recorded for it
    assert _seen(golden_db, token) == seen.isoformat()


def test_a_use_slides_the_expiry(
    api_client: TestClient, golden_db: sqlite3.Connection, touch_clock: TouchClock
) -> None:
    token = _login(api_client)
    _at(api_client, GOLDEN_NOW + 20 * _DAY)
    assert api_client.get("/api/accounts").status_code == 200
    assert _seen(golden_db, token) == GOLDEN_NOW.isoformat()  # not while the page loads
    touch_clock.settle()
    assert _seen(golden_db, token) == (GOLDEN_NOW + 20 * _DAY).isoformat()
    _at(api_client, GOLDEN_NOW + 45 * _DAY)  # 45 days after the login, 25 after its last use
    assert api_client.get("/api/accounts").status_code == 200


def test_a_use_is_recorded_at_most_once_a_day(
    api_client: TestClient, golden_db: sqlite3.Connection, touch_clock: TouchClock
) -> None:
    token = _login(api_client)
    login_stamp = _seen(golden_db, token)
    assert login_stamp == GOLDEN_NOW.isoformat()
    _at(api_client, GOLDEN_NOW + timedelta(hours=23, minutes=59, seconds=59))
    for _ in range(3):
        assert api_client.get("/api/accounts").status_code == 200
    touch_clock.settle()
    assert _seen(golden_db, token) == login_stamp
    _at(api_client, GOLDEN_NOW + timedelta(hours=24))
    assert api_client.get("/api/accounts").status_code == 200
    touch_clock.settle()
    assert _seen(golden_db, token) == (GOLDEN_NOW + timedelta(hours=24)).isoformat()


def test_a_stale_read_does_not_touch_again_within_the_interval(
    api_client: TestClient, golden_db: sqlite3.Connection, touch_clock: TouchClock
) -> None:
    """The in-process guard: once this process recorded a use, a request that still reads the
    old stamp (it read before the touch committed) arms no second touch that day."""
    token = _login(api_client)
    _at(api_client, GOLDEN_NOW + _DAY)
    assert api_client.get("/api/accounts").status_code == 200
    touch_clock.settle()
    assert _seen(golden_db, token) == (GOLDEN_NOW + _DAY).isoformat()
    golden_db.execute("UPDATE auth_sessions SET last_seen_at = ? WHERE token = ?",
                      (GOLDEN_NOW.isoformat(), token))  # what a stale reader sees
    golden_db.commit()
    _at(api_client, GOLDEN_NOW + _DAY + timedelta(hours=23))
    assert api_client.get("/api/accounts").status_code == 200
    assert not touch_clock.armed  # nothing pending
    touch_clock.settle()
    assert _seen(golden_db, token) == GOLDEN_NOW.isoformat()  # untouched: the guard held
    _at(api_client, GOLDEN_NOW + 2 * _DAY)
    assert api_client.get("/api/accounts").status_code == 200
    touch_clock.settle()
    assert _seen(golden_db, token) == (GOLDEN_NOW + 2 * _DAY).isoformat()


def test_a_request_answered_by_an_exception_handler_still_counts_as_a_use(
    api_client: TestClient, golden_db: sqlite3.Connection, touch_clock: TouchClock
) -> None:
    """The touch is armed by the gate, before the route runs: a request answered by an
    exception handler (a refused query parameter here: ``api/errors.py`` answers it 400) was a
    use all the same. Before the deferred touch it ran as a background task, which an
    exception handler's response never runs."""
    token = _login(api_client)
    _at(api_client, GOLDEN_NOW + _DAY)
    assert api_client.get("/api/dashboard", params={"trend_days": 0}).status_code == 400
    touch_clock.settle()
    assert _seen(golden_db, token) == (GOLDEN_NOW + _DAY).isoformat()


def test_a_stamp_from_the_future_is_refused(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    token = _login(api_client)
    golden_db.execute("UPDATE auth_sessions SET last_seen_at = ? WHERE token = ?",
                      ((GOLDEN_NOW + 400 * _DAY).isoformat(), token))
    golden_db.commit()
    _at(api_client, GOLDEN_NOW + 200 * _DAY)
    r = api_client.get("/api/accounts")
    assert r.status_code == 401 and r.json()["error"]["message"] == A.SESSION_EXPIRED_MESSAGE


@pytest.mark.parametrize("failure", [sqlite3.OperationalError("database is locked"),
                                     RuntimeError("anything else")])
def test_a_failing_touch_never_fails_the_request(
    api_client: TestClient, golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
    touch_clock: TouchClock, failure: Exception,
) -> None:
    token = _login(api_client)

    def broken(*_a: Any, **_k: Any) -> bool:
        raise failure

    monkeypatch.setattr(A, "touch_session", broken)
    _at(api_client, GOLDEN_NOW + _DAY)
    r = api_client.get("/api/accounts")
    assert r.status_code == 200, r.text
    touch_clock.settle()  # the touch fails on its timer: nothing raises, nothing recorded
    assert _seen(golden_db, token) == GOLDEN_NOW.isoformat()


def test_auth_session_tells_an_expired_login_from_a_signed_out_one(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _login(api_client)
    signed_in = api_client.get("/api/auth/session").json()
    assert signed_in == {"mode": "user", "username": _USER, "name": "家明", "locked": False}
    _at(api_client, GOLDEN_NOW + 30 * _DAY + timedelta(seconds=1))
    assert api_client.get("/api/auth/session").json() == {
        "mode": "user", "username": None, "name": None, "locked": False,
        "ended": "expired", "notice": "登入已逾期（30 天未使用），請重新登入"}
    # A cookie the store no longer knows (the boot purged it, the user was removed): the
    # login page can still say why the form is back.
    api_client.cookies.set("pd_session", "purged-token")
    assert api_client.get("/api/auth/session").json() == {
        "mode": "user", "username": None, "name": None, "locked": False,
        "ended": "invalid", "notice": "先前的登入已失效，請重新登入"}
    # No cookie at all: the answer is exactly what it was before this change.
    api_client.cookies.clear()
    assert api_client.get("/api/auth/session").json() == {
        "mode": "user", "username": None, "name": None, "locked": False}


def test_logout_still_works(api_client: TestClient, golden_db: sqlite3.Connection) -> None:
    token = _login(api_client)
    _at(api_client, GOLDEN_NOW + 10 * _DAY)
    assert api_client.post("/api/auth/logout").status_code == 204
    assert _seen(golden_db, token) is None  # the row is gone
    assert api_client.cookies.get("pd_session") is None  # and the cookie with it
    assert api_client.get("/api/auth/session").json() == {
        "mode": "user", "username": None, "name": None, "locked": False}
    assert api_client.get("/api/accounts").status_code == 401


def _clears_the_cookie(r: Any) -> bool:
    return any(h.startswith("pd_session=") and ("Max-Age=0" in h or "expires=" in h.lower())
               for h in r.headers.get_list("set-cookie"))


@pytest.mark.parametrize("cookie", ["expired", "unknown", None])
def test_logout_clears_an_expired_or_unknown_cookie(
    api_client: TestClient, golden_db: sqlite3.Connection, cookie: str | None
) -> None:
    """Logout is an open path: an expired cookie can be cleared (before, the gate answered
    401 and the cookie stayed). It needs only possession of the token; SameSite=Lax keeps a
    cross-site POST from carrying it. An unknown token or none at all is still a 204."""
    token = _login(api_client)
    api_client.cookies.clear()
    if cookie == "expired":
        _at(api_client, GOLDEN_NOW + 31 * _DAY)
        api_client.cookies.set("pd_session", token)
    elif cookie == "unknown":
        api_client.cookies.set("pd_session", "never-issued")
    r = api_client.post("/api/auth/logout")
    assert r.status_code == 204, r.text
    assert _clears_the_cookie(r), r.headers.get_list("set-cookie")
    if cookie == "expired":
        assert _seen(golden_db, token) is None  # the expired row went with it
    else:
        assert _seen(golden_db, token) == GOLDEN_NOW.isoformat()  # someone else's: untouched


def test_lock_of_an_expired_session_is_still_refused(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """Lock stays behind the gate: an expired session has nothing left to lock."""
    _login(api_client)
    _at(api_client, GOLDEN_NOW + 31 * _DAY)
    assert api_client.post("/api/auth/lock").status_code == 401


def _logged(golden_db: sqlite3.Connection, path: str) -> list[tuple[str | None, int]]:
    return [(r[0], int(r[1])) for r in golden_db.execute(
        "SELECT username, status FROM action_log WHERE path = ? ORDER BY id", (path,))]


def test_the_action_log_names_only_an_authenticated_login(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """The gate stores the login it let through on ``request.state``; the action log reads
    it from there. A request refused as expired (or locked) names nobody — before, the log
    looked the cookie up again, clock-free, and named the expired session's user."""
    action_log.ensure_table(golden_db)
    golden_db.execute("DELETE FROM action_log")
    _login(api_client)
    body = {"account_id": "tw_broker", "date": "2026-02-02", "kind": "deposit", "ccy": "TWD",
            "amount": "1000"}
    assert api_client.post("/api/cash/movements", json=body).status_code == 201
    _at(api_client, GOLDEN_NOW + 31 * _DAY)
    assert api_client.post("/api/cash/movements", json=body).status_code == 401
    assert _logged(golden_db, "/api/cash/movements") == [(_USER, 201), (None, 401)]
    # login names who logged in; logout names whose live session it ended, nobody otherwise
    assert _logged(golden_db, "/api/auth/login") == [(_USER, 200)]
    assert api_client.post("/api/auth/logout").status_code == 204  # the expired cookie
    assert api_client.post("/api/auth/login", json={"username": _USER,
                                                    "password": _PASS}).status_code == 200
    assert api_client.post("/api/auth/lock").status_code == 204
    assert api_client.post("/api/cash/movements", json=body).status_code == 401  # locked
    assert api_client.post("/api/auth/logout").status_code == 204  # the locked cookie
    assert _logged(golden_db, "/api/auth/logout") == [(None, 204), (None, 204)]
    assert _logged(golden_db, "/api/auth/lock") == [(_USER, 204)]
    assert _logged(golden_db, "/api/cash/movements")[-1] == (None, 401)
    api_client.post("/api/auth/login", json={"username": _USER, "password": _PASS})
    assert api_client.post("/api/auth/logout").status_code == 204  # a live session
    assert _logged(golden_db, "/api/auth/logout")[-1] == (_USER, 204)


def test_guest_mode_is_unchanged(api_client: TestClient, golden_db: sqlite3.Connection) -> None:
    """No authorised user: every route is open whatever the cookie, nothing is recorded."""
    golden_db.execute(
        "INSERT INTO auth_sessions (token, username, created_at, last_seen_at, locked) "
        "VALUES ('old', 'ghost', ?, ?, 0)",
        ((GOLDEN_NOW - 400 * _DAY).isoformat(), (GOLDEN_NOW - 400 * _DAY).isoformat()))
    golden_db.commit()
    api_client.cookies.set("pd_session", "old")
    assert api_client.get("/api/auth/session").json() == {"mode": "guest"}
    assert api_client.get("/api/accounts").status_code == 200
    assert _seen(golden_db, "old") == (GOLDEN_NOW - 400 * _DAY).isoformat()


# --- a real database file: the touch's own connection ----------------------------------------


@pytest.fixture
def protected_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A file-backed, protected database as the configured one (no lifespan)."""
    path = tmp_path / "auth.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    init_golden_base(conn)
    conn.execute("PRAGMA journal_mode = WAL")
    A.create_user(conn, name="家明", username=_USER, password=_PASS, now=GOLDEN_NOW)
    conn.commit()
    conn.close()
    monkeypatch.setenv("DB_PATH", str(path))
    get_settings.cache_clear()
    yield path
    get_settings.cache_clear()


def _file_session(path: Path, *, idle: timedelta) -> str:
    """A session last seen *idle* ago on the real clock (the app's own, no override)."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        token = A.create_session(conn, _USER, now=app_now() - idle)
    finally:
        conn.close()
    return token


def _file_seen(path: Path, token: str) -> str | None:
    conn = sqlite3.connect(path)
    try:
        return _seen(conn, token)
    finally:
        conn.close()


def _drive(app: FastAPI, path: str, token: str,
           on_send: Callable[[MutableMapping[str, Any]], None]) -> None:
    """One GET through the ASGI app, calling *on_send* with every message it sends."""
    scope: dict[str, Any] = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(),
        "root_path": "", "query_string": b"",
        "headers": [(b"cookie", f"pd_session={token}".encode())],
        "server": ("probe", 80), "client": ("probe", 1),
    }
    received = False

    async def receive() -> dict[str, Any]:
        nonlocal received
        if received:
            await asyncio.sleep(3600)
        received = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: MutableMapping[str, Any]) -> None:
        on_send(message)

    enable_socket()  # the event loop's self-pipe on Windows; the request is in-process
    try:
        asyncio.run(app(scope, receive, send))
    finally:
        disable_socket(allow_unix_socket=True)


def test_the_touch_waits_for_the_page_load_to_settle_on_its_own_connection(
    protected_db: Path, monkeypatch: pytest.MonkeyPatch, touch_clock: TouchClock
) -> None:
    """The response is complete (its last body message sent) before the use is recorded —
    and so is the page load: the use is recorded only once the session has been quiet for
    ``SESSION_TOUCH_SETTLE_S``. The connection that records it is not the request's: the
    request's connection runs no write and takes no write lock."""
    token = _file_session(protected_db, idle=timedelta(hours=25))
    before = _file_seen(protected_db, token)
    statements: dict[int, list[str]] = {}
    real = shared_db.connect_path

    def traced(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
        conn = real(path, read_only=read_only)
        log = statements.setdefault(len(statements), [])
        conn.set_trace_callback(log.append)
        return conn

    monkeypatch.setattr(shared_db, "connect_path", traced)
    at_end: list[str | None] = []

    def on_send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.start":
            assert message["status"] == 200
        if message["type"] == "http.response.body" and not message.get("more_body"):
            at_end.append(_file_seen(protected_db, token))

    _drive(create_app(), "/api/accounts", token, on_send)
    assert at_end == [before], "the use was recorded before the response was complete"
    touch_clock.advance(A.SESSION_TOUCH_SETTLE_S - 0.1)
    assert _file_seen(protected_db, token) == before, "recorded before the page load settled"
    touch_clock.advance(0.1)
    after = _file_seen(protected_db, token)
    assert after is not None and before is not None and after > before
    touchers = [i for i, log in statements.items()
                if any(s.lstrip().startswith("UPDATE auth_sessions") for s in log)]
    assert len(touchers) == 1, statements
    (touch_conn,) = touchers
    readers = [i for i, log in statements.items()
               if any("FROM auth_sessions WHERE token" in s for s in log)]
    assert readers and touch_conn not in readers, "the touch ran on the request's connection"
    for i in readers:
        assert not any(s.lstrip().upper().startswith(
            ("INSERT", "UPDATE", "DELETE", "REPLACE", "BEGIN IMMEDIATE", "BEGIN EXCLUSIVE"))
            for s in statements[i]), statements[i]
    # the touch's connection ran its pragmas and the one UPDATE, nothing else
    assert all(s.lstrip().upper().startswith(("PRAGMA", "UPDATE AUTH_SESSIONS"))
               for s in statements[touch_conn]), statements[touch_conn]


def test_a_held_write_lock_skips_the_touch_and_the_request_answers(
    protected_db: Path, touch_clock: TouchClock
) -> None:
    token = _file_session(protected_db, idle=timedelta(hours=25))
    before = _file_seen(protected_db, token)
    enable_socket()
    try:
        client = TestClient(create_app())
        client.cookies.set("pd_session", token)
        holder = sqlite3.connect(protected_db, isolation_level=None)
        try:
            holder.execute("BEGIN IMMEDIATE")
            r = client.get("/api/accounts")
            started = time.perf_counter()
            touch_clock.settle()  # the touch fires while the lock is still held
            elapsed = time.perf_counter() - started
            holder.execute("ROLLBACK")
        finally:
            holder.close()
        assert r.status_code == 200, r.text
        # the touch gave up after its own busy timeout, not the 5 s every other one waits
        assert elapsed < 3.0, f"the touch waited {elapsed:.1f} s behind the write lock"
        assert _file_seen(protected_db, token) == before  # skipped
        assert client.get("/api/accounts").status_code == 200  # a later request arms it again
        touch_clock.settle()
        after = _file_seen(protected_db, token)
        assert after is not None and before is not None and after > before
    finally:
        disable_socket(allow_unix_socket=True)


def test_a_request_that_records_a_use_prewarms_the_cache_once(
    protected_db: Path, monkeypatch: pytest.MonkeyPatch, touch_clock: TouchClock
) -> None:
    """The touch's commit moves ``PRAGMA data_version``, so the result cache is empty for the
    next read: the touch that recorded the use schedules the ordinary pre-warm AT ONCE (it
    waited for the page load itself); the requests after it (no touch due) schedule none."""
    from portfolio_dash.api import alert_inputs
    from portfolio_dash.shared import result_cache

    calls: list[tuple[object, float]] = []

    def record(fn: object, delay: float) -> bool:
        calls.append((fn, delay))
        return True

    monkeypatch.setenv("PD_PREWARM", "1")
    monkeypatch.setattr(result_cache, "schedule_prewarm", record)
    token = _file_session(protected_db, idle=timedelta(hours=25))
    enable_socket()
    try:
        client = TestClient(create_app())
        client.cookies.set("pd_session", token)
        for _ in range(3):  # one page load
            assert client.get("/api/accounts").status_code == 200
        assert calls == []  # nothing recorded while the page loads
        touch_clock.settle()
        assert calls == [(alert_inputs.prewarm_today, 0.0)]
        for _ in range(3):  # the next page: the use is recorded, no touch is due
            assert client.get("/api/accounts").status_code == 200
        touch_clock.settle()
    finally:
        disable_socket(allow_unix_socket=True)
    assert calls == [(alert_inputs.prewarm_today, 0.0)]


# --- the boot: upgrade and purge, through the real lifespan ----------------------------------


@pytest.fixture
def boot_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "boot.db"
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setenv("PD_DISABLE_SCHEDULER", "1")
    get_settings.cache_clear()
    enable_socket()
    try:
        yield path
    finally:
        disable_socket(allow_unix_socket=True)
        get_settings.cache_clear()


def _boot(caplog: pytest.LogCaptureFixture) -> A.SessionBoot:
    app = create_app()
    caplog.set_level(logging.INFO, logger="portfolio_dash.api.app")
    with TestClient(app):
        outcome = app.state.session_boot
    assert isinstance(outcome, A.SessionBoot)
    return outcome


def test_the_boot_dates_old_sessions_from_the_upgrade_and_purges_dead_ones(
    boot_db: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A database from before this change: its sessions have no `last_seen_at`. The first
    boot dates every one of them from the boot (nobody is logged out on deploy day), and purges
    the locked one (locked sessions are never revived: re-login issues a new token) and the
    one whose user is gone."""
    now = app_now()
    conn = sqlite3.connect(boot_db)
    conn.executescript(  # the auth schema as it stood through v0.1.32
        "CREATE TABLE auth_users (username TEXT PRIMARY KEY, name TEXT NOT NULL,"
        " password_hash TEXT NOT NULL, created_at TEXT NOT NULL);"
        "CREATE TABLE auth_sessions (token TEXT PRIMARY KEY, username TEXT NOT NULL,"
        " created_at TEXT NOT NULL, locked INTEGER NOT NULL DEFAULT 0);"
    )
    conn.execute("INSERT INTO auth_users VALUES (?, '家明', ?, ?)",
                 (_USER, A.hash_password(_PASS), now.isoformat()))
    old = (now - 90 * _DAY).isoformat()
    conn.executemany("INSERT INTO auth_sessions VALUES (?, ?, ?, ?)", [
        ("in-use", _USER, old, 0), ("locked", _USER, old, 1), ("orphan", "ghost", old, 0)])
    conn.commit()
    conn.close()

    first = _boot(caplog)
    assert (first.dated, first.expired, first.locked, first.orphaned) == (3, 0, 1, 1)
    assert any("auth sessions" in r.getMessage() and "2 purged" in r.getMessage()
               for r in caplog.records), [r.getMessage() for r in caplog.records]
    conn = sqlite3.connect(boot_db)
    conn.row_factory = sqlite3.Row
    rows = {r["token"]: r["last_seen_at"] for r in conn.execute("SELECT * FROM auth_sessions")}
    assert set(rows) == {"in-use"}
    assert datetime.fromisoformat(rows["in-use"]) >= now  # dated from the boot, not 90 d ago
    # 31 days later nobody used it: the next boot purges it
    conn.execute("UPDATE auth_sessions SET last_seen_at = ?",
                 ((app_now() - 31 * _DAY).isoformat(),))
    conn.commit()
    conn.close()
    second = _boot(caplog)
    assert (second.dated, second.expired, second.purged) == (0, 1, 1)
    assert _file_seen(boot_db, "in-use") is None


def test_a_session_with_a_non_text_stamp_does_not_stop_the_boot(
    boot_db: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A BLOB (or any non-text) stamp from a hand edit made ``fromisoformat`` raise TypeError,
    which escaped the boot purge, so the app did not start. It is expired and purged."""
    now = app_now()
    conn = sqlite3.connect(boot_db)
    conn.row_factory = sqlite3.Row
    A.create_auth_tables(conn)
    A.create_user(conn, name="家明", username=_USER, password=_PASS, now=now)
    blob = A.create_session(conn, _USER, now=now)
    future = A.create_session(conn, _USER, now=now + 400 * _DAY)
    live = A.create_session(conn, _USER, now=now)
    conn.execute("UPDATE auth_sessions SET last_seen_at = ? WHERE token = ?", (b"\x00", blob))
    conn.commit()
    conn.close()
    outcome = _boot(caplog)
    assert (outcome.expired, outcome.purged) == (2, 2)
    conn = sqlite3.connect(boot_db)
    try:
        left = {r[0] for r in conn.execute("SELECT token FROM auth_sessions")}
    finally:
        conn.close()
    assert left == {live} and not {blob, future} & left


def test_the_stop_cancels_a_pending_touch_before_the_cache_closes(
    boot_db: Path, monkeypatch: pytest.MonkeyPatch, touch_clock: TouchClock
) -> None:
    """The lifespan's stop cancels every pending touch BEFORE ``result_cache.shutdown()`` and
    the WAL checkpoint: a touch firing afterwards would commit past the checkpoint (``-wal``
    left beside the database) and pre-warm into a closed cache. The cancelled use is not
    recorded; the session's first request after the next boot records it."""
    from portfolio_dash.shared import result_cache

    conn = sqlite3.connect(boot_db)
    conn.row_factory = sqlite3.Row
    A.create_auth_tables(conn)
    A.create_user(conn, name="家明", username=_USER, password=_PASS, now=app_now())
    token = A.create_session(conn, _USER, now=app_now() - timedelta(hours=25))
    conn.close()
    before = _file_seen(boot_db, token)
    order: list[str] = []
    real_cancel, real_shutdown = A.cancel_pending_touches, result_cache.shutdown

    def cancel(timeout: float = A.SHUTDOWN_TOUCH_WAIT_S) -> int:
        cancelled = real_cancel(timeout)
        order.append(f"touches:{cancelled}")
        return cancelled

    def shutdown(timeout: float = result_cache.SHUTDOWN_PREWARM_WAIT_S) -> bool:
        order.append("cache")
        return real_shutdown(timeout)

    monkeypatch.setattr(A, "cancel_pending_touches", cancel)
    monkeypatch.setattr(result_cache, "shutdown", shutdown)
    with TestClient(create_app()) as client:
        client.cookies.set("pd_session", token)
        assert client.get("/api/accounts").status_code == 200
        assert len(touch_clock.armed) == 1
    assert order == ["touches:1", "cache"]
    touch_clock.settle()  # the cancelled timer never fires
    assert _file_seen(boot_db, token) == before
