"""Concurrent identical dashboard builds run once; a write is never hidden (2026-10-13).

`build_dashboard` shares a build that is RUNNING with callers that ask for the same one
(`shared/flight.py`). What must never happen is a page reloaded after 儲存 receiving an answer
computed before the save — so the key is pinned for every case it must refuse to share, and
both write signals (the HTTP write window, a session that changed a row) are pinned at
their seams.
"""

import sqlite3
import threading
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from pytest_socket import disable_socket, enable_socket

from portfolio_dash.api import app as app_module
from portfolio_dash.api.app import create_app
from portfolio_dash.api.deps import get_conn
from portfolio_dash.portfolio import dashboard
from portfolio_dash.portfolio.dashboard import _flight_key, build_dashboard
from portfolio_dash.portfolio.dashboard_models import DashboardData
from portfolio_dash.shared import db as db_module
from portfolio_dash.shared import flight
from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.db import session
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.flight import begin_write, end_write, write_generation
from tests.conftest import GOLDEN_NOW, _seed_golden, init_golden_base

TWD = Currency.TWD


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture
def db_file(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "ledger.db"
    conn = _connect(path)
    init_golden_base(conn)
    _seed_golden(conn)
    conn.commit()
    conn.close()
    yield path


def test_the_key_shares_only_a_committed_file_database_outside_a_write(db_file: Path) -> None:
    conn = _connect(db_file)
    try:
        key = _flight_key(conn, GOLDEN_NOW, TWD)
        assert key is not None
        assert Path(key[1]).resolve() == db_file.resolve()
        # another day, another currency, another write generation: another build
        assert _flight_key(conn, GOLDEN_NOW + timedelta(days=1), TWD) != key
        assert _flight_key(conn, GOLDEN_NOW, Currency.USD) != key
        flight.note_write()
        assert _flight_key(conn, GOLDEN_NOW, TWD) != key
        # a write in progress anywhere: nobody shares
        begin_write()
        try:
            assert _flight_key(conn, GOLDEN_NOW, TWD) is None
        finally:
            end_write()
        # the caller's own uncommitted change must be in its answer: it builds alone
        conn.execute("UPDATE accounts SET name = name")
        assert conn.in_transaction
        assert _flight_key(conn, GOLDEN_NOW, TWD) is None
        conn.rollback()
    finally:
        conn.close()


def test_an_in_memory_database_is_never_shared() -> None:
    conn = sqlite3.connect(":memory:")
    assert _flight_key(conn, GOLDEN_NOW, TWD) is None
    conn.close()


def test_two_concurrent_requests_build_once_and_each_owns_its_answer(
    db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()
    started = threading.Event()
    runs = [0]
    real = dashboard._build_dashboard

    def gated(conn: sqlite3.Connection, *, now: Any, reporting: Currency) -> DashboardData:
        runs[0] += 1
        started.set()
        assert release.wait(10)
        return real(conn, now=now, reporting=reporting)

    monkeypatch.setattr(dashboard, "_build_dashboard", gated)
    out: dict[str, DashboardData] = {}

    def request(name: str) -> None:
        conn = _connect(db_file)               # each request has its own connection
        try:
            out[name] = build_dashboard(conn, now=GOLDEN_NOW, reporting=TWD)
        finally:
            conn.close()

    first = threading.Thread(target=request, args=("first",))
    first.start()
    assert started.wait(10)
    second = threading.Thread(target=request, args=("second",))
    second.start()
    deadline = time.monotonic() + 10
    while not any(f.joiners for f in flight._flights.values()):
        assert time.monotonic() < deadline, "the second request never joined"
        time.sleep(0.005)
    release.set()
    first.join(30)
    second.join(30)

    assert runs[0] == 1
    assert out["first"] == out["second"]
    assert out["first"] is not out["second"]
    assert flight._flights == {}


def test_a_session_that_changed_a_row_ends_sharing_and_a_read_does_not() -> None:
    before = write_generation()
    with session() as conn:
        conn.execute("SELECT 1").fetchone()
    assert write_generation() == before
    with session() as conn:
        conn.execute("CREATE TEMP TABLE probe (x)")
        conn.execute("INSERT INTO probe VALUES (1)")
    after = write_generation()
    assert before is not None and after is not None and after > before


def _generation_at_every_commit(monkeypatch: pytest.MonkeyPatch) -> list[int | None]:
    """Record :func:`write_generation` at each COMMIT a ``shared.db.session`` issues."""
    seen: list[int | None] = []
    real = db_module.get_connection

    def on_sql(sql: str) -> None:
        if sql.strip().upper() == "COMMIT":
            seen.append(write_generation())

    def traced() -> sqlite3.Connection:
        conn = real()
        conn.set_trace_callback(on_sql)
        return conn

    monkeypatch.setattr(db_module, "get_connection", traced)
    return seen


def test_a_session_commits_its_changes_inside_a_write_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _generation_at_every_commit(monkeypatch)
    with session() as conn:
        conn.execute("CREATE TEMP TABLE probe (x)")
        conn.execute("INSERT INTO probe VALUES (1)")
    assert seen == [None]                   # nobody could join a build at the commit


def test_a_requests_own_commit_lands_inside_a_write_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Measured 2026-10-13: `get_conn` commits AFTER the HTTP middleware's window has closed
    # (FastAPI exits a yield dependency once the response has started), so the request's own
    # write is covered only by the session's window. Pinned through the real middleware stack
    # with a route that leaves its write for `get_conn` to commit (most store writers commit
    # inside the route, where the HTTP window still covers them).
    monkeypatch.setenv("DB_PATH", str(tmp_path / "portfolio.db"))
    monkeypatch.setenv("PD_DISABLE_SCHEDULER", "1")
    get_settings.cache_clear()
    app = create_app()

    def write(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, bool]:
        conn.execute("CREATE TEMP TABLE probe (x)")
        conn.execute("INSERT INTO probe VALUES (1)")
        return {"ok": True}

    app.add_api_route("/api/test-probe-write", write, methods=["POST"])
    app.router.routes.insert(0, app.router.routes.pop())   # ahead of the static mount at "/"
    seen = _generation_at_every_commit(monkeypatch)
    enable_socket()
    try:
        r = TestClient(app).post("/api/test-probe-write")
        assert r.status_code == 200, r.text
    finally:
        disable_socket(allow_unix_socket=True)
        get_settings.cache_clear()
    assert seen, "the request committed nothing"
    assert all(g is None for g in seen), seen


def test_every_mutating_request_opens_the_write_window_and_a_get_does_not(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(app_module, "begin_write", lambda: seen.append("begin"))
    monkeypatch.setattr(app_module, "end_write", lambda: seen.append("end"))

    api_client.get("/api/health")
    assert seen == []
    for method in ("post", "put", "patch", "delete"):
        getattr(api_client, method)("/api/no-such-route")
    assert seen == ["begin", "end"] * 4
